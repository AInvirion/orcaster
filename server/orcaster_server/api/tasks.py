"""Endpoints `/v1/tasks/*`.

There is no `POST /v1/tasks/claim`: claiming lives in
`POST /v1/workers/{id}/poll`. There is no explicit heartbeat endpoint either;
`/poll` carries only the cancel signal (`cancel_signals`), and lease renewal of
an execution in progress has no implemented replacement (see
`schemas/poll.py`). This router only offers output/complete/artifacts.

Output/complete use `lease_token` as proof of ownership of the execution.
Any mismatch (different worker, stale token, terminal status) returns
`'expired'`/409 without revealing the cause.
"""

from __future__ import annotations

import json
import logging
import os
from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server import artifacts_store, storage
from orcaster_server.api._retry_internals import (
    _SQL_BUMP_ATTEMPT_BUDGET,
    _SQL_CANCEL_EXECUTIONS,
    _SQL_RESET_TASK_QUEUED,
    _SQL_RESYNC_ATTEMPT_BUDGET_TO_INFLIGHT,
    _SQL_RETARGET_CLAIM_SAME_WORKER,
)
from orcaster_server.artifacts_store import (
    ArtifactTooLarge,
    InvalidRelPath,
    NoSpaceLeft,
    StagedArtifact,
    commit_staged,
    discard_staged,
    validate_rel_path,
)
from orcaster_server.audit import record_admin_action
from orcaster_server.config import settings as app_settings
from orcaster_server.db import get_session
from orcaster_server.models import (
    TERMINAL_EXECUTION_STATUSES,
    TERMINAL_NON_EXPIRED_EXECUTION_STATUSES,
    Worker,
    sql_status_set,
)
from orcaster_server.schemas.tasks import (
    AutoRetryPolicy,
    TaskArtifactOut,
    TaskArtifactsUploadResponse,
    TaskCompleteRequest,
    TaskCompleteResponse,
    TaskOutputRequest,
)
from orcaster_server.sse_notify import dashboard_notifier
from orcaster_server.task_outcomes import classify_terminal
from orcaster_server.worker_auth import require_worker_by_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
WorkerDep = Annotated[Worker, Depends(require_worker_by_key)]


# --- Heartbeat (no dedicated endpoint) -------------------------------------
#
# There is no `POST /v1/tasks/{id}/heartbeat`. Worker presence, the claim and
# the cancel signals travel in `POST /v1/workers/{id}/poll`, which acts as an
# implicit heartbeat.
#
# `running` state: PRODUCED by `/poll`. When the worker reports `status='busy'`
# with its `lease_token`, `_SQL_POLL_RENEW_LEASE` (orcaster_server/api/poll.py)
# makes the `claimed->running` transition and renews the lease. An execution
# goes `assigned->claimed->running->terminal`. `started_at` is set at claim time
# (the three INSERTs in poll.py). Output streaming (POST /output) does NOT change
# state: it only inserts events into `output_events`, the single source of truth
# for output.


# --- Output (events) -------------------------------------------------------
#
# Full capture: output travels as a batch of events
# `{seq, ts, stream, text}` inserted into `output_events`, instead of
# concatenating text into `task_executions.stdout/stderr` (which would be
# O(n^2): every flush would rewrite the whole row).
#
# Reliable streaming: the resolving SELECT is separate from the INSERT. If the
# lease does not match -> 409, so the worker can abort its backlog instead of
# retrying forever (see _SQL_RESOLVE_EXECUTION below).
#
# The batch is inserted via executemany: SQLAlchemy runs this statement once
# per event of the batch (params list below), not as a single INSERT with
# `unnest`. The total cost stays O(n) in the number of events (each flush is a
# small batch, ~8 KiB). `ts` is UTC (assigned by the worker in read order).

# The lease is resolved ONCE per request (not per event): reliable streaming
# requires telling "batch persisted" (202) apart from "invalid lease" (409) so
# the worker knows when to abort its backlog. Same validation as /complete.
# Accepted race: between the SELECT and the INSERT the execution may become
# terminal through another request; the events are inserted anyway, since it is
# real output of an attempt that just finished (FK + UNIQUE protect).
_SQL_RESOLVE_EXECUTION = text(
    """
    SELECT e.id
      FROM task_executions e
     WHERE e.task_id = :task_id
       AND e.worker_id = :worker_id
       AND e.lease_token = :lease_token
       AND e.status IN ('claimed', 'running')
    """
)

# Batch via executemany (one statement per event of the batch, O(n) cost);
# exact idempotency through UNIQUE(execution_id, seq).
_SQL_INSERT_OUTPUT_EVENT = text(
    """
    INSERT INTO output_events (execution_id, seq, ts, stream, text)
    VALUES (:execution_id, :seq, :ts, :stream, :text)
    ON CONFLICT (execution_id, seq) DO NOTHING
    """
)


