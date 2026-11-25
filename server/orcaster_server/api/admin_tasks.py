"""Endpoints `/admin/tasks*` (task creation).

No admin authentication yet (TODO). Both fan-out cases are covered:

- `target_mode='count'`: a plain INSERT into `tasks` with `status='queued'`.
- `target_mode='all_available'`: INSERT into `tasks` with `status='dispatching'`
  + INSERT of the executions snapshot (workers with `status='online'`) in the
  same transaction + UPDATE of `target_count` with the real size.

If the snapshot comes out empty (0 online workers meeting eligibility), the
task stays in `dispatching` with `target_count=0`. Housekeeping / the closing
policy eventually closes it; here it is enough to leave it materialized.

Eligibility matching: `eligibility={}` always matches any worker. The field is
persisted for future use.

**Universal signature**: every task is signed with `sign_task()` at INSERT
(there is no "unsigned task" branch). The `signed` field of the input wire is
accepted for client compatibility but ignored; the server treats it as always
`true`. For tasks with `target_mode='count' target_count=1` and
`target_worker_id`, the task is signed with that known UUID at INSERT. For
claim/all_available tasks, where the recipient worker is not known until the
claim, the task is signed with a placeholder `worker_id=""`; the `/poll`
endpoint re-signs with the real UUID of the polling worker when delivering the
task (see `server/orcaster_server/api/poll.py`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server import storage
from orcaster_server.api._reason import reason_from_request
from orcaster_server.api._retry_internals import (
    _SQL_BUMP_ATTEMPT_BUDGET,
    _SQL_CANCEL_EXECUTIONS,
    _SQL_RESET_TASK_QUEUED,
    _SQL_RETARGET_CLAIM_SAME_WORKER,
)
from orcaster_server.artifacts_store import delete_execution_artifacts
from orcaster_server.audit import record_admin_action
from orcaster_server.config import settings
from orcaster_server.db import get_session
from orcaster_server.deps import WorkspaceContext, require_workspace, require_workspace_owner
from orcaster_server.dispatch_eligibility import (
    SQL_SYNC_TARGET_COUNT_TO_SNAPSHOT,
    TASK_BY_ID,
    block_reason_text,
    capability_gate_sql,
    classify_dispatch_block,
    disk_gate_sql,
    stored_secrets_gate_sql,
)
from orcaster_server.schemas.tasks import (
    AdminControlOpRequest,
    AdminControlOpResponse,
    AdminCreateTaskRequest,
    AdminCreateTaskResponse,
    AdminRetryTaskRequest,
    AdminRetryTaskResponse,
    AutoRetryPolicy,
    BulkCancelTasksRequest,
    BulkCancelTasksResponse,
    BulkDeleteTasksRequest,
    BulkDeleteTasksResponse,
    BulkRetryTasksRequest,
    BulkRetryTasksResponse,
)
from orcaster_server.signing import get_private_key, is_signing_configured, sign_task
from orcaster_server.task_variant import extract_variant

router = APIRouter(prefix="/admin", tags=["admin"])

logger = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
WorkspaceDep = Annotated[WorkspaceContext, Depends(require_workspace)]
# Worker control-ops are owner-only, like the rest of worker control.
OwnerDep = Annotated[WorkspaceContext, Depends(require_workspace_owner)]


def _no_eligible_last_error(reason: str) -> str:
    """`tasks.last_error` text for a task closed without an eligible worker.

    The code comes first in the string: the code is what a consumer can
    compare, the phrase is what the operator reads. A distinction that only
    exists in prose is not a distinction.
    """
    return f"no_eligible_workers ({reason}): {block_reason_text(reason)}"


# --- SQL --------------------------------------------------------------------

_SQL_INSERT_TASK_COUNT = text(
    """
    INSERT INTO tasks (payload, target_mode, target_count, attempt_budget,
                       eligibility,
                       claim_timeout_seconds, execution_timeout_seconds,
                       status, priority, run_at,
                       delivery_mode, target_worker_id, idempotency_key,
                       target_worker_pool, signed, signature,
                       auto_retry_policy, parent_task_id, workspace_id,
                       variant_kind, variant_label)
    VALUES (CAST(:payload AS jsonb), 'count', :target_count, :target_count,
            CAST(:eligibility AS jsonb), :claim_timeout_seconds,
            :execution_timeout_seconds,
            'queued', :priority, COALESCE(:run_at, now()),
            :delivery_mode, :target_worker_id, :idempotency_key,
            CAST(:target_worker_pool AS jsonb),
            true, decode(repeat('00', 64), 'hex'),
            CAST(:auto_retry_policy AS jsonb), :parent_task_id, :workspace_id,
            :variant_kind, :variant_label)
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
    RETURNING id, created_at
    """
)

# Idempotent replay. The ON CONFLICT above turns a repeated key into "0 rows"
# (formerly: UniqueViolation -> 500). This SELECT fetches the existing task to
# decide: same workspace -> return it (replay); other workspace -> 409 without
# leaking anything about the other tenant (the index is global).
_SQL_SELECT_TASK_BY_IDEM_KEY = text(
    """
    SELECT id, status, target_mode, target_count, parent_task_id, workspace_id, payload
      FROM tasks
     WHERE idempotency_key = :idempotency_key
    """
)

# Universal signature: `signature` is NOT NULL. We insert a 64-byte
# placeholder (Ed25519 shape but invalid signature) and then overwrite it with
# the real signature, in the same tx. The CHECK constraint
# `signed=false OR signature IS NOT NULL` (0007) is satisfied because both go
# in as `signed=true` + NON-NULL bytes.

_SQL_INSERT_TASK_ALL_AVAILABLE = text(
    """
    INSERT INTO tasks (payload, target_mode, target_count, eligibility,
                       claim_timeout_seconds, execution_timeout_seconds,
                       status, priority, run_at,
                       target_worker_pool, signed, signature,
                       auto_retry_policy, parent_task_id, workspace_id,
                       variant_kind, variant_label)
    VALUES (CAST(:payload AS jsonb), 'all_available', 1,
            CAST(:eligibility AS jsonb), :claim_timeout_seconds,
            :execution_timeout_seconds,
            'dispatching', :priority, COALESCE(:run_at, now()),
            CAST(:target_worker_pool AS jsonb),
            true, decode(repeat('00', 64), 'hex'),
            CAST(:auto_retry_policy AS jsonb), :parent_task_id, :workspace_id,
            :variant_kind, :variant_label)
    RETURNING id, created_at
    """
)

# Existence validation of the pool UUIDs against the `workers` table. Returns
# the list of UUIDs that do NOT exist (for a detailed 422 to the operator).
# A soft-deleted worker is treated as "does not exist" from the targeting
# point of view. The JOIN filters `deleted_at IS NULL` so that the UUID of a
# deleted worker shows up in the unknown_ids list.
_SQL_VALIDATE_WORKER_IDS = text(
    """
    SELECT u.id::text AS id
      FROM unnest(CAST(:ids AS uuid[])) AS u(id)
      LEFT JOIN workers w ON w.id = u.id AND w.deleted_at IS NULL
                         AND w.workspace_id = :workspace_id
     WHERE w.id IS NULL
    """
)

_SQL_UPDATE_TASK_SIGNATURE = text(
    """
    UPDATE tasks SET signed = true, signature = :signature
     WHERE id = :task_id
    """
)

# Snapshot: one `assigned` row per online worker. Eligibility is not filtered
# here (eligibility={} matches everything). The field is available for a
# future Python matching implementation before the INSERT.
# `snapshot_epoch` is stamped from the task (single source): 0 in the creation
# snapshot; the already-bumped value when the caller is the retry
# (`_SQL_RESET_TASK_DISPATCHING` increments before the re-snapshot).
# Defense: the NOT EXISTS excludes workers that still have an ACTIVE execution
# of this task - a belt for any latent path that leaves executions active under
# a terminal task. Instead of violating the partial unique
# `task_executions_active_uk` (opaque 500 on retry), the snapshot is produced
# deterministically without that worker. In the creation snapshot it is a
# no-op by definition (new task, no executions).

# The three snapshot gates - capabilities, disk and secrets version floor -
# live in `orcaster_server/dispatch_eligibility.py`, because the block-reason
# classifier ("why did it come out empty?") counts workers with these SAME
# predicates and the housekeeping re-snapshot applies them over several tasks
# in a single statement. One copy per call site would drift, and the reported
# reason would stop describing the snapshot that was taken.
_SNAPSHOT_WORKER_SATISFIES_TASK = capability_gate_sql(TASK_BY_ID)
_SNAPSHOT_WORKER_NOT_DISK_GATED = disk_gate_sql()
_SNAPSHOT_WORKER_GETS_STORED_SECRETS = stored_secrets_gate_sql(TASK_BY_ID)

_SQL_INSERT_SNAPSHOT = text(
    f"""
    INSERT INTO task_executions (task_id, worker_id, status, attempt_index,
                                 snapshot_epoch)
    SELECT :task_id, w.id, 'assigned',
           COALESCE((SELECT MAX(attempt_index) FROM task_executions
                     WHERE task_id = :task_id AND worker_id = w.id), 0) + 1,
           (SELECT snapshot_epoch FROM tasks WHERE id = :task_id)
      FROM workers w
     WHERE w.status = 'online'
       AND w.deleted_at IS NULL
       AND w.workspace_id = :workspace_id
       -- Server-side disk gate: do not pre-assign to
       -- a worker gated by disk (see `disk_gate_sql`).
       AND {_SNAPSHOT_WORKER_NOT_DISK_GATED}
       -- Matching of `eligibility.capabilities` against
       -- `workers.capabilities`. A worker that does not match does not enter the
       -- snapshot, so `target_count` reflects the fan-out that can really run.
       AND {_SNAPSHOT_WORKER_SATISFIES_TASK}
       -- Version floor for tasks with stored secrets (see above).
       AND {_SNAPSHOT_WORKER_GETS_STORED_SECRETS}
       AND NOT EXISTS (SELECT 1 FROM task_executions e
                        WHERE e.task_id = :task_id
                          AND e.worker_id = w.id
                          AND e.status IN ('assigned', 'claimed', 'running'))
    RETURNING worker_id
    """
)

# Snapshot restricted to a subset (`all:<ids>`). Same semantics as the open
# snapshot, but bounded to the pool. If it comes out empty, the task is marked
# `failed/no_eligible_workers` in the handler.
# The defensive NOT EXISTS applies just as in the open snapshot.
_SQL_INSERT_SNAPSHOT_RESTRICTED = text(
    f"""
    INSERT INTO task_executions (task_id, worker_id, status, attempt_index,
                                 snapshot_epoch)
    SELECT :task_id, w.id, 'assigned',
           COALESCE((SELECT MAX(attempt_index) FROM task_executions
                     WHERE task_id = :task_id AND worker_id = w.id), 0) + 1,
           (SELECT snapshot_epoch FROM tasks WHERE id = :task_id)
      FROM workers w
     WHERE w.status = 'online'
       AND w.deleted_at IS NULL
       AND w.workspace_id = :workspace_id
       AND w.id = ANY(CAST(:pool AS uuid[]))
       -- Same disk gate as the open snapshot (see above).
       AND {_SNAPSHOT_WORKER_NOT_DISK_GATED}
       -- Matching of `eligibility.capabilities` against
       -- `workers.capabilities`. A worker that does not match does not enter the
       -- snapshot, so `target_count` reflects the fan-out that can really run.
       AND {_SNAPSHOT_WORKER_SATISFIES_TASK}
       -- Version floor for tasks with stored secrets (see above).
       AND {_SNAPSHOT_WORKER_GETS_STORED_SECRETS}
       AND NOT EXISTS (SELECT 1 FROM task_executions e
                        WHERE e.task_id = :task_id
                          AND e.worker_id = w.id
                          AND e.status IN ('assigned', 'claimed', 'running'))
    RETURNING worker_id
    """
)

# Close the task as `failed/no_eligible_workers` when the `all:<ids>` snapshot
# comes out empty (pool 100% offline at creation). It is materialized already
# closed - housekeeping has nothing to do.
# `last_error` stores WHICH gate left it without a worker, measured at close
# time. `cancelled_reason` is not touched: it is the close reason and its
# vocabulary lives in the consumers.
_SQL_MARK_TASK_FAILED_NO_ELIGIBLE = text(
    """
    UPDATE tasks
       SET status = 'failed',
           completed_at = now(),
           cancelled_reason = 'no_eligible_workers',
           last_error = :last_error,
           target_count = 1,
           attempt_budget = 1
     WHERE id = :task_id
    """
)

# all_available re-snapshot: the nominal count and the budget are re-synced to
# the snapshot size (they start equal; the budget only drifts through
# count-mode retries, and a re-snapshot is a new fan-out, not a retry).
# It lives in `orcaster_server/dispatch_eligibility.py`: the housekeeping
# re-snapshot needs it and cannot import this module.
_SQL_UPDATE_TARGET_COUNT = SQL_SYNC_TARGET_COUNT_TO_SNAPSHOT


# --- Endpoint ---------------------------------------------------------------


async def _validate_pool_ids(session: AsyncSession, pool: list[str], workspace_id: str) -> None:
    """Validate that all the UUIDs in `pool` exist in the workspace's `workers`.

    Raises `HTTPException(422)` with the detail of the unknown IDs.
    The Pydantic parser already guaranteed the syntax; here the DB lookup is
    scoped to the active workspace: a worker of another tenant is treated as
    unknown (cross-tenant existence is not leaked).
    """
    if not pool:
        return
    unknown = (
        (
            await session.execute(
                _SQL_VALIDATE_WORKER_IDS, {"ids": pool, "workspace_id": workspace_id}
            )
        )
        .scalars()
        .all()
    )
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown_worker_ids",
                "unknown_ids": list(unknown),
                "message": (
                    "target references worker UUIDs that do not exist in the workers table"
                ),
            },
        )


@router.post("/tasks", response_model=AdminCreateTaskResponse)
async def admin_create_task(
    body: AdminCreateTaskRequest,
    session: SessionDep,
    ctx: WorkspaceDep,
) -> AdminCreateTaskResponse:
    resp = await _create_task(session, body, ctx.workspace_id, parent_task_id=None)
    await session.commit()
    return resp


async def _replay_existing_task(
    session: AsyncSession,
    *,
    idempotency_key: str,
    workspace_id: str,
    incoming_payload: Any,
) -> AdminCreateTaskResponse:
    """Resolve an idempotency_key conflict as a replay, 422 or 409.

    Retrying a creation with the same key is the use case of the key: the
    caller must receive the existing task, with its CURRENT status (it may have
    advanced since the first attempt). If the key belongs to another workspace
    (the UNIQUE index is global), we answer a generic 409: returning the
    foreign task would be a cross-tenant leak, and the earlier 500 already
    leaked existence.

    Within the SAME workspace, if the incoming payload does NOT match that of
    the original task, it is a client error (it reused the key for something
    else). It is rejected with 422 - distinct from the cross-workspace 409,
    which is an ownership conflict - without leaking the original payload. The
    comparison is semantic (dict-vs-dict): the stored payload comes from JSONB
    already deserialized by asyncpg, so key order does not produce false
    positives.
    """
    existing = (
        await session.execute(_SQL_SELECT_TASK_BY_IDEM_KEY, {"idempotency_key": idempotency_key})
    ).first()
    # `existing is None` only if the task was deleted between the conflict and
    # this SELECT: treat it the same as the cross-tenant collision (generic 409).
    if existing is None or str(existing.workspace_id) != str(workspace_id):
        raise HTTPException(
            status_code=409,
            detail="idempotency_key already in use",
        )
    # JSONB: asyncpg returns a dict; defensive in case it arrives as str (other driver).
    stored_payload = existing.payload
    if isinstance(stored_payload, str):
        stored_payload = json.loads(stored_payload)
    if stored_payload != incoming_payload:
        raise HTTPException(
            status_code=422,
            detail="idempotency_key reused with different payload",
        )
    target_mode = existing.target_mode
    target_count = int(existing.target_count)
    return AdminCreateTaskResponse(
        task_id=int(existing.id),
        status=existing.status,
        target=str(target_count) if target_mode == "count" else "all-available",
        target_mode=target_mode,
        target_count=target_count,
        parent_task_id=existing.parent_task_id,
    )


async def _create_task(
    session: AsyncSession,
    body: AdminCreateTaskRequest,
    workspace_id: str,
    *,
    parent_task_id: int | None = None,
) -> AdminCreateTaskResponse:
    """Universal signature + INSERT (+ all_available snapshot). Does NOT commit.

    The caller (admin_create_task / admin_fork_task) commits - this lets the
    fork insert the task and its audit row atomically.

    `parent_task_id`: NULL for a normal creation; the parent's task_id for
    forks. It is propagated to both INSERT statements.
    """
    target_mode, target_count, pool = body.parsed_target()

    # `idempotency_key` only makes sense in the count branch, whose INSERT
    # carries the partial UNIQUE index + ON CONFLICT that materializes the
    # replay. The all_available branch (target `all-available` or `all:<ids>`)
    # does NOT persist the key and has no ON CONFLICT, so accepting it would
    # silently void the idempotency guarantee (every resend would re-snapshot).
    # It is rejected early - BEFORE signing/snapshotting/inserting - to avoid
    # useless work and leaking data.
    if body.idempotency_key is not None and target_mode == "all_available":
        raise HTTPException(
            status_code=422,
            detail="idempotency_key is not supported for target all-available/all:<ids>",
        )

    claim_timeout = body.claim_timeout_seconds or settings.default_claim_timeout_seconds

    # Validate that the pool UUIDs exist in `workers` BEFORE any INSERT.
    # Without this, the operator could fire "orphan" tasks that nobody would
    # ever see.
    if pool is not None:
        await _validate_pool_ids(session, pool, workspace_id)

    # Serialize the policy if present. Pydantic `model_dump()` produces a flat
    # dict suitable for JSONB; NULL if the operator does not set it.
    auto_retry_policy_json = (
        json.dumps(body.auto_retry.model_dump()) if body.auto_retry is not None else None
    )

    # Variant derived from the payload (tool + version) to segment the
    # dashboard. NULL/NULL if no extractor recognizes it (`other` bucket). A fork
    # inherits the parent's payload, so the variant is re-derived identically.
    variant_kind, variant_label = extract_variant(body.payload)

    common_params = {
        "payload": json.dumps(body.payload),
        "eligibility": json.dumps(body.eligibility),
        "claim_timeout_seconds": claim_timeout,
        "execution_timeout_seconds": body.execution_timeout_seconds,
        "priority": body.priority,
        "run_at": body.run_at,
        "target_worker_pool": json.dumps(pool) if pool is not None else None,
        "auto_retry_policy": auto_retry_policy_json,
        "parent_task_id": parent_task_id,
        "workspace_id": workspace_id,
        "variant_kind": variant_kind,
        "variant_label": variant_label,
    }

    # Universal signature. The key must be loaded or something broke
    # after the lifespan; we answer 503 so the operator retries once the server
    # has the key again (in practice it should never happen because the
    # lifespan fails fast; this check covers the theoretical race of a
    # concurrent reset_key_cache).
    if not is_signing_configured():
        raise HTTPException(
            status_code=503,
            detail="task signing unavailable: server has no Ed25519 signing key loaded",
            headers={"Retry-After": "60"},
        )
    try:
        priv = get_private_key()
    except (RuntimeError, FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="task signing unavailable: signing key unreadable",
            headers={"Retry-After": "60"},
        ) from exc

    if target_mode == "count":
        row = (
            await session.execute(
                _SQL_INSERT_TASK_COUNT,
                {
                    **common_params,
                    "target_count": target_count,
                    "delivery_mode": body.delivery_mode,
                    "target_worker_id": (
                        str(body.target_worker_id) if body.target_worker_id else None
                    ),
                    "idempotency_key": (
                        str(body.idempotency_key) if body.idempotency_key else None
                    ),
                },
            )
        ).first()
        if row is None:
            # Only possible through the ON CONFLICT of the partial UNIQUE index
            # `tasks_idempotency_key_idx`: the key is already used.
            return await _replay_existing_task(
                session,
                idempotency_key=str(body.idempotency_key),
                workspace_id=workspace_id,
                incoming_payload=body.payload,
            )
        task_id = int(row[0])
        # Two-step signing: use the DB's created_at so the client rebuilds the
        # exact canonical msg. For PUSH tasks with target_worker_id set, we sign
        # with that UUID; the polling worker will be that same one and the
        # verification matches. For CLAIM tasks (target_worker_id=NULL), we sign
        # with a "" placeholder and the /poll endpoint re-signs with the
        # polling worker's UUID at claim time - the final signature stored and
        # delivered to the client is the re-signed one (see poll.py).
        created_at = row[1]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        created_at_iso = created_at.isoformat().replace("+00:00", "Z")
        signature = sign_task(
            priv,
            task_id=task_id,
            worker_id=str(body.target_worker_id) if body.target_worker_id else "",
            command="run_batch",
            payload=body.payload,
            idempotency_key=str(body.idempotency_key) if body.idempotency_key else None,
            created_at_iso=created_at_iso,
        )
        await session.execute(
            _SQL_UPDATE_TASK_SIGNATURE,
            {"signature": signature, "task_id": task_id},
        )
        return AdminCreateTaskResponse(
            task_id=task_id,
            status="queued",
            target=body.target,
            target_mode="count",
            target_count=target_count,
            parent_task_id=parent_task_id,
        )

    # target_mode == "all_available": snapshot in the same transaction.
    # NOTE: target_count is set to 1 as a placeholder (satisfies the CHECK
    # `target_count >= 1`). After the snapshot INSERT, it is updated with the
    # effective size (may be 0; the CHECK is not applied on UPDATE because the
    # constraint was only evaluated at INSERT - verify). If necessary, leave 1
    # as a placeholder when the snapshot is empty.
    row = (await session.execute(_SQL_INSERT_TASK_ALL_AVAILABLE, common_params)).first()
    assert row is not None
    task_id = int(row[0])
    created_at = row[1]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    created_at_iso = created_at.isoformat().replace("+00:00", "Z")
    # Placeholder signature (worker_id="") to satisfy signature NOT NULL.
    # poll.py re-signs with the real UUID when delivering each execution.
    signature = sign_task(
        priv,
        task_id=task_id,
        worker_id="",
        command="run_batch",
        payload=body.payload,
        idempotency_key=None,
        created_at_iso=created_at_iso,
    )
    await session.execute(
        _SQL_UPDATE_TASK_SIGNATURE,
        {"signature": signature, "task_id": task_id},
    )

    # If there is a `pool` (`all:<ids>` syntax), the snapshot is restricted to
    # the subset. Otherwise, open snapshot (`all-available` behaviour).
    if pool is not None:
        snapshot_rows = (
            await session.execute(
                _SQL_INSERT_SNAPSHOT_RESTRICTED,
                {
                    "task_id": task_id,
                    "pool": pool,
                    "workspace_id": workspace_id,
                    "claim_min_free_fraction": settings.worker_claim_min_free_fraction,
                },
            )
        ).all()
    else:
        snapshot_rows = (
            await session.execute(
                _SQL_INSERT_SNAPSHOT,
                {
                    "task_id": task_id,
                    "workspace_id": workspace_id,
                    "claim_min_free_fraction": settings.worker_claim_min_free_fraction,
                },
            )
        ).all()
    snapshot_size = len(snapshot_rows)

    if snapshot_size >= 1:
        await session.execute(
            _SQL_UPDATE_TARGET_COUNT,
            {"task_id": task_id, "n": snapshot_size},
        )
        return AdminCreateTaskResponse(
            task_id=task_id,
            status="dispatching",
            target=body.target,
            target_mode="all_available",
            target_count=snapshot_size,
            parent_task_id=parent_task_id,
        )

    # snapshot_size == 0. The reason is measured HERE, with the same gates that
    # just left the snapshot empty, and returned to the caller - who until now
    # only saw `target_count=0` and had to guess whether it lacked online
    # workers or capabilities.
    block_reason = await classify_dispatch_block(
        session,
        task_id=task_id,
        workspace_id=workspace_id,
        pool=pool,
        min_free_fraction=settings.worker_claim_min_free_fraction,
    )
    if pool is not None:
        # `all:<ids>` with all workers offline at snapshot time -> `failed` task
        # with cancelled_reason='no_eligible_workers'.
        # `cancelled_reason` does not change (it is the close reason, and the
        # vocabulary is read by consumers); the detail goes to `last_error`,
        # which is where the closed task keeps it as measured at close time.
        # Recomputing it when looking at the task would lie: workers that were
        # not online when it closed may be online now.
        await session.execute(
            _SQL_MARK_TASK_FAILED_NO_ELIGIBLE,
            {"task_id": task_id, "last_error": _no_eligible_last_error(block_reason)},
        )
        return AdminCreateTaskResponse(
            task_id=task_id,
            status="failed",
            target=body.target,
            target_mode="all_available",
            target_count=0,
            parent_task_id=parent_task_id,
            no_eligible_reason=block_reason,
        )

    # `all-available` (pool=None) with no eligible worker: the task stays
    # `dispatching` with `target_count=0` (placeholder 1 in the DB) WAITING for
    # an eligible worker, and that is a state with a mechanism and a signal:
    # housekeeping re-snapshots the empty epoch when one comes in
    # (`housekeeping._resnapshot_awaiting_all_available`) and the admin UI
    # shows the wait with its reason.
    logger.info(
        "task %d (all-available) has no eligible worker yet (%s); "
        "it stays dispatching and housekeeping will re-snapshot it when one appears",
        task_id,
        block_reason,
    )
    return AdminCreateTaskResponse(
        task_id=task_id,
        status="dispatching",
        target=body.target,
        target_mode="all_available",
        target_count=0,
        parent_task_id=parent_task_id,
        no_eligible_reason=block_reason,
    )


# --- Bulk cancel / delete ----------------------------------------
#
# IMPORTANT: these routes are declared BEFORE the parametric
# `/tasks/{task_id}/...` ones because FastAPI evaluates routes in registration
# order. If they were declared afterwards, a request to `/tasks/bulk/cancel`
# would match `/tasks/{task_id}/cancel` and `"bulk"` would be parsed as an
# `int`, returning 422 before reaching the bulk handler.
#
# `ANY(:ids)` pattern: SQLAlchemy text() accepts a plain Python list for
# asyncpg (see `server/tests/conftest.py:247-249` as a reference).

_SQL_BULK_FETCH_TASK_STATUS = text(
    """
    SELECT id, status::text AS status
      FROM tasks
     WHERE id = ANY(:ids)
       AND workspace_id = :workspace_id
    """
)

_SQL_BULK_CANCEL = text(
    """
    UPDATE tasks
       SET status = 'cancelled',
           completed_at = now(),
           cancelled_reason = :reason
     WHERE id = ANY(:ids)
       AND workspace_id = :workspace_id
       AND status NOT IN ('completed', 'cancelled', 'failed')
    RETURNING id
    """
)

_SQL_BULK_CANCEL_EXECUTIONS = text(
    """
    UPDATE task_executions
       SET status = 'cancelled', finished_at = now()
     WHERE task_id = ANY(:ids)
       AND status IN ('assigned', 'claimed', 'running')
    """
)

_SQL_BULK_DELETE = text(
    """
    DELETE FROM tasks
     WHERE id = ANY(:ids)
       AND workspace_id = :workspace_id
       AND status IN ('completed', 'cancelled', 'failed')
    RETURNING id
    """
)

# Execution ids of a set of tasks, scoped to the workspace (EXISTS over
# tasks). Used to clean up the artifact bytes on the filesystem when deleting.
_SQL_EXECUTION_IDS_FOR_TASKS = text(
    """
    SELECT e.id::text AS id
      FROM task_executions e
      JOIN tasks t ON t.id = e.task_id
     WHERE e.task_id = ANY(:ids)
       AND t.workspace_id = :workspace_id
    """
)

# SUM of the artifact bytes of a set of tasks, scoped to the workspace.
# It is queried BEFORE the DELETE because the CASCADE removes the task_artifacts
# rows along with the task_executions.
#
# `ta.collected_at IS NULL`: a collected row already left the quota counter
# when the operational routine purged its bytes, so discounting it again when
# deleting the task would make the counter drift negative (clamped to 0 by the
# CHECK, but losing the real usage of the workspace's other artifacts). The
# criterion is the same as the reconciliation SUM in
# `orcaster_server/storage.py`: only artifacts with bytes on disk count. Here
# the filter can go in the WHERE: it is an INNER JOIN aggregating into a
# single scalar, not the per-workspace LEFT JOIN of the reconcile.
_SQL_ARTIFACT_BYTES_FOR_TASKS = text(
    """
    SELECT COALESCE(SUM(ta.size_bytes), 0) AS total
      FROM task_artifacts ta
      JOIN task_executions e ON e.id = ta.execution_id
      JOIN tasks t ON t.id = e.task_id
     WHERE e.task_id = ANY(:ids)
       AND t.workspace_id = :workspace_id
       AND ta.collected_at IS NULL
    """
)


def _purge_artifact_bytes(execution_ids: list[str]) -> None:
    """Delete the artifact byte dirs of several executions.

    Synchronous (`shutil.rmtree`): up to `ARTIFACT_MAX_COUNT` files /
    `ARTIFACT_MAX_TOTAL_BYTES` per execution, so it is invoked via
    `run_in_threadpool` from the handlers so as not to block the event loop.
    Idempotent: a missing dir is a no-op."""
    for ex_id in execution_ids:
        delete_execution_artifacts(settings.artifacts_root, str(ex_id))


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.post("/tasks/bulk/cancel", response_model=BulkCancelTasksResponse)
async def admin_bulk_cancel_tasks(
    body: BulkCancelTasksRequest,
    session: SessionDep,
    request: Request,
    ctx: WorkspaceDep,
) -> BulkCancelTasksResponse:
    """Bulk-cancel a set of tasks (best-effort, per-id).

    - `cancelled`: actually moved to `cancelled` by this request.
    - `skipped`: exist but were already in a terminal state.
    - `not_found`: do not exist in `tasks`.

    Writes one row in `admin_actions` for each cancelled task. `skipped` /
    `not_found` are not audited (convention: rejections are not persisted).
    """
    reason = reason_from_request(request, body.reason)

    ids = body.task_ids
    status_rows = (
        (
            await session.execute(
                _SQL_BULK_FETCH_TASK_STATUS,
                {"ids": ids, "workspace_id": ctx.workspace_id},
            )
        )
        .mappings()
        .all()
    )
    status_by_id = {int(r["id"]): r["status"] for r in status_rows}

    cancelled_rows = (
        await session.execute(
            _SQL_BULK_CANCEL,
            {"ids": ids, "reason": reason, "workspace_id": ctx.workspace_id},
        )
    ).all()
    cancelled_ids = [int(r[0]) for r in cancelled_rows]

    if cancelled_ids:
        await session.execute(_SQL_BULK_CANCEL_EXECUTIONS, {"ids": cancelled_ids})

    cancelled_set = set(cancelled_ids)
    not_found = [tid for tid in ids if tid not in status_by_id]
    skipped = [
        {"id": tid, "reason": f"already terminal: {status_by_id[tid]}"}
        for tid in ids
        if tid in status_by_id and tid not in cancelled_set
    ]

    for tid in cancelled_ids:
        await record_admin_action(
            session,
            workspace_id=ctx.workspace_id,
            action="bulk_cancel_task",
            target_kind="task",
            target_id=str(tid),
            reason=reason,
        )

    await session.commit()
    return BulkCancelTasksResponse(
        cancelled=cancelled_ids,
        skipped=skipped,  # type: ignore[arg-type]
        not_found=not_found,
    )


async def bulk_delete_tasks_core(
    session: AsyncSession, *, task_ids: list[int], workspace_id: str
) -> BulkDeleteTasksResponse:
    """Bulk hard-delete of terminal tasks (best-effort, per-id) + commit.

    Core shared by the dashboard (`admin_bulk_delete_tasks`) and the public API
    (`POST /api/v1/tasks/bulk/delete`). It NEVER aborts the batch because of an
    ineligible id; it classifies each id:

    - `deleted`: actually deleted (they were in a terminal state).
    - `skipped`: exist but are NOT in a terminal state (`not terminal: <st>`).
    - `not_found`: do not exist in the active workspace (cross-tenant included).

    Same handling of bytes / CASCADE / purge as `delete_task_core`: resolves
    `execution_id` + bytes BEFORE the CASCADE, decrements the quota in the same
    tx, audits one `bulk_delete_task` row per deleted id, commits and purges
    the bytes in the threadpool after the commit.
    """
    ids = task_ids
    status_rows = (
        (
            await session.execute(
                _SQL_BULK_FETCH_TASK_STATUS,
                {"ids": ids, "workspace_id": workspace_id},
            )
        )
        .mappings()
        .all()
    )
    status_by_id = {int(r["id"]): r["status"] for r in status_rows}

    deletable_ids = [tid for tid, st in status_by_id.items() if st in _TERMINAL_TASK_STATES]
    deleted_ids: list[int] = []
    # Artifact bytes live outside the DB (filesystem). The FK deletes the
    # metadata via CASCADE; the bytes are cleaned up here. We resolve the
    # execution_ids BEFORE the DELETE (afterwards they vanish via CASCADE),
    # scoped to the workspace, so as not to touch another tenant's bytes.
    execution_ids: list[str] = []
    purged_bytes = 0
    if deletable_ids:
        exec_rows = (
            await session.execute(
                _SQL_EXECUTION_IDS_FOR_TASKS,
                {"ids": deletable_ids, "workspace_id": workspace_id},
            )
        ).all()
        execution_ids = [str(r[0]) for r in exec_rows]
        # Compute the bytes to discount BEFORE the CASCADE.
        purged_bytes = int(
            (
                await session.execute(
                    _SQL_ARTIFACT_BYTES_FOR_TASKS,
                    {"ids": deletable_ids, "workspace_id": workspace_id},
                )
            ).scalar_one()
        )
        deleted_rows = (
            await session.execute(
                _SQL_BULK_DELETE,
                {"ids": deletable_ids, "workspace_id": workspace_id},
            )
        ).all()
        deleted_ids = [int(r[0]) for r in deleted_rows]
        if deleted_ids:
            # Decrement the workspace counter in the same transaction.
            await storage.bump_workspace_usage(session, workspace_id, -purged_bytes)

    # Audit AFTER the DELETE: RETURNING confirms what actually disappeared
    # (covers the very rare race with another admin deleting the row between
    # the SELECT and the DELETE). `admin_actions.target_id` is `text` with no
    # FK, so the audit row can outlive the task.
    for tid in deleted_ids:
        await record_admin_action(
            session,
            workspace_id=workspace_id,
            action="bulk_delete_task",
            target_kind="task",
            target_id=str(tid),
            payload={"prior_status": status_by_id[tid]},
        )

    deleted_set = set(deleted_ids)
    not_found = [tid for tid in ids if tid not in status_by_id]
    skipped = [
        {"id": tid, "reason": f"not terminal: {status_by_id[tid]}"}
        for tid in ids
        if tid in status_by_id and tid not in deleted_set
    ]

    await session.commit()
    # Byte cleanup after the delete is confirmed (idempotent). It only runs if
    # some task was actually deleted. In the threadpool: the synchronous rmtree
    # must not block the event loop.
    if deleted_ids and execution_ids:
        await run_in_threadpool(_purge_artifact_bytes, execution_ids)
    return BulkDeleteTasksResponse(
        deleted=deleted_ids,
        skipped=skipped,  # type: ignore[arg-type]
        not_found=not_found,
    )


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.post("/tasks/bulk/delete", response_model=BulkDeleteTasksResponse)
async def admin_bulk_delete_tasks(
    body: BulkDeleteTasksRequest,
    session: SessionDep,
    ctx: WorkspaceDep,
) -> BulkDeleteTasksResponse:
    """Bulk-delete tasks that are already terminal (best-effort, per-id)."""
    return await bulk_delete_tasks_core(
        session, task_ids=body.task_ids, workspace_id=ctx.workspace_id
    )


_SQL_GET_TASK_FOR_RETRY = text(
    """
    SELECT id, status::text AS status, target_mode, target_count,
           delivery_mode, target_worker_id, target_worker_pool
      FROM tasks
     WHERE id = :id
       AND workspace_id = :workspace_id
       FOR UPDATE
    """
)

# Reset to `dispatching` for the retry of `all_available` tasks. Identical to
# `_SQL_RESET_TASK_QUEUED` but leaves the task in `dispatching` (not `queued`):
# the `all_available` model materializes the assignments via snapshot
# (`assigned` rows in `task_executions`) and the worker picks them up in /poll
# with `_SQL_POLL_CLAIM_PREASSIGNED`. The close is decided by the current epoch
# in `MAYBE_CLOSE_TASK`: the `snapshot_epoch` bump here opens the new epoch
# BEFORE the re-snapshot (which stamps the already-bumped value), so that the
# terminal executions of earlier epochs stay out of the close - without the
# bump, `completed = total` would be unreachable after a retry (history is
# preserved, migration 0014).
_SQL_RESET_TASK_DISPATCHING = text(
    """
    UPDATE tasks
       SET status = 'dispatching',
           completed_at = NULL,
           cancelled_reason = NULL,
           last_error = NULL,
           run_at = now(),
           expires_at = NULL,
           snapshot_epoch = snapshot_epoch + 1
     WHERE id = :id
       AND workspace_id = :workspace_id
    """
)

# Last worker with a started attempt (started_at IS NOT NULL). Ordered by
# started_at DESC; on a tie, by assigned_at DESC. The selection is restricted
# to executions with started_at for safety - if it never started
# (status='assigned' and only assigned_at), it is not "the last one that
# ran".
_SQL_LAST_ATTEMPT_WORKER = text(
    """
    SELECT worker_id
      FROM task_executions
     WHERE task_id = :id
       AND started_at IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM tasks t
            WHERE t.id = task_executions.task_id
              AND t.workspace_id = :workspace_id
       )
     ORDER BY started_at DESC, assigned_at DESC
     LIMIT 1
    """
)

# Count of attempts (count of executions) to report attempt_number.
# `task_executions` has no `workspace_id` of its own; scoping goes through
# EXISTS against the owning task (defense-in-depth).
_SQL_COUNT_EXECUTIONS = text(
    """
    SELECT count(*)
      FROM task_executions
     WHERE task_id = :id
       AND EXISTS (
           SELECT 1 FROM tasks t
            WHERE t.id = task_executions.task_id
              AND t.workspace_id = :workspace_id
       )
    """
)

# Migration 0019: counter of the operator's in-place retries. RETURNING the
# post-bump value for the response. Independent of auto_retry_attempts_used.
_SQL_BUMP_RETRY_COUNT = text(
    "UPDATE tasks SET retry_count = retry_count + 1 "
    "WHERE id = :id AND workspace_id = :workspace_id RETURNING retry_count"
)

# NOTE: `_SQL_RESET_TASK_QUEUED`, `_SQL_RETARGET_CLAIM_SAME_WORKER`,
# `_SQL_BUMP_ATTEMPT_BUDGET` and `_SQL_CANCEL_EXECUTIONS` live in
# `orcaster_server/api/_retry_internals.py` so they can be shared with the
# auto-retry of `task_complete`. The block of dense comments on invariants
# (claim+pool vs push, attempt_budget bump, CHECK constraints) is there - read
# it before touching the SQL.


@dataclass(frozen=True)
class _RetryOutcome:
    """Result of applying the retry on an already-locked task.

    `status` is the immediate resulting state: "queued" (count-mode),
    "dispatching" (re-snapshotted all_available) or "failed" (`all:<ids>`
    with the pool 100% offline -> re-closed as no_eligible_workers).
    """

    status: str
    attempt_number: int
    target_worker_id: str | None
    delivery_mode: str
    retry_count: int


async def _apply_retry_locked(
    session: AsyncSession,
    *,
    task_id: int,
    workspace_id: str,
    row: Mapping[str, Any],
    force_same_worker: bool,
) -> _RetryOutcome:
    """Apply steps 3-5 of the retry on a task ALREADY locked (FOR UPDATE).

    `row` is the mapping returned by `_SQL_GET_TASK_FOR_RETRY` (or its bulk
    variant): the caller does the lock + status validation; this helper only
    mutates (reset + targeting + retry_count bump). It does NOT commit or
    audit - that stays in each endpoint.

    Single executable source of truth for the retry flow, shared by
    `admin_retry_task` and `admin_bulk_retry_tasks` (so the scheduling SQL is
    not duplicated).
    """
    # Micro-optimization with no semantic change: `_SQL_LAST_ATTEMPT_WORKER`
    # only feeds the sticky branch (`sticky_same_worker = force_same_worker and
    # last_worker_id is not None`); with force_same_worker=False its result is
    # not used, so the query is skipped.
    last_worker_id: str | None = None
    if force_same_worker:
        last_worker_row = (
            await session.execute(
                _SQL_LAST_ATTEMPT_WORKER, {"id": task_id, "workspace_id": workspace_id}
            )
        ).first()
        last_worker_id = str(last_worker_row[0]) if last_worker_row is not None else None

    # attempt_number BEFORE mutating anything - the post-mutation count might
    # not match the "attempt number" the operator expects to see.
    prior_attempts = (
        await session.execute(_SQL_COUNT_EXECUTIONS, {"id": task_id, "workspace_id": workspace_id})
    ).scalar_one()
    attempt_number = int(prior_attempts) + 1

    # Reset + targeting. The branch depends on the original `target_mode`.
    target_mode = row["target_mode"]
    sticky_same_worker = force_same_worker and last_worker_id is not None

    if target_mode == "all_available":
        # `all_available` retry: re-snapshot against the workers online NOW,
        # respecting the original pool restriction (if there was one).
        # `force_same_worker` is NOT honoured here - the semantics of
        # `all_available` is "run on all the available ones", and the operator
        # chose that mode on purpose. Pinning it to a single worker (the old
        # sticky-retry behaviour) broke the intent: if that worker was offline,
        # the task stayed `queued`/`dispatching` hanging indefinitely even if
        # other workers were online. To direct a retry to a specific worker
        # there is Fork with `target=worker:<id>`.
        await session.execute(
            _SQL_RESET_TASK_DISPATCHING, {"id": task_id, "workspace_id": workspace_id}
        )
        raw_pool = row["target_worker_pool"]
        pool = list(raw_pool) if raw_pool else None
        if pool:
            snapshot_rows = (
                await session.execute(
                    _SQL_INSERT_SNAPSHOT_RESTRICTED,
                    {
                        "task_id": task_id,
                        "pool": pool,
                        "workspace_id": workspace_id,
                        "claim_min_free_fraction": settings.worker_claim_min_free_fraction,
                    },
                )
            ).all()
        else:
            snapshot_rows = (
                await session.execute(
                    _SQL_INSERT_SNAPSHOT,
                    {
                        "task_id": task_id,
                        "workspace_id": workspace_id,
                        "claim_min_free_fraction": settings.worker_claim_min_free_fraction,
                    },
                )
            ).all()
        snapshot_size = len(snapshot_rows)
        if snapshot_size >= 1:
            await session.execute(
                _SQL_UPDATE_TARGET_COUNT, {"task_id": task_id, "n": snapshot_size}
            )
            status_out: str = "dispatching"
        else:
            # Same reason, measured with the same gates as in create.
            block_reason = await classify_dispatch_block(
                session,
                task_id=task_id,
                workspace_id=workspace_id,
                pool=pool,
                min_free_fraction=settings.worker_claim_min_free_fraction,
            )
            if pool:
                # `all:<ids>` with the pool 100% offline at retry -> same close
                # as in create: `failed/no_eligible_workers`.
                await session.execute(
                    _SQL_MARK_TASK_FAILED_NO_ELIGIBLE,
                    {"task_id": task_id, "last_error": _no_eligible_last_error(block_reason)},
                )
                status_out = "failed"
            else:
                # Open `all-available` with no eligible worker: it stays
                # `dispatching` waiting for one, in the NEW epoch that the reset
                # just bumped. That is exactly the one the housekeeping
                # re-snapshot picks up when an eligible worker comes in, and
                # the one the UI shows as "waiting for an eligible worker".
                logger.info(
                    "task %d (all-available) re-snapshotted empty (%s); it stays "
                    "dispatching until an eligible worker appears",
                    task_id,
                    block_reason,
                )
                status_out = "dispatching"
        target_worker_id_out: str | None = None
        delivery_mode_out = str(row["delivery_mode"])
    else:
        # Common count-mode reset (status -> queued, clears close fields,
        # normalizes scheduling).
        await session.execute(_SQL_RESET_TASK_QUEUED, {"id": task_id, "workspace_id": workspace_id})
        status_out = "queued"
        if sticky_same_worker:
            # Migration 0014: the previous execution is NOT deleted. The new PK
            # `id UUID` lets multiple rows with the same (task_id, worker_id)
            # coexist, told apart by `attempt_index`. The PARTIAL UNIQUE over
            # active states guarantees that only one is "live" - since the
            # previous one is in a terminal state (precondition of the retry),
            # the INSERT of the subsequent claim in /poll does not collide.
            # Full history is preserved.
            #
            # The original worker is targeted via `target_worker_pool` (claim
            # mode), NOT via `delivery_mode='push'`. See the comment in
            # `_SQL_RETARGET_CLAIM_SAME_WORKER`: `push` is reserved for signed
            # administrative commands.
            await session.execute(
                _SQL_RETARGET_CLAIM_SAME_WORKER,
                {
                    "id": task_id,
                    "pool": json.dumps([last_worker_id]),
                    "workspace_id": workspace_id,
                },
            )
            # The response keeps exposing `target_worker_id` so that the UI and
            # the audit log identify the target worker, even though at the DB
            # level that value lives in `target_worker_pool`.
            target_worker_id_out = last_worker_id
            delivery_mode_out = "claim"
        else:
            target_worker_id_out = str(row["target_worker_id"]) if row["target_worker_id"] else None
            delivery_mode_out = str(row["delivery_mode"])

        # Every count-mode retry reopens budget by bumping `attempt_budget` by 1
        # ("one more attempt"). The claim predicate counts every non-expired
        # execution against the budget, so the previous terminal ones
        # (precondition of the retry: terminal task) consume budget - without
        # the bump the retry's claim would never fire. `target_count` (success
        # threshold) is not touched: the retry gives one more attempt, it does
        # not demand one more success. See point 2 of the design block.
        await session.execute(
            _SQL_BUMP_ATTEMPT_BUDGET, {"id": task_id, "workspace_id": workspace_id}
        )

    # Migration 0019: increment of the in-place retry counter (once, outside
    # the targeting branches). RETURNING the post-bump value.
    retry_count = (
        await session.execute(_SQL_BUMP_RETRY_COUNT, {"id": task_id, "workspace_id": workspace_id})
    ).scalar_one()

    return _RetryOutcome(
        status=status_out,
        attempt_number=attempt_number,
        target_worker_id=target_worker_id_out,
        delivery_mode=delivery_mode_out,
        retry_count=int(retry_count),
    )


# Bulk lock with `ORDER BY id` for the retry. Same as
# `_SQL_GET_TASK_FOR_RETRY` but set-based. Important nuance: Postgres does NOT
# guarantee that the order in which row locks are acquired follows the
# `ORDER BY` - if the planner resolves it with a bitmap heap scan + sort node,
# the locks are taken during the scan (physical heap order) BEFORE the sort.
# The `ORDER BY id` only translates into lock order when the plan satisfies the
# order via an index scan over the PK (the usual case for `id = ANY(array)`
# with short lists, but not guaranteed). In practice two concurrent bulks with
# the same set of IDs do not deadlock because both run the SAME deterministic
# plan and therefore lock in the same order (whichever it is), regardless of
# the order of the input array - that is the real reason, not the `ORDER BY`
# per se. The `ORDER BY id` is kept anyway as an anti-deadlock convention
# (total order consistent with the rest of the operations that lock by
# ascending id). It replaces the lock-free `_SQL_BULK_FETCH_TASK_STATUS` of
# cancel/delete because the retry does mutate scheduling row by row and needs
# the same FOR UPDATE protection as the individual retry.
_SQL_GET_TASKS_FOR_RETRY_BULK = text(
    """
    SELECT id, status::text AS status, target_mode, target_count,
           delivery_mode, target_worker_id, target_worker_pool
      FROM tasks
     WHERE id = ANY(:ids)
       AND workspace_id = :workspace_id
     ORDER BY id
       FOR UPDATE
    """
)


# Terminal states that the bulk retry DOES retry: the "unsuccessful" closes.
# Deliberately stricter than the individual retry (which accepts any terminal
# state, incl. completed - which now includes closes with an outcome): a
# select-all over a mixed list must not re-run work that ALREADY succeeded by
# accident. `cancelled` is included because it is an unsuccessful close that
# the operator will want to retry just like `failed` (the individual retry
# already allowed it).
_BULK_RETRYABLE_TASK_STATES = ("failed", "cancelled")


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.post("/tasks/bulk/retry", response_model=BulkRetryTasksResponse)
async def admin_bulk_retry_tasks(
    body: BulkRetryTasksRequest,
    session: SessionDep,
    ctx: WorkspaceDep,
) -> BulkRetryTasksResponse:
    """Bulk-retry unsuccessful terminal tasks (`failed`/`cancelled`),
    best-effort per-id.

    - `retried`: the retry was applied - INCLUDING the `all:<ids>` task whose
      pool ended up 100% offline and is re-closed `failed/no_eligible_workers`
      (the retry ran: epoch bumped, fields reset, retry_count + 1; the
      immediate result was to fail again, just as the individual one answers
      `status: "failed"` in that case).
    - `skipped`: exist but are NOT in `failed`/`cancelled`. Stricter than the
      individual retry (which accepts any terminal state): a select-all over a
      mixed list must not re-run `completed` (work that already succeeded,
      including closes with an outcome) by accident.
    - `not_found`: do not exist in the active workspace (cross-tenant
      included).

    ALWAYS `force_same_worker=False`: the tasks go back to the general queue
    for any eligible worker; they are not re-pinned to the worker that failed.
    A single transaction with a commit at the end: a DB error midway through
    the loop -> total rollback; the replay is benign (the ones already retried
    will be `queued`/`dispatching` and fall into `skipped`). Exception: an
    `all:<ids>` whose pool ended up 100% offline goes back to `failed` within
    the same call, so a replay retries it again (it fails again, bumping
    epoch/retry_count/budget once more) instead of skipping it - just like the
    individual retry.
    One `bulk_retry_task` audit row per retried task; skipped/not_found are
    NOT audited (rejections are not persisted).

    Cost/concurrency: the batch runs inside ONE transaction that holds the
    `FOR UPDATE` locks on all the matched rows (up to the `BulkRetryTasksRequest`
    limit, 500) plus N serialized queries per candidate until the final commit.
    For large batches this holds locks and roundtrips for a while; the 500
    limit bounds it and is consistent with the cancel/delete bulk pattern. If
    the volume grew, the batch would have to be paginated/chunked (not needed
    today for an admin endpoint).
    """
    ids = body.task_ids
    rows = (
        (
            await session.execute(
                _SQL_GET_TASKS_FOR_RETRY_BULK,
                {"ids": ids, "workspace_id": ctx.workspace_id},
            )
        )
        .mappings()
        .all()
    )
    row_by_id = {int(r["id"]): r for r in rows}

    # Per-id classification (same pattern as admin_bulk_cancel_tasks):
    # no unexpected status aborts the batch.
    not_found = [tid for tid in ids if tid not in row_by_id]
    skipped = [
        {"id": tid, "reason": f"not failed/cancelled: {row_by_id[tid]['status']}"}
        for tid in ids
        if tid in row_by_id and row_by_id[tid]["status"] not in _BULK_RETRYABLE_TASK_STATES
    ]
    candidates = [
        tid
        for tid in ids
        if tid in row_by_id and row_by_id[tid]["status"] in _BULK_RETRYABLE_TASK_STATES
    ]

    retried: list[int] = []
    for tid in candidates:
        outcome = await _apply_retry_locked(
            session,
            task_id=tid,
            workspace_id=ctx.workspace_id,
            row=row_by_id[tid],
            force_same_worker=False,
        )
        retried.append(tid)
        # Payload mirroring the audit of the individual retry.
        await record_admin_action(
            session,
            workspace_id=ctx.workspace_id,
            action="bulk_retry_task",
            target_kind="task",
            target_id=str(tid),
            payload={
                "prior_status": row_by_id[tid]["status"],
                "force_same_worker": False,
                "target_worker_id": outcome.target_worker_id,
                "delivery_mode": outcome.delivery_mode,
                "attempt_number": outcome.attempt_number,
                "retry_count": outcome.retry_count,
            },
        )

    await session.commit()
    return BulkRetryTasksResponse(
        retried=retried,
        skipped=skipped,  # type: ignore[arg-type]
        not_found=not_found,
    )


# --- Async CRUD over tasks -------------------------------------------

# Terminal task states - DELETE is only allowed if the task already closed.
_TERMINAL_TASK_STATES = ("completed", "cancelled", "failed")
# States from which a task can be paused.
_PAUSEABLE_TASK_STATES = ("queued", "dispatching")


class _ReasonBody(BaseModel):
    reason: str | None = None


_SQL_GET_TASK_STATUS_MODE = text(
    "SELECT status, target_mode FROM tasks WHERE id = :id AND workspace_id = :workspace_id"
)

_SQL_PAUSE_TASK = text(
    """
    UPDATE tasks SET status = 'paused'
     WHERE id = :id AND workspace_id = :workspace_id
       AND status IN ('queued', 'dispatching')
    RETURNING 1
    """
)

# resume: queued if target_mode='count', dispatching if 'all_available'.
_SQL_RESUME_TASK = text(
    """
    UPDATE tasks
       SET status = CASE
              WHEN target_mode = 'count' THEN 'queued'::task_status
              ELSE 'dispatching'::task_status
           END
     WHERE id = :id AND workspace_id = :workspace_id AND status = 'paused'
    RETURNING status
    """
)

_SQL_DELETE_TASK = text(
    """
    DELETE FROM tasks
     WHERE id = :id AND workspace_id = :workspace_id
       AND status IN ('completed', 'cancelled', 'failed')
    RETURNING 1
    """
)


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.post("/tasks/{task_id}/pause")
async def admin_pause_task(
    task_id: int,
    session: SessionDep,
    ctx: WorkspaceDep,
    _body: dict | None = None,
) -> dict[str, str]:
    row = (
        await session.execute(
            _SQL_GET_TASK_STATUS_MODE, {"id": task_id, "workspace_id": ctx.workspace_id}
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    if row[0] not in _PAUSEABLE_TASK_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"task in status '{row[0]}' cannot be paused",
        )
    await session.execute(_SQL_PAUSE_TASK, {"id": task_id, "workspace_id": ctx.workspace_id})
    await session.commit()
    return {"status": "paused"}


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.post("/tasks/{task_id}/resume")
async def admin_resume_task(
    task_id: int,
    session: SessionDep,
    ctx: WorkspaceDep,
    _body: dict | None = None,
) -> dict[str, str]:
    row = (
        await session.execute(
            _SQL_GET_TASK_STATUS_MODE, {"id": task_id, "workspace_id": ctx.workspace_id}
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    if row[0] != "paused":
        raise HTTPException(
            status_code=409,
            detail=f"task in status '{row[0]}' cannot be resumed",
        )
    result = (
        await session.execute(_SQL_RESUME_TASK, {"id": task_id, "workspace_id": ctx.workspace_id})
    ).first()
    await session.commit()
    assert result is not None
    return {"status": str(result[0])}


async def delete_task_core(session: AsyncSession, *, task_id: int, workspace_id: str) -> str:
    """Hard-delete a terminal task scoped to the workspace + commit.

    Core shared by the dashboard (`admin_delete_task`) and the public API
    (`DELETE /api/v1/tasks/{id}`). Follows the pattern of `cancel_task_core`:
    it raises the `HTTPException` directly to tell 404 (does not exist /
    cross-tenant) from 409 (exists but not terminal), so that admin and v1 do
    not duplicate the status logic. Returns the deleted `prior_status` (enum
    string) so the caller can expose it if needed.

    It encapsulates the full flow (same as the original handler): terminal
    state guard, resolution of `execution_id` + artifact bytes BEFORE the
    CASCADE, DELETE with RETURNING, decrement of the workspace quota in the
    same tx, `delete_task` audit with `prior_status`, commit and purge of the
    artifact bytes in the threadpool after the commit (idempotent).
    """
    row = (
        await session.execute(
            _SQL_GET_TASK_STATUS_MODE, {"id": task_id, "workspace_id": workspace_id}
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    prior_status = row[0]
    if prior_status not in _TERMINAL_TASK_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"task in status '{prior_status}' cannot be deleted",
        )
    # Artifact bytes live outside the DB (filesystem). The FK deletes the
    # metadata via CASCADE; the bytes are cleaned up here. We resolve the
    # execution_ids BEFORE the DELETE (afterwards they vanish via CASCADE),
    # scoped to the workspace.
    exec_rows = (
        await session.execute(
            _SQL_EXECUTION_IDS_FOR_TASKS,
            {"ids": [task_id], "workspace_id": workspace_id},
        )
    ).all()
    execution_ids = [str(r[0]) for r in exec_rows]
    # Compute the bytes to discount BEFORE the CASCADE.
    purged_bytes = int(
        (
            await session.execute(
                _SQL_ARTIFACT_BYTES_FOR_TASKS,
                {"ids": [task_id], "workspace_id": workspace_id},
            )
        ).scalar_one()
    )
    deleted_row = (
        await session.execute(_SQL_DELETE_TASK, {"id": task_id, "workspace_id": workspace_id})
    ).first()
    # Only decrement if the DELETE really removed the row (mirror of the bulk
    # guard): avoids counter drift in a race with another delete.
    if deleted_row is not None:
        await storage.bump_workspace_usage(session, workspace_id, -purged_bytes)
    await record_admin_action(
        session,
        workspace_id=workspace_id,
        action="delete_task",
        target_kind="task",
        target_id=str(task_id),
        payload={"prior_status": prior_status},
    )
    await session.commit()
    if execution_ids:
        await run_in_threadpool(_purge_artifact_bytes, execution_ids)
    return prior_status


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_task(task_id: int, session: SessionDep, ctx: WorkspaceDep) -> Response:
    await delete_task_core(session, task_id=task_id, workspace_id=ctx.workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Cancellation + propagation --------------------------------------------

# Canonical SQL: cancels the task if it is not terminal and propagates to
# non-terminal executions. `cancelled_reason` persists the optional reason.
_SQL_CANCEL_TASK = text(
    """
    UPDATE tasks
       SET status = 'cancelled',
           completed_at = now(),
           cancelled_reason = :reason
     WHERE id = :id
       AND workspace_id = :workspace_id
       AND status NOT IN ('completed', 'cancelled', 'failed')
    RETURNING 1
    """
)

# NOTE: `_SQL_CANCEL_EXECUTIONS` (propagation to active executions) lives in
# `orcaster_server/api/_retry_internals.py` - shared with the close of
# `task_complete`. The workspace-scoping and lease_token details are
# documented there.

_SQL_TASK_EXISTS = text("SELECT 1 FROM tasks WHERE id = :id AND workspace_id = :workspace_id")


async def cancel_task_core(
    session: AsyncSession, *, task_id: int, workspace_id: str, reason: str | None
) -> None:
    """Cancel a task scoped to the workspace + audit + commit.

    Core shared by the dashboard (`admin_cancel_task`) and the public API
    (`/api/v1/tasks/{id}/cancel`). Raises 404 (does not exist / cross-tenant,
    without leaking existence) or 409 (already terminal).
    """
    affected = (
        await session.execute(
            _SQL_CANCEL_TASK,
            {"id": task_id, "reason": reason, "workspace_id": workspace_id},
        )
    ).first()
    if affected is None:
        # Tell 404 (does not exist / cross-tenant) from 409 (already terminal).
        # The exists check is scoped to the workspace, so a task of another
        # tenant falls into 404 without leaking its existence.
        exists = (
            await session.execute(_SQL_TASK_EXISTS, {"id": task_id, "workspace_id": workspace_id})
        ).first()
        if exists is None:
            raise HTTPException(status_code=404, detail="task not found")
        raise HTTPException(status_code=409, detail="task is already in a terminal state")
    await session.execute(_SQL_CANCEL_EXECUTIONS, {"id": task_id})
    await record_admin_action(
        session,
        workspace_id=workspace_id,
        action="cancel_task",
        target_kind="task",
        target_id=str(task_id),
        reason=reason,
    )
    await session.commit()


@router.post("/tasks/{task_id}/cancel")
async def admin_cancel_task(
    task_id: int,
    session: SessionDep,
    request: Request,
    ctx: WorkspaceDep,
    body: _ReasonBody | None = None,
) -> dict[str, str]:
    # Precedence body > `HX-Prompt` header (the dashboard's custom dialog sends
    # the reason there, percent-encoded) and normalization: orcaster_server/api/_reason.py.
    reason = reason_from_request(request, body.reason if body is not None else None)
    await cancel_task_core(session, task_id=task_id, workspace_id=ctx.workspace_id, reason=reason)
    return {"status": "cancelled"}


# --- In-place retry ----------------------------------------
#
# Design decisions (review before modifying):
#
# 1. **Accepted terminal states**: `completed`, `failed`, `cancelled`.
#    Those are the terminal states of the `task_status` ENUM (see
#    `orcaster_server/models.py:TASK_STATUS_VALUES`). A `completed` may carry
#    an `outcome` (e.g. `no_result`) - the nuance lives in the column, not in
#    a status of its own, so it is still `completed` for the purposes of this
#    list. `expired` does NOT exist at the task level (only at the execution
#    level). Any other state answers 409 - the operator needs to know why
#    (pause/cancel first).
#
# 2. **attempt_budget / target_count**:
#    - EVERY count-mode retry increments `attempt_budget` by 1 - with and
#      without `force_same_worker`. The `taken` predicate in `/poll` counts
#      every non-expired execution against the budget, so the previous
#      terminal ones consume budget; the bump reopens exactly one slot.
#      Moreover the failure threshold of `MAYBE_CLOSE_TASK` is
#      `terminal_non_expired >= attempt_budget` and the task already
#      accumulated terminal executions in its previous run; without the bump it
#      would close again right away, giving the new attempt no chance.
#    - `target_count` (nominal fan-out, success threshold of the close:
#      exactly target_count 'completed' executions) is NOT touched in any
#      retry. That is why the bump does not interact with the CHECK
#      `tasks_target_worker_exclusive_chk` (which constrains `target_count`)
#      and the bump/retarget order is not load-bearing. (A retry without force
#      of a push task no longer triggers an IntegrityError because of the
#      bump; the re-delivery semantics of push tasks remains open.)
#    - For `all_available` the budget is NOT used in the close
#      (`MAYBE_CLOSE_TASK` closes with `terminal = total`); the retry redoes
#      the snapshot (see point 6) and re-syncs `target_count` and
#      `attempt_budget` to the new snapshot_size (also for the UI's
#      "done / N").
#
# 3. **`run_at` and `expires_at`**: they are set to `now()` (run_at) and NULL
#    (expires_at) so the task is immediately eligible. The operator chose
#    retry - we want it to run now. If it had a future `run_at` or a past
#    `expires_at`, we discard them: the intent of the retry trumps the
#    original scheduling.
#
# 4. **`completed_at`, `cancelled_reason`, `last_error`**: they are cleared
#    (NULL). The task is open again and the close fields lose meaning until
#    the next close.
#
# 5. **Previous executions**: ALL the previous executions (of the same worker
#    or of others) are preserved intact. Migration 0014 changed the PK of
#    `task_executions` to an independent `id UUID` and added `attempt_index
#    INT`; now several rows with the same `(task_id, worker_id)` can coexist
#    as long as they differ in `attempt_index`. A PARTIAL UNIQUE INDEX
#    `(task_id, worker_id) WHERE status IN ('assigned','claimed',
#    'running')` keeps the critical invariant: at most ONE live execution per
#    task/worker pair. Since the previous executions are in a terminal state
#    when the retry is done (precondition of point 1), the INSERT of the new
#    attempt does not collide and the full history is kept. The INSERT of the
#    subsequent claim/poll computes `attempt_index = COALESCE(MAX, 0) + 1` per
#    (task_id, worker_id).
#
# 6. **Targeting by target_mode**:
#
#    - **`target_mode='all_available'`**: the retry IGNORES
#      `force_same_worker` and redoes the snapshot against the workers online
#      NOW (open) or against the original subset (`all:<ids>` ->
#      `target_worker_pool`). The task goes back to `dispatching` and each
#      worker picks up the new `assigned` rows in `task_executions` in /poll.
#      If the snapshot comes out empty: for `all:<ids>` it is closed
#      `failed/no_eligible_workers`; for open `all-available` it stays
#      `dispatching` (housekeeping decides). Conscious decision: pinning an
#      `all_available` retry to a single worker (the previous behaviour) left
#      the task hanging if that worker was offline, even if others were
#      available - it contradicted the intent "run on all the available
#      ones". To direct it to a specific worker there is Fork with
#      `target=worker:<id>`.
#
#    - **`target_mode='count'` with `force_same_worker=true`** (default): if
#      there is a previously started attempt (started_at IS NOT NULL), we pick
#      the most recent one. `delivery_mode='claim'`,
#      `target_worker_pool=[<last_worker_uuid>]`, `target_worker_id=NULL` are
#      set. If there is NO previously started attempt, the original targeting
#      is respected and only attempt_budget is bumped.
#
#    NOTE: this path must not pivot to `delivery_mode='push'` pointing at the
#    target worker, because the design reserves `push` for signed
#    administrative commands - the worker rejects non-reserved push tasks with
#    exit_code=2 and `stderr = "non-reserved push tasks not supported by this
#    worker"`, which would make ALL retries of user tasks fail without running
#    the command. The canonical path to "direct a user task to a specific
#    worker" is claim + `target_worker_pool=[uuid]` (equivalent to the
#    `target='worker:<uuid>'` syntax). See the detailed comment in
#    `_SQL_RETARGET_CLAIM_SAME_WORKER`.
#
# 7. **`force_same_worker=false`** (only applies to `target_mode='count'`):
#    delivery_mode/target_worker_id are NOT changed; attempt_budget is bumped
#    by 1. For `all_available` the flag is not looked at (see point 6).
#
# 8. **Creation of rows in `task_executions`**: for `count` nothing is created
#    in advance - the normal claim flow will do it on the next /poll. For
#    `all_available` the re-snapshot DOES insert `assigned` rows (same as in
#    create). `attempt_number` is reported as `count(executions) + 1` so the UI
#    shows "Attempt N".
#
# 9. **Idempotency_key**: NOT touched. The point of the in-place retry is
#    precisely that the worker reuses `ORCASTER_CACHE_DIR` (key =
#    idempotency_key or task_id). If the task had an idem_key, the retry will
#    keep using it. The partial UNIQUE index `tasks_idempotency_key_idx` is on
#    the `tasks` table (not executions), so it does not collide with anything
#    (we are doing an UPDATE of the same row, not an INSERT).
#
# 10. **Audit**: one row in `admin_actions` with `action='retry_task'`
#     (see migration `0013_admin_actions_retry.py`).


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.post("/tasks/{task_id}/retry", response_model=AdminRetryTaskResponse)
async def admin_retry_task(
    task_id: int,
    session: SessionDep,
    ctx: WorkspaceDep,
    body: AdminRetryTaskRequest | None = None,
) -> AdminRetryTaskResponse:
    """In-place retry of a terminal task - reuses the same `task_id`.

    Different from "Fork" (which creates a new task with a new id). The purpose
    is for the worker to be able to reuse `ORCASTER_CACHE_DIR` between attempts
    (key = idempotency_key or task_id).

    See the design decisions block above for the details of the contract and
    trade-offs. Steps 3-5 (reset + targeting + retry_count bump) live in
    `_apply_retry_locked`, shared with the bulk.
    """
    params = body or AdminRetryTaskRequest()

    # 1. Lock + load (scoped to the active workspace: cross-tenant -> 404).
    row = (
        (
            await session.execute(
                _SQL_GET_TASK_FOR_RETRY, {"id": task_id, "workspace_id": ctx.workspace_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")

    current_status = row["status"]
    if current_status not in _TERMINAL_TASK_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"task in status '{current_status}' is not retry-able; "
                "only terminal tasks "
                "(completed/failed/cancelled) can be retried"
            ),
        )

    # 2-5 (no audit/commit): single source shared with the bulk retry.
    outcome = await _apply_retry_locked(
        session,
        task_id=task_id,
        workspace_id=ctx.workspace_id,
        row=row,
        force_same_worker=params.force_same_worker,
    )

    # Audit. attempt_number was computed pre-mutation by the helper.
    await record_admin_action(
        session,
        workspace_id=ctx.workspace_id,
        action="retry_task",
        target_kind="task",
        target_id=str(task_id),
        payload={
            "prior_status": current_status,
            "force_same_worker": params.force_same_worker,
            "target_worker_id": outcome.target_worker_id,
            "delivery_mode": outcome.delivery_mode,
            "attempt_number": outcome.attempt_number,
            "retry_count": outcome.retry_count,
        },
    )

    await session.commit()

    return AdminRetryTaskResponse(
        task_id=task_id,
        status=outcome.status,  # type: ignore[arg-type]
        attempt_number=outcome.attempt_number,
        target_worker_id=outcome.target_worker_id,  # type: ignore[arg-type]
        delivery_mode=outcome.delivery_mode,  # type: ignore[arg-type]
        retry_count=outcome.retry_count,
    )


# --- Fork: POST /admin/tasks/{id}/fork (lineage + params_changed) ----------
#
# Parametric route `/tasks/{task_id}/fork`. It is declared AFTER the bulk
# routes (`/tasks/bulk/...`) so as not to break matching: with the bulk ones
# already registered above, FastAPI evaluates them first and `bulk` never falls
# into this `{task_id}`. It is grouped with the other parametric ones
# (retry/cancel/...).

# Columns compared to classify a fork as identical vs modified with respect to
# the parent. It does NOT include priority (design decision: changing the
# priority does not count as "modified params").
_SQL_FORK_PARAMS = text(
    """
    SELECT payload,
           target_mode,
           target_count,
           target_worker_pool,
           target_worker_id::text AS target_worker_id,
           claim_timeout_seconds,
           execution_timeout_seconds
      FROM tasks
     WHERE id = :id
       AND workspace_id = :workspace_id
    """
)


# Batch variant of _SQL_FORK_PARAMS for the dashboard list: it brings the same
# fields that feed _params_changed but for a set of ids (the parents and forked
# children of the current page) and includes `id` to index the result. Same
# source of truth as the detail; we do not duplicate the comparison in SQL.
_SQL_FORK_PARAMS_BATCH = text(
    """
    SELECT id,
           payload,
           target_mode,
           target_count,
           target_worker_pool,
           target_worker_id::text AS target_worker_id,
           claim_timeout_seconds,
           execution_timeout_seconds
      FROM tasks
     WHERE id = ANY(:ids)
       AND workspace_id = :workspace_id
    """
)


def _params_changed(parent: dict, child: dict) -> bool:
    """Did the fork modify the params with respect to the parent?

    Compares payload + resolved targeting + timeouts. Does NOT compare priority.
    Works over DB rows (not the Pydantic body) so that the endpoint and the
    render use identical logic. Single source of truth.
    """
    fields = (
        "payload",
        "target_mode",
        "target_count",
        "target_worker_pool",
        "target_worker_id",
        "claim_timeout_seconds",
        "execution_timeout_seconds",
    )
    return any(parent.get(f) != child.get(f) for f in fields)


# --- Server-side fork-merge -------------------------------
#
# Helper shared by the public API fork (`/api/v1/tasks/{id}/fork`) and the
# dashboard fork (`admin_fork_task`). Semantics:
#   - `payload`/`eligibility` (free JSON) -> recursive key-by-key deep-merge;
#     the override wins on collision, what is not mentioned is inherited from
#     the parent.
#   - typed scalars (`target`, `priority`, `claim/exec_timeout`) and
#     `auto_retry` -> override-if-present, otherwise inherited from the parent.
#   - `run_at`/`idempotency_key` are NOT inherited (we want to run now; idem is
#     UNIQUE).
# The merge happens server-side -> atomic and with a single source of truth.

# Parent columns needed to rebuild the fork's effective body.
_SQL_GET_FORK_SOURCE = text(
    """
    SELECT payload, eligibility, priority, target_mode, target_count,
           target_worker_pool, target_worker_id::text AS target_worker_id,
           claim_timeout_seconds, execution_timeout_seconds, auto_retry_policy
      FROM tasks WHERE id = :id AND workspace_id = :workspace_id
    """
)


def _coerce_json_dict(value: object) -> dict:
    """Normalize a JSONB column to a dict (asyncpg already returns a dict; defensive)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return json.loads(value)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive merge: nested dicts are merged key by key, the rest overrides."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _reconstruct_fork_target(parent: dict) -> str:
    """Rebuild the `target` string from the parent's stored targeting.

    Same criterion as `_build_fork_body` (without the last-attempt-worker
    fallback: the merge starts from the persisted targeting, not from the
    executions). `all_available` preserves the open intent or the `all:<ids>`
    subset.
    """
    target_mode = parent["target_mode"]
    raw_pool = parent.get("target_worker_pool")
    pool = list(raw_pool) if raw_pool else None
    target_worker_id = parent.get("target_worker_id")
    if target_mode == "all_available":
        return "all:" + ",".join(pool) if pool else "all-available"
    if pool:
        return f"worker:{pool[0]}" if len(pool) == 1 else "workers:" + ",".join(pool)
    if target_worker_id:
        return f"worker:{target_worker_id}"
    return str(int(parent["target_count"]))


