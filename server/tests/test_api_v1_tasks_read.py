"""Contract tests: public `/api/v1` API, reading tasks.

`GET /api/v1/tasks` (cursor-paginated list, id desc; `done/total` from a single
GROUP BY over the page's executions, no N+1) and `GET /api/v1/tasks/{id}`
(TaskDetail, no inline stdout/stderr). Both inherit the multi-tenant isolation
of `WorkspaceContext`: cross-tenant -> 404.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import EXAMPLE_KIND, example_payload, mint_pat_via_app


def _db_url() -> str:
    from orcaster_server.config import settings

    return settings.database_url


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


async def _seed_worker(workspace_id: str) -> str:
    """Insert a minimal worker in the given workspace; returns its id."""
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            wid = (
                await conn.execute(
                    text(
                        "INSERT INTO workers (api_key_hash, hostname, os, arch, workspace_id) "
                        "VALUES (:h, 'host', 'linux', 'amd64', :ws) RETURNING id::text"
                    ),
                    {"h": uuid.uuid4().hex, "ws": workspace_id},
                )
            ).scalar_one()
            return str(wid)
    finally:
        await engine.dispose()


async def _seed_execution(task_id: int, worker_id: str, status: str, attempt: int = 1) -> None:
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO task_executions (task_id, worker_id, status, attempt_index) "
                    "VALUES (:t, :w, :s, :a)"
                ),
                {"t": task_id, "w": worker_id, "s": status, "a": attempt},
            )
    finally:
        await engine.dispose()


async def _create_task(client: AsyncClient, token: str, **body) -> int:
    resp = await client.post("/api/v1/tasks", headers=_bearer(token), json=body or {"payload": {}})
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


# ---------------------------------------------------------------------------
# GET /tasks: list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_empty(app_client: AsyncClient) -> None:
    """Workspace without tasks -> empty list and null next_cursor."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"tasks": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_list_tasks_returns_summaries_id_desc(app_client: AsyncClient) -> None:
    """Returns `TaskListRow` ordered by id desc, with the expected contract."""
    token, _ = await mint_pat_via_app()
    id1 = await _create_task(app_client, token, payload={"n": 1})
    id2 = await _create_task(app_client, token, payload={"n": 2})

    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    tasks = resp.json()["tasks"]
    assert [t["id"] for t in tasks] == [id2, id1]
    summary = tasks[0]
    expected_keys = {
        "id",
        "status",
        "target_mode",
        "target_count",
        "done",
        "total",
        "priority",
        "retry_count",
        "parent_task_id",
        "created_at",
        "completed_at",
        "outcome",
        # Task variant: always present in the row.
        "variant_kind",
        "variant_label",
    }
    assert set(summary.keys()) == expected_keys
    # `payload` is an opt-in projection: without `include_payload` the key is NOT present.
    assert "payload" not in summary
    assert summary["status"] == "queued"
    assert summary["done"] == 0
    assert summary["total"] == 0


