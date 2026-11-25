"""Housekeeping tests.

Each helper is tested in isolation with direct SQL. In addition,
a smoke test runs `housekeeping_loop` in an `asyncio.Task` and checks
that shutdown via `stop_event` is clean.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server import housekeeping, storage
from orcaster_server.config import Settings
from orcaster_server.housekeeping import (
    _expire_assigned,
    _mark_offline_workers,
    _reclaim_expired_leases,
    housekeeping_loop,
)
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

DEFAULT_WS = DEFAULT_WORKSPACE_ID


@pytest.fixture(autouse=True)
def _reset_reconcile_watermark() -> Any:
    """The watermark of the last reconcile pass is module state:
    it is reset around each test so that order does not matter."""
    housekeeping.reset_reconcile_watermark()
    yield
    housekeeping.reset_reconcile_watermark()


async def _insert_worker(
    db_url: str,
    *,
    hostname: str = "w",
    status: str = "online",
    heartbeat_age_seconds: int = 0,
) -> str:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO workers (workspace_id, api_key_hash, hostname,
                                             os, arch, status,
                                             registered_at, last_heartbeat_at)
                        VALUES (:workspace_id, :hash, :host, 'linux', 'x86_64',
                                :status,
                                now() - make_interval(secs => :age),
                                now() - make_interval(secs => :age))
                        RETURNING id
                        """
                    ),
                    {
                        "hash": f"hk-{hostname}-{heartbeat_age_seconds}",
                        "host": hostname,
                        "status": status,
                        "age": heartbeat_age_seconds,
                        "workspace_id": DEFAULT_WORKSPACE_ID,
                    },
                )
            ).one()
        return str(row[0])
    finally:
        await engine.dispose()


async def _insert_task(
    db_url: str,
    *,
    claim_timeout_seconds: int | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, command, payload,
                                           target_mode, target_count,
                                           priority, status, claim_timeout_seconds,
                                           run_at, signed, signature)
                        VALUES (:workspace_id, 'echo hk', CAST(:payload AS jsonb),
                                'count', 1, 0,
                                'queued', :cto, now(),
                                true, decode(repeat('00', 64), 'hex'))
                        RETURNING id
                        """
                    ),
                    {
                        "payload": json.dumps(payload or {}),
                        "cto": claim_timeout_seconds,
                        "workspace_id": DEFAULT_WORKSPACE_ID,
                    },
                )
            ).one()
        return int(row[0])
    finally:
        await engine.dispose()


async def _insert_execution(
    db_url: str,
    *,
    task_id: int,
    worker_id: str,
    status: str,
    lease_age_seconds: int | None = None,
    assigned_age_seconds: int = 0,
) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO task_executions (task_id, worker_id, status,
                                                 lease_expires_at, assigned_at,
                                                 last_heartbeat_at)
                    VALUES (
                        :tid, :wid, :st,
                        CASE WHEN CAST(:lease_age AS int) IS NULL THEN NULL
                             ELSE now() - make_interval(secs => CAST(:lease_age AS int)) END,
                        now() - make_interval(secs => :a_age),
                        now() - make_interval(secs => :a_age)
                    )
                    """
                ),
                {
                    "tid": task_id,
                    "wid": worker_id,
                    "st": status,
                    "lease_age": lease_age_seconds,
                    "a_age": assigned_age_seconds,
                },
            )
    finally:
        await engine.dispose()


async def _fetch_one(db_url: str, sql: str, params: dict[str, Any]) -> Any:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params)).first()
    finally:
        await engine.dispose()


# ---------- _mark_offline_workers ----------


@pytest.mark.asyncio
async def test_mark_offline_workers_marks_stale_online(app_client, migrated_db_url: str) -> None:
    wid = await _insert_worker(migrated_db_url, hostname="stale", heartbeat_age_seconds=300)
    engine = create_async_engine(migrated_db_url)
    try:
        n = await _mark_offline_workers(engine, timeout_seconds=120)
    finally:
        await engine.dispose()
    assert n == 1
    row = await _fetch_one(migrated_db_url, "SELECT status FROM workers WHERE id=:i", {"i": wid})
    assert row[0] == "offline"


@pytest.mark.asyncio
async def test_mark_offline_workers_preserves_recent(app_client, migrated_db_url: str) -> None:
    wid = await _insert_worker(migrated_db_url, hostname="fresh", heartbeat_age_seconds=5)
    engine = create_async_engine(migrated_db_url)
    try:
        n = await _mark_offline_workers(engine, timeout_seconds=120)
    finally:
        await engine.dispose()
    assert n == 0
    row = await _fetch_one(migrated_db_url, "SELECT status FROM workers WHERE id=:i", {"i": wid})
    assert row[0] == "online"


@pytest.mark.asyncio
async def test_mark_offline_workers_skips_paused(app_client, migrated_db_url: str) -> None:
    # Only `status='online'` transitions; paused is preserved.
    wid = await _insert_worker(
        migrated_db_url, hostname="paused", status="paused", heartbeat_age_seconds=300
    )
    engine = create_async_engine(migrated_db_url)
    try:
        await _mark_offline_workers(engine, timeout_seconds=120)
    finally:
        await engine.dispose()
    row = await _fetch_one(migrated_db_url, "SELECT status FROM workers WHERE id=:i", {"i": wid})
    assert row[0] == "paused"


# ---------- _reclaim_expired_leases ----------


