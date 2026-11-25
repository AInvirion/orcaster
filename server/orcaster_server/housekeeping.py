"""Housekeeping cron.

Periodic `asyncio` task that the FastAPI lifespan launches at startup:

- Marks `online` workers as `offline` when `last_heartbeat_at` exceeds
  `WORKER_HEARTBEAT_TIMEOUT_SECONDS`.
- Reclaim of expired leases (`claimed`/`running` with
  `lease_expires_at < now()`) -> `status='expired'`. Identical to step 1 of the
  canonical claim SQL; here it runs periodically for tasks that receive no
  active claims.
- Timeout of pre-assignments (`assigned` whose
  `assigned_at + tasks.claim_timeout_seconds < now()`) -> `status='expired'`.
  Identical to step 2 of the canonical claim SQL.
- Reconcile of the `workspace_artifact_usage` counter every
  `ARTIFACT_USAGE_RECONCILE_PASSES` passes: full the first time in each
  process, and bounded to the workspaces with recent activity afterwards
  (in-process in-memory watermark).
- Quota over-commit check at the same cadence: WARNING if the sum of effective
  quotas exceeds the usable disk.

- Re-snapshot of the open `all-available` tasks that were left `dispatching`
  with no execution in their current epoch: when an eligible worker joins, the
  snapshot that could not be taken at creation is materialized for them.
  Skipped inside the startup grace (see `_run_once`).

- Sweep of expired `webauthn_challenges`: deletes in bounded batches the
  passkey challenges that nobody presented. Every pass, also inside the startup
  grace (it depends on the DB clock, which does not freeze).

Note: orphaned push tasks are NOT cleaned up. The FK
`tasks.target_worker_id -> workers.id` is `ON DELETE CASCADE` (see migration
0007), so deleting a worker removes its pending push tasks. The CHECK
`tasks_push_requires_target_worker_chk` also prevents creating a push task
without a target.

Each operation runs in its own transaction/`try` so that an isolated error does
not abort the whole loop. Logs go through standard `logging`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from orcaster_server import storage
from orcaster_server.config import Settings
from orcaster_server.dispatch_eligibility import (
    SQL_SYNC_TARGET_COUNT_TO_SNAPSHOT,
    task_ref_for_alias,
    worker_is_eligible_sql,
)
from orcaster_server.task_secrets.gate import (
    payload_declares_task_env_sql,
    worker_recently_refused_sql,
)

logger = logging.getLogger(__name__)


# --- Artifact reconcile state --------------------------------------------

# Instant (DB clock) at which the last successful reconcile pass started.
# Deliberately in process memory: there is no table or migration behind it.
# `None` (process start, or before the first successful pass) => the next pass is
# FULL, which gives a natural safety net on every restart. With
# `UVICORN_WORKERS=N` each process keeps its own: the worst case is N full passes
# at startup and overlapping windows afterwards, which is idempotent (the
# reconcile recomputes the real value, it does not apply deltas).
_last_reconcile_at: datetime | None = None

# Floor of the activity filter's overlap margin (see
# `_reconcile_overlap_seconds`).
_RECONCILE_OVERLAP_SECONDS = 300.0

_SQL_DB_NOW = text("SELECT now()")


# --- Quota over-commit ----------------------------------------------------
#
# Last commitment state logged by this process, so as to warn only when it
# CHANGES (not every ~5 min). The key excludes free space and usage, which move
# on every pass: the verdict and the quota configuration count. It is reset when
# the loop starts, so the first pass of each process always reports a standing
# over-commit.
_CommitmentKey = tuple[int, int, int, str]
_last_quota_commitment: _CommitmentKey | None = None


def reset_quota_commitment_state() -> None:
    """Forget the last logged state (loop start and tests)."""
    global _last_quota_commitment
    _last_quota_commitment = None


def _commitment_verdict(c: storage.QuotaCommitment) -> str:
    if c.available_bytes is None:
        return "unmeasurable"
    return "overcommitted" if c.overcommitted else "fits"


async def _check_quota_commitment(engine: AsyncEngine, settings: Settings) -> None:
    """Log if what the workspaces can still grow, sum of max(effective quota -
    usage, 0), exceeds the free space above the floor: free -
    ``ARTIFACT_DISK_MIN_FREE_BYTES``.

    WARNING on entering over-commit (or on starting with it standing) and every
    time the quota configuration changes while it lasts; INFO when it fits again;
    WARNING if the disk could not be measured. With no changes it emits nothing.
    """
    global _last_quota_commitment
    c = await storage.quota_commitment(engine, settings)
    verdict = _commitment_verdict(c)
    key = (c.workspaces, c.overridden, c.committed_bytes, verdict)
    if key == _last_quota_commitment:
        return
    previous = _last_quota_commitment
    _last_quota_commitment = key
    if verdict == "unmeasurable":
        logger.warning(
            "housekeeping: artifact quota over-commit check skipped: cannot measure "
            "the free space of %s",
            settings.artifacts_root,
        )
    elif verdict == "overcommitted":
        logger.warning(
            "housekeeping: artifact quotas over-committed: %d workspace(s) (%d with "
            "override) can still grow %d bytes (sum over workspaces of max(effective "
            "quota - usage, 0)) but only %d bytes are free above the floor (free %d - "
            "ARTIFACT_DISK_MIN_FREE_BYTES %d); committed quotas %d bytes, artifact "
            "usage %d bytes; the shared disk floor can pause polling for every workspace",
            c.workspaces,
            c.overridden,
            c.headroom_bytes,
            c.available_bytes,
            c.free_bytes,
            settings.artifact_disk_min_free_bytes,
            c.committed_bytes,
            c.usage_bytes,
        )
    elif previous is not None and previous[3] == "overcommitted":
        logger.info(
            "housekeeping: artifact quotas fit the disk again: workspaces can still "
            "grow %d bytes, %d bytes free above ARTIFACT_DISK_MIN_FREE_BYTES",
            c.headroom_bytes,
            c.available_bytes,
        )


def reset_reconcile_watermark() -> None:
    """Forget the last pass's watermark: the next one will be full.

    Called by `housekeeping_loop` on startup (starting the loop == starting the
    process, in production) and by tests so they do not depend on ordering."""
    global _last_reconcile_at
    _last_reconcile_at = None


# --- SQL helpers --------------------------------------------------------

# Only `online` workers (not `paused`) transition to `offline`.
_MARK_OFFLINE_SQL = text(
    """
    UPDATE workers
       SET status = 'offline'
     WHERE status = 'online'
       AND deleted_at IS NULL
       AND last_heartbeat_at < now() - make_interval(secs => :timeout)
    """
)

# Expired leases in claimed/running -> expired.
_RECLAIM_LEASES_SQL = text(
    """
    UPDATE task_executions
       SET status = 'expired', finished_at = now()
     WHERE status IN ('claimed', 'running')
       AND lease_expires_at < now()
    """
)

# Startup grace observability: read-only count of the expired leases that the
# grace is holding back (the ones `_reclaim_expired_leases` would take right now
# if we were not in grace). Same predicate as `_RECLAIM_LEASES_SQL`, without the
# UPDATE.
_COUNT_EXPIRED_LEASES_SQL = text(
    """
    SELECT count(*)
      FROM task_executions
     WHERE status IN ('claimed', 'running')
       AND lease_expires_at < now()
    """
)

# Relax of the sticky pool when the pinned worker has been offline for more than
# `sticky_offline_timeout_seconds`. It only applies to `queued` tasks with a
# `target_worker_pool` of size 1 (sticky single-worker) and a non-null
# `auto_retry_policy` (by convention this guarantees that the operator explicitly
# enabled sticky retry). Clearing the pool reopens the task to the claim of any
# eligible worker, the same effect as `target='1'` without restriction.
_RELAX_STICKY_POOL_SQL = text(
    """
    UPDATE tasks t
       SET target_worker_pool = NULL
      FROM workers w
     WHERE t.status = 'queued'
       AND t.auto_retry_policy IS NOT NULL
       AND jsonb_typeof(t.target_worker_pool) = 'array'
       AND jsonb_array_length(t.target_worker_pool) = 1
       AND w.id = (t.target_worker_pool ->> 0)::uuid
       AND w.last_heartbeat_at < now() - make_interval(
              secs => COALESCE(
                  (t.auto_retry_policy ->> 'sticky_offline_timeout_seconds')::int,
                  1800
              )
           )
    """
)

# Expired pre-assignments -> expired.
#
# The pre-assignment of a task that declares `task_env` does not expire while
# its worker keeps being refused secret delivery (a recent
# `workers.task_secrets_refused_at` mark, which its own poll renews). That worker
# is indeed asking for the task: it is the server that cannot hand it over. If it
# expired, `_SQL_MAYBE_CLOSE_TASK` would count it as terminal and another
# worker's `/complete` would close the task as `failed` for lack of the keys,
# which is what this exemption avoids; and even without that, the worker would
# not receive it once the keys return. When the worker stops polling, or the
# refusal ceases, the mark ages and the expiry runs as usual.
#
# The condition on the task is the `task_env` declaration and not the existence
# of rows in `task_secrets`: deleting the last secret is the documented way out
# to unblock the task and removes those rows while the mark is still fresh (up to
# 120 s). With the rows as the condition, a housekeeping pass before the worker's
# next poll would expire the pre-assignment right when the cause is fixed.
#
# The mark is only renewed by the worker's poll, so with the server down it ages,
# and the startup pass runs before anyone polls: an outage of more than ~1 min
# (always, past 2 min) would expire the pre-assignment right after "configure the
# keys file and restart". Inside the startup grace (`in_grace`) the exemption
# does not look at the age of the mark: it is enough that the worker has one.
# After the grace, the workers that are still alive have already polled (idle
# cadence of 30 s) and the mark is again valid only for its window. With
# `STARTUP_GRACE_SECONDS` lower than the poll cadence this protection shrinks or
# disappears. The mark, which only a refused poll writes, still bounds the
# exemption; the `task_env` declaration only rules out the tasks that could not
# be refused for secrets.
_EXPIRE_ASSIGNED_SQL = text(
    f"""
    UPDATE task_executions e
       SET status = 'expired', finished_at = now()
      FROM tasks t
     WHERE e.task_id = t.id
       AND e.status = 'assigned'
       AND t.claim_timeout_seconds IS NOT NULL
       AND e.assigned_at + make_interval(secs => t.claim_timeout_seconds) < now()
       AND NOT (
            EXISTS (
                SELECT 1 FROM workers w
                 WHERE w.id = e.worker_id
                   AND (
                        {worker_recently_refused_sql("w.task_secrets_refused_at")}
                        OR (CAST(:in_grace AS boolean)
                            AND w.task_secrets_refused_at IS NOT NULL)
                   )
            )
            AND {payload_declares_task_env_sql("t.payload")}
       )
    """
)


# Re-snapshot of an OPEN `all-available` task that was left `dispatching` with no
# execution in its current epoch, that is, with no eligible worker when it was
# created (or when it was retried). The worker list is frozen when the snapshot
# is materialized, so an eligible worker that joined later would never see the
# task, and nothing would close it either; this pass covers that case.
#
# Deliberate scope, and this is the half that matters:
#
# - **Only the open snapshot** (`target_worker_pool IS NULL`). `all:<ids>`
#   closes `failed/no_eligible_workers` at creation and waits for no one: there
#   the pool is explicit and finite. A closed task is not reopened.
# - **Only an epoch with NO execution at all.** This bounds the work to one
#   re-snapshot per epoch: as soon as it inserts rows, the epoch stops being a
#   candidate. If those rows expire without completing, the task stays
#   `dispatching`; recomputing the state of a task whose executions expired is a
#   separate matter and is not handled here.
# - **Outside the startup grace.** Inside the grace the workers are not marked
#   offline (their heartbeat froze while the server was down), so
#   `status='online'` does not mean "alive" and we would pre-assign to a dead
#   worker, whose `assigned` row would expire and leave the epoch a non-candidate.
# - **`FOR UPDATE ... SKIP LOCKED`** on the candidates: with several uvicorn
#   processes, two simultaneous passes would insert the same (task, worker) pair
#   and violate the partial unique `task_executions_active_uk`.
#
# The gates are the SAME as those of the creation snapshot, imported from
# `orcaster_server/dispatch_eligibility.py`, read here from the CTE row because
# this statement walks several tasks and has no single `:task_id`.
_RESNAPSHOT_MAX_TASKS = 50

_RESNAPSHOT_TASK = task_ref_for_alias("c")

_RESNAPSHOT_AWAITING_SQL = text(
    f"""
    WITH candidates AS (
        SELECT t.id, t.snapshot_epoch, t.workspace_id, t.eligibility, t.payload
          FROM tasks t
         WHERE t.target_mode = 'all_available'
           AND t.status = 'dispatching'
           AND t.target_worker_pool IS NULL
           AND (t.expires_at IS NULL OR t.expires_at > now())
           AND NOT EXISTS (
                 SELECT 1 FROM task_executions e
                  WHERE e.task_id = t.id
                    AND e.snapshot_epoch = t.snapshot_epoch
               )
         ORDER BY t.id
         LIMIT :limit
           FOR UPDATE SKIP LOCKED
    )
    INSERT INTO task_executions (task_id, worker_id, status, attempt_index,
                                 snapshot_epoch)
    SELECT c.id, w.id, 'assigned',
           COALESCE((SELECT MAX(attempt_index) FROM task_executions
                      WHERE task_id = c.id AND worker_id = w.id), 0) + 1,
           c.snapshot_epoch
      FROM candidates c
      JOIN workers w ON w.workspace_id = c.workspace_id
                    AND w.status = 'online'
                    AND w.deleted_at IS NULL
     WHERE {worker_is_eligible_sql(_RESNAPSHOT_TASK)}
       -- Defence, same as in the creation snapshots: a worker with an active
       -- execution of this same task is excluded (it would violate the partial
       -- unique and make the re-snapshot non-deterministic).
       AND NOT EXISTS (
             SELECT 1 FROM task_executions e
              WHERE e.task_id = c.id
                AND e.worker_id = w.id
                AND e.status IN ('assigned', 'claimed', 'running')
           )
    RETURNING task_id
    """
)


async def _resnapshot_awaiting_all_available(
    engine: AsyncEngine, settings: Settings
) -> tuple[int, int]:
    """Dispatch the open `all-available` tasks that were waiting for an eligible worker.

    Returns `(tasks, pre-assignments)`. Everything in one transaction: the
    `assigned` rows and the `target_count`/`attempt_budget` that counts them must
    be visible together, or closing the task would measure against a nominal that
    does not match its snapshot.
    """
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                _RESNAPSHOT_AWAITING_SQL,
                {
                    "limit": _RESNAPSHOT_MAX_TASKS,
                    "claim_min_free_fraction": settings.worker_claim_min_free_fraction,
                },
            )
        ).all()
        if not rows:
            return 0, 0
        per_task = Counter(int(r[0]) for r in rows)
        for task_id, n in per_task.items():
            await conn.execute(SQL_SYNC_TARGET_COUNT_TO_SNAPSHOT, {"task_id": task_id, "n": n})
        return len(per_task), len(rows)


# Expired passkey challenges. Bounded batches so as not to hold a long lock or a
# huge transaction if a flood left many rows; the cap on batches per pass bounds
# the work of one pass (the rest goes to the next).
_SWEEP_CHALLENGES_BATCH = 1000
_SWEEP_CHALLENGES_MAX_BATCHES = 50

_SWEEP_EXPIRED_CHALLENGES_SQL = text(
    """
    DELETE FROM webauthn_challenges
     WHERE id IN (
            SELECT id FROM webauthn_challenges
             WHERE expires_at <= now()
             LIMIT :batch
               FOR UPDATE SKIP LOCKED
           )
    """
)


async def _sweep_expired_challenges(
    engine: AsyncEngine,
    batch_size: int = _SWEEP_CHALLENGES_BATCH,
    max_batches: int = _SWEEP_CHALLENGES_MAX_BATCHES,
) -> int:
    """Delete the expired passkey challenges, in batches of `batch_size` (each in
    its own transaction), up to `max_batches` per pass. Returns the total.

    `_store_challenge` already purges the expired ones on insert, but a server
    with no new `begin` calls would leave them there; this sweeps them without
    waiting for a request. `SKIP LOCKED`: a row that a `login/complete` is
    consuming is skipped."""
    total = 0
    for _ in range(max_batches):
        async with engine.begin() as conn:
            result = await conn.execute(_SWEEP_EXPIRED_CHALLENGES_SQL, {"batch": batch_size})
            n = result.rowcount or 0
        total += n
        if n < batch_size:
            break
    return total


async def _mark_offline_workers(engine: AsyncEngine, timeout_seconds: int) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(_MARK_OFFLINE_SQL, {"timeout": timeout_seconds})
        return result.rowcount or 0


async def _reclaim_expired_leases(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(_RECLAIM_LEASES_SQL)
        return result.rowcount or 0


async def _count_expired_leases(engine: AsyncEngine) -> int:
    """Read-only: how many executions have an expired lease right now (the ones
    the reclaim would take). Only used for the startup grace observability
    signal."""
    async with engine.connect() as conn:
        result = await conn.execute(_COUNT_EXPIRED_LEASES_SQL)
        return int(result.scalar() or 0)


async def _expire_assigned(engine: AsyncEngine, in_grace: bool = False) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(_EXPIRE_ASSIGNED_SQL, {"in_grace": in_grace})
        return result.rowcount or 0


async def _relax_sticky_pool(engine: AsyncEngine) -> int:
    """Release the auto-retry sticky pool when the pinned worker has been offline
    for more than `sticky_offline_timeout_seconds` (field read from the policy
    JSONB; default 1800s)."""
    async with engine.begin() as conn:
        result = await conn.execute(_RELAX_STICKY_POOL_SQL)
        return result.rowcount or 0


async def _db_now(engine: AsyncEngine) -> datetime:
    """Current instant according to the **Postgres** clock.

    The reconcile watermark is compared against timestamps stamped by the DB
    (`task_artifacts.created_at`, `workspace_artifact_usage.updated_at`), so it
    has to come from the same clock: the app server's may run ahead of or behind
    the DB's."""
    async with engine.connect() as conn:
        return (await conn.execute(_SQL_DB_NOW)).scalar_one()


