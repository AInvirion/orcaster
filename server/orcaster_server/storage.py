"""Artifact quota per workspace and physical space floor.

Single layer of measurement and enforcement, consumed by the upload endpoint
(`api/tasks.py`), the poll (`api/poll.py`) and the dashboard (`api/admin_ui.py`).

- Logical quota: aggregate counter in `workspace_artifact_usage`, maintained
  incrementally (upload adds, purge subtracts) and reconciled in housekeeping
  (full pass when the process starts, filtered by recent activity afterwards;
  see `reconcile_workspace_usage`).
- Effective quota: `workspaces.artifact_quota_bytes` if the host set an
  override, otherwise `ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES`. See `workspace_quota`.
- Physical floor: `os.statvfs` on `ARTIFACTS_ROOT`. Fail-open if the syscall
  fails (do not block the queue over a measurement hiccup): returns `_UNLIMITED`.
  The floor is shared by ALL workspaces (same filesystem): it is the safety
  net, not the routine limit. `quota_commitment` measures whether what the
  workspaces can still grow (Σ max(effective quota − usage, 0)) fits in the
  free space above the floor (free − floor), and housekeeping warns if not.

`os.statvfs` is a blocking syscall and the poll used to call it on every request
inside the event loop. Async callers request it through
`disk_free_bytes_async` / `disk_total_bytes_async` (delegation to a thread) and
the poll hot path, in addition, through the TTL + single-flight cache
`disk_free_cache`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from orcaster_server.config import Settings

logger = logging.getLogger(__name__)

# Fail-open sentinel: if the disk cannot be measured, we treat free space as
# unlimited so as not to block delivery/upload over a measurement error. In
# prod ARTIFACTS_ROOT exists (the lifespan creates it at startup via
# ensure_artifacts_root, which refuses to start without it, in addition to
# systemd provisioning).
_UNLIMITED = sys.maxsize

_SQL_WORKSPACE_USAGE = text(
    """
    SELECT COALESCE(total_bytes, 0) AS total_bytes
      FROM workspace_artifact_usage
     WHERE workspace_id = :workspace_id
    """
)

# Workspace quota override. No row (nonexistent workspace) or NULL => global
# default; the caller resolves the COALESCE in Python to also know whether there
# was an override (the indicator shows it).
_SQL_WORKSPACE_QUOTA = text(
    """
    SELECT artifact_quota_bytes
      FROM workspaces
     WHERE id = :workspace_id
    """
)

# Workspace usage and override in a single round trip (poll hot path).
# No counter row => usage 0, same as `_SQL_WORKSPACE_USAGE`.
_SQL_WORKSPACE_STORAGE = text(
    """
    SELECT w.artifact_quota_bytes, COALESCE(u.total_bytes, 0) AS total_bytes
      FROM workspaces w
      LEFT JOIN workspace_artifact_usage u ON u.workspace_id = w.id
     WHERE w.id = :workspace_id
    """
)

# Quota commitment: per workspace, disabled ones included (their artifacts stay
# on disk and the quota applies again if they are re-enabled; counting them is
# the conservative reading), the effective quota (override or the default
# `:default_quota`) and its current usage. The growth headroom is summed per
# workspace with `GREATEST(quota − usage, 0)`: a workspace above its quota cannot
# grow, but its excess bytes are not space in which another workspace can grow
# either. Summing quotas and subtracting the aggregate usage would credit them
# and hide a real over-commit.
_SQL_QUOTA_COMMITMENT = text(
    """
    SELECT count(*) AS workspaces,
           count(w.artifact_quota_bytes) AS overridden,
           COALESCE(SUM(COALESCE(w.artifact_quota_bytes, :default_quota)), 0)
               AS committed,
           COALESCE(SUM(COALESCE(u.total_bytes, 0)), 0) AS usage_sum,
           COALESCE(SUM(GREATEST(
               COALESCE(w.artifact_quota_bytes, :default_quota)
                   - COALESCE(u.total_bytes, 0),
               0
           )), 0) AS headroom
      FROM workspaces w
      LEFT JOIN workspace_artifact_usage u ON u.workspace_id = w.id
    """
)

# UPSERT of the delta. On the initial INSERT it is clamped to >=0 (GREATEST) to
# respect the CHECK; on UPDATE the sum is clamped. Idempotent with delta=0 (no-op).
_SQL_BUMP_WORKSPACE_USAGE = text(
    """
    INSERT INTO workspace_artifact_usage (workspace_id, total_bytes, updated_at)
    VALUES (:workspace_id, GREATEST(0, :delta), now())
    ON CONFLICT (workspace_id)
    DO UPDATE SET
        total_bytes = GREATEST(0, workspace_artifact_usage.total_bytes + :delta),
        updated_at = now()
    """
)

# Recomputes the real SUM per workspace and corrects drift in the incremental
# counter.
#
# `{cte}` / `{scope}` restrict the pass to workspaces with recent activity:
# the full scan grows with the total number of `task_artifacts` rows and
# runs every ~5 min, even though 99% of the workspaces have not changed.
#
# The `WHERE` of the `DO UPDATE` is what makes the filtering possible: without it,
# each pass would bump `updated_at` of every workspace it touched and the next
# pass would see all of them as "recent activity" (the reconcile would flag
# itself). With it, `updated_at` means "last time the counter CHANGED" and a
# reconcile without drift writes nothing (nor does it generate dead tuples).
# Side effect on `rowcount`: it counts rows actually inserted/corrected, not
# workspaces inspected.
#
# `ta.collected_at IS NULL` excludes collected artifacts: their row survives as
# evidence that the task produced the file, but the bytes are NO longer on disk,
# so counting them would leave the quota blocked by freed space. The condition
# goes in the ON of the LEFT JOIN, not in the WHERE: in the WHERE it would turn
# the LEFT JOIN into an INNER one and workspaces without artifacts (or with ALL
# collected) would lose their counter row. Because it lives in the shared
# template, the filter applies equally to the full scan and to the pass filtered
# by `since`.
_RECONCILE_USAGE_TEMPLATE = """
    {cte}
    INSERT INTO workspace_artifact_usage (workspace_id, total_bytes, updated_at)
    SELECT w.id, COALESCE(SUM(ta.size_bytes), 0), now()
      FROM workspaces w
      {scope}
      LEFT JOIN tasks t ON t.workspace_id = w.id
      LEFT JOIN task_executions e ON e.task_id = t.id
      LEFT JOIN task_artifacts ta
             ON ta.execution_id = e.id AND ta.collected_at IS NULL
     GROUP BY w.id
    ON CONFLICT (workspace_id)
    DO UPDATE SET total_bytes = EXCLUDED.total_bytes, updated_at = now()
     WHERE workspace_artifact_usage.total_bytes <> EXCLUDED.total_bytes