@router.post(
    "/{task_id}/output",
    status_code=status.HTTP_202_ACCEPTED,
)
async def task_output(
    task_id: int,
    body: TaskOutputRequest,
    worker: WorkerDep,
    session: SessionDep,
) -> Response:
    row = (
        await session.execute(
            _SQL_RESOLVE_EXECUTION,
            {
                "task_id": task_id,
                "worker_id": worker.id,
                "lease_token": str(body.lease_token),
            },
        )
    ).first()
    if row is None:
        # Expired lease/reclaim or terminal execution: the worker must abort
        # its backlog (reliable streaming). Mirrors /complete.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="execution no longer owned by this lease",
        )
    if body.events:
        await session.execute(
            _SQL_INSERT_OUTPUT_EVENT,
            [
                {
                    "execution_id": row.id,
                    "seq": ev.seq,
                    "ts": ev.ts,
                    "stream": ev.stream,
                    "text": ev.text,
                }
                for ev in body.events
            ],
        )
        # Dashboard SSE signal in the SAME transaction (delivered on commit,
        # discarded on rollback). No-op if SSE_ENABLED=false. workspace_id comes
        # from the authenticated worker; execution_id is the already resolved
        # one (no JOIN).
        await dashboard_notifier.output_appended(
            session, execution_id=row.id, workspace_id=worker.workspace_id
        )
    await session.commit()
    return Response(status_code=status.HTTP_202_ACCEPTED)


# --- Complete --------------------------------------------------------------

# Full capture: complete does not touch output. Output lives in
# `output_events` (single source of truth); the `stdout`/`stderr` fields are
# not part of the request schema, so a worker that sends them still gets a 200
# because Pydantic ignores extras.
#
# Retention: `output_events` are NOT purged on complete. The events persist as
# the source of the historical live view and of the downloadable artifact
# (which will be materialized server-side from these same events).
_SQL_COMPLETE = text(
    """
    UPDATE task_executions
       SET status = :new_status,
           outcome = :outcome,
           finished_at = now(),
           exit_code = :exit_code
     WHERE task_id = :task_id
       AND worker_id = :worker_id
       AND lease_token = :lease_token
       AND status IN ('claimed', 'running')
     RETURNING id
    """
)

# The outcome is decided server-side from the task payload: payload map first,
# server default after. One light SELECT per complete (complete is not a hot
# path; the 3s poll does not go through here).
_SQL_GET_TASK_PAYLOAD = text(
    "SELECT payload FROM tasks WHERE id = :task_id AND workspace_id = :workspace_id"
)

# Task close: for target_mode='count', success requires EXACTLY target_count
# (nominal fan-out) 'completed' executions. Failure is decided against
# `attempt_budget` (attempt budget; born = target_count and each retry bumps
# it +1): if the budget is exhausted in real terminals (`completed`/`failed`/
# `cancelled`, the SAME non-expired set as the claim predicate) without
# reaching target_count successes -> 'failed'. A later retry reopens the task
# (reset to queued + budget bump), so a successful retry-after-failure DOES
# close 'completed' (completed reaches the nominal even with earlier failed
# ones in the history).
# `expired` does NOT count for the close (full claim/close symmetry): a crash
# returns its slot and the task stays open for the recovery re-claim instead
# of closing prematurely with N-1 successes.
# If terminal_non_expired >= attempt_budget then active = 0 (because of the
# claim cap: non_expired <= attempt_budget), so the close never leaves an
# orphan in-flight execution. For 'all_available' the task closes when all the
# executions of the CURRENT snapshot are terminal (including `expired`: the
# snapshot is fixed and there is no re-claim to wait for); success only if the
# WHOLE snapshot ended 'completed', analogous to the exact success of
# count-mode and consistent with the sticky conversion, which already required
# the complete snapshot. The current snapshot is the executions with
# `snapshot_epoch = tasks.snapshot_epoch` (migration 0033): the admin retry
# re-snapshots while keeping the terminal history (migration 0014), so without
# the epoch scope `completed = total` would be unreachable after a retry. The
# `total_epoch > 0` guard avoids a spurious 'completed' close if the close runs
# with the current epoch still without executions (e.g. a late complete from an
# old epoch after a retry whose re-snapshot came out empty).
# When the worker reports status='cancelled' (outer timeout, cooperative
# cancel, etc.) we propagate the close to `tasks` with `cancelled_reason`
# persisted. Idempotent against the admin cancel: if the task is already
# `cancelled`/`completed`/`failed`, it is not touched.
# RETURNING 1: the caller only propagates the cancel to the sibling executions
# if this UPDATE actually flipped the task, mirroring `cancel_task_core`
# (admin), which only propagates after a successful cancel.
_SQL_CLOSE_TASK_CANCELLED = text(
    """
    UPDATE tasks
       SET status = 'cancelled',
           completed_at = now(),
           cancelled_reason = COALESCE(cancelled_reason, :reason)
     WHERE id = :task_id
       AND workspace_id = :workspace_id
       AND status NOT IN ('completed', 'cancelled', 'failed')
    RETURNING 1
    """
)

