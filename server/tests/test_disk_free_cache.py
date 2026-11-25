"""Unit tests for the TTL cache of free disk space.

They touch neither the DB nor the real filesystem: `storage.disk_free_bytes` is
replaced by a stub that counts calls. The clock is injected to control
expiration without sleeps.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from orcaster_server import storage
from orcaster_server.storage import DiskFreeCache

ROOT = Path("/artifacts-root-de-test")
OTHER_ROOT = Path("/other-artifacts-root")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _stub_measure(monkeypatch, values: list[int], calls: list[Path]) -> None:
    """Replace the measurement with a stub that returns `values` in order."""
    seq = iter(values)

    def _measure(root: Path) -> int:
        calls.append(root)
        return next(seq)

    monkeypatch.setattr(storage, "disk_free_bytes", _measure)


@pytest.mark.asyncio
async def test_hit_within_ttl_skips_statvfs(monkeypatch) -> None:
    clock = _Clock()
    cache = DiskFreeCache(clock=clock)
    calls: list[Path] = []
    _stub_measure(monkeypatch, [100, 200], calls)

    first = await cache.get(ROOT, 5.0)
    clock.now = 4.9
    second = await cache.get(ROOT, 5.0)

    assert first == 100
    assert second == 100  # served the cached value, did not measure again
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expired_entry_remeasures(monkeypatch) -> None:
    clock = _Clock()
    cache = DiskFreeCache(clock=clock)
    calls: list[Path] = []
    _stub_measure(monkeypatch, [100, 200], calls)

    await cache.get(ROOT, 5.0)
    clock.now = 5.1  # the entry expires at now=5.0 (exp > now to be a hit)
    result = await cache.get(ROOT, 5.0)

    assert result == 200
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_ttl_zero_is_passthrough(monkeypatch) -> None:
    """TTL=0 => always measure, no caching (semantics used by the tests)."""
    clock = _Clock()
    cache = DiskFreeCache(clock=clock)
    calls: list[Path] = []
    _stub_measure(monkeypatch, [100, 200], calls)

    assert await cache.get(ROOT, 0.0) == 100
    assert await cache.get(ROOT, 0.0) == 200
    assert len(calls) == 2
    assert cache._entries == {}  # nothing left cached


@pytest.mark.asyncio
async def test_roots_are_independent(monkeypatch) -> None:
    clock = _Clock()
    cache = DiskFreeCache(clock=clock)
    calls: list[Path] = []
    _stub_measure(monkeypatch, [100, 200], calls)

    a = await cache.get(ROOT, 5.0)
    b = await cache.get(OTHER_ROOT, 5.0)

    assert (a, b) == (100, 200)
    assert calls == [ROOT, OTHER_ROOT]


@pytest.mark.asyncio
async def test_concurrent_misses_single_flight(monkeypatch) -> None:
    """N concurrent polls with a cold cache => a single statvfs."""
    clock = _Clock()
    cache = DiskFreeCache(clock=clock)
    calls: list[Path] = []
    gate = threading.Event()

    def _measure(root: Path) -> int:
        calls.append(root)
        gate.wait(5.0)  # keeps the measurement in flight until the gate is released
        return 100

    monkeypatch.setattr(storage, "disk_free_bytes", _measure)

    task = asyncio.gather(*(cache.get(ROOT, 5.0) for _ in range(5)))
    await asyncio.sleep(0.05)  # let the first one take the lock and measure
    gate.set()
    results = await task

    assert results == [100] * 5
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stale_value_served_while_refresh_in_flight(monkeypatch) -> None:
    """After expiry, the refresh runs once and the others reuse the stale value.

    This takes statvfs off the critical path of the poll: no request waits
    for the syscall except the very first one (cold cache).
    """
    clock = _Clock()
    cache = DiskFreeCache(clock=clock)
    calls: list[Path] = []
    gate = threading.Event()
    gate.set()  # the first measurement does not block

    def _measure(root: Path) -> int:
        calls.append(root)
        gate.wait(5.0)
        return 100 * len(calls)

    monkeypatch.setattr(storage, "disk_free_bytes", _measure)

    assert await cache.get(ROOT, 5.0) == 100
    clock.now = 5.1  # expirada
    gate.clear()  # the refresh stays in flight

    refresh = asyncio.ensure_future(cache.get(ROOT, 5.0))
    await asyncio.sleep(0.05)
    assert cache._locks[ROOT].locked()  # the refresh holds the lock

    assert await cache.get(ROOT, 5.0) == 100  # stale value, without waiting

    gate.set()
    assert await refresh == 200
    assert len(calls) == 2  # never two simultaneous statvfs calls


@pytest.mark.asyncio
async def test_measurement_runs_off_the_event_loop(monkeypatch) -> None:
    """The syscall runs in a thread, not in the event loop."""
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def _measure(_root: Path) -> int:
        seen.append(threading.current_thread())
        return 100

    monkeypatch.setattr(storage, "disk_free_bytes", _measure)
    cache = DiskFreeCache(clock=_Clock())

    await cache.get(ROOT, 5.0)  # with cache
    await cache.get(OTHER_ROOT, 0.0)  # passthrough

    assert len(seen) == 2
    assert all(t is not loop_thread for t in seen)


@pytest.mark.asyncio
async def test_clear_forgets_entries(monkeypatch) -> None:
    clock = _Clock()
    cache = DiskFreeCache(clock=clock)
    calls: list[Path] = []
    _stub_measure(monkeypatch, [100, 200], calls)

    await cache.get(ROOT, 5.0)
    cache.clear()
    assert cache._entries == {}
    assert cache._locks == {}

    result = await cache.get(ROOT, 5.0)

    assert result == 200  # measured again: clear dropped the live entry
    assert len(calls) == 2
