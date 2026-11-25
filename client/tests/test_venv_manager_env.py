from pathlib import Path

from orcaster_worker.venv_manager import _build_env


def _src(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("ORCH_API_KEY", "orch_x")
    monkeypatch.setenv("HOME", "/home/orcaster")


def test_build_env_legacy_none_keeps_secrets(monkeypatch):
    _src(monkeypatch)
    env = _build_env(Path("/tmp/venv"), None)
    assert env["GITHUB_TOKEN"] == "ghp_x"
    assert env["ORCH_API_KEY"] == "orch_x"
    assert env["VIRTUAL_ENV"] == "/tmp/venv"
    assert env["PATH"].startswith("/tmp/venv/bin")


def test_build_env_strict_scrubs_secrets(monkeypatch):
    _src(monkeypatch)
    env = _build_env(Path("/tmp/venv"), ["GITHUB_TOKEN"])
    assert env["GITHUB_TOKEN"] == "ghp_x"  # declared
    assert "ORCH_API_KEY" not in env  # denylist
    assert env["VIRTUAL_ENV"] == "/tmp/venv"
    assert env["PATH"].startswith("/tmp/venv/bin")


def test_build_env_strict_empty_has_no_secrets(monkeypatch):
    _src(monkeypatch)
    env = _build_env(Path("/tmp/venv"), [])
    assert "GITHUB_TOKEN" not in env
    assert "ORCH_API_KEY" not in env
    assert env["HOME"] == "/home/orcaster"
