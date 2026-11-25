"""Startup guard for strict isolation.

With `ORCH_ENFORCE_ISOLATION` off, a task without `task_env` inherits the whole
worker environment (legacy mode): whoever can create tasks can read
the enrollment token, the worker credential and any other variable.

The guard:
- logs on every startup the names it checks (empty built-in list ∪
  `ORCH_ISOLATION_REQUIRED_ENV`), names only;
- warns (WARNING) on every worker with strict mode off;
- refuses to start (`sys.exit(1)`) if in addition a variable from the list
  is present and non-empty.

These tests use a neutral name (`EXAMPLE_PROTECTED_VAR`): the core names no
workload.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from orcaster_worker.config import (
    BUILTIN_ISOLATION_REQUIRED_ENV,
    Settings,
    parse_isolation_required_env,
)
from orcaster_worker.main import _ensure_strict_isolation_or_die, run

_PROTECTED = "EXAMPLE_PROTECTED_VAR"
# Distinctive value: if it shows up in any log record, the guard leaked
# the very secret it is trying to protect.
_FAKE_VALUE = "sentinel-must-not-print-SECRET123"
_TOKEN_VALUE = "enrol-sentinel-must-not-print"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Without a stray `.env` in the cwd or dev environment variables that
    # change the result.
    monkeypatch.chdir(tmp_path)
    for name in (
        _PROTECTED,
        "ORCH_ENFORCE_ISOLATION",
        "ORCH_ISOLATION_REQUIRED_ENV",
        "ORCH_ENROLLMENT_TOKEN",
        "ORCH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(
    tmp_path: Path, *, enforce_isolation: bool, required: str | None = _PROTECTED
) -> Settings:
    kwargs: dict[str, object] = {}
    if required is not None:
        kwargs["isolation_required_env"] = required
    return Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        cache_root=tmp_path / "cache",
        egress_allow_file=tmp_path / "egress" / "allow.txt",
        enforce_isolation=enforce_isolation,
        **kwargs,
    )


def _records(caplog: pytest.LogCaptureFixture, level: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelname == level]


# --- List contract --------------------------------------------------------


def test_builtin_list_is_empty() -> None:
    """The core ships no workload or provider names."""
    assert BUILTIN_ISOLATION_REQUIRED_ENV == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("   ", ()),
        ("A", ("A",)),
        ("A,B", ("A", "B")),
        (" A , B ", ("A", "B")),
        ("A B\tC\nD", ("A", "B", "C", "D")),
        ("A,,B,", ("A", "B")),
        ("A,A,B", ("A", "B")),
        ("_x1,Y_2", ("_x1", "Y_2")),
    ],
)
def test_list_parsing(raw: str, expected: tuple[str, ...]) -> None:
    assert parse_isolation_required_env(raw) == expected


@pytest.mark.parametrize("raw", ["*_TOKEN", "A=1", "1ABC", "A-B", "A;B", "Ñ", "A.B"])
def test_malformed_list_fails_closed(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pattern or a `NAME=value` is not interpreted: the worker does not start."""
    with pytest.raises(ValueError):
        parse_isolation_required_env(raw)
    monkeypatch.setenv("ORCH_ISOLATION_REQUIRED_ENV", raw)
    with pytest.raises(ValidationError) as excinfo:
        Settings(server_url="http://server.test")  # type: ignore[arg-type]
    # The error names the offending element so the operator can find it.
    assert "ORCH_ISOLATION_REQUIRED_ENV" in str(excinfo.value)


def test_list_arrives_via_env_and_joins_the_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCH_ISOLATION_REQUIRED_ENV", f"{_PROTECTED}, OTHER_VAR {_PROTECTED}")
    settings = Settings(server_url="http://server.test")  # type: ignore[arg-type]
    assert settings.isolation_required_env == f"{_PROTECTED},OTHER_VAR"
    assert settings.isolation_required_names == BUILTIN_ISOLATION_REQUIRED_ENV + (
        _PROTECTED,
        "OTHER_VAR",
    )


