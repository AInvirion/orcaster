"""Startup warning for unsupported legacy `ORCH_TASKORCH_*` variable names.

`Settings` has `extra="ignore"`, so a variable under an unsupported legacy name
would be dropped without a trace. The worker logs one WARNING per such variable,
naming the `ORCH_ORCASTER_*` replacement, and never reads its value.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from orcaster_worker import main as worker_main
from orcaster_worker.config import legacy_env_replacements, warn_legacy_env


def test_replacements_map_each_legacy_name_to_its_current_name() -> None:
    environ = {
        "ORCH_TASKORCH_CACHE_ROOT": "/x",
        "ORCH_TASKORCH_UV_BIN": "uv",
        "ORCH_ORCASTER_CACHE_ROOT": "/y",
        "ORCH_BASE_URL": "http://localhost",
    }
    assert legacy_env_replacements(environ) == [
        ("ORCH_TASKORCH_CACHE_ROOT", "ORCH_ORCASTER_CACHE_ROOT"),
        ("ORCH_TASKORCH_UV_BIN", "ORCH_ORCASTER_UV_BIN"),
    ]


def test_no_legacy_names_no_replacements() -> None:
    assert legacy_env_replacements({"ORCH_ORCASTER_CACHE_ROOT": "/y"}) == []


def test_warning_names_the_replacement_and_never_the_value(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        warn_legacy_env({"ORCH_TASKORCH_CACHE_ROOT": "s3cr3t-value"})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "ORCH_TASKORCH_CACHE_ROOT" in message
    assert "ORCH_ORCASTER_CACHE_ROOT" in message
    assert "unsupported legacy name" in message
    assert "s3cr3t-value" not in message


def test_dotenv_file_entries_are_checked_too(tmp_path, caplog) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ORCH_TASKORCH_UV_BIN=uv\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        warn_legacy_env({}, env_file=env_file)
    assert "ORCH_ORCASTER_UV_BIN" in caplog.text


def test_missing_dotenv_file_is_not_an_error(tmp_path, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        warn_legacy_env({}, env_file=tmp_path / "absent.env")
    assert caplog.records == []


class _StopAfterWarning(Exception):
    pass


def test_run_warns_at_startup_before_anything_else(monkeypatch, caplog) -> None:
    """`run()` emits the warning right after logging is configured, before the
    settings are validated, so it is visible even when startup later fails."""
    monkeypatch.setenv("ORCH_TASKORCH_CACHE_ROOT", "/x")

    def _stop() -> None:
        raise _StopAfterWarning

    monkeypatch.setattr(worker_main, "load_settings", _stop)
    with caplog.at_level(logging.WARNING), pytest.raises(_StopAfterWarning):
        asyncio.run(worker_main.run())
    assert "ORCH_ORCASTER_CACHE_ROOT" in caplog.text