@pytest.mark.asyncio
async def test_list_tasks_done_total_no_n_plus_1(app_client: AsyncClient) -> None:
    """done = completed; total = number of the task's executions (a single GROUP BY)."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    w1 = await _seed_worker_default()
    w2 = await _seed_worker_default()
    await _seed_execution(task_id, w1, "completed", attempt=1)
    await _seed_execution(task_id, w2, "failed", attempt=1)

    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    summary = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
    assert summary["done"] == 1
    assert summary["total"] == 2


@pytest.mark.asyncio
async def test_list_tasks_status_filter(app_client: AsyncClient) -> None:
    """`status` (repeatable) filters by task state."""
    token, _ = await mint_pat_via_app()
    queued_id = await _create_task(app_client, token)
    cancelled_id = await _create_task(app_client, token)
    await app_client.post(f"/api/v1/tasks/{cancelled_id}/cancel", headers=_bearer(token), json={})

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"status": "cancelled"}
    )
    assert resp.status_code == 200, resp.text
    ids = [t["id"] for t in resp.json()["tasks"]]
    assert cancelled_id in ids
    assert queued_id not in ids


@pytest.mark.asyncio
async def test_list_tasks_status_filter_multi_value(app_client: AsyncClient) -> None:
    """Repeated `status` with TWO valid values -> 200 and only those states.

    Confirms the `",".join(...)` (api/v1.py) + `string_to_array(:status_filter, ',')`
    (SQL) round trip: it splits and joins correctly and does not pull in
    tasks of other states (here a `completed` one outside the filter)."""
    token, _ = await mint_pat_via_app()
    queued_id = await _create_task(app_client, token)
    cancelled_id = await _create_task(app_client, token)
    await app_client.post(f"/api/v1/tasks/{cancelled_id}/cancel", headers=_bearer(token), json={})
    # Third task outside the filter: move it to 'completed' via direct SQL.
    completed_id = await _create_task(app_client, token)
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'completed' WHERE id = :id"),
                {"id": completed_id},
            )
    finally:
        await engine.dispose()

    resp = await app_client.get(
        "/api/v1/tasks",
        headers=_bearer(token),
        params=[("status", "queued"), ("status", "cancelled")],
    )
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tasks"]}
    assert queued_id in ids
    assert cancelled_id in ids
    assert completed_id not in ids


@pytest.mark.asyncio
async def test_list_tasks_status_invalid_value(app_client: AsyncClient) -> None:
    """`status` with a value outside the state enum -> 422."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"status": "flying"}
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_list_tasks_status_multi_value_one_invalid(app_client: AsyncClient) -> None:
    """Repeated `status`: if one value is invalid, the whole request -> 422."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.get(
        "/api/v1/tasks",
        headers=_bearer(token),
        params=[("status", "queued"), ("status", "banana")],
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_list_tasks_cursor_pagination(app_client: AsyncClient) -> None:
    """`limit` + `cursor` paginate by id desc; next_cursor null when exhausted."""
    token, _ = await mint_pat_via_app()
    ids = [await _create_task(app_client, token) for _ in range(3)]
    ids_desc = sorted(ids, reverse=True)

    page1 = await app_client.get("/api/v1/tasks", headers=_bearer(token), params={"limit": 2})
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert [t["id"] for t in body1["tasks"]] == ids_desc[:2]
    assert body1["next_cursor"] == ids_desc[1]

    page2 = await app_client.get(
        "/api/v1/tasks",
        headers=_bearer(token),
        params={"limit": 2, "cursor": body1["next_cursor"]},
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert [t["id"] for t in body2["tasks"]] == ids_desc[2:]
    assert body2["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_tasks_limit_validation(app_client: AsyncClient) -> None:
    """limit out of range (>200) -> 422."""
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token), params={"limit": 500})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_list_tasks_cross_tenant_invisible(app_client: AsyncClient) -> None:
    """A task from another workspace does not appear in the active PAT's list."""
    token, _ = await mint_pat_via_app()
    mine = await _create_task(app_client, token)

    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email=None, label="b")
    theirs = await _create_task(app_client, token_b)

    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    ids = [t["id"] for t in resp.json()["tasks"]]
    assert mine in ids
    assert theirs not in ids


