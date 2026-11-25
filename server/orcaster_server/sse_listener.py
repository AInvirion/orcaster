"""Dedicated Postgres LISTEN/NOTIFY listener for the dashboard SSE.

Keeps ONE persistent asyncpg connection per uvicorn process (outside the
SQLAlchemy pool) listening on the `dashboard_events` channel. Each NOTIFY is
translated into an SseMessage and published on the in-memory bus. If the
connection drops, it retries after a fixed delay (`_RECONNECT_DELAY_SECONDS`)
until `stop_event` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import asyncpg

from orcaster_server.sse_bus import SseBus, SseMessage

logger = logging.getLogger(__name__)

_CHANNEL = "dashboard_events"
# Fixed (non-exponential) delay between connection retries: for LISTEN/NOTIFY a
# short, constant retry is enough; there is no storm to dampen.
_RECONNECT_DELAY_SECONDS = 2.0


def _asyncpg_dsn(database_url: str) -> str:
    """Translate the SQLAlchemy DSN (postgresql+asyncpg://) to the native asyncpg one."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def payload_to_message(payload: dict) -> tuple[str, SseMessage, str | None]:
    """Translate the NOTIFY payload into (workspace_id, SseMessage, coalesce_key)."""
    workspace_id = str(payload["workspace_id"])
    kind = payload["type"]
    if kind == "output":
        eid = str(payload["execution_id"])
        event = f"output-{eid}"
        return workspace_id, SseMessage(event=event), event
    entity_id = str(payload["id"])
    return workspace_id, SseMessage(event=f"{kind}-{entity_id}"), None


async def dashboard_listen_loop(
    database_url: str,
    bus: SseBus,
    stop_event: asyncio.Event,
    *,
    ready: asyncio.Event | None = None,
) -> None:
    """Main listener loop. Returns when `stop_event` is set.

    `ready` (optional, keyword-only): if given, it is set after a successful
    `add_listener` (signal that the LISTEN is active). Intended for tests that
    need to wait for the listener to be ready without fixed sleeps. Since the
    loop can reconnect, `set()` is idempotent and is not cleared on disconnect.
    """
    dsn = _asyncpg_dsn(database_url)
    logger.info("sse_listener: starting (channel=%s)", _CHANNEL)

    def _on_notify(_conn, _pid, _channel, payload_str) -> None:
        # The whole body is guarded: an invalid NOTIFY or a failure in publish
        # must never propagate into asyncpg's callback machinery.
        try:
            payload = json.loads(payload_str)
            workspace_id, message, coalesce_key = payload_to_message(payload)
            bus.publish(workspace_id, message, coalesce_key=coalesce_key)
        except Exception:  # noqa: BLE001
            logger.exception("sse_listener: error processing NOTIFY payload=%r", payload_str)

    while not stop_event.is_set():
        conn: asyncpg.Connection | None = None
        try:
            conn = await asyncpg.connect(dsn)
            await conn.add_listener(_CHANNEL, _on_notify)
            logger.info("sse_listener: connected and listening")
            if ready is not None:
                ready.set()
            # Wait until asked to stop; the connection receives NOTIFY in the
            # background via the registered callback.
            await stop_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sse_listener: connection lost, retrying")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=_RECONNECT_DELAY_SECONDS)
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):  # noqa: BLE001
                    await conn.remove_listener(_CHANNEL, _on_notify)
                with contextlib.suppress(Exception):  # noqa: BLE001
                    await asyncio.wait_for(conn.close(), timeout=2.0)

    logger.info("sse_listener: stopped")
