"""Integration tests for `POST /admin/tasks`."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import (
    EXAMPLE_KIND,
    browser_origin_hook,
    example_payload,
    mint_token_via_app,
)

REGISTER = {
    "hostname": "w",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


async def _register(client: AsyncClient, hostname: str = "w") -> tuple[str, str]:
    token = await mint_token_via_app()
    resp = await client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _set_worker_status(db_url: str, worker_id: str, status: str) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE workers SET status = :s WHERE id = :wid"),
                {"s": status, "wid": worker_id},
            )
    finally:
        await engine.dispose()


async def _set_worker_fraction(db_url: str, worker_id: str, fraction: float) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE workers SET free_disk_fraction = :f WHERE id = :wid"),
                {"f": fraction, "wid": worker_id},
            )
    finally:
        await engine.dispose()


async def _row(db_url: str, sql: str, params: dict[str, Any]) -> dict | None:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            r = (await conn.execute(text(sql), params)).mappings().first()
            return dict(r) if r else None
    finally:
        await engine.dispose()


async def _rows(db_url: str, sql: str, params: dict[str, Any]) -> list[dict]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            r = (await conn.execute(text(sql), params)).mappings().all()
            return [dict(x) for x in r]
    finally:
        await engine.dispose()


# --- target_mode='count' ---------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_target_1(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1"},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "queued"
    assert j["target"] == "1"
    assert j["target_mode"] == "count"
    assert j["target_count"] == 1

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count FROM tasks WHERE id=:tid",
        {"tid": j["task_id"]},
    )
    assert row == {"status": "queued", "target_mode": "count", "target_count": 1}


@pytest.mark.asyncio
async def test_create_task_target_5(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "5"},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "queued"
    assert j["target_count"] == 5
    assert j["target_mode"] == "count"


@pytest.mark.asyncio
async def test_create_task_default_target_is_1(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post("/admin/tasks", json={})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["target"] == "1"
    assert j["target_count"] == 1
    assert j["target_mode"] == "count"
    assert j["status"] == "queued"


# --- target_mode='all_available' -------------------------------------------


@pytest.mark.asyncio
async def test_create_all_available_empty_snapshot(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "dispatching"
    assert j["target_mode"] == "all_available"
    assert j["target_count"] == 0

    execs = await _rows(
        migrated_db_url,
        "SELECT worker_id, status FROM task_executions WHERE task_id=:tid",
        {"tid": j["task_id"]},
    )
    assert execs == []


@pytest.mark.asyncio
async def test_create_all_available_with_3_online_workers(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    await _register(authed_admin_client, "a")
    await _register(authed_admin_client, "b")
    await _register(authed_admin_client, "c")

    resp = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "dispatching"
    assert j["target_mode"] == "all_available"
    assert j["target_count"] == 3

    execs = await _rows(
        migrated_db_url,
        "SELECT status FROM task_executions WHERE task_id=:tid",
        {"tid": j["task_id"]},
    )
    assert len(execs) == 3
    assert all(e["status"] == "assigned" for e in execs)


@pytest.mark.asyncio
async def test_create_all_available_skips_offline_workers(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    w_a, _ = await _register(authed_admin_client, "a")
    w_b, _ = await _register(authed_admin_client, "b")
    w_c, _ = await _register(authed_admin_client, "c")
    await _set_worker_status(migrated_db_url, w_c, "offline")

    resp = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert resp.status_code == 200
    j = resp.json()
    assert j["target_count"] == 2

    execs = await _rows(
        migrated_db_url,
        "SELECT worker_id FROM task_executions WHERE task_id=:tid",
        {"tid": j["task_id"]},
    )
    worker_ids = {str(e["worker_id"]) for e in execs}
    assert worker_ids == {w_a, w_b}


@pytest.mark.asyncio
async def test_create_all_available_skips_disk_gated_workers(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The `all_available` snapshot skips the disk-gated worker.

    Step 5 of the pick only turns the `assigned` row into `claimed` for an
    `idle` worker, but a gated worker holds tasks back, so its pre-assignment
    would stay `assigned`, stuck and holding budget. The snapshot predicate
    excludes it because its `free_disk_fraction` is below the floor. A worker
    with a NULL fraction is included (compat with older workers).
    """
    w_a, _ = await _register(authed_admin_client, "a")  # NULL fraction → included
    w_gated, _ = await _register(authed_admin_client, "b")
    w_c, _ = await _register(authed_admin_client, "c")  # healthy → included
    await _set_worker_fraction(migrated_db_url, w_gated, 0.05)  # below the 0.20 floor
    await _set_worker_fraction(migrated_db_url, w_c, 0.5)  # above the floor

    resp = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["target_count"] == 2, "the gated worker must not be pre-assigned"

    execs = await _rows(
        migrated_db_url,
        "SELECT worker_id FROM task_executions WHERE task_id=:tid",
        {"tid": j["task_id"]},
    )
    worker_ids = {str(e["worker_id"]) for e in execs}
    assert worker_ids == {w_a, w_c}


