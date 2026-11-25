"""End-to-end tests of the key flows (unified endpoint).

All workers poll through `POST /v1/workers/{id}/poll`. Heartbeat, claim and
command delivery travel in the same round-trip. Push commands are regular tasks
with `delivery_mode='push'`, signed with Ed25519.

Each test exercises the full chain over HTTP using `authed_admin_client` (httpx
AsyncClient + ASGITransport mounted on the real FastAPI app).

Raw SQL is not used for flow *actions* (only for reading/verification or for
forcing time to advance, e.g. expiring a lease).
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server import signing
from orcaster_server.config import settings
from orcaster_server.housekeeping import _reclaim_expired_leases
from orcaster_server.signing import verify_task_signature
from tests.conftest import example_payload, mint_token_via_app

REGISTER_BODY = {
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
        json={**REGISTER_BODY, "hostname": hostname, "enrollment_token": token},
    )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    return j["worker_id"], j["api_key"]


async def _poll_idle(client: AsyncClient, worker_id: str, key: str) -> dict:
    """Convenience: poll with status=idle and return the body."""
    resp = await client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={"status": "idle", "lease_seconds": 30},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _set_lease_expired(db_url: str, task_id: int, worker_id: str) -> None:
    """Force time to advance by moving back the `lease_expires_at` of a
    given execution -- equivalent to "the worker lost the network"."""
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


async def _read_execution(db_url: str, task_id: int) -> dict:
    """Read the execution row of a task (status, lease_expires_at, progress)."""
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT status, lease_expires_at, progress "
                            "FROM task_executions WHERE task_id=:tid"
                        ),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert row is not None
    return dict(row)


async def _set_worker_offline(db_url: str, worker_id: str) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE workers SET status = 'offline' WHERE id = :id"),
                {"id": worker_id},
            )
    finally:
        await engine.dispose()


# --- Worker registration ---------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_71_register_and_poll(authed_admin_client: AsyncClient) -> None:
    """Registration flow: the worker registers and a poll with the api_key returns 200
    (no task, no cancel_signals)."""
    worker_id, api_key = await _register(authed_admin_client, hostname="alpha")
    body = await _poll_idle(authed_admin_client, worker_id, api_key)
    assert body["task"] is None
    assert body["cancel_signals"] == []
    assert "next_poll_after_seconds" in body


# --- all-available enqueueing -----------------------------------------------


@pytest.mark.asyncio
async def test_e2e_72_all_available_fanout(authed_admin_client: AsyncClient) -> None:
    """Enqueue flow: 3 workers online -> POST /admin/tasks target=all-available
    -> 3 executions assigned. Each worker receives the task when it polls."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    w_c, k_c = await _register(authed_admin_client, "c")

    create = await authed_admin_client.post(
        "/admin/tasks",
        json={"target": "all-available"},
    )
    assert create.status_code == 200, create.text
    j = create.json()
    assert j["target_mode"] == "all_available"
    assert j["target_count"] == 3
    task_id = j["task_id"]

    for wid, key in ((w_a, k_a), (w_b, k_b), (w_c, k_c)):
        body = await _poll_idle(authed_admin_client, wid, key)
        assert body["task"] is not None, (wid, body)
        assert body["task"]["task_id"] == task_id


async def _read_task_status_and_exec_count(db_url: str, task_id: int) -> tuple[str, int]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            status = (
                await conn.execute(
                    text("SELECT status FROM tasks WHERE id=:tid"),
                    {"tid": task_id},
                )
            ).scalar_one()
            n_exec = (
                await conn.execute(
                    text("SELECT count(*) FROM task_executions WHERE task_id=:tid"),
                    {"tid": task_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    return status, n_exec


async def _read_executions(db_url: str, task_id: int) -> list[dict]:
    """Read (worker_id, status, snapshot_epoch) of ALL executions of the
    task, ordered by epoch and worker -- for propagation asserts."""
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT worker_id::text AS worker_id, status, snapshot_epoch "
                            "FROM task_executions WHERE task_id = :tid "
                            "ORDER BY snapshot_epoch, worker_id"
                        ),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()
    return [dict(r) for r in rows]


async def _force_task_terminal(db_url: str, task_id: int) -> None:
    """Broken-state fixture (defense): force the task to terminal WITHOUT
    propagating to its executions -- simulates a latent path that left them active
    under a terminal task (which produced the 500 of the issue)."""
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET status = 'failed', completed_at = now() WHERE id = :tid"),
                {"tid": task_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_e2e_72b_all_available_partial_success_closes_failed(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The success of `all_available` requires the WHOLE snapshot to
    complete (`completed = total`), analogous to the exact success of count-mode
    and consistent with the sticky conversion of auto-retry.

    Snapshot of 2 workers: A completes (exit 0), B fails (exit 1). Previously the
    close used `completed >= 1` and this mix closed `completed`; with the new
    semantics it closes `failed`.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    token_a = body_a["task"]["lease_token"]
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None
    token_b = body_b["task"]["lease_token"]

    done_a = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={"lease_token": token_a, "exit_code": 0, "status": "completed"},
    )
    assert done_a.status_code == 200, done_a.text

    # While a snapshot member is still running, the task stays open
    # (the close waits for `terminal = total`, no fail-fast).
    status, _ = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "dispatching", status

    fail_b = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_b},
        json={"lease_token": token_b, "exit_code": 1, "status": "failed"},
    )
    assert fail_b.status_code == 200, fail_b.text

    status, n_exec = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", (
        "partial success over the snapshot must close 'failed'",
        status,
    )
    assert n_exec == 2, f"the snapshot is fixed: expected 2 executions, found {n_exec}"


