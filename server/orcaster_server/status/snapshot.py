"""Aggregation service of the public status page (application layer).

`build_status_snapshot(session, workspace_id)` returns a read-only snapshot in a
single read-only transaction. It knows nothing about HTTP or tokens; it receives
the already resolved `workspace_id`. It is the unit testable without HTTP.

Anonymity: the workers query selects ONLY `status` and the active load, never
hostname/name/os/arch/version/cpu/ram/disk or the UUID. The "Worker N" index is
assigned in Python by `registered_at` ASC order (stable: a new worker goes last
and does not reorder the previous ones).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.models import TASK_STATUS_VALUES
from orcaster_server.task_variant import (
    accumulate_variant_grid,
    build_variant_options,
    resolve_variant_filter,
    variant_row_matches,
    variant_token,
)

# Live workers of the workspace + their active load. ONLY non-sensitive columns.
# Ordered by registered_at ASC so the "Worker N" index is stable.
_SQL_FLEET = text(
    """
    SELECT w.status AS status,
           COALESCE(a.active_count, 0) AS active_count
      FROM workers w
      LEFT JOIN (
           SELECT te.worker_id, COUNT(*) AS active_count
             FROM task_executions te
             JOIN tasks t ON t.id = te.task_id
            WHERE te.status IN ('assigned','claimed','running')
              AND t.workspace_id = :workspace_id
            GROUP BY te.worker_id
      ) a ON a.worker_id = w.id
     WHERE w.deleted_at IS NULL
       AND w.workspace_id = :workspace_id
     ORDER BY w.registered_at ASC
    """
)

# Breakdown by (variant, status): a single scan feeds the variant lens filter.
# EVERYTHING is derived in Python: the variants present (for the selector), the
# "current" set (latest of each kind + other) and the status breakdown RE-SCALED to
# the selected variant (default = current). Small cardinality (~variants x
# statuses). It is the public equivalent of the admin's
# `_SQL_COUNT_TASKS_BY_VARIANT_STATUS`; it is defined here to keep the public plane
# decoupled from the admin router. The variant re-scales the TASK AGGREGATES; the
# worker fleet and "Active now" stay GLOBAL (fleet metrics, not version metrics;
# same line as the admin, which does not re-scale the worker pane).
_SQL_TASKS_BY_VARIANT_STATUS = text(
    "SELECT variant_kind, variant_label, status::text AS status, outcome, COUNT(*) AS n "
    "FROM tasks WHERE workspace_id = :workspace_id "
    "GROUP BY variant_kind, variant_label, status, outcome"
)

# Active executions of the workspace (scoped via a join to tasks).
_SQL_ACTIVE_EXECUTIONS = text(
    """
    SELECT COUNT(*)
      FROM task_executions te
      JOIN tasks t ON t.id = te.task_id
     WHERE t.workspace_id = :workspace_id
       AND te.status IN ('assigned','claimed','running')
    """
)

_SQL_WORKSPACE_NAME = text("SELECT name FROM workspaces WHERE id = :workspace_id")


@dataclass(frozen=True)
class WorkerEntry:
    index: int  # 1-based, stable by registered_at ASC
    status: str  # online | paused | offline
    active_count: int


@dataclass(frozen=True)
class StatusSnapshot:
    workspace_name: str
    workers_online: int
    workers_paused: int
    workers_offline: int
    workers: list[WorkerEntry]
    tasks_by_status: dict[str, int]
    completed_count: int
    # Public presentation value: `tasks_by_status['failed']`. A close with
    # a nuance keeps it in `outcome` on a real `completed`, so it counts as
    # a success for public progress.
    failed_count: int
    non_cancelled_total: int
    progress_pct: float  # 0-100, 1 decimal; 100.0 ONLY when everything is completed
    active_executions_now: int
    # raw numerator/denominator for "X of Y tasks completed".
    progress_numerator: int = field(default=0)
    progress_denominator: int = field(default=0)
    # Variant lens filter: structure of the hierarchical selector (the view only
    # renders it) and canonical token of the selected variant, to preserve the
    # lens in the HTMX poll and the GET form.
    variant_options: dict = field(default_factory=dict)
    selected_variant: str = "current"
    # Breakdown of closes with an outcome under "Completed" on the public
    # status page. List of (outcome_name, count) sorted by count DESC; an
    # anonymous sub-badge in _status_summary.html, without JS.
    completed_outcomes: list[tuple[str, int]] = field(default_factory=list)
    # Hero "P of Y tasks processed". processed = completed + failed
    # (terminal; cancelled stays out of the denominator). processed_pct shares the
    # semantics of progress_pct (exactly 100.0 only with nothing pending, capped at
    # 99.9). The segments of the stacked bar scale with these TWO capped pcts
    # (completed->progress_pct, failed underlay->processed_pct): an uncapped pct
    # would round 9999/10000 to 100.0 and the bar would LOOK full with pending work.
    processed_count: int = field(default=0)
    processed_pct: float = field(default=0.0)


@dataclass(frozen=True)
class Progress:
    """Aggregated progress of a status breakdown.

    Single source of the progress bar: the public status page (via
    `StatusSnapshot`) and the admin dashboard (`_progress_hero.html`) consume it.
    The field names match those of `StatusSnapshot` so that the partial accepts
    either object.
    """

    completed_count: int
    # Direct value of `tasks_by_status['failed']`; a close with
    # outcome='no_result' is a real 'completed' and counts as a success.
    failed_count: int
    # processed = completed + failed (terminal; cancelled excluded).
    processed_count: int
    # Denominator: total without cancelled.
    progress_denominator: int
    # 0-100, 1 decimal; 100.0 ONLY when no non-cancelled work remains.
    progress_pct: float
    processed_pct: float


def compute_progress(tasks_by_status: Mapping[str, int]) -> Progress:
    """Derives the progress of a status breakdown already re-scaled to the variant
    lens. Missing statuses count as 0."""
    completed_count = tasks_by_status.get("completed", 0)
    failed_count = tasks_by_status.get("failed", 0)
    cancelled_count = tasks_by_status.get("cancelled", 0)
    non_cancelled_total = sum(tasks_by_status.values()) - cancelled_count
    processed_count = completed_count + failed_count

    def _pct_capped(numerator: int) -> float:
        """0-100 with 1 decimal; exactly 100.0 ONLY when no non-cancelled work
        remains (capped at 99.9 so rounding never shows 100% with pending
        tasks, which would mislead the client)."""
        if not non_cancelled_total:
            return 0.0
        if numerator >= non_cancelled_total:
            return 100.0
        return min(99.9, round(100 * numerator / non_cancelled_total, 1))

    return Progress(
        completed_count=completed_count,
        failed_count=failed_count,
        processed_count=processed_count,
        progress_denominator=non_cancelled_total,
        progress_pct=_pct_capped(completed_count),
        processed_pct=_pct_capped(processed_count),
    )


async def build_status_snapshot(
    session: AsyncSession, workspace_id: str, variant_param: str = ""
) -> StatusSnapshot:
    name_row = (
        await session.execute(_SQL_WORKSPACE_NAME, {"workspace_id": workspace_id})
    ).scalar_one_or_none()
    workspace_name = name_row if name_row is not None else ""

    fleet_rows = (
        (await session.execute(_SQL_FLEET, {"workspace_id": workspace_id})).mappings().all()
    )
    workers = [
        WorkerEntry(index=i, status=r["status"], active_count=int(r["active_count"]))
        for i, r in enumerate(fleet_rows, start=1)
    ]
    workers_online = sum(1 for w in workers if w.status == "online")
    workers_paused = sum(1 for w in workers if w.status == "paused")
    workers_offline = sum(1 for w in workers if w.status == "offline")

    # A single scan of the breakdown by (variant, status). EVERYTHING is derived
    # from the in-memory grid: variants present (selector), "current" set and the
    # breakdown re-scaled to the selected variant (default = current).
    variant_rows = (
        await session.execute(_SQL_TASKS_BY_VARIANT_STATUS, {"workspace_id": workspace_id})
    ).all()
    present_counts, variant_grid = accumulate_variant_grid(variant_rows)
    vf = resolve_variant_filter(variant_param, present_counts.keys(), default_mode="current")

    # tasks_by_status re-scaled to the lens: we sum the grid rows whose
    # (kind, label) falls within the filter (default current = latest of each kind +
    # other). completed/failed/progress are derived from here, so they re-scale on
    # their own.
    counts: dict[str, int] = {}
    completed_outcome_counter: Counter = Counter()
    for vk, vl, st, oc, n in variant_grid:
        if variant_row_matches(vf, vk, vl):
            counts[st] = counts.get(st, 0) + n
            if st == "completed" and oc:
                completed_outcome_counter[oc] += n
    tasks_by_status = {s: counts.get(s, 0) for s in TASK_STATUS_VALUES}

    progress = compute_progress(tasks_by_status)

    active_executions_now = (
        await session.execute(_SQL_ACTIVE_EXECUTIONS, {"workspace_id": workspace_id})
    ).scalar_one()

    return StatusSnapshot(
        workspace_name=workspace_name,
        workers_online=workers_online,
        workers_paused=workers_paused,
        workers_offline=workers_offline,
        workers=workers,
        tasks_by_status=tasks_by_status,
        completed_count=progress.completed_count,
        failed_count=progress.failed_count,
        non_cancelled_total=progress.progress_denominator,
        progress_pct=progress.progress_pct,
        active_executions_now=int(active_executions_now),
        progress_numerator=progress.completed_count,
        progress_denominator=progress.progress_denominator,
        variant_options=build_variant_options(present_counts, vf),
        selected_variant=variant_token(vf),
        # Stable order: count DESC, name ASC. `most_common()` breaks ties by
        # insertion order, which without an ORDER BY in the query depends on the
        # Postgres row order, which is not deterministic for the public plane.
        completed_outcomes=sorted(
            completed_outcome_counter.items(), key=lambda kv: (-kv[1], kv[0])
        ),
        processed_count=progress.processed_count,
        processed_pct=progress.processed_pct,
    )
