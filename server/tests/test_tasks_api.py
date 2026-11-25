"""Integration tests for `/v1/tasks/*` (heartbeat/output/complete).

There is no `POST /v1/tasks/claim` endpoint: the claim travels in
`POST /v1/workers/{id}/poll`. These tests use
`/poll` to obtain a lease and then exercise heartbeat/output/complete.

Tasks are inserted through direct SQL (the admin endpoint is not the subject of this
test). It covers the happy path and the main errors.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.housekeeping import _reclaim_expired_leases
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


async def _poll(client: AsyncClient, worker_id: str, key: str) -> dict:
    resp = await client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _insert_task(
    db_url: str,
    command: str = "echo hello",
    payload: dict | None = None,
    target_count: int = 1,
    priority: int = 0,
) -> int:
    """Insert a `queued`/`target_mode='count'` task and return its id.

    Universal signature: `signature` is NOT NULL, so we use a 64-byte placeholder
    (an invalid Ed25519 signature that is structurally correct). The `/poll` endpoint
    re-signs with the real UUID of the polling worker at claim time, so
    the effective signature delivered to the client is valid. The claim
    happy-path tests do not verify the placeholder signature.
    """
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, command, payload,
                                           target_mode, target_count,
                                           attempt_budget,
                                           priority, status, run_at,
                                           signed, signature)
                        VALUES (:workspace_id, :cmd, CAST(:payload AS jsonb),
                                'count', :tc, :tc, :pri,
                                'queued', now(),
                                true, decode(repeat('00', 64), 'hex'))
                        RETURNING id
                        """
                    ),
                    {
                        "cmd": command,
                        "payload": json.dumps(payload if payload is not None else {}),
                        "tc": target_count,
                        "pri": priority,
                        "workspace_id": DEFAULT_WORKSPACE_ID,
                    },
                )
            ).first()
        return int(row[0])
    finally:
        await engine.dispose()