@pytest.mark.asyncio
async def test_e2e_72c_all_available_all_completed_closes_completed(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Success branch: snapshot of 2 workers where BOTH complete
    -> the task closes `completed`."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    for wid, key in ((w_a, k_a), (w_b, k_b)):
        body = await _poll_idle(authed_admin_client, wid, key)
        assert body["task"] is not None
        done = await authed_admin_client.post(
            f"/v1/tasks/{task_id}/complete",
            headers={"X-API-Key": key},
            json={
                "lease_token": body["task"]["lease_token"],
                "exit_code": 0,
                "status": "completed",
            },
        )
        assert done.status_code == 200, done.text

    status, n_exec = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "completed", status
    assert n_exec == 2, f"the snapshot is fixed: expected 2 executions, found {n_exec}"


@pytest.mark.asyncio
async def test_e2e_72d_all_available_retry_after_partial_failure_completes(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Snapshot recovery path per epoch -- a partial failure
    closes `failed`, the admin retry re-snapshots and a second 100% successful
    attempt closes `completed`.

    Regression: the re-snapshot keeps the terminal executions of the previous
    epoch (it inserts `assigned` with `attempt_index+1`), so the worker has 2 rows
    for the task. The UPDATE of `_SQL_POLL_CLAIM_PREASSIGNED` must re-filter
    `status='assigned'`; without that it flipped BOTH rows to 'claimed' and the
    poll returned 500 because of the partial unique `task_executions_active_uk`.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # Epoch 1: A completes, B fails -> closes `failed`.
    for (wid, key), (code, st) in (
        ((w_a, k_a), (0, "completed")),
        ((w_b, k_b), (1, "failed")),
    ):
        body = await _poll_idle(authed_admin_client, wid, key)
        assert body["task"] is not None
        done = await authed_admin_client.post(
            f"/v1/tasks/{task_id}/complete",
            headers={"X-API-Key": key},
            json={
                "lease_token": body["task"]["lease_token"],
                "exit_code": code,
                "status": st,
            },
        )
        assert done.status_code == 200, done.text
    status, _ = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", status

    # Admin retry -> re-snapshot (A and B are still online).
    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text

    # Epoch 2: BOTH complete. The poll must NOT return 500.
    for wid, key in ((w_a, k_a), (w_b, k_b)):
        body = await _poll_idle(authed_admin_client, wid, key)
        assert body["task"] is not None, (wid, body)
        done = await authed_admin_client.post(
            f"/v1/tasks/{task_id}/complete",
            headers={"X-API-Key": key},
            json={
                "lease_token": body["task"]["lease_token"],
                "exit_code": 0,
                "status": "completed",
            },
        )
        assert done.status_code == 200, done.text

    status, n_exec = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "completed", (
        "100% successful retry must close 'completed'",
        status,
    )
    assert n_exec == 4, f"re-snapshot keeps the history: 2 terminal + 2 new, found {n_exec}"


@pytest.mark.asyncio
async def test_e2e_72e_all_available_retry_excludes_offline_worker(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Current epoch: the retry redefines the fleet as the workers
    available NOW. If the worker that failed is offline at retry time, it is left
    out of the re-snapshot and its failure from the previous epoch does NOT block
    the success of the current snapshot."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    # Epoch 1: A completes, B fails -> failed.
    for (wid, key), (code, st) in (
        ((w_a, k_a), (0, "completed")),
        ((w_b, k_b), (1, "failed")),
    ):
        body = await _poll_idle(authed_admin_client, wid, key)
        assert body["task"] is not None
        done = await authed_admin_client.post(
            f"/v1/tasks/{task_id}/complete",
            headers={"X-API-Key": key},
            json={
                "lease_token": body["task"]["lease_token"],
                "exit_code": code,
                "status": st,
            },
        )
        assert done.status_code == 200, done.text
    status, _ = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", status

    # B goes offline; the retry re-snapshots with A only.
    await _set_worker_offline(migrated_db_url, w_b)
    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text

    # Epoch 2: A (only one in the current snapshot) completes -> completed.
    body = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body["task"] is not None, body
    done = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={
            "lease_token": body["task"]["lease_token"],
            "exit_code": 0,
            "status": "completed",
        },
    )
    assert done.status_code == 200, done.text

    status, n_exec = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "completed", (
        "B's failure in the previous epoch is not part of the current snapshot",
        status,
    )
    assert n_exec == 3, f"2 from epoch 1 + 1 from epoch 2, found {n_exec}"


@pytest.mark.asyncio
async def test_e2e_72f_all_available_old_successes_do_not_mask_new_failure(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Current epoch, failure branch: the `completed` rows of previous
    epochs do NOT count toward the success of the current snapshot. Epoch 1:
    A✓ B✓ C✗ -> failed. Retry (all online) -> epoch 2: A✓ B✗ C✓ -> failed
    (the history has 4 completed >= target 3, but the current epoch has
    2 < 3)."""
    workers = [await _register(authed_admin_client, h) for h in ("a", "b", "c")]
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    async def _epoch(outcomes: list[tuple[int, str]]) -> None:
        for (wid, key), (code, st) in zip(workers, outcomes, strict=True):
            body = await _poll_idle(authed_admin_client, wid, key)
            assert body["task"] is not None, (wid, body)
            done = await authed_admin_client.post(
                f"/v1/tasks/{task_id}/complete",
                headers={"X-API-Key": key},
                json={
                    "lease_token": body["task"]["lease_token"],
                    "exit_code": code,
                    "status": st,
                },
            )
            assert done.status_code == 200, done.text

    await _epoch([(0, "completed"), (0, "completed"), (1, "failed")])
    status, _ = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", status

    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text

    await _epoch([(0, "completed"), (1, "failed"), (0, "completed")])
    status, n_exec = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", (
        "the completed rows of epoch 1 must not mask B's failure in epoch 2",
        status,
    )
    assert n_exec == 6, f"3 per epoch x 2 epochs, found {n_exec}"


async def _complete_via_poll(
    client: AsyncClient,
    *,
    task_id: int,
    worker_id: str,
    key: str,
    exit_code: int | None,
    status: str,
) -> None:
    """Poll + complete of a given worker on a given task.

    Helper for the sticky conversion scenarios: the same worker alternates
    between a pre-assigned execution (`all_available`) and a count-mode re-claim
    after the conversion, and in both cases the flow is identical.
    """
    body = await _poll_idle(client, worker_id, key)
    assert body["task"] is not None, (worker_id, body)
    assert body["task"]["task_id"] == task_id, (worker_id, body)
    done = await client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={
            "lease_token": body["task"]["lease_token"],
            "exit_code": exit_code,
            "status": status,
        },
    )
    assert done.status_code == 200, done.text


async def _read_task_row(db_url: str, task_id: int, columns: str) -> dict:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(f"SELECT {columns} FROM tasks WHERE id = :tid"),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert row is not None, f"task {task_id} does not exist"
    return dict(row)


async def _setup_converted_sticky_task(
    client: AsyncClient,
    db_url: str,
    *,
    w_a: str,
    k_a: str,
    w_b: str,
    k_b: str,
) -> int:
    """Steps 1-3 of the end-to-end sticky conversion scenario.

    1. `all_available` task with snapshot {A,B} and sticky policy: epoch 0 closes
       `failed` (A✓ B✗, success requires the whole snapshot).
    2. Admin retry -> epoch 1, re-snapshot {A,B}, `target_count` resync = 2.
    3. Epoch 1: A✓; B fails with exit 75 (retryable) -> the sticky auto-retry
       converts the task to count-mode with pool=[B].

    Returns the `task_id` of the already converted, `queued` task.
    """
    create = await client.post(
        "/admin/tasks",
        json={
            "target": "all-available",
            "auto_retry": {"max_attempts": 1, "backoff_seconds": 0},
        },
    )
    assert create.status_code == 200, create.text
    task_id = int(create.json()["task_id"])

    # Epoch 0: A✓ B✗ -> failed (incomplete snapshot).
    await _complete_via_poll(
        client, task_id=task_id, worker_id=w_a, key=k_a, exit_code=0, status="completed"
    )
    await _complete_via_poll(
        client, task_id=task_id, worker_id=w_b, key=k_b, exit_code=1, status="failed"
    )
    status, _ = await _read_task_status_and_exec_count(db_url, task_id)
    assert status == "failed", status

    # Admin retry: epoch bump + re-snapshot {A,B} + target_count resync.
    retry = await client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text
    row = await _read_task_row(db_url, task_id, "snapshot_epoch, target_mode, target_count")
    assert row == {"snapshot_epoch": 1, "target_mode": "all_available", "target_count": 2}, row

    # Epoch 1: A✓ (does not close: the snapshot still has B in flight).
    await _complete_via_poll(
        client, task_id=task_id, worker_id=w_a, key=k_a, exit_code=0, status="completed"
    )
    # B fails with exit 75 -> sticky auto-retry: conversion to count-mode.
    await _complete_via_poll(
        client, task_id=task_id, worker_id=w_b, key=k_b, exit_code=75, status="failed"
    )
    return task_id


@pytest.mark.asyncio
async def test_e2e_72l_sticky_conversion_keeps_epoch_scope_on_failure(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The sticky `all_available`->`count` conversion of auto-retry
    must not reopen the full-history window of the count-mode close.

    Step 4 of the scenario: B exhausts its budget by failing. The history has
    two `completed` (A✓ from epoch 0 and from epoch 1) >= `target_count`
    = 2, so the full-history count-mode close would close `completed` and
    mask B's persistent failure. With the provenance mark
    (`converted_from='all_available'`) the close counts the current epoch
    (1 < 2) and closes `failed`.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    task_id = await _setup_converted_sticky_task(
        authed_admin_client, migrated_db_url, w_a=w_a, k_a=k_a, w_b=w_b, k_b=k_b
    )

    row = await _read_task_row(
        migrated_db_url,
        task_id,
        "status::text AS status, target_mode, target_count, converted_from, "
        "snapshot_epoch, target_worker_pool",
    )
    assert row["status"] == "queued", row
    assert row["target_mode"] == "count", row
    assert row["target_count"] == 2, row
    assert row["snapshot_epoch"] == 1, row
    assert row["target_worker_pool"] == [w_b], row
    assert row["converted_from"] == "all_available", (
        "the conversion must record its provenance",
        row,
    )

    # Step 4: B re-claims (sticky) and fails again -> budget exhausted.
    await _complete_via_poll(
        authed_admin_client,
        task_id=task_id,
        worker_id=w_b,
        key=k_b,
        exit_code=1,
        status="failed",
    )

    status, n_exec = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", (
        "A✓ from epoch 0 must not mask B's persistent failure",
        status,
    )
    assert n_exec == 5, f"2 from epoch 0 + 2 from epoch 1 + 1 re-claim, found {n_exec}"
    # The post-conversion re-claim belongs to the current epoch (single source
    # of the epoch): without stamping it, the scoped close would not see it.
    epochs = sorted(e["snapshot_epoch"] for e in await _read_executions(migrated_db_url, task_id))
    assert epochs == [0, 0, 1, 1, 1], epochs
    # The mark survives the close: the task stays in count because of the conversion.
    assert (await _read_task_row(migrated_db_url, task_id, "converted_from"))[
        "converted_from"
    ] == "all_available"


@pytest.mark.asyncio
async def test_e2e_72m_sticky_conversion_success_still_closes_completed(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Counterpart: the epoch scope must not break the legitimate success
    of the sticky conversion. Same scenario, but B succeeds on its re-claim: the
    current epoch gathers 2 `completed` (A from epoch 1 + B) = `target_count`,
    so the task closes `completed`."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    task_id = await _setup_converted_sticky_task(
        authed_admin_client, migrated_db_url, w_a=w_a, k_a=k_a, w_b=w_b, k_b=k_b
    )

    await _complete_via_poll(
        authed_admin_client,
        task_id=task_id,
        worker_id=w_b,
        key=k_b,
        exit_code=0,
        status="completed",
    )

    row = await _read_task_row(
        migrated_db_url, task_id, "status::text AS status, outcome, completed_at"
    )
    assert row["status"] == "completed", (
        "the current epoch completed the snapshot {A,B}: it must close completed",
        row,
    )
    assert row["outcome"] is None, row
    assert row["completed_at"] is not None, row


@pytest.mark.asyncio
async def test_e2e_72n_native_count_close_stays_full_history(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A NATIVE count task (no conversion)
    keeps the cumulative close over the whole history. Mixed history:
    B✗ + A✓ close the first round as `failed` (1 < target_count=2); the
    admin retry grants one more attempt and the `completed` that closes it reaches
    `target_count` by counting the earlier A✓ -> `completed`. The provenance
    mark stays NULL: nothing to scope by epoch."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")

    create = await authed_admin_client.post("/admin/tasks", json={"target": "2"})
    assert create.status_code == 200, create.text
    task_id = int(create.json()["task_id"])

    await _complete_via_poll(
        authed_admin_client,
        task_id=task_id,
        worker_id=w_a,
        key=k_a,
        exit_code=0,
        status="completed",
    )
    await _complete_via_poll(
        authed_admin_client,
        task_id=task_id,
        worker_id=w_b,
        key=k_b,
        exit_code=1,
        status="failed",
    )
    status, _ = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", status

    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text
    row = await _read_task_row(
        migrated_db_url, task_id, "target_mode, converted_from, snapshot_epoch"
    )
    assert row == {"target_mode": "count", "converted_from": None, "snapshot_epoch": 0}, row

    # A takes the extra attempt (the retry does not pin a pool -- default
    # `force_same_worker=false` -- and the fairness yield favors the worker with
    # the oldest `assigned_at`). Its `completed` brings the full-history
    # count to 2 = target_count.
    await _complete_via_poll(
        authed_admin_client,
        task_id=task_id,
        worker_id=w_a,
        key=k_a,
        exit_code=0,
        status="completed",
    )
    status, n_exec = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "completed", (
        "native count: the earlier completed still counts",
        status,
    )
    assert n_exec == 3, n_exec


@pytest.mark.asyncio
@pytest.mark.usefixtures("example_plugin")
async def test_e2e_72o_converted_outcome_preference_scoped_to_epoch(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """The plain-success preference of `outcome` is also scoped to the epoch
    in a converted task.

    Scenario: epoch 0 has a PLAIN `completed` (no outcome) and the current epoch
    closes only with successes WITH outcome (exit 3 -> `empty_result`). With the
    scoped window, `completed_plain_scoped` = 0 in the current epoch, so
    `tasks.outcome` takes that of the most recent successful execution of that
    epoch. Counting full-history there would be a `completed_plain` > 0 (the plain
    one from epoch 0) and the outcome would collapse to NULL, attributing to the
    current run a plain success that did not happen. It is the only case that
    distinguishes `completed_plain_scoped` from `completed_plain`.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")

    # Test plugin payload: a plugin's outcome profile only applies to a task
    # its extractor recognizes, and this scenario needs the exit 3 below to
    # close `completed`+`empty_result` through that profile.
    create = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": "all-available",
            "auto_retry": {"max_attempts": 1, "backoff_seconds": 0},
            "payload": example_payload(),
        },
    )
    assert create.status_code == 200, create.text
    task_id = int(create.json()["task_id"])

    # Epoch 0: PLAIN A✓ (exit 0, outcome NULL), B✗ -> failed.
    await _complete_via_poll(
        authed_admin_client,
        task_id=task_id,
        worker_id=w_a,
        key=k_a,
        exit_code=0,
        status="completed",
    )
    await _complete_via_poll(
        authed_admin_client, task_id=task_id, worker_id=w_b, key=k_b, exit_code=1, status="failed"
    )
    status, _ = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", status

    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text

    # Epoch 1: A closes with exit 3 -> `completed` WITH outcome 'empty_result'
    # (test plugin profile). The auto-retry guard skips the
    # completed+outcome combination, so A causes no conversion.
    await _complete_via_poll(
        authed_admin_client, task_id=task_id, worker_id=w_a, key=k_a, exit_code=3, status="failed"
    )
    # B fails with exit 75 -> sticky conversion (provenance mark).
    await _complete_via_poll(
        authed_admin_client, task_id=task_id, worker_id=w_b, key=k_b, exit_code=75, status="failed"
    )
    row = await _read_task_row(migrated_db_url, task_id, "converted_from, target_count")
    assert row == {"converted_from": "all_available", "target_count": 2}, row

    # B re-claims and also closes with exit 3: the current epoch gathers 2 successes,
    # both WITH outcome.
    await _complete_via_poll(
        authed_admin_client, task_id=task_id, worker_id=w_b, key=k_b, exit_code=3, status="failed"
    )

    closed = await _read_task_row(migrated_db_url, task_id, "status::text AS status, outcome")
    assert closed["status"] == "completed", closed
    assert closed["outcome"] == "empty_result", (
        "the plain success of epoch 0 must not erase the outcome of the current epoch",
        closed,
    )


@pytest.mark.asyncio
async def test_e2e_72p_legacy_converted_row_keeps_full_history(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Explicit back-compat of the "no backfill" decision. A task
    converted BEFORE the deploy has no mark (`converted_from` NULL) and its
    claim executions were born with the `DEFAULT 0` of `snapshot_epoch`. That
    row keeps the old semantics: the count-mode close accumulates the whole
    history and closes `completed`.

    The assert documents the inherited behavior, it does not endorse it: it is the
    accepted price of not being able to identify past conversions. A later admin
    retry on that task goes through the sticky retarget and the mark is set
    from then on.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    task_id = await _setup_converted_sticky_task(
        authed_admin_client, migrated_db_url, w_a=w_a, k_a=k_a, w_b=w_b, k_b=k_b
    )

    # Simulate the legacy row: the conversion happened before the 0050 migration,
    # so it left no mark.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET converted_from = NULL WHERE id = :tid"),
                {"tid": task_id},
            )
    finally:
        await engine.dispose()

    # B re-claims; its execution is forced to epoch 0 (the pre-fix claim did not
    # stamp `snapshot_epoch`, it stayed at the DEFAULT).
    body = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body["task"] is not None, body
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE task_executions SET snapshot_epoch = 0 "
                    "WHERE task_id = :tid AND status = 'claimed'"
                ),
                {"tid": task_id},
            )
    finally:
        await engine.dispose()

    done = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_b},
        json={
            "lease_token": body["task"]["lease_token"],
            "exit_code": 1,
            "status": "failed",
        },
    )
    assert done.status_code == 200, done.text

    closed = await _read_task_row(
        migrated_db_url, task_id, "status::text AS status, converted_from"
    )
    assert closed == {"status": "completed", "converted_from": None}, (
        "without a mark, the count-mode close is still full-history (back-compat)",
        closed,
    )


