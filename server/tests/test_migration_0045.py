"""Migration 0045: variant_kind/variant_label columns.

Checks:
  - head has the columns and the tasks_variant_idx index.
  - the migration does not backfill data: a task inserted before applying
    0045 keeps NULL in the variant_* columns. Tested end-to-end: downgrade to
    0044, insert raw tasks (bypassing _create_task), upgrade to head, verify.
  - downgrade removes the columns.

Same pattern as test_migration_0044.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import example_payload

SERVER_DIR = Path(__file__).resolve().parent.parent

_REV = "0045_tasks_variant"
_PREV = "0044_workspace_share_token"


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


async def _insert_task(conn, payload: dict) -> int:
    row = await conn.execute(
        text(
            """
            INSERT INTO tasks (workspace_id, command, payload,
                               target_mode, target_count, attempt_budget,
                               priority, status, run_at, signed, signature)
            VALUES (:ws, 'run_batch', CAST(:payload AS jsonb),
                    'count', 1, 1,
                    0, CAST('queued' AS task_status), now(),
                    true, decode(repeat('00', 64), 'hex'))
            RETURNING id
            """
        ),
        {"ws": DEFAULT_WORKSPACE_ID, "payload": json.dumps(payload)},
    )
    return row.scalar_one()


async def _columns(url: str, table: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
                    ),
                    {"t": table},
                )
            ).scalars()
        return set(rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_0045_head_has_columns_and_index(migrated_db_url: str) -> None:
    cols = await _columns(migrated_db_url, "tasks")
    assert {"variant_kind", "variant_label"} <= cols
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            n = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = 'tasks_variant_idx'")
                )
            ).scalar_one()
        assert n == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_0045_upgrade_leaves_existing_rows_without_variant(migrated_db_url: str) -> None:
    """There is no data backfill: pre-existing rows stay `other`.

    A raw task is inserted with 0044 applied (without the variant_* columns), with a
    payload the test plugin would recognise, and another without a tool. After
    upgrading to head, both keep the columns as NULL: the migration only adds DDL.
    """
    down = _alembic(["downgrade", _PREV], migrated_db_url)
    assert down.returncode == 0, f"{down.stdout}\n{down.stderr}"

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            ids = [
                await _insert_task(conn, example_payload()),
                await _insert_task(conn, {"commands": ["echo hello"], "requirements": []}),
            ]
    finally:
        await engine.dispose()

    up = _alembic(["upgrade", "head"], migrated_db_url)
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT variant_kind, variant_label FROM tasks "
                        "WHERE id = ANY(CAST(:ids AS bigint[]))"
                    ),
                    {"ids": ids},
                )
            ).all()
    finally:
        await engine.dispose()
    assert len(rows) == len(ids)
    assert all(tuple(r) == (None, None) for r in rows)


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_0045_downgrade_removes_columns(migrated_db_url: str) -> None:
    down = _alembic(["downgrade", _PREV], migrated_db_url)
    assert down.returncode == 0, f"{down.stdout}\n{down.stderr}"
    cols = await _columns(migrated_db_url, "tasks")
    assert "variant_kind" not in cols
    assert "variant_label" not in cols
    # restore head so the shared DB is not left off head
    up = _alembic(["upgrade", "head"], migrated_db_url)
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"
