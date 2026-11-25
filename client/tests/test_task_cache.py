"""Tests for the task-scoped scratch cache and its GC.

Covers:
        * resolve_job_key: idempotency_key wins over task_id; sanitizes characters.
        * ensure_task_cache_dir: idempotent, refreshes mtime, expected layout.
        * run_gc by age: deletes dirs with an old mtime, respects recent ones.
        * run_gc by pressure: reactive LRU only if free/total < min_free_fraction.
        * run_gc no-op: under normal conditions it deletes nothing.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from orcaster_worker.task_cache import (
    GcStats,
    ResetStats,
    _detach_for_delete,
    create_execution_run_dir,
    ensure_task_cache_dir,
    reset_other_job_caches,
    resolve_job_key,
    run_gc,
    sweep_run_dirs,
    sweep_trash_dirs,
)


class _FakeUsage:
    """Stand-in for shutil._ntuple_diskusage in mocks (.total/.used/.free access)."""

    def __init__(self, total: int, used: int, free: int) -> None:
        self.total = total
        self.used = used
        self.free = free


def _make_old(path: Path, days: int) -> None:
    """Backdate mtime/atime of ``path`` by ``days`` days."""
    target = time.time() - days * 86400
    os.utime(path, (target, target))


def test_resolve_job_key_prefers_idempotency() -> None:
    assert resolve_job_key(idempotency_key="abc-123", task_id=42) == "abc-123"


def test_resolve_job_key_falls_back_to_task_id() -> None:
    assert resolve_job_key(idempotency_key=None, task_id=42) == "task-42"


def test_resolve_job_key_sanitizes_chars() -> None:
    # Slashes / nulls / spaces must be replaced.
    out = resolve_job_key(idempotency_key="a/b c\x00d", task_id=1)
    assert "/" not in out
    assert " " not in out
    assert "\x00" not in out


def test_ensure_task_cache_dir_creates_layout(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    path = ensure_task_cache_dir(cache_root, "job-1")
    assert path.is_dir()
    assert path.parent == cache_root / "tasks"
    assert path.name == "job-1"


def test_ensure_task_cache_dir_is_idempotent(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    p1 = ensure_task_cache_dir(cache_root, "job-1")
    (p1 / "marker.txt").write_text("hello")
    p2 = ensure_task_cache_dir(cache_root, "job-1")
    assert p1 == p2
    assert (p2 / "marker.txt").read_text() == "hello"


def test_ensure_task_cache_dir_refreshes_mtime(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    path = ensure_task_cache_dir(cache_root, "job-1")
    _make_old(path, days=30)
    old_mtime = path.stat().st_mtime
    # Force enough delta for utime to have an observable effect.
    time.sleep(0.01)
    path2 = ensure_task_cache_dir(cache_root, "job-1")
    assert path2.stat().st_mtime > old_mtime


def test_run_gc_deletes_old_dirs(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    old = ensure_task_cache_dir(cache_root, "old")
    young = ensure_task_cache_dir(cache_root, "young")
    _make_old(old, days=30)
    # young stays with mtime ~ now.

    # normal disk_usage (no pressure) to isolate the "by age" path.
    fake = _FakeUsage(1_000_000, 200_000, 800_000)  # free/total=0.8
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )

    assert isinstance(stats, GcStats)
    assert stats.deleted_age == 1
    assert stats.deleted_pressure == 0
    assert not old.exists()
    assert young.exists()


def test_run_gc_pressure_triggers_lru(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    a = ensure_task_cache_dir(cache_root, "a")  # oldest (LRU first)
    time.sleep(0.01)
    b = ensure_task_cache_dir(cache_root, "b")
    time.sleep(0.01)
    c = ensure_task_cache_dir(cache_root, "c")  # most recent

    _make_old(a, days=2)
    _make_old(b, days=1)
    # c stays ~ now; none exceeds max_age_days=14 -> pressure only.

    # Sequence: disk_usage is queried once at the start (free_before),
    # then once per candidate evaluated in the LRU loop, and once at the end
    # (free_after). We simulate sustained pressure until 2 dirs are removed.
    usages = [
        _FakeUsage(1000, 990, 10),  # free_before: free/total = 0.01 < 0.05 -> trigger
        _FakeUsage(1000, 990, 10),  # before evaluating 'a' -> still below target 0.15
        _FakeUsage(1000, 800, 200),  # before evaluating 'b' -> free/total = 0.20 >= 0.15 -> stop
        _FakeUsage(1000, 800, 200),  # free_after
    ]
    fake_iter = iter(usages)

    def _fake_usage(_path: object) -> _FakeUsage:
        return next(fake_iter)

    with patch("orcaster_worker.task_cache.shutil.disk_usage", side_effect=_fake_usage):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )

    assert stats.deleted_age == 0
    assert stats.deleted_pressure == 1, (
        "expected a single delete: 'a' (oldest); then disk_usage reports "
        "free/total >= target and it stops"
    )
    assert not a.exists()
    assert b.exists()
    assert c.exists()


def test_run_gc_noop_in_normal_conditions(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    p = ensure_task_cache_dir(cache_root, "fresh")

    fake = _FakeUsage(1_000_000, 100_000, 900_000)  # 90% free
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )

    assert stats.deleted_age == 0
    assert stats.deleted_pressure == 0
    assert p.exists()


def test_run_gc_handles_missing_cache_root(tmp_path: Path) -> None:
    """If the cache was never used, GC returns empty stats without failing."""
    cache_root = tmp_path / "never-created"
    # tasks_root does not exist.
    fake = _FakeUsage(1000, 100, 900)
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )
    assert stats.scanned == 0
    assert stats.deleted_age == 0
    assert stats.deleted_pressure == 0


def test_run_gc_uses_injected_now(tmp_path: Path) -> None:
    """`now` allows deterministic cutoffs without touching the real mtime."""
    cache_root = tmp_path / "cache"
    p = ensure_task_cache_dir(cache_root, "x")
    _make_old(p, days=5)

    # We simulate "now = mtime + 20 days" with max_age_days=14 -> delete.
    fake_now = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC) + timedelta(days=20)
    fake = _FakeUsage(1_000_000, 100_000, 900_000)
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            now=fake_now,
        )
    assert stats.deleted_age == 1
    assert not p.exists()


# ---------------------------------------------------------------------------
# create_execution_run_dir and sweep_run_dirs
# ---------------------------------------------------------------------------


def test_create_execution_run_dir_creates_subdir_under_run(tmp_path: Path) -> None:
    """The ephemeral dir lands under <cache_root>/run/ with the expected prefix."""
    cache_root = tmp_path / "cache"
    run_dir = create_execution_run_dir(cache_root, task_id=42)
    assert run_dir.is_dir()
    assert run_dir.parent == cache_root / "run"
    assert run_dir.name.startswith("task-42-")


def test_create_execution_run_dir_two_calls_are_unique(tmp_path: Path) -> None:
    """Two calls for the same task_id generate different directories."""
    cache_root = tmp_path / "cache"
    a = create_execution_run_dir(cache_root, task_id=7)
    b = create_execution_run_dir(cache_root, task_id=7)
    assert a != b
    assert a.is_dir()
    assert b.is_dir()


def test_sweep_run_dirs_deletes_existing_subdirs(tmp_path: Path) -> None:
    """sweep_run_dirs removes all residual subdirs and returns the count."""
    cache_root = tmp_path / "cache"
    run_root = cache_root / "run"
    run_root.mkdir(parents=True)
    (run_root / "task-1-abc").mkdir()
    (run_root / "task-2-def").mkdir()

    count = sweep_run_dirs(cache_root)

    assert count == 2
    assert not (run_root / "task-1-abc").exists()
    assert not (run_root / "task-2-def").exists()


def test_sweep_run_dirs_noop_when_run_dir_absent(tmp_path: Path) -> None:
    """If <cache_root>/run/ does not exist, sweep_run_dirs returns 0 without error."""
    cache_root = tmp_path / "cache"
    # We do not create cache_root or the run/ subdir
    count = sweep_run_dirs(cache_root)
    assert count == 0


def test_sweep_run_dirs_returns_zero_when_empty(tmp_path: Path) -> None:
    """If <cache_root>/run/ exists but is empty, it returns 0."""
    cache_root = tmp_path / "cache"
    (cache_root / "run").mkdir(parents=True)
    count = sweep_run_dirs(cache_root)
    assert count == 0


# ---------------------------------------------------------------------------
# reset_other_job_caches
# ---------------------------------------------------------------------------


def test_reset_deletes_other_jobs_keeps_target(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    keep = ensure_task_cache_dir(cache_root, "job-keep")
    (keep / "marker.txt").write_text("keep-me")
    other = ensure_task_cache_dir(cache_root, "job-old")
    (other / "pkg").mkdir()
    (other / "pkg" / "data.bin").write_text("payload")

    stats = reset_other_job_caches(cache_root, keep_job_key="job-keep")

    assert isinstance(stats, ResetStats)
    assert stats.failed_dirs == ()
    assert stats.dirs_deleted == 1
    assert not other.exists()
    assert keep.exists()
    assert (keep / "marker.txt").read_text() == "keep-me"


def test_reset_does_not_touch_run_dir(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    ensure_task_cache_dir(cache_root, "job-old")
    run_dir = create_execution_run_dir(cache_root, task_id=42)

    reset_other_job_caches(cache_root, keep_job_key="job-new")

    assert run_dir.exists()


def test_reset_idempotent_and_tolerates_missing_tasks(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    # tasks/ does not exist yet
    stats = reset_other_job_caches(cache_root, keep_job_key="job-x")
    assert stats.dirs_deleted == 0
    assert stats.failed_dirs == ()


def test_reset_reports_failed_dirs(tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "cache"
    other = ensure_task_cache_dir(cache_root, "job-old")

    # Simulate a delete that does not remove the dir (permission/lock): _delete_dir
    # returns bytes but the tree is still present.
    import orcaster_worker.task_cache as tc

    def _fake_delete(path: Path) -> int:
        return 0  # deletes nothing

    monkeypatch.setattr(tc, "_delete_dir", _fake_delete)

    stats = reset_other_job_caches(cache_root, keep_job_key="job-new")

    assert other in stats.failed_dirs
    assert stats.dirs_deleted == 0


# ---------------------------------------------------------------------------
# run_gc excludes the active job
# ---------------------------------------------------------------------------


def test_run_gc_keeps_active_job_in_age_pass(tmp_path: Path) -> None:
    """The live job's dir survives the age pass even if its mtime is old.

    A multi-day job with a single attempt does not refresh the dir's mtime after
    ``ensure_task_cache_dir``; without the exclusion, the GC sweeps it away live.
    """
    cache_root = tmp_path / "cache"
    active = ensure_task_cache_dir(cache_root, "job-active")
    other = ensure_task_cache_dir(cache_root, "job-other")
    _make_old(active, days=30)
    _make_old(other, days=30)

    fake = _FakeUsage(1_000_000, 200_000, 800_000)  # no pressure
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=lambda: "job-active",
        )

    assert active.exists(), "the GC deleted the active job's dir in the age pass"
    assert not other.exists()
    assert stats.deleted_age == 1
    assert stats.skipped_active == 1


def test_run_gc_keeps_active_job_under_pressure(tmp_path: Path) -> None:
    """Under disk pressure, the LRU can never pick the live job's dir.

    This is the case where ``reset_other_job_caches`` leaves a single candidate (the
    active job) and the pressure branch deletes it while the task downloads.
    """
    cache_root = tmp_path / "cache"
    active = ensure_task_cache_dir(cache_root, "job-active")
    _make_old(active, days=1)  # the oldest -> first in the LRU

    fake = _FakeUsage(1000, 990, 10)  # free/total = 0.01 < 0.05 -> sustained pressure
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=lambda: "job-active",
        )

    assert active.exists(), "the GC deleted the active job's dir under disk pressure"
    assert stats.deleted_pressure == 0
    assert stats.pressure_unrecoverable is True


def test_run_gc_logs_when_pressure_is_unrecoverable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With no candidates to delete, the GC says so in the log instead of staying quiet."""
    cache_root = tmp_path / "cache"
    ensure_task_cache_dir(cache_root, "job-active")

    fake = _FakeUsage(1000, 990, 10)
    with (
        caplog.at_level(logging.WARNING, logger="orcaster.worker.task_cache"),
        patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake),
    ):
        run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=lambda: "job-active",
        )

    assert any("unrecoverable" in r.message for r in caplog.records), caplog.text


