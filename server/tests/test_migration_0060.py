"""Migration 0060: `workers.task_secrets_refused_at`.

Instant of the last secrets refusal for a worker, so that the open-claim
fairness-yield does not yield to a worker that cannot receive the task (a task
whose stored secret is not delivered stays `queued` and leaves no execution
that would move the worker's `max(assigned_at)`). Checks up (nullable column
without default at head), down (it disappears when going back to 0059, without
touching the rest of `workers`) and the ORM model mirror.

No backfill by design: NULL = "never refused" = the predicate does not exclude
the worker, which is the behaviour before the migration; so the existing fleet
does not change behaviour on deploy.
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
_PREV = "0059_task_secrets_worker_scope"

_SQL_COLUMNS = """
    SELECT column_name, data_type, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_name = 'workers'
       AND column_name = 'task_secrets_refused_at'
"""

_SQL_WORKER = """
    INSERT INTO workers (workspace_id, api_key_hash, hostname, os, arch)
    VALUES (:ws, 'mig0060-hash', 'mig0060-host', 'linux', 'x86_64') RETURNING id
"""


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


async def _columns(url: str) -> dict[str, object]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(_SQL_COLUMNS))).mappings().all()
        return {r["column_name"]: r for r in rows}
    finally:
        await engine.dispose()


async def _head(url: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0060_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    cols = await _columns(postgres_url)
    assert set(cols) == {"task_secrets_refused_at"}, cols
    col = cols["task_secrets_refused_at"]
    assert col["data_type"] == "timestamp with time zone", col["data_type"]
    assert col["is_nullable"] == "YES"
    assert col["column_default"] is None, (
        "no default: NULL = never refused = the predicate does not exclude the worker",
        col["column_default"],
    )

    # An earlier row: the worker starts with the marker as NULL (no backfill).
    engine = create_async_engine(postgres_url)
    try:
        async with engine.begin() as conn:
            worker_id = (
                await conn.execute(text(_SQL_WORKER), {"ws": DEFAULT_WORKSPACE_ID})
            ).scalar_one()
            mark = (
                await conn.execute(
                    text("SELECT task_secrets_refused_at FROM workers WHERE id = :id"),
                    {"id": worker_id},
                )
            ).scalar_one()
        assert mark is None

        down = _alembic(["downgrade", _PREV], postgres_url)
        assert down.returncode == 0, down.stderr
        assert await _columns(postgres_url) == {}
        assert await _head(postgres_url) == _PREV
        # The worker survives the downgrade: only the column is dropped.
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT 1 FROM workers WHERE id = :id"), {"id": worker_id})
            ).all()
        assert len(rows) == 1

        # Go back to head so other tests in the session are not broken; the
        # earlier worker keeps the marker as NULL, as before the migration.
        up = _alembic(["upgrade", "head"], postgres_url)
        assert up.returncode == 0, up.stderr
        assert set(await _columns(postgres_url)) == {"task_secrets_refused_at"}
        async with engine.connect() as conn:
            mark = (
                await conn.execute(
                    text("SELECT task_secrets_refused_at FROM workers WHERE id = :id"),
                    {"id": worker_id},
                )
            ).scalar_one()
        assert mark is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM workers WHERE api_key_hash = 'mig0060-hash'"))
        await engine.dispose()


def test_model_declares_task_secrets_refused_at() -> None:
    """Anti-drift: the ORM model is the documentary mirror of the DDL."""
    from orcaster_server.models import Worker

    assert "task_secrets_refused_at" in Worker.__table__.columns
    col = Worker.__table__.columns["task_secrets_refused_at"]
    assert col.nullable is True
    assert col.server_default is None
    assert col.type.timezone is True