"""

_SQL_RECONCILE_USAGE = text(_RECONCILE_USAGE_TEMPLATE.format(cte="", scope=""))

# Activity signals since `:since` (all the ways a workspace's usage could have
# changed):
#
# - `task_artifacts.created_at >= :since` → upload of new artifacts. It starts
#   from `task_artifacts` (not with an EXISTS per workspace) so the filter is
#   resolved with a single traversal bounded by the time index.
# - `workspace_artifact_usage.updated_at >= :since` → the counter changed through
#   an incremental delta: an upload (which rewrites an existing `rel_path` with a
#   different size, without touching `created_at`) or a purge/deletion of tasks
#   through the app, which DELETES the `task_artifacts` rows and therefore leaves
#   no other trace.
# - workspace without a counter row → never reconciled (new workspace);
#   it always enters so the filtered pass is self-healing.
#
# Known limitation: a direct DELETE on `task_artifacts` outside the app leaves no
# activity timestamp and the filtered pass does not see it. The official
# operational routine (an external local purge process) does not delete those
# rows: it MARKS them (`collected_at`, evidence of collection) and reconciles
# `workspace_artifact_usage` itself in the same transaction, so the edge case is
# mitigated at the root for that path. An ad-hoc DELETE outside that routine
# would remain invisible. Beyond that, the rescue is a process restart (first
# full pass).
_SQL_RECONCILE_USAGE_SINCE = text(
    _RECONCILE_USAGE_TEMPLATE.format(
        cte="""
    WITH active AS (
        SELECT DISTINCT t.workspace_id AS workspace_id
          FROM task_artifacts ta
          JOIN task_executions e ON e.id = ta.execution_id
          JOIN tasks t ON t.id = e.task_id
         WHERE ta.created_at >= :since
        UNION
        SELECT u.workspace_id
          FROM workspace_artifact_usage u
         WHERE u.updated_at >= :since
        UNION
        SELECT w2.id
          FROM workspaces w2
         WHERE NOT EXISTS (
                   SELECT 1 FROM workspace_artifact_usage u2
                    WHERE u2.workspace_id = w2.id
               )
    )""",
        scope="JOIN active a ON a.workspace_id = w.id",
    )
)


class ArtifactsRootError(RuntimeError):
    """``ARTIFACTS_ROOT`` cannot be created or written: the server must not start."""


def ensure_artifacts_root(root: Path) -> None:
    """Create ``root`` (with parents) if missing and check it is writable. Idempotent.

    Called ONCE at startup (lifespan) to take the ``mkdir`` out of the poll hot
    path: ``disk_free_bytes`` runs on every poll of every worker inside the event
    loop, and a ``mkdir`` per request is a redundant syscall once the dir exists.

    Fail-closed: a root that cannot be created, is not a directory or is not
    writable (no permission, read-only fs, a path outside the unit's
    ``ReadWritePaths``) raises ``ArtifactsRootError`` and stops the startup.
    Starting anyway would accept tasks whose artifacts cannot be stored and
    would hide an ``ARTIFACTS_ROOT`` that no longer points at the existing
    artifacts (e.g. after an upgrade that changed the default path).
    """
    if root.exists() and not root.is_dir():
        raise ArtifactsRootError(f"ARTIFACTS_ROOT {root} is not a directory")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactsRootError(
            f"ARTIFACTS_ROOT {root} cannot be created ({type(exc).__name__}: {exc.strerror}); "
            "create it for the service user or set ARTIFACTS_ROOT to the existing "
            "artifacts directory"
        ) from None
    if not root.is_dir():
        raise ArtifactsRootError(f"ARTIFACTS_ROOT {root} is not a directory")
    if not os.access(root, os.W_OK | os.X_OK):
        raise ArtifactsRootError(
            f"ARTIFACTS_ROOT {root} is not writable by the server (check its owner "
            "and the systemd unit's ReadWritePaths)"
        )


def disk_free_bytes(root: Path) -> int:
    """Bytes available on the filesystem of ``root``.

    Does not create ``root``: ``ensure_artifacts_root`` does that at startup.
    Fail-open: on any ``OSError`` (nonexistent path, not writable, odd fs) it
    returns ``_UNLIMITED`` and logs. This way a test environment without the root
    provisioned does not block the queue.
    """
    try:
        st = os.statvfs(root)
    except OSError:
        logger.warning("disk_free_bytes: could not measure %s; fail-open", root)
        return _UNLIMITED
    return st.f_bavail * st.f_frsize


def disk_total_bytes(root: Path) -> int:
    """Total bytes of the filesystem of ``root``. 0 if it cannot be measured.

    Does not create ``root`` (see ``ensure_artifacts_root``)."""
    try:
        st = os.statvfs(root)
    except OSError:
        return 0
    return st.f_blocks * st.f_frsize


async def disk_free_bytes_async(root: Path) -> int:
    """``disk_free_bytes`` without blocking the event loop.

    ``statvfs`` is a blocking syscall: run inside an async handler, the uvicorn
    process stops serving other requests while it lasts. It is delegated to a
    thread of the default pool. No cache: it measures on every call (the upload
    enforcement wants the value of the instant).
    """
    return await asyncio.to_thread(disk_free_bytes, root)


async def disk_total_bytes_async(root: Path) -> int:
    """``disk_total_bytes`` without blocking the event loop. No cache: the
    filesystem size does not vary, but the syscall is still blocking."""
    return await asyncio.to_thread(disk_total_bytes, root)


class DiskFreeCache:
    """TTL + single-flight cache of the free space of a ``root``.

    The poll queries the disk floor on EVERY request of EVERY worker: it is the
    system's hot path. Free space does not change meaningfully between consecutive
    polls (default interval 30 s) relative to a 2 GiB floor, so it is cached for a
    few seconds and measured outside the event loop.

    Semantics of ``get``:

    - valid hit → returns the cached value without measuring;
    - miss with a measurement ALREADY in flight and an old value available →
      returns the old value immediately (stale-while-revalidate: no poll waits
      for the syscall except the very first one, with a cold cache);
    - miss without a previous value → waits for the measurement by taking the lock,
      and concurrent callers reuse its result when re-checking after the lock.

    On every path there is at most ONE measurement in flight per ``root``: never
    N simultaneous ``statvfs`` calls from a burst of polls.

    ``ttl_s <= 0`` is passthrough (always measures, does not cache): the default of
    the test suite, where a stale value would break assertions.

    Module-level singleton (``disk_free_cache``), resettable with ``clear()`` from
    fixtures, same pattern as ``status.cache``. The TTL is passed by the caller on
    every call (read from settings), so the module stays free of config and
    monkeypatching settings takes effect immediately.

    It needs no cap on entries or locks: the cardinality of ``root`` is fixed by
    the server configuration (``ARTIFACTS_ROOT``, a single value), not by the client.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[Path, tuple[float, int]] = {}
        self._locks: dict[Path, asyncio.Lock] = {}

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()

    def _fresh(self, root: Path, now: float) -> int | None:
        entry = self._entries.get(root)
        if entry is not None and entry[0] > now:
            return entry[1]
        return None

    def _stale(self, root: Path) -> int | None:
        """Last known value, valid or not (``None`` if never measured)."""
        entry = self._entries.get(root)
        return entry[1] if entry is not None else None

    async def get(self, root: Path, ttl_s: float) -> int:
        if ttl_s <= 0:
            return await disk_free_bytes_async(root)
        free = self._fresh(root, self._clock())
        if free is not None:
            return free
        lock = self._locks.setdefault(root, asyncio.Lock())
        if lock.locked():
            # A measurement is already in flight: serve the old value instead of
            # queuing behind the lock. The bias is at most one TTL plus the
            # duration of the syscall, against a threshold with 2 GiB of margin.
            stale = self._stale(root)
            if stale is not None:
                return stale
        async with lock:
            now = self._clock()
            # Re-check after the lock: another request may have measured while we
            # waited (cold cache case, where there was no old value to serve).
            free = self._fresh(root, now)
            if free is not None:
                return free
            free = await disk_free_bytes_async(root)
            self._entries[root] = (now + ttl_s, free)
            return free