def test_run_gc_does_not_cry_wolf_when_pressure_was_resolved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the GC deleted enough to get out of the trigger floor, the pressure
    was resolved: no warning even if the target was not reached.

    A WARNING that cries wolf trains the operator to ignore it, which is the
    opposite of what is wanted from that log.
    """
    cache_root = tmp_path / "cache"
    stale = ensure_task_cache_dir(cache_root, "job-stale")
    _make_old(stale, days=1)

    usages = [
        _FakeUsage(1000, 990, 10),  # free_before: 0.01 < 0.05 -> triggers pressure
        _FakeUsage(1000, 990, 10),  # before evaluating 'job-stale' -> still below target
        _FakeUsage(1000, 900, 100),  # free_after: 0.10 >= min 0.05, although < target 0.15
    ]
    fake_iter = iter(usages)

    with (
        caplog.at_level(logging.WARNING, logger="orcaster.worker.task_cache"),
        patch(
            "orcaster_worker.task_cache.shutil.disk_usage",
            side_effect=lambda _p: next(fake_iter),
        ),
    ):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )

    assert stats.deleted_pressure == 1
    assert stats.pressure_unrecoverable is False
    assert not any("unrecoverable" in r.message for r in caplog.records), caplog.text


def test_run_gc_sanitizes_keep_job_key(tmp_path: Path) -> None:
    """The key is sanitized like when creating the dir; otherwise the exclusion misses."""
    cache_root = tmp_path / "cache"
    raw_key = "a/b c"
    active = ensure_task_cache_dir(cache_root, raw_key)
    _make_old(active, days=30)

    fake = _FakeUsage(1_000_000, 200_000, 800_000)
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=lambda: raw_key,
        )

    assert active.exists()
    assert stats.deleted_age == 0


def test_run_gc_rejects_a_snapshot_keep_job_key(tmp_path: Path) -> None:
    """The unsafe form -- a ``str`` captured once -- must not be representable.

    A ``str`` freezes the exclusion at the instant the pass started, and a
    pass lasts minutes (each deletion is an ``rglob`` plus a multi-GB
    ``rmtree``): the job claimed halfway through is left unprotected again,
    which is exactly the bug this exclusion prevents. Narrowing the type hint is not
    enough because CI does not run mypy, so the contract is enforced at
    runtime and fails early, before anything has been deleted.
    """
    cache_root = tmp_path / "cache"
    ensure_task_cache_dir(cache_root, "job-active")

    with pytest.raises(TypeError, match="keep_job_key"):
        run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key="job-active",  # type: ignore[arg-type]
        )


def test_run_gc_without_keep_job_key_keeps_previous_behaviour(tmp_path: Path) -> None:
    """Idle worker (``keep_job_key=None``): the GC keeps sweeping everything old."""
    cache_root = tmp_path / "cache"
    old = ensure_task_cache_dir(cache_root, "job-old")
    _make_old(old, days=30)

    fake = _FakeUsage(1_000_000, 200_000, 800_000)
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )

    assert not old.exists()
    assert stats.deleted_age == 1
    assert stats.skipped_active == 0
    assert stats.pressure_unrecoverable is False


def _live_key_once_a_dir_is_gone(*dirs: Path):
    """Getter that simulates a job claimed halfway through a pass.

    Returns ``None`` while the dirs are all still on disk and, as soon as the
    pass has deleted one, declares the remaining one live. Independent of the order of
    ``iterdir``: whichever one the GC processes first, the other becomes
    the live job before its turn comes.
    """

    def _getter() -> str | None:
        remaining = [p for p in dirs if p.exists()]
        if len(remaining) == len(dirs) or not remaining:
            return None
        return remaining[0].name

    return _getter


def test_run_gc_rereads_live_job_during_age_pass(tmp_path: Path) -> None:
    """The exclusion cannot be a snapshot: the live job changes during the pass."""
    cache_root = tmp_path / "cache"
    first = ensure_task_cache_dir(cache_root, "job-a")
    second = ensure_task_cache_dir(cache_root, "job-b")
    _make_old(first, days=30)
    _make_old(second, days=30)

    fake = _FakeUsage(1_000_000, 200_000, 800_000)  # no pressure
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=_live_key_once_a_dir_is_gone(first, second),
        )

    survivors = [p for p in (first, second) if p.exists()]
    assert len(survivors) == 1, "the GC deleted the dir of the job that started mid-pass"
    assert stats.deleted_age == 1
    assert stats.skipped_active == 1


def test_run_gc_rereads_live_job_during_pressure_pass(tmp_path: Path) -> None:
    """The long case: each ``_delete_dir`` is minutes of ``rmtree``.

    The dir of the job that starts mid-pass is also the last in the LRU
    (fresh mtime), i.e. the one whose turn takes longest to come.
    """
    cache_root = tmp_path / "cache"
    first = ensure_task_cache_dir(cache_root, "job-a")
    second = ensure_task_cache_dir(cache_root, "job-b")

    fake = _FakeUsage(1000, 990, 10)  # free/total = 0.01 -> sustained pressure
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=_live_key_once_a_dir_is_gone(first, second),
        )

    survivors = [p for p in (first, second) if p.exists()]
    assert len(survivors) == 1, "the GC deleted the dir of the job that started under pressure"
    assert stats.deleted_pressure == 1
    assert stats.skipped_active == 1


def test_run_gc_does_not_warn_when_final_disk_usage_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A final ``disk_usage`` that fails must not turn into a false alarm.

    The fallback leaves ``free_after = free_before``, which is below the floor by
    construction: without a guard, every pass under pressure would report unrecoverable.
    """
    cache_root = tmp_path / "cache"
    stale = ensure_task_cache_dir(cache_root, "job-stale")
    _make_old(stale, days=30)

    usages: list[object] = [
        _FakeUsage(1000, 990, 10),  # free_before: 0.01 < 0.05 -> triggers pressure
        OSError("degraded disk"),  # free_after: the final measurement fails
    ]
    it = iter(usages)

    def _next(_path: Path) -> _FakeUsage:
        value = next(it)
        if isinstance(value, OSError):
            raise value
        assert isinstance(value, _FakeUsage)
        return value

    with (
        caplog.at_level(logging.WARNING, logger="orcaster.worker.task_cache"),
        patch("orcaster_worker.task_cache.shutil.disk_usage", side_effect=_next),
    ):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )

    assert stats.pressure_unrecoverable is False
    assert not any("unrecoverable" in r.message for r in caplog.records), caplog.text


