"""Transactional round-trip of the app-side emitter against a real Postgres.

Verify that pg_notify emitted on the active session:
- is delivered after COMMIT,
- is NOT delivered before COMMIT,
- is NOT delivered if the transaction does ROLLBACK.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orcaster_server.sse_notify import dashboard_notifier


def _dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _make_listener(postgres_url: str):
    """Open an asyncpg connection with a listener; return (conn, received, fut, cb)."""
    received: list[dict] = []
    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    def cb(_c, _pid, _channel, payload):
        received.append(json.loads(payload))
        if not fut.done():
            fut.set_result(True)

    conn = await asyncpg.connect(_dsn(postgres_url))
    await conn.add_listener("dashboard_events", cb)
    return conn, received, fut, cb


@pytest.mark.asyncio
async def test_notify_delivered_after_commit(postgres_url, monkeypatch):
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    conn, received, fut, cb = await _make_listener(postgres_url)
    engine = create_async_engine(postgres_url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            await dashboard_notifier.output_appended(s, execution_id="ex-tx", workspace_id="ws-A")
            # Before the commit: nothing delivered yet.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(fut), timeout=0.3)
            await s.commit()
        # After the commit: the NOTIFY arrives.
        await asyncio.wait_for(fut, timeout=3.0)
        assert any(e["type"] == "output" and e["execution_id"] == "ex-tx" for e in received)
    finally:
        await conn.remove_listener("dashboard_events", cb)
        await engine.dispose()
        await conn.close()


@pytest.mark.asyncio
async def test_notify_not_delivered_on_rollback(postgres_url, monkeypatch):
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    conn, received, fut, cb = await _make_listener(postgres_url)
    engine = create_async_engine(postgres_url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            await dashboard_notifier.output_appended(s, execution_id="ex-rb", workspace_id="ws-A")
            await s.rollback()
        # After rollback nothing should arrive within a reasonable window.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(fut, timeout=0.5)
        assert received == []
    finally:
        await conn.remove_listener("dashboard_events", cb)
        await engine.dispose()
        await conn.close()