@pytest.mark.asyncio
async def test_list_tasks_requires_token_401(app_client: AsyncClient) -> None:
    resp = await app_client.get("/api/v1/tasks")
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# GET /tasks/{id}: detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_task_detail(app_client: AsyncClient) -> None:
    """TaskDetail = summary + payload/eligibility/timeouts/executions (no stdout)."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(
        app_client,
        token,
        payload={"cmd": "x"},
        eligibility={"gpu": True},
        claim_timeout_seconds=30,
    )
    w1 = await _seed_worker_default()
    await _seed_execution(task_id, w1, "completed", attempt=1)

    resp = await app_client.get(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == task_id
    assert body["payload"] == {"cmd": "x"}
    assert body["eligibility"] == {"gpu": True}
    assert body["claim_timeout_seconds"] == 30
    assert body["done"] == 1
    assert body["total"] == 1
    assert len(body["executions"]) == 1
    ex = body["executions"][0]
    assert ex["worker_id"] == w1
    assert ex["status"] == "completed"
    assert ex["attempt_index"] == 1
    # stdout/stderr are NOT inline (they may be large)
    assert "stdout" not in ex
    assert "stderr" not in ex


@pytest.mark.asyncio
async def test_get_task_detail_not_found_404(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/api/v1/tasks/999999", headers=_bearer(token))
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_get_task_detail_cross_tenant_404(app_client: AsyncClient) -> None:
    """Detail of a task from another workspace -> 404 (without leaking existence)."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email=None, label="b")
    theirs = await _create_task(app_client, token_b)

    token, _ = await mint_pat_via_app()
    resp = await app_client.get(f"/api/v1/tasks/{theirs}", headers=_bearer(token))
    assert resp.status_code == 404, resp.text


async def _seed_worker_default() -> str:
    from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

    return await _seed_worker(DEFAULT_WORKSPACE_ID)


# ---------------------------------------------------------------------------
# Outcome tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_outcome_in_summary(app_client: AsyncClient) -> None:
    """The list row includes outcome when the task has a terminal outcome."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)

    # Update the task to completed with outcome='no_result' via direct SQL
    # (as is done in other state-mutation tests)
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'completed', outcome = 'no_result' WHERE id = :id"),
                {"id": task_id},
            )
    finally:
        await engine.dispose()

    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    summary = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
    assert "outcome" in summary, f"outcome not in row keys: {summary.keys()}"
    assert summary["outcome"] == "no_result"


@pytest.mark.asyncio
async def test_list_tasks_outcome_null_when_not_set(app_client: AsyncClient) -> None:
    """`TaskListRow.outcome` = null when there is no outcome."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)

    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    summary = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
    assert "outcome" in summary
    assert summary["outcome"] is None


@pytest.mark.asyncio
async def test_get_task_detail_outcome_in_summary(app_client: AsyncClient) -> None:
    """TaskDetail.outcome includes outcome when present."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)

    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'completed', outcome = 'no_result' WHERE id = :id"),
                {"id": task_id},
            )
    finally:
        await engine.dispose()

    resp = await app_client.get(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "outcome" in body
    assert body["outcome"] == "no_result"


@pytest.mark.asyncio
async def test_get_task_detail_executions_have_outcome(app_client: AsyncClient) -> None:
    """TaskExecutionSummary.outcome is exposed in the task detail."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    w1 = await _seed_worker_default()
    await _seed_execution(task_id, w1, "completed", attempt=1)

    # Update the execution to completed with outcome='no_result'
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE task_executions SET outcome = 'no_result' WHERE task_id = :id"),
                {"id": task_id},
            )
    finally:
        await engine.dispose()

    resp = await app_client.get(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["executions"]) == 1
    ex = body["executions"][0]
    assert "outcome" in ex, f"outcome not in TaskExecutionSummary keys: {ex.keys()}"
    assert ex["outcome"] == "no_result"


# ---------------------------------------------------------------------------
# Variant filter and optional payload in the list
# ---------------------------------------------------------------------------


def _tool_payload(version: str) -> dict:
    """Payload that the test plugin recognizes as `example-tool~<version>`.

    Tests that create tasks with a variant request the `example_plugin` fixture: the core
    ships no extractors and without it every task is `other`.
    """
    return example_payload(version=version)


@pytest.mark.asyncio
async def test_list_tasks_variant_fields_in_summary(
    app_client: AsyncClient, example_plugin
) -> None:
    """The row carries `variant_kind`/`variant_label`; null/null for the other bucket."""
    token, _ = await mint_pat_via_app()
    tool_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))
    other_id = await _create_task(app_client, token, payload={"n": 1})

    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    rows = {t["id"]: t for t in resp.json()["tasks"]}
    assert rows[tool_id]["variant_kind"] == EXAMPLE_KIND
    assert rows[tool_id]["variant_label"] == "1.10.0"
    assert rows[other_id]["variant_kind"] is None
    assert rows[other_id]["variant_label"] is None