def test_run_gc_warns_only_on_the_transition(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With the pressure already warned about, later ticks stay quiet (288/day otherwise)."""
    cache_root = tmp_path / "cache"
    ensure_task_cache_dir(cache_root, "job-active")

    fake = _FakeUsage(1000, 990, 10)
    with (
        caplog.at_level(logging.WARNING, logger="orcaster.worker.task_cache"),
        patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake),
    ):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=lambda: "job-active",
            previously_unrecoverable=True,
        )

    assert stats.pressure_unrecoverable is True
    assert not any("unrecoverable" in r.message for r in caplog.records), caplog.text


def test_run_gc_logs_when_pressure_recovers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Closing the warning matters as much as opening it: the operator needs the all-clear."""
    cache_root = tmp_path / "cache"
    ensure_task_cache_dir(cache_root, "job-active")

    fake = _FakeUsage(1000, 100, 900)  # 0.90 free -> no pressure
    with (
        caplog.at_level(logging.INFO, logger="orcaster.worker.task_cache"),
        patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake),
    ):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=lambda: "job-active",
            previously_unrecoverable=True,
        )

    assert stats.pressure_unrecoverable is False
    assert any("disk pressure recovered" in r.message for r in caplog.records), caplog.text


def test_run_gc_does_not_announce_recovery_without_a_final_measurement(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The all-clear requires the same evidence as the warning.

    If the final measurement fails, we do not know whether the pressure eased: declaring it
    recovered would be the same false signal in reverse.
    """
    cache_root = tmp_path / "cache"
    ensure_task_cache_dir(cache_root, "job-active")

    usages: list[object] = [
        _FakeUsage(1000, 990, 10),  # free_before: below the floor
        _FakeUsage(1000, 990, 10),  # the LRU re-measures before looking at the live job
        OSError("degraded disk"),  # free_after: the final measurement fails
    ]
    it = iter(usages)

    def _next(_path: Path) -> _FakeUsage:
        value = next(it)
        if isinstance(value, OSError):
            raise value
        assert isinstance(value, _FakeUsage)
        return value

    with (
        caplog.at_level(logging.INFO, logger="orcaster.worker.task_cache"),
        patch("orcaster_worker.task_cache.shutil.disk_usage", side_effect=_next),
    ):
        run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=lambda: "job-active",
            previously_unrecoverable=True,
        )

    assert not any("recovered" in r.message for r in caplog.records), caplog.text


def test_run_gc_does_not_announce_recovery_without_an_initial_measurement(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The all-clear also requires that the tick was able to *evaluate* the pressure.

    If the initial ``disk_usage`` fails, ``total`` stays at 0 and the pressure is not even
    evaluated: ``pressure_unrecoverable`` is False for lack of a
    measurement, not because the disk recovered. Without this guard the all-clear
    fires with the volume still full, which is the same false signal that the
    final-measurement guard prevents from the other side.
    """
    cache_root = tmp_path / "cache"
    ensure_task_cache_dir(cache_root, "job-active")

    usages: list[object] = [
        OSError("degraded disk"),  # free_before: the initial measurement fails
        _FakeUsage(1000, 990, 10),  # free_after: measures fine, and is still below the floor
    ]
    it = iter(usages)

    def _next(_path: Path) -> _FakeUsage:
        value = next(it)
        if isinstance(value, OSError):
            raise value
        assert isinstance(value, _FakeUsage)
        return value

    with (
        caplog.at_level(logging.INFO, logger="orcaster.worker.task_cache"),
        patch("orcaster_worker.task_cache.shutil.disk_usage", side_effect=_next),
    ):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=lambda: "job-active",
            previously_unrecoverable=True,
        )

    assert stats.pressure_unrecoverable is False
    assert not any("recovered" in r.message for r in caplog.records), caplog.text