disk_free_cache = DiskFreeCache()


async def workspace_usage_bytes(session: AsyncSession, workspace_id: str) -> int:
    """Artifact bytes accumulated by the workspace (0 if there is no row)."""
    row = (await session.execute(_SQL_WORKSPACE_USAGE, {"workspace_id": workspace_id})).first()
    return int(row.total_bytes) if row is not None else 0


@dataclass(frozen=True)
class WorkspaceQuota:
    """Effective quota of a workspace and its origin."""

    quota_bytes: int
    overridden: bool


async def workspace_quota(
    session: AsyncSession, workspace_id: str, *, settings: Settings
) -> WorkspaceQuota:
    """Effective quota: the workspace override or, if NULL, the global default.

    A NULL override behaves the same as having no override.
    """
    override = (
        await session.execute(_SQL_WORKSPACE_QUOTA, {"workspace_id": workspace_id})
    ).scalar_one_or_none()
    if override is None:
        return WorkspaceQuota(settings.artifact_workspace_max_total_bytes, overridden=False)
    return WorkspaceQuota(int(override), overridden=True)


async def bump_workspace_usage(session: AsyncSession, workspace_id: str, delta: int) -> None:
    """Apply ``delta`` (positive or negative) to the workspace counter.

    Does not commit: runs inside the caller's transaction. Clamps to >=0.
    """
    if delta == 0:
        return
    await session.execute(_SQL_BUMP_WORKSPACE_USAGE, {"workspace_id": workspace_id, "delta": delta})


