"""Tests — cross-tenant safety net for fork / retry / bulk + worker `complete`.

These tests **pin the already-correct behavior** of the code, so that any
change to the defensive scoping of internal statements that breaks cross-tenant
isolation is caught. They introduce no new behavior.

Coverage:
  - `POST /admin/tasks/{id}/fork` with a parent in another workspace → 404.
  - `POST /admin/tasks/{id}/retry` with a task in another workspace → 404.
  - `POST /admin/tasks/bulk/cancel` / `bulk/delete`: a task from another
    workspace lands in `not_found` and is NOT mutated; in a mixed batch only
    the rows of the active workspace are affected.
  - Worker vector: `POST /v1/tasks/{id}/complete` against a task in another
    workspace (without a legitimate lease) → 409 (execution not owned).

Reuses the helpers of `test_admin_enforcement` to avoid duplicating the
session/workspace setup.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import mint_token_via_app
from tests.test_admin_enforcement import (
    REGISTER,
    _create_workspace,
    _db_url,
    _make_authed_client_for_workspace,
)


async def _task_status(task_id: int) -> str | None:
    """Read `tasks.status` straight from the DB (no scoping; DB verification)."""
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT status::text FROM tasks WHERE id = :id"), {"id": task_id}
                )
            ).scalar_one_or_none()
    finally:
        await engine.dispose()


# --- fork / retry cross-tenant → 404 ---------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_fork_404(authed_admin_client: AsyncClient) -> None:
    """Admin A (Default) cannot fork a task created in workspace B."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
    finally:
        await client_b.aclose()

    fork = await authed_admin_client.post(f"/admin/tasks/{task_b}/fork", json={"target": "1"})
    assert fork.status_code == 404, fork.text


@pytest.mark.asyncio
async def test_cross_tenant_retry_404(authed_admin_client: AsyncClient) -> None:
    """Admin A (Default) cannot retry a task of workspace B."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
        # Move B's task to a terminal state (cancelled) so it is retryable and
        # rule out that the 404 comes from a state-based 409.
        cancel_b = await client_b.post(f"/admin/tasks/{task_b}/cancel", json={"reason": "x"})
        assert cancel_b.status_code == 200, cancel_b.text
    finally:
        await client_b.aclose()

    retry = await authed_admin_client.post(f"/admin/tasks/{task_b}/retry", json={})
    assert retry.status_code == 404, retry.text


# --- bulk cancel / delete cross-tenant -------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_bulk_cancel_isolates(authed_admin_client: AsyncClient) -> None:
    """`bulk/cancel` by A on B's task: B lands in `not_found`, not cancelled."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
    finally:
        await client_b.aclose()

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/cancel", json={"task_ids": [task_b], "reason": "probe"}
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["cancelled"] == []
    assert payload["not_found"] == [task_b]
    # B's task is untouched (not cancelled).
    assert await _task_status(task_b) != "cancelled"


@pytest.mark.asyncio
async def test_cross_tenant_bulk_delete_isolates(authed_admin_client: AsyncClient) -> None:
    """`bulk/delete` by A on B's terminal task: B lands in `not_found`, not deleted."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
        # Make it terminal (cancelled) so it is a delete candidate.
        cancel_b = await client_b.post(f"/admin/tasks/{task_b}/cancel", json={"reason": "x"})
        assert cancel_b.status_code == 200, cancel_b.text
    finally:
        await client_b.aclose()

    resp = await authed_admin_client.post("/admin/tasks/bulk/delete", json={"task_ids": [task_b]})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["deleted"] == []
    assert payload["not_found"] == [task_b]
    # B's task still exists.
    assert await _task_status(task_b) == "cancelled"


@pytest.mark.asyncio
async def test_cross_tenant_bulk_retry_isolates(authed_admin_client: AsyncClient) -> None:
    """`bulk/retry` by A on B's failed task: B lands in `not_found`
    and is NOT mutated (stays `failed`, no reset to queued nor bumps)."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
    finally:
        await client_b.aclose()

    # Set it to `failed` directly in the DB: a legitimate retry candidate IN B,
    # to rule out that the not_found comes from status eligibility.
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'failed'::task_status WHERE id = :id"),
                {"id": task_b},
            )
    finally:
        await engine.dispose()

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_b]})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["retried"] == []
    assert payload["skipped"] == []
    assert payload["not_found"] == [task_b]
    # B's task is still `failed` (not reset to queued).
    assert await _task_status(task_b) == "failed"


@pytest.mark.asyncio
async def test_bulk_cancel_mixed_only_affects_own(authed_admin_client: AsyncClient) -> None:
    """Mixed batch (A + B): `bulk/cancel` only cancels A's; B → `not_found`."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
    finally:
        await client_b.aclose()

    create_a = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create_a.status_code == 200, create_a.text
    task_a = create_a.json()["task_id"]

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/cancel", json={"task_ids": [task_a, task_b], "reason": "probe"}
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["cancelled"] == [task_a]
    assert payload["not_found"] == [task_b]
    assert await _task_status(task_a) == "cancelled"
    assert await _task_status(task_b) != "cancelled"


@pytest.mark.asyncio
async def test_bulk_delete_mixed_only_affects_own(authed_admin_client: AsyncClient) -> None:
    """Mixed batch (A + B): `bulk/delete` only deletes A's; B → `not_found`."""
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
        cancel_b = await client_b.post(f"/admin/tasks/{task_b}/cancel", json={"reason": "x"})
        assert cancel_b.status_code == 200, cancel_b.text
    finally:
        await client_b.aclose()

    create_a = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create_a.status_code == 200, create_a.text
    task_a = create_a.json()["task_id"]
    cancel_a = await authed_admin_client.post(f"/admin/tasks/{task_a}/cancel", json={"reason": "x"})
    assert cancel_a.status_code == 200, cancel_a.text

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/delete", json={"task_ids": [task_a, task_b]}
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["deleted"] == [task_a]
    assert payload["not_found"] == [task_b]
    assert await _task_status(task_a) is None  # borrada
    assert await _task_status(task_b) == "cancelled"  # intacta


# --- worker `complete` cross-tenant → 409 ----------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_worker_complete_409(authed_admin_client: AsyncClient) -> None:
    """A worker of A cannot close (`complete`) a task of workspace B.

    `_SQL_COMPLETE` requires `(task_id, worker_id, lease_token)` with the
    execution in `claimed`/`running`. A's worker never obtains a lease for B's
    task (`/poll` is workspace-scoped), so the cross-tenant close finds no row
    → 409 (execution not owned by this lease).
    """
    # Task in workspace B.
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    client_b = await _make_authed_client_for_workspace(ws_b)
    try:
        create_b = await client_b.post("/admin/tasks", json={"target": "1"})
        assert create_b.status_code == 200, create_b.text
        task_b = create_b.json()["task_id"]
    finally:
        await client_b.aclose()

    # Legitimate worker in Default (workspace A).
    token = await mint_token_via_app()
    reg = await authed_admin_client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": "ws-a-worker", "enrollment_token": token},
    )
    assert reg.status_code == 200, reg.text
    key = reg.json()["api_key"]

    # A's worker tries to close B's task with a made-up lease → 409.
    complete = await authed_admin_client.post(
        f"/v1/tasks/{task_b}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": str(uuid.uuid4()), "status": "completed", "exit_code": 0},
    )
    assert complete.status_code == 409, complete.text
