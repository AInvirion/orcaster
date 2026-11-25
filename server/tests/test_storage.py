"""Tests of the storage module: disk measurement + counter."""

from __future__ import annotations

from pathlib import Path

import pytest

from orcaster_server import storage
from orcaster_server.config import settings

DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


def test_disk_free_bytes_positive_on_existing_dir(tmp_path: Path) -> None:
    free = storage.disk_free_bytes(tmp_path)
    assert free > 0


def test_disk_free_bytes_does_not_create_dir(tmp_path: Path) -> None:
    """The mkdir moved to startup (lifespan), out of the poll hot path.
    On a nonexistent path, statvfs fails → fail-open, without creating anything."""
    missing = tmp_path / "artifacts"
    free = storage.disk_free_bytes(missing)
    assert free == storage._UNLIMITED
    assert not missing.exists()


def test_ensure_artifacts_root_creates_dir(tmp_path: Path) -> None:
    """Creates the root (with parents) once at startup.
    Idempotent: a second call on an existing dir does not fail."""
    root = tmp_path / "a" / "b" / "artifacts"
    storage.ensure_artifacts_root(root)
    assert root.is_dir()
    storage.ensure_artifacts_root(root)  # idempotent
    assert root.is_dir()


def test_ensure_artifacts_root_fails_closed_when_it_cannot_be_created(tmp_path: Path) -> None:
    """A root that cannot be created stops startup with an error naming the
    path and the setting, instead of a warning."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a dir")
    target = blocker / "artifacts"
    with pytest.raises(storage.ArtifactsRootError) as excinfo:
        storage.ensure_artifacts_root(target)
    assert "ARTIFACTS_ROOT" in str(excinfo.value)
    assert str(target) in str(excinfo.value)
    assert not target.exists()


def test_ensure_artifacts_root_fails_closed_when_not_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "artifacts"
    target.write_text("not a dir")
    with pytest.raises(storage.ArtifactsRootError, match="not a directory"):
        storage.ensure_artifacts_root(target)


def test_ensure_artifacts_root_fails_closed_when_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers a read-only root (e.g. outside the unit's ReadWritePaths)."""
    target = tmp_path / "artifacts"
    target.mkdir()
    monkeypatch.setattr(storage.os, "access", lambda *_a, **_k: False)
    with pytest.raises(storage.ArtifactsRootError, match="not writable") as excinfo:
        storage.ensure_artifacts_root(target)
    assert str(target) in str(excinfo.value)


def test_disk_free_bytes_fails_open_on_oserror(tmp_path: Path, monkeypatch) -> None:
    """If statvfs blows up, fail-open: returns a large sentinel (do not block)."""

    def _boom(_path):
        raise OSError("statvfs boom")

    monkeypatch.setattr(storage.os, "statvfs", _boom)
    free = storage.disk_free_bytes(tmp_path)
    assert free == storage._UNLIMITED


@pytest.mark.asyncio
async def test_storage_status_reuses_cached_disk_free(db_session, monkeypatch) -> None:
    """With a live TTL, statvfs is not repeated on every poll call."""
    calls: list[Path] = []

    def _measure(root: Path) -> int:
        calls.append(root)
        return 12_345

    monkeypatch.setattr(storage, "disk_free_bytes", _measure)
    monkeypatch.setattr(settings, "artifact_disk_free_cache_ttl_s", 60.0)
    storage.disk_free_cache.clear()
    try:
        first = await storage.storage_status(db_session, DEFAULT_WORKSPACE_ID, settings=settings)
        second = await storage.storage_status(db_session, DEFAULT_WORKSPACE_ID, settings=settings)
    finally:
        storage.disk_free_cache.clear()

    assert first.free_bytes == second.free_bytes == 12_345
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_storage_status_measures_every_call_with_ttl_zero(db_session, monkeypatch) -> None:
    """TTL=0 (the tests' default): no cache, every call measures."""
    calls: list[Path] = []

    def _measure(root: Path) -> int:
        calls.append(root)
        return 12_345

    monkeypatch.setattr(storage, "disk_free_bytes", _measure)
    monkeypatch.setattr(settings, "artifact_disk_free_cache_ttl_s", 0.0)
    storage.disk_free_cache.clear()

    await storage.storage_status(db_session, DEFAULT_WORKSPACE_ID, settings=settings)
    await storage.storage_status(db_session, DEFAULT_WORKSPACE_ID, settings=settings)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_workspace_usage_zero_when_no_row(db_session) -> None:
    used = await storage.workspace_usage_bytes(db_session, DEFAULT_WORKSPACE_ID)
    assert used == 0


@pytest.mark.asyncio
async def test_bump_workspace_usage_upsert_and_clamp(db_session) -> None:
    await storage.bump_workspace_usage(db_session, DEFAULT_WORKSPACE_ID, 500)
    await db_session.commit()
    assert await storage.workspace_usage_bytes(db_session, DEFAULT_WORKSPACE_ID) == 500

    # Additional increment.
    await storage.bump_workspace_usage(db_session, DEFAULT_WORKSPACE_ID, 250)
    await db_session.commit()
    assert await storage.workspace_usage_bytes(db_session, DEFAULT_WORKSPACE_ID) == 750

    # A decrement below 0 is clamped to 0 (never negative).
    await storage.bump_workspace_usage(db_session, DEFAULT_WORKSPACE_ID, -10_000)
    await db_session.commit()
    assert await storage.workspace_usage_bytes(db_session, DEFAULT_WORKSPACE_ID) == 0
