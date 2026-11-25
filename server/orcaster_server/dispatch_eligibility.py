"""Gates of the `all_available` snapshot and why a snapshot comes out empty.

The three gates that decide whether a worker enters the snapshot of an
`all_available` task, and the reason reported when none passes, are defined in
this single module. The reason distinguishes "no worker is online" from "workers
are online and none declares the capability" (`no_eligible_workers` is only the
generic fallback).

The gates
---------

A worker enters the snapshot when it passes all three, in this order (which is
also the order in which the reason reports them):

1. **capabilities**: `workers.capabilities` satisfies
   `tasks.eligibility -> 'capabilities'`.
2. **disk**: the worker is not gated by free disk.
3. **secrets version floor**: if the task uses stored secrets, the worker must
   support `secrets` in the poll.

Each gate is built from a SQL expression that locates the task's columns
(`TaskRef`), not from a fixed bind: the creation snapshot reads them with a
subquery by `:task_id`, and the housekeeping re-snapshot reads them from the CTE
row it already has at hand. A single SQL text per gate serves both, and that is
the point, because the classifier below decides the reason by counting workers
with these same predicates. Two copies would drift and the reported reason would
stop describing the snapshot that was actually taken.

The reason
----------

`classify_dispatch_block()` counts the workers that survive each gate and
returns the **first** one that drops to zero. It is a measurement of the instant
at which it is called, not a stored datum: a persisted reason ages badly. A task
that was born with no workers online and today has three without the capability
would still say "no workers online". That is why the UI recomputes it on every
render and the creation response returns it measured at that moment.

`no_eligible_workers` remains the **generic** reason: it is returned when the
three gates pass and the snapshot came out empty anyway (today only the
`NOT EXISTS` defense, which excludes a worker with an active execution of that
same task). Explicit before plausible: a gate that was not the one that failed is
not reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.eligibility import capability_match_sql
from orcaster_server.task_secrets.gate import (
    payload_uses_stored_secrets_sql,
    worker_supports_task_secrets_sql,
)


@dataclass(frozen=True)
class TaskRef:
    """SQL expressions that locate the task's columns in a statement.

    `capabilities` evaluates to `eligibility -> 'capabilities'` (NULL if the task
    does not declare the key), `payload` to the JSONB `payload` and `workspace_id`
    to the task's workspace. They are interpolated at build time from literals in
    the code; they never carry user input.
    """

    capabilities: str
    payload: str
    workspace_id: str


#: The task is read by `:task_id` with subqueries. This is the form used by the
#: two snapshots of creation/retry, which only have the id at hand.
TASK_BY_ID: Final = TaskRef(
    capabilities="SELECT eligibility -> 'capabilities' FROM tasks WHERE id = :task_id",
    payload="SELECT payload FROM tasks WHERE id = :task_id",
    workspace_id=":workspace_id",
)


def task_ref_for_alias(alias: str) -> TaskRef:
    """`TaskRef` for a statement that already has the task row at hand.

    Used by the housekeeping re-snapshot, which walks several tasks in a
    single statement and therefore cannot read them with a fixed `:task_id`.
    """
    return TaskRef(
        capabilities=f"{alias}.eligibility -> 'capabilities'",
        payload=f"{alias}.payload",
        workspace_id=f"{alias}.workspace_id",
    )


def capability_gate_sql(task: TaskRef) -> str:
    """Gate 1: worker `w` satisfies `eligibility.capabilities`."""
    return capability_match_sql(requirements=task.capabilities, capabilities="w.capabilities")


def disk_gate_sql(min_free_fraction: str = ":claim_min_free_fraction") -> str:
    """Gate 2: worker `w` is not gated by disk.

    Do not pre-assign to a gated worker: step 5 of the pick only turns the
    `assigned` row into `claimed` with `status='idle'`, but a gated worker
    withholds tasks (poll handler guard), so that pre-assignment would stay
    `assigned`, stuck, holding budget. NULL = "unknown" = IS included
    (compatibility with older workers that do not report the fraction).
    """
    return f"NOT (w.free_disk_fraction IS NOT NULL AND w.free_disk_fraction < {min_free_fraction})"


def stored_secrets_gate_sql(task: TaskRef) -> str:
    """Gate 3: worker `w` would receive the stored secrets.

    A worker below the version floor does not enter the snapshot of a task that
    uses stored secrets. The poll would never deliver that pre-assignment to it
    (it ignores `secrets`), and the `assigned` row is only expired by the
    housekeeping, which does not recompute the task state: it would stay
    `dispatching` forever even after the capable workers finish.
    """
    return (
        "NOT ("
        + payload_uses_stored_secrets_sql(
            payload_ref=task.payload,
            workspace_ref=task.workspace_id,
        )
        + " AND NOT "
        + worker_supports_task_secrets_sql("w.version")
        + ")"
    )


def worker_is_eligible_sql(
    task: TaskRef, *, min_free_fraction: str = ":claim_min_free_fraction"
) -> str:
    """The three gates, in conjunction. What "eligible worker" means."""
    return (
        "("
        + capability_gate_sql(task)
        + " AND "
        + disk_gate_sql(min_free_fraction)
        + " AND "
        + stored_secrets_gate_sql(task)
        + ")"
    )


# After materializing a snapshot, the nominal count and the budget are
# re-synchronized to its size (they are born equal; the budget only drifts through
# count-mode retries, and a re-snapshot is a new fan-out, not a retry). Used by
# creation, retry (`admin_tasks.py`) and the housekeeping re-snapshot.
SQL_SYNC_TARGET_COUNT_TO_SNAPSHOT: Final = text(
    """
    UPDATE tasks
       SET target_count = GREATEST(:n, 0),
           attempt_budget = GREATEST(:n, 1)
     WHERE id = :task_id
    """
)


# --- Reason for an empty snapshot --------------------------------------------

#: No worker in the workspace (or in the pool, with `all:<ids>`) is `online`.
NO_WORKERS_ONLINE: Final = "no_workers_online"
#: Workers are online, but none satisfies `eligibility.capabilities`.
NO_WORKERS_WITH_CAPABILITIES: Final = "no_workers_with_capabilities"
#: The eligible workers are gated by free disk.
WORKERS_LOW_ON_DISK: Final = "workers_low_on_disk"
#: The eligible workers are below the secrets version floor.
WORKERS_BELOW_SECRETS_VERSION: Final = "workers_below_secrets_version"
#: Generic: the three gates pass and the snapshot came out empty anyway.
NO_ELIGIBLE_WORKERS: Final = "no_eligible_workers"

BLOCK_REASONS: Final = (
    NO_WORKERS_ONLINE,
    NO_WORKERS_WITH_CAPABILITIES,
    WORKERS_LOW_ON_DISK,
    WORKERS_BELOW_SECRETS_VERSION,
    NO_ELIGIBLE_WORKERS,
)

# Text for the operator. UI rule of the repo: English, short, and every line
# ends with what to do: a notice without an action is a silent pass with a
# sentence on top. Single source: consumed by the admin UI and by the
# `last_error` left on the task closed by `all:<ids>`.
BLOCK_REASON_TEXT: Final[dict[str, str]] = {
    NO_WORKERS_ONLINE: (
        "No worker is online. Bring one online (or resume a paused one) "
        "and it will pick this task up; cancel the task if nobody will."
    ),
    NO_WORKERS_WITH_CAPABILITIES: (
        "Workers are online, but none declares the capabilities this task "
        "requires. Start a worker with ORCH_CAPABILITIES covering them, or "
        "cancel the task."
    ),
    WORKERS_LOW_ON_DISK: (
        "The workers that match this task are held back by free disk. "
        "Free disk space on them, or cancel the task."
    ),
    WORKERS_BELOW_SECRETS_VERSION: (
        "This task uses stored secrets and every matching worker is below the "
        "version that receives them. Upgrade a worker, or cancel the task."
    ),
    NO_ELIGIBLE_WORKERS: (
        "No worker is eligible for this task right now. Check the workers "
        "page; an earlier attempt of this task may still be active on them."
    ),
}


def block_reason_text(reason: str) -> str:
    """Sentence for the operator. An unknown reason is not swallowed silently."""
    return BLOCK_REASON_TEXT.get(
        reason, f"No worker is eligible for this task right now ({reason})."
    )


# Count per gate, in the SAME order in which the reason reports them. Each
# column is the previous one plus a gate, so the first one that is 0 is the gate
# that left the snapshot empty. A single scan of `workers`.
#
# `:pool` NULL = open snapshot (the whole workspace); a list of UUIDs =
# `all:<ids>`, and then "online" is also measured inside the pool: what the
# operator needs to know is that the workers they named are not online, not
# that the workspace had others.
_SQL_COUNT_BY_GATE = text(
    f"""
    SELECT count(*) AS online,
           count(*) FILTER (WHERE {capability_gate_sql(TASK_BY_ID)}) AS capable,
           count(*) FILTER (WHERE {capability_gate_sql(TASK_BY_ID)}
                              AND {disk_gate_sql()}) AS capable_with_disk,
           count(*) FILTER (WHERE {worker_is_eligible_sql(TASK_BY_ID)}) AS eligible
      FROM workers w
     WHERE w.status = 'online'
       AND w.deleted_at IS NULL
       AND w.workspace_id = :workspace_id
       AND (CAST(:pool AS uuid[]) IS NULL OR w.id = ANY(CAST(:pool AS uuid[])))
    """
)


async def classify_dispatch_block(
    session: AsyncSession,
    *,
    task_id: int,
    workspace_id: str,
    pool: list[str] | None,
    min_free_fraction: float,
) -> str:
    """Why there is no eligible worker for this task, measured NOW.

    Returns one of `BLOCK_REASONS`. Calling it only makes sense when the
    snapshot came out empty: with eligible workers it returns the generic
    `no_eligible_workers`, which then means nothing.
    """
    row = (
        await session.execute(
            _SQL_COUNT_BY_GATE,
            {
                "task_id": task_id,
                "workspace_id": workspace_id,
                "pool": pool,
                "claim_min_free_fraction": min_free_fraction,
            },
        )
    ).one()
    online, capable, capable_with_disk, eligible = (int(v) for v in row)
    if online == 0:
        return NO_WORKERS_ONLINE
    if capable == 0:
        return NO_WORKERS_WITH_CAPABILITIES
    if capable_with_disk == 0:
        return WORKERS_LOW_ON_DISK
    if eligible == 0:
        return WORKERS_BELOW_SECRETS_VERSION
    return NO_ELIGIBLE_WORKERS
