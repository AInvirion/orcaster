"""Host telemetry visible to the worker.

RAM and CPU are measured from the container's cgroup v2 when it is readable
(Linux); otherwise from the host via psutil. Disk is measured on the cache
volume (`cache_root`, default `/var/cache/orcaster`): it is where tasks download
very large files, the real storage bottleneck.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import psutil

_MB = 1024 * 1024

# Container cgroup v2. In Podman/Docker with a private cgroupns, these paths
# reflect the container, not the host. Replaceable in tests.
_CGROUP_CPU_STAT = Path("/sys/fs/cgroup/cpu.stat")
_CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")  # container quota ('<quota> <period>' in µs)
_CGROUP_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")


class RamSnapshot(NamedTuple):
    total_mb: int
    used_mb: int


class DiskSnapshot(NamedTuple):
    """Poll disk telemetry, plus the free fraction for the claim gate.

    ``free_fraction`` is ``free / total`` computed **in bytes** in the same
    measurement, not derived from ``total_mb - used_mb``: ``used`` excludes the
    reserved blocks (5% on ext4 by default), so ``used + free != total`` and the
    subtraction would give a fraction that disagrees with the GC criterion right
    near the threshold. It is the same computation ``task_cache.run_gc`` does.
    ``None`` when the ``cache_root`` could not be measured (``total`` 0, a
    filesystem error, or the fallback to ``/`` that saves the telemetry but
    measures another filesystem): the gate does not fire without a measurement.
    """

    total_mb: int
    used_mb: int
    free_fraction: float | None = None


def detect_ram() -> RamSnapshot:
    """Total and used RAM, per container when cgroup v2 is available.

    `used_mb`: the cgroup `memory.current` (bytes → MB) if readable; otherwise
    the host psutil fallback `total - available` (htop style). `total_mb`:
    `memory.max` if the cgroup imposes a limit (the fleet runs with `--memory`,
    see `WORKER_MEMORY` in the Makefile), or the host RAM. Returning both from a
    single read avoids a double syscall.

    Note when reading telemetry: a `total_mb` equal to the host's physical RAM,
    and not to the cap, means that container started WITHOUT a cap.
    """
    vm = psutil.virtual_memory()
    total_host_mb = vm.total // _MB
    used_cgroup_mb = _read_cgroup_mem_current_mb()
    if used_cgroup_mb is not None:
        max_mb = _read_cgroup_mem_max_mb()
        total_mb = max_mb if max_mb is not None else total_host_mb
        return RamSnapshot(total_mb=total_mb, used_mb=used_cgroup_mb)
    used_mb = max(0, (vm.total - vm.available)) // _MB
    return RamSnapshot(total_mb=total_host_mb, used_mb=used_mb)


def detect_total_ram_mb() -> int:
    """Shortcut for `register`, which only needs the total."""
    return detect_ram().total_mb


def cgroup_memory_cap_missing() -> bool:
    """True only if we run in a readable cgroup v2 that imposes NO limit.

    It is the same discrimination `detect_ram` makes: a readable
    `memory.current` means "we are in a container"; `memory.max` missing or
    `max` means "no cap". Outside a container (native dev, tests, macOS) there
    is no cgroup to read and the absence of a cap is not a defect — hence False.

    The worker startup uses it to warn about what used to be silent: a container
    without `--memory` lets a runaway task trigger the host's OOM killer instead
    of dying as `CONSTRAINT_MEMCG` in its own cgroup.
    """
    if _read_cgroup_mem_current_mb() is None:
        return False
    return _read_cgroup_mem_max_mb() is None


def detect_disk(cache_root: Path) -> DiskSnapshot:
    """Space of the filesystem backing the cache volume (`cache_root`,
    default `/var/cache/orcaster`): it is where tasks download very large
    files, the real bottleneck. It is not per container (the disk is not
    isolated without a quota); it is the relevant disk, not the overlay rootfs.

    If `cache_root` does not exist yet (volume not mounted at startup), it falls
    back to `/` so as not to break register/poll. That fallback feeds the
    telemetry, but **not** `free_fraction`: the claim gate compares the fraction
    against the cache volume floor, and another filesystem's fraction would make
    it decide on a disk that is not the one that matters. Without a measurement
    of `cache_root` there is no fraction, and without a fraction the gate does
    not fire.
    """
    try:
        du = psutil.disk_usage(str(cache_root))
        measured_cache_root = True
    except OSError:
        du = psutil.disk_usage("/")
        measured_cache_root = False
    return DiskSnapshot(
        total_mb=du.total // _MB,
        used_mb=du.used // _MB,
        free_fraction=(du.free / du.total) if (measured_cache_root and du.total > 0) else None,
    )


def detect_total_disk_mb(cache_root: Path) -> int:
    """Shortcut for `register`, which only needs the total."""
    return detect_disk(cache_root).total_mb


def _cgroup_available() -> bool:
    """Readable cgroup v2 on this OS (enables the CPU/RAM cgroup paths)."""
    return sys.platform == "linux" and _CGROUP_CPU_STAT.exists()


def _read_cgroup_cpu_usage_usec() -> int | None:
    """`usage_usec` (accumulated cgroup CPU µs) from `cpu.stat`, or None."""
    try:
        content = _CGROUP_CPU_STAT.read_text()
    except OSError:
        return None
    for line in content.splitlines():
        if line.startswith("usage_usec "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _read_cgroup_cpu_quota() -> float | None:
    """CPUs allowed by the cgroup quota (`cpu.max` = '<quota> <period>', µs per
    period). None if it is 'max' (no quota, the current case) or unreadable."""
    try:
        parts = _CGROUP_CPU_MAX.read_text().split()
    except OSError:
        return None
    if len(parts) != 2:
        return None
    quota_raw, period_raw = parts
    if quota_raw == "max":
        return None
    try:
        quota, period = int(quota_raw), int(period_raw)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def _read_cgroup_mem_current_mb() -> int | None:
    """RAM usage of the cgroup (`memory.current`, bytes → MB), or None."""
    try:
        return int(_CGROUP_MEMORY_CURRENT.read_text().strip()) // _MB
    except (OSError, ValueError):
        return None


def _read_cgroup_mem_max_mb() -> int | None:
    """RAM limit of the cgroup (`memory.max` → MB). None if it is 'max' (no
    limit) or unreadable."""
    try:
        raw = _CGROUP_MEMORY_MAX.read_text().strip()
    except OSError:
        return None
    if raw == "max":
        return None
    try:
        return int(raw) // _MB
    except ValueError:
        return None


def _clamp_pct(pct: float) -> int:
    """Round and clamp to 0..100 (integer)."""
    return max(0, min(100, int(round(pct))))


class CpuSampler:
    """Sample the CPU% attributable to THIS worker/container between polls.

    With several workers per host, the global `psutil.cpu_percent()` (reads
    /proc/stat = host) is useless: all the cards would show the same number. The
    container cgroup is measured; fallback to the worker's process tree.

    - Linux + cgroup v2: delta `usage_usec` / wall-time = CPUs used, normalized
      ÷ cgroup quota (`cpu.max`) × 100 if the container runs with `--cpus`; if
      there is no quota (`cpu.max = "max"`), ÷ `os.cpu_count()`. This is what
      `podman stats` reports.
    - Fallback (FreeBSD/macOS or unreadable cgroup): sum of
      `cpu_percent(interval=None)` of the worker process + descendants, caching
      the `Process` objects by pid so the delta is valid. Always normalizes by
      `os.cpu_count()`.

    The FIRST sample returns None (a two-point delta is needed); from the second
    on there is a value. Instantiate once at worker startup.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._use_cgroup = _cgroup_available()
        # Normalization divisor: the cgroup quota (cpu.max, allowed CPUs) if the
        # container runs with --cpus → cpu_pct is "% of its own quota"; if there
        # is no quota (the current case, cpu.max = "max") or it is not Linux, use
        # os.cpu_count() → cpu_pct is "% of the host". May be a float (quota).
        if self._use_cgroup:
            quota = _read_cgroup_cpu_quota()
            self._ncpu: float = quota if quota is not None else (os.cpu_count() or 1)
        else:
            self._ncpu = os.cpu_count() or 1
        self._prev_usage_usec: int | None = None
        self._prev_time: float | None = None
        self._procs: dict[int, psutil.Process] = {}

    def sample(self) -> int | None:
        if self._use_cgroup:
            return self._sample_cgroup()
        return self._sample_psutil()

    def _sample_cgroup(self) -> int | None:
        usage = _read_cgroup_cpu_usage_usec()
        if usage is None:
            # Transient read failure: keep the previous baseline instead of
            # overwriting it with None. The next valid read computes the delta
            # over the longer interval (cpu.stat is cumulative).
            return None
        now = self._clock()
        prev_usage, prev_t = self._prev_usage_usec, self._prev_time
        self._prev_usage_usec, self._prev_time = usage, now
        if prev_usage is None or prev_t is None:
            return None
        dt_wall_usec = (now - prev_t) * 1_000_000
        if dt_wall_usec <= 0:
            return None
        cpus_used = (usage - prev_usage) / dt_wall_usec
        return _clamp_pct((cpus_used / self._ncpu) * 100.0)

    def _sample_psutil(self) -> int | None:
        first_call = not self._procs
        try:
            me = psutil.Process(os.getpid())
            procs = [me, *me.children(recursive=True)]
        except psutil.NoSuchProcess:
            return None
        total = 0.0
        new_cache: dict[int, psutil.Process] = {}
        for p in procs:
            try:
                cached = self._procs.get(p.pid, p)
                total += cached.cpu_percent(interval=None)
                new_cache[p.pid] = cached
            except psutil.NoSuchProcess:
                continue
        self._procs = new_cache
        if first_call:
            # cpu_percent(interval=None) on the first call per process returns
            # 0.0 and sets the baseline: the real value arrives on the 2nd.
            return None
        return _clamp_pct(total / self._ncpu)
