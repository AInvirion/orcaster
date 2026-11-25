"""SQL shared between the admin plane (`/admin/tasks/{id}/retry`,
`cancel_task_core`) and the worker path in `task_complete` (sticky
auto-retry; cancel propagation on worker-driven completion).

These snippets are the single source of truth for the re-queue +
retarget UPDATEs. Any change here must consider BOTH callsites:

- `orcaster_server/api/admin_tasks.py::admin_retry_task` (manual operator retry).
- `orcaster_server/api/tasks.py::task_complete` (sticky auto-retry on throttling).

Defense in depth: every UPDATE carries `AND workspace_id = :workspace_id`
so that security is local to the query and does not depend on the upstream
gate. **Both callsites must pass `workspace_id`** (worker path:
`worker.workspace_id`; admin path: `ctx.workspace_id`). No observable
behaviour change: the task_id is already validated as belonging to the
workspace before reaching this point.

Preserved invariants (see the dense comments in `admin_tasks.py`):

- `_SQL_RETARGET_CLAIM_SAME_WORKER`: we pivot to claim+pool (NOT push)
  because `delivery_mode='push'` is reserved for signed control ops.
  The worker rejects push tasks with a non-reserved command with
  `exit_code=2` and "non-reserved push tasks not supported".
- `target_worker_id` stays NULL: the CHECK
  `tasks_target_worker_exclusive_chk` only allows a non-NULL
  `target_worker_id` when `target_mode='count' AND target_count=1`. Since
  the target already lives in `target_worker_pool`, we do not combine both
  mechanisms.
- `attempt_budget`: the count-mode retry bumps it by 1 via
  `_SQL_BUMP_ATTEMPT_BUDGET`, including the sticky/force_same_worker
  branches. The `taken` predicate in `/poll` counts every non-expired
  execution against `attempt_budget`, so previous terminal executions
  consume budget and the bump is what reopens exactly one slot.
  **Exception**: the `all_available` → sticky conversion of the auto-retry
  does NOT bump by +1; it re-anchors `attempt_budget` to
  `count(non-expired)+1` via `_SQL_RESYNC_ATTEMPT_BUDGET_TO_INFLIGHT`. In
  `all_available` the budget does not gate the claim, so the `expired`
  executions of the snapshot returned their slot without it being
  re-consumed; a blind bump on `attempt_budget=N` would open
  `1+count(expired)` phantom slots. The resync opens exactly one re-claim
  (see the dense comment on that statement below). `target_count` (nominal
  fan-out, success threshold of the closure) is NOT touched by any retry:
  that is why neither the bump nor the resync interacts with the CHECK
  `tasks_target_worker_exclusive_chk` (which constrains `target_count`, not
  the budget). The retarget itself touches neither; the bump/resync lives in
  the callsites (both run it after locking the row with FOR UPDATE).
"""

from __future__ import annotations

from sqlalchemy import text

from ..models import EXPIRED_EXECUTION_STATUS

# Common reset for re-queueable tasks: clears closure fields, normalizes
# scheduling. Identical to the SQL of `admin_retry_task`; we do not inflate
# target_count here, that is the caller's job when it applies.
_SQL_RESET_TASK_QUEUED = text(
    """
    UPDATE tasks
       SET status = 'queued',
           completed_at = NULL,
           cancelled_reason = NULL,
           last_error = NULL,
           run_at = now(),
           expires_at = NULL
     WHERE id = :id
       AND workspace_id = :workspace_id
    """
)