@pytest.mark.asyncio
async def test_list_tasks_variant_default_is_all(app_client: AsyncClient, example_plugin) -> None:
    """Without `variant` NOTHING is filtered: the default is `all`, not `current`.

    This is the central trap: the `current` mode would hide the non-latest
    versions of each kind. The choice is explicit in the caller
    (`resolve_variant_filter(..., default_mode="all")`) instead of a helper default
    that each surface had to neutralize by hand.
    """
    token, _ = await mint_pat_via_app()
    old_id = await _create_task(app_client, token, payload=_tool_payload("1.2.0"))
    new_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))
    other_id = await _create_task(app_client, token, payload={"n": 1})

    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tasks"]}
    assert {old_id, new_id, other_id} <= ids

    explicit = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": "all"}
    )
    assert explicit.status_code == 200, explicit.text
    assert explicit.json() == resp.json()


@pytest.mark.asyncio
async def test_list_tasks_variant_pair_filter(app_client: AsyncClient, example_plugin) -> None:
    """`variant=<kind>~<label>` filters by that exact pair."""
    token, _ = await mint_pat_via_app()
    old_id = await _create_task(app_client, token, payload=_tool_payload("1.2.0"))
    new_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))
    other_id = await _create_task(app_client, token, payload={"n": 1})

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": f"{EXAMPLE_KIND}~1.2.0"}
    )
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tasks"]}
    assert ids == {old_id}
    assert new_id not in ids
    assert other_id not in ids


@pytest.mark.asyncio
async def test_list_tasks_variant_kind_filter(app_client: AsyncClient, example_plugin) -> None:
    """`variant=<kind>` returns the whole tool, without the other bucket."""
    token, _ = await mint_pat_via_app()
    old_id = await _create_task(app_client, token, payload=_tool_payload("1.2.0"))
    new_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))
    other_id = await _create_task(app_client, token, payload={"n": 1})

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": EXAMPLE_KIND}
    )
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tasks"]}
    assert ids == {old_id, new_id}
    assert other_id not in ids


@pytest.mark.asyncio
async def test_list_tasks_variant_other_filter(app_client: AsyncClient, example_plugin) -> None:
    """`variant=other` leaves only the tasks without a recognized variant."""
    token, _ = await mint_pat_via_app()
    tool_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))
    other_id = await _create_task(app_client, token, payload={"n": 1})

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": "other"}
    )
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tasks"]}
    assert other_id in ids
    assert tool_id not in ids


@pytest.mark.asyncio
async def test_list_tasks_variant_current_rejected_422(
    app_client: AsyncClient, example_plugin
) -> None:
    """`variant=current` -> 422: resolving it requires the set of present variants."""
    token, _ = await mint_pat_via_app()
    await _create_task(app_client, token, payload=_tool_payload("1.10.0"))

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": "current"}
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_token",
    [
        "example_tool~1.10.0",  # underscore instead of hyphen: the most likely typo
        "example_tool",
        "exampletool",
        "~1.10.0",  # the leading separator yields an empty kind
    ],
)
async def test_list_tasks_unknown_variant_kind_rejected_422(
    app_client: AsyncClient, example_plugin, bad_token: str
) -> None:
    """A kind outside the catalog -> 422, NOT an empty page with 200.

    Without the guard, a typo was indistinguishable from "there are no tasks in that variant", and
    an automation read it as an empty queue. The label is NOT validated (see the next case).
    """
    token, _ = await mint_pat_via_app()
    await _create_task(app_client, token, payload=_tool_payload("1.10.0"))

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": bad_token}
    )
    assert resp.status_code == 422, resp.text
    assert "unknown variant kind" in resp.text
    assert f"known kinds: {EXAMPLE_KIND}" in resp.text  # the message lists the catalog


