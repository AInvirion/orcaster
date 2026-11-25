"""The worker script entry point."""

import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _project():
    return tomllib.loads(_PYPROJECT.read_text())["project"]


def test_distribution_is_orcaster_worker():
    assert _project()["name"] == "orcaster-worker"


def test_only_the_orcaster_worker_script_exists():
    assert _project()["scripts"] == {"orcaster-worker": "orcaster_worker.main:cli_entry"}


def test_script_target_resolves_to_callable():
    import importlib

    module, _, attr = _project()["scripts"]["orcaster-worker"].partition(":")
    assert callable(getattr(importlib.import_module(module), attr))


def test_cli_prog_and_version_use_new_name(monkeypatch, capsys):
    from orcaster_worker import main

    monkeypatch.setattr("sys.argv", ["x", "--version"])
    with pytest.raises(SystemExit):
        main.cli_entry()
    assert capsys.readouterr().out.startswith("orcaster-worker ")
