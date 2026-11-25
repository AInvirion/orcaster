"""Aggregation service of the status page (isolated, no HTTP).

Seeds workers as online/paused/offline (one soft-deleted) + tasks in several
states + active/terminal executions, and checks counts, global progress,
active_count per worker, total active executions, and that workers with
deleted_at do NOT count or appear. Seeding pattern of test_housekeeping.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from orcaster_server.status.snapshot import build_status_snapshot
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

_WS = DEFAULT_WORKSPACE_ID


async def _insert_worker(session, *, hostname: str, status: str, deleted: bool = False) -> str:
    row = await session.execute(
        text(
            """
            INSERT INTO workers (workspace_id, api_key_hash, hostname, os, arch,
                                 status, registered_at, last_heartbeat_at, deleted_at)
            VALUES (:ws, :hash, :host, 'linux', 'x86_64', :status,
                    now(), now(), :deleted_at)
            RETURNING id::text
            """
        ),
        {
            "ws": _WS,
            "hash": f"snap-{hostname}",
            "host": hostname,
            "status": status,
            "deleted_at": datetime.now(UTC) if deleted else None,
        },
    )
    return row.scalar_one()


async def _insert_task(session, *, status: str, outcome: str | None = None) -> int:
    row = await session.execute(
        text(
            """
            INSERT INTO tasks (workspace_id, command, payload, target_mode,
                               target_count, priority, status, outcome, run_at,
                               signed, signature)
            VALUES (:ws, 'echo', CAST(:p AS jsonb), 'count', 1, 0, :status, :outcome,
                    now(), true, decode(repeat('00', 64), 'hex'))
            RETURNING id
            """
        ),
        {"ws": _WS, "p": json.dumps({}), "status": status, "outcome": outcome},
    )
    return int(row.scalar_one())


async def _insert_execution(
    session, *, task_id: int, worker_id: str, status: str, attempt_index: int = 1
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO task_executions (task_id, worker_id, status,
                                         attempt_index, assigned_at, last_heartbeat_at)
            VALUES (:t, :w, :s, :attempt, now(), now())
            """
        ),
        {"t": task_id, "w": worker_id, "s": status, "attempt": attempt_index},
    )


@pytest.mark.asyncio
async def test_build_status_snapshot_counts_and_progress(db_session) -> None:
    s = db_session
    w_online = await _insert_worker(s, hostname="w-online", status="online")
    await _insert_worker(s, hostname="w-paused", status="paused")
    await _insert_worker(s, hostname="w-offline", status="offline")
    # Soft-deleted: must NOT count or appear.
    await _insert_worker(s, hostname="w-gone", status="online", deleted=True)

    # Tasks: 3 completed, 1 failed, 2 queued, 1 cancelled (+1 dispatching below)
    # -> total 8, non_cancelled 7.
    for _ in range(3):
        await _insert_task(s, status="completed")
    await _insert_task(s, status="failed")
    for _ in range(2):
        await _insert_task(s, status="queued")
    await _insert_task(s, status="cancelled")

    # One active execution (running) on the online worker + one terminal (does not count).
    t_run = await _insert_task(s, status="dispatching")
    await _insert_execution(s, task_id=t_run, worker_id=w_online, status="running")
    await _insert_execution(
        s, task_id=t_run, worker_id=w_online, status="completed", attempt_index=2
    )
    await s.commit()

    snap = await build_status_snapshot(s, _WS)

    assert snap.workspace_name == "Default"
    assert snap.workers_online == 1
    assert snap.workers_paused == 1
    assert snap.workers_offline == 1
    # 3 alive, ordered by registered_at ASC, 1-based, without the deleted one.
    assert [w.index for w in snap.workers] == [1, 2, 3]
    assert len(snap.workers) == 3
    online_entry = next(w for w in snap.workers if w.status == "online")
    assert online_entry.active_count == 1  # only the running one, not the completed

    # tasks_by_status normalizes to the 6 task_status states (includes
    # dispatching=1; this case seeds no outcome).
    assert snap.tasks_by_status == {
        "queued": 2,
        "dispatching": 1,
        "paused": 0,
        "completed": 3,
        "cancelled": 1,
        "failed": 1,
    }
    assert snap.completed_count == 3
    # failed_count = tasks_by_status['failed'] directly (no folding).
    assert snap.failed_count == 1
    assert snap.non_cancelled_total == 7  # 8 total - 1 cancelled
    assert snap.progress_pct == round(100 * 3 / 7, 1)  # 42.9, 1 decimal
    assert snap.active_executions_now == 1