def _reconcile_overlap_seconds(settings: Settings) -> float:
    """Overlap margin of the activity filter (seconds).

    It is subtracted from the watermark so as not to lose activity at the edge: a
    transaction that inserted an artifact with a `created_at` earlier than the
    mark but committed after the pass read would not have been seen, and without
    a margin would not be picked up in the next one either. Fixed floor of 5 min,
    and 2x the housekeeping interval if that is larger. Cheap: it enlarges the set
    of inspected workspaces, never shrinks it."""
    return max(_RECONCILE_OVERLAP_SECONDS, 2.0 * float(settings.housekeeping_interval_seconds))


async def _reconcile_workspace_usage(engine: AsyncEngine, settings: Settings) -> int:
    """Housekeeping wrapper over the storage reconcile.

    First pass of the process (mark `None`) -> full. The following ones filter by
    activity since the mark of the last **successful** pass, with the overlap
    margin subtracted. The mark advances only if the reconcile does not blow up:
    a failure leaves the window open and the next pass covers the same range.

    The instant is captured BEFORE reconciling: whatever happens during the pass
    falls in the next one's window, it is not lost.
    """
    global _last_reconcile_at
    started_at = await _db_now(engine)
    since = (
        None
        if _last_reconcile_at is None
        else _last_reconcile_at - timedelta(seconds=_reconcile_overlap_seconds(settings))
    )
    n = await storage.reconcile_workspace_usage(engine, since=since)
    _last_reconcile_at = started_at
    return n