@pytest.mark.asyncio
async def test_e2e_72g_b3_cancel_propagates_to_siblings(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """When a worker reports `status='cancelled'` and that
    report CLOSES the task, the active sibling executions must move to
    `cancelled` -- mirror of the admin cancel (`cancel_task_core`). Without the fix,
    the close (`_SQL_CLOSE_TASK_CANCELLED`) does not touch the siblings and B
    stays `running` under a terminal task."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None

    # A reports a terminal cancel (outer timeout) -> the cancelled close closes the task.
    done_a = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={
            "lease_token": body_a["task"]["lease_token"],
            "exit_code": None,
            "status": "cancelled",
            "cancelled_reason": "outer_timeout",
        },
    )
    assert done_a.status_code == 200, done_a.text

    status, _ = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "cancelled", status

    by_worker = {
        e["worker_id"]: e["status"] for e in await _read_executions(migrated_db_url, task_id)
    }
    assert by_worker[w_a] == "cancelled"
    assert by_worker[w_b] == "cancelled", (
        "the active sibling execution must propagate to 'cancelled'",
        by_worker,
    )


@pytest.mark.asyncio
async def test_e2e_72h_b3_cancel_then_retry_succeeds(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Report sequence: all_available {A,B}; A reports
    `cancelled` -> task `cancelled`; the admin retry must answer 200 and
    re-snapshot the new epoch with A and B. Without the fix, B stays `running`
    and the re-snapshot INSERT violates the partial unique
    `task_executions_active_uk` -> IntegrityError -> HTTP 500."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None

    done_a = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={
            "lease_token": body_a["task"]["lease_token"],
            "exit_code": None,
            "status": "cancelled",
            "cancelled_reason": "outer_timeout",
        },
    )
    assert done_a.status_code == 200, done_a.text

    # Canonical recovery path: admin retry. It must NOT give 500.
    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text

    # Epoch 2 with both workers; they complete it -> the task closes `completed`.
    for wid, key in ((w_a, k_a), (w_b, k_b)):
        body = await _poll_idle(authed_admin_client, wid, key)
        assert body["task"] is not None, (wid, body)
        done = await authed_admin_client.post(
            f"/v1/tasks/{task_id}/complete",
            headers={"X-API-Key": key},
            json={
                "lease_token": body["task"]["lease_token"],
                "exit_code": 0,
                "status": "completed",
            },
        )
        assert done.status_code == 200, done.text

    status, n_exec = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "completed", status
    assert n_exec == 4, f"2 from epoch 1 (cancelled) + 2 from epoch 2, found {n_exec}"


@pytest.mark.asyncio
async def test_e2e_72i_b3_cancel_kills_sibling_via_poll(
    authed_admin_client: AsyncClient,
) -> None:
    """The sibling cancelled by the propagation keeps its
    `lease_token` (`_SQL_COMPLETE` does not clear it), so its next poll
    must carry the `cancel_signal` -- the same machinery as the admin cancel
    (`_SQL_POLL_CANCEL_SIGNALS`), zero new signal code."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None
    token_b = body_b["task"]["lease_token"]

    done_a = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={
            "lease_token": body_a["task"]["lease_token"],
            "exit_code": None,
            "status": "cancelled",
            "cancelled_reason": "outer_timeout",
        },
    )
    assert done_a.status_code == 200, done_a.text

    # B's next poll carries the kill signal for its cancelled execution.
    nxt = await _poll_idle(authed_admin_client, w_b, k_b)
    signals = nxt["cancel_signals"]
    assert len(signals) == 1, nxt
    assert signals[0]["task_id"] == task_id
    assert signals[0]["lease_token"] == token_b


