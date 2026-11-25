import asyncio

import pytest

from orcaster_server.sse_bus import SseBus, SseMessage
from orcaster_server.sse_listener import dashboard_listen_loop, payload_to_message


def test_payload_to_message_task():
    ws, msg, ck = payload_to_message({"type": "task", "id": "5", "workspace_id": "ws-A"})
    assert ws == "ws-A"
    assert msg == SseMessage(event="task-5")
    assert ck is None


def test_payload_to_message_output_has_coalesce_key():
    ws, msg, ck = payload_to_message(
        {"type": "output", "execution_id": "ex-1", "workspace_id": "ws-A"}
    )
    assert ws == "ws-A"
    assert msg == SseMessage(event="output-ex-1")
    assert ck == "output-ex-1"


@pytest.mark.asyncio
async def test_listen_loop_publishes_to_bus(postgres_url):
    bus = SseBus()
    q = bus.subscribe("ws-A")
    stop = asyncio.Event()
    ready = asyncio.Event()
    task = asyncio.create_task(dashboard_listen_loop(postgres_url, bus, stop, ready=ready))
    await asyncio.wait_for(ready.wait(), timeout=5.0)  # wait for the connection and LISTEN
    # emit a manual NOTIFY from another connection
    import asyncpg

    dsn = postgres_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        'SELECT pg_notify(\'dashboard_events\', \'{"type":"task","id":"9","workspace_id":"ws-A"}\')'
    )
    await conn.close()
    msg = await asyncio.wait_for(q.get(), timeout=3.0)
    assert msg == SseMessage(event="task-9")
    stop.set()
    await asyncio.wait_for(task, timeout=3.0)


@pytest.mark.asyncio
async def test_listen_loop_publishes_output_event(postgres_url):
    """Verify that an `output` NOTIFY arrives as SseMessage(event='output-<eid>')."""
    bus = SseBus()
    q = bus.subscribe("ws-A")
    stop = asyncio.Event()
    ready = asyncio.Event()
    task = asyncio.create_task(dashboard_listen_loop(postgres_url, bus, stop, ready=ready))
    await asyncio.wait_for(ready.wait(), timeout=5.0)  # wait for the connection and LISTEN
    import asyncpg

    dsn = postgres_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "SELECT pg_notify('dashboard_events',"
        ' \'{"type":"output","execution_id":"ex-77","workspace_id":"ws-A"}\')'
    )
    await conn.close()
    msg = await asyncio.wait_for(q.get(), timeout=3.0)
    assert msg == SseMessage(event="output-ex-77")
    stop.set()
    await asyncio.wait_for(task, timeout=3.0)
