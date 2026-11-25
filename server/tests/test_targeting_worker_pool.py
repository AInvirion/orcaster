"""Tests — Task targeting by worker IDs.

Covers the 5 syntaxes of the `target` field:

  - "N"               — current behavior (count), pool=NULL.
  - "all-available"   — current behavior (all_available), pool=NULL.
  - "worker:<id>"     — count=1, pool=[id].
  - "workers:<u1>,<u2>" — count=2, pool=[u1, u2].
  - "all:<u1>,..."    — snapshot at creation restricted to the subset; pool=ids.

Plus:
  - Unknown UUID → 422 with detail.
  - Malformed syntax → 422.
  - all:<ids> with all offline at snapshot time → task `failed/no_eligible_workers`.
  - A worker outside the pool does not receive the task via /poll (CTE excludes it).
"""

from __future__ import annotations

import uuid
from typing import Any

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


# --- 5 syntaxes, happy path ------------------------------------------------


@pytest.mark.asyncio
async def test_target_count_n_pool_is_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "3"})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["target_mode"] == "count"
    assert j["target_count"] == 3
    row = await _row(
        migrated_db_url,
        "SELECT target_worker_pool FROM tasks WHERE id = :tid",
        {"tid": j["task_id"]},
    )
    assert row is not None
    assert row["target_worker_pool"] is None


