"""Endpoints `/v1/workers/*`.

This router exposes:
- `POST /register`
- `POST /{worker_id}/goodbye`
- `POST /{worker_id}/poll` (in `orcaster_server.api.poll`, same prefix)

There is no separate heartbeat endpoint: workers use `/poll`, which carries
heartbeat + claim + commands in a single round trip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.audit import record_admin_action
from orcaster_server.db import get_session
from orcaster_server.models import Worker
from orcaster_server.schemas.workers import (
    WorkerGoodbyeResponse,
    WorkerRegisterRequest,
    WorkerRegisterResponse,
)
from orcaster_server.security import generate_api_key, hash_api_key
from orcaster_server.worker_auth import require_worker
from orcaster_server.workspaces.enrollment import (
    EnrollmentState,
    consume_enrollment_token,
    peek_enrollment_token,
)

router = APIRouter(prefix="/v1/workers", tags=["workers"])


SessionDep = Annotated[AsyncSession, Depends(get_session)]
WorkerDep = Annotated[Worker, Depends(require_worker)]


@router.post(
    "/register",
    response_model=WorkerRegisterResponse,
    status_code=status.HTTP_200_OK,
)
async def register_worker(
    body: WorkerRegisterRequest,
    session: SessionDep,
) -> WorkerRegisterResponse:
    # Everything runs in a single transaction with no intermediate commit; any
    # raise rolls back when the get_session context closes (no auto-commit).
    # Generic message on every failure path: do not leak which check failed.
    token_row = await peek_enrollment_token(session, body.enrollment_token)
    if (
        token_row is None
        or token_row.revoked_at is not None
        or token_row.use_count >= token_row.max_uses
        or token_row.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid enrollment token"
        )

    api_key = generate_api_key()
    worker = Worker(
        workspace_id=token_row.workspace_id,
        api_key_hash=hash_api_key(api_key),
        hostname=body.hostname,
        name=body.name,
        os=body.os,
        arch=body.arch,
        ram_mb=body.ram_mb,
        disk_mb=body.disk_mb,
        version=body.version,
        tags=body.tags,
        capabilities=body.capabilities,
    )
    session.add(worker)
    await session.flush()

    # Atomic consumption: the conditional UPDATE loses the race if another
    # ceremony already used the token → the raise rolls back the worker just
    # flushed.
    state, consumed_tok = await consume_enrollment_token(
        session, body.enrollment_token, worker_id=worker.id
    )
    if state is not EnrollmentState.OK:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid enrollment token"
        )

    # Token consumption is audited (forensic trail of the enrolled worker).
    # actor=None: register has no admin session (same precedent as the
    # worker API auto-retry). Same transaction.
    await record_admin_action(
        session,
        workspace_id=consumed_tok.workspace_id,
        action="consume_enrollment_token",
        target_kind="enrollment_token",
        target_id=str(consumed_tok.id),
        actor=None,
        payload={"worker_id": worker.id, "label": consumed_tok.label},
    )

    await session.commit()
    await session.refresh(worker)
    return WorkerRegisterResponse(worker_id=worker.id, api_key=api_key)


@router.post(
    "/{worker_id}/goodbye",
    response_model=WorkerGoodbyeResponse,
    status_code=status.HTTP_200_OK,
)
async def worker_goodbye(
    worker: WorkerDep,
    session: SessionDep,
) -> WorkerGoodbyeResponse:
    # `paused` is preserved. It is operator intent, not observed
    # connectivity, so a cooperative shutdown does not revert it: without
    # this guard the goodbye would leave `offline` and the poll on the next
    # start would fall into its `ELSE` branch -> `online`, resuming work on
    # its own. The guard lives in the WHERE (same shape as
    # `_MARK_OFFLINE_SQL` and `_SQL_PAUSE_WORKER`): a paused worker does not
    # match and no row is written.
    await session.execute(
        update(Worker)
        .where(Worker.id == worker.id, Worker.status != "paused")
        .values(status="offline")
    )
    await session.commit()
    return WorkerGoodbyeResponse()
