"""Tests for `Settings.enforce_isolation` (`ORCH_ENFORCE_ISOLATION`).

Default `True`: a worker without the variable runs strict. An explicit
per-worker `false` keeps the legacy tri-state while the old queue
(tasks without the isolation keys in the payload) has not drained."""

from orcaster_worker.config import Settings

_ENV = "ORCH_ENFORCE_ISOLATION"


def test_default_true_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert Settings(_env_file=None).enforce_isolation is True


def test_true_when_env_true(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "true")
    assert Settings().enforce_isolation is True


def test_false_when_env_false(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "false")
    assert Settings().enforce_isolation is False
