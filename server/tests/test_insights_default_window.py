"""Unit tests for the default time window of the insights.

Covers the pure helper `_default_window` (v1.py) and `start_of_utc_day`
(failures.py): with neither `since` nor `until`, the default window is the
current day, with the lower bound clamped to UTC midnight and the upper
bound OPEN in the SQL but echoed as "now" in `filters.until`;
any explicit bound is respected without injecting anything on that edge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from orcaster_server.api.v1 import _default_window
from orcaster_server.failures import start_of_utc_day


def test_start_of_utc_day_truncates_to_midnight() -> None:
    now = datetime(2026, 7, 23, 18, 6, 42, 123456, tzinfo=UTC)
    assert start_of_utc_day(now) == datetime(2026, 7, 23, 0, 0, 0, tzinfo=UTC)


def test_start_of_utc_day_naive_is_anchored_to_utc() -> None:
    now = datetime(2026, 7, 23, 18, 6, 42)  # naive
    assert start_of_utc_day(now) == datetime(2026, 7, 23, 0, 0, 0, tzinfo=UTC)


def test_start_of_utc_day_converts_other_tz_before_truncating() -> None:
    # 2026-07-23 01:30 in UTC-3 is 04:30 UTC of the SAME day → midnight 07-23.
    now = datetime(2026, 7, 23, 1, 30, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert start_of_utc_day(now) == datetime(2026, 7, 23, 0, 0, 0, tzinfo=UTC)
    # 2026-07-23 22:00 in UTC-3 is 01:00 UTC of the NEXT day → midnight 07-24.
    late = datetime(2026, 7, 23, 22, 0, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert start_of_utc_day(late) == datetime(2026, 7, 24, 0, 0, 0, tzinfo=UTC)


_NOW = datetime(2026, 7, 23, 18, 6, 42, tzinfo=UTC)
_MIDNIGHT = datetime(2026, 7, 23, 0, 0, 0, tzinfo=UTC)


def test_default_window_injects_both_bounds_when_unbounded() -> None:
    # Without since or until: since → today's UTC midnight, echoed until → now,
    # but the SQL upper bound is open.
    assert _default_window(None, None, now=_NOW) == (_MIDNIGHT, _NOW, None)


def test_default_window_only_since_explicit_injects_until_as_now() -> None:
    explicit = datetime(2020, 1, 1, tzinfo=UTC)
    assert _default_window(explicit, None, now=_NOW) == (explicit, _NOW, None)


def test_default_window_only_until_explicit_injects_since_as_midnight() -> None:
    # "Only until" no longer leaves since unbounded.
    until = datetime(2026, 7, 23, tzinfo=UTC)
    assert _default_window(None, until, now=_NOW) == (_MIDNIGHT, until, until)


def test_default_window_respects_both_explicit_bounds() -> None:
    since = datetime(2026, 7, 1, tzinfo=UTC)
    until = datetime(2026, 7, 23, tzinfo=UTC)
    assert _default_window(since, until, now=_NOW) == (since, until, until)


def test_default_window_sql_until_is_open_when_until_is_default() -> None:
    """Without a client `until`, the SQL gets no upper bound.

    The effective instant is still echoed in `filters.until` (informative),
    but `sql_until is None` is what eliminates app↔DB clock skew by
    construction: `completed_at` is stamped by the Postgres clock and the
    default was computed by the app clock, so a DB running ahead left
    recently closed tasks outside the window.
    """
    window = _default_window(None, None, now=_NOW)
    assert window.sql_until is None
    assert window.until == _NOW
    assert window.since == _MIDNIGHT


def test_default_window_sql_until_mirrors_explicit_until() -> None:
    """With an explicit `until`, the SQL does receive it: the client's intent wins."""
    until = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
    window = _default_window(None, until, now=_NOW)
    assert window.sql_until == until
    assert window.until == until
