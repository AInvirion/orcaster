"""Tests — workspace scoping of the worker API (claim/poll).

They verify that `POST /v1/workers/{id}/poll` never hands a worker tasks of
ANOTHER workspace. Three paths have a dedicated test (open-claim, push and
fairness-yield):

1. open-claim (`target_mode='count'`): a WS_B worker does not claim a task that
   lives in the Default workspace, even though eligibility/pool are NOT the
   blocker (eligibility={} matches everyone and pool=NULL does not restrict). A
   worker in Default does claim it → proves only the workspace gates it.
2. push (`delivery_mode='push'`): a push aimed at a Default worker is never
   delivered to a WS_B worker; the Default worker does receive it.
3. fairness without cross-tenant starvation (covers the fix of the
   `NOT EXISTS (... workers other ...)` subquery): a task in Default with
   target_count=1; an online WS_B worker exists that is "more idle" (older
   assigned_at) than the Default worker. The Default worker MUST claim — it must
   not yield by fairness to a worker of another tenant that could never claim
   it. Without the subquery fix, the Default worker would yield forever
   (starvation).

The pre-assignment path (`_SQL_POLL_CLAIM_PREASSIGNED`) carries the SAME
predicate `t.workspace_id = :workspace_id` but has no dedicated test: reaching a
cross-tenant pre-assigned execution would require a data inconsistency, so the
predicate is defensive (belt-and-suspenders).

Isolation setup: tasks created via `/admin/tasks` land in the `Default`
workspace (the column DEFAULT; admin create is not workspace-scoped yet). The
"isolated" worker enrolls in WS_B by minting a token for WS_B.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
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


async def _register(
    client: AsyncClient,
    hostname: str,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> tuple[str, str]:
    """Register a worker enrolled in `workspace_id` (default = Default)."""
    token = await mint_token_via_app(workspace_id=workspace_id)
    resp = await client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _create_workspace(db_url: str, name: str, slug: str) -> str:
    """Create a workspace via the service and return its id."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from orcaster_server.workspaces.service import create_workspace

    engine = create_async_engine(db_url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            ws_id = await create_workspace(s, name=name, slug=slug)
            await s.commit()
            return ws_id
    finally:
        await engine.dispose()


async def _poll(client: AsyncClient, worker_id: str, api_key: str) -> dict | None:
    resp = await client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": api_key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["task"]


# --- 1. Cross-workspace isolation (open-claim) -----------------------------


@pytest.mark.asyncio
async def test_open_claim_worker_in_other_workspace_gets_nothing(
    authed_admin_client: AsyncClient,
) -> None:
    """A WS_B worker does not claim a count-task that lives in Default.

    The task has eligibility={} (matches everyone) and pool=NULL (no pool
    restriction), so neither capability nor pool is the blocker: the ONLY gate
    is the workspace. A Default worker claims it → proves the task is claimable
    and only the workspace separates it.
    """
    ws_b = await _create_workspace(
        _db_url(authed_admin_client), "WS_B", f"ws-b-{uuid.uuid4().hex[:8]}"
    )

    # Worker in WS_B (WS_B token). Satisfies eligibility (empty) but is in
    # another tenant.
    w_b, key_b = await _register(authed_admin_client, "in-ws-b", workspace_id=ws_b)

    # Task count=1 in Default (admin create is not workspace-scoped → DEFAULT).
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # WS_B worker: does NOT receive the task (it lives in Default).
    assert await _poll(authed_admin_client, w_b, key_b) is None

    # Worker in Default: DOES claim it (proves only the workspace gated it).
    w_def, key_def = await _register(authed_admin_client, "in-default")
    task = await _poll(authed_admin_client, w_def, key_def)
    assert task is not None
    assert task["task_id"] == task_id


# --- 2. Cross-workspace isolation (push) -----------------------------------


@pytest.mark.asyncio
async def test_push_task_not_delivered_to_other_workspace_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """A push aimed at a Default worker never reaches a WS_B worker.

    The push already filters by `target_worker_id=:worker_id`, so the workspace
    filter is exercised specifically this way: the push targets a Default
    worker; the WS_B worker (different id, different tenant) receives nothing,
    and the targeted Default worker does.
    """
    ws_b = await _create_workspace(
        _db_url(authed_admin_client), "WS_B", f"ws-b-{uuid.uuid4().hex[:8]}"
    )

    # Target worker in Default.
    w_def, key_def = await _register(authed_admin_client, "target-default")
    # Worker in WS_B.
    w_b, key_b = await _register(authed_admin_client, "in-ws-b", workspace_id=ws_b)

    # Push aimed at the Default worker.
    create = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "1", "delivery_mode": "push", "target_worker_id": w_def},
    )
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # WS_B worker: nothing (neither by target nor by workspace).
    assert await _poll(authed_admin_client, w_b, key_b) is None

    # Targeted Default worker: receives the push.
    task = await _poll(authed_admin_client, w_def, key_def)
    assert task is not None
    assert task["task_id"] == task_id