def merge_fork_params(parent: dict, override: dict) -> AdminCreateTaskRequest:
    """Build the fork's effective `AdminCreateTaskRequest`.

    `parent` is a row of `_SQL_GET_FORK_SOURCE`. `override` carries ONLY the
    fields explicitly sent by the client (`model_dump(exclude_unset=True)`);
    the absent ones are inherited. See the block above for the full semantics.
    """
    parent_payload = _coerce_json_dict(parent.get("payload"))
    parent_elig = _coerce_json_dict(parent.get("eligibility"))

    payload = (
        _deep_merge(parent_payload, override["payload"] or {})
        if "payload" in override
        else parent_payload
    )
    eligibility = (
        _deep_merge(parent_elig, override["eligibility"] or {})
        if "eligibility" in override
        else parent_elig
    )
    target = override["target"] if override.get("target") else _reconstruct_fork_target(parent)
    priority = override["priority"] if "priority" in override else int(parent["priority"])
    claim_timeout = (
        override["claim_timeout_seconds"]
        if "claim_timeout_seconds" in override
        else parent.get("claim_timeout_seconds")
    )
    exec_timeout = (
        override["execution_timeout_seconds"]
        if "execution_timeout_seconds" in override
        else parent.get("execution_timeout_seconds")
    )

    if "auto_retry" in override:
        ar = override["auto_retry"]
        auto_retry = AutoRetryPolicy(**ar) if ar else None
    else:
        parent_policy = parent.get("auto_retry_policy")
        auto_retry = AutoRetryPolicy(**_coerce_json_dict(parent_policy)) if parent_policy else None

    return AdminCreateTaskRequest(
        payload=payload,
        target=target,
        eligibility=eligibility,
        claim_timeout_seconds=claim_timeout,
        execution_timeout_seconds=exec_timeout,
        # run_at / idempotency_key are NOT inherited (runs now; idem is UNIQUE).
        priority=priority,
        auto_retry=auto_retry,
    )


