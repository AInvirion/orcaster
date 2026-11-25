"""Migration 0054: `workers.free_disk_fraction`.

Free disk fraction self-reported by the worker on poll, input of the
server-side disk gate. Checks up (nullable column without default,
`double precision`, at head), down (it disappears when going back to 0053) and
the ORM model mirror.

No backfill by design: NULL = "unknown" = do not gate / do not exclude / do not
skip, so the server stays inert until workers report the field.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0053_workers_reported_status"

_SQL_COLUMNS = """
    SELECT column_name, data_type, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_name = 'workers'
       AND column_name = 'free_disk_fraction'
"""

# The `status` CHECK must stay untouched: the new column is independent.
_SQL_STATUS_CHECK = """
    SELECT pg_get_constraintdef(oid) AS def
      FROM pg_constraint
     WHERE conname = 'workers_status_chk'
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


async def _status_check_def(url: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(_SQL_STATUS_CHECK))).first()
        return row[0] if row else None
    finally:
        await engine.dispose()


async def _head(url: str) -> str:
    """The revision the DB has applied (`version_num` column)."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT version_num FROM alembic_version"))).first()
        assert row is not None
        return row[0]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0054_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    # The exact head is not pinned: later migrations (0055 onwards) move it.
    # The column below proves that 0054 is applied.
    cols = await _columns(postgres_url)
    assert set(cols) == {"free_disk_fraction"}, cols
    col = cols["free_disk_fraction"]
    assert col["data_type"] == "double precision", col["data_type"]
    assert col["is_nullable"] == "YES"
    assert col["column_default"] is None, (
        "no default: NULL = unknown = do not gate (fail-open)",
        col["column_default"],
    )

    # The new column does not touch the domain of `workers.status`.
    check_def = await _status_check_def(postgres_url)
    assert check_def is not None
    for value in ("online", "paused", "offline"):
        assert value in check_def, check_def
    assert "free_disk_fraction" not in check_def, check_def

    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr
    assert await _columns(postgres_url) == {}
    # `last_reported_status` (0053) is still there after downgrading only 0054.
    assert await _head(postgres_url) == _PREV

    # Go back to head so other tests in the session are not broken.
    up = _alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, up.stderr
    assert set(await _columns(postgres_url)) == {"free_disk_fraction"}


def test_model_declares_free_disk_fraction() -> None:
    """Anti-drift: the ORM model is the documentary mirror of the DDL."""
    from orcaster_server.models import Worker

    assert "free_disk_fraction" in Worker.__table__.columns
    col = Worker.__table__.columns["free_disk_fraction"]
    assert col.nullable is True
    assert col.server_default is None
