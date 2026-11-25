"""Explicit emission of dashboard events via Postgres pg_notify.

The NOTIFY is emitted from application code on the SAME AsyncSession/
AsyncConnection as the write, so it inherits Postgres transactional semantics
(delivered on COMMIT, discarded on ROLLBACK) and the inter-process fan-out of
LISTEN/NOTIFY (multi-worker).

No-op when SSE_ENABLED=false: zero overhead on the hot path with the flag off.

The payloads match orcaster_server.sse_listener.payload_to_message.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy import text

from orcaster_server.config import settings


class _Executor(Protocol):
    async def execute(self, statement: Any, params: Any) -> Any: ...


class DashboardNotifier:
    """Emits dashboard events on the active transaction."""

    CHANNEL = "dashboard_events"

    async def _emit(self, executor: _Executor, payload: dict[str, str]) -> None:
        if not settings.sse_enabled:
            return
        await executor.execute(
            text("SELECT pg_notify(:ch, :p)"),
            {"ch": self.CHANNEL, "p": json.dumps(payload)},
        )

    async def output_appended(
        self, executor: _Executor, *, execution_id: Any, workspace_id: Any
    ) -> None:
        await self._emit(
            executor,
            {
                "type": "output",
                "execution_id": str(execution_id),
                "workspace_id": str(workspace_id),
            },
        )

    async def task_changed(self, executor: _Executor, *, task_id: Any, workspace_id: Any) -> None:
        await self._emit(
            executor,
            {"type": "task", "id": str(task_id), "workspace_id": str(workspace_id)},
        )

    async def worker_changed(
        self, executor: _Executor, *, worker_id: Any, workspace_id: Any
    ) -> None:
        await self._emit(
            executor,
            {"type": "worker", "id": str(worker_id), "workspace_id": str(workspace_id)},
        )

    async def execution_changed(
        self, executor: _Executor, *, execution_id: Any, workspace_id: Any
    ) -> None:
        await self._emit(
            executor,
            {
                "type": "execution",
                "id": str(execution_id),
                "workspace_id": str(workspace_id),
            },
        )


# Module singleton: stateless (only reads settings), safe to share.
dashboard_notifier = DashboardNotifier()