@pytest.mark.asyncio
async def test_list_tasks_variant_kind_rejected_422_without_plugins(
    app_client: AsyncClient,
) -> None:
    """Without plugins the catalog is empty: every explicit kind -> 422.

    `all` and `other` are still accepted, because they do not depend on the registry.
    """
    token, _ = await mint_pat_via_app()
    other_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": EXAMPLE_KIND}
    )
    assert resp.status_code == 422, resp.text
    assert "unknown variant kind" in resp.text
    assert EXAMPLE_KIND not in resp.text.split("known kinds:", 1)[1]
    assert "known kinds: none (no plugin registers a variant)" in resp.text

    other = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": "other"}
    )
    assert other.status_code == 200, other.text
    assert other_id in {t["id"] for t in other.json()["tasks"]}


@pytest.mark.asyncio
async def test_list_tasks_unknown_variant_label_still_empty_page(
    app_client: AsyncClient, example_plugin
) -> None:
    """An unknown label with a valid kind still gives 200 and an empty page.

    This is correct: labels come from the payload (external, open data), so a
    label that is not in the table literally means that variant does not exist.
    Only the kind is a closed catalog, so only the kind is validated.
    """
    token, _ = await mint_pat_via_app()
    await _create_task(app_client, token, payload=_tool_payload("1.10.0"))

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"variant": f"{EXAMPLE_KIND}~1.10.99"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tasks"] == []


@pytest.mark.asyncio
async def test_list_tasks_include_payload_non_object_fails_explicitly(
    app_client: AsyncClient,
) -> None:
    """A stored `payload` that is not a JSON object -> 500 that NAMES the task.

    It is not degraded to `{}`: that value would be indistinguishable from a legitimately
    empty payload, and with `include_payload` the response feeds requeue decisions.
    The guard also does not touch the list without the flag, which does not project the column.
    """
    token, _ = await mint_pat_via_app()
    await _create_task(app_client, token, payload={"cmd": "x"})
    bad_id = await _create_task(app_client, token, payload={"cmd": "y"})

    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET payload = CAST(:p AS jsonb) WHERE id = :id"),
                {"p": "[1, 2]", "id": bad_id},
            )
    finally:
        await engine.dispose()

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"include_payload": "true"}
    )
    assert resp.status_code == 500, resp.text
    assert f"task {bad_id}" in resp.text
    assert "payload is not a JSON object" in resp.text

    plain = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert plain.status_code == 200, plain.text
    assert bad_id in {t["id"] for t in plain.json()["tasks"]}

    detail = await app_client.get(f"/api/v1/tasks/{bad_id}", headers=_bearer(token))
    assert detail.status_code == 500, detail.text
    assert f"task {bad_id}" in detail.text


@pytest.mark.asyncio
async def test_list_tasks_variant_empty_is_all(app_client: AsyncClient, example_plugin) -> None:
    """`variant=` (empty) does NOT fall into `current` mode: it is treated as `all`."""
    token, _ = await mint_pat_via_app()
    old_id = await _create_task(app_client, token, payload=_tool_payload("1.2.0"))
    new_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))

    resp = await app_client.get("/api/v1/tasks", headers=_bearer(token), params={"variant": ""})
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tasks"]}
    assert {old_id, new_id} <= ids


@pytest.mark.asyncio
async def test_list_tasks_variant_combines_with_status(
    app_client: AsyncClient, example_plugin
) -> None:
    """The variant filter composes with the status filter (same WHERE)."""
    token, _ = await mint_pat_via_app()
    queued_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))
    cancelled_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))
    await app_client.post(f"/api/v1/tasks/{cancelled_id}/cancel", headers=_bearer(token), json={})

    resp = await app_client.get(
        "/api/v1/tasks",
        headers=_bearer(token),
        params={"variant": f"{EXAMPLE_KIND}~1.10.0", "status": "queued"},
    )
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()["tasks"]}
    assert ids == {queued_id}


