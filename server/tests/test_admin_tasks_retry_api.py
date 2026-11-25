"""Integration tests for `POST /admin/tasks/{id}/retry`.

In-place retry: the endpoint reuses the same `task_id` to run a new attempt
of a terminal task (unlike Fork, which creates a new task). It guarantees that
the worker's `ORCASTER_CACHE_DIR` can be reused across attempts
(key = idempotency_key or task_id).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

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


async def _exec_sql(db_url: str, sql: str, params: dict[str, Any]) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
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


async def _set_task_status(
    db_url: str, task_id: int, status: str, outcome: str | None = None
) -> None:
    await _exec_sql(
        db_url,
        "UPDATE tasks SET status = CAST(:s AS task_status), outcome = :outcome, "
        "completed_at = now(), last_error = 'boom' WHERE id = :tid",
        {"s": status, "outcome": outcome, "tid": task_id},
    )


async def _insert_execution(
    db_url: str,
    task_id: int,
    worker_id: str,
    status: str,
    *,
    started: bool = True,
    started_offset_seconds: int = 0,
) -> None:
    """Insert a row into task_executions. `started_offset_seconds` allows a
    deterministic tiebreak when several tests insert executions in
    parallel (now() can collide at the microsecond level)."""
    await _exec_sql(
        db_url,
        """
        INSERT INTO task_executions
            (task_id, worker_id, status, started_at, finished_at, exit_code)
        VALUES (:tid, :wid, CAST(:s AS execution_status),
                CASE WHEN :started
                     THEN now() + make_interval(secs => :off)
                     ELSE NULL END,
                now() + make_interval(secs => :off), 1)
        """,
        {
            "tid": task_id,
            "wid": worker_id,
            "s": status,
            "started": started,
            "off": started_offset_seconds,
        },
    )


# --- Schema defaults -------------------------------------------------------


def test_retry_request_schemas_default_to_any_worker() -> None:
    """Default `force_same_worker=False` in both per-task retry schemas:
    retrying on any eligible worker is the default behavior;
    "same worker" is an explicit opt-in."""
    from orcaster_server.schemas.api_v1 import RetryTaskRequest
    from orcaster_server.schemas.tasks import AdminRetryTaskRequest

    assert AdminRetryTaskRequest().force_same_worker is False
    assert RetryTaskRequest().force_same_worker is False


# --- 200: retry happy paths ------------------------------------------------


@pytest.mark.asyncio
async def test_retry_failed_task_transitions_to_queued(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Retrying a `failed` task puts it back in `queued`, clears the closing
    fields, and reports attempt_number=1 (no previous executions)."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "failed")

    resp = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/retry", json={"force_same_worker": True}
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["task_id"] == task_id
    assert j["status"] == "queued"
    # No previous attempts: attempt_number=1 (count(executions)+1 = 0+1).
    assert j["attempt_number"] == 1
    # No known last worker → no pivot to push; keeps the original
    # targeting (claim, target_worker_id=None).
    assert j["delivery_mode"] == "claim"
    assert j["target_worker_id"] is None

    row = await _row(
        migrated_db_url,
        "SELECT status, completed_at, last_error, cancelled_reason, "
        "target_count, attempt_budget, expires_at FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["completed_at"] is None
    assert row["last_error"] is None
    assert row["cancelled_reason"] is None
    # The retry bumps attempt_budget (budget); target_count
    # (nominal fan-out, success threshold) is not touched.
    assert row["target_count"] == 1
    assert row["attempt_budget"] == 2
    assert row["expires_at"] is None


@pytest.mark.asyncio
async def test_retry_completed_task_with_force_same_worker_pivots_to_claim_pool(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Retry with `force_same_worker=true` and a previously started attempt:
    the task ends up in `delivery_mode='claim'` with the last attempt's worker
    inside `target_worker_pool`, `target_worker_id` NULL.

    It does not pivot to `delivery_mode='push'`, which the worker rejects for
    non-reserved commands (`"non-reserved push tasks not supported by this
    worker"`). The `push` mode is reserved for signed admin commands.
    The canonical way to direct a user task to a specific worker is
    `delivery_mode='claim'` + `target_worker_pool=[<uuid>]`
    (equivalent to the `target='worker:<uuid>'` syntax).
    """
    worker_id, _ = await _register(authed_admin_client, "wA")
    # We also register another worker to verify it is NOT chosen.
    other_id, _ = await _register(authed_admin_client, "wB")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "5"})
    task_id = create.json()["task_id"]

    # Simulates a previous failed execution on wB (older) and a completed one on wA
    # (more recent → it is the "last worker").
    await _insert_execution(
        migrated_db_url, task_id, other_id, "failed", started_offset_seconds=-10
    )
    await _insert_execution(migrated_db_url, task_id, worker_id, "completed")
    await _set_task_status(migrated_db_url, task_id, "completed")

    resp = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/retry", json={"force_same_worker": True}
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "queued"
    # Uses claim + pool, not push.
    assert j["delivery_mode"] == "claim"
    # target_worker_id in the response reflects the target worker (for UI
    # and audit), although at DB level it lives in `target_worker_pool`.
    assert j["target_worker_id"] == worker_id
    # 2 previous executions → attempt_number=3.
    assert j["attempt_number"] == 3

    # DB state: claim + pool [worker_id], target_worker_id=NULL.
    row = await _row(
        migrated_db_url,
        "SELECT status, delivery_mode, target_worker_id::text AS target_worker_id, "
        "target_mode, target_count, attempt_budget, target_worker_pool "
        "FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["delivery_mode"] == "claim"
    # CHECK `tasks_target_worker_exclusive_chk` requires target_worker_id NULL
    # when target_mode='count' AND target_count=1 does not hold — here we
    # always leave it NULL because targeting goes via pool.
    assert row["target_worker_id"] is None
    assert row["target_mode"] == "count"
    # EVERY count-mode retry bumps attempt_budget by 1 — also
    # with force_same_worker. The `taken` predicate in /poll counts every
    # non-expired execution against the budget, so previous terminal ones
    # consume budget; the bump reopens exactly one slot for the re-claim.
    # target_count (nominal, success threshold) stays intact.
    assert row["target_count"] == 5
    assert row["attempt_budget"] == 6
    # The target worker stays in the pool (JSONB array of UUIDs as strings).
    assert row["target_worker_pool"] == [worker_id]

    # ALL previous executions are preserved (full history). The target
    # worker's previous execution (wA, completed) is still there with
    # attempt_index=1; the next /poll will insert a new one with
    # attempt_index=2. The other worker's execution (wB) also survives intact.
    execs = await _rows(
        migrated_db_url,
        "SELECT worker_id::text AS worker_id, status::text AS status, "
        "attempt_index FROM task_executions WHERE task_id=:id "
        "ORDER BY worker_id, attempt_index",
        {"id": task_id},
    )
    by_wid: dict[str, list[dict]] = {}
    for e in execs:
        by_wid.setdefault(e["worker_id"], []).append(e)
    # wA (worker_id) keeps its completed row with attempt_index=1.
    assert worker_id in by_wid
    assert by_wid[worker_id][0]["status"] == "completed"
    assert by_wid[worker_id][0]["attempt_index"] == 1
    # wB (other_id) keeps its failed row.
    assert by_wid[other_id][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_retry_completed_task_rerun_claims_exactly_once(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Re-running a `completed` task via manual retry opens
    exactly ONE new slot.

    Real flow: a worker completes the task (target=1), retry without
    force_same_worker → bump attempt_budget 1→2 (nominal target_count
    stays at 1). The previous completed execution consumes 1 of the budget
    under the non-expired predicate, so of the two workers that poll only
    one claims (with an active-only predicate both would have claimed:
    a latent double claim). When the re-run completes, completed=2 >= nominal
    1 → the task closes `completed` again.
    """
    w_a, k_a = await _register(authed_admin_client, "wA")
    w_b, k_b = await _register(authed_admin_client, "wB")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    poll = await authed_admin_client.post(
        f"/v1/workers/{w_a}/poll",
        headers={"X-API-Key": k_a},
        json={"status": "idle", "lease_seconds": 30},
    )
    token = poll.json()["task"]["lease_token"]
    done = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={"lease_token": token, "exit_code": 0, "status": "completed"},
    )
    assert done.status_code == 200, done.text

    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text
    row = await _row(
        migrated_db_url,
        "SELECT status, target_count, attempt_budget FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["target_count"] == 1
    assert row["attempt_budget"] == 2

    # Two workers poll: exactly one claims the only new slot.
    polls = []
    for wid, key in ((w_a, k_a), (w_b, k_b)):
        p = await authed_admin_client.post(
            f"/v1/workers/{wid}/poll",
            headers={"X-API-Key": key},
            json={"status": "idle", "lease_seconds": 30},
        )
        assert p.status_code == 200, p.text
        polls.append((key, p.json()))
    claimed = [(key, b) for key, b in polls if b["task"] is not None]
    assert len(claimed) == 1, (
        "the re-run must open exactly one slot",
        [b for _, b in polls],
    )
    rerun_key, rerun_body = claimed[0]
    done2 = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": rerun_key},
        json={
            "lease_token": rerun_body["task"]["lease_token"],
            "exit_code": 0,
            "status": "completed",
        },
    )
    assert done2.status_code == 200, done2.text

    row = await _row(
        migrated_db_url,
        "SELECT status FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "completed"
    execs = await _rows(
        migrated_db_url,
        "SELECT count(*) AS n FROM task_executions WHERE task_id=:id",
        {"id": task_id},
    )
    assert execs[0]["n"] == 2


@pytest.mark.asyncio
async def test_retry_cancelled_task_succeeds(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`cancelled` is also terminal and retryable."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = 'cancelled'::task_status, "
        "cancelled_reason = 'user asked', completed_at = now() WHERE id=:id",
        {"id": task_id},
    )

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "queued"

    row = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["cancelled_reason"] is None


# --- 409: non-terminal --------------------------------------------------


@pytest.mark.asyncio
async def test_retry_queued_task_returns_409(authed_admin_client: AsyncClient) -> None:
    """Retrying a non-terminal task returns 409."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    # `queued` is not terminal.
    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert resp.status_code == 409, resp.text
    detail = resp.json().get("detail", "")
    assert "queued" in detail
    assert "retry-able" in detail or "retry" in detail


@pytest.mark.asyncio
async def test_retry_running_task_returns_409(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Retrying a running task (forced via an intermediate state) returns 409."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    # Force to `dispatching` (an alternative non-terminal state, simulates running
    # from the task's point of view).
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = 'dispatching'::task_status WHERE id=:id",
        {"id": task_id},
    )
    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_retry_paused_task_returns_409(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET status = 'paused'::task_status WHERE id=:id",
        {"id": task_id},
    )
    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert resp.status_code == 409


# --- 404 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_missing_task_returns_404(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post("/admin/tasks/99999/retry", json={})
    assert resp.status_code == 404


# --- Empty body acceptable (defaults) ----------------------------------


@pytest.mark.asyncio
async def test_retry_empty_body_defaults_to_any_worker(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Default `force_same_worker=False`: a retry WITHOUT a body is not
    pinned to the last attempt's worker — it keeps the original targeting and
    goes back to the general pool. Contrast with
    test_retry_completed_task_with_force_same_worker_pivots_to_claim_pool,
    which sends an explicit `force_same_worker=true` and does pivot to claim+pool.
    """
    worker_id, _ = await _register(authed_admin_client, "wPrev")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # Previous attempt started on a specific worker + failed task.
    await _insert_execution(migrated_db_url, task_id, worker_id, "failed")
    await _set_task_status(migrated_db_url, task_id, "failed")

    before = await _row(
        migrated_db_url,
        "SELECT delivery_mode, target_worker_id::text AS target_worker_id, "
        "target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert before is not None

    # No explicit body ⇒ default False ⇒ general pool (not sticky).
    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry")
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "queued"
    # No pivot to a claim on the previous worker: the response reports no target_worker.
    assert j["target_worker_id"] is None

    after = await _row(
        migrated_db_url,
        "SELECT status, delivery_mode, target_worker_id::text AS target_worker_id, "
        "target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert after is not None
    assert after["status"] == "queued"
    # Original targeting intact (identical to the bulk retry semantics).
    assert after["delivery_mode"] == before["delivery_mode"]
    assert after["target_worker_id"] == before["target_worker_id"]
    assert after["target_worker_pool"] == before["target_worker_pool"]


@pytest.mark.asyncio
async def test_retry_default_preserves_existing_pool_after_sticky_retry(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Documented semantics: `force_same_worker`
    keeps the CURRENT targeting, it does not restore a "pristine" one. If a
    previous sticky retry left `target_worker_pool=[W]`, a later retry with the
    default False PRESERVES it — it does not unpin.

    This is intentional: at row level a sticky-retry pin is indistinguishable
    from a legitimate `target='worker:<uuid>'` (both are claim+pool), so
    clearing the pool would break the operator's explicit targeting. To release
    the pin there is Fork (new targeting) or the housekeeping `_relax_sticky_pool`
    (auto-retry). This test pins the contract so that any future change is
    deliberate.
    """
    worker_id, _ = await _register(authed_admin_client, "wSticky")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # 1) Attempt started on W + failed task → sticky retry pins to pool=[W].
    await _insert_execution(migrated_db_url, task_id, worker_id, "failed")
    await _set_task_status(migrated_db_url, task_id, "failed")
    sticky = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/retry", json={"force_same_worker": True}
    )
    assert sticky.status_code == 200, sticky.text
    assert sticky.json()["delivery_mode"] == "claim"
    assert sticky.json()["target_worker_id"] == worker_id

    pinned = await _row(
        migrated_db_url,
        "SELECT delivery_mode, target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert pinned is not None
    assert pinned["delivery_mode"] == "claim"
    assert pinned["target_worker_pool"] == [worker_id]

    # 2) Back to a terminal state and retry with the DEFAULT (False, empty body).
    await _set_task_status(migrated_db_url, task_id, "failed")
    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry")
    assert resp.status_code == 200, resp.text

    # The previous pin is PRESERVED (the default False does not clear it).
    after = await _row(
        migrated_db_url,
        "SELECT delivery_mode, target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert after is not None
    assert after["delivery_mode"] == "claim"
    assert after["target_worker_pool"] == [worker_id]


# --- Integration with /v1/workers/{id}/poll ----------------------------


@pytest.mark.asyncio
async def test_retry_makes_task_pollable_again(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """End-to-end check: after retry with force_same_worker, the
    worker receives the task on its next /poll."""
    worker_id, key = await _register(authed_admin_client)

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # Simulates a previous completed execution + completed task.
    await _insert_execution(migrated_db_url, task_id, worker_id, "completed")
    await _set_task_status(migrated_db_url, task_id, "completed")

    # Retry.
    retry = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/retry", json={"force_same_worker": True}
    )
    assert retry.status_code == 200, retry.text
    # The retry uses claim + pool, not push.
    # The worker rejects non-reserved push tasks.
    assert retry.json()["delivery_mode"] == "claim"
    assert retry.json()["target_worker_id"] == worker_id

    # The worker polls and must receive the same task_id again.
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
async def test_retry_pool_restricts_to_target_worker(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Regression: after retry with `force_same_worker=true`, the
    `target_worker_pool` must restrict the claim to the original worker —
    another worker that polls must NOT receive the task.

    This validates that `delivery_mode='claim' +
    target_worker_pool=[<uuid>]` gives the "sticky retry" behavior without
    falling into push delivery (which the worker rejects for non-reserved
    commands).
    """
    target_worker_id, target_key = await _register(authed_admin_client, "wTarget")
    other_worker_id, other_key = await _register(authed_admin_client, "wOther")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # Previous execution of target (it will be the sticky one after retry).
    # Old started_at so that when we later insert other_worker's, other's has a
    # greater `assigned_at` (the open-claim yield_check does not yield to
    # target). For target to remain "the last one" according to the endpoint
    # (which orders by started_at DESC), we force other's started_at to a
    # large NEGATIVE offset.
    await _insert_execution(migrated_db_url, task_id, target_worker_id, "completed")
    # Previous execution of other_worker on the SAME task with status
    # 'expired' (crash: does NOT consume budget — with 'failed' the fabricated
    # state would be unreachable: 2 non-expired with target_count=1 would violate
    # the predicate's invariant). Old started_at → it is not "the last worker"
    # for the retry. But `assigned_at` gets now() at insert time (later than
    # target's), which neutralizes the open-claim yield_check (other has a more
    # recent assigned_at, so it does not trigger a fairness yield).
    await _insert_execution(
        migrated_db_url,
        task_id,
        other_worker_id,
        "expired",
        started_offset_seconds=-100,
    )
    await _set_task_status(migrated_db_url, task_id, "completed")

    retry = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/retry", json={"force_same_worker": True}
    )
    assert retry.status_code == 200, retry.text
    j = retry.json()
    assert j["delivery_mode"] == "claim"
    assert j["target_worker_id"] == target_worker_id

    # The OTHER worker polls: it must NOT receive this task (the pool excludes it).
    poll_other = await authed_admin_client.post(
        f"/v1/workers/{other_worker_id}/poll",
        headers={"X-API-Key": other_key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll_other.status_code == 200
    assert poll_other.json()["task"] is None

    # The target worker polls: it MUST receive the task.
    poll_target = await authed_admin_client.post(
        f"/v1/workers/{target_worker_id}/poll",
        headers={"X-API-Key": target_key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll_target.status_code == 200
    task = poll_target.json()["task"]
    assert task is not None
    assert task["task_id"] == task_id


# --- admin_actions audit row -------------------------------------------


@pytest.mark.asyncio
async def test_retry_records_admin_action(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "failed")

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert resp.status_code == 200

    rows = await _rows(
        migrated_db_url,
        "SELECT action, target_kind, target_id, payload "
        "FROM admin_actions WHERE target_id = :tid AND action = 'retry_task'",
        {"tid": str(task_id)},
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "retry_task"
    assert r["target_kind"] == "task"
    assert r["payload"]["prior_status"] == "failed"
    # Empty body ⇒ default False; the audit reflects the flag used.
    assert r["payload"]["force_same_worker"] is False
    assert r["payload"]["attempt_number"] == 1


# --- History preservation -----------------------------------------------


@pytest.mark.asyncio
async def test_retry_preserves_history_across_multiple_attempts(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """After multiple retries on the same worker, all previous executions
    are preserved (no history is lost).

    Flow: we register a worker, run 3 retries+poll, and expect 4 total
    rows (attempt_index 1..4) — the initial snapshot (target=1 claim-mode)
    inserts no execution; each /poll does.
    """
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # Helper to run a poll + complete cycle on the task.
    async def _run_cycle(exit_code: int = 0, st: str = "completed") -> None:
        poll = await authed_admin_client.post(
            f"/v1/workers/{worker_id}/poll",
            headers={"X-API-Key": key},
            json={"status": "idle", "lease_seconds": 30},
        )
        assert poll.status_code == 200
        task = poll.json()["task"]
        assert task is not None and task["task_id"] == task_id
        complete = await authed_admin_client.post(
            f"/v1/tasks/{task_id}/complete",
            headers={"X-API-Key": key},
            json={
                "lease_token": task["lease_token"],
                "status": st,
                "exit_code": exit_code,
            },
        )
        assert complete.status_code == 200

    # Attempt 1 (original claim): poll + complete.
    await _run_cycle()

    # Retry × 3.
    for _ in range(3):
        retry = await authed_admin_client.post(
            f"/admin/tasks/{task_id}/retry", json={"force_same_worker": True}
        )
        assert retry.status_code == 200, retry.text
        await _run_cycle()

    # We expect 4 rows with attempt_index = 1..4.
    execs = await _rows(
        migrated_db_url,
        "SELECT attempt_index, status::text AS status "
        "FROM task_executions WHERE task_id=:tid AND worker_id=:wid "
        "ORDER BY attempt_index",
        {"tid": task_id, "wid": worker_id},
    )
    assert len(execs) == 4, f"expected 4 attempts, found {len(execs)}: {execs}"
    assert [e["attempt_index"] for e in execs] == [1, 2, 3, 4]
    # All terminal.
    assert all(e["status"] == "completed" for e in execs)


# --- all_available re-snapshot -----------------------------------------


async def _set_worker_offline(db_url: str, worker_id: str) -> None:
    await _exec_sql(
        db_url,
        "UPDATE workers SET status = 'offline' WHERE id = :id",
        {"id": worker_id},
    )


async def _complete_all_available(db_url: str, task_id: int) -> None:
    """Bring an already snapshotted `all_available` task to a terminal state:
    marks all its `assigned` executions as `completed` and the task as
    `completed`. Simulates every worker in the snapshot having finished."""
    await _exec_sql(
        db_url,
        "UPDATE task_executions SET status = 'completed', "
        "started_at = now(), finished_at = now(), exit_code = 0 "
        "WHERE task_id = :tid",
        {"tid": task_id},
    )
    await _exec_sql(
        db_url,
        "UPDATE tasks SET status = 'completed', completed_at = now() WHERE id = :tid",
        {"tid": task_id},
    )


@pytest.mark.asyncio
async def test_retry_all_available_resnapshots_online_workers(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Retrying an `all_available` task redoes the snapshot against the
    workers online NOW: it goes back to `dispatching`, preserves `target_mode`,
    and inserts new `assigned` executions (attempt_index incremented).
    It is NOT pinned to a single worker (ignores force_same_worker)."""
    wa, _ = await _register(authed_admin_client, "wA")
    wb, _ = await _register(authed_admin_client, "wB")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    body = create.json()
    task_id = body["task_id"]
    assert body["target_mode"] == "all_available"
    # The initial snapshot assigned the 2 online workers.
    assert body["target_count"] == 2

    await _complete_all_available(migrated_db_url, task_id)

    # force_same_worker default True — must be IGNORED for all_available.
    resp = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/retry", json={"force_same_worker": True}
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["status"] == "dispatching"
    assert j["target_worker_id"] is None

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "dispatching"
    assert row["target_mode"] == "all_available"
    assert row["target_count"] == 2

    # Each worker now has 2 executions: the old completed one (#1) and the
    # new assigned one (#2) from the re-snapshot.
    for wid in (wa, wb):
        execs = await _rows(
            migrated_db_url,
            "SELECT status::text AS status, attempt_index FROM task_executions "
            "WHERE task_id=:tid AND worker_id=:wid ORDER BY attempt_index",
            {"tid": task_id, "wid": wid},
        )
        assert [e["attempt_index"] for e in execs] == [1, 2]
        assert execs[0]["status"] == "completed"
        assert execs[1]["status"] == "assigned"


@pytest.mark.asyncio
async def test_retry_all_available_excludes_offline_worker(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Regression: an all_available retry must NOT hang waiting for an
    offline worker. The re-snapshot must run on the workers available at the
    time of the retry, even if there are fewer than in the original execution."""
    wa, _ = await _register(authed_admin_client, "wOnline")
    wb, _ = await _register(authed_admin_client, "wOffline")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    task_id = create.json()["task_id"]
    assert create.json()["target_count"] == 2

    await _complete_all_available(migrated_db_url, task_id)

    # wB goes offline before the retry.
    await _set_worker_offline(migrated_db_url, wb)

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dispatching"

    # target_count reflects the reduced snapshot (only wA online).
    row = await _row(
        migrated_db_url,
        "SELECT target_count FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["target_count"] == 1

    # wA gets a new assigned execution; wB does NOT (it keeps its old #1).
    wa_execs = await _rows(
        migrated_db_url,
        "SELECT status::text AS status, attempt_index FROM task_executions "
        "WHERE task_id=:tid AND worker_id=:wid ORDER BY attempt_index",
        {"tid": task_id, "wid": wa},
    )
    assert [e["attempt_index"] for e in wa_execs] == [1, 2]
    assert wa_execs[1]["status"] == "assigned"

    wb_execs = await _rows(
        migrated_db_url,
        "SELECT status::text AS status, attempt_index FROM task_executions "
        "WHERE task_id=:tid AND worker_id=:wid ORDER BY attempt_index",
        {"tid": task_id, "wid": wb},
    )
    assert len(wb_execs) == 1
    assert wb_execs[0]["attempt_index"] == 1


@pytest.mark.asyncio
async def test_retry_restricted_all_available_all_offline_marks_failed(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Retrying an `all:<ids>` whose pool is 100% offline at the time of the
    retry → the task closes `failed/no_eligible_workers` (same closing
    as on create with an empty snapshot)."""
    wa, _ = await _register(authed_admin_client, "wRestricted")

    create = await authed_admin_client.post("/admin/tasks", json={"target": f"all:{wa}"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    await _complete_all_available(migrated_db_url, task_id)
    await _set_worker_offline(migrated_db_url, wa)

    resp = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"

    row = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["cancelled_reason"] == "no_eligible_workers"


@pytest.mark.asyncio
async def test_partial_unique_blocks_two_active_executions_same_pair(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The PARTIAL UNIQUE `(task_id, worker_id) WHERE status IN
    ('assigned','claimed','running')` prevents having two "live"
    executions for the same pair. Trying to insert a second `claimed`
    row must fail with IntegrityError. Terminal rows do NOT
    count (tested in test_retry_preserves_history)."""
    from sqlalchemy.exc import IntegrityError

    worker_id, _ = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # 1st 'claimed' row (manual, simulates a claim). attempt_index=1.
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status, attempt_index) "
        "VALUES (:tid, :wid, 'claimed', 1)",
        {"tid": task_id, "wid": worker_id},
    )

    # 2nd 'claimed' row with attempt_index=2 — MUST fail because of the
    # PARTIAL UNIQUE, although the stable UNIQUE (task_id, worker_id,
    # attempt_index) would syntactically allow it.
    with pytest.raises(IntegrityError):
        await _exec_sql(
            migrated_db_url,
            "INSERT INTO task_executions (task_id, worker_id, status, attempt_index) "
            "VALUES (:tid, :wid, 'claimed', 2)",
            {"tid": task_id, "wid": worker_id},
        )

    # But if the 1st becomes terminal, we can insert another active one.
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'completed', finished_at = now() "
        "WHERE task_id = :tid AND worker_id = :wid AND attempt_index = 1",
        {"tid": task_id, "wid": worker_id},
    )
    await _exec_sql(
        migrated_db_url,
        "INSERT INTO task_executions (task_id, worker_id, status, attempt_index) "
        "VALUES (:tid, :wid, 'claimed', 2)",
        {"tid": task_id, "wid": worker_id},
    )
    rows = await _rows(
        migrated_db_url,
        "SELECT attempt_index, status::text AS status "
        "FROM task_executions WHERE task_id=:tid AND worker_id=:wid "
        "ORDER BY attempt_index",
        {"tid": task_id, "wid": worker_id},
    )
    assert len(rows) == 2
    assert rows[0]["status"] == "completed"
    assert rows[1]["status"] == "claimed"


# --- push task retry: retarget to claim+pool ----------------------------


@pytest.mark.asyncio
async def test_retry_push_task_force_same_worker_retargets_then_bumps(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Regression: the `force_same_worker=true` retry
    of a PUSH task (control-op, non-NULL `target_worker_id`) retargets to
    claim+pool and bumps the budget without raising IntegrityError.

    The bump touches `attempt_budget` (not `target_count`), so the CHECK
    `tasks_target_worker_exclusive_chk` (NOT deferrable; requires
    `target_count=1` while `target_worker_id` is not NULL) is unreachable by
    this path; the test covers the whole flow: 200, `delivery_mode='claim'`,
    `target_worker_id` NULL in DB, `pool=[wid]`, `target_count=1` intact,
    `attempt_budget=2`.
    """
    wid, key = await _register(authed_admin_client)

    # Real push task via control-op (delivery_mode='push', target_worker_id=wid).
    op = await authed_admin_client.post(
        f"/admin/workers/{wid}/control-op",
        json={"control_op": "__reload_config__", "payload": {}},
    )
    assert op.status_code == 200, op.text
    task_id = op.json()["task_id"]

    # The worker receives it via /poll (push delivery) and completes it.
    poll = await authed_admin_client.post(
        f"/v1/workers/{wid}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert poll.status_code == 200, poll.text
    task = poll.json()["task"]
    assert task is not None and task["task_id"] == task_id
    assert task["delivery_mode"] == "push"
    done = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": task["lease_token"], "exit_code": 0, "status": "completed"},
    )
    assert done.status_code == 200, done.text

    # Retry with force_same_worker: retarget push→claim+pool and bump.
    retry = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/retry", json={"force_same_worker": True}
    )
    assert retry.status_code == 200, retry.text
    j = retry.json()
    assert j["status"] == "queued"
    assert j["delivery_mode"] == "claim"
    assert j["target_worker_id"] == wid

    row = await _row(
        migrated_db_url,
        "SELECT delivery_mode, target_worker_id::text AS target_worker_id, "
        "target_mode, target_count, attempt_budget, target_worker_pool "
        "FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["delivery_mode"] == "claim"
    assert row["target_worker_id"] is None
    assert row["target_mode"] == "count"
    assert row["target_worker_pool"] == [wid]
    # The bump touches attempt_budget (budget, reopens exactly
    # one slot); target_count (nominal) stays intact and the CHECK does not apply.
    assert row["target_count"] == 1
    assert row["attempt_budget"] == 2


# --- retry_count counter -----------------------------------------------


@pytest.mark.asyncio
async def test_retry_increments_retry_count(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Each in-place retry increments tasks.retry_count and reports it in the
    response and in the admin_actions payload. It does not create a new task."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    await _set_task_status(migrated_db_url, task_id, "failed")
    r1 = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert r1.status_code == 200, r1.text
    assert r1.json()["retry_count"] == 1

    await _set_task_status(migrated_db_url, task_id, "failed")
    r2 = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert r2.json()["retry_count"] == 2

    row = await _row(migrated_db_url, "SELECT retry_count FROM tasks WHERE id=:id", {"id": task_id})
    assert row is not None
    assert row["retry_count"] == 2

    actions = await _rows(
        migrated_db_url,
        "SELECT payload FROM admin_actions WHERE action='retry_task' AND target_id=:tid "
        "ORDER BY ts",
        {"tid": str(task_id)},
    )
    assert actions[-1]["payload"]["retry_count"] == 2


# --- bulk retry: POST /admin/tasks/bulk/retry (shared semantics) --------


@pytest.mark.asyncio
async def test_bulk_retry_count_mode_resets_to_queued_and_bumps_budget(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Bulk retry of a failed count-mode task: status → `queued`,
    `attempt_budget + 1`, `retry_count + 1`, closing fields
    (`completed_at`/`last_error`/`cancelled_reason`) and `expires_at` → NULL."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "failed")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET cancelled_reason = 'boom', "
        "expires_at = now() + interval '1 hour' WHERE id = :tid",
        {"tid": task_id},
    )

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retried"] == [task_id]

    row = await _row(
        migrated_db_url,
        "SELECT status, completed_at, last_error, cancelled_reason, expires_at, "
        "target_count, attempt_budget, retry_count FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["completed_at"] is None
    assert row["last_error"] is None
    assert row["cancelled_reason"] is None
    assert row["expires_at"] is None
    # Budget bump; target_count (nominal) intact.
    assert row["target_count"] == 1
    assert row["attempt_budget"] == 2
    assert row["retry_count"] == 1


@pytest.mark.asyncio
async def test_bulk_retry_cancelled_resets_to_queued_and_bumps_budget(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Bulk retry of a count-mode `cancelled` task: it is retried just like a
    `failed` one (status -> `queued`, `attempt_budget + 1`, `retry_count + 1`,
    and `cancelled_reason` is cleared). The bulk covers the unsuccessful terminal states."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "cancelled")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET cancelled_reason = 'outer_timeout' WHERE id = :tid",
        {"tid": task_id},
    )

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retried"] == [task_id]
    assert resp.json()["skipped"] == []

    row = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason, attempt_budget, retry_count FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["cancelled_reason"] is None
    assert row["attempt_budget"] == 2
    assert row["retry_count"] == 1


@pytest.mark.asyncio
async def test_bulk_retry_skips_completed_states(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The bulk stays strict with the SUCCESSFUL terminal `completed`: it is not
    retried (avoids re-running already successful work on a select-all). Covers
    both a plain `completed` and one with an outcome: both are `completed` at
    status level, so both must be skipped with the same reason."""
    create_plain = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    tid_plain = create_plain.json()["task_id"]
    await _set_task_status(migrated_db_url, tid_plain, "completed")

    create_outcome = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    tid_outcome = create_outcome.json()["task_id"]
    await _set_task_status(migrated_db_url, tid_outcome, "completed", outcome="no_result")

    resp = await authed_admin_client.post(
        "/admin/tasks/bulk/retry", json={"task_ids": [tid_plain, tid_outcome]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retried"] == []
    reasons = {s["id"]: s["reason"] for s in body["skipped"]}
    assert reasons[tid_plain] == "not failed/cancelled: completed"
    assert reasons[tid_outcome] == "not failed/cancelled: completed"


@pytest.mark.asyncio
async def test_bulk_retry_does_not_retarget_to_failed_worker(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`force_same_worker=False` semantics: after the bulk retry,
    `delivery_mode`, `target_worker_id` and `target_worker_pool` stay
    intact — the task is NOT pinned to the worker that failed (contrast with
    test_retry_completed_task_with_force_same_worker_pivots_to_claim_pool)."""
    worker_id, _ = await _register(authed_admin_client, "wFailed")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # Previous failed execution of that worker + failed task.
    await _insert_execution(migrated_db_url, task_id, worker_id, "failed")
    await _set_task_status(migrated_db_url, task_id, "failed")

    before = await _row(
        migrated_db_url,
        "SELECT delivery_mode, target_worker_id::text AS target_worker_id, "
        "target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert before is not None

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retried"] == [task_id]

    after = await _row(
        migrated_db_url,
        "SELECT status, delivery_mode, target_worker_id::text AS target_worker_id, "
        "target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert after is not None
    assert after["status"] == "queued"
    # Original targeting intact: no pivot to claim+pool on the last worker.
    assert after["delivery_mode"] == before["delivery_mode"]
    assert after["target_worker_id"] == before["target_worker_id"]
    assert after["target_worker_pool"] == before["target_worker_pool"]
    assert after["target_worker_pool"] is None


@pytest.mark.asyncio
async def test_bulk_retry_makes_task_pollable_again(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """End-to-end: after the bulk retry, any worker claims the task
    on /poll — also verifies that the `attempt_budget` bump reopened the
    claim (the previous failed execution consumes budget)."""
    worker_id, key = await _register(authed_admin_client)

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    # Previous failed execution (consumes the initial budget) + failed task.
    await _insert_execution(migrated_db_url, task_id, worker_id, "failed")
    await _set_task_status(migrated_db_url, task_id, "failed")

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retried"] == [task_id]

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
async def test_bulk_retry_replay_skips_already_queued(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Safe replay: a second identical POST classifies the task (already
    `queued`) as skipped — no double bump of `attempt_budget` or
    `retry_count` (same style as test_bulk_cancel_idempotent_on_replay)."""
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]
    await _set_task_status(migrated_db_url, task_id, "failed")

    r1 = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert r1.status_code == 200 and r1.json()["retried"] == [task_id]

    r2 = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert r2.status_code == 200
    body = r2.json()
    assert body["retried"] == []
    assert [s["id"] for s in body["skipped"]] == [task_id]
    assert body["skipped"][0]["reason"] == "not failed/cancelled: queued"
    assert body["not_found"] == []

    row = await _row(
        migrated_db_url,
        "SELECT attempt_budget, retry_count FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["attempt_budget"] == 2  # a single bump (1 → 2)
    assert row["retry_count"] == 1  # a single retry counted


@pytest.mark.asyncio
async def test_bulk_retry_all_available_resnapshots_online_workers(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Bulk retry of a failed `all_available` task: it goes back to `dispatching`,
    bumps `snapshot_epoch` (new epoch BEFORE the re-snapshot),
    inserts new `assigned` rows for the online workers and resyncs
    `target_count` to the snapshot size. No +1 bump of `attempt_budget`
    (the all_available branch does not run it; `_SQL_UPDATE_TARGET_COUNT`
    resyncs it to the snapshot)."""
    wa, _ = await _register(authed_admin_client, "wA")
    wb, _ = await _register(authed_admin_client, "wB")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]
    assert create.json()["target_count"] == 2

    # Close the task as failed: terminal executions + failed status.
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'failed', "
        "started_at = now(), finished_at = now(), exit_code = 1 "
        "WHERE task_id = :tid",
        {"tid": task_id},
    )
    await _set_task_status(migrated_db_url, task_id, "failed")

    epoch_before = await _row(
        migrated_db_url,
        "SELECT snapshot_epoch FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert epoch_before is not None

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retried"] == [task_id]

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count, attempt_budget, "
        "snapshot_epoch, retry_count FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "dispatching"
    assert row["target_mode"] == "all_available"
    assert row["target_count"] == 2
    # Budget resynced to the snapshot (no count-mode +1 bump).
    assert row["attempt_budget"] == 2
    # New epoch opened before the re-snapshot.
    assert row["snapshot_epoch"] == epoch_before["snapshot_epoch"] + 1
    assert row["retry_count"] == 1

    # Each worker: the old failed one (#1) + the new assigned one (#2) stamped
    # with the bumped epoch.
    for wid in (wa, wb):
        execs = await _rows(
            migrated_db_url,
            "SELECT status::text AS status, attempt_index, snapshot_epoch "
            "FROM task_executions WHERE task_id=:tid AND worker_id=:wid "
            "ORDER BY attempt_index",
            {"tid": task_id, "wid": wid},
        )
        assert [e["attempt_index"] for e in execs] == [1, 2]
        assert execs[0]["status"] == "failed"
        assert execs[1]["status"] == "assigned"
        assert execs[1]["snapshot_epoch"] == row["snapshot_epoch"]


@pytest.mark.asyncio
async def test_bulk_retry_cancelled_all_available_resnapshots(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Bulk retry of a `cancelled` `all_available` task (not only `failed`):
    it takes the same re-snapshot branch as the failed case (target_mode decides
    the branch, not the original status) — it goes back to `dispatching`, bumps
    `snapshot_epoch`, clears `cancelled_reason` and adds new `assigned`
    executions. Fills a coverage gap: cancelled was only covered in
    count-mode (test_bulk_retry_cancelled_resets_to_queued_and_bumps_budget)."""
    wa, _ = await _register(authed_admin_client, "wA")
    wb, _ = await _register(authed_admin_client, "wB")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]
    assert create.json()["target_count"] == 2

    # Close the task as cancelled: terminal executions + cancelled status
    # + cancelled_reason populated (same pattern as
    # test_bulk_retry_cancelled_resets_to_queued_and_bumps_budget).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'cancelled', "
        "started_at = now(), finished_at = now() WHERE task_id = :tid",
        {"tid": task_id},
    )
    await _set_task_status(migrated_db_url, task_id, "cancelled")
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET cancelled_reason = 'outer_timeout' WHERE id = :tid",
        {"tid": task_id},
    )

    epoch_before = await _row(
        migrated_db_url,
        "SELECT snapshot_epoch FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert epoch_before is not None

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retried"] == [task_id]
    assert resp.json()["skipped"] == []

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count, cancelled_reason, "
        "snapshot_epoch, retry_count FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "dispatching"
    assert row["target_mode"] == "all_available"
    assert row["target_count"] == 2
    assert row["cancelled_reason"] is None
    # New epoch opened before the re-snapshot.
    assert row["snapshot_epoch"] == epoch_before["snapshot_epoch"] + 1
    assert row["retry_count"] == 1

    # Each worker: the old cancelled one (#1) + the new assigned one (#2) stamped
    # with the bumped epoch.
    for wid in (wa, wb):
        execs = await _rows(
            migrated_db_url,
            "SELECT status::text AS status, attempt_index, snapshot_epoch "
            "FROM task_executions WHERE task_id=:tid AND worker_id=:wid "
            "ORDER BY attempt_index",
            {"tid": task_id, "wid": wid},
        )
        assert [e["attempt_index"] for e in execs] == [1, 2]
        assert execs[0]["status"] == "cancelled"
        assert execs[1]["status"] == "assigned"
        assert execs[1]["snapshot_epoch"] == row["snapshot_epoch"]


@pytest.mark.asyncio
async def test_bulk_retry_all_available_restricted_pool_offline_stays_failed(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """`all:<ids>` with the pool 100% offline at retry: the id GOES in `retried`
    (the retry was applied: epoch bumped, fields reset, retry_count + 1)
    even though the task closes again as `failed/no_eligible_workers` — mirrors
    the individual retry, which answers `status: "failed"` in this case."""
    wa, _ = await _register(authed_admin_client, "wRestricted")

    create = await authed_admin_client.post("/admin/tasks", json={"target": f"all:{wa}"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    await _complete_all_available(migrated_db_url, task_id)
    await _set_task_status(migrated_db_url, task_id, "failed")
    await _set_worker_offline(migrated_db_url, wa)

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retried"] == [task_id]
    assert body["skipped"] == []
    assert body["not_found"] == []

    row = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason, retry_count FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["cancelled_reason"] == "no_eligible_workers"
    assert row["retry_count"] == 1


@pytest.mark.asyncio
async def test_bulk_retry_preserves_prior_executions(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """History: previous terminal executions remain in the table after the bulk
    retry — the retry path does NOT touch executions (it does not run
    _SQL_CANCEL_EXECUTIONS; that SQL belongs to cancel)."""
    worker_id, _ = await _register(authed_admin_client)

    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    await _insert_execution(migrated_db_url, task_id, worker_id, "failed")
    await _set_task_status(migrated_db_url, task_id, "failed")

    resp = await authed_admin_client.post("/admin/tasks/bulk/retry", json={"task_ids": [task_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retried"] == [task_id]

    execs = await _rows(
        migrated_db_url,
        "SELECT worker_id::text AS worker_id, status::text AS status, "
        "attempt_index FROM task_executions WHERE task_id=:tid "
        "ORDER BY attempt_index",
        {"tid": task_id},
    )
    # The previous failed execution survives intact; count-mode does not insert
    # new rows up front (the claim on the next /poll will).
    assert len(execs) == 1
    assert execs[0]["worker_id"] == worker_id
    assert execs[0]["status"] == "failed"
    assert execs[0]["attempt_index"] == 1
