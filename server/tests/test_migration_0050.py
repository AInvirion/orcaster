"""Migration 0050: `tasks.converted_from`.

Provenance marker for `target_mode` after the sticky auto-retry conversion.
Checks up (nullable text column without default at head), down (the column
disappears when going back to 0049) and the ORM model mirror.

No backfill by design: NULL = current behaviour (cumulative count-mode
closing), so every pre-existing row keeps its semantics.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0049_drop_output_columns"

_SQL_COLUMN = """
    SELECT data_type, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_name = 'tasks' AND column_name = 'converted_from'
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


async def _column_row(url: str):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(_SQL_COLUMN))).first()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0050_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    row = await _column_row(postgres_url)
    assert row is not None, "tasks.converted_from should exist at head"
    assert row.data_type == "text"
    assert row.is_nullable == "YES"
    assert row.column_default is None, (
        "no default: NULL = native count (current behaviour)",
        row.column_default,
    )

    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr
    assert await _column_row(postgres_url) is None

    # Go back to head so other tests in the session are not broken.
    up = _alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, up.stderr
    assert await _column_row(postgres_url) is not None


def test_model_declares_converted_from() -> None:
    """Anti-drift: the ORM model is the documentary mirror of the DDL."""
    from orcaster_server.models import Task

    assert "converted_from" in Task.__table__.columns
    col = Task.__table__.columns["converted_from"]
    assert col.nullable is True
    assert col.server_default is None