# --- Validation ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "abc", "-1", "1.5", " 1", "01", ""])
@pytest.mark.asyncio
async def test_invalid_target_returns_422(authed_admin_client: AsyncClient, bad: str) -> None:
    resp = await authed_admin_client.post("/admin/tasks", json={"target": bad})
    assert resp.status_code == 422, (bad, resp.text)


# --- Integration with /v1/workers/{id}/poll --------------------------------


@pytest.mark.asyncio
async def test_count_task_then_worker_polls_it(authed_admin_client: AsyncClient) -> None:
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text
    task = poll.json()["task"]
    assert task is not None
    assert task["task_id"] == task_id
    assert task["command"] == "run_batch"


@pytest.mark.asyncio
async def test_all_available_preassigned_worker_claims_via_step3(
    authed_admin_client: AsyncClient,
) -> None:
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    j = create.json()
    assert j["target_count"] == 1

    # The pre-assigned worker must receive the task via /poll (step 5 SQL).
    poll = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text
    task = poll.json()["task"]
    assert task is not None
    assert task["task_id"] == j["task_id"]


# --- work tasks no longer require `command` --------------------------------


@pytest.mark.asyncio
async def test_create_task_without_command_succeeds(authed_admin_client: AsyncClient) -> None:
    """Work tasks no longer require `command`. Server defaults to 'run_batch'."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"payload": {"requirements": [], "commands": ["echo hello"]}, "target": "1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] > 0
    assert body["status"] == "queued"


@pytest.mark.asyncio
async def test_create_task_command_field_rejected(authed_admin_client: AsyncClient) -> None:
    """Passing `command` to POST /admin/tasks is a 422 (field removed from schema)."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"command": "echo", "payload": {}, "target": "1"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_poll_delivers_work_task_with_run_batch_command(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Work task delivered via /poll has command='run_batch'."""
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, payload, target_mode,
                                           target_count, priority, status, run_at,
                                           signed, signature)
                        VALUES (:workspace_id,
                                '{"requirements":[],"commands":["echo"]}'::jsonb,
                                'count', 1, 0, 'queued', now(),
                                true, decode(repeat('00', 64), 'hex'))
                        RETURNING id
                        """
                    ),
                    {"workspace_id": DEFAULT_WORKSPACE_ID},
                )
            ).first()
        task_id = int(row[0])
    finally:
        await engine.dispose()

    token = await mint_token_via_app()
    resp = await authed_admin_client.post(
        "/v1/workers/register",
        json={
            "hostname": "poll-w",
            "os": "linux",
            "arch": "x86_64",
            "ram_mb": 1024,
            "disk_mb": 10000,
            "tags": [],
            "capabilities": {},
            "enrollment_token": token,
        },
    )
    wid, key = resp.json()["worker_id"], resp.json()["api_key"]

    poll = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    t = poll.json()["task"]
    assert t is not None
    assert t["command"] == "run_batch"
    assert t["task_id"] == task_id


# --- idempotency_key: hardening --------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_key_all_available_returns_422(authed_admin_client: AsyncClient) -> None:
    """`idempotency_key` + `target='all-available'` → 422 (not supported)."""
    import uuid

    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "all-available", "idempotency_key": str(uuid.uuid4())},
    )
    assert resp.status_code == 422, resp.text
    assert "idempotency_key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_idempotency_key_all_ids_returns_422(authed_admin_client: AsyncClient) -> None:
    """`idempotency_key` + `target='all:<id>'` → 422.

    The guard runs BEFORE pool validation, so an arbitrary UUID is enough: what
    is exercised is the rejection by target_mode, not the existence of the
    worker.
    """
    import uuid

    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": f"all:{uuid.uuid4()}",
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422, resp.text
    assert "idempotency_key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_idempotency_key_same_payload_replays(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Same key + same payload (different order) → replay, a single row."""
    import uuid

    key = str(uuid.uuid4())
    first = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": {"a": 1, "b": 2}, "idempotency_key": key},
    )
    assert first.status_code == 200, first.text
    task_id = first.json()["task_id"]

    replay = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": {"b": 2, "a": 1}, "idempotency_key": key},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["task_id"] == task_id

    row = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM tasks WHERE idempotency_key = :k",
        {"k": key},
    )
    assert row is not None
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_idempotency_key_different_payload_returns_422(
    authed_admin_client: AsyncClient,
) -> None:
    """Same key + different payload (same workspace) → 422 without a leak."""
    import uuid

    key = str(uuid.uuid4())
    first = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": {"cmd": "echo one"}, "idempotency_key": key},
    )
    assert first.status_code == 200, first.text

    clash = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": {"cmd": "echo TWO"}, "idempotency_key": key},
    )
    assert clash.status_code == 422, clash.text
    assert clash.json()["detail"] == "idempotency_key reused with different payload"
    assert "echo one" not in clash.text


