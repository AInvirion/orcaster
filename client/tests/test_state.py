"""Tests for local credential persistence."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from orcaster_worker.state import Credentials, load, save, state_path


def test_load_returns_none_when_missing(tmp_path: Path):
    assert load(tmp_path) is None


def test_save_then_load_roundtrips(tmp_path: Path):
    creds = Credentials(worker_id="wid-1", api_key="key-1")
    save(tmp_path, creds)
    loaded = load(tmp_path)
    assert loaded == creds


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_save_writes_with_0600(tmp_path: Path):
    save(tmp_path, Credentials(worker_id="w", api_key="k"))
    mode = state_path(tmp_path).stat().st_mode & 0o777
    # Others must not have read access.
    assert mode & stat.S_IROTH == 0
    assert mode & stat.S_IRGRP == 0
    # The owner must be able to read/write.
    assert mode & stat.S_IRUSR
    assert mode & stat.S_IWUSR


def test_load_handles_corrupted_json(tmp_path: Path):
    state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert load(tmp_path) is None


def test_load_handles_missing_fields(tmp_path: Path):
    state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state_path(tmp_path).write_text('{"worker_id": "x"}', encoding="utf-8")
    assert load(tmp_path) is None
    # cleanup permissions
    os.chmod(tmp_path, 0o700)