@pytest.mark.asyncio
async def test_reclaim_expired_leases_running(app_client, migrated_db_url: str) -> None:
    wid = await _insert_worker(migrated_db_url, hostname="r1")
    tid = await _insert_task(migrated_db_url)
    await _insert_execution(
        migrated_db_url,
        task_id=tid,
        worker_id=wid,
        status="running",
        lease_age_seconds=60,  # lease expired 60s ago
    )
    engine = create_async_engine(migrated_db_url)
    try:
        n = await _reclaim_expired_leases(engine)
    finally:
        await engine.dispose()
    assert n == 1
    row = await _fetch_one(
        migrated_db_url,
        "SELECT status, finished_at FROM task_executions WHERE task_id=:t AND worker_id=:w",
        {"t": tid, "w": wid},
    )
    assert row[0] == "expired"
    assert row[1] is not None


@pytest.mark.asyncio
async def test_reclaim_expired_leases_skips_valid_lease(app_client, migrated_db_url: str) -> None:
    wid = await _insert_worker(migrated_db_url, hostname="r2")
    tid = await _insert_task(migrated_db_url)
    await _insert_execution(
        migrated_db_url,
        task_id=tid,
        worker_id=wid,
        status="running",
        lease_age_seconds=-30,  # lease expires in the future
    )
    engine = create_async_engine(migrated_db_url)
    try:
        n = await _reclaim_expired_leases(engine)
    finally:
        await engine.dispose()
    assert n == 0
    row = await _fetch_one(
        migrated_db_url,
        "SELECT status FROM task_executions WHERE task_id=:t AND worker_id=:w",
        {"t": tid, "w": wid},
    )
    assert row[0] == "running"


# ---------- _expire_assigned ----------


@pytest.mark.asyncio
async def test_expire_assigned_past_claim_timeout(app_client, migrated_db_url: str) -> None:
    wid = await _insert_worker(migrated_db_url, hostname="a1")
    tid = await _insert_task(migrated_db_url, claim_timeout_seconds=60)
    await _insert_execution(
        migrated_db_url,
        task_id=tid,
        worker_id=wid,
        status="assigned",
        lease_age_seconds=None,
        assigned_age_seconds=120,  # excede claim_timeout 60s
    )
    engine = create_async_engine(migrated_db_url)
    try:
        n = await _expire_assigned(engine)
    finally:
        await engine.dispose()
    assert n == 1
    row = await _fetch_one(
        migrated_db_url,
        "SELECT status FROM task_executions WHERE task_id=:t AND worker_id=:w",
        {"t": tid, "w": wid},
    )
    assert row[0] == "expired"


@pytest.mark.asyncio
async def test_expire_assigned_no_claim_timeout(app_client, migrated_db_url: str) -> None:
    # claim_timeout_seconds NULL -> never expires by timeout.
    wid = await _insert_worker(migrated_db_url, hostname="a2")
    tid = await _insert_task(migrated_db_url, claim_timeout_seconds=None)
    await _insert_execution(
        migrated_db_url,
        task_id=tid,
        worker_id=wid,
        status="assigned",
        lease_age_seconds=None,
        assigned_age_seconds=99999,
    )
    engine = create_async_engine(migrated_db_url)
    try:
        n = await _expire_assigned(engine)
    finally:
        await engine.dispose()
    assert n == 0


async def _expire_with_refused_worker(
    db_url: str,
    *,
    mark_age_seconds: int | None,
    payload: dict[str, Any],
    in_grace: bool,
    hostname: str = "refused",
) -> str:
    """Expired pre-assigned execution of a worker with a refusal mark of that age;
    returns the execution status after a pass of `_expire_assigned`."""
    wid = await _insert_worker(db_url, hostname=hostname)
    if mark_age_seconds is not None:
        engine = create_async_engine(db_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE workers SET task_secrets_refused_at = "
                        "now() - make_interval(secs => :a) WHERE id = :w"
                    ),
                    {"a": mark_age_seconds, "w": wid},
                )
        finally:
            await engine.dispose()
    tid = await _insert_task(db_url, claim_timeout_seconds=60, payload=payload)
    await _insert_execution(
        db_url,
        task_id=tid,
        worker_id=wid,
        status="assigned",
        lease_age_seconds=None,
        assigned_age_seconds=120,
    )
    engine = create_async_engine(db_url)
    try:
        await _expire_assigned(engine, in_grace=in_grace)
    finally:
        await engine.dispose()
    row = await _fetch_one(
        db_url,
        "SELECT status FROM task_executions WHERE task_id=:t AND worker_id=:w",
        {"t": tid, "w": wid},
    )
    return str(row[0])


@pytest.mark.asyncio
async def test_expire_assigned_restart_keeps_refused_worker_execution_in_grace(
    app_client, migrated_db_url: str
) -> None:
    """With the server down the mark ages past the 120 s window;
    the startup housekeeping pass, before the worker polls again,
    must not expire its pre-assigned execution. Within the startup grace it is kept."""
    status = await _expire_with_refused_worker(
        migrated_db_url,
        mark_age_seconds=3600,
        payload={"task_env": ["API_KEY"]},
        in_grace=True,
    )
    assert status == "assigned"


@pytest.mark.asyncio
async def test_expire_assigned_stale_mark_expires_after_grace(
    app_client, migrated_db_url: str
) -> None:
    """Past the grace the mark counts again only for its window: if the worker has not
    polled again, its pre-assigned execution expires as usual."""
    status = await _expire_with_refused_worker(
        migrated_db_url,
        mark_age_seconds=3600,
        payload={"task_env": ["API_KEY"]},
        in_grace=False,
    )
    assert status == "expired"