def test_run_gc_keeps_a_dir_live_during_the_age_pass_as_a_pressure_candidate(
    tmp_path: Path,
) -> None:
    """The live job is excluded from deletion, not from the set of candidates.

    If the age pass removes it from ``survivors``, a job that finishes between the
    two passes leaves its dir out of the LRU for the whole tick: the GC declares the
    pressure unrecoverable while having a multi-GB candidate available, and the disk
    stays below the floor for an entire ``cache_gc_interval_seconds``.
    """
    cache_root = tmp_path / "cache"
    job_dir = ensure_task_cache_dir(cache_root, "job-a")
    (job_dir / "blob").write_bytes(b"x" * 64)

    calls = {"n": 0}

    def _live_key() -> str | None:
        # Live when the age pass looks at it; dead by the time the pressure
        # pass re-reads it.
        calls["n"] += 1
        return "job-a" if calls["n"] == 1 else None

    fake = _FakeUsage(1000, 990, 10)  # 0.01 < 0.05 -> sustained pressure
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=_live_key,
        )

    assert stats.deleted_pressure == 1
    assert not job_dir.exists()
    assert stats.skipped_active == 0


# ---------------------------------------------------------------------------
# _dir_size_estimate does not count the same byte twice
# ---------------------------------------------------------------------------