@pytest.mark.asyncio
async def test_e2e_72j_retry_snapshot_excludes_worker_with_active_execution(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Defense: if some latent path leaves an ACTIVE execution under
    a terminal task, the retry re-snapshot must EXCLUDE that worker
    (`NOT EXISTS`) instead of violating `task_executions_active_uk` -> 500.
    With the propagation of section 1 this is a no-op in normal operation --
    it is a safety belt: deterministic snapshot without that worker, never an opaque 500.

    Fixture: A completes normally; we force the task to `failed` with B's
    execution still `running` (broken state, via SQL)."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None

    done_a = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={
            "lease_token": body_a["task"]["lease_token"],
            "exit_code": 0,
            "status": "completed",
        },
    )
    assert done_a.status_code == 200, done_a.text

    # Broken state: terminal task with B's execution still active.
    await _force_task_terminal(migrated_db_url, task_id)

    # Retry: without the defense, the snapshot INSERT for B violates the partial
    # unique -> 500. With the defense, B is left out and only A enters epoch 2.
    retry = await authed_admin_client.post(f"/admin/tasks/{task_id}/retry", json={})
    assert retry.status_code == 200, retry.text
    # Response contract: non-empty snapshot (A got in) -> 'dispatching'.
    assert retry.json()["status"] == "dispatching", retry.text

    execs = await _read_executions(migrated_db_url, task_id)
    epoch2 = [e for e in execs if e["snapshot_epoch"] == 1]
    assert [e["worker_id"] for e in epoch2] == [w_a], (
        "the re-snapshot must exclude the worker with an active execution",
        execs,
    )


