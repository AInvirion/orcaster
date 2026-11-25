"""Tests for the Jinja `ts()` macro: unified timestamp markup."""

from datetime import UTC, datetime

from orcaster_server.api.admin_ui import templates


def _render(value, fmt="datetime", cls=""):
    """Render the `ts` macro in isolation using the real env (with its filters)."""
    macro = templates.env.get_template("_macros.html").module.ts
    return str(macro(value, fmt, cls))


DT = datetime(2026, 6, 21, 14, 35, 42, tzinfo=UTC)


def test_emits_time_with_js_ts_class():
    html = _render(DT, "datetime")
    assert "<time" in html
    assert 'class="js-ts"' in html


def test_datetime_attr_and_title_are_iso_utc():
    html = _render(DT, "datetime")
    assert 'datetime="2026-06-21T14:35:42+00:00"' in html
    assert 'title="UTC · 2026-06-21T14:35:42+00:00"' in html


def test_format_is_exposed_as_data_attr():
    assert 'data-ts-format="compact"' in _render(DT, "compact")
    assert 'data-ts-format="relative"' in _render(DT, "relative")
    assert 'data-ts-format="datetime"' in _render(DT, "datetime")


def test_datetime_fallback_uses_dt_human():
    # dt_human => "%Y-%m-%d %H:%M:%S" in UTC
    assert ">2026-06-21 14:35:42</time>" in _render(DT, "datetime")


def test_compact_fallback_uses_dt_compact():
    # dt_compact => "Mmm DD HH:MM"
    assert ">Jun 21 14:35</time>" in _render(DT, "compact")


def test_extra_class_is_appended():
    assert 'class="js-ts runtime-start"' in _render(DT, "compact", "runtime-start")


def test_none_renders_em_dash_span():
    html = _render(None, "datetime")
    assert html.strip() == '<span class="muted">—</span>'
