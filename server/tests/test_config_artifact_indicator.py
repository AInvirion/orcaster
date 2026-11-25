"""Artifact indicator settings: the suffix and the sync_status.json path."""

from __future__ import annotations

from orcaster_server.config import Settings

_NEW_PATH = "/var/lib/new/sync_status.json"


def test_suffix_default_empty() -> None:
    assert Settings(_env_file=None).artifact_indicator_suffix == ""


def test_sync_status_path_default_empty() -> None:
    assert Settings(_env_file=None).artifact_indicator_sync_status_path == ""


def test_new_names_work(monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACT_INDICATOR_SUFFIX", ".report.json")
    monkeypatch.setenv("ARTIFACT_INDICATOR_SYNC_STATUS_PATH", _NEW_PATH)
    s = Settings(_env_file=None)
    assert s.artifact_indicator_suffix == ".report.json"
    assert s.artifact_indicator_sync_status_path == _NEW_PATH