@pytest.mark.asyncio
async def test_expire_assigned_grace_does_not_shield_unrelated_executions(
    app_client, migrated_db_url: str
) -> None:
    """The grace only covers workers with a mark and tasks that declare `task_env`."""
    no_env = await _expire_with_refused_worker(
        migrated_db_url,
        mark_age_seconds=3600,
        payload={},
        in_grace=True,
        hostname="refused-noenv",
    )
    assert no_env == "expired"
    never_refused = await _expire_with_refused_worker(
        migrated_db_url,
        mark_age_seconds=None,
        payload={"task_env": ["API_KEY"]},
        in_grace=True,
        hostname="never-refused",
    )
    assert never_refused == "expired"


# ---------- orphan push tasks: cleanup removed ----------
# With the unified endpoint: the FK tasks.target_worker_id → workers.id is
# `ON DELETE CASCADE` (migration 0007). Deleting a worker removes its push
# tasks. The CHECK constraint prevents creating a push task with a NULL target.
# Therefore the case "push task with target_worker_id IS NULL" does not exist;
# there is no cleanup helper nor a test exercising that scenario.


# ---------- loop smoke ----------


@pytest.mark.asyncio
async def test_housekeeping_loop_marks_offline_then_stops(app_client, migrated_db_url: str) -> None:
    wid = await _insert_worker(migrated_db_url, hostname="loop-stale", heartbeat_age_seconds=300)
    engine = create_async_engine(migrated_db_url)
    try:
        settings = Settings(
            DATABASE_URL=migrated_db_url,
            WORKER_HEARTBEAT_TIMEOUT_SECONDS=120,
            COMMANDS_RETENTION_DAYS=7,
            HOUSEKEEPING_INTERVAL_SECONDS=1,
            HOUSEKEEPING_ENABLED=True,
            STARTUP_GRACE_SECONDS=0,  # no grace so the smoke test can mark offline
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(housekeeping_loop(engine, settings, stop_event))
        # Wait for the first pass to complete.
        await asyncio.sleep(0.5)
        # Check that the worker ended up offline before stopping.
        row = await _fetch_one(
            migrated_db_url, "SELECT status FROM workers WHERE id=:i", {"i": wid}
        )
        assert row[0] == "offline"
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)
        assert task.done()
    finally:
        await engine.dispose()


# ---------- _reconcile_workspace_usage ----------


@pytest.mark.asyncio
async def test_reconcile_workspace_usage_corrects_drift(app_client, migrated_db_url: str) -> None:
    """The reconcile recomputes the counter from task_artifacts (fixes drift)."""
    from orcaster_server.housekeeping import _reconcile_workspace_usage

    settings = Settings(DATABASE_URL=migrated_db_url)
    wid = await _insert_worker(migrated_db_url, hostname="rc")
    tid = await _insert_task(migrated_db_url)
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            eid = (
                await conn.execute(
                    text(
                        "INSERT INTO task_executions (task_id, worker_id, status) "
                        "VALUES (:t, :w, 'completed') RETURNING id::text"
                    ),
                    {"t": tid, "w": wid},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO task_artifacts (execution_id, rel_path, size_bytes, "
                    "sha256, content_type) VALUES (CAST(:e AS uuid), 'x', 777, 'h', 'text/plain')"
                ),
                {"e": eid},
            )
            # Drift artificial: contador desalineado.
            await conn.execute(
                text(
                    "INSERT INTO workspace_artifact_usage (workspace_id, total_bytes) "
                    "VALUES (:w, 5) ON CONFLICT (workspace_id) DO UPDATE SET total_bytes = 5"
                ),
                {"w": DEFAULT_WS},
            )
        n = await _reconcile_workspace_usage(engine, settings)
        assert n >= 1
        async with engine.connect() as conn:
            total = (
                await conn.execute(
                    text(
                        "SELECT total_bytes FROM workspace_artifact_usage WHERE workspace_id = :w"
                    ),
                    {"w": DEFAULT_WS},
                )
            ).scalar_one()
        assert total == 777
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_excludes_collected_artifacts(app_client, migrated_db_url: str) -> None:
    """The quota SUM does not count collected rows.

    A collected artifact keeps its row as evidence, but its bytes are no
    longer on disk: continuing to count it would leave the quota blocked by
    space that was freed. The workspace WITHOUT artifacts must still appear
    with 0 (the filter goes in the ON of the LEFT JOIN, not in the WHERE).
    """
    wid = await _insert_worker(migrated_db_url, hostname="rc-collected")
    tid = await _insert_task(migrated_db_url)
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            eid = (
                await conn.execute(
                    text(
                        "INSERT INTO task_executions (task_id, worker_id, status) "
                        "VALUES (:t, :w, 'completed') RETURNING id::text"
                    ),
                    {"t": tid, "w": wid},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO task_artifacts (execution_id, rel_path, size_bytes, "
                    "sha256, content_type) "
                    "VALUES (CAST(:e AS uuid), 'live', 100, 'h', 'text/plain')"
                ),
                {"e": eid},
            )
            await conn.execute(
                text(
                    "INSERT INTO task_artifacts (execution_id, rel_path, size_bytes, "
                    "sha256, content_type, collected_at) "
                    "VALUES (CAST(:e AS uuid), 'gone', 900, 'h', 'text/plain', now())"
                ),
                {"e": eid},
            )
        await storage.reconcile_workspace_usage(engine, since=None)
        async with engine.connect() as conn:
            total = (
                await conn.execute(
                    text(
                        "SELECT total_bytes FROM workspace_artifact_usage WHERE workspace_id = :w"
                    ),
                    {"w": DEFAULT_WS},
                )
            ).scalar_one()
            n_rows = (
                await conn.execute(text("SELECT count(*) FROM workspace_artifact_usage"))
            ).scalar_one()
            n_ws = (await conn.execute(text("SELECT count(*) FROM workspaces"))).scalar_one()
        # Only the live artifact counts.
        assert total == 100
        # And every workspace (including those without artifacts) still has its row:
        # the collected filter goes in the ON, not in the WHERE.
        assert n_rows == n_ws
    finally:
        await engine.dispose()


# ---------- reconcile filtered by recent activity ----------
#
# The reconcile accepts `since`: with `None` it recomputes all workspaces
# (historical behavior), and with a given instant only those whose usage may
# have changed since then. Housekeeping keeps the watermark of the last
# successful pass in process memory.


async def _insert_workspace(db_url: str, slug: str) -> str:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text("INSERT INTO workspaces (name, slug) VALUES (:n, :s) RETURNING id::text"),
                    {"n": slug, "s": slug},
                )
            ).one()
        return str(row[0])
    finally:
        await engine.dispose()


