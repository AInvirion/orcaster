from __future__ import annotations

from pathlib import Path

from orcaster_worker import sysinfo


def test_read_cgroup_cpu_usage_usec_parses(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "cpu.stat"
    f.write_text("usage_usec 123456789\nuser_usec 100\nsystem_usec 50\n")
    monkeypatch.setattr(sysinfo, "_CGROUP_CPU_STAT", f)
    assert sysinfo._read_cgroup_cpu_usage_usec() == 123456789


def test_read_cgroup_cpu_usage_usec_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sysinfo, "_CGROUP_CPU_STAT", tmp_path / "nope.stat")
    assert sysinfo._read_cgroup_cpu_usage_usec() is None


def test_read_cgroup_mem_current_mb(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "memory.current"
    f.write_text(str(512 * 1024 * 1024))  # 512 MB in bytes
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_CURRENT", f)
    assert sysinfo._read_cgroup_mem_current_mb() == 512


def test_read_cgroup_mem_max_unlimited(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "memory.max"
    f.write_text("max")
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_MAX", f)
    assert sysinfo._read_cgroup_mem_max_mb() is None


def test_read_cgroup_mem_max_limited(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "memory.max"
    f.write_text(str(2048 * 1024 * 1024))  # 2048 MB
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_MAX", f)
    assert sysinfo._read_cgroup_mem_max_mb() == 2048


def test_detect_ram_uses_cgroup_used_when_available(tmp_path: Path, monkeypatch) -> None:
    cur = tmp_path / "memory.current"
    cur.write_text(str(700 * 1024 * 1024))  # 700 MB used
    mx = tmp_path / "memory.max"
    mx.write_text("max")  # no limit -> total = host
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_CURRENT", cur)
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_MAX", mx)
    snap = sysinfo.detect_ram()
    assert snap.used_mb == 700
    assert snap.total_mb > 0  # host total (psutil), we do not validate the exact number


def test_detect_ram_falls_back_to_psutil(tmp_path: Path, monkeypatch) -> None:
    # cgroup not readable -> host psutil behavior (total - available).
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_CURRENT", tmp_path / "nope")
    snap = sysinfo.detect_ram()
    assert snap.total_mb > 0
    assert 0 <= snap.used_mb <= snap.total_mb


def test_detect_disk_measures_given_path(tmp_path: Path) -> None:
    snap = sysinfo.detect_disk(tmp_path)
    assert snap.total_mb > 0
    assert 0 <= snap.used_mb <= snap.total_mb


def test_detect_disk_falls_back_when_path_missing(tmp_path: Path) -> None:
    # A nonexistent path falls back to "/" so as not to break register/poll.
    snap = sysinfo.detect_disk(tmp_path / "does-not-exist")
    assert snap.total_mb > 0


def test_cpu_sampler_cgroup_first_sample_is_none(monkeypatch) -> None:
    monkeypatch.setattr(sysinfo, "_cgroup_available", lambda: True)
    usages = iter([1_000_000, 2_000_000])
    monkeypatch.setattr(sysinfo, "_read_cgroup_cpu_usage_usec", lambda: next(usages))
    clock = iter([10.0, 11.0])  # 1 s of wall-time between samples
    monkeypatch.setattr(sysinfo, "os", sysinfo.os)  # no-op: documents the dependency
    sampler = sysinfo.CpuSampler(clock=lambda: next(clock))
    # We force nCPUs=1 for a deterministic calculation.
    sampler._ncpu = 1
    assert sampler.sample() is None  # first sample: no delta


def test_cpu_sampler_cgroup_second_sample_computes_pct(monkeypatch) -> None:
    monkeypatch.setattr(sysinfo, "_cgroup_available", lambda: True)
    # +1_000_000 usec of CPU in 1 s of wall = 1.0 CPU used.
    usages = iter([1_000_000, 2_000_000])
    monkeypatch.setattr(sysinfo, "_read_cgroup_cpu_usage_usec", lambda: next(usages))
    clock = iter([10.0, 11.0])
    sampler = sysinfo.CpuSampler(clock=lambda: next(clock))
    sampler._ncpu = 1
    assert sampler.sample() is None
    # 1 CPU used / 1 nCPU * 100 = 100 %.
    assert sampler.sample() == 100


def test_cpu_sampler_cgroup_normalizes_by_ncpu(monkeypatch) -> None:
    monkeypatch.setattr(sysinfo, "_cgroup_available", lambda: True)
    usages = iter([0, 2_000_000])  # 2.0 CPU-seconds in 1 s wall = 2 CPUs
    monkeypatch.setattr(sysinfo, "_read_cgroup_cpu_usage_usec", lambda: next(usages))
    clock = iter([0.0, 1.0])
    sampler = sysinfo.CpuSampler(clock=lambda: next(clock))
    sampler._ncpu = 4  # 2 CPUs / 4 * 100 = 50 %
    assert sampler.sample() is None
    assert sampler.sample() == 50


def test_cpu_sampler_psutil_fallback_first_sample_none(monkeypatch) -> None:
    monkeypatch.setattr(sysinfo, "_cgroup_available", lambda: False)
    sampler = sysinfo.CpuSampler()
    # On the psutil path, the first sample is always None (baseline).
    assert sampler.sample() is None


def test_read_cgroup_cpu_quota_parses(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "cpu.max"
    f.write_text("200000 100000")  # 2.0 CPUs allowed
    monkeypatch.setattr(sysinfo, "_CGROUP_CPU_MAX", f)
    assert sysinfo._read_cgroup_cpu_quota() == 2.0


def test_read_cgroup_cpu_quota_unlimited(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "cpu.max"
    f.write_text("max 100000")  # no quota -> None
    monkeypatch.setattr(sysinfo, "_CGROUP_CPU_MAX", f)
    assert sysinfo._read_cgroup_cpu_quota() is None


def test_read_cgroup_cpu_quota_malformed(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "cpu.max"
    f.write_text("abc")  # invalid content -> None
    monkeypatch.setattr(sysinfo, "_CGROUP_CPU_MAX", f)
    assert sysinfo._read_cgroup_cpu_quota() is None


def test_cpu_sampler_normalizes_by_cgroup_quota(monkeypatch) -> None:
    # __init__ must take the quota (2.0) from the cgroup instead of os.cpu_count().
    # 2.0 CPU-seconds in 1 s wall = 2.0 CPUs used / 2.0 quota * 100 = 100%.
    monkeypatch.setattr(sysinfo, "_cgroup_available", lambda: True)
    monkeypatch.setattr(sysinfo, "_read_cgroup_cpu_quota", lambda: 2.0)
    usages = iter([0, 2_000_000])
    monkeypatch.setattr(sysinfo, "_read_cgroup_cpu_usage_usec", lambda: next(usages))
    clock = iter([0.0, 1.0])
    sampler = sysinfo.CpuSampler(clock=lambda: next(clock))
    # _ncpu must have been set to 2.0 by __init__ (without a manual set).
    assert sampler._ncpu == 2.0
    assert sampler.sample() is None  # first sample: no delta
    assert sampler.sample() == 100  # 2.0 CPUs / 2.0 quota * 100 = 100%


def test_cpu_sampler_cgroup_transient_failure_preserves_baseline(monkeypatch) -> None:
    # Sequence: [1_000_000, None, 3_000_000], clock: [10.0, 11.0, 12.0], nCPU=1.
    # Sample 1 -> None (first baseline, no previous delta).
    # Sample 2 -> None (failed read; the previous baseline must be preserved).
    # Sample 3 -> delta (3_000_000 - 1_000_000) usec in (12.0 - 10.0) s wall = 1.0 CPU = 100%.
    monkeypatch.setattr(sysinfo, "_cgroup_available", lambda: True)
    usages = iter([1_000_000, None, 3_000_000])
    monkeypatch.setattr(sysinfo, "_read_cgroup_cpu_usage_usec", lambda: next(usages))
    clock = iter([10.0, 11.0, 12.0])
    sampler = sysinfo.CpuSampler(clock=lambda: next(clock))
    sampler._ncpu = 1
    assert sampler.sample() is None  # baseline: no delta
    assert sampler.sample() is None  # transient failure: baseline preserved
    assert sampler.sample() == 100  # delta over 2s of wall-time = 100%


def test_detect_disk_reports_free_fraction(tmp_path: Path) -> None:
    """The claim gate compares against the same fraction as the GC, so
    it comes from the same measurement and not from `total_mb - used_mb`: psutil excludes
    the reserved blocks (5% on ext4), i.e. `used + free != total`."""
    snap = sysinfo.detect_disk(tmp_path)
    assert snap.free_fraction is not None
    assert 0.0 <= snap.free_fraction <= 1.0


def test_detect_disk_free_fraction_matches_shutil(tmp_path: Path) -> None:
    import shutil as _shutil

    snap = sysinfo.detect_disk(tmp_path)
    usage = _shutil.disk_usage(tmp_path)
    assert snap.free_fraction is not None
    # The same computation `run_gc` does; we tolerate noise from concurrent
    # writes on the runner's FS.
    assert abs(snap.free_fraction - usage.free / usage.total) < 0.01


def test_detect_disk_without_measuring_cache_root_gives_no_fraction(
    tmp_path: Path, monkeypatch
) -> None:
    """The fallback to `/` serves telemetry, not the claim gate.

    When the cache volume cannot be measured (not mounted, stale mount), telemetry
    falls back to `/` so as not to break register. But the gate compares that
    fraction against the cache volume's floor, so a fraction from another
    filesystem would make it decide on a disk that is not the one that matters. The
    gate's principle is explicit: no measurement, no gating, and this is not a
    measurement of cache_root. The telemetry MB keep being reported.
    """
    real_disk_usage = sysinfo.psutil.disk_usage

    def fails_only_for_cache_root(path):
        if str(path) == str(tmp_path):
            raise OSError("ESTALE: stale NFS file handle")
        return real_disk_usage(path)

    monkeypatch.setattr(sysinfo.psutil, "disk_usage", fails_only_for_cache_root)

    snap = sysinfo.detect_disk(tmp_path)

    assert snap.free_fraction is None, "the gate cannot decide with another FS's fraction"
    assert snap.total_mb > 0, "telemetry still falls back to `/` so as not to break register"


# --- Container memory cap guard ------------------------------------------
# The `--memory` cap of `podman run` is containment: without it, a
# runaway task triggers the host's OOM killer instead of dying as
# CONSTRAINT_MEMCG inside its cgroup. Its absence used to be silent (it was
# discovered by reading the `ceiling` of the telemetry the task itself reports), so
# the worker detects it and shouts at startup.


def test_cgroup_memory_cap_missing_when_cgroup_has_no_limit(tmp_path: Path, monkeypatch) -> None:
    cur = tmp_path / "memory.current"
    cur.write_text(str(512 * 1024 * 1024))
    mx = tmp_path / "memory.max"
    mx.write_text("max")
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_CURRENT", cur)
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_MAX", mx)
    assert sysinfo.cgroup_memory_cap_missing() is True


def test_cgroup_memory_cap_missing_false_with_cap_set(tmp_path: Path, monkeypatch) -> None:
    cur = tmp_path / "memory.current"
    cur.write_text(str(512 * 1024 * 1024))
    mx = tmp_path / "memory.max"
    mx.write_text(str(30 * 1024 * 1024 * 1024))  # 30 GiB, the fleet's cap
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_CURRENT", cur)
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_MAX", mx)
    assert sysinfo.cgroup_memory_cap_missing() is False


def test_cgroup_memory_cap_missing_false_without_readable_cgroup(
    tmp_path: Path, monkeypatch
) -> None:
    """Outside a container (dev on macOS, tests, native execution) there is no
    cgroup to read, and there the absence of a cap is not a defect: the guard has
    to stay silent instead of shouting on every local startup."""
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_CURRENT", tmp_path / "does-not-exist.current")
    monkeypatch.setattr(sysinfo, "_CGROUP_MEMORY_MAX", tmp_path / "does-not-exist.max")
    assert sysinfo.cgroup_memory_cap_missing() is False