# --- Loop ---------------------------------------------------------------


async def _run_once(
    engine: AsyncEngine, settings: Settings, passes: int = 0, in_grace: bool = False
) -> None:
    """Run one housekeeping pass. Each step isolated in try/except.

    During the startup grace (`in_grace=True`) the operations whose clock freezes
    while the server is down are skipped (offline by heartbeat, lease expiry,
    sticky pool relax), so as not to penalize healthy workers after a restart.
    `_expire_assigned` (claim timeout) and the artifact reconcile keep running;
    inside the grace, the claim timeout does not expire the pre-assignments of
    workers with a secret-refusal mark, whatever its age.

    During the grace it also emits an observability signal: if there are held
    expired leases (the ones the reclaim would take), it logs a WARNING so the
    operator sees that the grace is protecting work in progress.
    """
    if not in_grace:
        try:
            n = await _mark_offline_workers(engine, settings.worker_heartbeat_timeout_seconds)
            if n:
                logger.info("housekeeping: marked %d workers offline", n)
        except Exception:
            logger.exception("housekeeping: mark_offline_workers failed")

        try:
            n = await _reclaim_expired_leases(engine)
            if n:
                logger.info("housekeeping: reclaimed %d expired leases", n)
        except Exception:
            logger.exception("housekeeping: reclaim_expired_leases failed")
    else:
        # Grace observability signal: only emitted when there is something to
        # protect, so an idle grace generates no noise. WARNING because it is a
        # transient condition that the operator will want to see after a restart.
        try:
            held = await _count_expired_leases(engine)
            if held:
                logger.warning(
                    "housekeeping: startup grace protecting %d execution(s) with "
                    "expired leases (reclaim suppressed; healthy workers re-polling "
                    "after restart will renew them)",
                    held,
                )
        except Exception:
            logger.exception("housekeeping: count_expired_leases failed")

    try:
        n = await _expire_assigned(engine, in_grace=in_grace)
        if n:
            logger.info("housekeeping: expired %d assigned executions", n)
    except Exception:
        logger.exception("housekeeping: expire_assigned failed")

    if not in_grace:
        try:
            n = await _relax_sticky_pool(engine)
            if n:
                logger.info("housekeeping: relaxed sticky pool on %d tasks", n)
        except Exception:
            logger.exception("housekeeping: relax_sticky_pool failed")

        # It goes AFTER `_mark_offline_workers`, on purpose. That pass has
        # already demoted workers without a heartbeat to `offline`, so the
        # `status='online'` that this re-snapshot reads is the freshly corrected
        # one and we do not pre-assign to a worker that has just died.
        try:
            tasks_n, execs_n = await _resnapshot_awaiting_all_available(engine, settings)
            if tasks_n:
                logger.info(
                    "housekeeping: re-snapshotted %d all-available task(s) that were "
                    "waiting for an eligible worker (%d pre-assignments)",
                    tasks_n,
                    execs_n,
                )
        except Exception:
            logger.exception("housekeeping: resnapshot_awaiting_all_available failed")

    # Sweep of expired webauthn challenges (every pass).
    try:
        n = await _sweep_expired_challenges(engine)
        if n:
            logger.info("housekeeping: swept %d expired webauthn challenges", n)
    except Exception:
        logger.exception("housekeeping: sweep_expired_challenges failed")

    # Reconcile of the artifact counter every N passes (not on every one: it is an
    # aggregate over `task_artifacts`). The first pass of the process is full; the
    # following ones are bounded to the workspaces with recent activity, so the
    # log counts CORRECTED counters, not inspected workspaces: in a healthy steady
    # state it emits nothing.
    every = max(1, int(settings.artifact_usage_reconcile_passes))
    if passes % every == 0:
        try:
            n = await _reconcile_workspace_usage(engine, settings)
            if n:
                logger.info("housekeeping: reconciled artifact usage for %d workspaces", n)
        except Exception:
            logger.exception("housekeeping: reconcile_workspace_usage failed")
        try:
            await _check_quota_commitment(engine, settings)
        except Exception:
            logger.exception("housekeeping: quota commitment check failed")


