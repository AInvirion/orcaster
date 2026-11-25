"""Shared builder for the on-demand output download (stdout/stderr log).

Generates the artifact on the fly from `output_events`, with no persisted
materialization. The snapshot is fixed with MAX(seq) at the start: if the
execution is still running, whatever exists up to that moment is downloaded, and
the download ends even if the worker keeps emitting events. Each batch uses its
own short session (not the request's): a slow client downloading GBs does not
hold a pool connection hostage.

Shared by the admin handler (`/admin/executions/{id}/output/download`, session
auth + membership) and the public v1 endpoints (`/api/v1/tasks/{id}/output` and
`/api/v1/executions/{id}/output`, PAT auth). Each caller authorizes the
`execution_id` with its own rules and delegates only the transport here;
`resolve_execution_in_workspace` is the resolver+authz shared by the two
per-execution routes (UUID-in-workspace).
"""

from __future__ import annotations

import json
import zlib
from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.db import get_sessionmaker

_DOWNLOAD_BATCH_SIZE = 1000

_SQL_EXECUTION_IN_WORKSPACE = text(
    """
    SELECT 1
      FROM task_executions e
      JOIN tasks t ON t.id = e.task_id
     WHERE e.id = CAST(:execution_id AS uuid)
       AND t.workspace_id = :workspace_id
    """
)


async def resolve_execution_in_workspace(
    session: AsyncSession, *, workspace_id: str, execution_id: str
) -> str:
    """Normalize the `execution_id` and validate that it belongs to the workspace.

    Returns the canonical UUID (str). Raises `404` if the value is not a valid
    UUID or the execution does not exist in the workspace; multi-tenant isolation
    makes an execution from another workspace indistinguishable from a
    nonexistent one. `UUID()` also normalizes variants (`urn:uuid:`/braces) that
    Postgres' CAST would reject and sanitizes the value by construction.

    Companion of `stream_output_gzip`: the caller resolves and authorizes here,
    and delegates only the transport there. Handlers that need the full row
    (e.g. artifacts) use the resolver with a projection from their own module.
    """
    try:
        execution_id = str(UUID(execution_id))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="execution not found") from exc
    found = (
        await session.execute(
            _SQL_EXECUTION_IN_WORKSPACE,
            {"execution_id": execution_id, "workspace_id": workspace_id},
        )
    ).first()
    if found is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return execution_id


_SQL_OUTPUT_MAX_SEQ = text(
    """
    SELECT COALESCE(MAX(seq), 0)
      FROM output_events
     WHERE execution_id = CAST(:execution_id AS uuid)
    """
)

_SQL_DOWNLOAD_OUTPUT_EVENTS = text(
    """
    SELECT seq, ts, stream, text
      FROM output_events
     WHERE execution_id = CAST(:execution_id AS uuid)
       AND seq > :after
       AND seq <= :max_seq
     ORDER BY seq
     LIMIT :limit
    """
)


def stream_output_gzip(
    execution_id: str,
    fmt: Literal["text", "jsonl"],
) -> StreamingResponse:
    """Gzip `StreamingResponse` of the output of an already authorized execution.

    `execution_id` must arrive normalized (canonical UUID) and with its scope
    already verified by the caller. `fmt`: `text` = raw concatenation of the
    `text` values in `seq` order (stdout+stderr interleaved, faithful to the
    terminal, no prefixes); `jsonl` = one JSON line `{seq, ts, stream, text}` per
    event. Transit uses `Content-Encoding: gzip`. Empty output -> empty gzip body
    (200), not an error.
    """
    sessionmaker = get_sessionmaker()
    if sessionmaker is None:  # impossible if a session dependency resolved
        raise RuntimeError("Database engine not initialized")

    async def _generate() -> AsyncIterator[bytes]:
        # wbits=31 -> gzip container (not raw deflate): understood by browsers,
        # gunzip and curl --compressed.
        compressor = zlib.compressobj(wbits=31)
        async with sessionmaker() as s:
            max_seq = (
                await s.execute(_SQL_OUTPUT_MAX_SEQ, {"execution_id": execution_id})
            ).scalar_one()
        after = 0
        while after < max_seq:
            async with sessionmaker() as s:
                rows = (
                    (
                        await s.execute(
                            _SQL_DOWNLOAD_OUTPUT_EVENTS,
                            {
                                "execution_id": execution_id,
                                "after": after,
                                "max_seq": max_seq,
                                "limit": _DOWNLOAD_BATCH_SIZE,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            if not rows:
                break  # gap in seq: should not happen (reliable streaming); do not loop further
            after = rows[-1]["seq"]
            # Compress row by row: a batch is up to _DOWNLOAD_BATCH_SIZE events;
            # joining the whole batch would materialize hundreds of MB in the
            # worst case. This bounds the memory peak to a single event.
            for r in rows:
                if fmt == "text":
                    piece = r["text"]
                else:
                    piece = (
                        json.dumps(
                            {
                                "seq": r["seq"],
                                "ts": r["ts"].isoformat(),
                                "stream": r["stream"],
                                "text": r["text"],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                chunk = compressor.compress(piece.encode("utf-8"))
                if chunk:
                    yield chunk
        yield compressor.flush()

    ext = "log" if fmt == "text" else "jsonl"
    media_type = "text/plain; charset=utf-8" if fmt == "text" else "application/x-ndjson"
    return StreamingResponse(
        _generate(),
        media_type=media_type,
        headers={
            "Content-Encoding": "gzip",
            "Content-Disposition": f'attachment; filename="output-{execution_id}.{ext}"',
        },
    )
