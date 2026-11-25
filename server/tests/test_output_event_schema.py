"""OutputEventIn accepts the third stream 'command'."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orcaster_server.schemas.tasks import OutputEventIn


def test_output_event_accepts_command_stream() -> None:
    ev = OutputEventIn(seq=1, ts=datetime.now(UTC), stream="command", text="$ ls\n")
    assert ev.stream == "command"


def test_output_event_rejects_unknown_stream() -> None:
    with pytest.raises(ValidationError):
        OutputEventIn(seq=1, ts=datetime.now(UTC), stream="weird", text="x")
