"""Tests for idempotency.py -- local sqlite commands_seen."""

from __future__ import annotations

import os
import stat
import sys

from orcaster_worker.idempotency import db_path, is_seen, mark_seen


def test_mark_and_is_seen_basic(tmp_path):
    """mark_seen + is_seen is the base contract."""
    state = tmp_path / "state"
    uuid = "11111111-1111-1111-1111-111111111111"
    assert is_seen(state, uuid) is False
    mark_seen(state, uuid)
    assert is_seen(state, uuid) is True


def test_mark_seen_is_idempotent(tmp_path):
    """A second call with the same uuid does not crash (INSERT OR IGNORE)."""
    state = tmp_path / "state"
    uuid = "deadbeef-dead-beef-dead-beefdeadbeef"
    mark_seen(state, uuid)
    mark_seen(state, uuid)  # must not raise IntegrityError
    assert is_seen(state, uuid) is True


def test_db_file_has_0600_permissions_on_posix(tmp_path):
    """On POSIX the file must have mode 0600 (same as state.json)."""
    if sys.platform == "win32":
        return
    state = tmp_path / "state"
    mark_seen(state, "u-1")
    p = db_path(state)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    # We accept exactly 0600; any group/world permission raises an alert.
    assert mode & 0o077 == 0, f"db file has too-permissive mode {oct(mode)}"


def test_distinct_uuids_are_independent(tmp_path):
    """is_seen('u1') must not contaminate is_seen('u2')."""
    state = tmp_path / "state"
    mark_seen(state, "u1")
    assert is_seen(state, "u1") is True
    assert is_seen(state, "u2") is False


def test_persistence_across_reopens(tmp_path):
    """sqlite persists across 'sessions' (each call opens/closes the connection)."""
    state = tmp_path / "state"
    mark_seen(state, "persist-me")
    # Simulate a worker restart: a new flag at another path does not affect this one.
    assert is_seen(state, "persist-me") is True


# --- push_task_results ------------------------------------------------------


def test_get_push_result_returns_none_when_missing(tmp_path):
    from orcaster_worker.idempotency import get_push_result

    assert get_push_result(tmp_path / "state", "nope-key") is None


def test_store_and_get_push_result_roundtrip(tmp_path):
    from orcaster_worker.idempotency import get_push_result, store_push_result

    state = tmp_path / "state"
    store_push_result(
        state,
        "key-1",
        status="completed",
        exit_code=0,
        stdout="hello",
        stderr="",
        result={"a": 1, "b": [2, 3]},
    )
    out = get_push_result(state, "key-1")
    assert out is not None
    assert out["status"] == "completed"
    assert out["exit_code"] == 0
    assert out["stdout"] == "hello"
    assert out["result"] == {"a": 1, "b": [2, 3]}


def test_store_push_result_is_idempotent(tmp_path):
    """INSERT OR IGNORE: the first execution is the one that counts."""
    from orcaster_worker.idempotency import get_push_result, store_push_result

    state = tmp_path / "state"
    store_push_result(state, "key-2", status="completed", exit_code=0, stdout="first")
    # Second call with different data: must not overwrite.
    store_push_result(state, "key-2", status="failed", exit_code=1, stdout="second")
    out = get_push_result(state, "key-2")
    assert out is not None
    assert out["status"] == "completed"
    assert out["stdout"] == "first"


def test_commands_seen_and_push_task_results_coexist(tmp_path):
    """Both tables live in the same sqlite without interfering."""
    from orcaster_worker.idempotency import (
        get_push_result,
        is_seen,
        mark_seen,
        store_push_result,
    )

    state = tmp_path / "state"
    mark_seen(state, "legacy-cmd-uuid")
    store_push_result(state, "key-3", status="completed", exit_code=0)
    assert is_seen(state, "legacy-cmd-uuid") is True
    assert get_push_result(state, "key-3") is not None
    assert is_seen(state, "key-3") is False
    assert get_push_result(state, "legacy-cmd-uuid") is None
