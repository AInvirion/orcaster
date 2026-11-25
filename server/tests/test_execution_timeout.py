"""Tests for execution_timeout_seconds on tasks (outer layer).

Covers:
- Roundtrip of the field through POST /admin/tasks and the DB column.
- Default NULL when it is not sent.
- 422 validation for invalid values (0, negative).
- The field travels in the /poll envelope.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import mint_token_via_app

REGISTER = {
    "hostname": "wt",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


async def _register(client: AsyncClient) -> tuple[str, str]:
    token = await mint_token_via_app()
    resp = await client.post("/v1/workers/register", json={**REGISTER, "enrollment_token": token})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _row(db_url: str, sql: str, params: dict) -> dict | None:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            r = (await conn.execute(text(sql), params)).mappings().first()
            return dict(r) if r else None
    finally:
        await engine.dispose()


# --- POST /admin/tasks: campo new_worker -----------------------------------------


@pytest.mark.asyncio
async def test_create_task_with_execution_timeout(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """execution_timeout_seconds is persisted correctly in the DB."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "execution_timeout_seconds": 300},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    row = await _row(
        migrated_db_url,
        "SELECT execution_timeout_seconds FROM tasks WHERE id = :tid",
        {"tid": task_id},
    )
    assert row is not None
    assert row["execution_timeout_seconds"] == 300


@pytest.mark.asyncio
async def test_create_task_without_execution_timeout_defaults_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Without the field in the body, the column stays NULL."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1"},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    row = await _row(
        migrated_db_url,
        "SELECT execution_timeout_seconds FROM tasks WHERE id = :tid",
        {"tid": task_id},
    )
    assert row is not None
    assert row["execution_timeout_seconds"] is None


@pytest.mark.asyncio
async def test_create_task_execution_timeout_zero_rejected(
    authed_admin_client: AsyncClient,
) -> None:
    """execution_timeout_seconds=0 → 422."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "execution_timeout_seconds": 0},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_task_execution_timeout_negative_rejected(
    authed_admin_client: AsyncClient,
) -> None:
    """execution_timeout_seconds negativo → 422."""
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "execution_timeout_seconds": -10},
    )
    assert resp.status_code == 422


# --- /poll envelope includes the field --------------------------------------


@pytest.mark.asyncio
async def test_poll_includes_execution_timeout_in_task_payload(
    authed_admin_client: AsyncClient,
) -> None:
    """The poll envelope includes execution_timeout_seconds from the DB."""
    worker_id, api_key = await _register(authed_admin_client)
    await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "execution_timeout_seconds": 120},
    )
    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        json={"status": "idle", "lease_seconds": 30},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["task"] is not None
    assert j["task"]["execution_timeout_seconds"] == 120


@pytest.mark.asyncio
async def test_poll_execution_timeout_null_when_not_set(
    authed_admin_client: AsyncClient,
) -> None:
    """execution_timeout_seconds=null in the poll when the task does not have it."""
    worker_id, api_key = await _register(authed_admin_client)
    await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1"},
    )
    resp = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        json={"status": "idle", "lease_seconds": 30},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["task"] is not None
    assert j["task"].get("execution_timeout_seconds") is None
