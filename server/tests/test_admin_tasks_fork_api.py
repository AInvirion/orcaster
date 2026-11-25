"""Integration tests for POST /admin/tasks/{id}/fork.

Fork: creates a new task with parent_task_id pointing to the parent and audits
params_changed (identical vs modified) in admin_actions. Different from the
in-place retry (same task_id).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


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


@pytest.mark.asyncio
async def test_create_task_sets_parent_task_id_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Regression: normal creation via /admin/tasks leaves parent_task_id NULL."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    body = create.json()
    assert body["parent_task_id"] is None
    row = await _row(
        migrated_db_url,
        "SELECT parent_task_id, retry_count FROM tasks WHERE id=:id",
        {"id": body["task_id"]},
    )
    assert row is not None
    assert row["parent_task_id"] is None
    assert row["retry_count"] == 0


@pytest.mark.asyncio
async def test_fork_sets_parent_task_id(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Fork creates a new task with parent_task_id pointing to the parent."""
    parent = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    parent_id = parent.json()["task_id"]

    resp = await authed_admin_client.post(f"/admin/tasks/{parent_id}/fork", json={"target": "1"})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["parent_task_id"] == parent_id
    assert j["task_id"] != parent_id

    row = await _row(
        migrated_db_url,
        "SELECT parent_task_id FROM tasks WHERE id=:id",
        {"id": j["task_id"]},
    )
    assert row is not None
    assert row["parent_task_id"] == parent_id


@pytest.mark.asyncio
async def test_fork_nonexistent_parent_404(authed_admin_client: AsyncClient) -> None:
    """Fork of a nonexistent parent → 404."""
    resp = await authed_admin_client.post("/admin/tasks/999999/fork", json={"target": "1"})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_fork_with_idempotency_key_all_available_422(
    authed_admin_client: AsyncClient,
) -> None:
    """Forking with `idempotency_key` + `target='all-available'` → 422.

    The fork goes through `_create_task`, so the guard that rejects the
    `idempotency_key` + all_available branch combination also applies to the
    fork endpoint.
    """
    import uuid

    parent = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    parent_id = parent.json()["task_id"]
    resp = await authed_admin_client.post(
        f"/admin/tasks/{parent_id}/fork",
        json={"target": "all-available", "idempotency_key": str(uuid.uuid4())},
    )
    assert resp.status_code == 422, resp.text
    assert "idempotency_key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_fork_records_admin_action_identical(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Fork with identical params audits fork_task with params_changed=False."""
    parent = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "payload": {"commands": ["echo hi"]}},
    )
    parent_id = parent.json()["task_id"]

    resp = await authed_admin_client.post(
        f"/admin/tasks/{parent_id}/fork",
        json={"target": "1", "payload": {"commands": ["echo hi"]}},
    )
    child_id = resp.json()["task_id"]

    actions = await _rows(
        migrated_db_url,
        "SELECT action, target_id, payload FROM admin_actions "
        "WHERE action='fork_task' AND target_id=:tid",
        {"tid": str(child_id)},
    )
    assert len(actions) == 1
    assert actions[0]["payload"]["parent_task_id"] == parent_id
    assert actions[0]["payload"]["params_changed"] is False


@pytest.mark.asyncio
async def test_fork_modified_payload_params_changed_true(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """different payload → params_changed=True."""
    parent = await authed_admin_client.post(
        "/admin/tasks", json={"target": "1", "payload": {"commands": ["a"]}}
    )
    parent_id = parent.json()["task_id"]
    resp = await authed_admin_client.post(
        f"/admin/tasks/{parent_id}/fork",
        json={"target": "1", "payload": {"commands": ["b"]}},
    )
    child_id = resp.json()["task_id"]
    actions = await _rows(
        migrated_db_url,
        "SELECT payload FROM admin_actions WHERE action='fork_task' AND target_id=:tid",
        {"tid": str(child_id)},
    )
    assert actions[0]["payload"]["params_changed"] is True


@pytest.mark.asyncio
async def test_fork_modified_timeout_params_changed_true(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """different execution_timeout_seconds → params_changed=True."""
    parent = await authed_admin_client.post(
        "/admin/tasks", json={"target": "1", "execution_timeout_seconds": 100}
    )
    parent_id = parent.json()["task_id"]
    resp = await authed_admin_client.post(
        f"/admin/tasks/{parent_id}/fork",
        json={"target": "1", "execution_timeout_seconds": 200},
    )
    child_id = resp.json()["task_id"]
    actions = await _rows(
        migrated_db_url,
        "SELECT payload FROM admin_actions WHERE action='fork_task' AND target_id=:tid",
        {"tid": str(child_id)},
    )
    assert actions[0]["payload"]["params_changed"] is True


@pytest.mark.asyncio
async def test_fork_only_priority_changed_params_changed_false(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """different priority (and nothing else) → params_changed=False (priority excluded)."""
    parent = await authed_admin_client.post("/admin/tasks", json={"target": "1", "priority": 0})
    parent_id = parent.json()["task_id"]
    resp = await authed_admin_client.post(
        f"/admin/tasks/{parent_id}/fork", json={"target": "1", "priority": 5}
    )
    child_id = resp.json()["task_id"]
    actions = await _rows(
        migrated_db_url,
        "SELECT payload FROM admin_actions WHERE action='fork_task' AND target_id=:tid",
        {"tid": str(child_id)},
    )
    assert actions[0]["payload"]["params_changed"] is False


@pytest.mark.asyncio
async def test_fork_on_delete_parent_sets_child_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Deleting the parent (FK ON DELETE SET NULL) leaves the child with parent_task_id=NULL."""
    parent = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    parent_id = parent.json()["task_id"]
    fork = await authed_admin_client.post(f"/admin/tasks/{parent_id}/fork", json={"target": "1"})
    child_id = fork.json()["task_id"]

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tasks WHERE id=:id"), {"id": parent_id})
    finally:
        await engine.dispose()

    row = await _row(
        migrated_db_url, "SELECT parent_task_id FROM tasks WHERE id=:id", {"id": child_id}
    )
    assert row is not None
    assert row["parent_task_id"] is None
