"""Contract tests: public `/api/v1` API, single + bulk delete.

- `DELETE /tasks/{id}` reuses `delete_task_core` (204/409/404), hard delete with
  CASCADE to `task_executions`, byte purge, quota decrement, audit.
- `POST /tasks/bulk/delete` reuses `bulk_delete_tasks_core` (best-effort per id,
  HTTP 200 with deleted/skipped/not_found).

Same style as `test_api_v1_tasks_lifecycle.py`: PAT via `mint_pat_via_app`,
terminal state / executions / artifacts set up directly in the DB.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import mint_pat_via_app


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


async def _create_task(client: AsyncClient, token: str, **body) -> int:
    resp = await client.post("/api/v1/tasks", headers=_bearer(token), json=body or {"payload": {}})
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


async def _exec_sql(sql: str, params: dict) -> None:
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _scalar(sql: str, params: dict):
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar_one_or_none()
    finally:
        await engine.dispose()


async def _rows(sql: str, params: dict) -> list[dict]:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]
    finally:
        await engine.dispose()


async def _set_status(task_id: int, status: str) -> None:
    await _exec_sql(
        "UPDATE tasks SET status = CAST(:st AS task_status) WHERE id = :id",
        {"st": status, "id": task_id},
    )


async def _add_execution(task_id: int, worker_id: str, status: str = "completed") -> str:
    """Insert a terminal execution and return its id."""
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            ex_id = (
                await conn.execute(
                    text(
                        "INSERT INTO task_executions (task_id, worker_id, status) "
                        "VALUES (:tid, :wid, CAST(:st AS execution_status)) RETURNING id::text"
                    ),
                    {"tid": task_id, "wid": worker_id, "st": status},
                )
            ).scalar_one()
            return str(ex_id)
    finally:
        await engine.dispose()


async def _insert_worker_sql(workspace_id: str = DEFAULT_WORKSPACE_ID) -> str:
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


# ---------------------------------------------------------------------------
# DELETE /v1/tasks/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "cancelled", "failed"])
async def test_v1_delete_task_terminal_returns_204(app_client: AsyncClient, terminal: str) -> None:
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    worker_id = await _insert_worker_sql()
    await _add_execution(task_id, worker_id, "completed")
    await _set_status(task_id, terminal)

    resp = await app_client.delete(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    assert await _scalar("SELECT id FROM tasks WHERE id = :id", {"id": task_id}) is None
    # CASCADE: the executions disappear.
    assert (
        await _scalar("SELECT count(*) FROM task_executions WHERE task_id = :id", {"id": task_id})
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("non_terminal", ["queued", "dispatching", "paused"])
async def test_v1_delete_task_non_terminal_returns_409(
    app_client: AsyncClient, non_terminal: str
) -> None:
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    await _set_status(task_id, non_terminal)

    resp = await app_client.delete(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "conflict"
    # The task is still intact.
    assert await _scalar("SELECT id FROM tasks WHERE id = :id", {"id": task_id}) == task_id


@pytest.mark.asyncio
async def test_v1_delete_task_not_found_returns_404(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    resp = await app_client.delete("/api/v1/tasks/999999", headers=_bearer(token))
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_v1_delete_task_cross_tenant_returns_404(app_client: AsyncClient) -> None:
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email=None, label="b")
    theirs = await _create_task(app_client, token_b)
    await _set_status(theirs, "completed")

    token, _ = await mint_pat_via_app()
    resp = await app_client.delete(f"/api/v1/tasks/{theirs}", headers=_bearer(token))
    assert resp.status_code == 404, resp.text
    # The other tenant's task was not deleted.
    assert await _scalar("SELECT id FROM tasks WHERE id = :id", {"id": theirs}) == theirs


@pytest.mark.asyncio
async def test_v1_delete_task_audit_and_bytes(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    task_id = await _create_task(app_client, token)
    worker_id = await _insert_worker_sql()
    ex_id = await _add_execution(task_id, worker_id, "completed")
    # 12-byte artifact + workspace quota at 12.
    await _exec_sql(
        "INSERT INTO task_artifacts (execution_id, rel_path, size_bytes, sha256, content_type) "
        "VALUES (:ex, 'out.txt', 12, :sha, 'text/plain')",
        {"ex": ex_id, "sha": "a" * 64},
    )
    await _exec_sql(
        "INSERT INTO workspace_artifact_usage (workspace_id, total_bytes) VALUES (:w, 12) "
        "ON CONFLICT (workspace_id) DO UPDATE SET total_bytes = 12",
        {"w": DEFAULT_WORKSPACE_ID},
    )
    await _set_status(task_id, "completed")

    resp = await app_client.delete(f"/api/v1/tasks/{task_id}", headers=_bearer(token))
    assert resp.status_code == 204, resp.text

    audit = await _rows(
        "SELECT action, payload FROM admin_actions "
        "WHERE action = 'delete_task' AND target_id = :tid",
        {"tid": str(task_id)},
    )
    assert len(audit) == 1
    assert audit[0]["payload"] == {"prior_status": "completed"}

    usage = await _scalar(
        "SELECT total_bytes FROM workspace_artifact_usage WHERE workspace_id = :w",
        {"w": DEFAULT_WORKSPACE_ID},
    )
    assert usage == 0


# ---------------------------------------------------------------------------
# POST /v1/tasks/bulk/delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v1_bulk_delete_mixed_terminal_nonterminal_and_missing(
    app_client: AsyncClient,
) -> None:
    token, _ = await mint_pat_via_app()
    completed = await _create_task(app_client, token)
    failed = await _create_task(app_client, token)
    cancelled_t = await _create_task(app_client, token)
    queued = await _create_task(app_client, token)
    await _set_status(completed, "completed")
    await _set_status(failed, "failed")
    await _set_status(cancelled_t, "cancelled")
    # queued stays non-terminal.

    resp = await app_client.post(
        "/api/v1/tasks/bulk/delete",
        headers=_bearer(token),
        json={"task_ids": [completed, failed, cancelled_t, queued, 9_999_997]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["deleted"]) == sorted([completed, failed, cancelled_t])
    assert [s["id"] for s in body["skipped"]] == [queued]
    assert body["skipped"][0]["reason"] == "not terminal: queued"
    assert body["not_found"] == [9_999_997]

    rows = await _rows(
        "SELECT id FROM tasks WHERE id = ANY(:ids)",
        {"ids": [completed, failed, cancelled_t]},
    )
    assert rows == []


@pytest.mark.asyncio
async def test_v1_bulk_delete_audit_one_row_per_deleted(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    completed = await _create_task(app_client, token)
    failed = await _create_task(app_client, token)
    queued = await _create_task(app_client, token)
    await _set_status(completed, "completed")
    await _set_status(failed, "failed")

    resp = await app_client.post(
        "/api/v1/tasks/bulk/delete",
        headers=_bearer(token),
        json={"task_ids": [completed, failed, queued, 9_999_996]},
    )
    assert resp.status_code == 200

    rows = await _rows(
        "SELECT target_id, payload FROM admin_actions "
        "WHERE action = 'bulk_delete_task' ORDER BY target_id",
        {},
    )
    by_target = {r["target_id"]: r for r in rows}
    assert set(by_target) == {str(completed), str(failed)}
    assert by_target[str(completed)]["payload"] == {"prior_status": "completed"}
    assert by_target[str(failed)]["payload"] == {"prior_status": "failed"}

    # skipped / not_found are not audited.
    extra = await _rows(
        "SELECT 1 FROM admin_actions WHERE action = 'bulk_delete_task' AND target_id = ANY(:t)",
        {"t": [str(queued), "9999996"]},
    )
    assert extra == []


@pytest.mark.asyncio
async def test_v1_bulk_delete_validation_errors(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    h = _bearer(token)
    assert (
        await app_client.post("/api/v1/tasks/bulk/delete", headers=h, json={"task_ids": []})
    ).status_code == 422
    assert (
        await app_client.post(
            "/api/v1/tasks/bulk/delete", headers=h, json={"task_ids": list(range(1, 502))}
        )
    ).status_code == 422
    assert (
        await app_client.post("/api/v1/tasks/bulk/delete", headers=h, json={"task_ids": [3, 3]})
    ).status_code == 422
    assert (
        await app_client.post("/api/v1/tasks/bulk/delete", headers=h, json={"task_ids": [0]})
    ).status_code == 422
    assert (
        await app_client.post("/api/v1/tasks/bulk/delete", headers=h, json={"task_ids": [-1]})
    ).status_code == 422


@pytest.mark.asyncio
async def test_v1_bulk_delete_cross_tenant_isolates(app_client: AsyncClient) -> None:
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    token_b, _ = await mint_pat_via_app(workspace_id=ws_b, email=None, label="b")
    theirs = await _create_task(app_client, token_b)
    await _set_status(theirs, "completed")

    token, _ = await mint_pat_via_app()
    resp = await app_client.post(
        "/api/v1/tasks/bulk/delete",
        headers=_bearer(token),
        json={"task_ids": [theirs]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == []
    assert body["skipped"] == []
    assert body["not_found"] == [theirs]
    # The other tenant's task is still alive.
    assert await _scalar("SELECT id FROM tasks WHERE id = :id", {"id": theirs}) == theirs