async def _set_lease_expired(db_url: str, task_id: int, worker_id: str) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE task_executions
                       SET lease_expires_at = now() - interval '10 seconds'
                     WHERE task_id = :tid AND worker_id = :wid
                    """
                ),
                {"tid": task_id, "wid": worker_id},
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
            rs = (await conn.execute(text(sql), params)).mappings().all()
            return [dict(r) for r in rs]
    finally:
        await engine.dispose()


# --- Claim via /poll -------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_returns_null_task_when_no_tasks(app_client: AsyncClient) -> None:
    worker_id, key = await _register(app_client)
    body = await _poll(app_client, worker_id, key)
    assert body["task"] is None


@pytest.mark.asyncio
async def test_poll_returns_task_and_promotes_to_dispatching(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url, command="ls /tmp")

    body = await _poll(app_client, worker_id, key)
    task = body["task"]
    assert task is not None
    assert task["task_id"] == task_id
    assert task["command"] == "ls /tmp"
    assert "lease_token" in task and "lease_expires_at" in task

    # Task went from 'queued' -> 'dispatching'.
    row = await _row(migrated_db_url, "SELECT status FROM tasks WHERE id=:tid", {"tid": task_id})
    assert row is not None and row["status"] == "dispatching"


@pytest.mark.asyncio
async def test_poll_wrong_key_is_401(app_client: AsyncClient) -> None:
    _, key_a = await _register(app_client, hostname="a")
    worker_b_id, _ = await _register(app_client, hostname="b")
    resp = await app_client.post(
        f"/v1/workers/{worker_b_id}/poll",
        headers={"X-API-Key": key_a},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_poll_target_count_limits_concurrency(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """target=1: 2 workers compete, only one wins."""
    w_a, k_a = await _register(app_client, hostname="a")
    w_b, k_b = await _register(app_client, hostname="b")
    await _insert_task(migrated_db_url, target_count=1)

    r_a, r_b = await asyncio.gather(
        app_client.post(
            f"/v1/workers/{w_a}/poll",
            headers={"X-API-Key": k_a},
            json={"status": "idle", "lease_seconds": 30},
        ),
        app_client.post(
            f"/v1/workers/{w_b}/poll",
            headers={"X-API-Key": k_b},
            json={"status": "idle", "lease_seconds": 30},
        ),
    )
    assert r_a.status_code == 200 and r_b.status_code == 200
    bodies = [r_a.json(), r_b.json()]
    got = sum(1 for b in bodies if b["task"] is not None)
    assert got == 1, [b.get("task") for b in bodies]


@pytest.mark.asyncio
async def test_expired_lease_reclaimed_by_background_loop_not_poll(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Lease reclaim lives in the background job, NOT in the poll.

    With a just-expired lease (execution still 'claimed'), the claim's
    `taken` still counts it as active, so another worker's poll
    does NOT deliver the task until `housekeeping_loop` expires the
    execution. We verify both sides:
      1. B's poll right after the lease expires -> no task
         (the poll no longer does housekeeping).
      2. after the background reclaim runs (`_reclaim_expired_leases`), the
         poll of B does claim the task.
    """
    w_a, k_a = await _register(app_client, hostname="a")
    w_b, k_b = await _register(app_client, hostname="b")
    task_id = await _insert_task(migrated_db_url, target_count=1)

    first = await _poll(app_client, w_a, k_a)
    assert first["task"] is not None

    await _set_lease_expired(migrated_db_url, task_id, w_a)

    # 1. The poll no longer reclaims: B does not receive the task (slot still held by
    #    A's execution, whose lease lapsed but which has not been expired).
    second = await _poll(app_client, w_b, k_b)
    assert second["task"] is None, second

    # 2. The background job expires A's lapsed execution.
    engine = create_async_engine(migrated_db_url)
    try:
        reclaimed = await _reclaim_expired_leases(engine)
    finally:
        await engine.dispose()
    assert reclaimed >= 1

    # Now B can claim the same task.
    third = await _poll(app_client, w_b, k_b)
    assert third["task"] is not None
    assert third["task"]["task_id"] == task_id


# --- Heartbeat (REMOVED) ----------------------------------------
#
# The `POST /v1/tasks/{id}/heartbeat` endpoint was removed: `/poll` covers it.
# It was the only producer of the `running` state; since then an execution goes
# `assigned->claimed->terminal`. The heartbeat tests (ok/expired/cancelled) were
# removed with the endpoint. The cancel signal is now tested via `/poll`
# (see test_poll_api.py) and lease ownership via output/complete (below).


# --- Output ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_inserts_events(app_client: AsyncClient, migrated_db_url: str) -> None:
    """/output inserts events into output_events (it does not concatenate into
    stdout/stderr). Preserves seq/ts/stream/text per event."""
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url)
    body = await _poll(app_client, worker_id, key)
    token = body["task"]["lease_token"]

    batch = [
        {"seq": 1, "ts": "2026-06-03T00:00:00+00:00", "stream": "stdout", "text": "hello "},
        {"seq": 2, "ts": "2026-06-03T00:00:01+00:00", "stream": "stderr", "text": "warn\n"},
        {"seq": 3, "ts": "2026-06-03T00:00:02+00:00", "stream": "stdout", "text": "world"},
    ]
    r = await app_client.post(
        f"/v1/tasks/{task_id}/output",
        headers={"X-API-Key": key},
        json={"lease_token": token, "events": batch},
    )
    assert r.status_code == 202

    rows = await _rows(
        migrated_db_url,
        "SELECT oe.seq, oe.stream, oe.text "
        "FROM output_events oe "
        "JOIN task_executions e ON e.id = oe.execution_id "
        "WHERE e.task_id=:tid AND e.worker_id=:wid "
        "ORDER BY oe.seq",
        {"tid": task_id, "wid": worker_id},
    )
    assert [(row["seq"], row["stream"], row["text"]) for row in rows] == [
        (1, "stdout", "hello "),
        (2, "stderr", "warn\n"),
        (3, "stdout", "world"),
    ]


