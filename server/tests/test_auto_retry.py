"""Tests for the sticky auto-retry.

Covers the scenario matrix:

1.  Task without policy → normal terminal close (backward compatibility).
2.  Policy `max_attempts=0` → off.
3.  exit_code=75 with policy → sticky retry (pool=[worker_uuid]).
4.  exit_code=1 with default policy → terminal.
5.  attempts_used == max_attempts → terminal (budget exhausted).
6.  attempts_used is incremented on each retry.
7.  Cancelled with a retry-able reason → retry.
8.  Cancelled with a NON retry-able reason → terminal.
9.  sticky=False → attempt_budget bump instead of retarget.
10. Housekeeping `_relax_sticky_pool`: worker offline > timeout → pool cleared.
11. Housekeeping respects an online worker: pool preserved.
12. Multi-retry: 3 consecutive attempts on the same worker, the last exceeds the budget.
13. backoff_seconds applied to run_at.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.housekeeping import _relax_sticky_pool
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


# --- helpers ---------------------------------------------------------------


async def _register(client: AsyncClient, hostname: str = "w") -> tuple[str, str]:
    token = await mint_token_via_app()
    resp = await client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _poll(client: AsyncClient, worker_id: str, key: str) -> dict:
    resp = await client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


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


async def _create_task_with_policy(
    client: AsyncClient,
    *,
    target: str = "1",
    auto_retry: dict | None = None,
    payload: dict | None = None,
) -> int:
    body: dict[str, Any] = {"target": target}
    if auto_retry is not None:
        body["auto_retry"] = auto_retry
    if payload is not None:
        body["payload"] = payload
    resp = await client.post("/admin/tasks", json=body)
    assert resp.status_code == 200, resp.text
    return int(resp.json()["task_id"])


async def _claim_lease(client: AsyncClient, worker_id: str, key: str) -> str:
    body = await _poll(client, worker_id, key)
    assert body["task"] is not None, "expected a task to be claimed"
    return body["task"]["lease_token"]


async def _complete(
    client: AsyncClient,
    *,
    task_id: int,
    worker_key: str,
    lease_token: str,
    status: str,
    exit_code: int | None = None,
    cancelled_reason: str | None = None,
) -> int:
    payload: dict[str, Any] = {
        "lease_token": lease_token,
        "status": status,
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if cancelled_reason is not None:
        payload["cancelled_reason"] = cancelled_reason
    resp = await client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": worker_key},
        json=payload,
    )
    return resp.status_code


# --- 1. No policy → back-compat -------------------------------------------


@pytest.mark.asyncio
async def test_no_policy_preserves_terminal_behavior(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(authed_admin_client, target="1")
    token = await _claim_lease(authed_admin_client, worker_id, key)

    code = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    assert code == 200

    row = await _row(
        migrated_db_url,
        "SELECT status, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    # Without a policy: exit_code=75 still closes as terminal.
    assert row["status"] == "completed"
    assert row["auto_retry_attempts_used"] == 0


# --- 2. max_attempts=0 → off ----------------------------------------------


@pytest.mark.asyncio
async def test_policy_max_attempts_zero_is_off(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(authed_admin_client, auto_retry={"max_attempts": 0})
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "completed"
    assert row["auto_retry_attempts_used"] == 0


# --- 3. exit_code=75 with policy → sticky retry ---------------------------


@pytest.mark.asyncio
async def test_exit_75_with_policy_triggers_sticky_retry(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client, auto_retry={"max_attempts": 3, "backoff_seconds": 0}
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count, attempt_budget, "
        "target_worker_pool, auto_retry_attempts_used, delivery_mode "
        "FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["delivery_mode"] == "claim"
    assert row["target_mode"] == "count"
    assert row["target_worker_pool"] == [worker_id]
    assert row["auto_retry_attempts_used"] == 1
    # The sticky branch also bumps attempt_budget (+1 per retry).
    # The terminal execution that triggered the retry consumes budget under the
    # non-expired predicate; the bump reopens exactly one slot. target_count
    # (nominal, success threshold) stays intact.
    assert row["target_count"] == 1
    assert row["attempt_budget"] == 2


# --- 4. exit_code=1 (not retry-able) → terminal ---------------------------


@pytest.mark.asyncio
async def test_exit_1_not_in_retryable_list_terminal(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(authed_admin_client, auto_retry={"max_attempts": 3})
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="failed",
        exit_code=1,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    # exit_code=1 is not in retryable_exit_codes (default [75]).
    assert row["status"] == "failed"
    assert row["auto_retry_attempts_used"] == 0


# --- 4b. status='failed' + retryable exit_code → retry --------------------


@pytest.mark.asyncio
async def test_failed_status_with_retryable_exit_triggers_retry(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The real worker reports `status='failed'+exit_code=75` when the shell
    exits with 75 (the dispatcher returns 'error', which is mapped to
    'failed'). The policy must still trigger a retry: the server decides
    whether the code is retry-able, not the worker."""
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client, auto_retry={"max_attempts": 3, "backoff_seconds": 0}
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="failed",
        exit_code=75,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, auto_retry_attempts_used, target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["auto_retry_attempts_used"] == 1
    assert row["target_worker_pool"] == [worker_id]