async def _seed_artifact(
    db_url: str,
    *,
    workspace_id: str,
    size_bytes: int,
    artifact_age_seconds: int = 0,
    tag: str = "a",
    collected: bool = False,
) -> None:
    """task + execution + artifact in `workspace_id`.

    The artifact's `created_at` is aged by `artifact_age_seconds` to
    simulate old activity (outside the window of the filtered reconcile).

    With `collected=True` the row is born with a non-null `collected_at`: the
    bytes are no longer on disk and the quota SUM must ignore it.
    """
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            wid = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO workers (workspace_id, api_key_hash, hostname,
                                             os, arch, status)
                        VALUES (CAST(:ws AS uuid), :hash, :host, 'linux', 'x86_64', 'online')
                        RETURNING id
                        """
                    ),
                    {"ws": workspace_id, "hash": f"hk78-{workspace_id}-{tag}", "host": f"h{tag}"},
                )
            ).scalar_one()
            tid = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO tasks (workspace_id, command, payload, target_mode,
                                           target_count, priority, status, run_at,
                                           signed, signature)
                        VALUES (CAST(:ws AS uuid), 'echo hk78', '{}'::jsonb, 'count',
                                1, 0, 'queued', now(),
                                true, decode(repeat('00', 64), 'hex'))
                        RETURNING id
                        """
                    ),
                    {"ws": workspace_id},
                )
            ).scalar_one()
            eid = (
                await conn.execute(
                    text(
                        "INSERT INTO task_executions (task_id, worker_id, status) "
                        "VALUES (:t, :w, 'completed') RETURNING id::text"
                    ),
                    {"t": tid, "w": wid},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    """
                    INSERT INTO task_artifacts (execution_id, rel_path, size_bytes,
                                                sha256, content_type, created_at,
                                                collected_at)
                    VALUES (CAST(:e AS uuid), :p, :size, 'h', 'text/plain',
                            now() - make_interval(secs => :age),
                            CASE WHEN :collected THEN now() ELSE NULL END)
                    """
                ),
                {
                    "e": eid,
                    "p": f"art-{tag}",
                    "size": size_bytes,
                    "age": artifact_age_seconds,
                    "collected": collected,
                },
            )
    finally:
        await engine.dispose()


