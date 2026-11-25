"""Regression: the fairness yield must not defer to BUSY workers.

The anti-starvation fairness predicate of the open claim
(`_SQL_POLL_CLAIM_OPEN_LOCK`, subquery `NOT EXISTS (... workers other ...)`)
makes an idle worker claim only if no other online worker has an older
`max(assigned_at)`. The subquery took pool and workspace into account,
but NOT the other worker's busy state.

Real deadlock scenario:
  - Worker A: online, running a long task (hours). Its `running` execution
    gives it the OLDEST `max(assigned_at)` of the fleet → fairness "favors it".
  - Worker B: online, idle, with a more recent `max(assigned_at)`.
  - N queued count=1 tasks.
Without the fix, B yields to A for fairness, but A is BUSY and cannot claim →
the whole idle fleet yields to A indefinitely and nothing is dispatched
(priority inversion / head-of-line blocking). With the fix, the yield check
ignores workers with an active execution (assigned/claimed/running), so B
claims on its first /poll.

Extension (migration 0053): the active execution was a PROXY for "this
worker is busy", and the disk gate broke it. A worker below the disk floor
reports `status='busy'` in the poll WITHOUT any execution row, stays `online`
with a fresh heartbeat and its `max(assigned_at)` only gets older: it
reproduced the same deadlock through a path that the active-execution
exclusion did not cover. The predicate now also excludes a worker that
self-reports `busy` (`workers.last_reported_status`), with `NULL` = "has not
polled yet" = behavior prior to the migration.
"""

from __future__ import annotations

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


async def _register(client: AsyncClient, hostname: str) -> tuple[str, str]:
    token = await mint_token_via_app(workspace_id=DEFAULT_WORKSPACE_ID)
    resp = await client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _poll(
    client: AsyncClient,
    worker_id: str,
    api_key: str,
    status: str = "idle",
) -> dict | None:
    resp = await client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": api_key},
        json={"status": status, "lease_seconds": 30},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["task"]


def _db_url(client: AsyncClient) -> str:
    from orcaster_server.config import settings

    return settings.database_url


@pytest.mark.asyncio
async def test_fairness_does_not_yield_to_busy_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """An idle worker claims even if a BUSY worker is "more idle".

    Worker A is running a long task (`running` execution) with the oldest
    assigned_at; worker B is idle with a more recent assigned_at.
    B MUST claim the queued count-task: it must not yield for fairness to A,
    which is busy and would never claim while running its own work.
    """
    db_url = _db_url(authed_admin_client)

    w_busy, _key_busy = await _register(authed_admin_client, "busy-long-task")
    w_idle, key_idle = await _register(authed_admin_client, "idle-claimer")

    # Seed the state: A busy (`running` execution, old assigned_at) and
    # B with a more recent assigned_at via a terminal execution. Both online
    # with a fresh heartbeat → candidates for the yield check.
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            # Seed task in `dispatching` that hosts A's `running` execution.
            # attempt_budget=1 with 1 running already saturates the budget → this
            # seed task is not a claim candidate for anyone (no interference).
            seed_task = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, command, payload,
                                           target_mode, target_count, priority,
                                           status, run_at, signed, signature,
                                           attempt_budget)
                        VALUES (:workspace_id, 'seed', '{}'::jsonb, 'count', 1, 0,
                                'dispatching', now(), true,
                                decode(repeat('00',64),'hex'), 1)
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
                        (:tid, :busy, 'running', now() - interval '120 seconds',
                         NULL, 1),
                        (:tid, :idle, 'completed', now() - interval '30 seconds',
                         now() - interval '20 seconds', 1)
                    """
                ),
                {"tid": seed_task_id, "busy": w_busy, "idle": w_idle},
            )
            await conn.execute(
                text(
                    "UPDATE workers SET status='online', last_heartbeat_at=now() "
                    "WHERE id IN (:busy, :idle)"
                ),
                {"busy": w_busy, "idle": w_idle},
            )
    finally:
        await engine.dispose()

    # Queued count=1 task in Default.
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # B (idle) MUST claim: it does not yield to the busy worker A.
    task = await _poll(authed_admin_client, w_idle, key_idle)
    assert task is not None, (
        "The idle worker yielded to a BUSY worker (deadlock): nobody "
        "can dispatch while the long task runs."
    )
    assert task["task_id"] == task_id


# --- self-reported `busy` without an execution (disk gate) ---------------------


async def _seed_terminal_executions(
    db_url: str,
    *,
    older_worker: str,
    newer_worker: str,
) -> None:
    """Seed two TERMINAL executions, one per worker, with different ages.

    Terminal on purpose: with an active execution the predicate would already
    exclude the worker and the test would pass without the self-reported
    `busy` exclusion. Here the only differentiator must be
    `workers.last_reported_status`.

    `older_worker` ends up with the OLDEST `max(assigned_at)`, which is what
    the fairness yield "favors": the other worker should yield the claim to it
    unless it is excluded from the predicate.
    """
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            seed_task = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, command, payload,
                                           target_mode, target_count, priority,
                                           status, run_at, signed, signature,
                                           attempt_budget)
                        VALUES (:workspace_id, 'seed', '{}'::jsonb, 'count', 1, 0,
                                'completed', now(), true,
                                decode(repeat('00',64),'hex'), 1)
                        RETURNING id
                        """
                    ),
                    {"workspace_id": DEFAULT_WORKSPACE_ID},
                )
            ).first()
            assert seed_task is not None
            await conn.execute(
                text(
                    """
                    INSERT INTO task_executions
                        (task_id, worker_id, status, assigned_at, finished_at,
                         attempt_index)
                    VALUES
                        (:tid, :older, 'completed', now() - interval '300 seconds',
                         now() - interval '290 seconds', 1),
                        (:tid, :newer, 'completed', now() - interval '30 seconds',
                         now() - interval '20 seconds', 1)
                    """
                ),
                {"tid": seed_task[0], "older": older_worker, "newer": newer_worker},
            )
            await conn.execute(
                text(
                    "UPDATE workers SET status='online', last_heartbeat_at=now() "
                    "WHERE id IN (:older, :newer)"
                ),
                {"older": older_worker, "newer": newer_worker},
            )
    finally:
        await engine.dispose()