def test_code_default_is_strict(monkeypatch) -> None:
    """Without `ORCH_ENFORCE_ISOLATION`, the worker runs strict."""
    monkeypatch.delenv("ORCH_ENFORCE_ISOLATION", raising=False)
    settings = Settings(server_url="http://server.test", _env_file=None)  # type: ignore[arg-type]
    assert settings.enforce_isolation is True
    assert settings.isolation_required_names == ()


# --- Rejection with a protected variable ----------------------------------


def test_aborts_with_the_variable_and_without_isolation(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv(_PROTECTED, _FAKE_VALUE)
    with caplog.at_level("INFO"), pytest.raises(SystemExit) as excinfo:
        _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=False))
    assert excinfo.value.code == 1
    errors = _records(caplog, "ERROR")
    assert len(errors) == 1
    text = errors[0].getMessage()
    assert _PROTECTED in text
    assert "refusing to start" in text
    # The error carries the full recipe: whoever reads it is facing a worker
    # in a crash loop and needs to know which variable to set, how to recreate it (a
    # restart does not re-read the --env-file), that the queue must be drained first, or
    # the alternative of moving the variable to the central secret.
    assert "ORCH_ENFORCE_ISOLATION=true" in text
    assert "central task secret" in text
    assert "setup.env" in text
    assert "run-fleet.sh" in text
    assert "client/.env" in text
    assert "make up client" in text
    assert "must declare it in task_env" in text
    assert "drain or recreate queued tasks without it" in text


def test_aborts_with_any_name_in_the_list(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("SECOND_VAR", "x")
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        _ensure_strict_isolation_or_die(
            _settings(tmp_path, enforce_isolation=False, required=f"{_PROTECTED},SECOND_VAR")
        )
    text = _records(caplog, "ERROR")[0].getMessage()
    assert "SECOND_VAR" in text
    # Names only what it found, not the whole list.
    assert _PROTECTED not in text


def test_match_by_exact_name(tmp_path: Path, monkeypatch, caplog) -> None:
    """Neither prefix, nor suffix, nor different case: exact name."""
    monkeypatch.setenv(_PROTECTED.lower(), _FAKE_VALUE)
    monkeypatch.setenv(_PROTECTED + "_2", _FAKE_VALUE)
    monkeypatch.setenv("X" + _PROTECTED, _FAKE_VALUE)
    with caplog.at_level("ERROR"):
        _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=False))
    assert _records(caplog, "ERROR") == []


def test_empty_list_does_not_reject(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setenv(_PROTECTED, _FAKE_VALUE)
    with caplog.at_level("INFO"):
        _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=False, required=None))
    assert _records(caplog, "ERROR") == []
    assert any("(none)" in r.getMessage() for r in _records(caplog, "INFO"))


def test_silent_with_the_variable_and_strict_isolation(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setenv(_PROTECTED, _FAKE_VALUE)
    with caplog.at_level("DEBUG"):
        _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=True))
    assert _records(caplog, "WARNING") == []
    assert _records(caplog, "ERROR") == []


@pytest.mark.parametrize("value", ["true", "1", "TRUE", "yes"])
def test_silent_with_the_flag_via_env(tmp_path: Path, monkeypatch, caplog, value: str) -> None:
    """The flag and the list arrive via `ORCH_*` in the environment, not as kwargs:
    it pins the alias and prefix of `Settings`. If that wiring breaks, every
    well-configured worker would end up in a crash loop."""
    monkeypatch.setenv("ORCH_ENFORCE_ISOLATION", value)
    monkeypatch.setenv("ORCH_ISOLATION_REQUIRED_ENV", _PROTECTED)
    monkeypatch.setenv(_PROTECTED, _FAKE_VALUE)
    settings = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        cache_root=tmp_path / "cache",
        egress_allow_file=tmp_path / "egress" / "allow.txt",
    )
    assert settings.enforce_isolation is True
    assert settings.isolation_required_names == (_PROTECTED,)
    with caplog.at_level("DEBUG"):
        _ensure_strict_isolation_or_die(settings)
    assert _records(caplog, "WARNING") == []
    assert _records(caplog, "ERROR") == []