async def reconcile_workspace_usage(engine: AsyncEngine, *, since: datetime | None = None) -> int:
    """Recompute the counter from the real source (`task_artifacts`) and return
    how many rows it inserted or corrected. Runs in its own transaction
    (housekeeping has no session).

    - ``since=None`` → **full** pass: all workspaces.
    - ``since`` given → only the workspaces whose usage COULD have changed since
      that instant: artifacts created since then, counter bumped since then
      (upload or purge through the app) or workspaces without a counter row. The
      caller passes the instant with an overlap margin already subtracted.

    ``since`` must come from the DB clock (see ``housekeeping._db_now``): the
    timestamps it compares against are stamped by Postgres, and the app server
    clock may be ahead of or behind it.

    Known limitation: a direct DELETE of `task_artifacts` rows outside the app
    leaves no activity timestamp, so a filtered pass does not detect it.
    """
    async with engine.begin() as conn:
        if since is None:
            result = await conn.execute(_SQL_RECONCILE_USAGE)
        else:
            result = await conn.execute(_SQL_RECONCILE_USAGE_SINCE, {"since": since})
        return result.rowcount or 0


@dataclass(frozen=True)
class StorageStatus:
    """Storage snapshot for a workspace at an instant."""

    usage_bytes: int
    quota_bytes: int
    free_bytes: int
    min_free_bytes: int
    # True if `quota_bytes` comes from the workspace override.
    quota_overridden: bool = False

    @property
    def quota_full(self) -> bool:
        return self.usage_bytes >= self.quota_bytes

    @property
    def disk_low(self) -> bool:
        return self.free_bytes < self.min_free_bytes

    @property
    def paused_reason(self) -> str | None:
        """``"quota"`` / ``"disk"`` / ``None``. The quota takes priority."""
        if self.quota_full:
            return "quota"
        if self.disk_low:
            return "disk"
        return None