async def housekeeping_loop(
    engine: AsyncEngine,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    """Main loop. Returns when `stop_event` is set.

    Between passes it waits `settings.housekeeping_interval_seconds`. It uses
    `wait_for(stop_event.wait())` so that shutdown is immediate.

    During the first `settings.startup_grace_seconds` (per process) the
    operations based on freezable clocks are skipped; see `_run_once`.

    Starting the loop resets the artifact reconcile watermark: the first pass of
    this process recomputes ALL the workspaces and the following ones filter by
    recent activity.
    """
    reset_reconcile_watermark()
    reset_quota_commitment_state()
    interval = max(1, int(settings.housekeeping_interval_seconds))
    grace = max(0, int(settings.startup_grace_seconds))
    boot = time.monotonic()
    logger.info("housekeeping: starting (interval=%ds, startup_grace=%ds)", interval, grace)
    passes = 0
    was_in_grace = False
    while not stop_event.is_set():
        elapsed = time.monotonic() - boot
        in_grace = elapsed < grace
        if in_grace:
            # Per-pass state at DEBUG: during the grace it is several lines per
            # process and adds nothing in normal operation. The actionable signal
            # is the WARNING from `_run_once` (protected leases) and the INFO at
            # the end of the grace.
            logger.debug(
                "housekeeping: startup grace active (%.0fs remaining), "
                "skipping offline/lease/sticky steps",
                grace - elapsed,
            )
        elif was_in_grace:
            # One-shot signal: marks the end of the grace so that the logs show
            # when housekeeping resumes reclaim/offline/sticky.
            logger.info(
                "housekeeping: startup grace ended (%ds elapsed), resuming full housekeeping",
                int(elapsed),
            )
        was_in_grace = in_grace
        await _run_once(engine, settings, passes, in_grace=in_grace)
        passes += 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
    logger.info("housekeeping: stopped")