# --- idempotency_key: cross-workspace replay -------------------------------


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


async def _make_authed_client_for_workspace(workspace_id: str) -> AsyncClient:
    """Create an AsyncClient authenticated as owner of `workspace_id`.

    Inserts a User, makes it owner of the workspace and mints a session on the
    app's live engine. Returns a new AsyncClient (same ASGI app) with the
    `session` cookie already set. The caller closes the client.
    """
    from httpx import ASGITransport

    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.main import app
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES ('B-Admin') RETURNING id")
        )
        user_id = str(row.scalar_one())
        await add_member(s, workspace_id=workspace_id, user_id=user_id, role="owner")
        token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
        await s.commit()

    client = AsyncClient(
        event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"HX-Request": "true"},
    )
    client.cookies.set("session", token)
    return client


@pytest.mark.asyncio
async def test_create_task_idempotency_key_cross_workspace_409(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A key already used in ANOTHER workspace → 409 via `POST /admin/tasks`.

    Symmetric to `test_create_task_idempotency_key_cross_workspace_409` of the
    public v1 API, but exercised through the admin endpoint. The UNIQUE index on
    `idempotency_key` is global: returning the foreign task would be a
    cross-tenant leak. The admin replay resolver answers a generic 409 with
    `{"detail": "idempotency_key already in use"}` without leaking the other
    workspace's task_id.
    """
    import uuid

    key = str(uuid.uuid4())

    # Admin A (Default) creates the task with the key.
    first = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": {"cmd": "echo a"}, "idempotency_key": key},
    )
    assert first.status_code == 200, first.text
    task_a = first.json()["task_id"]

    # Admin B (another workspace) reuses the same key → 409, without leaking A's task.
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        clash = await client_b.post(
            "/admin/tasks",
            json={"target": "1", "payload": {"cmd": "echo b"}, "idempotency_key": key},
        )
    finally:
        await client_b.aclose()

    assert clash.status_code == 409, clash.text
    body = clash.json()
    assert body["detail"] == "idempotency_key already in use"
    # The message must not reveal the other tenant's task_id.
    assert str(task_a) not in body["detail"]

    # No second row with the key was inserted.
    count = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM tasks WHERE idempotency_key = :k",
        {"k": key},
    )
    assert count is not None
    assert count["n"] == 1


# --- variant derived from the payload --------------------------------------


@pytest.mark.asyncio
async def test_create_task_populates_variant_with_plugin(
    authed_admin_client: AsyncClient, migrated_db_url: str, example_plugin
) -> None:
    """With a plugin loaded, creating the task fills variant_kind/variant_label."""
    payload = {"requirements": [], **example_payload("example-tool run")}
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "1", "payload": payload})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    row = await _row(
        migrated_db_url,
        "SELECT variant_kind, variant_label FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert row is not None
    assert row["variant_kind"] == EXAMPLE_KIND
    assert row["variant_label"] == "1.2.3"


@pytest.mark.asyncio
async def test_create_task_without_plugins_variant_is_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Without plugins, the same payload ends up with a NULL variant (bucket `other`)."""
    payload = {"requirements": [], **example_payload("example-tool run")}
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "1", "payload": payload})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    row = await _row(
        migrated_db_url,
        "SELECT variant_kind, variant_label FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert row is not None
    assert row["variant_kind"] is None
    assert row["variant_label"] is None


@pytest.mark.asyncio
async def test_create_task_unrecognised_payload_variant_is_null(
    authed_admin_client: AsyncClient, migrated_db_url: str, example_plugin
) -> None:
    """A task that no extractor recognizes ends up with a NULL variant (bucket `other`)."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": {"requirements": [], "commands": ["echo hello"]}},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    row = await _row(
        migrated_db_url,
        "SELECT variant_kind, variant_label FROM tasks WHERE id = :id",
        {"id": task_id},
    )
    assert row is not None
    assert row["variant_kind"] is None
    assert row["variant_label"] is None