async def storage_status(
    session: AsyncSession, workspace_id: str, *, settings: Settings
) -> StorageStatus:
    """Measure quota (DB) + physical space (statvfs) for the workspace.

    The physical space comes from the TTL cache: this function runs in the poll
    hot path, where a measurement per request adds no useful precision.
    """
    row = (await session.execute(_SQL_WORKSPACE_STORAGE, {"workspace_id": workspace_id})).first()
    usage = int(row.total_bytes) if row is not None else 0
    override = row.artifact_quota_bytes if row is not None else None
    quota = (
        WorkspaceQuota(settings.artifact_workspace_max_total_bytes, overridden=False)
        if override is None
        else WorkspaceQuota(int(override), overridden=True)
    )
    free = await disk_free_cache.get(
        settings.artifacts_root, settings.artifact_disk_free_cache_ttl_s
    )
    return StorageStatus(
        usage_bytes=usage,
        quota_bytes=quota.quota_bytes,
        free_bytes=free,
        min_free_bytes=settings.artifact_disk_min_free_bytes,
        quota_overridden=quota.overridden,
    )


@dataclass(frozen=True)
class QuotaCommitment:
    """Quota growth headroom against the free space above the floor.

    - ``headroom_bytes`` = Σ max(effective quota − usage, 0) per workspace: what
      artifacts can still grow if every workspace fills its quota. A workspace
      above its quota contributes 0, not a negative.
    - ``available_bytes`` = free space of the ``ARTIFACTS_ROOT`` filesystem −
      ``ARTIFACT_DISK_MIN_FREE_BYTES``: what can be written before crossing the
      shared floor. It can be negative if the disk is already below the floor.
      ``None`` if the disk could not be measured: no verdict.

    Equivalent to comparing Σ max(quota, usage) against usage + free − floor. The
    filesystem total is not used: ``ARTIFACTS_ROOT`` may share it with the DB,
    logs or git clones, and that space will never be available for artifacts.
    ``committed_bytes`` (Σ effective quotas) and ``usage_bytes`` are informative.
    """

    workspaces: int
    overridden: int
    committed_bytes: int
    usage_bytes: int
    headroom_bytes: int
    free_bytes: int | None
    available_bytes: int | None

    @property
    def overcommitted(self) -> bool:
        return self.available_bytes is not None and self.headroom_bytes > self.available_bytes


async def quota_commitment(engine: AsyncEngine, settings: Settings) -> QuotaCommitment:
    """Measure whether what the quotas still allow to grow fits above the floor.

    If it does not fit, the shared disk floor (``ARTIFACT_DISK_MIN_FREE_BYTES``)
    stops being the last safety net and becomes the routine limit: a workspace
    that fills its quota can push the disk below the floor and pause ALL of them.
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                _SQL_QUOTA_COMMITMENT,
                {"default_quota": settings.artifact_workspace_max_total_bytes},
            )
        ).one()
    # Fresh, without the poll cache. `_UNLIMITED` is the measurement's fail-open:
    # here it means "could not be measured", not "infinite disk".
    free: int | None = await disk_free_bytes_async(settings.artifacts_root)
    if free == _UNLIMITED:
        free = None
    available = None if free is None else free - settings.artifact_disk_min_free_bytes
    return QuotaCommitment(
        workspaces=int(row.workspaces),
        overridden=int(row.overridden),
        committed_bytes=int(row.committed),
        usage_bytes=int(row.usage_sum),
        headroom_bytes=int(row.headroom),
        free_bytes=free,
        available_bytes=available,
    )
