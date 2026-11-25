"""Per-client limit on anonymous passkey `begin` requests."""

import asyncio
import base64
import contextlib
import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.rate_limit import (
    MAX_TRACKED_CLIENTS,
    BeginRateLimiter,
    BoundedWarning,
    KeyedBoundedWarning,
    client_key,
    group_key,
)
from orcaster_server.auth.service import passkey as passkey_service
from orcaster_server.auth.service.passkey import _take_challenge, begin_authentication


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- BeginRateLimiter (unit) -----------------------------------------------


def test_bucket_allows_burst_then_rejects_with_retry_after():
    clock = FakeClock()
    limiter = BeginRateLimiter(per_minute=30, burst=3, clock=clock)
    assert [limiter.check("198.51.100.1") for _ in range(3)] == [None, None, None]
    # 30/min = 1 token every 2 s.
    assert limiter.check("198.51.100.1") == 2


def test_bucket_refills_over_time():
    clock = FakeClock()
    limiter = BeginRateLimiter(per_minute=30, burst=2, clock=clock)
    limiter.check("198.51.100.1")
    limiter.check("198.51.100.1")
    assert limiter.check("198.51.100.1") is not None
    clock.now += 2.0
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.1") is not None


def test_bucket_never_exceeds_burst_after_long_idle():
    clock = FakeClock()
    limiter = BeginRateLimiter(per_minute=60, burst=2, clock=clock)
    clock.now += 10_000
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.1") is not None


def test_clients_are_isolated():
    limiter = BeginRateLimiter(per_minute=30, burst=1, clock=FakeClock())
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.1") is not None
    assert limiter.check("198.51.100.2") is None


def test_zero_rate_disables_the_limit():
    limiter = BeginRateLimiter(per_minute=0, burst=1, clock=FakeClock())
    assert all(limiter.check("198.51.100.1") is None for _ in range(100))


def test_tracked_clients_are_bounded():
    limiter = BeginRateLimiter(per_minute=30, burst=1, clock=FakeClock(), max_clients=3)
    for i in range(10):
        limiter.check(f"198.51.100.{i}")
    assert len(limiter._buckets) == 3


def test_drained_client_is_not_reset_by_other_keys_filling_the_table():
    # A client with an empty bucket does not recover a full burst because other
    # keys fill the table. With the clock stopped, nobody refills.
    limiter = BeginRateLimiter(per_minute=30, burst=2, clock=FakeClock(), max_clients=3)
    victim = "198.51.100.1"
    assert limiter.check(victim) is None
    assert limiter.check(victim) is None
    assert limiter.check(victim) is not None
    for i in range(2, 200):
        limiter.check(f"198.51.100.{i}")
    assert limiter.check(victim) is not None
    assert len(limiter._buckets) == 3