def test_dir_size_estimate_counts_symlinked_file_once(tmp_path: Path) -> None:
    """A layout with symlinks (``snapshots/<rev>/data.bin`` -> ``blobs/<sha>``) is a
    symlink to the same byte on disk: counting it twice inflates ``bytes_freed`` to
    ~2x the real size of the tree."""
    from orcaster_worker.task_cache import _dir_size_estimate

    root = tmp_path / "store"
    blobs = root / "blobs"
    snapshots = root / "snapshots" / "rev1"
    blobs.mkdir(parents=True)
    snapshots.mkdir(parents=True)
    blob = blobs / "sha256-deadbeef"
    blob.write_bytes(b"x" * 1024)
    (snapshots / "data.bin").symlink_to(blob)

    assert _dir_size_estimate(root) == 1024


# ---------------------------------------------------------------------------
# Detach-before-delete: the in-flight deletion does not touch the live namespace
# ---------------------------------------------------------------------------


def _claiming_rmtree(cache_root: Path, key: str, marker: str):
    """``rmtree`` that simulates a claim of job ``key`` halfway through the deletion.

    The case: the GC chose the dir of a job that was not live at that moment,
    and the ``rmtree`` (minutes over a multi-GB tree) is still in flight
    when the worker claims that same job and starts downloading into it.
    """
    real_rmtree = shutil.rmtree
    calls: list[Path] = []

    def fake(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(Path(path))
        if len(calls) == 1:
            fresh = ensure_task_cache_dir(cache_root, key)
            (fresh / marker).write_text("download of the claimed task")
        return real_rmtree(path, *args, **kwargs)

    return fake, calls


def test_run_gc_detaches_the_victim_before_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The active-job guard protects the deletion that has not started; this is the one already
    in flight. With the dir out of the namespace before the ``rmtree``, the job that
    claims that key gets a fresh dir that the deletion can no longer reach."""
    cache_root = tmp_path / "cache"
    victim = ensure_task_cache_dir(cache_root, "job-b")
    (victim / "old.bin").write_text("garbage from the previous attempt")
    _make_old(victim, days=30)

    fake_rmtree, calls = _claiming_rmtree(cache_root, "job-b", "download.bin")
    monkeypatch.setattr("orcaster_worker.task_cache.shutil.rmtree", fake_rmtree)

    fake = _FakeUsage(1_000_000, 200_000, 800_000)  # no pressure: age pass only
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=None,
        )

    assert stats.deleted_age == 1
    assert calls, "the GC deleted nothing; the test did not prove what it claims to prove"
    fresh = cache_root / "tasks" / "job-b"
    assert (fresh / "download.bin").exists(), (
        "the in-flight rmtree took the file the claimed task had just written"
    )
    assert not (fresh / "old.bin").exists(), "the old dir had to disappear from the namespace"


def test_run_gc_detaches_the_victim_under_pressure_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same protection in the pressure pass, where the LRU picks the victim."""
    cache_root = tmp_path / "cache"
    victim = ensure_task_cache_dir(cache_root, "job-b")
    (victim / "old.bin").write_text("garbage")

    fake_rmtree, calls = _claiming_rmtree(cache_root, "job-b", "download.bin")
    monkeypatch.setattr("orcaster_worker.task_cache.shutil.rmtree", fake_rmtree)

    fake = _FakeUsage(1000, 990, 10)  # free/total = 0.01 -> sustained pressure
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
            keep_job_key=None,
        )

    assert stats.deleted_pressure == 1
    assert calls, "the GC deleted nothing; the test did not prove what it claims to prove"
    assert (cache_root / "tasks" / "job-b" / "download.bin").exists()