async def _last_reported_status(db_url: str, worker_id: str) -> str | None:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT last_reported_status FROM workers WHERE id = :wid"),
                    {"wid": worker_id},
                )
            ).first()
        assert row is not None
        return row[0]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fairness_does_not_yield_to_self_reported_busy_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """A worker that reports `busy` WITHOUT an execution does not force a yield.

    This is the disk-gated worker: `online`, fresh heartbeat, the oldest
    `max(assigned_at)` of the fleet and ZERO execution rows, so the
    active-execution exclusion does not reach it. It never claims (steps 5 and
    6 of the pick only run with `idle`), so if it forced a yield the whole
    fleet would sit idle with the tasks in `queued`.
    """
    db_url = _db_url(authed_admin_client)

    w_gated, key_gated = await _register(authed_admin_client, "disk-gated")
    w_idle, key_idle = await _register(authed_admin_client, "idle-claimer")

    await _seed_terminal_executions(db_url, older_worker=w_gated, newer_worker=w_idle)

    # The gated worker polls `busy` BEFORE the task exists: this way its
    # self-report is persisted through the real contract path (not direct SQL).
    assert await _poll(authed_admin_client, w_gated, key_gated, status="busy") is None
    assert await _last_reported_status(db_url, w_gated) == "busy"

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    task = await _poll(authed_admin_client, w_idle, key_idle)
    assert task is not None, (
        "The idle worker yielded to a worker that self-reported `busy` without "
        "an execution (disk gate): nobody claims and the tasks stay "
        "in `queued` with the fleet idle."
    )
    assert task["task_id"] == task_id


@pytest.mark.asyncio
async def test_fairness_still_yields_to_oldest_idle_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """The legitimate fairness yield is still alive: only the unavailable worker is excluded.

    Same scenario as the test above except for one thing: the worker with the
    oldest `max(assigned_at)` reports `idle`, so it CAN claim. The other must
    yield its turn (anti-starvation, TD-001) instead of keeping the task.
    """
    db_url = _db_url(authed_admin_client)

    w_older, key_older = await _register(authed_admin_client, "idle-oldest")
    w_newer, key_newer = await _register(authed_admin_client, "idle-newest")

    await _seed_terminal_executions(db_url, older_worker=w_older, newer_worker=w_newer)

    # The oldest polls `idle` BEFORE the task is created: if it polled afterwards
    # it would take the task itself and the other's assertion would be vacuous.
    assert await _poll(authed_admin_client, w_older, key_older) is None
    assert await _last_reported_status(db_url, w_older) == "idle"

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text

    task = await _poll(authed_admin_client, w_newer, key_newer)
    assert task is None, (
        "The fairness yield stopped working: the worker was supposed to yield "
        "to the idle worker with the oldest assigned_at."
    )

    # And the turn really is its own: when it polls it claims.
    assert await _poll(authed_admin_client, w_older, key_older) is not None