@pytest.mark.asyncio
async def test_target_all_available_pool_is_null(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    resp = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["target_mode"] == "all_available"
    row = await _row(
        migrated_db_url,
        "SELECT target_worker_pool FROM tasks WHERE id = :tid",
        {"tid": j["task_id"]},
    )
    assert row is not None
    assert row["target_worker_pool"] is None


@pytest.mark.asyncio
async def test_target_worker_single_id(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    wid, _ = await _register(authed_admin_client, "a")
    resp = await authed_admin_client.post("/admin/tasks", json={"target": f"worker:{wid}"})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["target_mode"] == "count"
    assert j["target_count"] == 1
    assert j["status"] == "queued"
    row = await _row(
        migrated_db_url,
        "SELECT target_worker_pool FROM tasks WHERE id = :tid",
        {"tid": j["task_id"]},
    )
    assert row is not None
    assert row["target_worker_pool"] == [wid]


@pytest.mark.asyncio
async def test_target_workers_multiple_ids(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    w_a, _ = await _register(authed_admin_client, "a")
    w_b, _ = await _register(authed_admin_client, "b")
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": f"workers:{w_a},{w_b}"},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["target_mode"] == "count"
    assert j["target_count"] == 2
    row = await _row(
        migrated_db_url,
        "SELECT target_worker_pool FROM tasks WHERE id = :tid",
        {"tid": j["task_id"]},
    )
    assert row is not None
    assert set(row["target_worker_pool"]) == {w_a, w_b}


@pytest.mark.asyncio
async def test_target_all_with_ids_snapshots_subset(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    w_a, _ = await _register(authed_admin_client, "a")
    w_b, _ = await _register(authed_admin_client, "b")
    w_c, _ = await _register(authed_admin_client, "c")
    # `all:` restricts to the subset {a, c}; b stays out of the pool.
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": f"all:{w_a},{w_c}"},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["target_mode"] == "all_available"
    assert j["status"] == "dispatching"
    assert j["target_count"] == 2

    # Snapshot must have materialized 2 executions, one per worker of the subset.
    execs = await _rows(
        migrated_db_url,
        "SELECT worker_id::text AS worker_id, status::text AS status "
        "FROM task_executions WHERE task_id = :tid",
        {"tid": j["task_id"]},
    )
    assert {e["worker_id"] for e in execs} == {w_a, w_c}
    assert all(e["status"] == "assigned" for e in execs)

    row = await _row(
        migrated_db_url,
        "SELECT target_worker_pool FROM tasks WHERE id = :tid",
        {"tid": j["task_id"]},
    )
    assert row is not None
    assert set(row["target_worker_pool"]) == {w_a, w_c}


# --- Validation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_uuid_in_worker_target_returns_422(
    authed_admin_client: AsyncClient,
) -> None:
    unknown = str(uuid.uuid4())
    resp = await authed_admin_client.post("/admin/tasks", json={"target": f"worker:{unknown}"})
    assert resp.status_code == 422, resp.text
    assert unknown in resp.text


@pytest.mark.asyncio
async def test_unknown_uuid_in_workers_target_lists_all_invalid(
    authed_admin_client: AsyncClient,
) -> None:
    w_a, _ = await _register(authed_admin_client, "a")
    bad1 = str(uuid.uuid4())
    bad2 = str(uuid.uuid4())
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": f"workers:{w_a},{bad1},{bad2}"},
    )
    assert resp.status_code == 422, resp.text
    # Detail must mention both invalid IDs.
    assert bad1 in resp.text
    assert bad2 in resp.text


@pytest.mark.asyncio
async def test_unknown_uuid_in_all_target_returns_422(
    authed_admin_client: AsyncClient,
) -> None:
    w_a, _ = await _register(authed_admin_client, "a")
    unknown = str(uuid.uuid4())
    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": f"all:{w_a},{unknown}"},
    )
    assert resp.status_code == 422, resp.text
    assert unknown in resp.text


@pytest.mark.parametrize(
    "bad_target",
    [
        "worker:",
        "workers:",
        "all:",
        "worker:not-a-uuid",
        "workers:a,,b",
        "workers:,",
        "all:not-a-uuid",
        "worker:abc-not-uuid",
    ],
)
@pytest.mark.asyncio
async def test_malformed_target_returns_422(
    authed_admin_client: AsyncClient, bad_target: str
) -> None:
    resp = await authed_admin_client.post("/admin/tasks", json={"target": bad_target})
    assert resp.status_code == 422, (bad_target, resp.text)


# --- Snapshot all:<ids> with a 100% offline pool ---------------------------


@pytest.mark.asyncio
async def test_all_with_ids_all_offline_marks_task_failed_no_eligible(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    w_a, _ = await _register(authed_admin_client, "a")
    w_b, _ = await _register(authed_admin_client, "b")
    # Both offline at snapshot time.
    await _set_worker_status(migrated_db_url, w_a, "offline")
    await _set_worker_status(migrated_db_url, w_b, "offline")

    resp = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": f"all:{w_a},{w_b}"},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "failed"
    assert j["target_count"] == 0

    row = await _row(
        migrated_db_url,
        "SELECT status::text AS status, cancelled_reason FROM tasks WHERE id = :tid",
        {"tid": j["task_id"]},
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["cancelled_reason"] == "no_eligible_workers"

    # No executions were created.
    execs = await _rows(
        migrated_db_url,
        "SELECT 1 FROM task_executions WHERE task_id = :tid",
        {"tid": j["task_id"]},
    )
    assert execs == []


# --- A worker outside the pool does not receive the task via /poll ----------


@pytest.mark.asyncio
async def test_worker_outside_pool_does_not_claim_task(
    authed_admin_client: AsyncClient,
) -> None:
    """A task with `target=worker:<a>` must not be visible to worker b."""
    w_a, key_a = await _register(authed_admin_client, "a")
    w_b, key_b = await _register(authed_admin_client, "b")

    create = await authed_admin_client.post("/admin/tasks", json={"target": f"worker:{w_a}"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # Worker B (outside the pool) must NOT receive anything.
    poll_b = await authed_admin_client.post(
        f"/v1/workers/{w_b}/poll",
        headers={"X-API-Key": key_b},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll_b.status_code == 200, poll_b.text
    assert poll_b.json()["task"] is None

    # Worker A (inside the pool) does.
    poll_a = await authed_admin_client.post(
        f"/v1/workers/{w_a}/poll",
        headers={"X-API-Key": key_a},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll_a.status_code == 200, poll_a.text
    task = poll_a.json()["task"]
    assert task is not None
    assert task["task_id"] == task_id


@pytest.mark.asyncio
async def test_workers_pool_distributes_to_listed_workers(
    authed_admin_client: AsyncClient,
) -> None:
    """`target=workers:<a>,<b>` lets a and b claim the task but not c."""
    w_a, key_a = await _register(authed_admin_client, "a")
    w_b, key_b = await _register(authed_admin_client, "b")
    w_c, key_c = await _register(authed_admin_client, "c")

    create = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": f"workers:{w_a},{w_b}"},
    )
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # C outside the pool: receives nothing.
    poll_c = await authed_admin_client.post(
        f"/v1/workers/{w_c}/poll",
        headers={"X-API-Key": key_c},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll_c.json()["task"] is None

    # A inside the pool: claims.
    poll_a = await authed_admin_client.post(
        f"/v1/workers/{w_a}/poll",
        headers={"X-API-Key": key_a},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll_a.json()["task"]["task_id"] == task_id

    # B inside the pool: also claims (target_count=2).
    poll_b = await authed_admin_client.post(
        f"/v1/workers/{w_b}/poll",
        headers={"X-API-Key": key_b},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll_b.json()["task"]["task_id"] == task_id


# --- Bug: yield-check must not yield to workers outside the pool ---------


@pytest.mark.asyncio
async def test_pooled_task_does_not_yield_to_outside_worker(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Regression (yield_check deadlock).

    Scenario:
      - 2 online workers: A (outside the pool) and B (inside the pool).
      - A has an older `max(assigned_at)` than B (fairness "favors it").
      - Task with `target=worker:<B>` stays queued.
    Before the fix: B yielded indefinitely to A for "global" fairness, but A
    could not claim (it is not in the pool) → deadlock.
    After the fix: yield-check ignores workers outside the candidate task's
    pool, so B claims on its first /poll.
    """
    w_a, _key_a = await _register(authed_admin_client, "outside")
    w_b, key_b = await _register(authed_admin_client, "inside")

    # Seed an artificial `max(assigned_at)`: A older than B. For
    # `yield_check` to "favor" the outsider A, B must have a more
    # recent assigned_at. We insert terminal rows (they do not block other claims)
    # with a controlled assigned_at.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            # "Previous" task (any value; we only care that it exists).
            prev = (
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
            seed_task_id = prev[0]
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
    finally:
        await engine.dispose()

    # Create a task restricted to the pool {B}.
    create = await authed_admin_client.post("/admin/tasks", json={"target": f"worker:{w_b}"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # B (inside the pool) polls and MUST claim — before the fix it yielded
    # to A for global fairness and the task stayed queued indefinitely.
    poll_b = await authed_admin_client.post(
        f"/v1/workers/{w_b}/poll",
        headers={"X-API-Key": key_b},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll_b.status_code == 200, poll_b.text
    task = poll_b.json()["task"]
    assert task is not None, "B should have claimed the task; yield_check yielded wrongly"
    assert task["task_id"] == task_id