def test_explicit_false_with_the_variable_aborts(tmp_path: Path, monkeypatch, caplog) -> None:
    """An explicit `false` keeps legacy mode (today and after the default
    change), but the guard still applies: with a variable from the list
    present, the worker does not start."""
    monkeypatch.setenv("ORCH_ENFORCE_ISOLATION", "false")
    monkeypatch.setenv("ORCH_ISOLATION_REQUIRED_ENV", _PROTECTED)
    monkeypatch.setenv(_PROTECTED, _FAKE_VALUE)
    settings = Settings(server_url="http://server.test")  # type: ignore[arg-type]
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as excinfo:
        _ensure_strict_isolation_or_die(settings)
    assert excinfo.value.code == 1


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_empty_variable_does_not_reject(tmp_path: Path, monkeypatch, caplog, value: str) -> None:
    """An empty variable (or only whitespace) carries nothing to protect."""
    monkeypatch.setenv(_PROTECTED, value)
    with caplog.at_level("ERROR"):
        _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=False))
    assert _records(caplog, "ERROR") == []


# --- Warning and log of names ----------------------------------------------


def test_warning_on_every_worker_without_isolation(tmp_path: Path, monkeypatch, caplog) -> None:
    """Without a protected variable, the worker starts but warns and names the
    credentials that a task without `task_env` would inherit (no values)."""
    monkeypatch.setenv("ORCH_ENROLLMENT_TOKEN", _TOKEN_VALUE)
    monkeypatch.setenv("ORCH_API_KEY", _FAKE_VALUE)
    with caplog.at_level("INFO"):
        _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=False, required=None))
    warnings = _records(caplog, "WARNING")
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "strict isolation is OFF" in text
    assert "ORCH_ENROLLMENT_TOKEN" in text
    assert "ORCH_API_KEY" in text
    assert "ORCH_ENFORCE_ISOLATION=true" in text
    assert _TOKEN_VALUE not in caplog.text
    assert _FAKE_VALUE not in caplog.text


def test_warning_without_credentials_in_the_environment(tmp_path: Path, caplog) -> None:
    with caplog.at_level("WARNING"):
        _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=False, required=None))
    text = _records(caplog, "WARNING")[0].getMessage()
    assert "enrolment token" in text


def test_log_of_checked_names(tmp_path: Path, caplog) -> None:
    with caplog.at_level("INFO"):
        _ensure_strict_isolation_or_die(
            _settings(tmp_path, enforce_isolation=True, required=f"{_PROTECTED} OTHER_VAR")
        )
    infos = [r.getMessage() for r in _records(caplog, "INFO")]
    assert f"startup: isolation-required variables checked: {_PROTECTED}, OTHER_VAR" in infos


def test_error_never_contains_the_value(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setenv(_PROTECTED, _FAKE_VALUE)
    monkeypatch.setenv("ORCH_ENROLLMENT_TOKEN", _TOKEN_VALUE)
    with caplog.at_level("DEBUG"), pytest.raises(SystemExit):
        _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=False))
    assert caplog.records, "the guard had to log in this scenario"
    for record in caplog.records:
        rendered = record.getMessage()
        assert _FAKE_VALUE not in rendered
        assert _TOKEN_VALUE not in rendered