# --- 5. attempts_used == max_attempts → terminal --------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_closes_terminal(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client, auto_retry={"max_attempts": 2, "backoff_seconds": 0}
    )
    # Pre-set attempts_used to 2 (equal to the budget) to force exhaustion.
    await _exec_sql(
        migrated_db_url,
        "UPDATE tasks SET auto_retry_attempts_used = 2 WHERE id = :id",
        {"id": task_id},
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "completed"
    # The counter is not incremented when closing as terminal.
    assert row["auto_retry_attempts_used"] == 2


# --- 6. Counter increments per retry --------------------------------------


@pytest.mark.asyncio
async def test_counter_increments(authed_admin_client: AsyncClient, migrated_db_url: str) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client, auto_retry={"max_attempts": 5, "backoff_seconds": 0}
    )
    # First cycle.
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    row = await _row(
        migrated_db_url,
        "SELECT auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None and row["auto_retry_attempts_used"] == 1

    # Second cycle.
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    row = await _row(
        migrated_db_url,
        "SELECT auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None and row["auto_retry_attempts_used"] == 2


# --- 7. Cancelled with retry-able reason → retry --------------------------


@pytest.mark.asyncio
async def test_cancelled_with_retryable_reason_triggers_retry(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={
            "max_attempts": 3,
            "backoff_seconds": 0,
            "retryable_cancelled_reasons": ["outer_timeout"],
        },
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="cancelled",
        cancelled_reason="outer_timeout",
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, target_count, attempt_budget, target_worker_pool, "
        "auto_retry_attempts_used, cancelled_reason FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["target_worker_pool"] == [worker_id]
    assert row["auto_retry_attempts_used"] == 1
    # The sticky retry bumps attempt_budget (+1 per retry);
    # target_count (nominal) stays intact.
    assert row["target_count"] == 1
    assert row["attempt_budget"] == 2
    # _SQL_RESET_TASK_QUEUED clears cancelled_reason on every retry.
    assert row["cancelled_reason"] is None


# --- 8. Cancelled with non-retry-able reason → terminal -------------------


@pytest.mark.asyncio
async def test_cancelled_with_non_retryable_reason_terminal(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={
            "max_attempts": 3,
            "retryable_cancelled_reasons": ["outer_timeout"],
        },
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="cancelled",
        cancelled_reason="user_request",
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "cancelled"
    assert row["cancelled_reason"] == "user_request"
    assert row["auto_retry_attempts_used"] == 0


# --- 9. sticky=False → bump attempt_budget, no retarget --------------------


@pytest.mark.asyncio
async def test_sticky_false_bumps_attempt_budget(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        target="1",
        auto_retry={
            "max_attempts": 3,
            "sticky": False,
            "backoff_seconds": 0,
        },
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, target_count, attempt_budget, target_worker_pool, "
        "auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    # sticky=False: the pool is NOT set; the task stays open to any
    # eligible worker.
    assert row["target_worker_pool"] is None
    # attempt_budget bumped by 1 (was 1, now 2) to reopen budget;
    # target_count (nominal) stays intact.
    assert row["target_count"] == 1
    assert row["attempt_budget"] == 2
    assert row["auto_retry_attempts_used"] == 1


# --- 10. Housekeeping: stale sticky pool gets relaxed ---------------------


@pytest.mark.asyncio
async def test_relax_sticky_pool_when_worker_offline(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={
            "max_attempts": 3,
            "sticky_offline_timeout_seconds": 60,
            "backoff_seconds": 0,
        },
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    # The task is queued + pool=[worker_id]. Age the worker's heartbeat
    # beyond sticky_offline_timeout_seconds (60s).
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET last_heartbeat_at = now() - interval '120 seconds' WHERE id = :id",
        {"id": worker_id},
    )

    engine = create_async_engine(migrated_db_url)
    try:
        n = await _relax_sticky_pool(engine)
    finally:
        await engine.dispose()
    assert n == 1

    row = await _row(
        migrated_db_url,
        "SELECT target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["target_worker_pool"] is None


# --- 10b. relax pool does not over-open slots -----------------------------------


@pytest.mark.asyncio
async def test_relax_sticky_pool_opens_exactly_one_slot(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The sticky retry bump (+1) does NOT over-open budget
    when housekeeping relaxes the pool.

    After the sticky retry, attempt_budget=2 with 1 terminal execution that
    consumes budget under the non-expired predicate → exactly
    1 slot remains. If the sticky worker dies and `_relax_sticky_pool` opens the pool,
    two new competing workers must produce exactly 1 claim
    (with an active-only predicate, such a bump would have allowed 2).
    """
    worker_id, key = await _register(authed_admin_client, "wSticky")
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={
            "max_attempts": 3,
            "sticky_offline_timeout_seconds": 60,
            "backoff_seconds": 0,
        },
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    # Sticky retry applied: queued, pool=[worker], attempt_budget=2.
    await _exec_sql(
        migrated_db_url,
        "UPDATE workers SET last_heartbeat_at = now() - interval '120 seconds' WHERE id = :id",
        {"id": worker_id},
    )
    engine = create_async_engine(migrated_db_url)
    try:
        n = await _relax_sticky_pool(engine)
    finally:
        await engine.dispose()
    assert n == 1

    w_b, k_b = await _register(authed_admin_client, "wB")
    w_c, k_c = await _register(authed_admin_client, "wC")
    body_b = await _poll(authed_admin_client, w_b, k_b)
    body_c = await _poll(authed_admin_client, w_c, k_c)
    got = [b for b in (body_b, body_c) if b["task"] is not None]
    assert len(got) == 1, (
        "exactly one worker must take the single reopened slot",
        body_b,
        body_c,
    )
    assert got[0]["task"]["task_id"] == task_id


# --- 11. Housekeeping respects fresh heartbeat ----------------------------


@pytest.mark.asyncio
async def test_relax_sticky_pool_keeps_pool_when_worker_online(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={
            "max_attempts": 3,
            "sticky_offline_timeout_seconds": 60,
            "backoff_seconds": 0,
        },
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    # The worker just completed → fresh heartbeat. The pool must not be relaxed.
    engine = create_async_engine(migrated_db_url)
    try:
        n = await _relax_sticky_pool(engine)
    finally:
        await engine.dispose()
    assert n == 0

    row = await _row(
        migrated_db_url,
        "SELECT target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["target_worker_pool"] == [worker_id]


# --- 12. Multi-retry: 3 attempts then exhaust -----------------------------


@pytest.mark.asyncio
async def test_multi_retry_exhausts_budget(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={"max_attempts": 2, "backoff_seconds": 0},
    )
    # Cycle 1: retry.
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    # Cycle 2: retry.
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    # Cycle 3: budget exhausted (attempts_used=2 == max_attempts=2) → terminal.
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "completed"
    assert row["auto_retry_attempts_used"] == 2


# --- 13. backoff_seconds delays run_at ------------------------------------


@pytest.mark.asyncio
async def test_backoff_seconds_pushes_run_at(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client,
        auto_retry={"max_attempts": 3, "backoff_seconds": 120},
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    row = await _row(
        migrated_db_url,
        "SELECT status, EXTRACT(EPOCH FROM (run_at - now())) AS delay_secs FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["status"] == "queued"
    # Backoff of 120s; allow a 60..130 window to tolerate test
    # latency.
    assert 60 < float(row["delay_secs"]) <= 130


# --- 14. Audit row recorded -----------------------------------------------


@pytest.mark.asyncio
async def test_auto_retry_writes_audit_row(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(authed_admin_client)
    task_id = await _create_task_with_policy(
        authed_admin_client, auto_retry={"max_attempts": 3, "backoff_seconds": 0}
    )
    token = await _claim_lease(authed_admin_client, worker_id, key)
    await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=key,
        lease_token=token,
        status="completed",
        exit_code=75,
    )

    row = await _row(
        migrated_db_url,
        "SELECT action, target_id, payload FROM admin_actions "
        "WHERE action = 'auto_retry_task' AND target_id = :tid "
        "ORDER BY ts DESC LIMIT 1",
        {"tid": str(task_id)},
    )
    assert row is not None
    assert row["action"] == "auto_retry_task"
    payload = row["payload"]
    assert payload["sticky"] is True
    assert payload["attempts_used"] == 1
    assert payload["max_attempts"] == 3
    assert payload["trigger_exit_code"] == 75
    assert payload["sticky_worker_id"] == worker_id


# --- 15. all_available→sticky resync budget to in-flight -------------------


@pytest.mark.asyncio
async def test_all_available_sticky_retry_resyncs_budget_excluding_expired(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The `all_available`→sticky conversion must NOT blindly bump `+1`
    over the snapshot budget (N); it must re-anchor to
    `count(non-expired)+1`.

    Scenario: `all_available` task with a snapshot of N=3 workers
    (`attempt_budget=3`). K=1 execution ends `expired` (worker crash:
    it returns its slot without re-consumption, because in all_available the budget does not
    gate the claim). Another execution is `completed` (terminal non-expired).
    The third worker claims its pre-assigned execution and reports `exit_code=75`
    → triggers the sticky auto-retry, which re-routes the task to count-mode.

    After the retry there are 2 non-expired executions (the completed one + the
    trigger) and 1 expired. The budget MUST end at `2+1 = 3`, NOT at the
    `N+1 = 4` of the blind bump: re-anchoring opens EXACTLY one re-claim no matter
    how many expired, avoiding the `1+K` phantom slots.
    """
    w1, k1 = await _register(authed_admin_client, "wExpired")
    w2, k2 = await _register(authed_admin_client, "wDone")
    w3, k3 = await _register(authed_admin_client, "wRetry")

    # all_available snapshot over the 3 online workers + sticky policy.
    task_id = await _create_task_with_policy(
        authed_admin_client,
        target="all-available",
        auto_retry={"max_attempts": 3, "backoff_seconds": 0},
    )

    # Verify the precondition: snapshot of N=3 (target_count == attempt_budget == 3).
    pre = await _row(
        migrated_db_url,
        "SELECT target_mode, target_count, attempt_budget FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert pre is not None
    assert pre["target_mode"] == "all_available"
    assert pre["target_count"] == 3
    assert pre["attempt_budget"] == 3

    # K=1: worker w1 crashed → its snapshot execution became `expired`
    # (it returned its budget slot without re-consumption).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'expired', finished_at = now() "
        "WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w1},
    )
    # w2 finished normally → `completed` (terminal non-expired, consumes a slot).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'completed', started_at = now(), "
        "finished_at = now(), exit_code = 0 WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w2},
    )

    # w3 claims its pre-assigned execution (assigned → claimed with lease) and
    # completes it with exit_code=75 → triggers the sticky auto-retry.
    token = await _claim_lease(authed_admin_client, w3, k3)
    code = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=k3,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    assert code == 200

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count, attempt_budget, "
        "target_worker_pool, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    # Sticky conversion applied: re-routed to count-mode, pool=[w3].
    assert row["status"] == "queued"
    assert row["target_mode"] == "count"
    assert row["target_worker_pool"] == [w3]
    assert row["auto_retry_attempts_used"] == 1

    # Actual count of non-expired executions after the trigger closes.
    inflight = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM task_executions WHERE task_id = :tid AND status <> 'expired'",
        {"tid": task_id},
    )
    assert inflight is not None
    assert inflight["n"] == 2  # w2 completed + w3 (trigger) terminal

    # Budget re-anchored to count(non-expired)+1 = 3, NOT to the blind
    # bump N+1 = 4 (which would leave 1+K=2 phantom slots).
    assert row["attempt_budget"] == inflight["n"] + 1
    assert row["attempt_budget"] == 3


# --- 15b. without expired the resync matches the bump -----------------------


@pytest.mark.asyncio
async def test_all_available_sticky_retry_with_zero_expired_matches_bump(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """When there are NO `expired` executions (K=0), the resync to
    `count(non-expired)+1` must match EXACTLY what the blind
    +1 bump would give; there is no regression when nothing expires.

    Scenario: `all_available` task with a snapshot of N=3 workers
    (`attempt_budget=3`). NO execution expires: w1 and w2 end
    `completed` (terminal non-expired) and w3 claims its pre-assigned execution and
    reports `exit_code=75` → triggers the sticky auto-retry.

    After the retry there are 3 non-expired executions (the 2 completed + the
    trigger) and 0 expired. The budget MUST end at `3+1 = 4`, identical to
    `count(non-expired)+1`: with K=0 the re-anchor is the bump.
    """
    w1, k1 = await _register(authed_admin_client, "wDone1")
    w2, k2 = await _register(authed_admin_client, "wDone2")
    w3, k3 = await _register(authed_admin_client, "wRetry")

    # all_available snapshot over the 3 online workers + sticky policy.
    task_id = await _create_task_with_policy(
        authed_admin_client,
        target="all-available",
        auto_retry={"max_attempts": 3, "backoff_seconds": 0},
    )

    # Verify the precondition: snapshot of N=3 (target_count == attempt_budget == 3).
    pre = await _row(
        migrated_db_url,
        "SELECT target_mode, target_count, attempt_budget FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert pre is not None
    assert pre["target_mode"] == "all_available"
    assert pre["target_count"] == 3
    assert pre["attempt_budget"] == 3

    # K=0: w1 and w2 finish normally → `completed` (terminal non-expired).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'completed', started_at = now(), "
        "finished_at = now(), exit_code = 0 WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w1},
    )
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'completed', started_at = now(), "
        "finished_at = now(), exit_code = 0 WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w2},
    )

    # w3 claims its pre-assigned execution (assigned → claimed with lease) and
    # completes it with exit_code=75 → triggers the sticky auto-retry.
    token = await _claim_lease(authed_admin_client, w3, k3)
    code = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=k3,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    assert code == 200

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count, attempt_budget, "
        "target_worker_pool, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    # Sticky conversion applied: re-routed to count-mode, pool=[w3].
    assert row["status"] == "queued"
    assert row["target_mode"] == "count"
    assert row["target_worker_pool"] == [w3]
    assert row["auto_retry_attempts_used"] == 1

    # Actual count of non-expired executions after the trigger closes.
    inflight = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM task_executions WHERE task_id = :tid AND status <> 'expired'",
        {"tid": task_id},
    )
    assert inflight is not None
    assert inflight["n"] == 3  # w1 completed + w2 completed + w3 (trigger) terminal

    # K=0: the resync count(non-expired)+1 = 4 matches the bump N+1 = 4.
    assert row["attempt_budget"] == inflight["n"] + 1
    assert row["attempt_budget"] == 4


# --- 15c. all_available without sticky does not touch attempt_budget ------------------


@pytest.mark.asyncio
async def test_all_available_non_sticky_retry_leaves_budget_untouched(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The `all_available` branch WITHOUT sticky must NOT touch
    `attempt_budget` nor convert to count-mode.

    In `all_available` the budget does not govern the claim and we cannot
    re-snapshot from the complete (same limit as the manual retry without
    `force_same_worker`); the task stays in `all_available`. The retry
    increments the attempts counter but leaves `target_mode`,
    `target_worker_pool` and `attempt_budget` intact.

    Scenario: `all_available` task with a snapshot of N=3 (`attempt_budget=3`)
    and policy `sticky=False`. w3 claims its pre-assigned execution and reports
    `exit_code=75` → triggers the NON-sticky auto-retry.
    """
    w1, k1 = await _register(authed_admin_client, "wDone1")
    w2, k2 = await _register(authed_admin_client, "wDone2")
    w3, k3 = await _register(authed_admin_client, "wRetry")

    # all_available snapshot over the 3 online workers + NON-sticky policy.
    task_id = await _create_task_with_policy(
        authed_admin_client,
        target="all-available",
        auto_retry={"max_attempts": 3, "sticky": False, "backoff_seconds": 0},
    )

    # Verify the precondition: snapshot of N=3 (target_count == attempt_budget == 3).
    pre = await _row(
        migrated_db_url,
        "SELECT target_mode, target_count, attempt_budget FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert pre is not None
    assert pre["target_mode"] == "all_available"
    assert pre["target_count"] == 3
    assert pre["attempt_budget"] == 3

    # w1 and w2 finish normally → `completed` (terminal non-expired).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'completed', started_at = now(), "
        "finished_at = now(), exit_code = 0 WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w1},
    )
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'completed', started_at = now(), "
        "finished_at = now(), exit_code = 0 WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w2},
    )

    # w3 claims its pre-assigned execution (assigned → claimed with lease) and
    # completes it with exit_code=75 → triggers the NON-sticky auto-retry.
    token = await _claim_lease(authed_admin_client, w3, k3)
    code = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=k3,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    assert code == 200

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count, attempt_budget, "
        "target_worker_pool, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    # NON-sticky: the task is NOT converted to count-mode; it stays all_available
    # and the pool stays NULL.
    assert row["status"] == "queued"
    assert row["target_mode"] == "all_available"
    assert row["target_worker_pool"] is None
    assert row["auto_retry_attempts_used"] == 1
    # all_available without sticky does not touch the budget: the snapshot (N=3) is intact.
    assert row["target_count"] == 3
    assert row["attempt_budget"] == 3


# --- 15d. the resync counts ACTIVE (running) in-flight executions -----------


@pytest.mark.asyncio
async def test_all_available_sticky_retry_counts_running_executions(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The resync re-anchors to `count(non-expired)`, which includes the
    ACTIVE (`running`) executions of the original snapshot, not just the
    terminal ones. This is the realistic `all_available` case: when the trigger
    fires, other workers of the snapshot may still be executing in parallel.

    Scenario: `all_available` task with a snapshot of N=4 workers
    (`attempt_budget=4`). At the moment of the trigger:
      - w1 crashed → `expired` (returned its slot without re-consumption) → does NOT count.
      - w2 is still executing → `running` (active, non-expired) → counts.
      - w3 finished → `completed` (terminal non-expired) → counts.
      - w4 claims its pre-assigned execution and reports `exit_code=75` → triggers
        the sticky auto-retry; its terminal execution (non-expired) → counts.

    After the retry there are 3 non-expired executions (w2 running + w3 completed +
    w4 trigger) and 1 expired. The budget MUST end at `3+1 = 4`
    (`count(non-expired)+1`), NOT at the blind bump `N+1 = 5`: the `running` one of
    w2 consumes budget just like a terminal one, and the `expired` one of w1 does not.
    """
    w1, k1 = await _register(authed_admin_client, "wExpired")
    w2, k2 = await _register(authed_admin_client, "wRunning")
    w3, k3 = await _register(authed_admin_client, "wDone")
    w4, k4 = await _register(authed_admin_client, "wRetry")

    # all_available snapshot over the 4 online workers + sticky policy.
    task_id = await _create_task_with_policy(
        authed_admin_client,
        target="all-available",
        auto_retry={"max_attempts": 3, "backoff_seconds": 0},
    )

    # Verify the precondition: snapshot of N=4 (target_count == attempt_budget == 4).
    pre = await _row(
        migrated_db_url,
        "SELECT target_mode, target_count, attempt_budget FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert pre is not None
    assert pre["target_mode"] == "all_available"
    assert pre["target_count"] == 4
    assert pre["attempt_budget"] == 4

    # w1 crashed → its snapshot execution became `expired` (does not count).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'expired', finished_at = now() "
        "WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w1},
    )
    # w2 is still executing at the time of the trigger → `running` (active, non-expired).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'running', started_at = now() "
        "WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w2},
    )
    # w3 finished normally → `completed` (terminal non-expired).
    await _exec_sql(
        migrated_db_url,
        "UPDATE task_executions SET status = 'completed', started_at = now(), "
        "finished_at = now(), exit_code = 0 WHERE task_id = :tid AND worker_id = :wid",
        {"tid": task_id, "wid": w3},
    )

    # w4 claims its pre-assigned execution (assigned → claimed with lease) and
    # completes it with exit_code=75 → triggers the sticky auto-retry.
    token = await _claim_lease(authed_admin_client, w4, k4)
    code = await _complete(
        authed_admin_client,
        task_id=task_id,
        worker_key=k4,
        lease_token=token,
        status="completed",
        exit_code=75,
    )
    assert code == 200

    row = await _row(
        migrated_db_url,
        "SELECT status, target_mode, target_count, attempt_budget, "
        "target_worker_pool, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    # Sticky conversion applied: re-routed to count-mode, pool=[w4].
    assert row["status"] == "queued"
    assert row["target_mode"] == "count"
    assert row["target_worker_pool"] == [w4]
    assert row["auto_retry_attempts_used"] == 1

    # Actual count of non-expired executions: w2 running + w3 completed + w4 trigger.
    inflight = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM task_executions WHERE task_id = :tid AND status <> 'expired'",
        {"tid": task_id},
    )
    assert inflight is not None
    assert inflight["n"] == 3  # the `running` one of w2 counts as in-flight

    # Budget re-anchored to count(non-expired)+1 = 4, NOT to the blind bump
    # N+1 = 5. The `running` execution consumes budget; the `expired` one does not.
    assert row["attempt_budget"] == inflight["n"] + 1
    assert row["attempt_budget"] == 4


# --- 16. provenance marker of the sticky conversion -----------------------


@pytest.mark.asyncio
async def test_all_available_sticky_conversion_marks_converted_from(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The sticky auto-retry's `all_available`→count conversion
    records its provenance in `tasks.converted_from`. This is the marker that
    the count-mode close reads to count only the current epoch instead of the
    whole history."""
    w1, k1 = await _register(authed_admin_client, "wOne")

    task_id = await _create_task_with_policy(
        authed_admin_client,
        target="all-available",
        auto_retry={"max_attempts": 1, "backoff_seconds": 0},
    )
    pre = await _row(
        migrated_db_url,
        "SELECT target_mode, converted_from FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert pre == {"target_mode": "all_available", "converted_from": None}

    token = await _claim_lease(authed_admin_client, w1, k1)
    assert (
        await _complete(
            authed_admin_client,
            task_id=task_id,
            worker_key=k1,
            lease_token=token,
            status="failed",
            exit_code=75,
        )
        == 200
    )

    row = await _row(
        migrated_db_url,
        "SELECT target_mode, converted_from, target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    assert row["target_mode"] == "count"
    assert row["target_worker_pool"] == [w1]
    assert row["converted_from"] == "all_available"

    # Closing a conversion in epoch 0 does not change: the current epoch IS
    # the whole history, so `completed_epoch` and `completed` coincide. The
    # successful re-claim closes `completed` (target_count = 1 = snapshot
    # size).
    token = await _claim_lease(authed_admin_client, w1, k1)
    assert (
        await _complete(
            authed_admin_client,
            task_id=task_id,
            worker_key=k1,
            lease_token=token,
            status="completed",
            exit_code=0,
        )
        == 200
    )
    closed = await _row(
        migrated_db_url,
        "SELECT status::text AS status, outcome, converted_from FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert closed == {"status": "completed", "outcome": None, "converted_from": "all_available"}


@pytest.mark.asyncio
async def test_second_sticky_retry_preserves_converted_from(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The provenance marker survives subsequent sticky retries.
    The second auto-retry goes through the SAME retarget with the task
    already in count-mode, so it falls into the `ELSE` branch of the `CASE`: that
    branch must PRESERVE `converted_from`, not clear it. An `ELSE NULL` would leave the
    converted task counting full-history from the second failure onward, and the
    rest of the suite would not notice."""
    w1, k1 = await _register(authed_admin_client, "wOne")

    task_id = await _create_task_with_policy(
        authed_admin_client,
        target="all-available",
        auto_retry={"max_attempts": 2, "backoff_seconds": 0},
    )

    # Failure 1: all_available → count conversion, marker set.
    token = await _claim_lease(authed_admin_client, w1, k1)
    assert (
        await _complete(
            authed_admin_client,
            task_id=task_id,
            worker_key=k1,
            lease_token=token,
            status="failed",
            exit_code=75,
        )
        == 200
    )
    first = await _row(
        migrated_db_url,
        "SELECT target_mode, converted_from, auto_retry_attempts_used FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert first == {
        "target_mode": "count",
        "converted_from": "all_available",
        "auto_retry_attempts_used": 1,
    }

    # Failure 2: the retarget runs again, now with target_mode already 'count'.
    token = await _claim_lease(authed_admin_client, w1, k1)
    assert (
        await _complete(
            authed_admin_client,
            task_id=task_id,
            worker_key=k1,
            lease_token=token,
            status="failed",
            exit_code=75,
        )
        == 200
    )
    second = await _row(
        migrated_db_url,
        "SELECT status::text AS status, target_mode, converted_from, "
        "auto_retry_attempts_used, target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert second is not None
    assert second["status"] == "queued"
    assert second["auto_retry_attempts_used"] == 2
    assert second["target_mode"] == "count"
    assert second["target_worker_pool"] == [w1]
    assert second["converted_from"] == "all_available", (
        "the ELSE branch of the CASE must preserve the marker, not clear it",
        second,
    )


@pytest.mark.asyncio
async def test_count_native_sticky_retry_does_not_mark_converted_from(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The same sticky retarget on a task that was ALREADY
    count-mode must NOT mark provenance: there was no conversion, and marking it
    would scope the close of a native count by epoch (breaking the non-expired
    budget count and the count-mode close with mixed history).
    The UPDATE's `CASE` sees the row's OLD `target_mode`, which here is already
    'count'."""
    w1, k1 = await _register(authed_admin_client, "wOne")

    task_id = await _create_task_with_policy(
        authed_admin_client,
        target="1",
        auto_retry={"max_attempts": 1, "backoff_seconds": 0},
    )
    pre = await _row(
        migrated_db_url,
        "SELECT target_mode, converted_from FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert pre == {"target_mode": "count", "converted_from": None}

    token = await _claim_lease(authed_admin_client, w1, k1)
    assert (
        await _complete(
            authed_admin_client,
            task_id=task_id,
            worker_key=k1,
            lease_token=token,
            status="failed",
            exit_code=75,
        )
        == 200
    )

    row = await _row(
        migrated_db_url,
        "SELECT status::text AS status, target_mode, converted_from, "
        "target_worker_pool FROM tasks WHERE id=:id",
        {"id": task_id},
    )
    assert row is not None
    # The sticky retarget ran (pool pinned) but without a provenance marker.
    assert row["status"] == "queued"
    assert row["target_mode"] == "count"
    assert row["target_worker_pool"] == [w1]
    assert row["converted_from"] is None
