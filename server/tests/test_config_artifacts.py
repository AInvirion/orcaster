"""Artifact settings: defaults and env override."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orcaster_server.config import Settings


def test_artifacts_defaults() -> None:
    s = Settings()
    # Default outside /opt/<repo> to survive the deploy rsync --delete.
    assert str(s.artifacts_root) == "/var/lib/orcaster-artifacts"
    assert s.artifact_max_file_bytes == 50 * 1024 * 1024
    assert s.artifact_max_total_bytes == 200 * 1024 * 1024
    assert s.artifact_max_count == 500


def test_artifacts_root_env_override(monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACTS_ROOT", "/tmp/my-artifacts")
    monkeypatch.setenv("ARTIFACT_MAX_FILE_BYTES", "1024")
    s = Settings()
    assert str(s.artifacts_root) == "/tmp/my-artifacts"
    assert s.artifact_max_file_bytes == 1024


def test_artifact_storage_defaults() -> None:
    """The new storage limits have sane defaults (10 GiB / 2 GiB)."""
    # _env_file=None: isolates the test from a local .env that could override it.
    s = Settings(_env_file=None)
    assert s.artifact_workspace_max_total_bytes == 10 * 1024 * 1024 * 1024
    assert s.artifact_disk_min_free_bytes == 2 * 1024 * 1024 * 1024


def test_artifact_indicator_suffix_default_is_off() -> None:
    # _env_file=None: isolates from a local .env that could override the alias.
    s = Settings(_env_file=None)
    assert s.artifact_indicator_suffix == ""


def test_artifact_indicator_suffix_env_override(monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACT_INDICATOR_SUFFIX", ".fp.json")
    s = Settings(_env_file=None)
    assert s.artifact_indicator_suffix == ".fp.json"


def test_artifact_indicator_suffix_can_be_empty(monkeypatch) -> None:
    # Empty => feature off (the endpoint does not render the pill).
    monkeypatch.setenv("ARTIFACT_INDICATOR_SUFFIX", "")
    s = Settings(_env_file=None)
    assert s.artifact_indicator_suffix == ""


def test_default_artifact_upload_body_limit_is_total_plus_overhead() -> None:
    s = Settings(_env_file=None)
    assert s.max_artifact_upload_body_bytes == (200 + 8) * 1024 * 1024


@pytest.mark.parametrize("overhead", ["0", "-1"])
def test_artifact_upload_overhead_below_one_fails_at_startup(monkeypatch, overhead) -> None:
    """An overhead < 1 makes a file of the maximum size receive 413 (the multipart
    framing adds bytes): it fails at startup and names the variable, no fallback."""
    monkeypatch.setenv("ARTIFACT_UPLOAD_OVERHEAD_BYTES", overhead)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    message = str(excinfo.value)
    assert "ARTIFACT_UPLOAD_OVERHEAD_BYTES" in message
    assert "greater than or equal to 1" in message


@pytest.mark.parametrize(
    "env",
    [
        {
            "ARTIFACT_MAX_TOTAL_BYTES": "-10",
            "ARTIFACT_MAX_FILE_BYTES": "-20",
            "ARTIFACT_UPLOAD_OVERHEAD_BYTES": "5",
        },
        {
            "ARTIFACT_MAX_TOTAL_BYTES": "-8",
            "ARTIFACT_MAX_FILE_BYTES": "-8",
            "ARTIFACT_UPLOAD_OVERHEAD_BYTES": "8",
        },
    ],
)
def test_non_positive_artifact_upload_limit_fails_at_startup(monkeypatch, env) -> None:
    """Never a silent fallback: an upload limit <= 0 would block every upload."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match="artifact upload body limit must be positive"):
        Settings(_env_file=None)


def test_positive_artifact_upload_limit_with_one_zero_setting_is_accepted(monkeypatch) -> None:
    monkeypatch.setenv("ARTIFACT_MAX_TOTAL_BYTES", "0")
    monkeypatch.setenv("ARTIFACT_MAX_FILE_BYTES", "1024")
    monkeypatch.setenv("ARTIFACT_UPLOAD_OVERHEAD_BYTES", "1")
    assert Settings(_env_file=None).max_artifact_upload_body_bytes == 1025