# Sticky retarget: directs the task to a single worker via
# target_worker_pool. See the doc block above for why claim+pool and NOT push.
#
# `converted_from`: the conversion to count-mode records its provenance when
# the previous mode was `all_available`. Without that mark the count-mode
# closure (which aggregates over the WHOLE history) counted the `completed`
# executions of already-closed epochs and could close a task as `completed`
# whose worker kept failing in the current epoch.
#
# The CASE is the CONDITIONAL mark required by both callsites (see the doc
# block above): in Postgres the SET expressions see the OLD values of the
# row, so `target_mode` here is the mode BEFORE the retarget.
#   - auto-retry (`tasks.py::task_complete`) on `all_available` → mark.
#   - auto-retry on native count → `converted_from` is preserved (NULL).
#   - admin retry (`admin_tasks.py::_apply_retry_locked`) → only reaches here
#     on the count-mode branch (`all_available` is re-snapshotted by another
#     branch, see `_SQL_RESET_TASK_DISPATCHING`), so it preserves the value:
#     if the task was already converted the mark survives (it stays in count
#     by conversion, the epoch is NOT bumped on that branch) and if it is
#     native count it stays NULL.
_SQL_RETARGET_CLAIM_SAME_WORKER = text(
    """
    UPDATE tasks
       SET delivery_mode = 'claim',
           target_worker_id = NULL,
           target_mode = 'count',
           converted_from = CASE
               WHEN target_mode = 'all_available' THEN 'all_available'
               ELSE converted_from
           END,
           target_worker_pool = CAST(:pool AS jsonb)
     WHERE id = :id
       AND workspace_id = :workspace_id
    """
)

# Bump attempt_budget by 1 to reopen the budget on EVERY count-mode retry,
# with or without retarget. "One retry = one more attempt".
# `target_count` (nominal) is NOT touched: it is the success threshold of the
# closure.
_SQL_BUMP_ATTEMPT_BUDGET = text(
    """
    UPDATE tasks
       SET attempt_budget = attempt_budget + 1
     WHERE id = :id
       AND workspace_id = :workspace_id
    """
)

# Re-anchors `attempt_budget` to the in-flight reality: count(non-expired)+1.
# EXCLUSIVE to the `all_available` → sticky conversion. Why a resync and NOT
# the blind +1 bump of native count-mode:
#
#   In `all_available` the `attempt_budget` does NOT gate the claim (the
#   `_sql_budget_available` predicate in poll.py only applies to the
#   count-mode claim); the N workers of the snapshot run in parallel. So,
#   when K executions of the snapshot end `expired` (worker crash), they
#   return their slot WITHOUT anyone re-consuming it, something impossible in
#   native count-mode, where the budget gates every claim and the freed slot
#   is taken by the next claim. The snapshot left `attempt_budget = N`.
#
#   On converting to sticky (count-mode, pool=[worker]), a blind +1 bump would
#   give `attempt_budget = N+1` with `count(non-expired) = N-K`, i.e. a margin
#   of `1+K` slots: 1 legitimate (the re-claim by the pinned worker) + K
#   PHANTOM slots that other workers take when the `_relax_sticky_pool`
#   housekeeping opens the pool. Re-anchoring to `count(non-expired)+1` opens
#   EXACTLY one re-claim no matter how many expired. The +1 bump of NATIVE
#   count-mode is left alone: there the budget gates every claim, so the +1
#   reopens exactly the slot consumed by the terminal execution that
#   motivates the retry.
#
# `target_count` (nominal, success threshold) is NOT touched, same as in the
# bump.
_SQL_RESYNC_ATTEMPT_BUDGET_TO_INFLIGHT = text(
    f"""
    UPDATE tasks
       SET attempt_budget = (
           SELECT count(*) FROM task_executions ee
            WHERE ee.task_id = tasks.id
              AND ee.status <> '{EXPIRED_EXECUTION_STATUS}'
       ) + 1
     WHERE id = :id
       AND workspace_id = :workspace_id
    """
)

# Cancel propagation to active executions:
# every 'assigned'/'claimed'/'running' execution of the task becomes
# 'cancelled'. Shared by `cancel_task_core` (admin cancel, `admin_tasks.py`)
# and `task_complete` (worker-driven closure, `tasks.py`, only if the closure
# actually flipped the task).
# It does NOT clear `lease_token`: `_SQL_POLL_CANCEL_SIGNALS` filters on
# `lease_token IS NOT NULL` and needs it to deliver the kill exactly once on
# the affected worker's next poll.
# No workspace filter: `task_executions` has no workspace_id; both callsites
# already validated the task against the workspace before reaching this
# point (admin: scoped `_SQL_CANCEL_TASK`; worker: lease ownership in
# `_SQL_COMPLETE`).
_SQL_CANCEL_EXECUTIONS = text(
    """
    UPDATE task_executions
       SET status = 'cancelled', finished_at = now()
     WHERE task_id = :id
       AND status IN ('assigned', 'claimed', 'running')
    """
)