# Task close. `completed` includes closes with an outcome
# (closes_as='completed'): for count-mode and all_available a success with a
# nuance IS a success. `tasks.outcome` on close:
#   - completed: NULL if at least one completed execution has NO outcome
#     (a plain success wins); if every successful execution has an outcome,
#     the most recent one (current epoch in all_available).
#   - failed: the outcome of the last failed attempt (NULL if it declared none).
# The rest of the semantics (budget, epoch, expired) is unchanged.
#
# Counting window of the count-mode close: the NATIVE count is cumulative over
# the whole history, but a task CONVERTED by the sticky auto-retry
# (`converted_from='all_available'`) keeps the epoch scope of its origin mode.
# Without that, the `completed` of already closed epochs would reach
# `target_count` and close `completed` a task whose worker kept failing in the
# current epoch. The window choice lives in the `scoped` CTE (`*_scoped`
# columns), which is the ONLY source of that decision for the three count
# branches (status, outcome, completed_at); misaligning them would leave, e.g.,
# a `completed_at` set on a task that is not closed.
_SQL_MAYBE_CLOSE_TASK = text(
    f"""
    WITH agg AS (
        SELECT t.id, t.target_mode, t.target_count, t.attempt_budget,
               t.converted_from,
               count(*) FILTER (WHERE e.status IN
                   {sql_status_set(TERMINAL_NON_EXPIRED_EXECUTION_STATUSES)})
                   AS terminal_non_expired,
               count(*) FILTER (WHERE e.status = 'completed') AS completed,
               count(*) FILTER (WHERE e.status = 'completed' AND e.outcome IS NULL)
                   AS completed_plain,
               count(*) FILTER (WHERE e.snapshot_epoch = t.snapshot_epoch
                                  AND e.status IN
                   {sql_status_set(TERMINAL_EXECUTION_STATUSES)})
                   AS terminal_epoch,
               count(*) FILTER (WHERE e.snapshot_epoch = t.snapshot_epoch
                                  AND e.status = 'completed')
                   AS completed_epoch,
               count(*) FILTER (WHERE e.snapshot_epoch = t.snapshot_epoch
                                  AND e.status = 'completed' AND e.outcome IS NULL)
                   AS completed_plain_epoch,
               count(*) FILTER (WHERE e.snapshot_epoch = t.snapshot_epoch)
                   AS total_epoch
          FROM tasks t
          LEFT JOIN task_executions e ON e.task_id = t.id
         WHERE t.id = :task_id
         GROUP BY t.id
    ),
    scoped AS (
        -- Counting window of the count-mode close. With a provenance mark
        -- (`all_available`) it counts the current epoch; without a mark
        -- (native count, including everything created before migration 0050)
        -- it accumulates the whole history.
        SELECT a.*,
               CASE WHEN a.converted_from = 'all_available'
                    THEN a.completed_epoch ELSE a.completed END AS completed_scoped,
               CASE WHEN a.converted_from = 'all_available'
                    THEN a.completed_plain_epoch
                    ELSE a.completed_plain END AS completed_plain_scoped
          FROM agg a
    )
    UPDATE tasks t
       SET status = CASE
              WHEN a.target_mode = 'count' AND a.completed_scoped >= a.target_count
                   THEN 'completed'::task_status
              WHEN a.target_mode = 'count'
                   AND a.terminal_non_expired >= a.attempt_budget
                   THEN 'failed'::task_status
              WHEN a.target_mode = 'all_available' AND a.total_epoch > 0
                   AND a.terminal_epoch = a.total_epoch
                   AND a.completed_epoch = a.total_epoch
                   THEN 'completed'::task_status
              WHEN a.target_mode = 'all_available' AND a.total_epoch > 0
                   AND a.terminal_epoch = a.total_epoch
                   AND a.completed_epoch < a.total_epoch
                   THEN 'failed'::task_status
              ELSE t.status
           END,
           outcome = CASE
              -- plain success present: outcome NULL (preferred)
              WHEN (a.target_mode = 'count' AND a.completed_scoped >= a.target_count
                    AND a.completed_plain_scoped > 0)
                OR (a.target_mode = 'all_available' AND a.total_epoch > 0
                    AND a.terminal_epoch = a.total_epoch
                    AND a.completed_epoch = a.total_epoch
                    AND a.completed_plain_epoch > 0)
                   THEN NULL
              -- completed close with outcomes only: the most recent
              -- execution's. Same window as the count: in a converted task
              -- the outcome comes from the current epoch, like the
              -- all_available branch below; without a mark, full history.
              WHEN a.target_mode = 'count' AND a.completed_scoped >= a.target_count
                   THEN (SELECT e2.outcome FROM task_executions e2
                          WHERE e2.task_id = t.id AND e2.status = 'completed'
                            AND e2.outcome IS NOT NULL
                            AND (a.converted_from IS DISTINCT FROM 'all_available'
                                 OR e2.snapshot_epoch = t.snapshot_epoch)
                          ORDER BY e2.finished_at DESC NULLS LAST LIMIT 1)
              WHEN a.target_mode = 'all_available' AND a.total_epoch > 0
                   AND a.terminal_epoch = a.total_epoch
                   AND a.completed_epoch = a.total_epoch
                   THEN (SELECT e2.outcome FROM task_executions e2
                          WHERE e2.task_id = t.id
                            AND e2.snapshot_epoch = t.snapshot_epoch
                            AND e2.status = 'completed' AND e2.outcome IS NOT NULL
                          ORDER BY e2.finished_at DESC NULLS LAST LIMIT 1)
              -- failed close: the outcome of the last failed attempt. Scoped
              -- to the current epoch (t.snapshot_epoch) like the successful
              -- close above: for count-mode the epoch never bumps (always 0),
              -- so the scope changes nothing; for all_available it keeps a
              -- failed execution of an ALREADY CLOSED/re-snapshotted epoch
              -- from overwriting the outcome of the current epoch.
              WHEN (a.target_mode = 'count'
                    AND a.terminal_non_expired >= a.attempt_budget)
                OR (a.target_mode = 'all_available' AND a.total_epoch > 0
                    AND a.terminal_epoch = a.total_epoch
                    AND a.completed_epoch < a.total_epoch)
                   THEN (SELECT e3.outcome FROM task_executions e3
                          WHERE e3.task_id = t.id
                            AND e3.snapshot_epoch = t.snapshot_epoch
                            AND e3.status = 'failed'
                          ORDER BY e3.finished_at DESC NULLS LAST LIMIT 1)
              ELSE t.outcome
           END,
           completed_at = CASE
              WHEN t.status NOT IN ('completed','failed','cancelled')
               AND ((a.target_mode = 'count'
                     AND (a.completed_scoped >= a.target_count
                          OR a.terminal_non_expired >= a.attempt_budget))
                 OR (a.target_mode = 'all_available' AND a.total_epoch > 0
                     AND a.terminal_epoch = a.total_epoch))
              THEN now()
              ELSE t.completed_at
           END
      FROM scoped a
     WHERE t.id = a.id
       AND t.workspace_id = :workspace_id
       AND t.status NOT IN ('completed','failed','cancelled')
    """
)


