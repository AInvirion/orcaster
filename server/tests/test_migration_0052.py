"""Migration 0052: `task_artifacts.collected_at` / `collected_note`.

Collection evidence: the housekeeping routine purges the bytes from disk and
marks the row instead of deleting it. Checks up (two nullable columns without
default at head), down (they disappear when going back to 0051) and the ORM
model mirror.

No backfill by design: NULL = artifact available = current behaviour.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0051_task_artifacts_created_idx"

_SQL_COLUMNS = """
    SELECT column_name, data_type, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_name = 'task_artifacts'
       AND column_name IN ('collected_at', 'collected_note')
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0052_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    cols = await _columns(postgres_url)
    assert set(cols) == {"collected_at", "collected_note"}, cols
    assert cols["collected_at"]["data_type"] == "timestamp with time zone"
    assert cols["collected_note"]["data_type"] == "text"
    for name, col in cols.items():
        assert col["is_nullable"] == "YES", name
        assert col["column_default"] is None, (
            "no default: NULL = artifact available (current behaviour)",
            name,
            col["column_default"],
        )

    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr
    assert await _columns(postgres_url) == {}

    # Go back to head so other tests in the session are not broken.
    up = _alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, up.stderr
    assert set(await _columns(postgres_url)) == {"collected_at", "collected_note"}


def test_model_declares_collection_columns() -> None:
    """Anti-drift: the ORM model is the documentary mirror of the DDL."""
    from orcaster_server.models import TaskArtifact

    for name in ("collected_at", "collected_note"):
        assert name in TaskArtifact.__table__.columns
        col = TaskArtifact.__table__.columns[name]
        assert col.nullable is True, name
        assert col.server_default is None, name
