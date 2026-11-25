import asyncio

import pytest

from orcaster_server.sse_bus import SseBus, SseMessage


@pytest.mark.asyncio
async def test_publish_reaches_subscriber_of_same_workspace():
    bus = SseBus()
    q = bus.subscribe("ws-A")
    bus.publish("ws-A", SseMessage(event="task-1"))
    msg = await asyncio.wait_for(q.get(), timeout=1.0)
    assert msg == SseMessage(event="task-1")


@pytest.mark.asyncio
async def test_publish_does_not_cross_workspaces():
    bus = SseBus()
    q_b = bus.subscribe("ws-B")
    bus.publish("ws-A", SseMessage(event="task-1"))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q_b.get(), timeout=0.2)


@pytest.mark.asyncio
async def test_unsubscribe_removes_queue():
    bus = SseBus()
    q = bus.subscribe("ws-A")
    assert bus.subscriber_count("ws-A") == 1
    bus.unsubscribe("ws-A", q)
    assert bus.subscriber_count("ws-A") == 0
    # publishing without subscribers does not blow up
    bus.publish("ws-A", SseMessage(event="task-1"))


@pytest.mark.asyncio
async def test_coalesce_drops_duplicate_within_window(monkeypatch):
    bus = SseBus(coalesce_ms=1000)
    fake = {"t": 100.0}
    monkeypatch.setattr("orcaster_server.sse_bus.time.monotonic", lambda: fake["t"])
    q = bus.subscribe("ws-A")
    # Not applicable without a running loop: this test monkeypatches monotonic
    # and does not run the loop, so the trailing edge degrades to leading-only
    # (no timer is scheduled). We only verify the immediate leading semantics.
    bus.publish("ws-A", SseMessage(event="output-x"), coalesce_key="output-x")
    bus.publish("ws-A", SseMessage(event="output-x"), coalesce_key="output-x")
    # only the first one passed within the window
    assert q.qsize() == 1
    # once the window has elapsed, it passes again
    fake["t"] = 102.0
    bus.publish("ws-A", SseMessage(event="output-x"), coalesce_key="output-x")
    assert q.qsize() == 2


# ---------------------------------------------------------------------------
# Leak in _last_sent (lazy GC + per-workspace purge)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_sent_does_not_grow_unbounded(monkeypatch):
    """Many distinct coalesce_keys with the clock advancing past the window
    must not make `_last_sent` grow monotonically: the amortized GC sweeps the
    stale entries."""
    fake = {"t": 0.0}
    # Monkeypatch BEFORE constructing: `__init__` sets `_last_gc` from
    # monotonic, so it starts aligned with the fake clock.
    monkeypatch.setattr("orcaster_server.sse_bus.time.monotonic", lambda: fake["t"])
    bus = SseBus(coalesce_ms=100)
    bus.subscribe("ws-A")
    # Each key is published once and never repeated; the clock advances a full
    # window between keys, so all of them become stale right away.
    for i in range(1000):
        fake["t"] += 0.2  # 200ms > coalesce_ms (100ms)
        bus.publish("ws-A", SseMessage(event=f"output-{i}"), coalesce_key=f"output-{i}")
    # The amortized GC must have kept the size well below 1000.
    assert len(bus._last_sent) < 50


@pytest.mark.asyncio
async def test_unsubscribe_purges_last_sent_for_empty_workspace(monkeypatch):
    """When a workspace is left without subscribers, its `_last_sent` entries
    are purged."""
    bus = SseBus(coalesce_ms=1000)
    fake = {"t": 100.0}
    monkeypatch.setattr("orcaster_server.sse_bus.time.monotonic", lambda: fake["t"])
    q_a = bus.subscribe("ws-A")
    q_b = bus.subscribe("ws-B")
    bus.publish("ws-A", SseMessage(event="output-x"), coalesce_key="output-x")
    bus.publish("ws-B", SseMessage(event="output-y"), coalesce_key="output-y")
    assert ("ws-A", "output-x") in bus._last_sent
    assert ("ws-B", "output-y") in bus._last_sent
    # Empty ws-A: its entries must disappear, ws-B entries are kept.
    bus.unsubscribe("ws-A", q_a)
    assert ("ws-A", "output-x") not in bus._last_sent
    assert ("ws-B", "output-y") in bus._last_sent
    bus.unsubscribe("ws-B", q_b)
    assert ("ws-B", "output-y") not in bus._last_sent