# --- Sticky auto-retry ---------------------------------------------------

# Reads the sticky policy + current counter + target_mode under `FOR UPDATE`
# to avoid races with other concurrent completes (unlikely in the normal flow
# because each execution is "owned" by a single worker, but cheap and correct).
_SQL_GET_RETRY_POLICY = text(
    """
    SELECT auto_retry_policy, auto_retry_attempts_used,
           target_mode, run_at, workspace_id
      FROM tasks
     WHERE id = :id
       AND workspace_id = :workspace_id
       FOR UPDATE
    """
)

_SQL_INC_AUTO_RETRY_USED = text(
    """
    UPDATE tasks
       SET auto_retry_attempts_used = auto_retry_attempts_used + 1
     WHERE id = :id
       AND workspace_id = :workspace_id
    """
)

# Applies backoff by setting run_at = now() + backoff (we do not use
# `expires_at` because it expires the task; backoff only delays the next claim
# eligibility). `_SQL_RESET_TASK_QUEUED` sets run_at = now(); this UPDATE
# overrides it with the backoff when it applies. If the caller wants no backoff
# (backoff_seconds=0), it is not executed.
_SQL_APPLY_BACKOFF = text(
    """
    UPDATE tasks
       SET run_at = now() + make_interval(secs => :secs)
     WHERE id = :id
       AND workspace_id = :workspace_id
    """
)


