"""Tests for `_resolve_isolation`: promotion of absent -> strict.

`ORCH_ENFORCE_ISOLATION` is the rollout lever: with False (an explicit
per-worker `false`), a key absent from the payload remains the legacy
tri-state (``None``, no restriction). With True (the code default)
an absent key is promoted to strict (``[]``) without needing to
re-create each task."""

from orcaster_worker.main import _resolve_isolation


def test_legacy_when_absent_and_flag_off():
    assert _resolve_isolation({"commands": ["x"]}, enforce=False) == (None, None)


def test_absent_promoted_to_strict_when_flag_on():
    assert _resolve_isolation({"commands": ["x"]}, enforce=True) == ([], [])


def test_present_wins_over_flag():
    assert _resolve_isolation(
        {"task_env": ["GITHUB_TOKEN"], "egress_allowlist": ["a.test"]}, enforce=True
    ) == (
        ["GITHUB_TOKEN"],
        ["a.test"],
    )


def test_malformed_present_task_env_promoted_to_strict_when_flag_on():
    # egress_allowlist absent -> also promoted to strict (already existing
    # behavior for absent keys); what matters here is task_env.
    assert _resolve_isolation({"task_env": "x"}, enforce=True) == ([], [])


def test_malformed_present_egress_promoted_to_strict_when_flag_on():
    # task_env absent -> also promoted to strict; what matters here
    # is the malformed, present egress_allowlist.
    assert _resolve_isolation({"egress_allowlist": [1]}, enforce=True) == ([], [])


def test_both_malformed_present_promoted_to_strict_when_flag_on():
    assert _resolve_isolation({"task_env": "x", "egress_allowlist": [1]}, enforce=True) == ([], [])


def test_malformed_task_env_with_wellformed_egress_when_flag_on():
    assert _resolve_isolation({"task_env": "x", "egress_allowlist": ["a.test"]}, enforce=True) == (
        [],
        ["a.test"],
    )


def test_null_task_env_is_legacy_when_flag_off():
    """`"task_env": null` counts as absent: legacy, not strict."""
    assert _resolve_isolation({"task_env": None}, enforce=False) == (None, None)


def test_null_task_env_promoted_to_strict_with_warning_when_flag_on(caplog):
    """With the flag, `null` is treated as a malformed key: `[]` and a warning."""
    with caplog.at_level("WARNING", logger="orcaster.worker"):
        assert _resolve_isolation({"task_env": None}, enforce=True) == ([], [])
    assert "task_env present but malformed" in caplog.text


def test_malformed_present_stays_none_when_flag_off():
    """Regression: enforce=False must not touch the tolerant legacy path."""
    assert _resolve_isolation({"task_env": "x", "egress_allowlist": [1]}, enforce=False) == (
        None,
        None,
    )


def test_malformed_present_logs_warning_naming_key(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="orcaster.worker"):
        _resolve_isolation({"task_env": "x"}, enforce=True)
    assert any("task_env" in record.message for record in caplog.records)


# --- Effective environment of a task without `task_env` ---------------------
#
# Full chain payload -> `_resolve_isolation` -> `build_scrubbed_base_env`
# with the flag as it arrives from the worker's env file. With the flag on
# (what the installer stamps on a new install) a task without
# `task_env` receives neither the enrollment token nor the worker
# credential; with an explicit `false` (what is stamped on an existing host)
# it keeps the legacy behavior.

_WORKER_ENV = {
    "PATH": "/usr/bin",
    "ORCH_ENROLLMENT_TOKEN": "enrol-sentinel",
    "ORCH_API_KEY": "key-sentinel",
    "EXAMPLE_PROTECTED_VAR": "value-sentinel",
}


def _task_env_for(flag: str | None, monkeypatch) -> dict[str, str]:
    from orcaster_worker.config import Settings
    from orcaster_worker.task_env import build_scrubbed_base_env

    if flag is None:
        monkeypatch.delenv("ORCH_ENFORCE_ISOLATION", raising=False)
    else:
        monkeypatch.setenv("ORCH_ENFORCE_ISOLATION", flag)
    settings = Settings(server_url="http://server.test", _env_file=None)  # type: ignore[arg-type]
    declared, _egress = _resolve_isolation({"commands": ["x"]}, enforce=settings.enforce_isolation)
    return build_scrubbed_base_env(declared, source=_WORKER_ENV)


def test_new_install_flag_gives_task_without_task_env_a_clean_env(monkeypatch):
    env = _task_env_for("true", monkeypatch)
    assert "ORCH_ENROLLMENT_TOKEN" not in env
    assert "ORCH_API_KEY" not in env
    assert "EXAMPLE_PROTECTED_VAR" not in env
    assert env["PATH"] == "/usr/bin"


def test_worker_without_the_variable_runs_strict(monkeypatch):
    """Without `ORCH_ENFORCE_ISOLATION` the worker runs strict."""
    env = _task_env_for(None, monkeypatch)
    assert "ORCH_ENROLLMENT_TOKEN" not in env
    assert "ORCH_API_KEY" not in env
    assert "EXAMPLE_PROTECTED_VAR" not in env
    assert env["PATH"] == "/usr/bin"


def test_explicit_false_keeps_legacy_inheritance(monkeypatch):
    env = _task_env_for("false", monkeypatch)
    assert env == _WORKER_ENV