def test_value_does_not_reach_stderr(tmp_path: Path, monkeypatch, capsys) -> None:
    """With a real handler to stderr (the one `run()` configures), the value does not
    appear in what the journal would receive; the name does."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    monkeypatch.setenv(_PROTECTED, _FAKE_VALUE)
    try:
        with pytest.raises(SystemExit):
            _ensure_strict_isolation_or_die(_settings(tmp_path, enforce_isolation=False))
    finally:
        root.removeHandler(handler)
    err = capsys.readouterr().err
    assert _PROTECTED in err
    assert _FAKE_VALUE not in err


# --- Wiring in run() ----------------------------------------------------------


async def test_worker_startup_runs_the_guard(tmp_path: Path, monkeypatch) -> None:
    """The guard must be wired into `run()`, after configuring the
    logging (and its redaction) and the orphaned-allowlist sweep (the
    sidecar keeps serving even if the worker aborts), and before the pubkey
    fail-fast and any poll."""
    settings = _settings(tmp_path, enforce_isolation=False)
    order: list[str] = []
    called: list[Settings] = []

    def _guard(s: Settings) -> None:
        order.append("guard")
        called.append(s)

    monkeypatch.setattr("orcaster_worker.main._ensure_strict_isolation_or_die", _guard)
    monkeypatch.setattr(
        "orcaster_worker.main.install_log_redaction", lambda: order.append("redaction")
    )
    monkeypatch.setattr(
        "orcaster_worker.main._sweep_orphan_egress_allowlist", lambda *_a: order.append("sweep")
    )
    with (
        patch(
            "orcaster_worker.main._ensure_pubkey_loadable_or_die",
            side_effect=lambda *a, **k: (order.append("pubkey"), sys.exit(1)),
        ),
        pytest.raises(SystemExit),
    ):
        await run(settings)
    assert called == [settings]
    assert order == ["redaction", "sweep", "guard", "pubkey"]


async def test_startup_aborts_before_the_pubkey(tmp_path: Path, monkeypatch) -> None:
    """With the real guard: `run()` exits with code 1 and never reaches the
    pubkey fail-fast, nor, therefore, registering or polling."""
    monkeypatch.setenv(_PROTECTED, _FAKE_VALUE)
    monkeypatch.setattr("orcaster_worker.main.install_log_redaction", lambda: None)
    monkeypatch.setattr("orcaster_worker.main.logging.basicConfig", lambda **_k: None)
    reached: list[str] = []
    with (
        patch(
            "orcaster_worker.main._ensure_pubkey_loadable_or_die",
            side_effect=lambda *a, **k: reached.append("pubkey"),
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        await run(_settings(tmp_path, enforce_isolation=False))
    assert excinfo.value.code == 1
    assert reached == []


async def test_malformed_list_in_run_logs_error_and_exits_1(monkeypatch, caplog) -> None:
    """From the real environment: `run()` does not let a traceback escape, logs an
    ERROR naming the offending element and exits with code 1 before
    registering."""
    monkeypatch.setenv("ORCH_SERVER_URL", "http://server.test")
    monkeypatch.setenv("ORCH_ISOLATION_REQUIRED_ENV", "GOOD_NAME, *_TOKEN")
    monkeypatch.setattr("orcaster_worker.main.logging.basicConfig", lambda **_k: None)
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as excinfo:
        await run()
    assert excinfo.value.code == 1
    errors = _records(caplog, "ERROR")
    assert len(errors) == 1
    assert "*_TOKEN" in errors[0].getMessage()
    assert "refusing to start" in errors[0].getMessage()


async def test_invalid_config_does_not_print_raw_values(monkeypatch, caplog) -> None:
    monkeypatch.setenv("ORCH_SERVER_URL", "http://server.test")
    monkeypatch.setenv("ORCH_ISOLATION_REQUIRED_ENV", f"BAD={_FAKE_VALUE}")
    monkeypatch.setattr("orcaster_worker.main.logging.basicConfig", lambda **_k: None)
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        await run()
    # A `NAME=value` pasted by mistake: the error names `BAD=…` and never the
    # value, nor pydantic's raw `input_value`.
    assert "BAD=…" in caplog.text
    assert _FAKE_VALUE not in caplog.text
    assert "input_value" not in caplog.text
