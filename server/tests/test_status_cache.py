"""Unit tests for the in-process cache of the public snapshot.

They do not touch the DB: the builder is an async stub. The clock is injected to
control expiration without sleeps.
"""

from __future__ import annotations

import asyncio

import pytest

from orcaster_server.status.cache import _LOCKS_MAX, SnapshotCache


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _builder(calls: list[int], value: object):
    async def build() -> object:
        calls.append(1)
        await asyncio.sleep(0)
        return value

    return build


class _Boom(Exception):
    """Test exception: simulates a failing builder."""


def _boom_builder(calls: list[int]):
    async def build() -> object:
        calls.append(1)
        await asyncio.sleep(0)
        raise _Boom("build failed")

    return build


@pytest.mark.asyncio
async def test_hit_within_ttl_skips_builder() -> None:
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []
    key = ("ws-1", "")

    first = await cache.get_or_build(key, 10.0, _builder(calls, "v1"))
    clock.now = 5.0
    second = await cache.get_or_build(key, 10.0, _builder(calls, "v2"))

    assert first == "v1"
    assert second == "v1"  # served the cached value, did not rebuild
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expired_entry_rebuilds() -> None:
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []
    key = ("ws-1", "")

    await cache.get_or_build(key, 10.0, _builder(calls, "v1"))
    clock.now = 10.1  # the entry expires at now=10.0 (exp > now to be a hit)
    result = await cache.get_or_build(key, 10.0, _builder(calls, "v2"))

    assert result == "v2"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_ttl_zero_is_passthrough() -> None:
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []
    key = ("ws-1", "")

    await cache.get_or_build(key, 0.0, _builder(calls, "v1"))
    await cache.get_or_build(key, 0.0, _builder(calls, "v2"))

    assert len(calls) == 2  # no cache: every call builds


@pytest.mark.asyncio
async def test_keys_are_independent() -> None:
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []

    a = await cache.get_or_build(("ws-1", ""), 10.0, _builder(calls, "a"))
    b = await cache.get_or_build(("ws-1", "all"), 10.0, _builder(calls, "b"))
    c = await cache.get_or_build(("ws-2", ""), 10.0, _builder(calls, "c"))

    assert (a, b, c) == ("a", "b", "c")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_concurrent_misses_single_flight() -> None:
    """N concurrent requests on a miss => ONE single build (anti-stampede)."""
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []
    key = ("ws-1", "")

    results = await asyncio.gather(
        *(cache.get_or_build(key, 10.0, _builder(calls, "v1")) for _ in range(5))
    )

    assert all(r == "v1" for r in results)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_locks_dict_is_capped() -> None:
    """Unseen keys do not accumulate locks forever.

    `variant` comes from the query string of an anonymous endpoint: without a cap, each
    new value would leave a lock retained by the process.
    """
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []

    for i in range(_LOCKS_MAX + 5):
        await cache.get_or_build(("ws-1", f"v{i}"), 10.0, _builder(calls, i))

    assert len(calls) == _LOCKS_MAX + 5  # every distinct key built
    assert len(cache._locks) <= _LOCKS_MAX


@pytest.mark.asyncio
async def test_prune_keeps_lock_with_build_in_flight() -> None:
    """Pruning does not drop the lock of an in-flight build (it would break single-flight)."""
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    gate = asyncio.Event()
    held = ("ws-1", "in-flight")

    async def slow_build() -> str:
        await gate.wait()
        return "held"

    task = asyncio.create_task(cache.get_or_build(held, 10.0, slow_build))
    await asyncio.sleep(0)  # let it take the lock and wait on the gate
    assert cache._locks[held].locked()

    calls: list[int] = []
    for i in range(_LOCKS_MAX + 5):
        await cache.get_or_build(("ws-1", f"v{i}"), 10.0, _builder(calls, i))

    assert held in cache._locks  # survived the pruning
    assert cache._locks[held].locked()
    assert len(cache._locks) <= _LOCKS_MAX

    gate.set()
    assert await task == "held"


@pytest.mark.asyncio
async def test_clear_forgets_entries() -> None:
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []
    key = ("ws-1", "")

    await cache.get_or_build(key, 10.0, _builder(calls, "v1"))
    cache.clear()
    result = await cache.get_or_build(key, 10.0, _builder(calls, "v2"))

    assert result == "v2"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_builder_exception_propagates_and_caches_nothing() -> None:
    """If build() raises, the exception propagates to the caller and NOTHING is
    cached under that key (the `async with lock` has no try/except, so it never
    reaches `self._entries[key] = ...`). A later call with a healthy builder
    rebuilds normally: the lock is not poisoned by the previous failure (it was
    released on leaving the `async with`)."""
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []
    key = ("ws-1", "")

    with pytest.raises(_Boom):
        await cache.get_or_build(key, 10.0, _boom_builder(calls))

    assert key not in cache._entries
    assert len(calls) == 1

    result = await cache.get_or_build(key, 10.0, _builder(calls, "v1"))

    assert result == "v1"
    assert cache._entries[key][1] == "v1"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_concurrent_misses_builder_exception_each_waiter_rebuilds() -> None:
    """Behavior PINNED by this test (not necessarily the ideal): unlike the
    successful hit (a single build for N concurrent calls), when the builder raises
    single-flight does NOT share the exception between waiters. Since a failure never
    writes to `_entries`, each waiter queued on the lock retries its own build when
    its turn comes, and each receives the exception from its own attempt. This is
    exactly what the issue describes: "waiters retry the build, each on its turn".
    N concurrent calls with an always-failing builder => N build attempts, N
    exceptions, nothing cached."""
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []
    key = ("ws-1", "")
    n = 5

    results = await asyncio.gather(
        *(cache.get_or_build(key, 10.0, _boom_builder(calls)) for _ in range(n)),
        return_exceptions=True,
    )

    assert len(results) == n
    assert all(isinstance(r, _Boom) for r in results)
    assert len(calls) == n  # no dedup: each waiter tried its own build
    assert key not in cache._entries


@pytest.mark.asyncio
async def test_prune_entries_shrinks_dict_and_keeps_fresh() -> None:
    """The opportunistic pruning of `_entries` on every build SHRINKS the dict
    (it does not merely stop it from growing). Expired and live entries are set up by
    hand (same pattern as the `_locks` tests that already touch private attributes)
    so as not to depend on per-call TTLs, and then a build is triggered on a
    new key to force the pruning."""
    clock = _Clock()
    cache = SnapshotCache(clock=clock)
    calls: list[int] = []

    # Expired for now=100 (exp <= now).
    cache._entries[("ws-1", "old-a")] = (50.0, "stale-a")
    cache._entries[("ws-1", "old-b")] = (99.0, "stale-b")
    # Live for now=100 (exp > now).
    cache._entries[("ws-1", "fresh")] = (200.0, "fresh-value")
    clock.now = 100.0
    assert len(cache._entries) == 3

    result = await cache.get_or_build(("ws-1", "new"), 10.0, _builder(calls, "new-value"))

    assert result == "new-value"
    assert len(calls) == 1
    assert len(cache._entries) == 2  # real shrink: 3 -> 2, not 3 -> 4
    assert set(cache._entries) == {("ws-1", "fresh"), ("ws-1", "new")}
    assert cache._entries[("ws-1", "fresh")][1] == "fresh-value"