# ---------------------------------------------------------------------------
# Trailing edge of the throttle (leading + trailing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trailing_edge_flushes_last_dropped_message():
    """The second event dropped by coalescing within the window must be resent
    when the window expires (trailing flush), even if no further event
    arrives."""
    bus = SseBus(coalesce_ms=50)
    q = bus.subscribe("ws-A")
    bus.publish("ws-A", SseMessage(event="output-x", data="first"), coalesce_key="output-x")
    bus.publish("ws-A", SseMessage(event="output-x", data="second"), coalesce_key="output-x")
    # Leading: only the first one passed immediately.
    assert q.qsize() == 1
    first = await asyncio.wait_for(q.get(), timeout=1.0)
    assert first.data == "first"
    # After the window, the trailing flush resends the last dropped one.
    second = await asyncio.wait_for(q.get(), timeout=1.0)
    assert second.data == "second"


@pytest.mark.asyncio
async def test_trailing_edge_flushes_most_recent_dropped():
    """If several events arrive before the flush, the trailing flush resends the
    most recent one, not the intermediate one."""
    bus = SseBus(coalesce_ms=50)
    q = bus.subscribe("ws-A")
    bus.publish("ws-A", SseMessage(event="output-x", data="first"), coalesce_key="output-x")
    bus.publish("ws-A", SseMessage(event="output-x", data="second"), coalesce_key="output-x")
    bus.publish("ws-A", SseMessage(event="output-x", data="third"), coalesce_key="output-x")
    first = await asyncio.wait_for(q.get(), timeout=1.0)
    assert first.data == "first"
    trailing = await asyncio.wait_for(q.get(), timeout=1.0)
    assert trailing.data == "third"
    # Nothing else must remain.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.2)


@pytest.mark.asyncio
async def test_trailing_timer_on_empty_workspace_is_noop():
    """A trailing timer that fires on a workspace that already has no
    subscribers must not blow up."""
    bus = SseBus(coalesce_ms=50)
    q = bus.subscribe("ws-A")
    bus.publish("ws-A", SseMessage(event="output-x", data="first"), coalesce_key="output-x")
    bus.publish("ws-A", SseMessage(event="output-x", data="second"), coalesce_key="output-x")
    # Unsubscribe before the trailing flush fires.
    bus.unsubscribe("ws-A", q)
    # Wait past the window: the timer (if one is left) must not blow up.
    await asyncio.sleep(0.1)
    assert bus.subscriber_count("ws-A") == 0


# ---------------------------------------------------------------------------
# Per-subscription topic filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_with_topics_filters_events():
    """A subscriber with topics={'output'} receives output-x but NOT task-1."""
    bus = SseBus()
    q = bus.subscribe("ws-A", topics=frozenset({"output"}))
    bus.publish("ws-A", SseMessage(event="task-1"))
    bus.publish("ws-A", SseMessage(event="output-x"))
    msg = await asyncio.wait_for(q.get(), timeout=1.0)
    assert msg.event == "output-x"
    # task-1 must not be in the queue.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.2)


@pytest.mark.asyncio
async def test_subscribe_without_topics_receives_all():
    """A subscriber with topics=None receives all types."""
    bus = SseBus()
    q = bus.subscribe("ws-A")
    bus.publish("ws-A", SseMessage(event="task-1"))
    bus.publish("ws-A", SseMessage(event="output-x"))
    first = await asyncio.wait_for(q.get(), timeout=1.0)
    second = await asyncio.wait_for(q.get(), timeout=1.0)
    assert {first.event, second.event} == {"task-1", "output-x"}