@pytest.mark.asyncio
async def test_list_tasks_include_payload(app_client: AsyncClient) -> None:
    """`include_payload=true` adds `payload` to each row; without the flag it is absent.

    EXACT equality of the key set (15), not just the presence of `payload`: this
    assert is the tripwire for the `TaskListResponse.tasks` union.
    `TaskListRowWithPayload` and `TaskListRow` are SIBLINGS on purpose; if
    someone collapsed them into a parent/child hierarchy, the union could pick the
    member without payload for a row that does carry it and pydantic would silently
    drop the key. Here that would show up as 14 keys instead of 15.
    """
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token, payload={"cmd": "x"})

    with_payload = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"include_payload": "true"}
    )
    assert with_payload.status_code == 200, with_payload.text
    row = next(t for t in with_payload.json()["tasks"] if t["id"] == task_id)
    assert row["payload"] == {"cmd": "x"}
    assert set(row.keys()) == {
        "id",
        "status",
        "target_mode",
        "target_count",
        "done",
        "total",
        "priority",
        "retry_count",
        "parent_task_id",
        "created_at",
        "completed_at",
        "outcome",
        "variant_kind",
        "variant_label",
        "payload",
    }

    without = await app_client.get("/api/v1/tasks", headers=_bearer(token))
    assert without.status_code == 200, without.text
    row_plain = next(t for t in without.json()["tasks"] if t["id"] == task_id)
    assert "payload" not in row_plain
    # Except for `payload`, the row is identical with and without the flag.
    assert {k: v for k, v in row.items() if k != "payload"} == row_plain