@pytest.mark.asyncio
async def test_output_event_text_over_cap_rejected(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Per-event cap: a legitimate worker chunk never exceeds
    64 KiB (dispatcher read); the schema rejects with 422 any text
    over `OUTPUT_EVENT_TEXT_MAX_CHARS` and accepts one exactly at the cap."""
    from orcaster_server.schemas.tasks import OUTPUT_EVENT_TEXT_MAX_CHARS

    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url)
    body = await _poll(app_client, worker_id, key)
    token = body["task"]["lease_token"]

    def _batch(text_len: int) -> dict:
        return {
            "lease_token": token,
            "events": [
                {
                    "seq": 1,
                    "ts": "2026-06-04T00:00:00+00:00",
                    "stream": "stdout",
                    "text": "x" * text_len,
                }
            ],
        }

    r = await app_client.post(
        f"/v1/tasks/{task_id}/output",
        headers={"X-API-Key": key},
        json=_batch(OUTPUT_EVENT_TEXT_MAX_CHARS + 1),
    )
    assert r.status_code == 422

    r = await app_client.post(
        f"/v1/tasks/{task_id}/output",
        headers={"X-API-Key": key},
        json=_batch(OUTPUT_EVENT_TEXT_MAX_CHARS),
    )
    assert r.status_code == 202

    # The rejected batch left no trace; the valid one inserted its single event.
    row = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM output_events oe "
        "JOIN task_executions e ON e.id = oe.execution_id "
        "WHERE e.task_id=:tid AND e.worker_id=:wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert row is not None and row["n"] == 1


@pytest.mark.asyncio
async def test_output_event_insert_is_idempotent(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Resending the same batch (same seq) does not duplicate rows: UNIQUE(execution_id, seq)
    + ON CONFLICT DO NOTHING. Covers worker network retries."""
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url)
    body = await _poll(app_client, worker_id, key)
    token = body["task"]["lease_token"]

    batch = [
        {"seq": 1, "ts": "2026-06-03T00:00:00+00:00", "stream": "stdout", "text": "a"},
        {"seq": 2, "ts": "2026-06-03T00:00:00+00:00", "stream": "stdout", "text": "b"},
    ]
    payload = {"lease_token": token, "events": batch}
    for _ in range(3):  # mismo batch enviado 3 veces
        r = await app_client.post(
            f"/v1/tasks/{task_id}/output", headers={"X-API-Key": key}, json=payload
        )
        assert r.status_code == 202

    count_row = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM output_events oe "
        "JOIN task_executions e ON e.id = oe.execution_id "
        "WHERE e.task_id=:tid AND e.worker_id=:wid",
        {"tid": task_id, "wid": worker_id},
    )
    assert count_row is not None and count_row["n"] == 2


@pytest.mark.asyncio
async def test_output_bad_lease_returns_409(app_client: AsyncClient, migrated_db_url: str) -> None:
    """Invalid lease -> 409 (reliable streaming): the worker needs to tell apart
    "batch persisted" (202) from "attempt is no longer mine" (409) so it can abort the
    backlog instead of retrying forever. It used to be a best-effort 202."""
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url)
    await _poll(app_client, worker_id, key)

    r = await app_client.post(
        f"/v1/tasks/{task_id}/output",
        headers={"X-API-Key": key},
        json={
            "lease_token": "00000000-0000-0000-0000-000000000000",
            "events": [
                {"seq": 1, "ts": "2026-06-03T00:00:00+00:00", "stream": "stdout", "text": "x"},
            ],
        },
    )
    assert r.status_code == 409
    count_row = await _row(
        migrated_db_url,
        "SELECT count(*) AS n FROM output_events oe "
        "JOIN task_executions e ON e.id = oe.execution_id "
        "WHERE e.task_id=:tid",
        {"tid": task_id},
    )
    assert count_row is not None and count_row["n"] == 0