@pytest.mark.asyncio
async def test_e2e_72k_b3_late_cancel_after_terminal_does_not_propagate(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """No-flip guard: if the task is ALREADY terminal when a
    `complete` with `status='cancelled'` arrives, `_SQL_CLOSE_TASK_CANCELLED`
    (`RETURNING 1`) returns no row and `_SQL_CANCEL_EXECUTIONS` is NOT propagated.
    Propagating unconditionally would mask broken invariants: here it would cancel
    A (still active under the `failed` task) without anyone asking for it.

    Fixture: we force the task to `failed` (broken state, via SQL) with A and B still
    active (`claimed` -- they only become `running` with the heartbeat); B reports
    a late `cancelled` -> 200 (its execution closes through `_SQL_COMPLETE`), the
    task stays `failed` (it is not overwritten to `cancelled`) and A stays `claimed`
    untouched (zero propagation)."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "all-available"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None

    # Broken state: terminal task with BOTH executions still active.
    await _force_task_terminal(migrated_db_url, task_id)

    # Late cancelled close from B: its execution is still active (`claimed`) with a valid
    # lease, so `_SQL_COMPLETE` matches -> 200. But the task is already terminal: the
    # flip returns empty and the propagation must NOT run.
    done_b = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_b},
        json={
            "lease_token": body_b["task"]["lease_token"],
            "exit_code": None,
            "status": "cancelled",
            "cancelled_reason": "outer_timeout",
        },
    )
    assert done_b.status_code == 200, done_b.text

    # The task is NOT overwritten to `cancelled` (idempotency of cancelled close vs terminal).
    status, _ = await _read_task_status_and_exec_count(migrated_db_url, task_id)
    assert status == "failed", status

    by_worker = {
        e["worker_id"]: e["status"] for e in await _read_executions(migrated_db_url, task_id)
    }
    assert by_worker[w_b] == "cancelled", by_worker  # closed through _SQL_COMPLETE
    assert by_worker[w_a] == "claimed", (
        "without a flip there is NO propagation: A's execution must not be touched",
        by_worker,
    )


# --- Task claim (concurrency) ---------------------------------------------


@pytest.mark.asyncio
async def test_e2e_73_concurrent_polls_respect_target(
    authed_admin_client: AsyncClient,
) -> None:
    """Concurrent claim flow: target=2 with 4 workers competing for the task.

    The correctness invariant is **never exceed `target_count` active claims at
    once** (cap), and that the mechanism eventually delivers the 2 slots. We
    model reality (workers that poll again): a single concurrent burst may
    deliver <2 because the claimer holds the `tasks` row lock while it
    signs+commits, making the others SKIP and receive `null` in THAT burst -- they
    poll again and fill the remaining slots. What must NEVER happen is a 3rd claim
    (overclaim). See the concurrency note in
    `orcaster_server/api/poll.py::_SQL_POLL_CLAIM_OPEN_LOCK`.
    """
    workers: list[tuple[str, str]] = [
        await _register(authed_admin_client, f"w{i}") for i in range(4)
    ]
    create = await authed_admin_client.post("/admin/tasks", json={"target": "2"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    claimers: set[str] = set()
    # Re-round: each round, the workers that have not claimed yet poll at once.
    # Generous round cap: with 4 workers and target=2, 2 rounds suffice barring
    # extreme contention; 10 leaves margin without hanging if something goes wrong.
    for _ in range(10):
        pending = [(wid, key) for wid, key in workers if wid not in claimers]
        if len(claimers) >= 2 or not pending:
            break
        results = await asyncio.gather(
            *(
                authed_admin_client.post(
                    f"/v1/workers/{wid}/poll",
                    headers={"X-API-Key": key},
                    json={"status": "idle", "lease_seconds": 30},
                )
                for wid, key in pending
            )
        )
        for (wid, _key), resp in zip(pending, results, strict=True):
            assert resp.status_code == 200, resp.text
            body = resp.json()
            if body["task"] is not None:
                assert body["task"]["task_id"] == task_id, body
                claimers.add(wid)
        # Cap: at no point can there be more than target_count claimers.
        assert len(claimers) <= 2, claimers

    # The mechanism eventually delivers exactly the 2 slots.
    assert len(claimers) == 2, claimers


@pytest.mark.asyncio
async def test_e2e_73b_staggered_completion_runs_exactly_target_count(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Regression: target=2 with staggered completion produces **exactly
    2** executions.

    With the active-only predicate, the `completed` of the fast worker
    released its slot before the close (`terminal >= target_count`)
    applied, and its re-poll slipped in with an attempt 3. Now the
    budget counts every non-expired execution, so the fast worker's re-poll
    does NOT receive the task.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "2"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    token_a = body_a["task"]["lease_token"]
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None
    token_b = body_b["task"]["lease_token"]

    # A (fast worker) completes while B is still running.
    done_a = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={"lease_token": token_a, "exit_code": 0, "status": "completed"},
    )
    assert done_a.status_code == 200, done_a.text

    # The bug window: A re-polls idle BEFORE B finishes. The slot of
    # its completed must NOT reopen.
    repoll_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert repoll_a["task"] is None, (
        "the fast worker slipped in with an extra attempt",
        repoll_a,
    )

    done_b = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_b},
        json={"lease_token": token_b, "exit_code": 0, "status": "completed"},
    )
    assert done_b.status_code == 200, done_b.text

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            trow = (
                (
                    await conn.execute(
                        text("SELECT status FROM tasks WHERE id=:tid"),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
            n_exec = (
                await conn.execute(
                    text("SELECT count(*) FROM task_executions WHERE task_id=:tid"),
                    {"tid": task_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert trow is not None and trow["status"] == "completed"
    assert n_exec == 2, f"expected exactly 2 executions, found {n_exec}"


@pytest.mark.asyncio
async def test_e2e_73c_failed_execution_does_not_reopen_slot(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """A `failed` execution (without an auto-retry policy) consumes
    budget -- it does NOT reopen the slot for a third worker.

    target=2: A fails (exit 1) while B is still running. A third worker
    C polls -> nothing (A's failed consumes its slot). B completes -> the task
    closes `failed` (budget exhausted with completed=1 < target_count=2;
    count-mode success requires EXACTLY target_count completed)
    with exactly 2 executions. Previously (active-only predicate) C would have
    claimed and its result would have arrived orphaned after the close.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    w_c, k_c = await _register(authed_admin_client, "c")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "2"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    token_a = body_a["task"]["lease_token"]
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None
    token_b = body_b["task"]["lease_token"]

    fail_a = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={"lease_token": token_a, "exit_code": 1, "status": "failed"},
    )
    assert fail_a.status_code == 200, fail_a.text

    # A's failed consumes budget: C does not receive the task.
    body_c = await _poll_idle(authed_admin_client, w_c, k_c)
    assert body_c["task"] is None, (
        "a failed must not reopen the slot",
        body_c,
    )

    done_b = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_b},
        json={"lease_token": token_b, "exit_code": 0, "status": "completed"},
    )
    assert done_b.status_code == 200, done_b.text

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            trow = (
                (
                    await conn.execute(
                        text("SELECT status FROM tasks WHERE id=:tid"),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
            n_exec = (
                await conn.execute(
                    text("SELECT count(*) FROM task_executions WHERE task_id=:tid"),
                    {"tid": task_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    # Count-mode success = exactly target_count completed. With 1 failed +
    # 1 completed the budget ran out without reaching 2 successes -> 'failed'.
    assert trow is not None and trow["status"] == "failed"
    assert n_exec == 2, f"expected exactly 2 executions, found {n_exec}"


@pytest.mark.asyncio
async def test_e2e_73d_expired_does_not_close_task_with_n_minus_1_successes(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Regression: crash (`expired`) + staggered completion does NOT close the
    task with N-1 successes.

    target=2: A crashes (its execution becomes `expired`, returning its slot)
    while B is still running. B completes -> the task does NOT close (expired does not
    count for the count-mode close; completed=1 < 2). A recovery worker C
    claims the returned slot and completes -> completed=2 -> the task
    closes `completed`. Previously (`expired` counted as terminal in the close)
    the task closed `completed` with a single success when B completed, also
    leaving the recovery claim orphaned if it had already fired.
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    w_c, k_c = await _register(authed_admin_client, "c")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "2"})
    assert create.status_code == 200, create.text
    task_id = create.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None
    token_b = body_b["task"]["lease_token"]

    # Crash of A: housekeeping would mark its expired lease as `expired`;
    # here we emulate it directly in the DB.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE task_executions SET status='expired', finished_at=now() "
                    "WHERE task_id=:tid AND worker_id=:wid"
                ),
                {"tid": task_id, "wid": w_a},
            )
    finally:
        await engine.dispose()

    done_b = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_b},
        json={"lease_token": token_b, "exit_code": 0, "status": "completed"},
    )
    assert done_b.status_code == 200, done_b.text

    # The task stays open: expired does not count for the close.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            trow = (
                (
                    await conn.execute(
                        text("SELECT status FROM tasks WHERE id=:tid"),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert trow is not None and trow["status"] not in ("completed", "failed"), (
        "the task must not close with N-1 successes when there is an expired",
        trow,
    )

    # Recovery: C takes the slot returned by the expired one and completes.
    body_c = await _poll_idle(authed_admin_client, w_c, k_c)
    assert body_c["task"] is not None, "the expired slot must be available"
    done_c = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_c},
        json={
            "lease_token": body_c["task"]["lease_token"],
            "exit_code": 0,
            "status": "completed",
        },
    )
    assert done_c.status_code == 200, done_c.text

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            trow = (
                (
                    await conn.execute(
                        text("SELECT status FROM tasks WHERE id=:tid"),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
            n_completed = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM task_executions "
                        "WHERE task_id=:tid AND status='completed'"
                    ),
                    {"tid": task_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert trow is not None and trow["status"] == "completed"
    assert n_completed == 2, f"expected exactly 2 completed, found {n_completed}"


@pytest.mark.asyncio
async def test_e2e_73e_full_open_task_does_not_starve_lower_priority(
    authed_admin_client: AsyncClient,
) -> None:
    """Regression: a count task with an exhausted budget but STILL OPEN
    must not block the dispatch of lower-priority tasks.

    T1 (priority=10, target=2): A fails (exit 1) and B keeps running ->
    count(non-expired)=2 = attempt_budget, no slots, but T1 does not close until
    B finishes (exact success needs B's outcome). T2 (priority=0)
    waits in the queue. An idle worker C polls -> it must receive T2.

    Before the budget pre-filter in step 4a, the LOCK always picked T1
    (higher priority, LIMIT 1), the step 4b INSERT rejected it and the handler
    returned `task: null` without trying the next candidate: T2 stayed
    starved until T1 closed (the duration of B's execution).
    """
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    w_c, k_c = await _register(authed_admin_client, "c")

    create_t1 = await authed_admin_client.post("/admin/tasks", json={"target": "2", "priority": 10})
    assert create_t1.status_code == 200, create_t1.text
    t1_id = create_t1.json()["task_id"]

    body_a = await _poll_idle(authed_admin_client, w_a, k_a)
    assert body_a["task"] is not None and body_a["task"]["task_id"] == t1_id
    token_a = body_a["task"]["lease_token"]
    body_b = await _poll_idle(authed_admin_client, w_b, k_b)
    assert body_b["task"] is not None and body_b["task"]["task_id"] == t1_id
    token_b = body_b["task"]["lease_token"]

    # A fails without auto-retry -> its slot is consumed and T1 stays
    # open waiting for B (running).
    fail_a = await authed_admin_client.post(
        f"/v1/tasks/{t1_id}/complete",
        headers={"X-API-Key": k_a},
        json={"lease_token": token_a, "exit_code": 1, "status": "failed"},
    )
    assert fail_a.status_code == 200, fail_a.text

    # Lower-priority T2 enters the queue AFTER T1 became full.
    create_t2 = await authed_admin_client.post("/admin/tasks", json={"target": "1", "priority": 0})
    assert create_t2.status_code == 200, create_t2.text
    t2_id = create_t2.json()["task_id"]

    # Idle C must receive T2: a full-but-open T1 cannot clog the queue.
    body_c = await _poll_idle(authed_admin_client, w_c, k_c)
    assert body_c["task"] is not None, (
        "a full-but-open T1 starved T2: the poll returned null",
        body_c,
    )
    assert body_c["task"]["task_id"] == t2_id, body_c

    # Scenario hygiene: B completes and T1 closes `failed` (1 failed +
    # 1 completed with exhausted budget, exact success requires 2 completed).
    done_b = await authed_admin_client.post(
        f"/v1/tasks/{t1_id}/complete",
        headers={"X-API-Key": k_b},
        json={"lease_token": token_b, "exit_code": 0, "status": "completed"},
    )
    assert done_b.status_code == 200, done_b.text


# --- Heartbeats with progress (not applicable) -----------------------
#
# Heartbeat is implicit in `/poll`: there is no explicit heartbeat endpoint,
# no `claimed`->`running` transition driven by it, and no persistence of
# `progress`/`progress_message` from it, so there is no e2e test for it here.


# --- Output + complete ----------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_75_output_then_complete_closes_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Output and complete flow: poll -> several POST /output (event batches) -> POST
    /complete with exit_code=0 -> task becomes `completed` and the events remain in
    output_events (single source of truth)."""
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    body = await _poll_idle(authed_admin_client, worker_id, key)
    token = body["task"]["lease_token"]

    for seq, line in enumerate(("line1\n", "line2\n", "line3\n"), start=1):
        out = await authed_admin_client.post(
            f"/v1/tasks/{task_id}/output",
            headers={"X-API-Key": key},
            json={
                "lease_token": token,
                "events": [
                    {
                        "seq": seq,
                        "ts": "2026-06-03T00:00:00+00:00",
                        "stream": "stdout",
                        "text": line,
                    }
                ],
            },
        )
        assert out.status_code == 202

    # complete: `stdout` travels as an extra (outside the schema) and is
    # ignored; it adds nothing to the output.
    done = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={
            "lease_token": token,
            "exit_code": 0,
            "stdout": "ignored\n",
            "status": "completed",
        },
    )
    assert done.status_code == 200, done.text

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            erow = (
                (
                    await conn.execute(
                        text("SELECT status, exit_code FROM task_executions WHERE task_id=:tid"),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
            # output_events: NOT purged on complete (source of truth).
            events = (
                (
                    await conn.execute(
                        text(
                            "SELECT oe.text FROM output_events oe "
                            "JOIN task_executions e ON e.id = oe.execution_id "
                            "WHERE e.task_id=:tid ORDER BY oe.seq"
                        ),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .all()
            )
            trow = (
                (
                    await conn.execute(
                        text("SELECT status, completed_at FROM tasks WHERE id=:tid"),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert erow is not None
    assert erow["status"] == "completed"
    assert erow["exit_code"] == 0
    # The full output lives in output_events, unpurged.
    assert "".join(e["text"] for e in events) == "line1\nline2\nline3\n"
    assert trow is not None
    assert trow["status"] == "completed"
    assert trow["completed_at"] is not None


# --- Worker loses network, another claims ---------------------------------


@pytest.mark.asyncio
async def test_e2e_76_expired_lease_reclaimed(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Expired lease flow: worker A claims, "loses network" (lease expired), the
    background job reclaims the lease, worker B claims the same task; A's late
    `complete` returns 409 (its lease no longer owns the execution -> its work is
    discarded).

    Notes on the flow:
    - A late heartbeat ('expired') is not covered here; `complete`
      gives the same guarantee (-> 409).
    - Reclaiming the expired lease lives in the background job, not in the
      poll: we run `_reclaim_expired_leases` before B can claim."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    first = await _poll_idle(authed_admin_client, w_a, k_a)
    assert first["task"] is not None
    token_a = first["task"]["lease_token"]

    await _set_lease_expired(migrated_db_url, task_id, w_a)

    # The reclaim does not travel in the poll; the background job triggers it.
    engine = create_async_engine(migrated_db_url)
    try:
        await _reclaim_expired_leases(engine)
    finally:
        await engine.dispose()

    second = await _poll_idle(authed_admin_client, w_b, k_b)
    assert second["task"] is not None, second
    assert second["task"]["task_id"] == task_id

    # A's late `complete`: its lease no longer owns a live execution -> 409.
    done_a = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": k_a},
        json={"lease_token": token_a, "exit_code": 0, "status": "completed"},
    )
    assert done_a.status_code == 409, done_a.text


# --- Cancellation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_77_cancel_propagates_via_poll(
    authed_admin_client: AsyncClient,
) -> None:
    """Cancellation flow: poll (claim) -> admin cancel -> the worker's next poll
    carries the `cancel_signal` of the cancelled execution.

    The cancel signal travels in the `/poll` (`cancel_signals`), delivered
    exactly-once (see test_poll_api.py)."""
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    body = await _poll_idle(authed_admin_client, worker_id, key)
    assert body["task"] is not None, body
    token = body["task"]["lease_token"]

    cancel = await authed_admin_client.post(
        f"/admin/tasks/{task_id}/cancel", json={"reason": "user asked"}
    )
    assert cancel.status_code == 200, cancel.text

    # The worker's next poll carries the cancel signal of its live execution.
    nxt = await _poll_idle(authed_admin_client, worker_id, key)
    signals = nxt["cancel_signals"]
    assert len(signals) == 1, nxt
    assert signals[0]["task_id"] == task_id
    assert signals[0]["lease_token"] == token


# --- Signed command push --------------------------------------------------


@pytest.fixture(scope="module")
def _ed25519_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path_factory.mktemp("ed25519_e2e") / "server.pem"
    p.write_bytes(pem)
    return p


@pytest.mark.asyncio
async def test_e2e_78_signed_push_task_via_poll(
    authed_admin_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    _ed25519_key: Path,
) -> None:
    """Signed push flow (unify): admin push command -> signed `delivery_mode='push'`
    task -> worker receives it in /poll -> signature verifiable with the public key
    over the 6 canonical fields {command, created_at, idempotency_key,
    payload, task_id, worker_id}."""
    monkeypatch.setattr(settings, "ed25519_private_key_path", str(_ed25519_key))
    signing.reset_key_cache()

    worker_id, api_key = await _register(authed_admin_client)
    expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()

    push = await authed_admin_client.post(
        f"/admin/workers/{worker_id}/command",
        json={"command": "restart", "payload": {"force": True}, "expires_at": expires},
    )
    assert push.status_code == 200, push.text
    pushed = push.json()
    task_id = int(pushed["task_id"])
    sig_b64 = pushed["signature"]
    assert base64.b64decode(sig_b64)  # decodable signature

    body = await _poll_idle(authed_admin_client, worker_id, api_key)
    assert body["task"] is not None, body
    task = body["task"]
    assert task["task_id"] == task_id
    assert task["command"] == "restart"
    assert task["delivery_mode"] == "push"
    assert task["signed"] is True
    assert task["signature"] == sig_b64
    assert task["worker_id"] == worker_id
    assert task["target_worker_id"] == worker_id

    # Rebuild the canonical message and verify the signature.
    priv = serialization.load_pem_private_key(_ed25519_key.read_bytes(), password=None)
    pub = priv.public_key()
    sig = base64.b64decode(sig_b64)
    # `created_at` travels as an ISO string in the JSON; Pydantic parses it, but
    # the real client will receive it as a string. Here the JSON already carries it that way.
    created_at_iso = task["created_at"]
    assert verify_task_signature(
        pub,
        sig,
        task_id=task_id,
        worker_id=worker_id,
        command="restart",
        payload={"force": True},
        idempotency_key=task["idempotency_key"],
        created_at_iso=created_at_iso,
    )


# --- Pause/resume task ----------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_79_pause_blocks_poll_resume_unblocks(
    authed_admin_client: AsyncClient,
) -> None:
    """Pause/resume flow: queued task -> pause -> worker receives no task (task=null);
    resume -> worker claims OK."""
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    pause = await authed_admin_client.post(f"/admin/tasks/{task_id}/pause", json={})
    assert pause.status_code == 200, pause.text

    blocked = await _poll_idle(authed_admin_client, worker_id, key)
    assert blocked["task"] is None, blocked

    resume = await authed_admin_client.post(f"/admin/tasks/{task_id}/resume", json={})
    assert resume.status_code == 200, resume.text
    assert resume.json() == {"status": "queued"}

    unblocked = await _poll_idle(authed_admin_client, worker_id, key)
    assert unblocked["task"] is not None
    assert unblocked["task"]["task_id"] == task_id


# --- Deleting a completed task --------------------------------------------


@pytest.mark.asyncio
async def test_e2e_711_delete_completed_task(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Task deletion flow: task completes through the real flow -> DELETE 204 -> row
    disappears from the DB."""
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = create.json()["task_id"]

    body = await _poll_idle(authed_admin_client, worker_id, key)
    token = body["task"]["lease_token"]

    complete = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={
            "lease_token": token,
            "exit_code": 0,
            "status": "completed",
        },
    )
    assert complete.status_code == 200, complete.text

    deleted = await authed_admin_client.delete(f"/admin/tasks/{task_id}")
    assert deleted.status_code == 204

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(text("SELECT id FROM tasks WHERE id=:tid"), {"tid": task_id})
            ).first()
    finally:
        await engine.dispose()
    assert row is None


# --- Deleting an offline worker -------------------------------------------


@pytest.mark.asyncio
async def test_e2e_712_delete_offline_worker(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Worker deletion flow: registered worker -> goodbye -> offline -> DELETE 204.

    After the soft-delete the row persists (to preserve the history
    of task_executions), but is marked with `deleted_at IS NOT NULL`.
    """
    worker_id, api_key = await _register(authed_admin_client)

    bye = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/goodbye",
        headers={"X-API-Key": api_key},
    )
    assert bye.status_code == 200, bye.text

    await _set_worker_offline(migrated_db_url, worker_id)

    deleted = await authed_admin_client.delete(f"/admin/workers/{worker_id}")
    assert deleted.status_code == 204, deleted.text

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT deleted_at FROM workers WHERE id=:id"),
                    {"id": worker_id},
                )
            ).first()
    finally:
        await engine.dispose()
    assert row is not None, "soft-delete must preserve the row"
    assert row[0] is not None, "deleted_at must be set"


# --- Sticky auto-retry ----------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_713_auto_retry_sticky_max_attempts_3(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Acceptance criterion of sticky auto-retry: a task with `auto_retry.max_attempts=3`
    whose worker reports exit_code=75 retries 3 times on the same worker
    without operator intervention. The 4th complete (attempts_used==max) closes
    the task as terminal."""
    worker_id, key = await _register(authed_admin_client)
    other_id, other_key = await _register(authed_admin_client, hostname="other")

    create = await authed_admin_client.post(
        "/admin/tasks",
        json={
            "target": "1",
            "auto_retry": {"max_attempts": 3, "backoff_seconds": 0},
        },
    )
    assert create.status_code == 200, create.text
    task_id = int(create.json()["task_id"])

    for expected_attempts_used in (1, 2, 3):
        body = await _poll_idle(authed_admin_client, worker_id, key)
        assert body["task"] is not None, f"cycle {expected_attempts_used}: expected re-claim"
        assert body["task"]["task_id"] == task_id
        token = body["task"]["lease_token"]

        done = await authed_admin_client.post(
            f"/v1/tasks/{task_id}/complete",
            headers={"X-API-Key": key},
            json={"lease_token": token, "exit_code": 75, "status": "failed"},
        )
        assert done.status_code == 200, done.text

        engine = create_async_engine(migrated_db_url)
        try:
            async with engine.connect() as conn:
                trow = (
                    (
                        await conn.execute(
                            text(
                                "SELECT status, auto_retry_attempts_used, "
                                "target_worker_pool FROM tasks WHERE id=:id"
                            ),
                            {"id": task_id},
                        )
                    )
                    .mappings()
                    .first()
                )
        finally:
            await engine.dispose()
        assert trow is not None
        assert trow["auto_retry_attempts_used"] == expected_attempts_used
        assert trow["status"] == "queued"
        assert trow["target_worker_pool"] == [worker_id]

        other_body = await _poll_idle(authed_admin_client, other_id, other_key)
        assert other_body["task"] is None, "sticky pool must not yield to other worker"

    body = await _poll_idle(authed_admin_client, worker_id, key)
    assert body["task"] is not None
    token = body["task"]["lease_token"]
    done = await authed_admin_client.post(
        f"/v1/tasks/{task_id}/complete",
        headers={"X-API-Key": key},
        json={"lease_token": token, "exit_code": 75, "status": "completed"},
    )
    assert done.status_code == 200, done.text

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            trow = (
                (
                    await conn.execute(
                        text(
                            "SELECT status, auto_retry_attempts_used, completed_at "
                            "FROM tasks WHERE id=:id"
                        ),
                        {"id": task_id},
                    )
                )
                .mappings()
                .first()
            )
            exec_count = (
                await conn.execute(
                    text("SELECT count(*) FROM task_executions WHERE task_id=:id"),
                    {"id": task_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert trow is not None
    assert trow["status"] == "completed"
    assert trow["auto_retry_attempts_used"] == 3
    assert trow["completed_at"] is not None
    assert exec_count == 4


# --- Implicit lease renewal via busy-poll -----------------------------------


@pytest.mark.asyncio
async def test_e2e_714_busy_poll_renews_in_flight_lease(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Implicit renewal flow: a `busy` worker that polls again with its
    `lease_token` renews the lease of its in-progress execution (implicit
    heartbeat). Without this renewal, a task longer than `lease_seconds`
    would see its execution expired by housekeeping midway through.

    Verified guarantees:
    - `lease_expires_at` advances well ahead of the claim value (claim with
      30s -> busy-poll with 300s => jump of >200s);
    - the execution does NOT expire: it transitions `claimed` -> `running`;
    - `current_execution_progress` is persisted as `progress`;
    - the busy-poll does not deliver a new task (the worker is already busy).
    """
    worker_id, key = await _register(authed_admin_client)
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = int(create.json()["task_id"])

    body = await _poll_idle(authed_admin_client, worker_id, key)
    assert body["task"] is not None, body
    token = body["task"]["lease_token"]

    before = await _read_execution(migrated_db_url, task_id)
    assert before["status"] == "claimed"

    busy = await authed_admin_client.post(
        f"/v1/workers/{worker_id}/poll",
        headers={"X-API-Key": key},
        json={
            "status": "busy",
            "lease_token": token,
            "current_task_id": task_id,
            "current_execution_progress": 42,
            "lease_seconds": 300,
        },
    )
    assert busy.status_code == 200, busy.text
    assert busy.json()["task"] is None, "a busy worker must not receive a new task"

    after = await _read_execution(migrated_db_url, task_id)
    assert after["lease_expires_at"] - before["lease_expires_at"] > timedelta(seconds=200), (
        f"lease not renewed: {before['lease_expires_at']} -> {after['lease_expires_at']}"
    )
    assert after["status"] == "running"
    assert after["progress"] == 42


@pytest.mark.asyncio
async def test_e2e_715_busy_poll_does_not_renew_foreign_execution(
    authed_admin_client: AsyncClient, migrated_db_url: str
) -> None:
    """Renewal only touches the worker's own execution. A worker that
    presents a `lease_token` that does not belong to it must not renew (or touch)
    another worker's execution."""
    w_a, k_a = await _register(authed_admin_client, "a")
    w_b, k_b = await _register(authed_admin_client, "b")
    create = await authed_admin_client.post("/admin/tasks", json={"target": "1"})
    task_id = int(create.json()["task_id"])

    first = await _poll_idle(authed_admin_client, w_a, k_a)
    assert first["task"] is not None
    token_a = first["task"]["lease_token"]

    before = await _read_execution(migrated_db_url, task_id)

    # Worker B presents A's lease_token. It is not its execution -> no renewal.
    busy_b = await authed_admin_client.post(
        f"/v1/workers/{w_b}/poll",
        headers={"X-API-Key": k_b},
        json={
            "status": "busy",
            "lease_token": token_a,
            "current_task_id": task_id,
            "lease_seconds": 300,
        },
    )
    assert busy_b.status_code == 200, busy_b.text

    after = await _read_execution(migrated_db_url, task_id)
    assert after["lease_expires_at"] == before["lease_expires_at"]
    assert after["status"] == before["status"]