def test_run_gc_leaves_no_trash_behind_after_a_clean_pass(tmp_path: Path) -> None:
    """The detach is an intermediate step, not an accumulation: the ``rmtree`` runs
    just as synchronously and the detach area is empty when the pass ends."""
    cache_root = tmp_path / "cache"
    victim = ensure_task_cache_dir(cache_root, "old-job")
    (victim / "f.bin").write_text("x" * 1024)
    _make_old(victim, days=30)

    fake = _FakeUsage(1_000_000, 200_000, 800_000)
    with patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )

    assert stats.deleted_age == 1
    assert stats.bytes_freed_estimate >= 1024, "the size is estimated on the already detached dir"
    trash = cache_root / "trash"
    assert not trash.exists() or list(trash.iterdir()) == []


def test_run_gc_falls_back_to_in_place_delete_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If the rename is not possible (different FS, permissions), the deletion still
    happens in place as before: losing the protection is preferable to
    no longer reclaiming disk."""
    cache_root = tmp_path / "cache"
    victim = ensure_task_cache_dir(cache_root, "old-job")
    _make_old(victim, days=30)

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("EXDEV: cross-device link")

    monkeypatch.setattr("orcaster_worker.task_cache.os.replace", boom)

    fake = _FakeUsage(1_000_000, 200_000, 800_000)
    with (
        caplog.at_level(logging.WARNING, logger="orcaster.worker.task_cache"),
        patch("orcaster_worker.task_cache.shutil.disk_usage", return_value=fake),
    ):
        stats = run_gc(
            cache_root,
            max_age_days=14,
            min_free_fraction=0.05,
            recover_free_fraction=0.15,
        )

    assert stats.deleted_age == 1
    assert not victim.exists()
    assert any("could not detach" in r.getMessage() for r in caplog.records), (
        "losing the detach protection must be stated in the log"
    )


def test_detach_does_not_warn_when_the_tree_already_vanished(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A tree that is already gone is not a detach failure, and warning about it is noise.

    The real case: the GC enumerates candidates under ``tasks/`` and, before reaching
    one, ``flush_caches`` detaches the whole ``tasks/`` (it runs in parallel, in
    another thread). The GC's ``os.replace`` fails with ``FileNotFoundError``, which
    is an ``OSError``, and the fallback WARNING said the deletion was left
    "unprotected" when in fact there was nothing to protect. A
    WARNING that shows up in a benign race trains the operator to ignore the
    one that does matter (EXDEV, permissions: there the protection is really lost).
    """
    cache_root = tmp_path / "cache"
    (cache_root / "tasks").mkdir(parents=True)
    ghost = cache_root / "tasks" / "job-already-gone"

    with caplog.at_level(logging.DEBUG, logger="orcaster.worker.task_cache"):
        detached = _detach_for_delete(ghost, cache_root)

    assert detached == ghost, "with no tree there is nothing to detach; the rmtree will be a no-op"
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a benign race with the flush must not be logged as a loss of protection"
    )
    trash = cache_root / "trash"
    assert not trash.exists() or list(trash.iterdir()) == [], (
        "the holder reserved for the rename must be left clean"
    )


