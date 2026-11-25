"""Per-client limit for the anonymous passkey routes.

`POST /auth/login/begin` and `POST /auth/accept/{token}/register/begin` insert
a row in `webauthn_challenges` without a prior credential. Without a limit, an
anonymous client evicts (via `challenge_max_pending`) the pending challenge of other
users and forces them to retry. The limit acts BEFORE the insert: a rejected
request does not touch the table. Anonymous `complete` calls (`login/complete`,
`register/complete`) are also limited, with an instance of their own and the
same settings: they do not share buckets with the `begin` ones, so an admitted
`begin` always has its `complete` token.

In-process token bucket with two levels: the per-client bucket
(IPv4 /32, IPv6 /64) and a coarser per-network one (IPv4 /24, IPv6 /48),
so that rotating addresses within one network does not multiply the cap.
Known limits:

- It is per process: with `UVICORN_WORKERS=N` the effective per-client cap is up to
  N times the configured one (balancing across processes spreads it).
- It is lost on restart: after a restart each client has a full bucket
  again.
- The bucket tables are bounded to `MAX_TRACKED_CLIENTS` keys per level.
  With the table full, a new key displaces the least recent one only if the latter,
  refilled to now, is full (equivalent to a new bucket); otherwise the new
  key is not tracked and draws from a single shared overflow bucket.
  A sweep of more than 10 000 keys shares that bucket with the legitimate new
  clients that arrive while it lasts.
- The key is the IP resolved by the app (`request.client.host`, rewritten
  by uvicorn's `ProxyHeadersMiddleware` only if the peer is trusted,
  `--forwarded-allow-ips`, 127.0.0.1 by default). This module does NOT read
  `X-Forwarded-For`: a header from an untrusted peer does not change the key.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orcaster_server.auth.config import AuthConfig

logger = logging.getLogger(__name__)

# Distinct clients (and networks) remembered at once. Bounds memory against
# an IP sweep. When exceeded, a bucket that is not full is NOT forgotten (that
# would give a full burst to a client that exhausted it): see `_BucketSet`.
MAX_TRACKED_CLIENTS = 10_000

# No client address (e.g. ASGI transport without `client`): a single bucket.
_UNKNOWN_CLIENT = "unknown"

# An IPv6 client controls a whole /64: it is grouped by prefix, or rotating
# addresses within its /64 would evade the limit.
_IPV6_PREFIX_BITS = 64

# Prefix of the coarse bucket: rotating among addresses of an IPv4 /24 or of
# an IPv6 /48 (the usual allocation to a site) falls into a single bucket.
_GROUP_IPV4_PREFIX_BITS = 24
_GROUP_IPV6_PREFIX_BITS = 48


def client_key(host: str | None) -> str:
    """Bucket key for the IP already resolved by the app."""
    if not host:
        return _UNKNOWN_CLIENT
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return _UNKNOWN_CLIENT
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        net = ipaddress.ip_network(f"{addr}/{_IPV6_PREFIX_BITS}", strict=False)
        return str(net)
    return str(addr)


def group_key(host: str | None) -> str:
    """Key of the coarse bucket (IPv4 /24, IPv6 /48) for the already resolved IP."""
    if not host:
        return _UNKNOWN_CLIENT
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return _UNKNOWN_CLIENT
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        else:
            return str(ipaddress.ip_network(f"{addr}/{_GROUP_IPV6_PREFIX_BITS}", strict=False))
    return str(ipaddress.ip_network(f"{addr}/{_GROUP_IPV4_PREFIX_BITS}", strict=False))


@dataclass
class _Bucket:
    tokens: float
    updated: float

    def refill(self, now: float, *, rate: float, burst: int) -> None:
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(float(burst), self.tokens + elapsed * rate)
        self.updated = now


class _BucketSet:
    """Token buckets per key, with memory bounded to `max_keys` keys.

    With the table full, a new key displaces the least recent one only if
    the latter, refilled to now, is full: then it equals a new bucket and
    forgetting it gives nothing away. If it is not full, the new key is not tracked and
    draws from a single overflow bucket (same rate and burst). Thus a client
    with an empty bucket does not recover a full burst because another 10 000
    keys pass through the limiter, and memory stays bounded."""

    def __init__(self, max_keys: int) -> None:
        self.max_keys = max_keys
        self.buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self.overflow: _Bucket | None = None

    def clear(self) -> None:
        self.buckets.clear()
        self.overflow = None

    def get(self, key: str, now: float, *, rate: float, burst: int) -> _Bucket:
        """Bucket of `key` refilled to `now`, without consuming. Creates it if new."""
        bucket = self.buckets.get(key)
        if bucket is not None:
            self.buckets.move_to_end(key)
            bucket.refill(now, rate=rate, burst=burst)
            return bucket
        if len(self.buckets) >= self.max_keys:
            oldest_key, oldest = next(iter(self.buckets.items()))
            oldest.refill(now, rate=rate, burst=burst)
            if oldest.tokens < burst:
                if self.overflow is None:
                    self.overflow = _Bucket(tokens=float(burst), updated=now)
                self.overflow.refill(now, rate=rate, burst=burst)
                return self.overflow
            del self.buckets[oldest_key]
        bucket = _Bucket(tokens=float(burst), updated=now)
        self.buckets[key] = bucket
        return bucket


def _wait_seconds(bucket: _Bucket, rate: float) -> int:
    """Whole seconds (>= 1) until `bucket` has a token."""
    # Rounding absorbs floating-point error (e.g. 7.999999999 -> 8,
    # 8.000000001 -> 8) before the ceiling.
    return max(1, math.ceil(round((1.0 - bucket.tokens) / rate, 6)))


def limiter_from_config(config: AuthConfig) -> BeginRateLimiter:
    """Limiter with the `begin_*` settings of `config` (`begin` and
    `complete` use one instance each, with these same values)."""
    return BeginRateLimiter(
        config.begin_rate_per_minute,
        config.begin_rate_burst,
        group_per_minute=config.begin_group_rate_per_minute,
        group_burst=config.begin_group_burst,
    )


class BeginRateLimiter:
    """Token bucket per client and per network. `per_minute == 0` disables the
    per-client level; `group_per_minute == 0`, the per-network level."""

    def __init__(
        self,
        per_minute: int,
        burst: int,
        *,
        group_per_minute: int = 0,
        group_burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self.configure(per_minute, burst, group_per_minute, group_burst)
        self._clock = clock
        self._clients = _BucketSet(max_clients)
        self._groups = _BucketSet(max_clients)
        self._rejections = BoundedWarning(
            logger,
            "auth: %d anonymous passkey request(s) rejected by the rate limit (429)",
        )

    @property
    def _buckets(self) -> OrderedDict[str, _Bucket]:
        return self._clients.buckets

    def configure(
        self,
        per_minute: int,
        burst: int,
        group_per_minute: int | None = None,
        group_burst: int | None = None,
    ) -> None:
        """Omitted per-network level parameters keep their value."""
        self.per_minute = per_minute
        self.burst = burst
        self._rate = per_minute / 60.0
        if group_per_minute is not None:
            self.group_per_minute = group_per_minute
        if group_burst is not None:
            self.group_burst = group_burst
        self._group_rate = self.group_per_minute / 60.0

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0 or self.group_per_minute > 0

    def reset(self) -> None:
        self._clients.clear()
        self._groups.clear()
        self._rejections.reset()

    def check(self, host: str | None) -> int | None:
        """None if the request passes; otherwise, seconds (>= 1) until it can pass.

        First both levels are refilled and inspected; only if both have
        a token is one consumed from each. A rejected request consumes
        nothing: neither its network's bucket (a flooder does not exhaust its
        neighbours) nor its own. `Retry-After` is the longest of the waits of the
        levels without a token."""
        if not self.enabled:
            return None
        now = self._clock()
        levels: list[tuple[_Bucket, float]] = []
        if self.per_minute > 0:
            bucket = self._clients.get(client_key(host), now, rate=self._rate, burst=self.burst)
            levels.append((bucket, self._rate))
        if self.group_per_minute > 0:
            bucket = self._groups.get(
                group_key(host), now, rate=self._group_rate, burst=self.group_burst
            )
            levels.append((bucket, self._group_rate))
        waits = [_wait_seconds(b, rate) for b, rate in levels if b.tokens < 1.0]
        if waits:
            self._rejections.hit()
            return max(waits)
        for bucket, _ in levels:
            bucket.tokens -= 1.0
        return None


class BoundedWarning:
    """WARNING with a count, at most one per `interval` seconds.

    A flood must not produce one log line per request. Events that fall
    within the window accumulate and come out in one line when the window closes
    (a `call_later` of the event loop), so a burst that ends before
    another event arrives is also reported in full. With no running event
    loop (synchronous tests) the count comes out with the next event after the
    window. The message is fixed: it quotes no client value.

    `args` are the fixed arguments that precede the count in the template
    (`template % (*args, n)`): they allow a template with server-side ids
    without formatting the message by hand."""

    def __init__(
        self,
        logger: logging.Logger,
        template: str,
        *,
        interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        args: tuple[object, ...] = (),
    ) -> None:
        self._logger = logger
        self._template = template
        self._interval = interval
        self._clock = clock
        self._args = args
        self._pending = 0
        self._last: float | None = None
        self._flush_handle: asyncio.TimerHandle | None = None

    def reset(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        self._flush_handle = None
        self._pending = 0
        self._last = None

    def _emit(self) -> None:
        # Cancel the deferred timer (if the timer itself triggers the flush,
        # cancelling it is harmless): a direct emission must not leave it alive, or
        # it would fire an early line in the new window and orphan the
        # handle of the next timer.
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        self._flush_handle = None
        if not self._pending:
            return
        n, self._pending = self._pending, 0
        self._last = self._clock()
        self._logger.warning(self._template, *self._args, n)

    def flush(self) -> None:
        """Emit the pending count now (if any) and cancel the deferred flush."""
        self._emit()

    def hit(self, count: int = 1) -> None:
        self._pending += count
        now = self._clock()
        if self._last is None or now - self._last >= self._interval:
            self._emit()
            return
        if self._flush_handle is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._flush_handle = loop.call_later(
                max(0.0, self._interval - (now - self._last)), self._emit
            )


class KeyedBoundedWarning:
    """`BoundedWarning` per key, with a cap on remembered keys (LRU).

    One line per (key, window) instead of one per event: the count of what was
    suppressed comes out in the next line of that key. Reuses the mechanics
    of `BoundedWarning` (window, count, flush on close); it only adds the
    per-key map. Known limits:

    - It is per process and is lost on restart, like the rest of the module.
    - When `max_keys` is exceeded the least recent key is forgotten. Its pending
      count is emitted at that moment (it is not lost silently) and, if the
      key returns, it opens a new window and emits immediately: the memory cap
      is kept at the cost of relaxing the line cap only while there are more than
      `max_keys` active keys at once.
    - The message is fixed: `args` must carry server ids, never client values
      (nor the token)."""

    def __init__(
        self,
        logger: logging.Logger,
        template: str,
        *,
        interval: float = 60.0,
        max_keys: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._template = template
        self._interval = interval
        self._max_keys = max_keys
        self._clock = clock
        self._entries: OrderedDict[str, BoundedWarning] = OrderedDict()

    def reset(self) -> None:
        for warn in self._entries.values():
            warn.reset()
        self._entries.clear()

    def hit(self, key: str, *args: object) -> None:
        warn = self._entries.get(key)
        if warn is None:
            warn = BoundedWarning(
                self._logger,
                self._template,
                interval=self._interval,
                clock=self._clock,
                args=args,
            )
            self._entries[key] = warn
            if len(self._entries) > self._max_keys:
                _, evicted = self._entries.popitem(last=False)
                evicted.flush()
        else:
            self._entries.move_to_end(key)
        warn.hit()
