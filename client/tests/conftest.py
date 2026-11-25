"""Shared fixtures for the client test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_non_dumpable_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that call `run()` must not leave the pytest process non-dumpable on
    Linux: that would remove its read access to `/proc/self/environ` for the
    rest of the suite. The wiring test patches it again with its own double and
    the kernel test runs in a separate subprocess."""
    monkeypatch.setattr("orcaster_worker.main.make_process_non_dumpable", lambda: None)