@pytest.mark.asyncio
async def test_list_tasks_include_payload_empty_payload(app_client: AsyncClient) -> None:
    """Task without payload -> `payload: {}` (same criterion as the detail)."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)

    resp = await app_client.get(
        "/api/v1/tasks", headers=_bearer(token), params={"include_payload": "true"}
    )
    assert resp.status_code == 200, resp.text
    row = next(t for t in resp.json()["tasks"] if t["id"] == task_id)
    assert row["payload"] == {}


@pytest.mark.asyncio
async def test_get_task_detail_has_variant_fields(app_client: AsyncClient, example_plugin) -> None:
    """The detail is still 200 and carries `variant_kind`/`variant_label`."""
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))

    resp = await app_client.get(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variant_kind"] == EXAMPLE_KIND
    assert body["variant_label"] == "1.10.0"
    # The detail's payload does not change shape.
    assert body["payload"] == _tool_payload("1.10.0")


@pytest.mark.asyncio
async def test_list_children_shape_unchanged(app_client: AsyncClient) -> None:
    """The children list does NOT change shape with the variant (outside the contract).

    EXACT equality of the key set, not just the absence of the three new ones. The
    shape is determined by two things that reinforce each other: the
    route's declared response model (`TaskChildListResponse` over
    `TaskChildRow`), which is what OpenAPI publishes, and `_LIST_CHILDREN_FIELDS`,
    derived from that same model, which is what is requested from the row. A column
    dropped from `_SQL_LIST_CHILDREN` no longer silently removes a key: it blows
    up with `NoSuchColumnError` naming the column. This assert remains the one that
    pins the exact set on the wire, which is what the client sees.
    """
    token, _ = await mint_pat_via_app()
    parent_id = await _create_task(app_client, token, payload=_tool_payload("1.10.0"))
    fork = await app_client.post(f"/api/v1/tasks/{parent_id}/fork", headers=_bearer(token), json={})
    assert fork.status_code == 201, fork.text

    resp = await app_client.get(f"/api/v1/tasks/{parent_id}/children", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    row = resp.json()["tasks"][0]
    assert set(row.keys()) == {
        "id",
        "status",
        "target_mode",
        "target_count",
        "done",
        "total",
        "priority",
        "retry_count",
        "parent_task_id",
        "created_at",
        "completed_at",
        "outcome",
    }


@pytest.mark.asyncio
async def test_list_tasks_variant_too_long_rejected_422(app_client: AsyncClient) -> None:
    """A variant token longer than `VARIANT_TOKEN_MAX_LEN` -> 422.

    Ingestion CLAMPS what it persists to that same cap (`clamp_variant`), so
    a longer token cannot match any stored variant:
    rejecting it does not hide rows.
    """
    from orcaster_server.task_variant import VARIANT_TOKEN_MAX_LEN

    token, _ = await mint_pat_via_app()
    resp = await app_client.get(
        "/api/v1/tasks",
        headers=_bearer(token),
        params={"variant": "x" * (VARIANT_TOKEN_MAX_LEN + 1)},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_insights_outcomes_variant_too_long_rejected_422(app_client: AsyncClient) -> None:
    """Same variant cap in `/insights/outcomes` as in `/tasks`.

    Without `max_length`, a 10 KB token would run `terminal_variant_counts` and be
    bound whole in `VARIANT_WHERE_SQL`, while the IDENTICAL token on `/tasks`
    was rejected before touching the DB. The cap is the same one with which ingestion
    CLAMPS what it persists (`clamp_variant`), so a longer token cannot
    match any stored variant and rejecting it does not hide rows.
    """
    from orcaster_server.task_variant import VARIANT_TOKEN_MAX_LEN

    token, _ = await mint_pat_via_app()
    resp = await app_client.get(
        "/api/v1/insights/outcomes",
        headers=_bearer(token),
        params={"variant": "x" * (VARIANT_TOKEN_MAX_LEN + 1)},
    )
    assert resp.status_code == 422, resp.text


def _resolve_schema(spec: dict, node: dict) -> dict:
    """Follow an OpenAPI `$ref` to the schema object it points to."""
    ref = node.get("$ref")
    if ref is None:
        return node
    assert ref.startswith("#/"), ref
    target: object = spec
    for part in ref.removeprefix("#/").split("/"):
        assert isinstance(target, dict), ref
        target = target[part]
    assert isinstance(target, dict), ref
    return target


@pytest.mark.asyncio
async def test_openapi_children_row_has_no_variant_or_payload(app_client: AsyncClient) -> None:
    """The children OpenAPI does NOT declare variant or payload.

    This is the root defect: `response_model_exclude_unset` is a SERIALIZATION
    flag and plays no part in schema generation, so the route would
    publish `variant_kind`/`variant_label`/`payload` as properties of its
    response while `_LIST_CHILDREN_FIELDS` guaranteed they never traveled. A
    client generated from the schema would expect impossible fields. The assert descends
    through the `$ref`s from the path instead of naming the component, so it stays valid
    if the model is renamed.
    """
    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/openapi.json", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    spec = resp.json()

    envelope_ref = spec["paths"]["/api/v1/tasks/{task_id}/children"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    envelope = _resolve_schema(spec, envelope_ref)
    row = _resolve_schema(spec, envelope["properties"]["tasks"]["items"])
    properties = set(row["properties"])
    assert {"payload", "variant_kind", "variant_label"}.isdisjoint(properties), properties
    assert "next_cursor" in envelope["properties"]


@pytest.mark.asyncio
async def test_openapi_info_version_matches_installed_package(app_client: AsyncClient) -> None:
    """`info.version` in `/openapi.json` reports the installed package, not a literal.

    `FastAPI(version=...)` used to carry a hard-coded literal and version bumps
    only edit `pyproject.toml`/`CHANGELOG.md`, so it never changed. `llms.txt` calls this
    document "the authoritative request and response shapes", so a
    client comparing versions against the authoritative source would receive a
    frozen number. The second assert reproduces the defect
    exactly: if the fix is reverted to the literal, `info.version` goes back to
    that literal and this line fails on its own, regardless of which version is installed.
    """
    from orcaster_server import package_version

    token, _ = await mint_pat_via_app()
    resp = await app_client.get("/openapi.json", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    info_version = resp.json()["info"]["version"]

    assert info_version == package_version()
    assert info_version != "0.1.0"