@pytest.mark.asyncio
async def test_active_count_scoped_by_workspace(db_session) -> None:
    """Defense in depth: an execution from ANOTHER workspace on the same
    worker must NOT inflate the worker's active_count in this workspace."""
    s = db_session
    w_online = await _insert_worker(s, hostname="w-scope", status="online")

    # Another workspace with its own task.
    other_ws = (
        await s.execute(
            text(
                """
                INSERT INTO workspaces (name, slug)
                VALUES ('Other WS', 'other-ws-snapshot-scope')
                RETURNING id::text
                """
            )
        )
    ).scalar_one()
    other_task = (
        await s.execute(
            text(
                """
                INSERT INTO tasks (workspace_id, command, payload, target_mode,
                                   target_count, priority, status, run_at,
                                   signed, signature)
                VALUES (:ws, 'echo', CAST('{}' AS jsonb), 'count', 1, 0,
                        'dispatching', now(), true, decode(repeat('00', 64), 'hex'))
                RETURNING id
                """
            ),
            {"ws": other_ws},
        )
    ).scalar_one()

    # A task of the workspace under test with an active execution on the worker.
    t_mine = await _insert_task(s, status="dispatching")
    await _insert_execution(s, task_id=t_mine, worker_id=w_online, status="running")
    # An active execution from ANOTHER workspace on the SAME worker: must not count.
    await _insert_execution(
        s, task_id=int(other_task), worker_id=w_online, status="running", attempt_index=2
    )
    await s.commit()

    snap = await build_status_snapshot(s, _WS)
    online_entry = next(w for w in snap.workers if w.status == "online")
    # Only the execution of THIS workspace counts.
    assert online_entry.active_count == 1
    assert snap.active_executions_now == 1


@pytest.mark.asyncio
async def test_build_status_snapshot_empty_workspace(db_session) -> None:
    snap = await build_status_snapshot(db_session, _WS)
    assert snap.workers == []
    assert snap.workers_online == snap.workers_paused == snap.workers_offline == 0
    assert snap.tasks_by_status == {
        "queued": 0,
        "dispatching": 0,
        "paused": 0,
        "completed": 0,
        "cancelled": 0,
        "failed": 0,
    }
    assert snap.completed_count == 0
    assert snap.failed_count == 0
    assert snap.non_cancelled_total == 0
    assert snap.progress_pct == 0  # no division by zero
    assert snap.processed_count == 0
    assert snap.processed_pct == 0.0
    assert snap.active_executions_now == 0


@pytest.mark.asyncio
async def test_progress_is_100_only_when_fully_completed(db_session) -> None:
    """Exactly 100.0 ONLY when no non-cancelled work remains; with a pending
    task the percentage never reaches 100 (capped at 99.9 after rounding)."""
    s = db_session
    # 4 completed + 1 cancelled: the (non-cancelled) denominator is 4, all done.
    for _ in range(4):
        await _insert_task(s, status="completed")
    await _insert_task(s, status="cancelled")
    await s.commit()
    snap = await build_status_snapshot(s, _WS)
    assert snap.non_cancelled_total == 4
    assert snap.progress_pct == 100.0

    # Adding a queued task breaks completeness: it must no longer show 100.
    await _insert_task(s, status="queued")
    await s.commit()
    snap2 = await build_status_snapshot(s, _WS)
    assert snap2.completed_count == 4
    assert snap2.non_cancelled_total == 5
    assert snap2.progress_pct == 80.0
    assert snap2.progress_pct < 100.0


@pytest.mark.asyncio
async def test_outcome_closed_task_no_longer_folds_into_failed(db_session) -> None:
    """A close with an outcome (e.g. `outcome='no_result'`) is a real
    `completed` at the status level; `failed_count` is
    `tasks_by_status['failed']` as is, with no folding.
    """
    s = db_session
    await _insert_task(s, status="failed")
    await _insert_task(s, status="failed")
    await _insert_task(s, status="completed", outcome="no_result")
    await s.commit()

    snap = await build_status_snapshot(s, _WS)
    assert snap.tasks_by_status["failed"] == 2
    assert snap.tasks_by_status["completed"] == 1
    assert snap.failed_count == 2


@pytest.mark.asyncio
async def test_outcome_closed_tasks_count_toward_progress(db_session) -> None:
    """A close with an outcome DOES count as a success for public progress.

    That close is literally `completed` (the nuance lives in `outcome`, not in
    a separate status), so a workspace where everything closed with an
    outcome shows 100%.
    """
    s = db_session
    for _ in range(3):
        await _insert_task(s, status="completed", outcome="no_result")
    await s.commit()

    snap = await build_status_snapshot(s, _WS)
    assert snap.completed_count == 3
    assert snap.non_cancelled_total == 3
    assert snap.progress_pct == 100.0


@pytest.mark.asyncio
async def test_mix_plain_and_outcome_completed_progress(db_session) -> None:
    """Mix of plain completed + completed with outcome: both count toward 100%.

    2 plain completed + 2 completed with outcome='no_result' → all 4 are
    `completed` at the status level → denominator 4, numerator 4 → 100%.
    """
    s = db_session
    for _ in range(2):
        await _insert_task(s, status="completed")
    for _ in range(2):
        await _insert_task(s, status="completed", outcome="no_result")
    await s.commit()

    snap = await build_status_snapshot(s, _WS)
    assert snap.completed_count == 4
    assert snap.failed_count == 0
    assert snap.non_cancelled_total == 4
    assert snap.progress_pct == 100.0


