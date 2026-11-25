"""In-process cache of the public snapshot (defense in depth).

TTL + single-flight: a valid hit saves the snapshot aggregation (the seq scan of
_SQL_TASKS_BY_VARIANT_STATUS and friends); token resolution still queries the DB
on every request. On a miss, the first request builds and concurrent ones wait on
the per-key lock and reuse the result (anti-stampede). Share-token resolution
stays OUTSIDE the cache: revocation remains immediate and the last_seen_at touch
keeps its own throttle (status/tokens.py).

Module-level singleton. Tests reset it via clear() from the app_client fixture
(same pattern as signing.reset_key_cache). The TTL is passed by the caller on
each call (read from settings), so the module stays free of config and
monkeypatching settings takes effect immediately.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from orcaster_server.status.snapshot import StatusSnapshot

# (workspace_id, variant_param) - the token does NOT take part: N tokens of the same
# workspace share a snapshot, and a revoked token never gets this far.
_Key = tuple[str, str]

# Cap of the locks dict. `variant` comes from the query string of an anonymous
# endpoint: the key cardinality is NOT "workspaces x real variants", it is
# whatever the client chooses to request. The handler bounds the size of the value
# (strip + max_length=64), and this cap bounds the count: without it, each unseen
# variant would leave a lock retained forever (monotonic growth per request).
_LOCKS_MAX = 1024


class SnapshotCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[_Key, tuple[float, StatusSnapshot]] = {}
        # Entries are pruned by expiry; locks, by cap (_LOCKS_MAX).
        self._locks: dict[_Key, asyncio.Lock] = {}

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()

    def _prune_locks(self) -> None:
        """Discards the locks that are NOT held; keeps those with a build in flight.

        A held lock (`locked()`) protects a running builder: discarding it would
        break the single-flight, so it stays.

        Accepted benign race: a lock can be free and have pending waiters (asyncio
        releases `_locked` before the waiter wakes up and takes it again). If the
        pruning falls in that window, the waiter continues with the orphaned lock
        while a new request creates another one for the same key -> at most ONE
        duplicated build. The cache remains correct: both builds produce the same
        snapshot and the write to `_entries` is idempotent. There is never
        corruption or data crossing between keys.
        """
        self._locks = {k: lock for k, lock in self._locks.items() if lock.locked()}

    def _fresh(self, key: _Key, now: float) -> StatusSnapshot | None:
        entry = self._entries.get(key)
        if entry is not None and entry[0] > now:
            return entry[1]
        return None

    async def get_or_build(
        self,
        key: _Key,
        ttl_s: float,
        build: Callable[[], Awaitable[StatusSnapshot]],
    ) -> StatusSnapshot:
        if ttl_s <= 0:
            return await build()
        snap = self._fresh(key, self._clock())
        if snap is not None:
            return snap
        if key not in self._locks and len(self._locks) >= _LOCKS_MAX:
            self._prune_locks()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._clock()
            # Re-check after the lock: another request may have built while we
            # were waiting.
            snap = self._fresh(key, now)
            if snap is not None:
                return snap
            snap = await build()
            # Opportunistic pruning of expired entries (atomic replacement of the
            # dict: lock-free readers see the old or the new dict, both valid).
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
            self._entries[key] = (now + ttl_s, snap)
            return snap


snapshot_cache = SnapshotCache()