@pytest.mark.asyncio
async def test_output_after_complete_returns_409(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """After complete the execution is terminal: a late /output gets 409.
    It is the abort signal for the worker drain when the lease expired and the
    execution was closed/reassigned."""
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url)
    body = await _poll(app_client, worker_id, key)
    token = body["task"]["lease_token"]

    r = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": token, "exit_code": 0, "status": "completed"},
    )
    assert r.status_code == 200

    r = await app_client.post(
        f"/v1/tasks/{task_id}/output",
        headers={"X-API-Key": key},
        json={
            "lease_token": token,
            "events": [
                {"seq": 1, "ts": "2026-06-03T00:00:00+00:00", "stream": "stdout", "text": "late"},
            ],
        },
    )
    assert r.status_code == 409


# --- Complete --------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_closes_execution_and_task(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url, target_count=1)
    body = await _poll(app_client, worker_id, key)
    token = body["task"]["lease_token"]

    # `stdout` is no longer a schema field: it travels as an extra and Pydantic
    # ignores it. Sending it on purpose keeps wire compat with old workers.
    done = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={
            "lease_token": token,
            "exit_code": 0,
            "stdout": "ignored",
            "status": "completed",
        },
    )
    assert done.status_code == 200, done.text

    exec_row = await _row(
        migrated_db_url,
        "SELECT status, exit_code FROM task_executions WHERE task_id=:tid",
        {"tid": task_id},
    )
    assert exec_row == {"status": "completed", "exit_code": 0}

    task_row = await _row(
        migrated_db_url,
        "SELECT status, completed_at FROM tasks WHERE id=:tid",
        {"tid": task_id},
    )
    assert task_row is not None
    assert task_row["status"] == "completed"
    assert task_row["completed_at"] is not None


@pytest.mark.asyncio
async def test_complete_failure_then_task_failed_with_no_completed(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url, target_count=1)
    body = await _poll(app_client, worker_id, key)
    token = body["task"]["lease_token"]
    r = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": token, "exit_code": 1, "status": "failed"},
    )
    assert r.status_code == 200
    task_row = await _row(
        migrated_db_url, "SELECT status FROM tasks WHERE id=:tid", {"tid": task_id}
    )
    assert task_row is not None and task_row["status"] == "failed"


@pytest.mark.asyncio
async def test_complete_cancelled_persists_cancelled_reason(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """A worker may report `status='cancelled'` with
    `cancelled_reason` (e.g. outer_timeout). The server accepts it, closes the
    execution as `cancelled` and persists tasks.cancelled_reason.
    """
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url, target_count=1)
    body = await _poll(app_client, worker_id, key)
    token = body["task"]["lease_token"]

    done = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={
            "lease_token": token,
            "exit_code": None,
            "status": "cancelled",
            "cancelled_reason": "outer_timeout",
        },
    )
    assert done.status_code == 200, done.text

    exec_row = await _row(
        migrated_db_url,
        "SELECT status FROM task_executions WHERE task_id=:tid",
        {"tid": task_id},
    )
    assert exec_row is not None and exec_row["status"] == "cancelled"

    task_row = await _row(
        migrated_db_url,
        "SELECT status, cancelled_reason, completed_at FROM tasks WHERE id=:tid",
        {"tid": task_id},
    )
    assert task_row is not None
    assert task_row["status"] == "cancelled"
    assert task_row["cancelled_reason"] == "outer_timeout"
    assert task_row["completed_at"] is not None


@pytest.mark.asyncio
async def test_complete_409_on_stale_lease(app_client: AsyncClient, migrated_db_url: str) -> None:
    worker_id, key = await _register(app_client)
    task_id = await _insert_task(migrated_db_url)
    await _poll(app_client, worker_id, key)
    r = await app_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={
            "lease_token": "00000000-0000-0000-0000-000000000000",
            "exit_code": 0,
            "status": "completed",
        },
    )
    assert r.status_code == 409
