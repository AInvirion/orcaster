"""Migration 0046: outcome columns + UPDATE cnf->completed/no_fingerprint
+ rebuild of both enums without 'completed_no_fingerprint' + partial index.

Checks up (cnf data seeded at 0045 becomes completed+outcome; the value
disappears from both enums; the index exists) and down (exact return to the
0045 state: cnf rows restored, outcome columns removed).
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
_REV = "0046_task_outcomes"
_PREV = "0045_tasks_variant"


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


async def _exec(url: str, sql: str, **binds):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            return await conn.execute(text(sql), binds)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0046_up_and_down(migrated_db_url: str) -> None:
    # `migrated_db_url` is session-scoped and guarantees the DB starts at
    # head; to seed the 0045 state (with the cnf value in both enums) an
    # explicit DOWNGRADE is needed, not an upgrade: an upgrade to an already
    # applied revision is a no-op and would leave the DB at head (without the
    # enum value), breaking the raw INSERT below when this test runs in the
    # same session as others that already touched `migrated_db_url`.
    postgres_url = migrated_db_url
    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr

    # Seed a cnf task with a cnf execution (raw SQL, bypassing the app).
    task_id = (
        await _exec(
            postgres_url,
            """
        INSERT INTO tasks (workspace_id, command, payload, target_mode, target_count,
                           attempt_budget, priority, status, run_at, signed, signature)
        VALUES (:ws, 'run_batch', '{}'::jsonb, 'count', 1, 1, 0,
                CAST('completed_no_fingerprint' AS task_status), now(), true,
                decode(repeat('00', 64), 'hex'))
        RETURNING id
    """,
            ws=DEFAULT_WORKSPACE_ID,
        )
    ).scalar_one()
    worker_id = (
        await _exec(
            postgres_url,
            """
        INSERT INTO workers (workspace_id, hostname, os, arch, api_key_hash)
        VALUES (:ws, 'w-0046', 'linux', 'x86_64', decode(repeat('11', 32), 'hex'))
        RETURNING id
    """,
            ws=DEFAULT_WORKSPACE_ID,
        )
    ).scalar_one()
    await _exec(
        postgres_url,
        """
        INSERT INTO task_executions (task_id, worker_id, status, exit_code)
        VALUES (:tid, :wid, CAST('completed_no_fingerprint' AS execution_status), 3)
    """,
        tid=task_id,
        wid=worker_id,
    )

    up = _alembic(["upgrade", _REV], postgres_url)
    assert up.returncode == 0, up.stderr

    # Migrated rows.
    assert (
        await _scalar(postgres_url, "SELECT status::text FROM tasks WHERE id = :id", id=task_id)
        == "completed"
    )
    assert (
        await _scalar(postgres_url, "SELECT outcome FROM tasks WHERE id = :id", id=task_id)
        == "no_fingerprint"
    )
    assert (
        await _scalar(
            postgres_url, "SELECT outcome FROM task_executions WHERE task_id = :id", id=task_id
        )
        == "no_fingerprint"
    )
    # The value disappeared from BOTH enums.
    for enum_name in ("task_status", "execution_status"):
        vals = await _scalar(
            postgres_url,
            """
            SELECT COUNT(*) FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
             WHERE t.typname = :n AND e.enumlabel = 'completed_no_fingerprint'
        """,
            n=enum_name,
        )
        assert vals == 0, enum_name
    # Partial index present.
    assert (
        await _scalar(
            postgres_url, "SELECT COUNT(*) FROM pg_indexes WHERE indexname = 'tasks_outcome_idx'"
        )
        == 1
    )

    # Down: restores cnf and removes the columns.
    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr
    assert (
        await _scalar(postgres_url, "SELECT status::text FROM tasks WHERE id = :id", id=task_id)
        == "completed_no_fingerprint"
    )
    cols = await _scalar(
        postgres_url,
        """
        SELECT COUNT(*) FROM information_schema.columns
         WHERE table_name = 'tasks' AND column_name = 'outcome'
    """,
    )
    assert cols == 0
    # Go back to head so other tests in the session are not broken.
    up = _alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, up.stderr
