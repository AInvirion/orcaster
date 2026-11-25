"""_output_event_view: the command echo is rendered without the trailing \n."""

from __future__ import annotations

from datetime import UTC, datetime

from orcaster_server.api.admin_ui import _output_event_view


def test_command_event_strips_trailing_newline() -> None:
    row = {"seq": 1, "stream": "command", "text": "$ ls /tmp\n", "ts": datetime.now(UTC)}
    view = _output_event_view(row)
    assert view["stream"] == "command"
    assert view["text"] == "$ ls /tmp"


def test_stdout_event_preserves_text() -> None:
    row = {"seq": 2, "stream": "stdout", "text": "line\n", "ts": None}
    view = _output_event_view(row)
    assert view["text"] == "line\n"
    assert view["ts_display"] == ""
