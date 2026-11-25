"""Tests for the SSE endpoint GET /admin/events.

Note on test strategy:
- httpx `ASGITransport` does not support real streaming: it buffers the whole
  response before returning it (`await self.app(scope, receive, send)` in
  handle_async_request). This makes `client.stream()` hang on an infinite SSE
  response.
- Therefore the streaming tests exercise the generator directly (without httpx),
  mocking `Request` and `WorkspaceContext`. The 404 test does use the client because
  the endpoint returns immediately (no generator).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from orcaster_server.api.admin_ui import format_sse
from orcaster_server.sse_bus import SseMessage


def test_format_sse_with_data():
    out = format_sse(SseMessage(event="output-1", data="x"))
    assert out == "event: output-1\ndata: x\n\n"


def test_format_sse_empty_data():
    out = format_sse(SseMessage(event="task-1"))
    assert out == "event: task-1\ndata: \n\n"


def test_format_sse_multiline_data_prefixes_each_line():
    """`data` with `\\n` emits one `data:` per line (SSE protocol)."""
    out = format_sse(SseMessage(event="log-1", data="line one\nline two\nline three"))
    assert out == "event: log-1\ndata: line one\ndata: line two\ndata: line three\n\n"


def test_format_sse_normalizes_crlf_and_cr():
    """`\\r\\n` and `\\r` are normalized to `\\n` before prefixing."""
    out = format_sse(SseMessage(event="log-2", data="a\r\nb\rc"))
    assert out == "event: log-2\ndata: a\ndata: b\ndata: c\n\n"


@pytest.mark.asyncio
async def test_events_endpoint_streams_published_message(monkeypatch):
    """The SSE generator emits the message published in the right workspace."""
    from orcaster_server import main as main_module
    from orcaster_server.api.admin_ui import admin_events
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    monkeypatch.setattr(settings, "sse_heartbeat_interval_seconds", 5)

    workspace_id = "ws-test-stream"

    # Request mock: is_disconnected always returns False.
    mock_request = MagicMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)
    mock_request.query_params = {}

    # WorkspaceContext mock with the active workspace.
    mock_ctx = MagicMock()
    mock_ctx.workspace_id = workspace_id

    # Publish after a small delay so the generator has time to subscribe.
    async def publish_soon():
        await asyncio.sleep(0.1)
        main_module.sse_bus_instance.publish(workspace_id, SseMessage(event="task-42"))

    asyncio.create_task(publish_soon())

    # Invoke the handler and consume the generator until the expected event arrives.
    streaming_resp = await admin_events(mock_request, mock_ctx)
    assert streaming_resp.media_type == "text/event-stream"

    received = b""

    async def _consume():
        nonlocal received
        async for chunk in streaming_resp.body_iterator:
            received += chunk.encode() if isinstance(chunk, str) else chunk
            if b"task-42" in received:
                break

    # Timeout guard: if the expected chunk never arrives, the test fails in ~5s.
    await asyncio.wait_for(_consume(), timeout=5.0)

    # Deterministic close so the handler's finally (unsubscribe) runs now.
    await streaming_resp.body_iterator.aclose()

    assert b": connected" in received
    assert b"event: task-42" in received

    # Verify the subscribed queue is gone (no leak).
    assert main_module.sse_bus_instance.subscriber_count(workspace_id) == 0


@pytest.mark.asyncio
async def test_events_endpoint_passes_topics_filter_to_subscribe(monkeypatch):
    """`?topics=output` is translated to the right frozenset passed to subscribe."""
    from orcaster_server import main as main_module
    from orcaster_server.api.admin_ui import admin_events
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    monkeypatch.setattr(settings, "sse_heartbeat_interval_seconds", 5)

    workspace_id = "ws-test-topics"

    # Request mock with query param topics=output.
    mock_request = MagicMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)
    mock_request.query_params = {"topics": "output"}

    mock_ctx = MagicMock()
    mock_ctx.workspace_id = workspace_id

    # Spy on subscribe in the real bus instance.
    real_subscribe = main_module.sse_bus_instance.subscribe
    captured = {}

    def spy_subscribe(ws, topics=None):
        captured["workspace_id"] = ws
        captured["topics"] = topics
        return real_subscribe(ws, topics=topics)

    monkeypatch.setattr(main_module.sse_bus_instance, "subscribe", spy_subscribe)

    streaming_resp = await admin_events(mock_request, mock_ctx)
    # Start the generator (first chunk `: connected`) so the finally with
    # unsubscribe runs on close.
    await streaming_resp.body_iterator.__anext__()
    await streaming_resp.body_iterator.aclose()

    assert captured["workspace_id"] == workspace_id
    assert captured["topics"] == frozenset({"output"})

    # The subscribed queue must be gone.
    assert main_module.sse_bus_instance.subscriber_count(workspace_id) == 0


@pytest.mark.asyncio
async def test_events_endpoint_no_topics_param_subscribes_all(monkeypatch):
    """Without the `topics` query param, subscribe is called with topics=None."""
    from orcaster_server import main as main_module
    from orcaster_server.api.admin_ui import admin_events
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    monkeypatch.setattr(settings, "sse_heartbeat_interval_seconds", 5)

    workspace_id = "ws-test-no-topics"

    mock_request = MagicMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)
    mock_request.query_params = {}

    mock_ctx = MagicMock()
    mock_ctx.workspace_id = workspace_id

    real_subscribe = main_module.sse_bus_instance.subscribe
    captured = {}

    def spy_subscribe(ws, topics=None):
        captured["topics"] = topics
        return real_subscribe(ws, topics=topics)

    monkeypatch.setattr(main_module.sse_bus_instance, "subscribe", spy_subscribe)

    streaming_resp = await admin_events(mock_request, mock_ctx)
    await streaming_resp.body_iterator.__anext__()
    await streaming_resp.body_iterator.aclose()

    assert captured["topics"] is None
    assert main_module.sse_bus_instance.subscriber_count(workspace_id) == 0


@pytest.mark.asyncio
async def test_events_endpoint_omits_hop_by_hop_connection_header(monkeypatch):
    """Does not emit the hop-by-hop `Connection` header (invalid under HTTP/2)."""
    from orcaster_server.api.admin_ui import admin_events
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    monkeypatch.setattr(settings, "sse_heartbeat_interval_seconds", 5)

    mock_request = MagicMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)
    mock_request.query_params = {}

    mock_ctx = MagicMock()
    mock_ctx.workspace_id = "ws-test-headers"

    streaming_resp = await admin_events(mock_request, mock_ctx)
    # Start and close the generator so the finally (unsubscribe) runs.
    await streaming_resp.body_iterator.__anext__()
    await streaming_resp.body_iterator.aclose()

    assert "connection" not in {k.lower() for k in streaming_resp.headers}
    assert streaming_resp.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_events_endpoint_404_when_disabled(authed_admin_client, monkeypatch):
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", False)
    resp = await authed_admin_client.get("/admin/events")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_events_isolation_cross_workspace(monkeypatch):
    """Messages published in another workspace do not reach the subscribed client."""
    from orcaster_server import main as main_module
    from orcaster_server.api.admin_ui import admin_events
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    monkeypatch.setattr(settings, "sse_heartbeat_interval_seconds", 1)

    workspace_id = "ws-test-isolation"

    mock_request = MagicMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)
    mock_request.query_params = {}

    mock_ctx = MagicMock()
    mock_ctx.workspace_id = workspace_id

    # Publish in ANOTHER workspace; the client subscribed to ws-test-isolation must not get it.
    async def publish_other():
        await asyncio.sleep(0.2)
        main_module.sse_bus_instance.publish("ws-OTHER", SseMessage(event="task-999"))

    asyncio.create_task(publish_other())

    streaming_resp = await admin_events(mock_request, mock_ctx)

    received = b""

    async def _consume():
        nonlocal received
        async for chunk in streaming_resp.body_iterator:
            received += chunk.encode() if isinstance(chunk, str) else chunk
            if b": ping" in received:  # keepalive arrived; nothing from the other workspace
                break

    # Timeout guard: if the ping never arrives, the test fails in ~5s.
    await asyncio.wait_for(_consume(), timeout=5.0)

    # Deterministic close so the handler's finally (unsubscribe) runs now.
    await streaming_resp.body_iterator.aclose()

    assert b"task-999" not in received

    # Verify the subscribed queue is gone (no leak).
    assert main_module.sse_bus_instance.subscriber_count(workspace_id) == 0


@pytest.mark.asyncio
async def test_events_endpoint_sends_keepalive(monkeypatch):
    """The generator emits `: ping` when there are no messages after the heartbeat."""
    from orcaster_server import main as main_module  # noqa: F401
    from orcaster_server.api.admin_ui import admin_events
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "sse_enabled", True)
    monkeypatch.setattr(settings, "sse_heartbeat_interval_seconds", 1)

    workspace_id = "ws-test-heartbeat"

    mock_request = MagicMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)
    mock_request.query_params = {}

    mock_ctx = MagicMock()
    mock_ctx.workspace_id = workspace_id

    # Publish nothing: after 1s the keepalive ping must arrive.
    streaming_resp = await admin_events(mock_request, mock_ctx)

    received = b""

    async def _consume():
        nonlocal received
        async for chunk in streaming_resp.body_iterator:
            received += chunk.encode() if isinstance(chunk, str) else chunk
            if b": ping" in received:
                break

    # Timeout guard: if the ping never arrives, the test fails in ~5s.
    await asyncio.wait_for(_consume(), timeout=5.0)

    # Deterministic close so the handler's finally (unsubscribe) runs now.
    await streaming_resp.body_iterator.aclose()

    assert b": connected" in received
    assert b": ping" in received
