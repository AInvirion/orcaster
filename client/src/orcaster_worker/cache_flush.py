"""Forced cleanup of the worker disk cache (on-demand).

Symmetric to ``task_cache.run_gc`` but without an age/pressure policy: it
deletes ALL the scratch (``<cache_root>/tasks/*``) and the UV cache. Pure,
testable logic; filesystem errors are best-effort (not propagated). Idempotent.

``shutil.rmtree`` is preferred over invoking ``uv cache clean`` so as not to
depend on the ``uv`` binary and to keep the function testable; ``uv`` recreates
its cache on the next install.

The freed bytes (``FlushStats``) are DEBUG-only telemetry: computing them
requires walking the whole cache tree with ``rglob`` + ``stat`` right before
deleting it (double I/O over a large UV cache). That is why the walk only runs
when the module logger has DEBUG enabled. The real signal of freed space is the
``disk_used_mb`` the worker reports on every poll, not these counters; with
INFO the fields stay at 0.

Detach before deleting: ``_run_cache_flush`` bounds the flush with a timeout
and, when it expires, **abandons the OS thread**: the ``rmtree`` keeps going in
the background while the worker accepts tasks again. That is why both
``tasks/`` and the uv cache leave the namespace through a rename before being
deleted: the orphaned thread operates on an unreachable tree and the new job
writes into a freshly created ``tasks/``. The flush semantics do not change
(the flush still takes EVERYTHING); only the moment the tree stops being
visible changes, which becomes immediate.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from orcaster_worker.task_cache import _detach_for_delete, _dir_size_estimate, _tasks_root

log = logging.getLogger("orcaster.worker.cache_flush")


@dataclass(frozen=True)
class FlushStats:
    """Result of a forced cleanup (DEBUG-only telemetry).

    The byte counters are only computed when the module logger has DEBUG
    enabled; at INFO or higher levels they stay at 0 to avoid the full
    filesystem walk (see the module docstring).
    The real signal of freed space is the poll's ``disk_used_mb``.
    """

    tasks_bytes_freed: int
    uv_bytes_freed: int


def _dir_size(path: Path) -> int:
    """Bytes of the subtree, reusing the GC walk.

    This used to be a copy of the ``task_cache`` helper and fell behind when
    that one was fixed to not follow symlinks: a cache that publishes each file
    as a symlink to ``blobs/<sha>`` made the old copy count the same byte twice,
    so ``FlushStats.tasks_bytes_freed`` reported ~2x the real size,
    contradicting the GC log of the same worker.
    """
    return _dir_size_estimate(path)


def flush_caches(cache_root: Path, uv_cache_dir: Path | None) -> FlushStats:
    """Delete ALL the scratch (<cache_root>/tasks/*) and the uv cache. Idempotent.

    The freed-bytes calculation is DEBUG-only telemetry: it only walks the tree
    with ``_dir_size`` when the logger has DEBUG enabled. At INFO the
    ``FlushStats`` fields stay at 0 and the double I/O is avoided. The wipe and
    the recreation of ``tasks_root`` always happen, regardless of the log level.
    The real signal of freed space is the ``disk_used_mb`` the worker reports on
    every poll.

    Both trees are detached before being deleted (see the module docstring), so
    the deletion, and the byte walk that comes after the rename, no longer run
    over paths a new job may be using. A pathological case remains out of
    scope: if the ``os.replace`` itself hangs past the flush timeout (NFS
    hard-mount), the orphaned thread may rename a ``tasks/`` that already has a
    new job inside. It is a syscall hung for minutes; this is accepted.
    """
    want_bytes = log.isEnabledFor(logging.DEBUG)

    tasks_root = _tasks_root(cache_root)
    tasks_freed = 0
    if tasks_root.exists():
        # Detach first: the rename takes the tree out of the namespace
        # immediately, so the `mkdir` below already leaves a clean, usable
        # `tasks/` even if the deletion (or the byte walk) takes minutes.
        detached = _detach_for_delete(tasks_root, cache_root)
        if want_bytes:
            tasks_freed = _dir_size(detached)
        shutil.rmtree(detached, ignore_errors=True)
        if detached != tasks_root:
            with contextlib.suppress(OSError):
                detached.parent.rmdir()
    try:
        tasks_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Best-effort: a forced cleanup must not propagate filesystem errors.
        log.warning("cache_flush: could not recreate %s", tasks_root, exc_info=True)

    uv_freed = 0
    if uv_cache_dir is not None and uv_cache_dir.exists():
        # The worker-wide uv cache (`ORCH_ORCASTER_UV_CACHE_DIR`) may live on a
        # different filesystem than `cache_root`, where the rename would give
        # EXDEV: its detach area is a sibling of its own, not the one under
        # `cache_root`. If they coincide (uv under `cache_root`, the per-job
        # default), it is the same `trash/`.
        detached_uv = _detach_for_delete(uv_cache_dir, uv_cache_dir.parent)
        if want_bytes:
            uv_freed = _dir_size(detached_uv)
        shutil.rmtree(detached_uv, ignore_errors=True)
        if detached_uv != uv_cache_dir:
            with contextlib.suppress(OSError):
                detached_uv.parent.rmdir()

    stats = FlushStats(tasks_bytes_freed=tasks_freed, uv_bytes_freed=uv_freed)
    if want_bytes:
        log.debug(
            "cache_flush: tasks_bytes_freed~=%d uv_bytes_freed~=%d",
            stats.tasks_bytes_freed,
            stats.uv_bytes_freed,
        )
    else:
        log.info("cache_flush: flush completed (enable DEBUG for freed bytes)")
    return stats