async def fork_task_core(
    session: AsyncSession,
    *,
    task_id: int,
    workspace_id: str,
    body: AdminCreateTaskRequest,
) -> AdminCreateTaskResponse:
    """Create the child task (new id, parent_task_id=task_id) + audit + commit.

    Core shared by the dashboard (`admin_fork_task`) and the public API
    (`/api/v1/tasks/{id}/fork`). `body` is the `AdminCreateTaskRequest` ALREADY
    effective (the dashboard builds it client-side; the API builds it with
    `merge_fork_params`). 404 if the parent does not exist / is cross-tenant.
    """
    parent = (
        (await session.execute(_SQL_FORK_PARAMS, {"id": task_id, "workspace_id": workspace_id}))
        .mappings()
        .first()
    )
    if parent is None:
        raise HTTPException(status_code=404, detail="parent task not found")

    resp = await _create_task(session, body, workspace_id, parent_task_id=task_id)

    child = (
        (
            await session.execute(
                _SQL_FORK_PARAMS, {"id": resp.task_id, "workspace_id": workspace_id}
            )
        )
        .mappings()
        .first()
    )
    assert child is not None
    params_changed = _params_changed(dict(parent), dict(child))

    await record_admin_action(
        session,
        workspace_id=workspace_id,
        action="fork_task",
        target_kind="task",
        target_id=str(resp.task_id),
        payload={"parent_task_id": task_id, "params_changed": params_changed},
    )
    await session.commit()
    return resp