async def _set_usage(
    db_url: str, workspace_id: str, total_bytes: int, *, updated_age_seconds: int = 0
) -> None:
    """Set the counter (and the age of its `updated_at`) of a workspace."""
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO workspace_artifact_usage (workspace_id, total_bytes, updated_at)
                    VALUES (CAST(:w AS uuid), :b, now() - make_interval(secs => :age))
                    ON CONFLICT (workspace_id) DO UPDATE
                       SET total_bytes = EXCLUDED.total_bytes,
                           updated_at = EXCLUDED.updated_at
                    """
                ),
                {"w": workspace_id, "b": total_bytes, "age": updated_age_seconds},
            )
    finally:
        await engine.dispose()


async def _usage_row(db_url: str, workspace_id: str) -> Any:
    return await _fetch_one(
        db_url,
        "SELECT total_bytes, updated_at FROM workspace_artifact_usage "
        "WHERE workspace_id = CAST(:w AS uuid)",
        {"w": workspace_id},
    )


@pytest.mark.asyncio
async def test_reconcile_since_none_reconciles_all(app_client, migrated_db_url: str) -> None:
    """`since=None` keeps the full scan: it fixes even inactive workspaces."""
    ws = await _insert_workspace(migrated_db_url, "hk78-full")
    await _seed_artifact(
        migrated_db_url, workspace_id=ws, size_bytes=777, artifact_age_seconds=3600
    )
    await _set_usage(migrated_db_url, ws, 5, updated_age_seconds=3600)
    engine = create_async_engine(migrated_db_url)
    try:
        await storage.reconcile_workspace_usage(engine, since=None)
    finally:
        await engine.dispose()
    assert (await _usage_row(migrated_db_url, ws))[0] == 777


@pytest.mark.asyncio
async def test_reconcile_since_also_excludes_collected(app_client, migrated_db_url: str) -> None:
    """The collected filter lives in the shared template.

    It is in the `ON` of the `LEFT JOIN` of `_RECONCILE_USAGE_TEMPLATE`, so the
    pass filtered by `since` applies it just like the full scan: if it were
    only in the full variant, steady-state housekeeping (the one that uses
    `since`) would keep counting bytes that are no longer on disk.
    """
    ws = await _insert_workspace(migrated_db_url, "hk75-since")
    # One live and one collected artifact, both recent (they enter via `since`).
    await _seed_artifact(migrated_db_url, workspace_id=ws, size_bytes=100, tag="live")
    await _seed_artifact(
        migrated_db_url, workspace_id=ws, size_bytes=900, tag="gone", collected=True
    )
    await _set_usage(migrated_db_url, ws, 5, updated_age_seconds=3600)
    engine = create_async_engine(migrated_db_url)
    try:
        since = await housekeeping._db_now(engine) - timedelta(seconds=60)
        await storage.reconcile_workspace_usage(engine, since=since)
    finally:
        await engine.dispose()
    assert (await _usage_row(migrated_db_url, ws))[0] == 100


@pytest.mark.asyncio
async def test_reconcile_since_includes_recent_artifact(app_client, migrated_db_url: str) -> None:
    """An artifact created after `since` puts the workspace in the pass."""
    ws = await _insert_workspace(migrated_db_url, "hk78-recent")
    # New artifact (age 0) and old/misaligned counter: the only activity signal
    # is `task_artifacts.created_at`.
    await _seed_artifact(migrated_db_url, workspace_id=ws, size_bytes=777, artifact_age_seconds=0)
    await _set_usage(migrated_db_url, ws, 5, updated_age_seconds=3600)
    engine = create_async_engine(migrated_db_url)
    try:
        since = await housekeeping._db_now(engine) - timedelta(seconds=60)
        await storage.reconcile_workspace_usage(engine, since=since)
    finally:
        await engine.dispose()
    assert (await _usage_row(migrated_db_url, ws))[0] == 777


@pytest.mark.asyncio
async def test_reconcile_since_excludes_workspace_without_activity(
    app_client, migrated_db_url: str
) -> None:
    """Without activity since `since`, the workspace is not included: its drift stays intact."""
    ws = await _insert_workspace(migrated_db_url, "hk78-inactive")
    await _seed_artifact(
        migrated_db_url, workspace_id=ws, size_bytes=777, artifact_age_seconds=3600
    )
    await _set_usage(migrated_db_url, ws, 5, updated_age_seconds=3600)
    engine = create_async_engine(migrated_db_url)
    try:
        since = await housekeeping._db_now(engine) - timedelta(seconds=60)
        await storage.reconcile_workspace_usage(engine, since=since)
    finally:
        await engine.dispose()
    assert (await _usage_row(migrated_db_url, ws))[0] == 5


@pytest.mark.asyncio
async def test_reconcile_since_includes_bumped_counter(app_client, migrated_db_url: str) -> None:
    """A recent bump of the counter (upload or purge) is also activity.

    It is the only signal left by an artifact deletion done by the app: the
    `task_artifacts` row disappears, but the delta UPSERT touches `updated_at`.
    """
    ws = await _insert_workspace(migrated_db_url, "hk78-bump")
    await _seed_artifact(
        migrated_db_url, workspace_id=ws, size_bytes=777, artifact_age_seconds=3600
    )
    await _set_usage(migrated_db_url, ws, 5, updated_age_seconds=0)
    engine = create_async_engine(migrated_db_url)
    try:
        since = await housekeeping._db_now(engine) - timedelta(seconds=60)
        await storage.reconcile_workspace_usage(engine, since=since)
    finally:
        await engine.dispose()
    assert (await _usage_row(migrated_db_url, ws))[0] == 777


@pytest.mark.asyncio
async def test_reconcile_since_includes_workspace_without_row(
    app_client, migrated_db_url: str
) -> None:
    """A workspace without a counter row is always included (self-healing)."""
    ws = await _insert_workspace(migrated_db_url, "hk78-no-row")
    await _seed_artifact(
        migrated_db_url, workspace_id=ws, size_bytes=777, artifact_age_seconds=3600
    )
    engine = create_async_engine(migrated_db_url)
    try:
        since = await housekeeping._db_now(engine) - timedelta(seconds=60)
        await storage.reconcile_workspace_usage(engine, since=since)
    finally:
        await engine.dispose()
    assert (await _usage_row(migrated_db_url, ws))[0] == 777


@pytest.mark.asyncio
async def test_reconcile_without_drift_does_not_touch_updated_at(
    app_client, migrated_db_url: str
) -> None:
    """The reconcile does not mark itself as activity.

    If the counter was already correct, `updated_at` does not move; if it
    bumped it, every reconciled workspace would stay "active" forever and the
    recent-activity filter would filter nothing.
    """
    ws = await _insert_workspace(migrated_db_url, "hk78-no-drift")
    await _seed_artifact(
        migrated_db_url, workspace_id=ws, size_bytes=777, artifact_age_seconds=3600
    )
    await _set_usage(migrated_db_url, ws, 777, updated_age_seconds=3600)
    before = (await _usage_row(migrated_db_url, ws))[1]
    engine = create_async_engine(migrated_db_url)
    try:
        await storage.reconcile_workspace_usage(engine, since=None)
    finally:
        await engine.dispose()
    after = await _usage_row(migrated_db_url, ws)
    assert after[0] == 777
    assert after[1] == before


@pytest.mark.asyncio
async def test_housekeeping_first_pass_complete_then_filtered(
    app_client, migrated_db_url: str, monkeypatch
) -> None:
    """The first pass of the process is full; the following ones filter."""
    calls: list[Any] = []

    async def _fake(engine: Any, *, since: Any = None) -> int:
        calls.append(since)
        return 0

    monkeypatch.setattr(storage, "reconcile_workspace_usage", _fake)
    settings = Settings(DATABASE_URL=migrated_db_url, HOUSEKEEPING_INTERVAL_SECONDS=30)
    engine = create_async_engine(migrated_db_url)
    try:
        await housekeeping._reconcile_workspace_usage(engine, settings)
        await housekeeping._reconcile_workspace_usage(engine, settings)
        now = await housekeeping._db_now(engine)
    finally:
        await engine.dispose()

    assert calls[0] is None, "the first pass after startup must be full"
    assert isinstance(calls[1], datetime), "the second pass must filter by activity"
    # The watermark carries the overlap margin subtracted and cannot be in the future.
    assert calls[1] < now
    assert calls[1] > now - timedelta(seconds=housekeeping._RECONCILE_OVERLAP_SECONDS + 60)


@pytest.mark.asyncio
async def test_housekeeping_does_not_advance_mark_if_it_fails(
    app_client, migrated_db_url: str, monkeypatch
) -> None:
    """If the pass blows up, the watermark does not advance: the next one is full again."""
    calls: list[Any] = []

    async def _boom(engine: Any, *, since: Any = None) -> int:
        calls.append(since)
        raise RuntimeError("reconcile broken")

    monkeypatch.setattr(storage, "reconcile_workspace_usage", _boom)
    settings = Settings(DATABASE_URL=migrated_db_url)
    engine = create_async_engine(migrated_db_url)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await housekeeping._reconcile_workspace_usage(engine, settings)
    finally:
        await engine.dispose()

    assert calls == [None, None]


@pytest.mark.asyncio
async def test_housekeeping_loop_resets_the_mark(app_client, migrated_db_url: str, monkeypatch):
    """Starting the loop forgets the watermark: the first reconcile of the process is full."""
    monkeypatch.setattr(housekeeping, "_run_once", AsyncMock(return_value=None))
    housekeeping._last_reconcile_at = datetime(2020, 1, 1, tzinfo=UTC)
    settings = Settings(DATABASE_URL=migrated_db_url, HOUSEKEEPING_INTERVAL_SECONDS=1)
    stop_event = asyncio.Event()
    task = asyncio.create_task(housekeeping_loop(None, settings, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=3.0)
    assert housekeeping._last_reconcile_at is None


# ---------- startup grace ----------


@pytest.mark.asyncio
async def test_run_once_in_grace_omits_sticky_offline_lease(monkeypatch):
    """During the grace, _run_once does NOT expire leases, mark offline or relax sticky,
    but DOES run expire_assigned and the reconcile."""
    mark = AsyncMock(return_value=0)
    reclaim = AsyncMock(return_value=0)
    relax = AsyncMock(return_value=0)
    expire = AsyncMock(return_value=0)
    reconcile = AsyncMock(return_value=0)
    count = AsyncMock(return_value=0)
    monkeypatch.setattr(housekeeping, "_mark_offline_workers", mark)
    monkeypatch.setattr(housekeeping, "_reclaim_expired_leases", reclaim)
    monkeypatch.setattr(housekeeping, "_relax_sticky_pool", relax)
    monkeypatch.setattr(housekeeping, "_expire_assigned", expire)
    monkeypatch.setattr(housekeeping, "_reconcile_workspace_usage", reconcile)
    monkeypatch.setattr(housekeeping, "_count_expired_leases", count)
    commitment = AsyncMock(return_value=None)
    monkeypatch.setattr(housekeeping, "_check_quota_commitment", commitment)

    settings = Settings()
    await housekeeping._run_once(engine=None, settings=settings, passes=0, in_grace=True)

    mark.assert_not_called()
    reclaim.assert_not_called()
    relax.assert_not_called()
    # The grace reaches the claim timeout to keep refused pre-assigned executions.
    expire.assert_called_once_with(None, in_grace=True)
    reconcile.assert_called_once()
    commitment.assert_called_once()  # clock-independent: also runs in grace
    count.assert_called_once()  # grace observability signal


@pytest.mark.asyncio
async def test_run_once_without_grace_runs_everything(monkeypatch):
    """Without grace, _run_once runs all the operations (no regression)."""
    mark = AsyncMock(return_value=0)
    reclaim = AsyncMock(return_value=0)
    relax = AsyncMock(return_value=0)
    expire = AsyncMock(return_value=0)
    reconcile = AsyncMock(return_value=0)
    count = AsyncMock(return_value=0)
    monkeypatch.setattr(housekeeping, "_mark_offline_workers", mark)
    monkeypatch.setattr(housekeeping, "_reclaim_expired_leases", reclaim)
    monkeypatch.setattr(housekeeping, "_relax_sticky_pool", relax)
    monkeypatch.setattr(housekeeping, "_expire_assigned", expire)
    monkeypatch.setattr(housekeeping, "_reconcile_workspace_usage", reconcile)
    monkeypatch.setattr(housekeeping, "_count_expired_leases", count)
    commitment = AsyncMock(return_value=None)
    monkeypatch.setattr(housekeeping, "_check_quota_commitment", commitment)

    settings = Settings()
    await housekeeping._run_once(engine=None, settings=settings, passes=0, in_grace=False)

    mark.assert_called_once()
    reclaim.assert_called_once()
    relax.assert_called_once()
    expire.assert_called_once_with(None, in_grace=False)
    reconcile.assert_called_once()
    commitment.assert_called_once()
    count.assert_not_called()  # the observability count only runs in grace


@pytest.mark.asyncio
async def test_run_once_in_grace_emits_signal_if_there_are_held_leases(monkeypatch, caplog):
    """If expired leases are retained during the grace, _run_once emits an
    observability WARNING with the count."""
    monkeypatch.setattr(housekeeping, "_mark_offline_workers", AsyncMock(return_value=0))
    monkeypatch.setattr(housekeeping, "_reclaim_expired_leases", AsyncMock(return_value=0))
    monkeypatch.setattr(housekeeping, "_relax_sticky_pool", AsyncMock(return_value=0))
    monkeypatch.setattr(housekeeping, "_expire_assigned", AsyncMock(return_value=0))
    monkeypatch.setattr(housekeeping, "_reconcile_workspace_usage", AsyncMock(return_value=0))
    monkeypatch.setattr(housekeeping, "_count_expired_leases", AsyncMock(return_value=3))
    monkeypatch.setattr(housekeeping, "_check_quota_commitment", AsyncMock(return_value=None))

    settings = Settings()
    with caplog.at_level("WARNING", logger="orcaster_server.housekeeping"):
        await housekeeping._run_once(engine=None, settings=settings, passes=0, in_grace=True)

    assert any("startup grace protecting 3 execution" in r.message for r in caplog.records)


def _patch_run_once_helpers(monkeypatch) -> dict[str, AsyncMock]:
    """Replace all the `_run_once` helpers with mocks and return them."""
    mocks = {
        name: AsyncMock(return_value=0)
        for name in (
            "_mark_offline_workers",
            "_reclaim_expired_leases",
            "_relax_sticky_pool",
            "_expire_assigned",
            "_reconcile_workspace_usage",
            "_count_expired_leases",
            "_sweep_expired_challenges",
        )
    }
    mocks["_check_quota_commitment"] = AsyncMock(return_value=None)
    # Returns (tasks, pre-assignments), not an integer.
    mocks["_resnapshot_awaiting_all_available"] = AsyncMock(return_value=(0, 0))
    for name, mock in mocks.items():
        monkeypatch.setattr(housekeeping, name, mock)
    return mocks


@pytest.mark.asyncio
async def test_run_once_checks_over_commit_with_the_reconcile_cadence(monkeypatch):
    """The over-commit check runs in the same passes as the
    reconcile: every ARTIFACT_USAGE_RECONCILE_PASSES, including the first."""
    mocks = _patch_run_once_helpers(monkeypatch)
    settings = Settings(ARTIFACT_USAGE_RECONCILE_PASSES=3)

    for passes in range(7):
        await housekeeping._run_once(engine=None, settings=settings, passes=passes, in_grace=False)

    assert mocks["_reconcile_workspace_usage"].await_count == 3  # passes 0, 3 and 6
    assert mocks["_check_quota_commitment"].await_count == 3
    mocks["_check_quota_commitment"].assert_awaited_with(None, settings)


@pytest.mark.asyncio
async def test_run_once_isolates_the_failure_of_the_over_commit_check(monkeypatch, caplog):
    """A failure of the over-commit check is logged with a traceback and does not
    cut the pass short: the reconcile already ran and `_run_once` does not
    propagate the exception."""
    mocks = _patch_run_once_helpers(monkeypatch)
    mocks["_check_quota_commitment"].side_effect = RuntimeError("statvfs broken")
    settings = Settings()

    with caplog.at_level("ERROR", logger="orcaster_server.housekeeping"):
        await housekeeping._run_once(engine=None, settings=settings, passes=0, in_grace=False)

    mocks["_reconcile_workspace_usage"].assert_awaited_once()
    mocks["_check_quota_commitment"].assert_awaited_once()
    (record,) = [r for r in caplog.records if r.levelname == "ERROR"]
    assert record.getMessage() == "housekeeping: quota commitment check failed"
    assert record.exc_info is not None and "statvfs broken" in str(record.exc_info[1])


@pytest.mark.asyncio
async def test_run_once_checks_over_commit_even_if_the_reconcile_fails(monkeypatch, caplog):
    """And conversely: a broken reconcile does not prevent the over-commit check."""
    mocks = _patch_run_once_helpers(monkeypatch)
    mocks["_reconcile_workspace_usage"].side_effect = RuntimeError("reconcile broken")
    settings = Settings()

    with caplog.at_level("ERROR", logger="orcaster_server.housekeeping"):
        await housekeeping._run_once(engine=None, settings=settings, passes=0, in_grace=False)

    mocks["_check_quota_commitment"].assert_awaited_once()
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == [
        "housekeeping: reconcile_workspace_usage failed"
    ]


@pytest.mark.asyncio
async def test_loop_passes_in_grace_true_inside_the_window(monkeypatch):
    """With a wide grace, the first pass of the loop runs within the window
    (in_grace=True)."""
    captured = []

    async def fake_run_once(engine, settings, passes=0, in_grace=False):
        captured.append(in_grace)
        stop_event.set()

    monkeypatch.setattr(housekeeping, "_run_once", fake_run_once)
    settings = Settings(STARTUP_GRACE_SECONDS=3600, HOUSEKEEPING_INTERVAL_SECONDS=1)
    stop_event = asyncio.Event()
    await asyncio.wait_for(housekeeping_loop(None, settings, stop_event), timeout=3.0)

    assert captured == [True]


@pytest.mark.asyncio
async def test_loop_passes_in_grace_false_after_the_grace(monkeypatch):
    """With grace=0, the first pass is already outside the window (in_grace=False)."""
    captured = []

    async def fake_run_once(engine, settings, passes=0, in_grace=False):
        captured.append(in_grace)
        stop_event.set()

    monkeypatch.setattr(housekeeping, "_run_once", fake_run_once)
    settings = Settings(STARTUP_GRACE_SECONDS=0, HOUSEKEEPING_INTERVAL_SECONDS=1)
    stop_event = asyncio.Event()
    await asyncio.wait_for(housekeeping_loop(None, settings, stop_event), timeout=3.0)

    assert captured == [False]


@pytest.mark.asyncio
async def test_loop_runs_a_pass_on_start_and_then_every_interval(monkeypatch):
    """The grace shield lasts a bounded number of passes, and the first one that
    runs outside it is the one that expires the pre-assigned execution of a worker that has not
    polled again yet. The loop runs one pass at startup and then every
    `HOUSEKEEPING_INTERVAL_SECONDS`, so with a grace of 2 intervals the passes
    fall in grace at ~0 and ~1 interval, and the one at ~2 intervals is already outside."""
    captured = []

    async def fake_run_once(engine, settings, passes=0, in_grace=False):
        captured.append(in_grace)
        if len(captured) == 3:
            stop_event.set()

    monkeypatch.setattr(housekeeping, "_run_once", fake_run_once)
    settings = Settings(STARTUP_GRACE_SECONDS=2, HOUSEKEEPING_INTERVAL_SECONDS=1)
    stop_event = asyncio.Event()
    await asyncio.wait_for(housekeeping_loop(None, settings, stop_event), timeout=10.0)

    assert captured == [True, True, False]


# --- Sweep of expired webauthn_challenges ----------------------------------------


async def _seed_challenges(db_url: str, *, expired: int, live: int) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE webauthn_challenges"))
            for offset, n in ((-60, expired), (300, live)):
                await conn.execute(
                    text(
                        "INSERT INTO webauthn_challenges (challenge, kind, expires_at) "
                        "SELECT '\\x00'::bytea, 'authentication', "
                        "now() + make_interval(secs => :o) "
                        "FROM generate_series(1, :n)"
                    ),
                    {"o": offset, "n": n},
                )
    finally:
        await engine.dispose()


async def _count_challenge_rows(db_url: str) -> tuple[int, int]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE expires_at <= now()), "
                        "count(*) FILTER (WHERE expires_at > now()) FROM webauthn_challenges"
                    )
                )
            ).one()
        return row[0], row[1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_deletes_expired_challenges_and_keeps_live_ones(
    app_client, migrated_db_url: str
) -> None:
    await _seed_challenges(migrated_db_url, expired=5, live=3)
    engine = create_async_engine(migrated_db_url)
    try:
        assert await housekeeping._sweep_expired_challenges(engine) == 5
    finally:
        await engine.dispose()
    assert await _count_challenge_rows(migrated_db_url) == (0, 3)


@pytest.mark.asyncio
async def test_sweep_deletes_in_bounded_batches(app_client, migrated_db_url: str) -> None:
    await _seed_challenges(migrated_db_url, expired=7, live=1)
    engine = create_async_engine(migrated_db_url)
    try:
        # A batch of 2 with a cap of 2 batches: deletes 4 and leaves the rest to the next pass.
        assert (
            await housekeeping._sweep_expired_challenges(engine, batch_size=2, max_batches=2) == 4
        )
        assert await _count_challenge_rows(migrated_db_url) == (3, 1)
        # Without an effective cap it empties what remains.
        assert await housekeeping._sweep_expired_challenges(engine, batch_size=2) == 3
    finally:
        await engine.dispose()
    assert await _count_challenge_rows(migrated_db_url) == (0, 1)


@pytest.mark.asyncio
async def test_run_once_sweeps_challenges_logs_count_and_is_quiet_when_idle(
    app_client, migrated_db_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    await _seed_challenges(migrated_db_url, expired=4, live=1)
    engine = create_async_engine(migrated_db_url)
    try:
        with caplog.at_level(logging.INFO, logger="orcaster_server.housekeeping"):
            await housekeeping._run_once(engine, Settings(), passes=1)
            first = [r for r in caplog.records if "webauthn challenges" in r.getMessage()]
            await housekeeping._run_once(engine, Settings(), passes=1)
            second = [r for r in caplog.records if "webauthn challenges" in r.getMessage()]
    finally:
        await engine.dispose()
    assert [r.getMessage() for r in first] == ["housekeeping: swept 4 expired webauthn challenges"]
    assert len(second) == 1  # the pass with nothing to delete does not log
    assert await _count_challenge_rows(migrated_db_url) == (0, 1)


@pytest.mark.asyncio
async def test_run_once_isolates_the_failure_of_the_challenges_sweep(monkeypatch, caplog):
    """A broken sweep is logged with a traceback and does not cut the rest of the pass short."""
    mocks = _patch_run_once_helpers(monkeypatch)
    mocks["_sweep_expired_challenges"].side_effect = RuntimeError("db down")

    with caplog.at_level("ERROR", logger="orcaster_server.housekeeping"):
        await housekeeping._run_once(engine=None, settings=Settings(), passes=0, in_grace=False)

    mocks["_reconcile_workspace_usage"].assert_awaited_once()
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == [
        "housekeeping: sweep_expired_challenges failed"
    ]


@pytest.mark.asyncio
async def test_run_once_sweeps_challenges_also_during_startup_grace(monkeypatch):
    mocks = _patch_run_once_helpers(monkeypatch)
    await housekeeping._run_once(engine=None, settings=Settings(), passes=1, in_grace=True)
    mocks["_sweep_expired_challenges"].assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_resnapshots_outside_the_grace(monkeypatch):
    """The re-snapshot of the `all-available` tasks waiting for a suitable worker
    runs in the normal pass."""
    mocks = _patch_run_once_helpers(monkeypatch)
    await housekeeping._run_once(engine=None, settings=Settings(), passes=0, in_grace=False)
    mocks["_resnapshot_awaiting_all_available"].assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_does_not_resnapshot_during_the_startup_grace(monkeypatch):
    """Within the grace `status='online'` does not mean "alive": heartbeats
    froze while the server was down and `_mark_offline_workers` is suspended.
    Pre-assigning there hands the task to a dead worker, and its `assigned` row
    on expiry leaves the epoch non-candidate: the task would wait again forever,
    which is exactly the defect this fixes."""
    mocks = _patch_run_once_helpers(monkeypatch)
    await housekeeping._run_once(engine=None, settings=Settings(), passes=0, in_grace=True)
    mocks["_resnapshot_awaiting_all_available"].assert_not_awaited()
