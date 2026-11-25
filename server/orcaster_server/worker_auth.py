"""API key authentication for worker endpoints.

Pattern: the path carries `{worker_id}` and the `X-API-Key` header carries the
secret. The worker is looked up by id and `sha256(api_key)` is compared with
`workers.api_key_hash`. Any mismatch (nonexistent worker, different hash) ->
generic 401, without disclosing which one failed.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.db import get_session
from orcaster_server.models import Worker
from orcaster_server.security import hash_api_key, verify_api_key


def _reject_if_soft_deleted(worker: Worker) -> None:
    """If the worker is soft-deleted, its credentials are still valid but no
    worker endpoint (poll/heartbeat/output/complete/goodbye) may operate on it.
    We respond 410 Gone so the client knows the identity existed and was
    permanently retired (it is neither a transient 401 nor an ambiguous 404).
    """
    if getattr(worker, "deleted_at", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="worker soft-deleted",
        )


async def require_worker_by_key(
    session: Annotated[AsyncSession, Depends(get_session)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Worker:
    """Resolves the worker from the API key without `worker_id` in the path.

    For `/v1/tasks/*` and `/v1/commands/*` endpoints where the path does not
    carry `{worker_id}`. The key acts as the identity: direct lookup by
    `api_key_hash` (which is unique-indexed).
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-API-Key",
        )
    result = await session.execute(
        select(Worker).where(Worker.api_key_hash == hash_api_key(x_api_key))
    )
    worker = result.scalar_one_or_none()
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid api_key",
        )
    # Valid auth first, then the soft-delete check.
    _reject_if_soft_deleted(worker)
    return worker


async def require_worker(
    worker_id: Annotated[UUID, Path()],
    session: Annotated[AsyncSession, Depends(get_session)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Worker:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-API-Key",
        )
    result = await session.execute(select(Worker).where(Worker.id == str(worker_id)))
    worker = result.scalar_one_or_none()
    if worker is None or not verify_api_key(x_api_key, worker.api_key_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid worker_id or api_key",
        )
    # Valid auth first, then the soft-delete check.
    _reject_if_soft_deleted(worker)
    return worker
