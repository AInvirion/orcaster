"""Tests for POST /admin/workers/{id}/control-op.

Control operations (e.g. __reload_config__) are submitted via this dedicated
endpoint, not via POST /admin/tasks. The endpoint creates a push task with
command = control_op, delivery_mode='push', target_worker_id set.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import mint_token_via_app

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


@pytest.mark.asyncio
async def test_control_op_creates_push_task(authed_admin_client: AsyncClient) -> None:
    """POST /admin/workers/{id}/control-op creates a push task with command=control_op."""
    wid, _ = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/admin/workers/{wid}/control-op",
        json={"control_op": "__reload_config__", "payload": {"reason": "test"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] > 0
    assert body["status"] == "queued"
    assert body["control_op"] == "__reload_config__"


@pytest.mark.asyncio
async def test_control_op_unknown_op_rejected(authed_admin_client: AsyncClient) -> None:
    """Unknown control_op (not in RESERVED_COMMANDS) → 422."""
    wid, _ = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/admin/workers/{wid}/control-op",
        json={"control_op": "not_reserved", "payload": {}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_control_op_unknown_worker_returns_404(authed_admin_client: AsyncClient) -> None:
    """Non-existent worker_id → 404."""
    resp = await authed_admin_client.post(
        "/admin/workers/00000000-0000-0000-0000-000000000000/control-op",
        json={"control_op": "__reload_config__", "payload": {}},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_control_op_worker_receives_task_via_poll(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The push task created by control-op is delivered to the worker via /poll."""
    wid, key = await _register(authed_admin_client)
    resp = await authed_admin_client.post(
        f"/admin/workers/{wid}/control-op",
        json={"control_op": "__reload_config__", "payload": {}},
    )
    assert resp.status_code == 200

    poll = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle"},
    )
    body = poll.json()
    assert body["task"] is not None
    assert body["task"]["command"] == "__reload_config__"
    assert body["task"]["delivery_mode"] == "push"