# auth: gated by require_workspace (session + membership); role-based authz (owner/member) TODO
@router.post("/tasks/{task_id}/fork", response_model=AdminCreateTaskResponse)
async def admin_fork_task(
    task_id: int,
    body: AdminCreateTaskRequest,
    session: SessionDep,
    ctx: WorkspaceDep,
) -> AdminCreateTaskResponse:
    """Create a new task (new id) with parent_task_id=task_id.

    The body is pre-filled by the "New task" modal (same as fork), but the
    operator can adjust it before sending. Audits params_changed
    (identical vs modified) in admin_actions for a durable audit trail; the
    task detail render re-derives the value with the same helper.

    NOTE: the public API uses `merge_fork_params` (server-side deep-merge).
    The dashboard builds the full body client-side; both converge in
    `fork_task_core`. The dashboard's adoption of the server-side merge
    (partial body) is still pending.
    """
    return await fork_task_core(session, task_id=task_id, workspace_id=ctx.workspace_id, body=body)


# --- Control-op: POST /admin/workers/{id}/control-op ------------

# A control-op against a soft-deleted worker returns 404 just as if it had
# never existed (the active worker identity requires deleted_at IS NULL).
_SQL_WORKER_EXISTS = text(
    "SELECT 1 FROM workers WHERE id = :id AND deleted_at IS NULL AND workspace_id = :workspace_id"
)

