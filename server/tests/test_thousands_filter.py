"""Jinja `thousands` filter: thousands separator for the artifact indicator count."""

from __future__ import annotations

from orcaster_server.api.admin_ui import _thousands


def test_thousands_formats_with_separator() -> None:
    assert _thousands(4762) == "4,762"
    assert _thousands(6785000) == "6,785,000"


def test_thousands_small_numbers_unchanged() -> None:
    assert _thousands(0) == "0"
    assert _thousands(7) == "7"


def test_thousands_guards_non_int() -> None:
    # COUNT is never NULL, but the filter must not blow up if it receives None.
    assert _thousands(None) == "0"
