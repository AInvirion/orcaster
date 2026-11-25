"""Migration 0061: `task_executions (worker_id, assigned_at)` index.

The fairness-yield of `_SQL_POLL_CLAIM_OPEN_LOCK` compares two
`max(assigned_at)` values per worker. Without an index that starts with
`(worker_id, assigned_at)`, Postgres scans all executions of each worker on
every idle poll. Checks up (the index exists at head with both columns in order
and no WHERE), that the yield's `max()` uses it, down (it disappears when going
back to 0060 without touching `te_worker_idx`) and the ORM model mirror.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0060_workers_secrets_refused_at"
_INDEX = "te_worker_assigned_idx"

# The same subselect as the fairness-yield (`orcaster_server/api/poll.py`).
_SQL_YIELD_MAX = "SELECT max(assigned_at) FROM task_executions WHERE worker_id = :w"


def _alembic(args: list[str], url: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def _scalar(url: str, sql: str, **binds):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), binds)).scalar()
    finally:
        await engine.dispose()


async def _indexdef(url: str, name: str) -> str | None:
    return await _scalar(url, "SELECT indexdef FROM pg_indexes WHERE indexname = :n", n=name)


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0061_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    indexdef = await _indexdef(postgres_url, _INDEX)
    assert indexdef is not None, "the index must exist at head"
    normalized = indexdef.lower()
    assert "on public.task_executions" in normalized
    assert "(worker_id, assigned_at)" in normalized
    assert " where " not in normalized, f"not partial: the yield looks at all rows: {indexdef}"

    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr
    assert await _indexdef(postgres_url, _INDEX) is None
    assert await _indexdef(postgres_url, "te_worker_idx") is not None

    up = _alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, up.stderr
    assert await _indexdef(postgres_url, _INDEX) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_max_assigned_at_of_the_yield_uses_the_index(migrated_db_url: str) -> None:
    """The per-worker `max(assigned_at)` is resolved by reading the index from the end.

    With few rows the planner prefers a seq scan on cost, so seq and bitmap
    scans are turned off: what is validated is that a path through
    `te_worker_assigned_idx` exists. Without the index the planner falls back to
    `te_worker_idx`, which is not ordered by `assigned_at` and forces reading
    all of the worker's rows.
    """
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            worker_id = (
                await conn.execute(
                    text(
                        "INSERT INTO workers (workspace_id, api_key_hash, hostname, os, arch) "
                        "VALUES (:ws, 'mig0061-hash', 'mig0061-host', 'linux', 'x86_64') "
                        "RETURNING id"
                    ),
                    {"ws": DEFAULT_WORKSPACE_ID},
                )
            ).scalar_one()
            await conn.execute(text("SET LOCAL enable_seqscan = off"))
            await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
            plan = "\n".join(
                (
                    await conn.execute(
                        text("EXPLAIN (COSTS OFF) " + _SQL_YIELD_MAX), {"w": worker_id}
                    )
                ).scalars()
            )
            await conn.rollback()
    finally:
        await engine.dispose()

    assert f"Index Only Scan Backward using {_INDEX}" in plan, plan
    assert "Limit" in plan, plan


def test_model_declares_te_worker_assigned_idx() -> None:
    """Anti-drift: the ORM model is the documentary mirror of the DDL."""
    from orcaster_server.models import TaskExecution

    idx = {i.name: i for i in TaskExecution.__table__.indexes}
    assert _INDEX in idx
    assert [c.name for c in idx[_INDEX].columns] == ["worker_id", "assigned_at"]
