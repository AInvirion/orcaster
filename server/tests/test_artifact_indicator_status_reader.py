"""Reader of the sync_status.json contract. Safe loading: any failure
(missing, corrupt, unknown schema) returns None and the app degrades to
showing only `produced`. It never raises."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from orcaster_server.artifact_indicator_status import (
    SUPPORTED_SCHEMA_VERSION,
    SyncStatus,
    read_sync_status,
)

_VALID = {
    "schema_version": 1,
    "uploaded_total": 5923,
    "by_source": {"primary": 5923},
    "commit_sha": "6a72bcd",
    "pushed_at": "2026-06-26T15:30:00Z",
}


def _write(p: Path, payload) -> str:
    p.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return str(p)


def test_supported_schema_version_is_one() -> None:
    assert SUPPORTED_SCHEMA_VERSION == 1


def test_valid_payload_parses(tmp_path: Path) -> None:
    path = _write(tmp_path / "sync_status.json", _VALID)
    st = read_sync_status(path)
    assert isinstance(st, SyncStatus)
    assert st.uploaded_total == 5923
    assert st.by_source == {"primary": 5923}
    assert st.commit_sha == "6a72bcd"
    assert isinstance(st.pushed_at, datetime)


def test_empty_path_returns_none() -> None:
    assert read_sync_status("") is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_sync_status(str(tmp_path / "nope.json")) is None


def test_invalid_json_returns_none(tmp_path: Path) -> None:
    path = _write(tmp_path / "bad.json", "{not json")
    assert read_sync_status(path) is None


def test_non_object_json_returns_none(tmp_path: Path) -> None:
    path = _write(tmp_path / "arr.json", "[1, 2, 3]")
    assert read_sync_status(path) is None


def test_unknown_schema_version_returns_none(tmp_path: Path) -> None:
    payload = dict(_VALID, schema_version=999)
    path = _write(tmp_path / "future.json", payload)
    assert read_sync_status(path) is None


def test_missing_schema_version_returns_none(tmp_path: Path) -> None:
    payload = {k: v for k, v in _VALID.items() if k != "schema_version"}
    path = _write(tmp_path / "noschema.json", payload)
    assert read_sync_status(path) is None


def test_missing_optional_fields_ok(tmp_path: Path) -> None:
    payload = {"schema_version": 1, "uploaded_total": 10}
    path = _write(tmp_path / "minimal.json", payload)
    st = read_sync_status(path)
    assert st is not None
    assert st.uploaded_total == 10
    assert st.by_source == {}
    assert st.commit_sha is None
    assert st.pushed_at is None


def test_bad_uploaded_total_returns_none(tmp_path: Path) -> None:
    payload = dict(_VALID, uploaded_total="lots")
    path = _write(tmp_path / "badtotal.json", payload)
    assert read_sync_status(path) is None


def test_additive_last_error_fields_do_not_break_the_reader(tmp_path: Path) -> None:
    """The external sync job adds `last_error*` to the status when
    it fails to fetch base, without bumping `schema_version`: the reader ignores them and
    keeps returning the counters and `pushed_at` of the last healthy run."""
    payload = dict(
        _VALID,
        last_error="commit " + "a" * 40 + " is missing from the clone",
        last_error_kind="missing_commit",
        last_error_at="2026-06-26T16:00:00Z",
    )
    st = read_sync_status(_write(tmp_path / "failing.json", payload))
    assert st is not None
    assert st.uploaded_total == 5923
    assert st.commit_sha == "6a72bcd"
    assert st.pushed_at == datetime.fromisoformat("2026-06-26T15:30:00+00:00")
