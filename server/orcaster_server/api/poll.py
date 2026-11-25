"""Unified endpoint `POST /v1/workers/{worker_id}/poll`.

Replaces the combination of `worker_heartbeat` + `claim_task` + `worker_commands`
delivery with a single polling-oriented endpoint. The worker client makes one
round trip per cycle and receives:

- a task (push or claim) when one is available;
- cancel signals for executions that were alive;
- a recommended cadence (`next_poll_after_seconds`).

Internal order (READ COMMITTED transaction):

1. Idempotent housekeeping (reclaim leases + expire assigned).
2. Worker update (last_heartbeat_at + optional telemetry).
3. Cancel signals (atomic UPDATE with RETURNING, exactly-once delivery).
4. Targeted push tasks (delivery_mode='push' + target_worker_id=self),
   delivered even if `status='busy'`.
5. all_available pre-assignment (only if idle, no push).
6. Open-claim count (only if idle, no push and no pre-assignment).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC
from typing import Annotated, NamedTuple

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server import storage
from orcaster_server.config import settings as app_settings
from orcaster_server.db import get_session
from orcaster_server.eligibility import (
    EligibilityError,
    capability_match_sql,
    validate_worker_capabilities,
)
from orcaster_server.models import EXPIRED_EXECUTION_STATUS, Worker
from orcaster_server.schemas.poll import (
    PollCancelSignal,
    PollRequest,
    PollResponse,
    PollTaskPayload,
)
from orcaster_server.signing import get_private_key, sign_task, signature_to_b64
from orcaster_server.task_secrets import blocked as task_secrets_blocked
from orcaster_server.task_secrets import keyring as task_secrets_keyring
from orcaster_server.task_secrets import service as task_secrets
from orcaster_server.task_secrets.gate import (
    TASK_SECRETS_MIN_WORKER_VERSION_STR,
    TASK_SECRETS_REFUSAL_WINDOW_SECONDS,
    task_uses_stored_secrets_sql,
    worker_recently_refused_sql,
    worker_supports_task_secrets,
    worker_supports_task_secrets_sql,
)
from orcaster_server.worker_auth import require_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workers", tags=["workers"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
WorkerDep = Annotated[Worker, Depends(require_worker)]


# --- SQL --------------------------------------------------------------------

# Lease housekeeping (reclaim of expired leases + expiry of assigned) is NOT part of
# the poll path: it lives only in `orcaster_server/housekeeping.py` (global
# background job, `_RECLAIM_LEASES_SQL` / `_EXPIRE_ASSIGNED_SQL`). See the note in
# `worker_poll`.

# Worker update: heartbeat + differential telemetry. `paused` is preserved.
#
# `last_reported_status` (migration 0053) persists the `status` that the worker
# reports about itself in the body. It has no `*_set` flag like the telemetry
# because `PollRequest.status` is mandatory (`Literal["idle","busy"]`): every
# poll carries it. It is INDEPENDENT of the `status` column, which remains the
# state the server decides (`online`/`paused`/`offline`, bounded by the CHECK
# `workers_status_chk`) and which this same statement forces to `online`
# unless paused. It is also written for a `paused` worker: it is a reported
# fact, not a decision, and the pick is already cut off earlier by the pause.
_SQL_POLL_UPDATE_WORKER = text(
    """
    UPDATE workers
       SET last_heartbeat_at = now(),
           status = CASE WHEN status = 'paused' THEN 'paused'::text ELSE 'online' END,
           last_reported_status = :last_reported_status,
           ram_used_mb = CASE WHEN :ram_used_mb_set THEN :ram_used_mb ELSE ram_used_mb END,
           disk_used_mb = CASE WHEN :disk_used_mb_set THEN :disk_used_mb ELSE disk_used_mb END,
           -- free_disk_fraction (migration 0054): GATE state, not cumulative
           -- telemetry like disk_used_mb. It is ALWAYS overwritten (no `*_set`
           -- guard): if the worker omits the field or sends garbage, the
           -- column goes back to NULL = "cannot measure" = fail-open. This way
           -- the claim (reads the body), fairness and snapshot (read this
           -- column) agree; with the `*_set` guard an old non-NULL value
           -- stayed stuck and the sites disagreed. The explicit CAST resolves
           -- asyncpg's type inference for the NULL bind (which the boolean
           -- flag avoided for the cumulative telemetry).
           free_disk_fraction = CAST(:free_disk_fraction AS double precision),
           cpu_pct = CASE WHEN :cpu_pct_set THEN :cpu_pct ELSE cpu_pct END,
           ram_mb = CASE WHEN :ram_mb_set AND :ram_mb IS DISTINCT FROM ram_mb
                         THEN :ram_mb ELSE ram_mb END,
           disk_mb = CASE WHEN :disk_mb_set AND :disk_mb IS DISTINCT FROM disk_mb
                          THEN :disk_mb ELSE disk_mb END,
           name = CASE WHEN :name_set AND :name IS DISTINCT FROM name
                       THEN :name ELSE name END,
           version = CASE WHEN :version_set AND :version IS DISTINCT FROM version
                          THEN :version ELSE version END,
           -- Capabilities declared in the poll. Without the flag (old
           -- worker, or invalid value ignored) the stored value is untouched. This
           -- UPDATE runs before the pick, in the same transaction: the claim of
           -- this very poll already evaluates `eligibility` against the new value.
           capabilities = CASE WHEN :capabilities_set
                                AND CAST(:capabilities AS jsonb) IS DISTINCT FROM capabilities
                               THEN CAST(:capabilities AS jsonb) ELSE capabilities END
     WHERE id = :worker_id
    RETURNING status
    """
)

# Implicit lease renewal of an execution in progress.
# The poll acts as a heartbeat: when the worker reports `busy` and presents the
# `lease_token` of its live execution, we refresh `lease_expires_at` so that
# housekeeping does not reclaim it mid-run. Same semantics as the canonical
# heartbeat (`api/tasks.py::_SQL_HEARTBEAT_RENEW`):
#   - only the worker's own execution (`worker_id` + `lease_token`);
#   - only if it is still alive (`claimed`/`running`) — a `cancelled`/`expired`
#     one is not resurrected;
#   - `claimed` → `running` on the first renewal;
#   - persists `current_execution_progress` as `progress` if it is sent.
_SQL_POLL_RENEW_LEASE = text(
    """
    UPDATE task_executions
       SET status = CASE WHEN status = 'claimed' THEN 'running'::execution_status
                         ELSE status END,
           last_heartbeat_at = now(),
           lease_expires_at = now() + make_interval(secs => :lease_seconds),
           progress = COALESCE(:progress, progress)
     WHERE worker_id = :worker_id
       AND lease_token = :lease_token
       AND status IN ('claimed', 'running')
    """
)

# Cancel signals: UPDATE + RETURNING = exactly-once delivery.
# Filters the worker's cancelled executions that have not been notified yet.
_SQL_POLL_CANCEL_SIGNALS = text(
    """
    UPDATE task_executions
       SET cancel_notified_at = now()
     WHERE worker_id = :worker_id
       AND status = 'cancelled'
       AND cancel_notified_at IS NULL
       AND lease_token IS NOT NULL
    RETURNING task_id, lease_token
    """
)

# Push tasks targeted at the worker. Delivered even if busy.
# Two steps to avoid an asyncpg gotcha with a complex CTE: first we select and
# lock the candidate task, then we insert the execution.

# Capabilities predicate for the polling worker. The worker's map comes from a
# subquery on `:worker_id` (constant parameter of the statement → Postgres
# resolves it as an InitPlan, once), and the requirements come from the candidate
# `tasks` row. Single source: `orcaster_server/eligibility.py`.
_POLLING_WORKER_SATISFIES_TASK = capability_match_sql(
    requirements="t.eligibility -> 'capabilities'",
    capabilities="SELECT capabilities FROM workers WHERE id = :worker_id",
)

# "The candidate task `t` uses stored secrets of its workspace". Single source:
# `orcaster_server/task_secrets/gate.py`. The three picks use it under
# `:exclude_stored_secrets`, which the handler computes per poll: a worker below
# the version floor does not receive those tasks (it would ignore them and run
# without them), and neither does a push that arrives in a `busy` poll (see
# step 4).
_TASK_USES_STORED_SECRETS = task_uses_stored_secrets_sql("t")
# "The candidate task `t` declares any of the names in `:exclude_secret_names`".
# After refusing a delivery because a secret cannot be decrypted, the poll picks
# again without the tasks that declare that name: a blocked task does not hold
# back the ones that use other, healthy secrets. `?|` on a JSONB array compares
# its string elements
# (https://www.postgresql.org/docs/current/functions-json.html); with an empty
# array it is false and the predicate excludes nothing. `COALESCE(..., false)`:
# without `task_env`, `jsonb_typeof` gives NULL and a `NOT NULL` in the WHERE
# would discard the task (the legacy mode, without `task_env`, declares nothing
# and is never excluded).
_TASK_DECLARES_EXCLUDED_NAME = (
    "COALESCE(jsonb_typeof(t.payload -> 'task_env') = 'array' "
    "AND t.payload -> 'task_env' ?| CAST(:exclude_secret_names AS text[]), false)"
)

# "The worker `other` had a secrets delivery refused recently".
# Single source of the window: `orcaster_server/task_secrets/gate.py`.
_OTHER_WORKER_RECENTLY_REFUSED = worker_recently_refused_sql("other.task_secrets_refused_at")

# Same source for the version of the worker `other` in the open claim's
# fairness-yield (see `_SQL_POLL_CLAIM_OPEN_LOCK`).
_OTHER_WORKER_SUPPORTS_TASK_SECRETS = worker_supports_task_secrets_sql("other.version")

_SQL_POLL_PICK_PUSH = text(
    f"""
    SELECT t.id, t.command, t.payload, t.signed, t.signature,
           t.idempotency_key, t.delivery_mode, t.created_at,
           t.target_worker_id, t.status AS task_status,
           t.execution_timeout_seconds, t.workspace_id::text AS workspace_id
      FROM tasks t
     WHERE t.delivery_mode = 'push'
       AND t.target_worker_id = :worker_id
       -- Scoped to the worker's workspace; never deliver tasks of another tenant.
       AND t.workspace_id = :workspace_id
       AND t.status IN ('queued', 'dispatching')
       AND t.run_at <= now()
       AND (t.expires_at IS NULL OR t.expires_at > now())
       -- Not even a targeted push is delivered to a worker that does not
       -- meet `eligibility.capabilities`. The operator named the worker, but
       -- the task declared what it needs to run properly; delivering it anyway
       -- is the defect this predicate avoids. The task stays `queued`.
       AND {_POLLING_WORKER_SATISFIES_TASK}
       -- A push that uses stored secrets is not picked for a worker below
       -- the version floor (it would ignore `secrets`) nor in a `busy` poll.
       -- In `busy` the worker would discard it and, without inserting the
       -- execution, the task stays `queued` until an `idle` poll, which goes
       -- through the secrets checks (including the refusal when the keys are
       -- missing: the task stays `queued`). It goes in the selection's WHERE:
       -- filtering afterwards would leave the next push untried.
       AND (NOT CAST(:exclude_stored_secrets AS boolean)
            OR NOT {_TASK_USES_STORED_SECRETS})
       -- Names whose delivery was just refused in this poll.
       AND NOT {_TASK_DECLARES_EXCLUDED_NAME}
       AND NOT EXISTS (
           -- Migration 0014: with history preservation, a terminal row
           -- (completed/failed/expired/cancelled) must NOT block the
           -- push of a retry. We only block if there is a live execution
           -- (assigned/claimed/running) — the PARTIAL UNIQUE also
           -- guarantees uniqueness at the DB level. Previously the filter was
           -- `NOT IN (expired,cancelled,failed)` because the composite PK
           -- forced never having more than one row per (task, worker).
           SELECT 1 FROM task_executions ee
            WHERE ee.task_id = t.id
              AND ee.worker_id = :worker_id
              AND ee.status IN ('assigned', 'claimed', 'running')
       )
     ORDER BY t.priority DESC, t.created_at
     FOR UPDATE OF t SKIP LOCKED
     LIMIT 1
    """
)

_SQL_POLL_INSERT_PUSH_EXECUTION = text(
    """
    INSERT INTO task_executions
        (task_id, worker_id, status, lease_token, lease_expires_at, started_at,
         attempt_index, snapshot_epoch)
    VALUES (:task_id, :worker_id, 'claimed', gen_random_uuid(),
            now() + make_interval(secs => :lease_seconds), now(),
            COALESCE((SELECT MAX(attempt_index) FROM task_executions
                      WHERE task_id = :task_id AND worker_id = :worker_id), 0) + 1,
            (SELECT snapshot_epoch FROM tasks WHERE id = :task_id))
    RETURNING lease_token, lease_expires_at
    """
)

_SQL_POLL_MARK_DISPATCHING_PUSH = text(
    """
    UPDATE tasks SET status = 'dispatching'
     WHERE id = :task_id AND status = 'queued'
    """
)

# Step 5: all_available pre-assignment (same SQL as the old claim).
_SQL_POLL_CLAIM_PREASSIGNED = text(
    f"""
    WITH picked AS (
        SELECT e.task_id
          FROM task_executions e
          JOIN tasks t ON t.id = e.task_id
         WHERE e.worker_id = :worker_id
           AND e.status = 'assigned'
           -- Scoped to the worker's workspace; never deliver tasks of another tenant.
           AND t.workspace_id = :workspace_id
           AND t.status NOT IN (
                   'paused', 'cancelled', 'completed', 'failed'
               )
           -- A worker below the version floor does not claim a task that
           -- uses stored secrets. Its execution stays `assigned` (housekeeping
           -- expires it) and the worker moves on to the next pre-assigned one.
           AND (NOT CAST(:exclude_stored_secrets AS boolean)
                OR NOT {_TASK_USES_STORED_SECRETS})
           -- Names whose delivery was just refused in this poll.
           AND NOT {_TASK_DECLARES_EXCLUDED_NAME}
         ORDER BY t.priority DESC, e.assigned_at
         FOR UPDATE OF e SKIP LOCKED
         LIMIT 1
    )
    UPDATE task_executions e
       SET status = 'claimed',
           lease_token = gen_random_uuid(),
           lease_expires_at = now() + make_interval(secs => :lease_seconds),
           started_at = now()
      FROM picked
     WHERE e.task_id = picked.task_id
       AND e.worker_id = :worker_id
       -- Status re-filter: after a re-snapshot retry the worker also has its
       -- terminal execution from the previous epoch (the new snapshot inserts
       -- `assigned` with attempt_index+1 and keeps the history); without this
       -- predicate the UPDATE would flip BOTH rows to 'claimed' and would
       -- violate the partial unique `task_executions_active_uk`. The `picked`
       -- row is locked and is 'assigned'; the same index guarantees there is
       -- no other 'assigned' for the (task_id, worker_id) pair.
       AND e.status = 'assigned'
     RETURNING e.task_id, e.lease_token, e.lease_expires_at,
               (SELECT command FROM tasks WHERE id = e.task_id) AS command,
               (SELECT payload FROM tasks WHERE id = e.task_id) AS payload,
               (SELECT created_at FROM tasks WHERE id = e.task_id) AS created_at,
               (SELECT delivery_mode FROM tasks WHERE id = e.task_id) AS delivery_mode,
               (SELECT signed FROM tasks WHERE id = e.task_id) AS signed,
               (SELECT signature FROM tasks WHERE id = e.task_id) AS signature,
               (SELECT idempotency_key FROM tasks WHERE id = e.task_id) AS idempotency_key,
               (SELECT target_worker_id FROM tasks WHERE id = e.task_id) AS target_worker_id,
               (SELECT execution_timeout_seconds FROM tasks
                WHERE id = e.task_id) AS execution_timeout_seconds,
               (SELECT workspace_id::text FROM tasks WHERE id = e.task_id) AS workspace_id
    """
)

# Step 6: open-claim for target_mode='count' (no push, no pre-assignment).
#
# Concurrency (overclaim race fix): the claim must NOT count active
# `task_executions` and lock `tasks` in the same statement. Under READ COMMITTED
# the snapshot is frozen at the start of the statement, while
# `FOR UPDATE OF t SKIP LOCKED` locks the `tasks` row. The lock serializes the
# claimers on that row, but the count reads `task_executions`: a txn that
# acquires the lock right after another committed its claim would still count
# against a pre-commit snapshot → undercount → insert above `attempt_budget`.
# That is why we split into two statements inside the SAME txn:
#   (1) `_SQL_POLL_CLAIM_OPEN_LOCK` acquires the lock on the `tasks` row.
#   (2) `_SQL_POLL_CLAIM_OPEN_INSERT` recounts and inserts conditionally.
# Since all claimers lock the `tasks` row first, while one txn holds it the
# others SKIP; and the second statement, starting after acquiring the lock,
# takes a fresh snapshot that sees everything committed before → exact count
# and cap respected.


def _sql_budget_available(task_ref: str, budget_ref: str) -> str:
    """Predicate "the task still has budget": count(non-expired) < budget.

    SINGLE encoding of the set that consumes budget: every non-expired
    execution — active in flight + real terminal ones — counts against
    `attempt_budget`; only `expired` (worker crash) gives its slot back.
    Interpolates constants at build time (`task_ref`/`budget_ref` are literal
    SQL references, never input).
    """
    return (
        f"(SELECT count(*) FROM task_executions ee "
        f"WHERE ee.task_id = {task_ref} "
        f"AND ee.status <> '{EXPIRED_EXECUTION_STATUS}') < {budget_ref}"
    )


_SQL_POLL_CLAIM_OPEN_LOCK = text(
    f"""
    SELECT t.id, t.attempt_budget, t.command, t.payload, t.created_at,
           t.delivery_mode, t.signed, t.signature, t.idempotency_key,
           t.target_worker_id, t.execution_timeout_seconds,
           t.workspace_id::text AS workspace_id
      FROM tasks t
     WHERE t.status IN ('queued', 'dispatching')
       AND t.target_mode = 'count'
       AND t.delivery_mode = 'claim'
       AND t.run_at <= now()
       AND (t.expires_at IS NULL OR t.expires_at > now())
       -- Scoped to the worker's workspace; never claim tasks of another tenant.
       AND t.workspace_id = :workspace_id
       -- Budget pre-filter. Without it, a full-but-open task (budget
       -- exhausted, waiting for an in-flight execution to close it) was ALWAYS
       -- the LIMIT 1 candidate by priority, the INSERT (2) rejected it and the
       -- handler returned null without trying the next one → starvation of
       -- lower-priority tasks for as long as the longest execution lasted.
       -- A stale count here is harmless: it only affects the candidate
       -- SELECTION; the cap is enforced by (2) with a fresh post-lock
       -- snapshot.
       AND {_sql_budget_available("t.id", "t.attempt_budget")}
       -- Real matching of `eligibility.capabilities` against
       -- `workers.capabilities`. It goes in the SELECTION's WHERE and not
       -- afterwards, for the same reason as the budget pre-filter: with
       -- `ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED`, discarding the already
       -- chosen candidate would return null without trying the next task.
       AND {_POLLING_WORKER_SATISFIES_TASK}
       -- A worker below the version floor does not claim a task that uses
       -- stored secrets; it moves on to the next candidate (same reason as the
       -- budget and capabilities pre-filters: filter in the selection).
       AND (NOT CAST(:exclude_stored_secrets AS boolean)
            OR NOT {_TASK_USES_STORED_SECRETS})
       -- Names whose delivery was just refused in this poll.
       AND NOT {_TASK_DECLARES_EXCLUDED_NAME}
       -- Fairness yield bounded to the candidate task's pool
       -- (workers outside the pool are not eligible → must not force a yield).
       AND NOT EXISTS (
            SELECT 1 FROM workers other
             WHERE other.id <> :worker_id
               AND other.status = 'online'
               AND other.deleted_at IS NULL
               -- The fairness yield only applies to workers of the SAME
               -- workspace. Without this, a worker would yield to workers of
               -- another tenant that can never claim this task → cross-tenant
               -- starvation (the worker yields forever).
               AND other.workspace_id = :workspace_id
               AND other.last_heartbeat_at > now() - interval '60 seconds'
               AND (
                    t.target_worker_pool IS NULL
                    OR cast(other.id AS text) IN (
                        SELECT jsonb_array_elements_text(t.target_worker_pool)
                    )
               )
               -- The fairness yield only applies to workers that can
               -- really compete NOW. A worker with an active execution
               -- (assigned/claimed/running) is BUSY and will not claim
               -- while it runs; if it also has the oldest assigned_at
               -- (e.g. a task that runs for hours) it would block all idle
               -- workers forever → deadlock by priority inversion. Same as
               -- pool and workspace: whoever cannot claim must not force a
               -- yield.
               --
               -- There are TWO reasons to exclude, and the active execution alone
               -- is not enough:
               --   (1) active execution: the server KNOWS it is busy.
               --   (2) self-reported unavailability (migration
               --       0053): the worker says `busy` in its poll WITHOUT an execution.
               -- Case (2) appeared with the disk gate (a
               -- worker below the floor reports `busy` to stop claiming) and
               -- already existed, bounded, with the `clear_cache` lane
               -- (`busy` without an execution while the flush lasts). That worker
               -- keeps `status='online'` (this same poll statement forces
               -- it), heartbeats every 5 s and its `max(assigned_at)` only
               -- ages, so it passed every leg of the predicate and
               -- forced the fleet to yield, while it never claimed itself
               -- (steps 5 and 6 of the pick only run with `idle`): the tasks
               -- stayed `queued` with all workers idle and no error.
               --
               -- `IS DISTINCT FROM` and not `<>` on purpose: `NULL` means
               -- "this worker has not polled yet since the 0053 deploy, we
               -- do not know what it reports", and `NULL IS DISTINCT FROM 'busy'` is
               -- TRUE, so it is NOT excluded and the yield behaves as before
               -- the migration. With `<>` the NULL would make the predicate
               -- unknown and would exclude the whole pre-existing fleet,
               -- changing behaviour at the same deploy.
               AND NOT EXISTS (
                    SELECT 1 FROM task_executions busy
                     WHERE busy.worker_id = other.id
                       AND busy.status IN ('assigned', 'claimed', 'running')
               )
               AND other.last_reported_status IS DISTINCT FROM 'busy'
               -- Server-side disk gate:
               -- third reason to exclude from the yield. When the gated
               -- worker sends the fraction, that worker will report `idle`
               -- (not `busy`): the `last_reported_status='busy'` exclusion
               -- above would NO longer cover it and would reopen the deadlock
               -- above (an idle gated worker, with the oldest `max(assigned_at)`,
               -- would force the fleet to yield a claim that it never takes
               -- because the server closes steps 4-6 for it). We exclude it by its
               -- disk fraction: if it is below the central threshold, it
               -- cannot claim NOW and must not force a yield. `IS NOT NULL`
               -- keeps the semantics NULL = "we do not know" = do not exclude,
               -- like the 0053 exclusion above.
               AND NOT (
                    other.free_disk_fraction IS NOT NULL
                    AND other.free_disk_fraction < :claim_min_free_fraction
               )
               -- Fourth reason to exclude from the yield. A
               -- worker below the version floor never claims a task
               -- that uses stored secrets (the filter above removes it from
               -- the selection). If it forced the yield with an older
               -- `max(assigned_at)`, the capable worker would yield forever a task that
               -- the other never takes: the same deadlock as above. The version is the
               -- stored one, which the poll updates before the pick. The
               -- version check goes first: it is cheap and, for a capable `other`,
               -- avoids the EXISTS on `task_secrets`.
               AND NOT (
                    NOT {_OTHER_WORKER_SUPPORTS_TASK_SECRETS}
                    AND {_TASK_USES_STORED_SECRETS}
               )
               -- Fifth reason to exclude from the yield. A worker whose
               -- secrets delivery the server just refused (its
               -- claim was undone: no execution remains and its `max(assigned_at)`
               -- does not advance) is not yielded a task that uses stored secrets:
               -- it would remain the "least recently assigned" and the others would
               -- yield forever tasks that it cannot receive (the
               -- same deadlock). The mark is written by its own poll and expires
               -- by itself; NULL = never refused = do not exclude.
               AND NOT (
                    {_OTHER_WORKER_RECENTLY_REFUSED}
                    AND {_TASK_USES_STORED_SECRETS}
               )
               AND COALESCE(
                     (SELECT max(assigned_at) FROM task_executions
                       WHERE worker_id = other.id),
                     '-infinity'::timestamptz
                   ) <
                   COALESCE(
                     (SELECT max(assigned_at) FROM task_executions
                       WHERE worker_id = :worker_id),
                     '-infinity'::timestamptz
                   )
       )
       AND NOT EXISTS (
            -- Migration 0014: the worker stays eligible for re-claim
            -- if its previous rows are all terminal. The PARTIAL
            -- UNIQUE in the DB prevents corruption if there is a race.
            SELECT 1 FROM task_executions ee
             WHERE ee.task_id = t.id
               AND ee.worker_id = :worker_id
               AND ee.status IN ('assigned', 'claimed', 'running')
       )
       -- If the task has a restricted pool the worker must belong to it.
       -- NULL = no restriction (N / all-available syntax).
       -- cast(... AS text) is used instead of "double-colon-text" to
       -- avoid the SQLAlchemy lexer clash between bind and type-cast.
       AND (
            t.target_worker_pool IS NULL
            OR cast(:worker_id AS text) IN (
                SELECT jsonb_array_elements_text(t.target_worker_pool)
            )
       )
     ORDER BY t.priority DESC, t.run_at
     FOR UPDATE OF t SKIP LOCKED
     LIMIT 1
    """
)

# Statement (2): with the `tasks` row already locked by statement (1) in the
# same txn, we recount the budget under a fresh snapshot (it sees the committed
# claims) and insert only if there is still a slot. The predicate is the same
# `_sql_budget_available` as in statement (1): active (in flight) +
# real terminal executions consume budget against `attempt_budget` (born =
# target_count and each retry bumps it +1); only `expired` gives its slot back.
# The terminal ones that count here are the same ones `MAYBE_CLOSE_TASK` uses
# for the count-mode failure threshold (terminal_non_expired >= attempt_budget)
# — without that, a staggered `completed` freed the slot before the close and the
# fast worker slipped in with an extra attempt. The claim/close symmetry
# is total: both exclude `expired`, so a crash gives its slot back without
# closing the task prematurely. `target_count` (nominal fan-out) does not
# take part in the claim: it is the close's success threshold.
# `snapshot_epoch` is stamped from the task, the same single source as the
# `all_available` snapshot (`admin_tasks.py::_SQL_INSERT_SNAPSHOT*`): the
# execution belongs to the task's CURRENT epoch. For native count it is a
# no-op (those tasks never bump the epoch: the bump lives in the
# `all_available` re-snapshot), so the DEFAULT 0 used to be enough. It stops being
# enough with the sticky conversion: a converted task ended up in count with
# `snapshot_epoch > 0`, and its close counts the current epoch; without
# stamping, the post-conversion re-claim would be born in epoch 0 and would not
# be counted.
_SQL_POLL_CLAIM_OPEN_INSERT = text(
    f"""
    INSERT INTO task_executions
        (task_id, worker_id, status, lease_token, lease_expires_at, started_at,
         attempt_index, snapshot_epoch)
    SELECT :task_id, :worker_id, 'claimed', gen_random_uuid(),
           now() + make_interval(secs => :lease_seconds), now(),
           COALESCE((SELECT MAX(attempt_index) FROM task_executions
                     WHERE task_id = :task_id AND worker_id = :worker_id), 0) + 1,
           (SELECT snapshot_epoch FROM tasks WHERE id = :task_id)
     WHERE {_sql_budget_available(":task_id", ":attempt_budget")}
    RETURNING task_id, lease_token, lease_expires_at
    """
)

_SQL_POLL_MARK_DISPATCHING = text(
    """
    UPDATE tasks SET status = 'dispatching'
     WHERE id = :task_id AND status = 'queued'
    """
)


_SQL_UPDATE_TASK_SIGNATURE = text(
    """
    UPDATE tasks SET signed = true, signature = :signature
     WHERE id = :task_id
    """
)

# Control lane: disk cache flush.
# Detects whether there is a pending, unsealed flush: requested_at not null and
# done_at null or older than the last request (allows re-requesting).
_SQL_POLL_PICK_CACHE_FLUSH = text(
    """
    SELECT 1 FROM workers
     WHERE id = :worker_id
       AND cache_flush_requested_at IS NOT NULL
       AND (cache_flush_done_at IS NULL OR cache_flush_done_at < cache_flush_requested_at)
    """
)

# Seals the flush: records when the worker completed the cleanup.
_SQL_POLL_ACK_CACHE_FLUSH = text(
    """
    UPDATE workers SET cache_flush_done_at = now()
     WHERE id = :worker_id
       AND cache_flush_requested_at IS NOT NULL
       AND (cache_flush_done_at IS NULL OR cache_flush_done_at < cache_flush_requested_at)
    """
)


async def _build_task_payload(
    row: dict,
    worker_id: str,
    session: AsyncSession,
    *,
    secrets_capable: bool,
    worker_version: str | None,
    busy: bool,
) -> PollTaskPayload:
    """Build PollTaskPayload from a claim/push row.

    `secrets_capable` says whether `worker_version` reaches the secrets floor
    (`orcaster_server/task_secrets/gate.py`) and `busy` whether the poll arrived
    as `busy`. The picks already exclude the tasks that use stored secrets for a
    worker below the floor and, for a push, in a `busy` poll: what arrives here
    in those cases declares no stored secret and resolves `{}` without an
    audit. See `_resolve_task_secrets` for the races that remain.

    **Universal signature**: EVERY delivered task carries `signed=true`
    and a non-null `signature`. For tasks with a `target_worker_id` set
    (push), the signature stored at INSERT was already computed with that UUID
    and the polling worker is necessarily the same one, so the signature
    can be used as is. For CLAIM tasks (`target_worker_id IS NULL`),
    the INSERT signed with a placeholder `worker_id=""`; here we re-sign
    with the UUID of the polling worker (who is the one that will receive the
    task) and persist the new signature. The row is left with the final
    signature, which is important because housekeeping could reclaim the lease
    and another worker re-claim it — in that case, this same path will re-sign
    for the new polling worker.

    `created_at` travels to the client as ISO with a "Z" suffix (Pydantic v2);
    so that the reconstructible signature matches byte for byte, we sign
    over the same representation.
    """
    # Secrets first. If a declared one cannot be decrypted it raises
    # `UndecryptableTaskSecrets` before re-signing: the caller undoes the claim
    # in its savepoint and the task stays `queued`, without an execution.
    resolved = await _resolve_task_secrets(
        row,
        worker_id,
        session,
        secrets_capable=secrets_capable,
        worker_version=worker_version,
        busy=busy,
    )

    target_wid_raw = row.get("target_worker_id")
    raw_sig = row.get("signature")

    if target_wid_raw is None:
        # CLAIM task: re-sign with the UUID of the polling worker.
        created_at = row["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        created_at_iso = created_at.isoformat().replace("+00:00", "Z")
        idem = row.get("idempotency_key")
        priv = get_private_key()
        sig_bytes = sign_task(
            priv,
            task_id=row["task_id"],
            worker_id=worker_id,
            command=row["command"] or "run_batch",
            payload=row["payload"] or {},
            idempotency_key=str(idem) if idem else None,
            created_at_iso=created_at_iso,
        )
        # Persist the re-computed signature (key: housekeeping + re-claim).
        await session.execute(
            _SQL_UPDATE_TASK_SIGNATURE,
            {"signature": sig_bytes, "task_id": row["task_id"]},
        )
        sig_b64 = signature_to_b64(sig_bytes)
    else:
        # PUSH task: the INSERT signature already carries the target_worker_id.
        assert raw_sig is not None, "universal signature: every task row must have signature"
        sig_b64 = signature_to_b64(bytes(raw_sig))

    return PollTaskPayload(
        task_id=row["task_id"],
        command=row.get("command"),
        payload=row["payload"] or {},
        created_at=row["created_at"],
        delivery_mode=row.get("delivery_mode") or "claim",
        signed=True,
        signature=sig_b64,
        idempotency_key=row.get("idempotency_key"),
        worker_id=worker_id,
        target_worker_id=row.get("target_worker_id"),
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        execution_timeout_seconds=row.get("execution_timeout_seconds"),
        secrets=resolved.values,
        worker_scoped_secrets=resolved.worker_scoped,
    )


_NO_SECRETS = task_secrets.ResolvedSecrets(values={}, worker_scoped=[])


async def _resolve_task_secrets(
    row: dict,
    worker_id: str,
    session: AsyncSession,
    *,
    secrets_capable: bool,
    worker_version: str | None,
    busy: bool,
) -> task_secrets.ResolvedSecrets:
    """Secrets to deliver with the task: `payload.task_env` ∩ secrets applicable to the worker.

    Applicable: those of the workspace and the worker's own, which win
    by name; never those of another worker, neither to deliver nor to fail.

    `{}` if the task does not declare `task_env` (legacy mode); in that case
    the table is not queried. The workspace is the one on the task row (the three
    pick SELECTs already bound it to the worker's). With secrets, it audits the
    delivery (`deliver_task_secrets`, with the names that came out of a row of
    the worker) in the same transaction as the claim. It never records values.

    With the store not configured, `{}` unless some declared name has a stored
    secret: then it raises `TaskSecretsKeysMissing` and the poll leaves the task
    `queued`, instead of delivering it without that secret.

    With `secrets_capable=False` (worker below the version floor)
    it never decrypts, never delivers and never audits: that worker ignores `secrets`.
    The pick already excludes the tasks that use stored secrets, so a task with
    a stored secret only gets here if it was set between the pick and this
    point; that case is logged with a `WARNING` (names only) and the task goes
    out without secrets, as it did before stored secrets existed.

    With `busy=True` (a push picked in a `busy` poll) it also does not decrypt,
    deliver or audit, even if the worker is capable: the worker discards what it
    receives while `busy`, and auditing that delivery would record one that had
    no effect. The pick already excludes, in `busy`, the pushes that use stored
    secrets; if one was set between the pick and this point, a `WARNING`
    `task_secrets.busy_push` (names only) remains and the push goes out with `{}`.
    """
    names = task_secrets.declared_task_env(row.get("payload"))
    if not names:
        return _NO_SECRETS
    workspace_id = str(row["workspace_id"])
    if not secrets_capable:
        stored = await task_secrets.stored_names(
            session, workspace_id=workspace_id, worker_id=worker_id, names=names
        )
        if stored:
            logger.warning(
                "task_secrets.worker_unsupported task_id=%s worker_id=%s version=%s "
                "min_version=%s names=%s: a secret was stored after the task was picked; "
                "delivering the task without stored secrets because this worker ignores them",
                row["task_id"],
                worker_id,
                worker_version,
                TASK_SECRETS_MIN_WORKER_VERSION_STR,
                ",".join(stored),
            )
        return _NO_SECRETS
    if busy:
        stored = await task_secrets.stored_names(
            session, workspace_id=workspace_id, worker_id=worker_id, names=names
        )
        if stored:
            logger.warning(
                "task_secrets.busy_push task_id=%s worker_id=%s names=%s: a secret was stored "
                "after the push was picked on a busy poll; delivering it without secrets, "
                "the worker discards pushes received while busy",
                row["task_id"],
                worker_id,
                ",".join(stored),
            )
        return _NO_SECRETS
    ring = task_secrets_keyring.get_keyring()
    if ring is None:
        stored = await task_secrets.stored_names(
            session, workspace_id=workspace_id, worker_id=worker_id, names=names
        )
        if stored:
            raise task_secrets.TaskSecretsKeysMissing(stored)
        return _NO_SECRETS
    resolved = await task_secrets.resolve_for_task(
        session, ring, workspace_id=workspace_id, worker_id=worker_id, names=names
    )
    if resolved.values:
        await task_secrets.record_delivery(
            session,
            workspace_id=workspace_id,
            task_id=row["task_id"],
            worker_id=worker_id,
            names=resolved.values.keys(),
            worker_scoped=resolved.worker_scoped,
        )
    return resolved


# Queued tasks that use stored secrets: open claims of its workspace and pushes
# targeted at this worker, already dispatchable (`run_at` in the past) and not
# expired. It is an upper bound for the operator, not what the worker would take:
# it applies no pool, capabilities or attempt budget. The pre-assigned ones of
# `all_available` do not count (they are `dispatching`, and the snapshot already
# leaves this worker out). It only counts; it neither reads nor names secrets.
_SQL_COUNT_PENDING_STORED_SECRET_TASKS = text(
    f"""
    SELECT count(*)
      FROM tasks t
     WHERE t.workspace_id = :workspace_id
       AND t.status = 'queued'
       AND t.run_at <= now()
       AND (t.expires_at IS NULL OR t.expires_at > now())
       AND (t.delivery_mode <> 'push' OR t.target_worker_id = :worker_id)
       AND {_TASK_USES_STORED_SECRETS}
    """
)

# Window of the `WARNING` `task_secrets.worker_unsupported` per worker, in
# monotonic clock seconds. In-process state: with several processes each one
# warns on its own, which is enough for an operational warning. Module constant
# so tests can adjust it or clear the dict. The handler deletes the key when the
# worker reaches the floor, and the dict is cleared entirely if it exceeds
# `_UNSUPPORTED_WORKER_MAX_ENTRIES` (worker identity rotation): at most it
# brings a warning forward.
UNSUPPORTED_WORKER_WARN_INTERVAL_SECONDS = 600.0
_UNSUPPORTED_WORKER_MAX_ENTRIES = 10_000
_unsupported_worker_last_check: dict[str, float] = {}


async def _warn_unsupported_worker(
    session: AsyncSession, worker: Worker, worker_version: str | None
) -> None:
    """One `WARNING` per window if there are tasks with secrets this worker does not receive.

    The window mark is set when querying, whether or not there are tasks, so that
    an old worker without tasks with secrets does not query on every poll either.
    It never records secret names or values. The caller invokes it after the poll
    commit and catches any error: it is diagnostic, it cannot take the poll down.
    """
    key = str(worker.id)
    now = time.monotonic()
    last = _unsupported_worker_last_check.get(key)
    if last is not None and now - last < UNSUPPORTED_WORKER_WARN_INTERVAL_SECONDS:
        return
    if len(_unsupported_worker_last_check) >= _UNSUPPORTED_WORKER_MAX_ENTRIES:
        _unsupported_worker_last_check.clear()
    _unsupported_worker_last_check[key] = now
    queued = (
        await session.execute(
            _SQL_COUNT_PENDING_STORED_SECRET_TASKS,
            {"workspace_id": worker.workspace_id, "worker_id": worker.id},
        )
    ).scalar_one()
    if queued:
        logger.warning(
            "task_secrets.worker_unsupported worker_id=%s version=%s min_version=%s "
            "queued_tasks=%d: tasks that declare stored secrets are not delivered to this "
            "worker; upgrade it",
            worker.id,
            worker_version,
            TASK_SECRETS_MIN_WORKER_VERSION_STR,
            queued,
        )


# Window of the `ERROR` `task_secrets.keys_missing` / `task_secrets.undecryptable`
# per worker and kind, in monotonic clock seconds. Same pattern as
# `_warn_unsupported_worker`: per-process state, module constant so tests can
# adjust it, and the dict is cleared entirely when it passes its cap.
REFUSAL_LOG_INTERVAL_SECONDS = 600.0
_REFUSAL_LOG_MAX_ENTRIES = 10_000
_refusal_last_logged: dict[tuple[str, str], float] = {}

_REFUSAL_ACTION = {
    "keys_missing": (
        "TASK_SECRETS_KEYS_PATH is not set; set it to the keys file and restart the server"
    ),
    "undecryptable": (
        "no configured key decrypts these secrets; restore the key that encrypted them, "
        "or set the secret again or delete it"
    ),
}


class _Refusal(NamedTuple):
    """Task the poll did not deliver: a declared secret is undecryptable or keys are missing."""

    task_id: int
    exc: task_secrets.UndecryptableTaskSecrets


@dataclass
class _Attempt:
    """Result of a delivery attempt inside its savepoint."""

    payload: PollTaskPayload | None = None
    refusal: _Refusal | None = None


async def _report_refusal(
    session: AsyncSession,
    worker: Worker,
    task_id: int,
    exc: task_secrets.UndecryptableTaskSecrets,
) -> None:
    """Record the refusal for the dashboard and, rate-limited, an ERROR.

    Called after the commit. It never raises: an error in the queued-task count
    is only logged and the ERROR goes out with `queued_tasks=unknown`. Secret
    names only; never values.
    """
    kind = (
        "keys_missing" if isinstance(exc, task_secrets.TaskSecretsKeysMissing) else "undecryptable"
    )
    names = list(exc.names)
    task_secrets_blocked.record_refusal(
        workspace_id=str(worker.workspace_id),
        worker_id=str(worker.id),
        task_id=task_id,
        kind=kind,
        names=names,
    )
    key = (str(worker.id), kind)
    now = time.monotonic()
    last = _refusal_last_logged.get(key)
    if last is not None and now - last < REFUSAL_LOG_INTERVAL_SECONDS:
        return
    if len(_refusal_last_logged) >= _REFUSAL_LOG_MAX_ENTRIES:
        _refusal_last_logged.clear()
    _refusal_last_logged[key] = now
    queued: int | str
    try:
        queued = (
            await session.execute(
                _SQL_COUNT_PENDING_STORED_SECRET_TASKS,
                {"workspace_id": worker.workspace_id, "worker_id": worker.id},
            )
        ).scalar_one()
    except Exception:
        logger.exception("task_secrets.refusal_count_failed worker_id=%s", worker.id)
        queued = "unknown"
    logger.error(
        "task_secrets.%s worker_id=%s task_id=%s names=%s queued_tasks=%s: the task was left "
        "queued and the delivery was refused; %s",
        kind,
        worker.id,
        task_id,
        ",".join(names),
        queued,
        _REFUSAL_ACTION[kind],
    )


# Savepoint of the claim inside the poll transaction. It only covers steps 4-6
# and the payload: the heartbeat, the lease, the cancellations and the cache ack
# are committed regardless.
_CLAIM_SAVEPOINT = "poll_claim"

# How many times a single poll picks again excluding the refused names before
# excluding every task that uses stored secrets. It bounds the work of a poll
# when there are many distinct names that cannot be decrypted.
_MAX_NAME_EXCLUSION_PICKS = 3

# Mark of the last secrets refusal for this worker; read by the fairness-yield
# of `_SQL_POLL_CLAIM_OPEN_LOCK` (migration 0060). The mark is rewritten at most
# once per `TASK_SECRETS_REFUSAL_MARK_INTERVAL_SECONDS` per worker (half of the
# yield window): a worker that keeps being refused does not generate a dead
# version of its row on every poll. The handler remembers, per process and with a
# monotonic clock, when it last wrote each worker's mark
# (`_refusal_mark_written`, only if the UPDATE matched a row) and does not even
# emit the statement before the interval; the WHERE repeats the condition against
# Postgres' clock in case another process has just written it. The server's clock
# is not compared with the database's: a skew between them would skip renewals.
TASK_SECRETS_REFUSAL_MARK_INTERVAL_SECONDS = TASK_SECRETS_REFUSAL_WINDOW_SECONDS // 2
_SQL_POLL_MARK_SECRETS_REFUSED = text(
    """
    UPDATE workers SET task_secrets_refused_at = now()
     WHERE id = :worker_id
       AND (task_secrets_refused_at IS NULL
            OR task_secrets_refused_at < now() - make_interval(secs => :min_interval))
    """
)

# Short, per-process memory of each worker's last refusal. While it lasts, that
# worker's poll goes straight to the pick that excludes what cannot be delivered
# (all tasks with stored secrets if the keys are missing; those that declare the
# undecryptable names if not) and skips the claim, refusal and rollback round,
# which with a long queue of blocked tasks would repeat on every poll, also in
# those that deliver another task and come back within a second. When it
# expires, the next poll tries the normal pick again: that is how it detects that
# the secret was set again or deleted, with a delay of at most
# `REFUSAL_MEMORY_SECONDS`. A change of the keys file is checked on every poll
# with memory (one `stat`), and discards it. At 0 the memory is off.
# Module constant so tests can adjust it; the dict is cleared entirely when it
# passes its cap, which at most brings a trial round forward.
REFUSAL_MEMORY_SECONDS = 30.0
_REFUSAL_MEMORY_MAX_ENTRIES = 10_000


class _RefusalMemory(NamedTuple):
    """What a worker's last refusal left undeliverable."""

    expires: float
    keys_missing: bool
    names: frozenset[str]
    exclude_all: bool


_refusal_memory: dict[str, _RefusalMemory] = {}
_refusal_mark_written: dict[str, float] = {}


def _recall_refusal(worker_id: str) -> _RefusalMemory | None:
    """The worker's current memory, or `None`; discards an expired one."""
    memory = _refusal_memory.get(worker_id)
    if memory is None:
        return None
    if time.monotonic() >= memory.expires:
        del _refusal_memory[worker_id]
        return None
    return memory


def _remember_refusal(worker_id: str, memory: _RefusalMemory) -> None:
    if len(_refusal_memory) >= _REFUSAL_MEMORY_MAX_ENTRIES:
        _refusal_memory.clear()
    _refusal_memory[worker_id] = memory


async def _pick_and_build(
    session: AsyncSession,
    worker: Worker,
    body: PollRequest,
    *,
    secrets_capable: bool,
    worker_version: str | None,
    exclude_stored_secrets: bool,
    exclude_secret_names: list[str],
) -> _Attempt:
    """Steps 4-6 of the poll (push, pre-assigned, open claim) and the payload of the pick.

    `exclude_stored_secrets` excludes, in the three picks, the tasks that use
    stored secrets (second attempt after a refusal). A worker below the floor
    and a push in `busy` also exclude them.
    `exclude_secret_names` additionally excludes those that declare any of those
    names (the ones of a delivery just refused).
    """
    busy = body.status == "busy"
    task_row: dict | None = None
    picked = (
        (
            await session.execute(
                _SQL_POLL_PICK_PUSH,
                {
                    "worker_id": worker.id,
                    "workspace_id": worker.workspace_id,
                    "exclude_stored_secrets": exclude_stored_secrets
                    or (not secrets_capable)
                    or busy,
                    "exclude_secret_names": exclude_secret_names,
                },
            )
        )
        .mappings()
        .first()
    )
    if picked is not None:
        ins = (
            (
                await session.execute(
                    _SQL_POLL_INSERT_PUSH_EXECUTION,
                    {
                        "task_id": picked["id"],
                        "worker_id": worker.id,
                        "lease_seconds": body.lease_seconds,
                    },
                )
            )
            .mappings()
            .first()
        )
        assert ins is not None
        await session.execute(_SQL_POLL_MARK_DISPATCHING_PUSH, {"task_id": picked["id"]})
        task_row = {
            "task_id": picked["id"],
            "command": picked["command"],
            "payload": picked["payload"],
            "created_at": picked["created_at"],
            "delivery_mode": picked["delivery_mode"],
            "signed": picked["signed"],
            "signature": picked["signature"],
            "idempotency_key": picked["idempotency_key"],
            "target_worker_id": picked["target_worker_id"],
            "execution_timeout_seconds": picked.get("execution_timeout_seconds"),
            "workspace_id": picked["workspace_id"],
            "lease_token": ins["lease_token"],
            "lease_expires_at": ins["lease_expires_at"],
        }

    # 5. all_available pre-assignment (only if idle).
    if task_row is None and body.status == "idle":
        pre = (
            (
                await session.execute(
                    _SQL_POLL_CLAIM_PREASSIGNED,
                    {
                        "worker_id": worker.id,
                        "lease_seconds": body.lease_seconds,
                        "workspace_id": worker.workspace_id,
                        "exclude_stored_secrets": (not secrets_capable) or exclude_stored_secrets,
                        "exclude_secret_names": exclude_secret_names,
                    },
                )
            )
            .mappings()
            .first()
        )
        if pre is not None:
            task_row = dict(pre)

    # 6. Open-claim count (only if idle, nothing before). Two statements
    # in the same txn: lock the task row, then recount+insert
    # with a fresh snapshot (see the note in _SQL_POLL_CLAIM_OPEN_LOCK).
    if task_row is None and body.status == "idle":
        locked = (
            (
                await session.execute(
                    _SQL_POLL_CLAIM_OPEN_LOCK,
                    {
                        "worker_id": worker.id,
                        "workspace_id": worker.workspace_id,
                        # Disk-gate threshold for the fairness-yield
                        # exclusion term (see the SQL).
                        "claim_min_free_fraction": (app_settings.worker_claim_min_free_fraction),
                        "exclude_stored_secrets": (not secrets_capable) or exclude_stored_secrets,
                        "exclude_secret_names": exclude_secret_names,
                    },
                )
            )
            .mappings()
            .first()
        )
        if locked is not None:
            ins = (
                (
                    await session.execute(
                        _SQL_POLL_CLAIM_OPEN_INSERT,
                        {
                            "task_id": locked["id"],
                            "worker_id": worker.id,
                            "lease_seconds": body.lease_seconds,
                            "attempt_budget": locked["attempt_budget"],
                        },
                    )
                )
                .mappings()
                .first()
            )
            if ins is not None:
                task_row = {
                    "task_id": ins["task_id"],
                    "lease_token": ins["lease_token"],
                    "lease_expires_at": ins["lease_expires_at"],
                    "command": locked["command"],
                    "payload": locked["payload"],
                    "created_at": locked["created_at"],
                    "delivery_mode": locked["delivery_mode"],
                    "signed": locked["signed"],
                    "signature": locked["signature"],
                    "idempotency_key": locked["idempotency_key"],
                    "target_worker_id": locked["target_worker_id"],
                    "execution_timeout_seconds": locked["execution_timeout_seconds"],
                    "workspace_id": locked["workspace_id"],
                }
                await session.execute(_SQL_POLL_MARK_DISPATCHING, {"task_id": ins["task_id"]})

    if task_row is None:
        return _Attempt()
    try:
        payload = await _build_task_payload(
            task_row,
            str(worker.id),
            session,
            secrets_capable=secrets_capable,
            worker_version=worker_version,
            busy=busy,
        )
    except task_secrets.UndecryptableTaskSecrets as exc:
        return _Attempt(refusal=_Refusal(task_id=task_row["task_id"], exc=exc))
    return _Attempt(payload=payload)


async def _attempt_delivery(
    session: AsyncSession,
    worker: Worker,
    body: PollRequest,
    *,
    secrets_capable: bool,
    worker_version: str | None,
    exclude_stored_secrets: bool,
    exclude_secret_names: list[str] | None = None,
) -> _Attempt:
    """One delivery attempt inside a savepoint.

    If the payload cannot be built because a secret cannot be decrypted, the
    savepoint is rolled back: no execution remains, no attempt is consumed and
    the task stays `queued`. The SQL is raw on purpose: an ORM `begin_nested()`
    would expire the `worker` objects the handler uses afterwards.
    """
    await session.execute(text(f"SAVEPOINT {_CLAIM_SAVEPOINT}"))
    attempt = await _pick_and_build(
        session,
        worker,
        body,
        secrets_capable=secrets_capable,
        worker_version=worker_version,
        exclude_stored_secrets=exclude_stored_secrets,
        exclude_secret_names=exclude_secret_names or [],
    )
    if attempt.refusal is not None:
        await session.execute(text(f"ROLLBACK TO SAVEPOINT {_CLAIM_SAVEPOINT}"))
    await session.execute(text(f"RELEASE SAVEPOINT {_CLAIM_SAVEPOINT}"))
    return attempt


def _poll_capabilities(raw: object, worker_id: object) -> dict | None:
    """Poll capabilities to write, or None to leave the stored value untouched.

    Same validator as `register` (`validate_worker_capabilities`), but without
    a 422: the poll is a hot path (see `PollRequest._normalize_version`). A 422
    here is not seen as "invalid field": the worker retries with backoff against
    a body that will never change and stays offline forever. An invalid value
    is ignored with a warning and the stored one is kept, which passed the
    validation of the register or of an earlier poll. It does not fall back to
    `{}`: that would take away capabilities the worker did have because of a
    typo in the `.env`.
    """
    if raw is None:
        return None
    try:
        return validate_worker_capabilities(raw)
    except EligibilityError as exc:
        logger.warning(
            "poll: worker %s sent invalid capabilities, keeping the stored value: %s",
            worker_id,
            exc,
        )
        return None


@router.post(
    "/{worker_id}/poll",
    response_model=PollResponse,
    status_code=status.HTTP_200_OK,
)
async def worker_poll(
    body: PollRequest,
    worker: WorkerDep,
    session: SessionDep,
) -> PollResponse:
    # 1. Lease housekeeping: MOVED to the global background job.
    # The poll used to reclaim expired leases and expire expired assigned at the
    # start of each request, which operationally coupled the tenants (the poll
    # of A triggered the housekeeping of B). Now that lives only in
    # `housekeeping_loop` (`orcaster_server/housekeeping.py`, started in
    # `main.py`), which runs every `housekeeping_interval_seconds`. Accepted
    # trade-off: a freshly expired lease takes up to that interval to become
    # re-claimable, instead of being released in the same poll. The claim below
    # counts as `taken` every active execution (including an expired lease not
    # yet expired by the job), so it does not hand out extra slots while the
    # loop has not run.

    # 2. Worker update. The *_set flags avoid overwriting with NULL
    # when the field is not sent (asyncpg does not infer the type if :v IS NULL).
    capabilities = _poll_capabilities(body.capabilities, worker.id)
    worker_row = (
        await session.execute(
            _SQL_POLL_UPDATE_WORKER,
            {
                "worker_id": worker.id,
                "last_reported_status": body.status,
                "ram_used_mb": body.ram_used_mb,
                "ram_used_mb_set": body.ram_used_mb is not None,
                "disk_used_mb": body.disk_used_mb,
                "disk_used_mb_set": body.disk_used_mb is not None,
                "free_disk_fraction": body.free_disk_fraction,
                "cpu_pct": body.cpu_pct,
                "cpu_pct_set": body.cpu_pct is not None,
                "ram_mb": body.ram_mb,
                "ram_mb_set": body.ram_mb is not None,
                "disk_mb": body.disk_mb,
                "disk_mb_set": body.disk_mb is not None,
                "name": body.name,
                "name_set": body.name is not None,
                "version": body.version,
                "version_set": body.version is not None,
                "capabilities": json.dumps(capabilities) if capabilities is not None else None,
                "capabilities_set": capabilities is not None,
            },
        )
    ).first()
    new_status = worker_row[0] if worker_row else worker.status

    # Does this worker read `secrets`? Same version the UPDATE above leaves:
    # the body's if sent, otherwise the stored one (`worker` was loaded before
    # the UPDATE). Without a version or with one that does not parse, it fails
    # closed: it does not receive tasks that use stored secrets.
    worker_version = body.version or worker.version
    secrets_capable = worker_supports_task_secrets(worker_version)

    # 2.5. Implicit lease renewal. If the worker reports
    # `busy` with its `lease_token`, we refresh the lease of ITS live execution.
    # It runs after the housekeeping reclaim (step 1): an already expired lease was
    # marked `expired` and this UPDATE does not match it — the worker that let
    # its lease lapse lost it, intentional behaviour.
    if body.status == "busy" and body.lease_token is not None:
        await session.execute(
            _SQL_POLL_RENEW_LEASE,
            {
                "worker_id": worker.id,
                "lease_token": body.lease_token,
                "lease_seconds": body.lease_seconds,
                "progress": body.current_execution_progress,
            },
        )

    # 3. Cancel signals: atomic delivery + mark.
    cancel_rows = (await session.execute(_SQL_POLL_CANCEL_SIGNALS, {"worker_id": worker.id})).all()
    cancel_signals = [PollCancelSignal(task_id=r[0], lease_token=r[1]) for r in cancel_rows]

    # Control lane: cache cleanup ack (seals the pending one).
    # The ack runs BEFORE the pick so that in the same poll an ack seals the
    # pending flush and the pick does not re-deliver it (the later commit persists everything).
    if body.cache_flush_completed:
        await session.execute(_SQL_POLL_ACK_CACHE_FLUSH, {"worker_id": worker.id})

    # Control lane: delivery of the cleanup signal. Only idle (defer-
    # until-idle); nothing is marked on delivery (repeatable until the ack).
    clear_cache = False
    if body.status == "idle":
        flush_row = (
            await session.execute(_SQL_POLL_PICK_CACHE_FLUSH, {"worker_id": worker.id})
        ).first()
        clear_cache = flush_row is not None

    # Measure the quota + disk of the worker's workspace. If it is at the limit,
    # no new tasks are delivered (queued ones wait; they resume when space frees up).
    storage_state = await storage.storage_status(
        session, str(worker.workspace_id), settings=app_settings
    )
    storage_paused = storage_state.paused_reason

    # Server-side disk gate, modelled EXACTLY
    # on `storage_paused`. The SERVER decides the threshold
    # (`worker_claim_min_free_fraction`); the worker only reports its fraction.
    #   - threshold 0.0 → gate disabled entirely;
    #   - NULL fraction (old worker / unmeasurable volume) → does NOT gate
    #     (fail-open): stopping task intake because it cannot measure would be
    #     worse than the problem, and preserves the previous behaviour of the
    #     pre-existing fleet.
    disk_gated = (
        app_settings.worker_claim_min_free_fraction > 0.0
        and body.free_disk_fraction is not None
        and body.free_disk_fraction < app_settings.worker_claim_min_free_fraction
    )

    # 4-6. Claim / push delivery. Only if the worker is not paused, there is no
    # workspace storage limit, and it is not disk-gated. The `clear_cache` lane
    # (above, conditioned only on `status == "idle"`) stays OPEN on purpose for
    # a gated worker, like the `storage_paused` pattern: we hold back new tasks
    # but can still flush its cache so that it recovers space (closes the cost
    # of the flush lane that the worker's disk gate left open).
    # The delivery attempt goes in a savepoint (`_attempt_delivery`). If a
    # declared secret cannot be decrypted or there are no keys, the claim is
    # undone and the task stays `queued`. Then:
    # (1) if the refusal is not due to missing keys and the file changed on
    #     disk, it is reloaded and the normal pick is retried once;
    # (2) while the refusal is for names that cannot be decrypted, it picks
    #     again without the tasks that declare those names, up to
    #     `_MAX_NAME_EXCLUSION_PICKS` times: those that use other, healthy
    #     secrets keep going out;
    # (3) without keys (no stored secret can be delivered) or once (2) is
    #     exhausted, a last pick without any task that uses stored secrets.
    attempt = _Attempt()
    refusals: list[_Refusal] = []
    if new_status != "paused" and storage_paused is None and not disk_gated:

        async def _try(*, exclude_all: bool, names: list[str]) -> _Attempt:
            return await _attempt_delivery(
                session,
                worker,
                body,
                secrets_capable=secrets_capable,
                worker_version=worker_version,
                exclude_stored_secrets=exclude_all,
                exclude_secret_names=names,
            )

        def _keys_missing(refusal: _Refusal) -> bool:
            return isinstance(refusal.exc, task_secrets.TaskSecretsKeysMissing)

        # (0) Memory of a recent refusal of this worker (`_refusal_memory`):
        # the first pick already excludes what is known to be undeliverable.
        worker_key = str(worker.id)
        memory = _recall_refusal(worker_key)
        exclude_all = False
        blocked_names: set[str] = set()
        if memory is not None:
            if memory.keys_missing:
                # Only valid while the keys remain missing.
                if task_secrets_keyring.get_keyring() is None:
                    exclude_all = True
                else:
                    memory = None
            elif task_secrets_keyring.reload_keyring_if_changed():
                # The keys file changed: what was remembered no longer holds.
                memory = None
            else:
                blocked_names = set(memory.names)
                exclude_all = memory.exclude_all
            if memory is None:
                _refusal_memory.pop(worker_key, None)

        attempt = await _try(exclude_all=exclude_all, names=sorted(blocked_names))
        if (
            memory is None
            and attempt.refusal is not None
            and not _keys_missing(attempt.refusal)
            and task_secrets_keyring.reload_keyring_if_changed()
        ):
            attempt = await _try(exclude_all=False, names=[])
        picks = 0
        while (
            attempt.refusal is not None
            and not exclude_all
            and not _keys_missing(attempt.refusal)
            and picks < _MAX_NAME_EXCLUSION_PICKS
        ):
            refusals.append(attempt.refusal)
            blocked_names.update(attempt.refusal.exc.names)
            picks += 1
            attempt = await _try(exclude_all=False, names=sorted(blocked_names))
        if attempt.refusal is not None and not exclude_all:
            refusals.append(attempt.refusal)
            exclude_all = True
            attempt = await _try(exclude_all=True, names=[])
        if attempt.refusal is not None:
            # A secret set between the pick and the decryption: nothing is delivered.
            refusals.append(attempt.refusal)
        if refusals and REFUSAL_MEMORY_SECONDS > 0:
            _remember_refusal(
                worker_key,
                _RefusalMemory(
                    # An already valid memory keeps its expiry: what it
                    # remembered is retried at its due time.
                    expires=memory.expires
                    if memory is not None
                    else time.monotonic() + REFUSAL_MEMORY_SECONDS,
                    keys_missing=any(_keys_missing(r) for r in refusals)
                    or (memory is not None and memory.keys_missing),
                    names=frozenset(blocked_names),
                    exclude_all=exclude_all,
                ),
            )
        if refusals or memory is not None:
            # Outside the savepoint, in the poll transaction: the other workers
            # stop yielding to this one the tasks that use stored secrets.
            # At most one write per interval (see the constant).
            now_mono = time.monotonic()
            last_mark = _refusal_mark_written.get(worker_key)
            if (
                last_mark is None
                or now_mono - last_mark >= TASK_SECRETS_REFUSAL_MARK_INTERVAL_SECONDS
            ):
                marked = await session.execute(
                    _SQL_POLL_MARK_SECRETS_REFUSED,
                    {
                        "worker_id": worker.id,
                        "min_interval": TASK_SECRETS_REFUSAL_MARK_INTERVAL_SECONDS,
                    },
                )
                # Sealed only if the UPDATE wrote. With 0 rows another process
                # wrote it less than an interval ago: sealing here would hide
                # that fact and this process would stop retrying for a full
                # interval even if the database mark ages sooner.
                if marked.rowcount == 1:
                    if len(_refusal_mark_written) >= _REFUSAL_MEMORY_MAX_ENTRIES:
                        _refusal_mark_written.clear()
                    _refusal_mark_written[worker_key] = now_mono
    task_payload: PollTaskPayload | None = attempt.payload

    await session.commit()

    # A refused delivery leaves the task `queued`. The operator is warned
    # (rate-limited ERROR) and it is recorded for the dashboard. After the
    # commit, like the rest of the poll telemetry.
    for refusal in refusals:
        await _report_refusal(session, worker, refusal.task_id, refusal.exc)

    # Operator visibility. A worker below the floor that gets no task in an
    # `idle` poll and has no other gate (pause, storage, disk) may be letting
    # tasks with stored secrets go by that it will never receive. With an active
    # gate the version is not what holds it back, so it does not warn.
    # Per-worker throttle in process memory: the query runs at most once per
    # window. It goes after the commit and only logs on failure: a count error
    # cannot return 500 nor undo the heartbeat and lease.
    if secrets_capable:
        _unsupported_worker_last_check.pop(str(worker.id), None)
    elif (
        body.status == "idle"
        and new_status != "paused"
        and storage_paused is None
        and not disk_gated
        and task_payload is None
        and not refusals
    ):
        try:
            await _warn_unsupported_worker(session, worker, worker_version)
        except Exception:
            logger.exception("task_secrets.worker_unsupported_check_failed worker_id=%s", worker.id)

    if task_payload is not None and task_payload.secrets:
        # Names only, after the commit (the audit row already persisted). The
        # `worker_scoped=` token only appears if there is any, so the line
        # stays the same as before for workspace secrets.
        scoped = task_payload.worker_scoped_secrets
        logger.info(
            "task_secrets.delivered task_id=%s worker_id=%s names=%s%s",
            task_payload.task_id,
            worker.id,
            ",".join(sorted(task_payload.secrets)),
            f" worker_scoped={','.join(scoped)}" if scoped else "",
        )

    # Server-driven cadence. With a task delivered, 1 s: there may be more work
    # in the queue. A refused delivery does not speed up the poll: the task
    # waits for the operator to fix the keys or the secret, and retrying in a
    # loop changes nothing; the normal cadence remains.
    if task_payload is not None:
        next_poll = 1
    elif body.status == "busy":
        next_poll = 5
    else:
        next_poll = 30

    return PollResponse(
        task=task_payload,
        cancel_signals=cancel_signals,
        next_poll_after_seconds=next_poll,
        clear_cache=clear_cache,
        storage_paused=storage_paused,
        disk_gated=disk_gated,
    )