def test_reset_other_job_caches_deletes_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-job reset does NOT use the detach: it runs before the job starts (there is
    no concurrent deletion to protect) and its contract is to reclaim disk
    synchronously, as well as to detect the dir that could not be deleted."""
    cache_root = tmp_path / "cache"
    other = ensure_task_cache_dir(cache_root, "job-foreign")
    ensure_task_cache_dir(cache_root, "live-job")

    monkeypatch.setattr(
        "orcaster_worker.task_cache.shutil.rmtree",
        lambda *a, **kw: None,  # the deletion "fails" without raising
    )
    stats = reset_other_job_caches(cache_root, keep_job_key="live-job")

    assert stats.dirs_deleted == 0
    assert stats.failed_dirs == (other,), (
        "the reset must keep reporting the dir that persists; a rename would make it "
        "disappear from the namespace and the failure would go unnoticed"
    )


def test_sweep_trash_dirs_deletes_residual_detached_trees(tmp_path: Path) -> None:
    """A crash halfway through the ``rmtree`` leaves the detached tree on disk: nobody
    else will look at it, so it is swept at startup (symmetric to ``run/``)."""
    cache_root = tmp_path / "cache"
    holder = cache_root / "trash" / "gone-abc"
    (holder / "old-job").mkdir(parents=True)
    (holder / "old-job" / "f.bin").write_text("x" * 10)

    assert sweep_trash_dirs(cache_root) == 1
    assert not holder.exists()
    assert (cache_root / "trash").is_dir()


def test_sweep_trash_dirs_noop_when_absent(tmp_path: Path) -> None:
    assert sweep_trash_dirs(tmp_path / "nothing") == 0


def test_sweep_trash_dirs_leaves_what_the_worker_did_not_create(tmp_path: Path) -> None:
    """``base`` is not always a orcaster tree: the worker-wide uv cache is
    detached in the parent of ``ORCH_ORCASTER_UV_CACHE_DIR``, which the operator
    chooses. A pre-existing ``<that_parent>/trash`` with foreign content cannot
    be deleted by the worker's startup."""
    base = tmp_path / "operator-volume"
    trash = base / "trash"
    foreign = trash / "operator-backups"
    foreign.mkdir(parents=True)
    (foreign / "important.tar").write_text("do not delete me")
    ours = trash / "gone-abc123"
    (ours / "old-job").mkdir(parents=True)

    assert sweep_trash_dirs(base) == 1
    assert not ours.exists()
    assert (foreign / "important.tar").exists(), "the sweep deleted foreign data"
