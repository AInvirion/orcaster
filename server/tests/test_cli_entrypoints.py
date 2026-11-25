"""Server console entry points: environment and argument handling."""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from pathlib import Path

import pytest

from orcaster_server.cli import migrate, server

SERVER_DIR = Path(__file__).resolve().parent.parent

EXPECTED_SCRIPTS = {
    "orcaster-server": "orcaster_server.cli.server:main",
    "orcaster-migrate": "orcaster_server.cli.migrate:main",
    "orcaster-bootstrap-owner": "orcaster_server.cli.bootstrap:main",
    "orcaster-task-secrets-keys": "orcaster_server.cli.task_secrets_keys:main",
    "orcaster-workspace-quota": "orcaster_server.cli.workspace_quota:main",
    "orcaster-dev-invite": "orcaster_server.cli.dev_invite:main",
}


def test_server_options_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOST", "PORT", "UVICORN_WORKERS"):
        monkeypatch.delenv(name, raising=False)
    assert server.serve_options() == {"host": "127.0.0.1", "port": 8000, "workers": 1}


def test_server_options_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9100")
    monkeypatch.setenv("UVICORN_WORKERS", "3")
    assert server.serve_options() == {"host": "0.0.0.0", "port": 9100, "workers": 3}


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_server_options_reject_invalid_port(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PORT", value)
    with pytest.raises(SystemExit):
        server.serve_options()


def test_server_main_runs_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setenv("PORT", "8123")
    server.main()
    assert calls == [
        (
            ("orcaster_server.main:app",),
            {"host": "127.0.0.1", "port": 8123, "workers": 1},
        )
    ]


def test_migrate_target_defaults_to_head() -> None:
    assert migrate.parse_args([]).target == "head"
    assert migrate.parse_args(["0061_te_worker_assigned_idx"]).target == (
        "0061_te_worker_assigned_idx"
    )


def test_migrate_main_upgrades_to_target(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(migrate.command, "upgrade", lambda cfg, rev: seen.append((cfg, rev)))
    migrate.main(["0002_admin_actions"])
    assert seen[0][1] == "0002_admin_actions"
    cfg = seen[0][0]
    assert Path(cfg.get_main_option("script_location")).joinpath("versions").is_dir()
    assert Path(cfg.config_file_name).is_file()


def test_console_scripts_registered() -> None:
    eps = {
        ep.name: ep.value
        for ep in importlib.metadata.entry_points(group="console_scripts")
        if ep.name.startswith("orcaster-")
    }
    for name, target in EXPECTED_SCRIPTS.items():
        assert eps.get(name) == target


@pytest.mark.parametrize("script", ["bootstrap", "dev_invite", "workspace_quota"])
def test_legacy_script_shims_expose_help(script: str) -> None:
    """`python scripts/<x>.py --help` still works from server/."""
    proc = subprocess.run(
        [sys.executable, f"scripts/{script}.py", "--help"],
        cwd=SERVER_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()
