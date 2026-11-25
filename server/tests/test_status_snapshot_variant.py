"""Variant lens filter on the PUBLIC status page snapshot.

The variant rescales the snapshot's TASK AGGREGATES (progress, tasks_by_status
and therefore Queued/Failed/completed), with the same vocabulary as the admin
(current/all/other/`kind`/`kind~label`, sep `~`). WORKERS and "Active now" stay
GLOBAL: they are fleet metrics, not per-version (same line as the admin, which does not
rescale the worker pane). Seeding: pattern of test_status_snapshot.py, but explicitly
populating `variant_kind`/`variant_label` (the app writes those columns when
creating the task, not a trigger; the snapshot READS them directly).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from orcaster_server.status.snapshot import build_status_snapshot
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

_WS = DEFAULT_WORKSPACE_ID

# Variant labels used by the tests (synthetic kind and labels).
_LATEST = "0.7.5 / ex3.13.0"
_OLD = "0.5.9 / ex3.10.1"
_KIND = "example-tool"


async def _insert_task(
    session,
    *,
    status: str,
    kind: str | None = None,
    label: str | None = None,
    outcome: str | None = None,
) -> int:
    row = await session.execute(
        text(
            """
            INSERT INTO tasks (workspace_id, command, payload, target_mode,
                               target_count, priority, status, outcome, run_at,
                               signed, signature, variant_kind, variant_label)
            VALUES (:ws, 'echo', CAST(:p AS jsonb), 'count', 1, 0, :status, :outcome,
                    now(), true, decode(repeat('00', 64), 'hex'),
                    :kind, :label)
            RETURNING id
            """
        ),
        {
            "ws": _WS,
            "p": json.dumps({}),
            "status": status,
            "outcome": outcome,
            "kind": kind,
            "label": label,
        },
    )
    return int(row.scalar_one())


async def _seed_three_variants(session) -> None:
    """Seed 3 variants: latest (0.7.5), old (0.5.9) and other (NULL).

    Global:  completed=6 failed=3 queued=2  (non_cancelled=11)
    current (latest + other): completed=3 failed=1 queued=2  (non_cancelled=6)
    pair old: completed=3 failed=2  (non_cancelled=5)
    other:    completed=1 queued=1  (non_cancelled=2)
    """
    for _ in range(2):
        await _insert_task(session, status="completed", kind=_KIND, label=_LATEST)
    await _insert_task(session, status="failed", kind=_KIND, label=_LATEST)
    await _insert_task(session, status="queued", kind=_KIND, label=_LATEST)

    for _ in range(3):
        await _insert_task(session, status="completed", kind=_KIND, label=_OLD)
    for _ in range(2):
        await _insert_task(session, status="failed", kind=_KIND, label=_OLD)

    await _insert_task(session, status="completed")  # other (NULL)
    await _insert_task(session, status="queued")  # other (NULL)
    await session.commit()


@pytest.mark.asyncio
async def test_default_is_current_rescales_to_latest_plus_other(db_session) -> None:
    """No param → current = latest of each kind (0.7.5) + other. NOT the old 0.5.9."""
    s = db_session
    await _seed_three_variants(s)

    snap = await build_status_snapshot(s, _WS)  # default = current

    # current = 0.7.5 (completed 2, failed 1, queued 1) + other (completed 1, queued 1).
    assert snap.tasks_by_status["completed"] == 3
    assert snap.tasks_by_status["failed"] == 1
    assert snap.tasks_by_status["queued"] == 2
    assert snap.completed_count == 3
    assert snap.failed_count == 1
    assert snap.non_cancelled_total == 6
    assert snap.progress_pct == 50.0
    assert snap.selected_variant == "current"


@pytest.mark.asyncio
async def test_variant_all_shows_global(db_session) -> None:
    s = db_session
    await _seed_three_variants(s)

    snap = await build_status_snapshot(s, _WS, "all")

    assert snap.tasks_by_status["completed"] == 6
    assert snap.tasks_by_status["failed"] == 3
    assert snap.tasks_by_status["queued"] == 2
    assert snap.non_cancelled_total == 11
    assert snap.progress_pct == round(100 * 6 / 11, 1)
    assert snap.selected_variant == "all"


@pytest.mark.asyncio
async def test_variant_pair_exact(db_session) -> None:
    s = db_session
    await _seed_three_variants(s)

    snap = await build_status_snapshot(s, _WS, f"{_KIND}~{_OLD}")

    assert snap.tasks_by_status["completed"] == 3
    assert snap.tasks_by_status["failed"] == 2
    assert snap.tasks_by_status["queued"] == 0
    assert snap.non_cancelled_total == 5
    assert snap.progress_pct == 60.0
    assert snap.selected_variant == f"{_KIND}~{_OLD}"


@pytest.mark.asyncio
async def test_processed_rescales_under_variant_lens(db_session) -> None:
    """`processed_count` and the segment pcts rescale with the lens.

    They derive from the same filtered grid as tasks_by_status, so the
    rescaling is automatic; this test pins it as a contract.
    """
    s = db_session
    await _seed_three_variants(s)

    # current: completed 3, failed 1, denom 6 → processed 4.
    snap = await build_status_snapshot(s, _WS)
    assert snap.processed_count == 4
    assert snap.processed_pct == round(100 * 4 / 6, 1)

    # old pair: completed 3, failed 2, denom 5 → everything processed (100.0).
    snap_old = await build_status_snapshot(s, _WS, f"{_KIND}~{_OLD}")
    assert snap_old.processed_count == 5
    assert snap_old.progress_pct == 60.0
    assert snap_old.processed_pct == 100.0


@pytest.mark.asyncio
async def test_variant_other_only(db_session) -> None:
    s = db_session
    await _seed_three_variants(s)

    snap = await build_status_snapshot(s, _WS, "other")

    assert snap.tasks_by_status["completed"] == 1
    assert snap.tasks_by_status["queued"] == 1
    assert snap.tasks_by_status["failed"] == 0
    assert snap.non_cancelled_total == 2
    assert snap.selected_variant == "other"


@pytest.mark.asyncio
async def test_workers_and_active_now_stay_global_across_variants(db_session) -> None:
    """The variant does NOT rescale fleet or "Active now": they are global metrics."""
    s = db_session
    # One online worker + an active execution on a task of the OLD variant.
    w = (
        await s.execute(
            text(
                """
                INSERT INTO workers (workspace_id, api_key_hash, hostname, os, arch,
                                     status, registered_at, last_heartbeat_at)
                VALUES (:ws, 'snap-var-w', 'w-var', 'linux', 'x86_64', 'online',
                        now(), now())
                RETURNING id::text
                """
            ),
            {"ws": _WS},
        )
    ).scalar_one()
    await _seed_three_variants(s)
    t_old = await _insert_task(s, status="dispatching", kind=_KIND, label=_OLD)
    await s.execute(
        text(
            """
            INSERT INTO task_executions (task_id, worker_id, status,
                                         attempt_index, assigned_at, last_heartbeat_at)
            VALUES (:t, :w, 'running', 1, now(), now())
            """
        ),
        {"t": t_old, "w": w},
    )
    await s.commit()

    # The "current" lens (0.7.5) does NOT include the old running task, but
    # workers_online and active_executions_now must remain global (1).
    snap_current = await build_status_snapshot(s, _WS)  # current
    snap_all = await build_status_snapshot(s, _WS, "all")
    for snap in (snap_current, snap_all):
        assert snap.workers_online == 1
        assert snap.active_executions_now == 1


@pytest.mark.asyncio
async def test_variant_options_structure(db_session) -> None:
    s = db_session
    await _seed_three_variants(s)

    snap = await build_status_snapshot(s, _WS)  # current default
    opts = snap.variant_options

    assert opts["is_current_default"] is True
    assert opts["has_other"] is True
    assert opts["selected_token"] == "current"
    assert len(opts["tools"]) == 1
    tool = opts["tools"][0]
    assert tool["kind"] == _KIND
    # Versions desc by semver: latest first, marked is_current.
    labels = [v["label"] for v in tool["versions"]]
    assert labels == [_LATEST, _OLD]
    latest_ver = tool["versions"][0]
    assert latest_ver["is_current"] is True
    assert latest_ver["count"] == 4  # 2 completed + 1 failed + 1 queued
    old_ver = tool["versions"][1]
    assert old_ver["is_current"] is False
    assert old_ver["count"] == 5  # 3 completed + 2 failed


@pytest.mark.asyncio
async def test_empty_workspace_current_is_safe(db_session) -> None:
    """With no tasks, the default current does not blow up and invents no variants."""
    snap = await build_status_snapshot(db_session, _WS)
    assert snap.tasks_by_status["completed"] == 0
    assert snap.non_cancelled_total == 0
    assert snap.progress_pct == 0
    assert snap.variant_options["tools"] == []
    assert snap.variant_options["has_other"] is False
    assert snap.selected_variant == "current"


@pytest.mark.asyncio
async def test_completed_outcomes_rescales_by_variant(db_session) -> None:
    """completed_outcomes rescales to the variant lens.

    Seeds completions with an outcome in TWO variants (latest + old);
    asserts that completed_outcomes changes between variant=current (latest only)
    and variant=all (latest + old). The accumulator lives inside the guard
    `variant_row_matches`, so the rescaling is automatic.
    """
    s = db_session

    # Latest variant: 2 completed with outcome='no_result'
    for _ in range(2):
        await _insert_task(
            s,
            status="completed",
            kind=_KIND,
            label=_LATEST,
            outcome="no_result",
        )

    # Old variant: 1 completed with outcome='no_result' + 1 completed with outcome='timeout'
    await _insert_task(s, status="completed", kind=_KIND, label=_OLD, outcome="no_result")
    await _insert_task(s, status="completed", kind=_KIND, label=_OLD, outcome="timeout")

    # Other (no variant): 1 completed with outcome='no_result'
    await _insert_task(s, status="completed", outcome="no_result")

    await s.commit()

    # variant=current (default): latest + other
    snap_current = await build_status_snapshot(s, _WS)
    # latest: 2 no_result, other: 1 no_result -> total 3 no_result
    assert snap_current.completed_outcomes == [("no_result", 3)]

    # variant=all: latest + old + other
    snap_all = await build_status_snapshot(s, _WS, variant_param="all")
    # latest: 2 no_result, old: 1 no_result + 1 timeout, other: 1 no_result
    # -> total: 4 no_result, 1 timeout (DESC order by count)
    assert snap_all.completed_outcomes == [("no_result", 4), ("timeout", 1)]

    # variant=old (pair selector): old only
    snap_old = await build_status_snapshot(s, _WS, variant_param=f"{_KIND}~{_OLD}")
    # old: 1 no_result + 1 timeout
    assert snap_old.completed_outcomes == [("no_result", 1), ("timeout", 1)]