# --- 3. Fairness: no cross-tenant starvation (covers the subquery fix) ---


@pytest.mark.asyncio
async def test_fairness_does_not_yield_to_other_workspace_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """The Default worker claims even if a WS_B worker is "more idle".

    Regression test for the `NOT EXISTS (... workers other ...)` subquery in
    `_SQL_POLL_CLAIM_OPEN_LOCK`. Scenario:
      - Worker A in WS_B: online, with the OLDEST `max(assigned_at)` (fairness
        "favors" it), but it can never claim Default tasks.
      - Worker B in Default: online, with a more recent `max(assigned_at)`.
      - Task count=1 in Default.
    Without the fix, B would yield to A by "global" fairness and the task would
    stay queued forever (cross-tenant starvation). With the fix, the yield check
    ignores workers of other workspaces and B claims on its first /poll.
    """
    db_url = _db_url(authed_admin_client)
    ws_b = await _create_workspace(db_url, "WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")

    w_a, _key_a = await _register(
        authed_admin_client, "idle-ws-b", workspace_id=ws_b
    )  # another tenant
    w_b, key_b = await _register(authed_admin_client, "default-claimer")  # Default

    # Seed max(assigned_at): A (WS_B) older than B (Default), and keep A
    # "online" with a recent heartbeat so it is a yield-check candidate.
    # Terminal rows (completed) do not block claims; they only set assigned_at.
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            seed_task = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, command, payload,
                                           target_mode, target_count, priority,
                                           status, run_at, signed, signature)
                        VALUES (:workspace_id, 'seed', '{}'::jsonb, 'count', 1, 0,
                                'completed', now(), true,
                                decode(repeat('00',64),'hex'))
                        RETURNING id
                        """
                    ),
                    {"workspace_id": DEFAULT_WORKSPACE_ID},
                )
            ).first()
            seed_task_id = seed_task[0]
            await conn.execute(
                text(
                    """
                    INSERT INTO task_executions
                        (task_id, worker_id, status, assigned_at, finished_at,
                         attempt_index)
                    VALUES
                        (:tid, :wa, 'completed', now() - interval '120 seconds',
                         now() - interval '110 seconds', 1),
                        (:tid, :wb, 'completed', now() - interval '30 seconds',
                         now() - interval '20 seconds', 1)
                    """
                ),
                {"tid": seed_task_id, "wa": w_a, "wb": w_b},
            )
            # A (WS_B) online with a recent heartbeat → yield-check candidate.
            await conn.execute(
                text("UPDATE workers SET status='online', last_heartbeat_at=now() WHERE id = :wid"),
                {"wid": w_a},
            )
    finally:
        await engine.dispose()

    # Task count=1 in Default.
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # B (Default) MUST claim — it does not yield to the WS_B worker.
    task = await _poll(authed_admin_client, w_b, key_b)
    assert task is not None, "Default worker yielded to a worker of another tenant (starvation)"
    assert task["task_id"] == task_id


# --- helpers ---------------------------------------------------------------


def _db_url(client: AsyncClient) -> str:
    """URL of the live DB (the one `authed_admin_client` set in settings)."""
    from orcaster_server.config import settings

    return settings.database_url
