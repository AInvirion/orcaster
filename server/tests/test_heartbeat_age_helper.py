"""Shared heartbeat-age helper (`_heartbeat_age_seconds`).

Single source of the calculation used by the worker detail (three-band
liveness dot) and the deck (`paused (stale)` badge suffix). Pure unit test:
the helper receives `now` as a parameter, so it touches neither the DB nor
the clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orcaster_server.api.admin_ui import _heartbeat_age_seconds

_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def test_heartbeat_age_is_none_without_heartbeat() -> None:
    assert _heartbeat_age_seconds(None, _NOW) is None


def test_heartbeat_age_counts_seconds_since_the_past() -> None:
    assert _heartbeat_age_seconds(_NOW - timedelta(seconds=185), _NOW) == 185


def test_heartbeat_age_clamps_a_future_heartbeat_to_zero() -> None:
    """Heartbeat dated in the future (worker clock ahead, or DB clock drift
    relative to the process): the age is clamped to 0, never negative.
    This is the reason for the `max`."""
    assert _heartbeat_age_seconds(_NOW + timedelta(seconds=45), _NOW) == 0


def test_heartbeat_age_normalizes_a_naive_timestamp() -> None:
    """A timestamp without tzinfo is interpreted as UTC. Without that
    normalization the subtraction would raise TypeError."""
    naive = (_NOW - timedelta(minutes=3)).replace(tzinfo=None)
    assert _heartbeat_age_seconds(naive, _NOW) == 180