@pytest.mark.asyncio
async def test_processed_count_and_processed_pct(db_session) -> None:
    """processed = completed + failed; processed_pct feeds the hero.

    3 completed + 1 failed + 2 queued + 1 cancelled → denominator 6 (non-
    cancelled), processed 4. The bar segments use progress_pct
    (completed layer) and processed_pct (failed layer), both capped;
    `progress_pct` (completed-based) does not change meaning.
    """
    s = db_session
    for _ in range(3):
        await _insert_task(s, status="completed")
    await _insert_task(s, status="failed")
    for _ in range(2):
        await _insert_task(s, status="queued")
    await _insert_task(s, status="cancelled")
    await s.commit()

    snap = await build_status_snapshot(s, _WS)
    assert snap.processed_count == 4
    assert snap.non_cancelled_total == 6
    assert snap.processed_pct == round(100 * 4 / 6, 1)  # 66.7
    assert snap.progress_pct == 50.0  # completed-based, unchanged


@pytest.mark.asyncio
async def test_processed_pct_100_only_when_all_processed(db_session) -> None:
    """processed_pct reaches exactly 100.0 only with no pending work.

    A workspace with failures but nothing pending is 100% PROCESSED even though
    progress_pct (completed-based) stays below 100.
    """
    s = db_session
    for _ in range(3):
        await _insert_task(s, status="completed")
    await _insert_task(s, status="failed")
    await s.commit()
    snap = await build_status_snapshot(s, _WS)
    assert snap.processed_count == 4
    assert snap.processed_pct == 100.0
    assert snap.progress_pct == 75.0

    # A queued task breaks the completeness of processed.
    await _insert_task(s, status="queued")
    await s.commit()
    snap2 = await build_status_snapshot(s, _WS)
    assert snap2.processed_count == 4
    assert snap2.processed_pct == 80.0
    assert snap2.processed_pct < 100.0


@pytest.mark.asyncio
async def test_processed_pct_never_rounds_to_100_with_pending(db_session) -> None:
    """Same cap at 99.9 as progress_pct — rounding to 1 decimal never
    shows 100% processed while a pending task remains. Since the
    bar segments scale with these same pcts, the cap also prevents
    the bar from LOOKING full with pending work."""
    s = db_session
    # 9999 completed at once (generate_series) + 1 queued → 99.99 rounds
    # unambiguously to 100.0; it must be capped at 99.9.
    await s.execute(
        text(
            """
            INSERT INTO tasks (workspace_id, command, payload, target_mode,
                               target_count, priority, status, run_at,
                               signed, signature)
            SELECT :ws, 'echo', CAST('{}' AS jsonb), 'count', 1, 0, 'completed',
                   now(), true, decode(repeat('00', 64), 'hex')
              FROM generate_series(1, 9999)
            """
        ),
        {"ws": _WS},
    )
    await _insert_task(s, status="queued")
    await s.commit()

    snap = await build_status_snapshot(s, _WS)
    assert snap.processed_count == 9999
    assert snap.processed_pct == 99.9
    assert snap.progress_pct == 99.9


@pytest.mark.asyncio
async def test_completed_outcomes_breakdown(db_session) -> None:
    """completed_outcomes exposes a breakdown of closes with an outcome.

    Seeds 1 plain completed + 2 completed with outcome='no_result' + 1
    failed. The snapshot must expose `completed_outcomes = [("no_result",
    2)]` (ordered by count DESC); this feeds a sub-badge under the chip
    "Completed" on the public status page.
    """
    s = db_session
    await _insert_task(s, status="completed")  # no outcome
    for _ in range(2):
        await _insert_task(s, status="completed", outcome="no_result")
    await _insert_task(s, status="failed")
    await s.commit()

    snap = await build_status_snapshot(s, _WS)
    assert snap.completed_count == 3
    assert snap.failed_count == 1
    # sub-badge: [("no_result", 2)]
    assert snap.completed_outcomes == [("no_result", 2)]
    # progress: 3 of 4 = 75%
    assert snap.progress_pct == 75.0


@pytest.mark.asyncio
async def test_completed_outcomes_tie_break_deterministic(db_session) -> None:
    """On count ties the sub-badge sorts by name ASC.

    The breakdown query has no ORDER BY, so the insertion order of the
    Counter (which `most_common()` uses as tie-break) depended on the order of rows
    Postgres returned. The public contract must be stable: count DESC,
    name ASC.
    """
    s = db_session
    for _ in range(3):
        await _insert_task(s, status="completed", outcome="mid_tool")
    for _ in range(2):
        await _insert_task(s, status="completed", outcome="zeta")
    for _ in range(2):
        await _insert_task(s, status="completed", outcome="alpha")
    await s.commit()

    snap = await build_status_snapshot(s, _WS)
    assert snap.completed_outcomes == [("mid_tool", 3), ("alpha", 2), ("zeta", 2)]