async def _maybe_auto_retry(
    *,
    session: AsyncSession,
    task_id: int,
    worker_id: str,
    workspace_id: str,
    body: TaskCompleteRequest,
    classified_status: str,
) -> bool:
    """Try to apply the sticky auto-retry policy.

    Returns True if the task was re-queued (the terminal paths
    `_SQL_CLOSE_TASK_CANCELLED` / `_SQL_MAYBE_CLOSE_TASK` must not run in this
    complete). Returns False in any other case (no policy, non-retryable close,
    budget exhausted).

    Invariant: this helper is called *after* the execution was marked terminal
    through `_SQL_COMPLETE`. The new execution is created by `/poll` when the
    worker comes back (preserving the "one active execution per (task, worker)"
    model).
    """
    row = (
        (
            await session.execute(
                _SQL_GET_RETRY_POLICY, {"id": task_id, "workspace_id": workspace_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["auto_retry_policy"] is None:
        return False

    try:
        policy = AutoRetryPolicy.model_validate(row["auto_retry_policy"])
    except Exception:
        # Corrupt policy (tampered with through direct SQL, downgrade, etc.).
        # We do not block the complete: log and fall through to the normal
        # terminal close.
        logger.exception(
            "auto_retry: invalid policy in tasks.auto_retry_policy for task_id=%s",
            task_id,
        )
        return False

    if not policy.applies_to(
        status=classified_status,
        exit_code=body.exit_code,
        cancelled_reason=body.cancelled_reason,
    ):
        return False

    used = int(row["auto_retry_attempts_used"])
    if used >= policy.max_attempts:
        # Budget exhausted: terminal close through the normal path.
        return False

    # Apply the retry. Order: bump counter -> reset queued -> retarget if
    # sticky -> budget mutation (bump +1 in native count-mode, or resync to
    # count(non-expired)+1 in the all_available->sticky conversion).
    # (That mutation touches `attempt_budget`, not `target_count`, so it does
    # not interact with the CHECK `tasks_target_worker_exclusive_chk` and the
    # order relative to the retarget is not load-bearing; it is kept for
    # readability.)
    await session.execute(_SQL_INC_AUTO_RETRY_USED, {"id": task_id, "workspace_id": workspace_id})
    await session.execute(_SQL_RESET_TASK_QUEUED, {"id": task_id, "workspace_id": workspace_id})

    if policy.sticky:
        # Reuse of the worker's local cache: re-route to this worker via
        # `target_worker_pool=[uuid]`. If the worker disconnects, the
        # `_relax_sticky_pool` housekeeping will open it after
        # `sticky_offline_timeout_seconds`.
        await session.execute(
            _SQL_RETARGET_CLAIM_SAME_WORKER,
            {
                "id": task_id,
                "pool": json.dumps([str(worker_id)]),
                "workspace_id": workspace_id,
            },
        )

    # Every NATIVE count-mode retry reopens budget by bumping
    # `attempt_budget` by 1, including the sticky-in-count branch. With the
    # non-expired predicate, the terminal execution that triggers this retry
    # consumes budget; without the bump the re-claim would never fire. The
    # consumed execution keeps the bump neutral: the sticky pool (or the
    # predicate cap) still admits exactly one re-claim. `target_count` (success
    # threshold) is NOT touched: the retry grants one more attempt, it does not
    # demand one more success.
    #
    # The `all_available`->sticky conversion is the EXCEPTIONAL case: a blind
    # +1 bump would be wrong. See the `if` block below.
    if row["target_mode"] == "all_available":
        if policy.sticky:
            # The all_available->sticky conversion left attempt_budget=N
            # (snapshot size) but the `expired` executions returned their slot
            # without re-consumption (in all_available the budget does not gate
            # the claim). A blind +1 bump would open 1+count(expired) phantom
            # slots. Re-anchoring to count(non-expired)+1 opens EXACTLY one
            # re-claim. Native count-mode (elif branch) does use +1 because
            # there the budget gates every claim.
            await session.execute(
                _SQL_RESYNC_ATTEMPT_BUDGET_TO_INFLIGHT,
                {"id": task_id, "workspace_id": workspace_id},
            )
        # all_available without sticky: we cannot re-snapshot from here (same
        # limit as the manual retry without force_same_worker) and the task
        # stays in all_available, where the budget does not govern the claim, so
        # it is not touched.
    elif row["target_mode"] == "count":
        await session.execute(
            _SQL_BUMP_ATTEMPT_BUDGET, {"id": task_id, "workspace_id": workspace_id}
        )
    else:
        # Defensive exhaustiveness: today only 'all_available' and 'count'
        # exist. A new mode would land here as a silent no-op (neither bump nor
        # resync), which would break the re-claim without any signal. We log it
        # to catch that.
        logger.error(
            "auto_retry: unexpected target_mode %r for task_id=%s; no budget mutation applied",
            row["target_mode"],
            task_id,
        )

    # Backoff: apply the delay if configured. `_SQL_RESET_TASK_QUEUED` just set
    # run_at=now(); this UPDATE overrides it.
    if policy.backoff_seconds > 0:
        await session.execute(
            _SQL_APPLY_BACKOFF,
            {"id": task_id, "secs": policy.backoff_seconds, "workspace_id": workspace_id},
        )

    # Audit. attempts_used+1 = number of the server-driven attempt that was
    # just scheduled (1-indexed).
    await record_admin_action(
        session,
        # Worker API: no ctx (not session-scoped). The audit row is attributed
        # to the tenant of the task being retried (workspace_id read from the
        # task row already locked by _SQL_GET_RETRY_POLICY ... FOR UPDATE).
        workspace_id=str(row["workspace_id"]),
        action="auto_retry_task",
        target_kind="task",
        target_id=str(task_id),
        payload={
            "trigger_status": body.status,
            "trigger_exit_code": body.exit_code,
            "trigger_cancelled_reason": body.cancelled_reason,
            "sticky": policy.sticky,
            "attempts_used": used + 1,
            "max_attempts": policy.max_attempts,
            "sticky_worker_id": str(worker_id) if policy.sticky else None,
        },
    )
    return True


@router.post("/{task_id}/complete", response_model=TaskCompleteResponse)
async def task_complete(
    task_id: int,
    body: TaskCompleteRequest,
    worker: WorkerDep,
    session: SessionDep,
) -> TaskCompleteResponse:
    execution_id = await close_execution_core(session, task_id=task_id, worker=worker, body=body)
    if execution_id is None:
        # Lease already expired or reclaimed. We return 409 so the worker knows
        # its work was discarded.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="execution no longer owned by this lease",
        )
    await session.commit()
    return TaskCompleteResponse()


async def close_execution_core(
    session: AsyncSession,
    *,
    task_id: int,
    worker: Worker,
    body: TaskCompleteRequest,
) -> str | None:
    """Terminal close of an execution with the full semantics of `/complete`.

    Classifies the outcome, marks the execution, applies the sticky auto-retry
    or propagates the close to the task. Returns the id of the closed
    execution, or `None` if the lease no longer belongs to this worker (the
    caller decides: `/complete` answers 409). It does not commit.

    `/poll` also reuses it to close as `failed` a just-claimed execution whose
    declared secret cannot be decrypted.
    """
    payload_row = (
        await session.execute(
            _SQL_GET_TASK_PAYLOAD,
            {"task_id": task_id, "workspace_id": str(worker.workspace_id)},
        )
    ).first()
    task_payload = payload_row[0] if payload_row is not None else None
    classified_status, outcome = classify_terminal(body.status, body.exit_code, task_payload)
    row = (
        await session.execute(
            _SQL_COMPLETE,
            {
                "task_id": task_id,
                "worker_id": worker.id,
                "lease_token": str(body.lease_token),
                "new_status": classified_status,
                "outcome": outcome,
                "exit_code": body.exit_code,
            },
        )
    ).first()
    if row is None:
        return None

    # The execution just became terminal. SSE signal in the SAME transaction
    # (delivered on commit, discarded on rollback) so the Execution output page
    # refreshes its status without waiting for more output. It goes before any
    # branch: it covers both the auto-retry path and the normal close, which both
    # end in a commit. No-op if SSE_ENABLED=false.
    await dashboard_notifier.execution_changed(
        session, execution_id=row.id, workspace_id=worker.workspace_id
    )

    # Sticky auto-retry: if the task has a policy and the close falls inside
    # the retryable matrix + budget remains, we re-queue BEFORE propagating the
    # terminal close. The current execution is already terminal through
    # `_SQL_COMPLETE`; the next `/poll` will insert a new execution with an
    # incremented attempt_index.
    #
    # Guard: an outcome with closes_as='completed' NEVER enters the retry
    # matrix, even if `retryable_exit_codes` lists its exit_code.
    # `classified_status` is literally 'completed' (the nuance lives in
    # `outcome`), and `applies_to` WOULD match it if the operator configured
    # that exit_code as retryable, reopening a task that already succeeded.
    # This guard keeps the invariant: the retry matrix is only evaluated when
    # the close is NOT a success with a nuance. An outcome with
    # closes_as='failed' (custom, via payload.outcomes) DOES enter the matrix,
    # since it behaves as a real failure, so the guard only excludes the
    # completed+outcome combination.
    retried = (
        await _maybe_auto_retry(
            session=session,
            task_id=task_id,
            worker_id=str(worker.id),
            workspace_id=str(worker.workspace_id),
            body=body,
            classified_status=classified_status,
        )
        if not (outcome is not None and classified_status == "completed")
        else False
    )
    if retried:
        return str(row.id)

    if body.status == "cancelled":
        # Propagate cancel + reason to the task before the general rollup.
        # `_SQL_MAYBE_CLOSE_TASK` does not emit 'cancelled' as a terminal task
        # state (only completed/failed), so this explicit path is the one that
        # covers outer_timeout-driven cancels.
        flipped = (
            await session.execute(
                _SQL_CLOSE_TASK_CANCELLED,
                {
                    "task_id": task_id,
                    "reason": body.cancelled_reason,
                    "workspace_id": str(worker.workspace_id),
                },
            )
        ).first()
        if flipped is not None:
            # The close flipped the task -> propagate the cancel to the active
            # sibling executions, mirroring `cancel_task_core`. The filter
            # `status IN ('assigned','claimed','running')` naturally excludes
            # the reporter (already terminal through `_SQL_COMPLETE`). The
            # siblings keep their `lease_token`, so `_SQL_POLL_CANCEL_SIGNALS`
            # delivers the kill on their next poll: the same machinery as the
            # admin cancel, no new signal code. If it did NOT flip: either the
            # admin already cancelled (the propagation already happened) or the
            # task closed completed/failed (the close gate guarantees zero
            # active ones); propagating here would be redundant and would mask
            # broken invariants.
            await session.execute(_SQL_CANCEL_EXECUTIONS, {"id": task_id})
    await session.execute(
        _SQL_MAYBE_CLOSE_TASK,
        {"task_id": task_id, "workspace_id": str(worker.workspace_id)},
    )
    return str(row.id)


# --- Artifacts -------------------------------------------------------------
#
# Real files produced by the task (distinct from the on-demand log). The worker
# uploads them in multipart batches BEFORE /complete; the server stores the
# bytes on the filesystem (ARTIFACTS_ROOT/<execution_id>/<rel_path>) and the
# metadata in task_artifacts. Idempotency: resending (execution_id, rel_path)
# with the same sha256 is a no-op. Limits (50MiB/file, 200MiB/execution, 500
# files) -> 413. Invalid lease -> 409 (the worker discards its artifact
# backlog).

_SQL_RESOLVE_EXECUTION_ARTIFACT = text(
    """
    SELECT e.id, t.workspace_id
      FROM task_executions e
      JOIN tasks t ON t.id = e.task_id
     WHERE e.task_id = :task_id
       AND e.worker_id = :worker_id
       AND e.lease_token = :lease_token
       AND e.status IN ('claimed', 'running')
    """
)

_SQL_ARTIFACT_TOTALS = text(
    """
    SELECT COALESCE(SUM(size_bytes), 0) AS total_bytes, COUNT(*) AS n
      FROM task_artifacts
     WHERE execution_id = :execution_id
    """
)

# sha256 + size_bytes already persisted for the rel_paths of the batch.
# size_bytes is needed to subtract the old size from the total when updating an
# existing rel_path (otherwise the total guard would count it twice). A single
# query per batch (`rel_path = ANY(:rel_paths)`) instead of one round-trip per
# file. `collected_at` marks a row whose bytes were purged from disk: it does
# not count toward the workspace quota and must return to available state if
# the upload restores the bytes.
_SQL_ARTIFACT_EXISTING_BATCH = text(
    """
    SELECT rel_path, sha256, size_bytes, collected_at FROM task_artifacts
     WHERE execution_id = :execution_id AND rel_path = ANY(:rel_paths)
    """
)

# The DO UPDATE clears the collection mark: if the bytes are back on disk, the
# artifact is available again and the download can no longer answer 410.
_SQL_UPSERT_ARTIFACT = text(
    """
    INSERT INTO task_artifacts (execution_id, rel_path, size_bytes, sha256, content_type)
    VALUES (:execution_id, :rel_path, :size_bytes, :sha256, :content_type)
    ON CONFLICT (execution_id, rel_path)
    DO UPDATE SET size_bytes = EXCLUDED.size_bytes,
                  sha256 = EXCLUDED.sha256,
                  content_type = EXCLUDED.content_type,
                  collected_at = NULL,
                  collected_note = NULL
    """
)


def _part_size(upload: UploadFile) -> int:
    """Size in bytes of a multipart part, WITHOUT consuming the stream.

    Uses ``UploadFile.size`` (populated by the multipart parser) when
    available; otherwise it does ``seek(END)``/``tell()`` on the spooled file
    (O(1), reads no bytes) and restores the position. This allows validating the
    limits BEFORE writing anything to disk."""
    if upload.size is not None:
        return upload.size
    f = upload.file
    pos = f.tell()
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(pos)
    return size


def _read_chunks(upload: UploadFile):
    """Generator of 1 MiB chunks from an ``UploadFile``. It is iterated inside
    ``run_in_threadpool`` (via ``stage_artifact_bytes``), so the synchronous
    read of the spooled file does NOT block the event loop."""
    upload.file.seek(0)
    while True:
        chunk = upload.file.read(1024 * 1024)
        if not chunk:
            break
        yield chunk


@router.post(
    "/{task_id}/artifacts",
    status_code=status.HTTP_202_ACCEPTED,
)
async def task_upload_artifacts(
    task_id: int,
    worker: WorkerDep,
    session: SessionDep,
    lease_token: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> TaskArtifactsUploadResponse:
    row = (
        await session.execute(
            _SQL_RESOLVE_EXECUTION_ARTIFACT,
            {
                "task_id": task_id,
                "worker_id": worker.id,
                "lease_token": lease_token,
            },
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="execution no longer owned by this lease",
        )
    execution_id = str(row.id)
    workspace_id = str(row.workspace_id)

    totals = (await session.execute(_SQL_ARTIFACT_TOTALS, {"execution_id": execution_id})).first()
    running_total = int(totals.total_bytes)
    running_count = int(totals.n)

    # --- Step 1: validate the WHOLE batch before touching disk ---------------
    #
    # rel_path (->422), per-file size and projected count/total limits (->413)
    # are checked for each part BEFORE writing a single byte. So, if any file
    # of the batch violates a limit, NO file is written: a later file that
    # triggers the 413 cannot leave new bytes on disk with the metadata row
    # uncommitted (the disk<->DB inconsistency a single-pass flow would allow
    # when updating a pre-existing path). The size is taken from the part
    # without consuming the stream (`_part_size`); the guards of
    # `stage_artifact_bytes` act as defense in depth in step 2.
    #
    # Step 1a: validate rel_path + per-file size for each part, collecting the
    # rel_paths for a single existence query (avoids N+1).
    parts: list[dict[str, Any]] = []
    for upload in files:
        rel_path_raw = upload.filename or ""
        try:
            rel_path = validate_rel_path(rel_path_raw)
        except InvalidRelPath as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid artifact rel_path: {rel_path_raw!r}",
            ) from exc

        size = _part_size(upload)
        if size > app_settings.artifact_max_file_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"artifact {rel_path!r} exceeds per-file limit",
            )
        parts.append(
            {
                "upload": upload,
                "rel_path": rel_path,
                "size": size,
                "content_type": upload.content_type or "application/octet-stream",
            }
        )

    # Step 1a': dedup of rel_path within the batch (last-wins). Batch staging
    # materializes a single .tmp per (execution_id, rel_path): two parts with
    # the same rel_path would share that .tmp and the second rename in
    # commit_staged would blow up (tmp already renamed) -> 500 + orphan bytes.
    # We keep the last part of each rel_path, mirroring the last-write-wins of
    # a single-pass rename. The per-file validation (rel_path/size) of 1a
    # already ran over ALL parts.
    if len({p["rel_path"] for p in parts}) != len(parts):
        deduped: dict[str, dict[str, Any]] = {}
        for p in parts:
            deduped[p["rel_path"]] = p
        parts = list(deduped.values())

    # Step 1b: a single query of the (sha256, size_bytes) already persisted for
    # the batch rel_paths, indexed by rel_path (avoids a round-trip per file).
    # A new rel_path simply does not appear in the dict.
    existing_rows = (
        await session.execute(
            _SQL_ARTIFACT_EXISTING_BATCH,
            {"execution_id": execution_id, "rel_paths": [p["rel_path"] for p in parts]},
        )
    ).all()
    existing_by_path = {r.rel_path: r for r in existing_rows}

    # Step 1c: project the count/total limits of the whole batch.
    planned: list[dict[str, Any]] = []
    for part in parts:
        rel_path = part["rel_path"]
        existing = existing_by_path.get(rel_path)
        existing_sha = existing.sha256 if existing is not None else None
        existing_size = int(existing.size_bytes) if existing is not None else 0
        # Collected row: the metadata exists but its bytes were purged from
        # disk and already left the quota counter. For the QUOTA it contributes
        # 0, so restoring it adds its full size. The per-execution limit still
        # uses `existing_size` (it is compared against a SUM that includes the
        # collected rows): they are two different notions and each delta uses
        # its own.
        existing_collected = existing is not None and existing.collected_at is not None
        quota_existing_size = 0 if existing_collected else existing_size

        # Incremental projection of the execution limits. When updating an
        # existing rel_path, subtract its old size (already in running_total via
        # SUM); if it is new, existing_size is 0 and it adds cleanly.
        new_count = running_count + (0 if existing is not None else 1)
        new_total = running_total - existing_size + part["size"]
        if new_count > app_settings.artifact_max_count:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="artifact count limit exceeded for this execution",
            )
        if new_total > app_settings.artifact_max_total_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="artifact total size limit exceeded for this execution",
            )
        running_count = new_count
        running_total = new_total
        planned.append(
            {
                "upload": part["upload"],
                "rel_path": rel_path,
                "size": part["size"],
                "existing_sha": existing_sha,
                "existing_size": existing_size,
                "existing_collected": existing_collected,
                "quota_existing_size": quota_existing_size,
                "content_type": part["content_type"],
            }
        )

    # --- Storage enforcement -------------------------------------------------
    # Logical per-workspace quota (net delta) + physical floor (raw bytes of the
    # batch, conservative). Before writing: if it does not fit, 507 without
    # touching disk. planned is 1:1 with parts (one append per validated part);
    # reusing the already derived `quota_existing_size` avoids the duplicate
    # lookup in existing_by_path (and it already carries the 0 of collected
    # rows).
    batch_new_bytes = sum(item["size"] for item in planned)
    net_delta = sum(item["size"] - item["quota_existing_size"] for item in planned)
    ws_usage = await storage.workspace_usage_bytes(session, workspace_id)
    # Effective quota: workspace override or global default.
    ws_quota = await storage.workspace_quota(session, workspace_id, settings=app_settings)
    # Strict >: uploading up to exactly the quota is allowed; the poll pauses at
    # >= (quota_full). Intentional asymmetry.
    if ws_usage + net_delta > ws_quota.quota_bytes:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="workspace artifact storage quota exceeded",
        )
    # Fresh (upload enforcement wants the instantaneous value), but measured in
    # a thread so as not to block the event loop.
    free_bytes = await storage.disk_free_bytes_async(app_settings.artifacts_root)
    if free_bytes - batch_new_bytes < app_settings.artifact_disk_min_free_bytes:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="insufficient disk space for artifacts",
        )

    # --- Step 2: write per batch (atomic on I/O failure) ---------------------
    #
    # Stage ALL the .tmp files first; only if the WHOLE batch was written,
    # promote (rename) in bulk. An ENOSPC happens during staging -> no rename
    # applied -> zero changes in the final paths.
    staged: list[StagedArtifact] = []
    try:
        for item in planned:
            try:
                s = await run_in_threadpool(
                    partial(
                        artifacts_store.stage_artifact_bytes,
                        app_settings.artifacts_root,
                        execution_id,
                        item["rel_path"],
                        _read_chunks(item["upload"]),
                        max_bytes=app_settings.artifact_max_file_bytes,
                        existing_sha=item["existing_sha"],
                    )
                )
            except ArtifactTooLarge as exc:
                # Defense in depth: the size was already validated in step 1.
                # stage already cleaned up its .tmp.
                discard_staged(staged)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"artifact {item['rel_path']!r} exceeds per-file limit",
                ) from exc
            except NoSpaceLeft as exc:
                discard_staged(staged)
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail="insufficient disk space for artifacts",
                ) from exc
            staged.append(s)

        # All written: promote in bulk (renames; very unlikely to fail).
        await run_in_threadpool(partial(commit_staged, staged))
    except BaseException:
        discard_staged(staged)
        raise

    # Metadata + counter (same transaction; commit at the end).
    # zip(staged, planned) is used instead of hanging attributes on the
    # dataclass (they go 1:1 in append order, equivalent in behavior and
    # cleaner). strict=True makes that 1:1 invariant explicit and checks it at
    # runtime.
    accepted: list[TaskArtifactOut] = []
    usage_delta = 0
    for s, item in zip(staged, planned, strict=True):
        existing_sha = item["existing_sha"]
        # `existing_collected` forces the UPSERT even if the sha did not change:
        # with the bytes restored, the collection mark must be cleared or the
        # download would keep answering 410 for a file that is present.
        if existing_sha != s.sha256 or item["existing_collected"]:
            await session.execute(
                _SQL_UPSERT_ARTIFACT,
                {
                    "execution_id": execution_id,
                    "rel_path": s.rel_path,
                    "size_bytes": s.size_bytes,
                    "sha256": s.sha256,
                    "content_type": item["content_type"],
                },
            )
            usage_delta += s.size_bytes - item["quota_existing_size"]
        accepted.append(
            TaskArtifactOut(
                rel_path=s.rel_path,
                size_bytes=s.size_bytes,
                sha256=s.sha256,
                content_type=item["content_type"],
            )
        )

    await storage.bump_workspace_usage(session, workspace_id, usage_delta)
    await session.commit()
    return TaskArtifactsUploadResponse(accepted=accepted)