_SQL_INSERT_CONTROL_OP_TASK = text(
    """
    INSERT INTO tasks (
        status, target_mode, target_count, delivery_mode,
        target_worker_id, payload, command,
        idempotency_key, signature, workspace_id
    ) VALUES (
        'queued', 'count', 1, 'push',
        :worker_id, CAST(:payload AS jsonb), :command,
        :idempotency_key, 'placeholder', :workspace_id
    )
    RETURNING id, created_at
    """
)


@router.post("/workers/{worker_id}/control-op", response_model=AdminControlOpResponse)
async def admin_control_op(
    worker_id: str,
    body: AdminControlOpRequest,
    session: SessionDep,
    ctx: OwnerDep,
) -> AdminControlOpResponse:
    """Send a control operation to a worker via a signed push task.

    Creates a push task with `command = control_op` aimed at the worker.
    Only control_ops registered in `_RESERVED_COMMANDS` are valid
    (validation in the schema).
    """
    import uuid as _uuid

    exists = (
        await session.execute(
            _SQL_WORKER_EXISTS, {"id": worker_id, "workspace_id": ctx.workspace_id}
        )
    ).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="worker not found")

    priv = get_private_key() if is_signing_configured() else None

    idempotency_key_str = str(body.idempotency_key) if body.idempotency_key else None

    row = (
        await session.execute(
            _SQL_INSERT_CONTROL_OP_TASK,
            {
                "worker_id": worker_id,
                "payload": json.dumps(body.payload),
                "command": body.control_op,
                "idempotency_key": idempotency_key_str,
                "workspace_id": ctx.workspace_id,
            },
        )
    ).first()
    assert row is not None
    task_id = int(row[0])
    created_at = row[1]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    created_at_iso = created_at.isoformat().replace("+00:00", "Z")

    if priv is not None:
        signature = sign_task(
            priv,
            task_id=task_id,
            worker_id=worker_id,
            command=body.control_op,
            payload=body.payload,
            idempotency_key=idempotency_key_str,
            created_at_iso=created_at_iso,
        )
        await session.execute(
            _SQL_UPDATE_TASK_SIGNATURE,
            {"signature": signature, "task_id": task_id},
        )

    await session.commit()
    return AdminControlOpResponse(
        task_id=task_id,
        status="queued",
        control_op=body.control_op,
        target_worker_id=_uuid.UUID(worker_id),
    )