def test_drained_client_survives_ten_thousand_other_keys_at_the_default_bound():
    limiter = BeginRateLimiter(per_minute=30, burst=10, clock=FakeClock())
    victim = "198.51.100.1"
    for _ in range(10):
        assert limiter.check(victim) is None
    assert limiter.check(victim) is not None
    for i in range(10_000):
        limiter.check(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
    assert limiter.check(victim) is not None
    assert len(limiter._buckets) <= MAX_TRACKED_CLIENTS


def test_untracked_new_keys_share_one_overflow_bucket():
    # With the table full of non-full buckets, new keys are not tracked: they
    # share a single overflow bucket with the same rate and burst.
    limiter = BeginRateLimiter(per_minute=30, burst=2, clock=FakeClock(), max_clients=2)
    for host in ("198.51.100.1", "198.51.100.2"):
        assert limiter.check(host) is None
    newcomers = [limiter.check(f"203.0.113.{i}") for i in range(1, 6)]
    assert newcomers[:2] == [None, None]
    assert all(r is not None for r in newcomers[2:])
    assert set(limiter._buckets) == {"198.51.100.1", "198.51.100.2"}


def test_least_recent_bucket_is_evicted_once_refilled_to_full():
    # A bucket that, refilled to now, is full is equivalent to a new one: it can
    # be forgotten without granting a burst, and the new key becomes tracked.
    clock = FakeClock()
    limiter = BeginRateLimiter(per_minute=60, burst=2, clock=clock, max_clients=2)
    a, b, c = "198.51.100.1", "198.51.100.2", "198.51.100.3"
    assert limiter.check(a) is None
    clock.now += 0.5
    assert limiter.check(b) is None
    clock.now += 0.6  # `a` is full again; `b` is not yet
    assert limiter.check(c) is None
    assert set(limiter._buckets) == {b, c}


def test_ipv6_clients_share_a_bucket_per_64_prefix():
    assert client_key("2001:db8:1:2::1") == client_key("2001:db8:1:2:ffff::9")
    assert client_key("2001:db8:1:2::1") != client_key("2001:db8:1:3::1")
    assert client_key("::ffff:198.51.100.7") == "198.51.100.7"


@pytest.mark.parametrize("host", [None, "", "not-an-ip", "198.51.100.1/24"])
def test_unparseable_client_falls_into_one_shared_bucket(host):
    assert client_key(host) == "unknown"


# --- coarse per-network level -------------------------------------------------


def test_group_key_is_a_24_for_ipv4_and_a_48_for_ipv6():
    assert group_key("198.51.100.7") == group_key("198.51.100.200") == "198.51.100.0/24"
    assert group_key("198.51.100.7") != group_key("198.51.101.7")
    assert group_key("2001:db8:1:2::1") == group_key("2001:db8:1:ffff:ffff::9")
    assert group_key("2001:db8:1::1") == "2001:db8:1::/48"
    assert group_key("2001:db8:1::1") != group_key("2001:db8:2::1")
    assert group_key("::ffff:198.51.100.7") == "198.51.100.0/24"


@pytest.mark.parametrize("host", [None, "", "not-an-ip"])
def test_group_key_unparseable_falls_into_one_shared_bucket(host):
    assert group_key(host) == "unknown"


def test_rotating_sources_in_one_network_hit_the_group_ceiling():
    # Each source has its own full per-client bucket, but the network shares 5.
    limiter = BeginRateLimiter(
        per_minute=30, burst=3, group_per_minute=60, group_burst=5, clock=FakeClock()
    )
    results = [limiter.check(f"198.51.100.{i}") for i in range(1, 21)]
    assert results[:5] == [None] * 5
    assert all(r is not None for r in results[5:])
    # 60/min = 1 token per second.
    assert results[5] == 1
    # Another network is unaffected.
    assert limiter.check("198.51.101.1") is None


def test_rotating_ipv6_sources_within_a_48_hit_the_group_ceiling():
    limiter = BeginRateLimiter(
        per_minute=30, burst=3, group_per_minute=60, group_burst=4, clock=FakeClock()
    )
    results = [limiter.check(f"2001:db8:7:{i:x}::1") for i in range(1, 11)]  # 10 /64s of a /48
    assert results[:4] == [None] * 4
    assert all(r is not None for r in results[4:])
    assert limiter.check("2001:db8:8::1") is None


def test_group_bucket_refills_over_time():
    clock = FakeClock()
    limiter = BeginRateLimiter(
        per_minute=30, burst=3, group_per_minute=60, group_burst=2, clock=clock
    )
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.2") is None
    assert limiter.check("198.51.100.3") is not None
    clock.now += 1.0
    assert limiter.check("198.51.100.4") is None


def test_client_rejected_by_its_own_bucket_does_not_drain_the_group():
    limiter = BeginRateLimiter(
        per_minute=30, burst=1, group_per_minute=60, group_burst=3, clock=FakeClock()
    )
    assert limiter.check("198.51.100.1") is None  # spends 1 of the network's 3
    for _ in range(50):
        assert limiter.check("198.51.100.1") is not None  # rejected by its own bucket
    # 2 network tokens remain for other neighbors.
    assert limiter.check("198.51.100.2") is None
    assert limiter.check("198.51.100.3") is None
    assert limiter.check("198.51.100.4") is not None


def test_group_rejection_does_not_spend_the_client_token():
    limiter = BeginRateLimiter(
        per_minute=6, burst=2, group_per_minute=6, group_burst=1, clock=FakeClock()
    )
    host = "198.51.100.1"
    assert limiter.check(host) is None  # client 1 of 2, network 0 of 1
    # 6/min = 1 token every 10 s: the wait is the network's, not a fixed 1.
    assert limiter.check(host) == 10
    assert limiter._clients.buckets[client_key(host)].tokens == 1.0


def test_retry_after_is_the_longest_wait_among_the_levels_lacking_a_token():
    clock = FakeClock()
    limiter = BeginRateLimiter(
        per_minute=30, burst=1, group_per_minute=6, group_burst=1, clock=clock
    )
    assert limiter.check("198.51.100.1") is None
    # Client: 2 s until the next token; network: 10 s. The larger wins.
    assert limiter.check("198.51.100.1") == 10
    clock.now += 2.0
    # The client already has a token; the network does not: the network's wait, 8 s.
    assert limiter.check("198.51.100.1") == 8
    assert limiter._clients.buckets[client_key("198.51.100.1")].tokens == 1.0


def test_client_rejection_does_not_spend_the_group_token():
    limiter = BeginRateLimiter(
        per_minute=6, burst=1, group_per_minute=60, group_burst=2, clock=FakeClock()
    )
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.1") == 10
    assert limiter._groups.buckets[group_key("198.51.100.1")].tokens == 1.0


def test_group_level_can_be_disabled_independently():
    limiter = BeginRateLimiter(per_minute=30, burst=3, group_per_minute=0, clock=FakeClock())
    assert all(limiter.check(f"198.51.100.{i}") is None for i in range(1, 100))


def test_group_only_limiter_works_with_the_client_level_disabled():
    limiter = BeginRateLimiter(
        per_minute=0, burst=1, group_per_minute=60, group_burst=2, clock=FakeClock()
    )
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.1") is None
    assert limiter.check("198.51.100.1") is not None


def test_group_buckets_are_bounded():
    limiter = BeginRateLimiter(
        per_minute=30, burst=1, group_per_minute=60, group_burst=1, clock=FakeClock(), max_clients=3
    )
    for i in range(10):
        limiter.check(f"198.51.{i}.1")
    assert len(limiter._groups.buckets) == 3


@pytest.mark.asyncio
async def test_bounded_warning_flushes_the_trailing_count_when_the_window_closes(caplog):
    warn = BoundedWarning(logging.getLogger("t.trailing"), "rejected %d", interval=0.05)
    with caplog.at_level(logging.WARNING, logger="t.trailing"):
        for _ in range(40):
            warn.hit()
        await asyncio.sleep(0.2)
    assert [r.getMessage() for r in caplog.records] == ["rejected 1", "rejected 39"]


@pytest.mark.asyncio
async def test_bounded_warning_trailing_flush_reports_the_accumulated_count(caplog):
    clock = FakeClock()
    warn = BoundedWarning(
        logging.getLogger("t.trailing_fake"), "rejected %d", interval=0.01, clock=clock
    )
    with caplog.at_level(logging.WARNING, logger="t.trailing_fake"):
        # Burst within the window (the fake clock does not advance): only the
        # first is emitted immediately, the rest accumulate (2 + 1 + 1 = 4).
        warn.hit()
        warn.hit(2)
        warn.hit()
        warn.hit()
        assert [r.getMessage() for r in caplog.records] == ["rejected 1"]
        # When the `call_later` fires, a single line with the accumulated count is emitted.
        await asyncio.sleep(0.1)
        assert [r.getMessage() for r in caplog.records] == ["rejected 1", "rejected 4"]
        # With no new events there are no more lines.
        await asyncio.sleep(0.05)
        assert len(caplog.records) == 2
        # An event in the next window accumulates and flushes again.
        warn.hit()
        assert len(caplog.records) == 2
        await asyncio.sleep(0.1)
    assert [r.getMessage() for r in caplog.records] == [
        "rejected 1",
        "rejected 4",
        "rejected 1",
    ]


@pytest.mark.asyncio
async def test_bounded_warning_direct_emit_cancels_the_pending_timer(caplog):
    # A direct emission (window already expired) must not leave the deferred
    # flush timer alive: it would fire an early line inside the new window and
    # orphan the next timer's handle.
    clock = FakeClock()
    warn = BoundedWarning(
        logging.getLogger("t.stale_timer"), "rejected %d", interval=0.05, clock=clock
    )
    with caplog.at_level(logging.WARNING, logger="t.stale_timer"):
        warn.hit()
        warn.hit()  # accumulates and schedules timer A
        stale = warn._flush_handle
        assert stale is not None
        clock.now += 1  # the window expires before A fires
        warn.hit()  # direct emission: must cancel A
        assert stale.cancelled()
        warn.hit()  # new window: schedules timer B
        assert warn._flush_handle is not None and warn._flush_handle is not stale
        await asyncio.sleep(0.2)
    assert [r.getMessage() for r in caplog.records] == [
        "rejected 1",
        "rejected 2",
        "rejected 1",
    ]


def test_bounded_warning_emits_one_line_per_window_with_the_count(caplog):
    clock = FakeClock()
    warn = BoundedWarning(logging.getLogger("t.bounded"), "rejected %d", interval=60, clock=clock)
    with caplog.at_level(logging.WARNING, logger="t.bounded"):
        for _ in range(500):
            warn.hit()
        assert [r.getMessage() for r in caplog.records] == ["rejected 1"]
        clock.now += 61
        warn.hit()
    assert [r.getMessage() for r in caplog.records] == ["rejected 1", "rejected 500"]


def test_bounded_warning_prefixes_fixed_args_before_the_count(caplog):
    clock = FakeClock()
    warn = BoundedWarning(
        logging.getLogger("t.args"),
        "rejected for %s (%s): %d",
        interval=60,
        clock=clock,
        args=("u-1", "p-1"),
    )
    with caplog.at_level(logging.WARNING, logger="t.args"):
        warn.hit()
    assert [r.getMessage() for r in caplog.records] == ["rejected for u-1 (p-1): 1"]


# --- KeyedBoundedWarning -----------------------------------------------------


def _keyed(clock, *, max_keys=1024, interval=60.0):
    return KeyedBoundedWarning(
        logging.getLogger("t.keyed"),
        "rejected %s: %d",
        interval=interval,
        max_keys=max_keys,
        clock=clock,
    )


def test_keyed_bounded_warning_one_line_per_key_per_window_with_suppressed_count(caplog):
    clock = FakeClock()
    warn = _keyed(clock)
    with caplog.at_level(logging.WARNING, logger="t.keyed"):
        for _ in range(500):
            warn.hit("a", "a")
        for _ in range(7):
            warn.hit("b", "b")
        # One line per key within the window, whatever the volume.
        assert [r.getMessage() for r in caplog.records] == ["rejected a: 1", "rejected b: 1"]
        clock.now += 61
        warn.hit("a", "a")
        # The suppressed count (499 + this event) goes out in the key's next line.
        assert [r.getMessage() for r in caplog.records][-1] == "rejected a: 500"
        warn.hit("b", "b")
    assert [r.getMessage() for r in caplog.records][-1] == "rejected b: 7"
    assert len(caplog.records) == 4


def test_keyed_bounded_warning_lru_bound_holds_and_evicted_count_is_not_lost(caplog):
    clock = FakeClock()
    warn = _keyed(clock, max_keys=3)
    with caplog.at_level(logging.WARNING, logger="t.keyed"):
        warn.hit("k0", "k0")
        warn.hit("k0", "k0")  # k0 accumulates 1 suppressed
        for i in range(1, 50):
            warn.hit(f"k{i}", f"k{i}")
            assert len(warn._entries) <= 3
    messages = [r.getMessage() for r in caplog.records]
    # The key cap does not lose the count silently: evicting k0 emits what was suppressed.
    assert "rejected k0: 1" in messages
    assert messages.count("rejected k0: 1") == 2  # first line + flush on eviction
    assert len(warn._entries) == 3
    assert list(warn._entries) == ["k47", "k48", "k49"]


def test_keyed_bounded_warning_lru_keeps_recently_used_key(caplog):
    clock = FakeClock()
    warn = _keyed(clock, max_keys=2)
    with caplog.at_level(logging.WARNING, logger="t.keyed"):
        warn.hit("a", "a")
        warn.hit("b", "b")
        warn.hit("a", "a")  # `a` becomes the most recent
        warn.hit("c", "c")  # evicts `b`, not `a`
    assert list(warn._entries) == ["a", "c"]


def test_keyed_bounded_warning_reset_forgets_every_key(caplog):
    clock = FakeClock()
    warn = _keyed(clock)
    with caplog.at_level(logging.WARNING, logger="t.keyed"):
        warn.hit("a", "a")
        warn.hit("a", "a")
        warn.reset()
        warn.hit("a", "a")
    assert [r.getMessage() for r in caplog.records] == ["rejected a: 1", "rejected a: 1"]
    assert len(warn._entries) == 1


# --- HTTP ---------------------------------------------------------------------


@contextlib.contextmanager
def _controlled(lim: BeginRateLimiter, clock: FakeClock):
    """Small limits and a controlled clock; restores the AuthConfig values the
    limiter had, without copying them as literals."""
    previous = (lim.per_minute, lim.burst, lim._clock, lim._rejections._clock)
    previous_group = (lim.group_per_minute, lim.group_burst)
    lim._clock = clock
    # The rejections WARNING uses the same clock: its 60 s window does not depend
    # on how long the test actually takes.
    lim._rejections._clock = clock
    lim.configure(30, 3)
    lim.reset()
    lim.test_clock = clock
    try:
        yield lim
    finally:
        per_minute, burst, lim._clock, lim._rejections._clock = previous
        lim.configure(per_minute, burst, *previous_group)
        lim.reset()


@pytest.fixture
def limiter(app_client):
    """Real `begin` limiter (and, with the same clock, the `complete` one)."""
    from orcaster_server.main import _auth

    clock = FakeClock()
    with (
        _controlled(_auth.begin_limiter, clock) as begin,
        _controlled(_auth.complete_limiter, clock),
    ):
        yield begin


@pytest.fixture
def complete_limiter(limiter):
    from orcaster_server.main import _auth

    return _auth.complete_limiter


def _client(peer: str, *, trusted: str | None = None, headers: dict | None = None):
    from orcaster_server.main import app

    asgi = ProxyHeadersMiddleware(app, trusted_hosts=trusted) if trusted else app
    return AsyncClient(
        transport=ASGITransport(app=asgi, client=(peer, 50000)),
        base_url="http://test",
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_login_begin_over_limit_is_429_with_retry_after(limiter, db_session, caplog):
    async with _client("198.51.100.1") as c:
        codes = [(await c.post("/auth/login/begin")).status_code for _ in range(3)]
        assert codes == [200, 200, 200]
        with caplog.at_level(logging.WARNING):
            res = await c.post("/auth/login/begin")
    assert res.status_code == 429
    assert res.headers["retry-after"] == "2"
    assert res.json() == {"ok": False, "reason": "rate_limited", "retry_after": 2}
    # The 429 emits no challenge cookie and does not touch the table.
    assert "webauthn_challenge" not in res.headers.get("set-cookie", "")
    await db_session.rollback()
    n = (await db_session.execute(text("SELECT count(*) FROM webauthn_challenges"))).scalar_one()
    assert n == 3
    warnings = [r for r in caplog.records if "rate limit" in r.getMessage()]
    assert len(warnings) == 1
    assert "198.51.100.1" not in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_rejection_warning_is_bounded_under_a_flood(limiter, caplog):
    async with _client("198.51.100.1") as c:
        with caplog.at_level(logging.WARNING):
            for _ in range(50):
                await c.post("/auth/login/begin")
    # 47 rejections with the clock stopped: the first logs and the other 46 fall
    # in the same 60 s window, so exactly one line is emitted.
    msgs = [r.getMessage() for r in caplog.records if "rate limit" in r.getMessage()]
    assert len(msgs) == 1
    assert msgs[0].startswith("auth: 1 anonymous passkey request(s) rejected")


@pytest.mark.asyncio
async def test_limit_is_per_client(limiter):
    async with _client("198.51.100.1") as a, _client("198.51.100.2") as b:
        for _ in range(3):
            assert (await a.post("/auth/login/begin")).status_code == 200
        assert (await a.post("/auth/login/begin")).status_code == 429
        assert (await b.post("/auth/login/begin")).status_code == 200


@pytest.mark.asyncio
async def test_login_begin_recovers_after_retry_after(limiter):
    async with _client("198.51.100.1") as c:
        for _ in range(3):
            await c.post("/auth/login/begin")
        res = await c.post("/auth/login/begin")
        assert res.status_code == 429
        limiter.test_clock.now += int(res.headers["retry-after"])
        assert (await c.post("/auth/login/begin")).status_code == 200


@pytest.mark.asyncio
async def test_register_begin_shares_the_limit(limiter):
    # A nonexistent token answers 410 only if it passed the limit.
    async with _client("198.51.100.1") as c:
        for _ in range(3):
            assert (await c.post("/auth/login/begin")).status_code == 200
        res = await c.post("/auth/accept/does-not-exist/register/begin")
    assert res.status_code == 429
    assert res.headers["retry-after"]


@pytest.mark.asyncio
async def test_x_forwarded_for_from_untrusted_peer_is_ignored(limiter):
    # Peer 198.51.100.1 is not in `trusted_hosts`: rotating the header gives no
    # new bucket, and pointing it at another IP does not spend that IP's bucket.
    async with _client("198.51.100.1", trusted="127.0.0.1") as c:
        for i in range(3):
            res = await c.post("/auth/login/begin", headers={"X-Forwarded-For": f"203.0.113.{i}"})
            assert res.status_code == 200
        res = await c.post("/auth/login/begin", headers={"X-Forwarded-For": "203.0.113.200"})
        assert res.status_code == 429
    async with _client("203.0.113.1", trusted="127.0.0.1") as victim:
        assert (await victim.post("/auth/login/begin")).status_code == 200


@pytest.mark.asyncio
async def test_x_forwarded_for_from_trusted_proxy_keys_the_real_client(limiter):
    # Behind Caddy (trusted peer 127.0.0.1) the key is the real client IP, not the
    # proxy's: two real clients do not share a bucket.
    async with _client("127.0.0.1", trusted="127.0.0.1") as proxy:
        h1 = {"X-Forwarded-For": "203.0.113.1"}
        h2 = {"X-Forwarded-For": "203.0.113.2"}
        for _ in range(3):
            assert (await proxy.post("/auth/login/begin", headers=h1)).status_code == 200
        assert (await proxy.post("/auth/login/begin", headers=h1)).status_code == 429
        assert (await proxy.post("/auth/login/begin", headers=h2)).status_code == 200


@pytest.mark.asyncio
async def test_flood_does_not_evict_a_victims_pending_challenge(limiter, db_session):
    from orcaster_server.main import _auth

    _, victim_cid = await begin_authentication(db_session, _auth.config)
    await db_session.commit()
    async with _client("198.51.100.66") as attacker:
        ok = 0
        for _ in range(1500):
            if (await attacker.post("/auth/login/begin")).status_code == 200:
                ok += 1
    # With the clock stopped no token refills: the attacker gets exactly the
    # burst, far from the 1000 cap that would evict the user.
    assert ok == limiter.burst
    assert await _take_challenge(db_session, victim_cid, "authentication") is not None


@pytest.mark.asyncio
async def test_rotating_sources_in_one_24_cannot_evict_a_victims_challenge(limiter, db_session):
    from orcaster_server.main import _auth

    limiter.configure(30, 3, 60, 5)
    limiter.reset()
    _, victim_cid = await begin_authentication(db_session, _auth.config)
    await db_session.commit()
    ok = 0
    for i in range(1, 201):
        async with _client(f"198.51.100.{i}") as c:
            if (await c.post("/auth/login/begin")).status_code == 200:
                ok += 1
    # With the clock stopped, the whole network gets exactly its coarse bucket.
    assert ok == 5
    assert await _take_challenge(db_session, victim_cid, "authentication") is not None
    async with _client("198.51.101.1") as other_net:
        assert (await other_net.post("/auth/login/begin")).status_code == 200


@pytest.mark.asyncio
async def test_group_429_carries_retry_after(limiter):
    limiter.configure(30, 3, 60, 1)
    limiter.reset()
    async with _client("198.51.100.1") as a, _client("198.51.100.2") as b:
        assert (await a.post("/auth/login/begin")).status_code == 200
        res = await b.post("/auth/login/begin")
    assert res.status_code == 429
    assert res.headers["retry-after"] == "1"
    assert res.json()["reason"] == "rate_limited"


# --- anonymous complete -------------------------------------------------------


@pytest.mark.asyncio
async def test_login_complete_over_limit_is_429_before_touching_the_challenge(
    complete_limiter, db_session
):
    from orcaster_server.main import _auth

    _, cid = await begin_authentication(db_session, _auth.config)
    await db_session.commit()
    async with _client("198.51.100.1") as c:
        for _ in range(3):
            assert (await c.post("/auth/login/complete", content=b"{}")).status_code == 400
        res = await c.post(
            "/auth/login/complete", content=b"{}", headers={"Cookie": f"webauthn_challenge={cid}"}
        )
    assert res.status_code == 429
    assert res.headers["retry-after"]
    # The 429 did not consume the challenge.
    assert await _take_challenge(db_session, cid, "authentication") is not None


@pytest.mark.asyncio
async def test_login_complete_within_limit_keeps_its_400_and_401_contract(limiter):
    async with _client("198.51.100.1") as c:
        assert (await c.post("/auth/login/complete", content=b"{}")).status_code == 400
        res = await c.post(
            "/auth/login/complete",
            content=b"{}",
            headers={"Cookie": "webauthn_challenge=00000000-0000-0000-0000-000000000000"},
        )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_register_complete_over_limit_is_429_without_inserting_users(limiter, db_session):
    async with _client("198.51.100.1") as c:
        codes = [
            (await c.post("/auth/accept/nope/register/complete", content=b"{}")).status_code
            for _ in range(3)
        ]
        assert codes == [400, 400, 400]  # cookie missing: passes the limit, the usual 400
        res = await c.post("/auth/accept/nope/register/complete", content=b"{}")
    assert res.status_code == 429
    assert res.headers["retry-after"]
    n = (await db_session.execute(text("SELECT count(*) FROM users"))).scalar_one()
    assert n == 0


@pytest.mark.asyncio
async def test_complete_and_begin_have_separate_buckets(limiter):
    # Same settings, different buckets: exhausting `complete` does not reject a
    # `begin`, nor the other way around.
    async with _client("198.51.100.1") as c:
        for _ in range(3):
            assert (await c.post("/auth/login/complete", content=b"{}")).status_code == 400
        assert (await c.post("/auth/login/complete", content=b"{}")).status_code == 429
        for _ in range(3):
            assert (await c.post("/auth/login/begin")).status_code == 200
        assert (await c.post("/auth/login/begin")).status_code == 429


@pytest.mark.asyncio
async def test_complete_limiter_uses_the_begin_settings():
    from orcaster_server.main import _auth

    b, c = _auth.begin_limiter, _auth.complete_limiter
    assert b is not c
    assert (c.per_minute, c.burst, c.group_per_minute, c.group_burst) == (
        b.per_minute,
        b.burst,
        b.group_per_minute,
        b.group_burst,
    )


@pytest.mark.asyncio
async def test_concurrent_logins_behind_one_ip_all_complete_at_default_settings(app_client):
    # Six simultaneous logins behind the same NAT, with the defaults: if all six
    # `begin` pass, all six `complete` pass (an admitted `begin` always has its
    # `complete` token).
    async with _client("198.51.100.1") as c:
        begins = await asyncio.gather(*(c.post("/auth/login/begin") for _ in range(6)))
        assert [r.status_code for r in begins] == [200] * 6
        completes = await asyncio.gather(
            *(
                c.post(
                    "/auth/login/complete",
                    content=b"{}",
                    headers={"Cookie": f"webauthn_challenge={r.cookies['webauthn_challenge']}"},
                )
                for r in begins
            )
        )
    assert [r.status_code for r in completes] == [401] * 6


@pytest.mark.asyncio
async def test_complete_flood_alone_is_still_429(complete_limiter):
    async with _client("198.51.100.1") as c:
        codes = [
            (await c.post("/auth/login/complete", content=b"{}")).status_code for _ in range(5)
        ]
    assert codes == [400, 400, 400, 429, 429]


# --- invalid body in `complete` ------------------------------------------------


async def _registration_cookie(db_session) -> str:
    from orcaster_server.main import _auth

    _, cid = await passkey_service.begin_registration(
        db_session, _auth.config, user_id="00000000-0000-0000-0000-000000000001", user_name="Ana"
    )
    await db_session.commit()
    return base64.urlsafe_b64encode(json.dumps({"cid": cid, "name": "Ana"}).encode()).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{}", b"not json", b"\xff\xfe\x00garbage", b"[]"])
async def test_register_complete_with_an_invalid_attestation_is_the_generic_400(
    limiter, db_session, body
):
    cookie = await _registration_cookie(db_session)
    async with _client("198.51.100.1") as c:
        res = await c.post(
            "/auth/accept/nope/register/complete",
            content=body,
            headers={"Cookie": f"webauthn_challenge={cookie}"},
        )
    assert res.status_code == 400
    assert res.json() == {"ok": False}
    await db_session.rollback()
    n = (await db_session.execute(text("SELECT count(*) FROM users"))).scalar_one()
    assert n == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{}", b"\xff\xfe\x00garbage"])
async def test_login_complete_with_an_invalid_assertion_is_the_generic_401(
    limiter, db_session, body
):
    from orcaster_server.main import _auth

    _, cid = await begin_authentication(db_session, _auth.config)
    await db_session.commit()
    async with _client("198.51.100.1") as c:
        res = await c.post(
            "/auth/login/complete",
            content=body,
            headers={"Cookie": f"webauthn_challenge={cid}"},
        )
    assert res.status_code == 401
    assert res.json() == {"ok": False}


# --- evictions by the cap ----------------------------------------------------


@pytest.mark.asyncio
async def test_cap_evictions_log_a_bounded_warning_with_the_count(db_session, monkeypatch, caplog):
    monkeypatch.setattr(
        passkey_service,
        "_evictions_warning",
        BoundedWarning(passkey_service.logger, passkey_service._evictions_warning._template),
    )
    cfg = AuthConfig(
        rp_id="localhost", rp_name="T", origin="https://localhost", challenge_max_pending=2
    )
    with caplog.at_level(logging.WARNING, logger=passkey_service.logger.name):
        for _ in range(6):
            await begin_authentication(db_session, cfg)
            await db_session.commit()
    msgs = [r.getMessage() for r in caplog.records if "evicted" in r.getMessage()]
    # First eviction (3rd begin) logs; the rest fall in the 60 s window.
    assert len(msgs) == 1
    assert msgs[0].startswith("passkey: evicted 1 pending challenge")


@pytest.mark.asyncio
async def test_no_eviction_log_when_only_expired_rows_are_purged(db_session, monkeypatch, caplog):
    monkeypatch.setattr(
        passkey_service,
        "_evictions_warning",
        BoundedWarning(passkey_service.logger, passkey_service._evictions_warning._template),
    )
    await begin_authentication(db_session, AuthConfig("localhost", "T", "https://localhost"))
    await db_session.execute(
        text("UPDATE webauthn_challenges SET expires_at = now() - interval '1 second'")
    )
    await db_session.commit()
    with caplog.at_level(logging.WARNING, logger=passkey_service.logger.name):
        await begin_authentication(db_session, AuthConfig("localhost", "T", "https://localhost"))
    assert not [r for r in caplog.records if "evicted" in r.getMessage()]


def test_auth_config_rejects_invalid_rate_settings():
    for kwargs in (
        {"begin_rate_per_minute": -1},
        {"begin_rate_burst": 0},
        {"begin_group_rate_per_minute": -1},
        {"begin_group_burst": 0},
    ):
        with pytest.raises(ValueError):
            AuthConfig(rp_id="localhost", rp_name="T", origin="https://localhost", **kwargs)
