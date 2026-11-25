from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from orcaster_worker.cache_flush import FlushStats, flush_caches
from orcaster_worker.task_cache import ensure_task_cache_dir

_LOGGER_NAME = "orcaster.worker.cache_flush"


def test_flush_wipes_tasks_and_uv(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    tasks = cache_root / "tasks"
    (tasks / "abc").mkdir(parents=True)
    (tasks / "abc" / "f.txt").write_text("x" * 100)
    uv = tmp_path / "uv"
    uv.mkdir()
    (uv / "blob").write_text("y" * 50)

    stats = flush_caches(cache_root, uv)

    assert isinstance(stats, FlushStats)
    assert tasks.exists()
    assert list(tasks.iterdir()) == []
    assert not uv.exists()


def test_flush_idempotent_on_missing_dirs(tmp_path: Path) -> None:
    cache_root = tmp_path / "nope"
    uv = tmp_path / "also-nope"
    stats = flush_caches(cache_root, uv)
    assert isinstance(stats, FlushStats)
    # A second pass still does not break and returns valid stats.
    assert isinstance(flush_caches(cache_root, uv), FlushStats)


def test_flush_creates_tasks_when_cache_root_exists_but_tasks_absent(tmp_path: Path) -> None:
    # First-boot scenario: cache_root exists but tasks/ does not yet.
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    stats = flush_caches(cache_root, None)
    assert isinstance(stats, FlushStats)
    assert (cache_root / "tasks").is_dir()


def test_flush_tolerates_none_uv(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    (cache_root / "tasks" / "x").mkdir(parents=True)
    stats = flush_caches(cache_root, None)
    assert isinstance(stats, FlushStats)
    assert (cache_root / "tasks").exists()


def test_flush_computes_bytes_when_debug_enabled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # With DEBUG enabled on the module logger, the walk runs and the freed
    # bytes are computed (> 0) when there are files.
    cache_root = tmp_path / "cache"
    tasks = cache_root / "tasks"
    (tasks / "abc").mkdir(parents=True)
    (tasks / "abc" / "f.txt").write_text("x" * 100)
    uv = tmp_path / "uv"
    uv.mkdir()
    (uv / "blob").write_text("y" * 50)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        stats = flush_caches(cache_root, uv)

    assert isinstance(stats, FlushStats)
    assert stats.tasks_bytes_freed > 0
    assert stats.uv_bytes_freed > 0
    # The wipe happens anyway.
    assert tasks.exists()
    assert list(tasks.iterdir()) == []
    assert not uv.exists()


def test_flush_skips_byte_walk_at_info_level(tmp_path: Path) -> None:
    # With DEBUG disabled (INFO level), the walk is skipped: the bytes
    # fields stay at 0 but the wipe is performed anyway.
    logger = logging.getLogger(_LOGGER_NAME)
    prev_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        cache_root = tmp_path / "cache"
        tasks = cache_root / "tasks"
        (tasks / "abc").mkdir(parents=True)
        (tasks / "abc" / "f.txt").write_text("x" * 100)
        uv = tmp_path / "uv"
        uv.mkdir()
        (uv / "blob").write_text("y" * 50)

        assert logger.isEnabledFor(logging.DEBUG) is False

        stats = flush_caches(cache_root, uv)
    finally:
        logger.setLevel(prev_level)

    assert isinstance(stats, FlushStats)
    assert stats.tasks_bytes_freed == 0
    assert stats.uv_bytes_freed == 0
    # The wipe happens anyway.
    assert tasks.exists()
    assert list(tasks.iterdir()) == []
    assert not uv.exists()


def test_flush_with_uv_none_only_wipes_tasks(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    tasks = cache_root / "tasks"
    (tasks / "abc").mkdir(parents=True)
    (tasks / "abc" / "f.txt").write_text("x" * 100)

    stats = flush_caches(cache_root, None)

    assert isinstance(stats, FlushStats)
    assert tasks.exists()
    assert list(tasks.iterdir()) == []
    assert stats.uv_bytes_freed == 0


# ---------------------------------------------------------------------------
# Detach-before-delete: the orphaned thread does not touch the live namespace
# ---------------------------------------------------------------------------


def test_flush_detaches_tasks_before_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case: ``_run_cache_flush`` abandons the thread when the timeout expires
    and the worker accepts tasks again, so the orphaned
    ``rmtree`` runs in parallel with a new job that writes into
    ``tasks/``. With the tree out of the namespace before deletion, the new job
    writes into a freshly created ``tasks/`` that the orphan can no longer reach.
    """
    cache_root = tmp_path / "cache"
    tasks = cache_root / "tasks"
    (tasks / "old-job").mkdir(parents=True)
    (tasks / "old-job" / "f.bin").write_text("x" * 100)

    real_rmtree = shutil.rmtree

    def orphaned_rmtree(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        # While the rmtree runs, the worker accepts a new job.
        fresh = ensure_task_cache_dir(cache_root, "new-job")
        (fresh / "download.bin").write_text("half-downloaded file")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("orcaster_worker.cache_flush.shutil.rmtree", orphaned_rmtree)

    flush_caches(cache_root, None)

    assert (tasks / "new-job" / "download.bin").exists(), (
        "the orphaned rmtree took the dir of the job that started after the timeout"
    )
    assert not (tasks / "old-job").exists(), "the flush must still remove the old cache"


def test_flush_detaches_the_uv_cache_before_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker-wide uv cache (``ORCH_ORCASTER_UV_CACHE_DIR``) has the same
    window: the new job repopulates it with ``uv venv`` / ``uv pip install``
    while the orphan unlinks it."""
    cache_root = tmp_path / "cache"
    (cache_root / "tasks").mkdir(parents=True)
    uv = tmp_path / "uvcache"
    uv.mkdir()
    (uv / "old.whl").write_text("z" * 20)

    real_rmtree = shutil.rmtree

    def orphaned_rmtree(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        # The new job repopulates the uv cache during the deletion.
        uv.mkdir(parents=True, exist_ok=True)
        (uv / "new.whl").write_text("w" * 20)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("orcaster_worker.cache_flush.shutil.rmtree", orphaned_rmtree)

    flush_caches(cache_root, uv)

    assert (uv / "new.whl").exists(), "the orphan took the uv cache that the new job repopulated"
    assert not (uv / "old.whl").exists()


def test_flush_leaves_no_trash_behind(tmp_path: Path) -> None:
    """The detach is an intermediate step: the flush deletes synchronously as it does today."""
    cache_root = tmp_path / "cache"
    (cache_root / "tasks" / "job").mkdir(parents=True)
    (cache_root / "tasks" / "job" / "f.bin").write_text("x" * 10)
    uv = tmp_path / "uvcache"
    uv.mkdir()
    (uv / "blob").write_text("y" * 10)

    flush_caches(cache_root, uv)

    for trash in (cache_root / "trash", uv.parent / "trash"):
        assert not trash.exists() or list(trash.iterdir()) == []


def test_flush_falls_back_to_in_place_delete_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a possible rename, the flush keeps deleting: its contract is to free disk."""
    cache_root = tmp_path / "cache"
    (cache_root / "tasks" / "job").mkdir(parents=True)
    (cache_root / "tasks" / "job" / "f.bin").write_text("x" * 10)

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("EXDEV: cross-device link")

    monkeypatch.setattr("orcaster_worker.task_cache.os.replace", boom)

    flush_caches(cache_root, None)

    assert (cache_root / "tasks").is_dir()
    assert list((cache_root / "tasks").iterdir()) == []


async def test_self_flush_uses_flush_caches_not_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The self-flush on the disk preflight deletes EVERYTHING via
    ``flush_caches``, NOT via ``reset_other_job_caches``.

    ``reset_other_job_caches`` deliberately preserves the in-progress job's dir,
    but in the preflight the disk was consumed by that same job's partial
    download: keeping it would free nothing and the retry would die again in the
    preflight. This test shields that decision against a future rewiring.
    """
    import orcaster_worker.main as main_mod
    from orcaster_worker.config import Settings

    calls = {"flush": 0, "reset": 0}

    def fake_flush(cache_root, uv_cache_dir):
        calls["flush"] += 1
        return FlushStats(0, 0)

    def fake_reset(*args, **kwargs):
        calls["reset"] += 1
        raise AssertionError("the self-flush must not use reset_other_job_caches")

    monkeypatch.setattr(main_mod, "flush_caches", fake_flush)
    monkeypatch.setattr(main_mod, "reset_other_job_caches", fake_reset)

    settings = Settings(
        server_url="http://server.test",  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
    )
    busy = main_mod.BusyState()
    await main_mod._run_cache_flush(settings, busy)

    assert calls["flush"] == 1
    assert calls["reset"] == 0
    assert busy.cache_flush_pending_ack is True