@pytest.mark.asyncio
async def test_fairness_yields_to_worker_with_null_last_reported_status(
    authed_admin_client: AsyncClient,
) -> None:
    """`NULL` = "unknown" = behavior prior to migration 0053.

    This is the pre-existing fleet on deploy day: registered workers that have
    not yet polled against the new server. Backward compatibility: while the
    column stays `NULL` the worker forces a yield exactly as before, and its
    behavior only changes once its first poll says `busy`.
    """
    db_url = _db_url(authed_admin_client)

    w_null, _key_null = await _register(authed_admin_client, "nunca-polleo")
    w_newer, key_newer = await _register(authed_admin_client, "idle-newest")

    # `w_null` NEVER polls: its heartbeat and age are seeded via SQL so that
    # the column stays untouched at NULL.
    await _seed_terminal_executions(db_url, older_worker=w_null, newer_worker=w_newer)
    assert await _last_reported_status(db_url, w_null) is None

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text

    task = await _poll(authed_admin_client, w_newer, key_newer)
    assert task is None, (
        "A worker with `last_reported_status` NULL stopped forcing a yield: "
        "migration 0053 changed the behavior of the pre-existing fleet."
    )


# --- Server-side disk gate ----------------------------------------------
#
# When the gated worker sends the fraction, that worker reports `idle`
# (not `busy`), so the `last_reported_status='busy'` exclusion would
# NO longer cover it and would reopen the deadlock: an idle gated worker, with
# the oldest `max(assigned_at)`, would force the fleet to yield a claim that it
# never takes (the server closes steps 4-6 for it). The fix is a third
# exclusion term in the fairness yield: by `free_disk_fraction` below the
# central floor.
# NULL = "unknown" = participates as before (fail-open).


async def _poll_with_fraction(
    client: AsyncClient,
    worker_id: str,
    api_key: str,
    *,
    status: str,
    free_disk_fraction: float | None,
) -> dict | None:
    body: dict = {"status": status, "lease_seconds": 30}
    if free_disk_fraction is not None:
        body["free_disk_fraction"] = free_disk_fraction
    resp = await client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": api_key},
        json=body,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["task"]


@pytest.mark.asyncio
async def test_fairness_does_not_yield_to_disk_gated_idle_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """A disk-gated worker that reports `idle` does not force a yield.

    This is the scenario that would reopen the deadlock after the worker PR:
    the gated worker reports `idle` (not `busy`), has the oldest
    `max(assigned_at)` and zero execution rows, so neither the active-execution
    exclusion nor the `busy` one reaches it. The only difference with the
    healthy worker is its `free_disk_fraction` below the floor.
    """
    db_url = _db_url(authed_admin_client)

    w_gated, key_gated = await _register(authed_admin_client, "disk-gated-idle")
    w_idle, key_idle = await _register(authed_admin_client, "idle-claimer")

    await _seed_terminal_executions(db_url, older_worker=w_gated, newer_worker=w_idle)

    # The gated worker polls `idle` with a low fraction BEFORE the task exists:
    # it persists its fraction through the real contract path. It claims nothing
    # (it is gated) and its last_reported_status stays 'idle'.
    assert (
        await _poll_with_fraction(
            authed_admin_client, w_gated, key_gated, status="idle", free_disk_fraction=0.05
        )
        is None
    )
    assert await _last_reported_status(db_url, w_gated) == "idle"

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    task = await _poll(authed_admin_client, w_idle, key_idle)
    assert task is not None, (
        "The idle worker yielded to a disk-gated worker that reports `idle`: "
        "the deadlock would reappear through a path the `busy` exclusion does not cover."
    )
    assert task["task_id"] == task_id


@pytest.mark.asyncio
async def test_fairness_still_yields_to_null_fraction_idle_worker(
    authed_admin_client: AsyncClient,
) -> None:
    """`free_disk_fraction` NULL = "unknown" = participates as before.

    The worker with the oldest assigned_at reports `idle` and does NOT send the
    fraction (older worker). It must keep forcing the legitimate fairness
    yield: the new term only excludes a worker that reports a fraction below
    the floor.
    """
    db_url = _db_url(authed_admin_client)

    w_older, key_older = await _register(authed_admin_client, "idle-no-fraction")
    w_newer, key_newer = await _register(authed_admin_client, "idle-newest")

    await _seed_terminal_executions(db_url, older_worker=w_older, newer_worker=w_newer)

    # The oldest polls `idle` WITHOUT a fraction → column stays NULL.
    assert (
        await _poll_with_fraction(
            authed_admin_client, w_older, key_older, status="idle", free_disk_fraction=None
        )
        is None
    )
    assert await _last_reported_status(db_url, w_older) == "idle"

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    assert create.status_code == 200, create.text

    task = await _poll(authed_admin_client, w_newer, key_newer)
    assert task is None, (
        "A worker with `free_disk_fraction` NULL stopped forcing a yield: the "
        "server-side disk gate must not change the behavior of the fleet that "
        "does not report the fraction."
    )
    # And the turn really belongs to the oldest: when it polls it claims.
    assert await _poll(authed_admin_client, w_older, key_older) is not None
