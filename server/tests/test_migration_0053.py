"""Migration 0053: `workers.last_reported_status`.

Availability self-reported by the worker on poll, so that the open-claim
fairness-yield does not yield to a worker that declares itself unavailable
without having an execution (disk gate, `clear_cache` lane). Checks up
(nullable column without default at head), down (it disappears when going back
to 0052) and the ORM model mirror.

No backfill by design: NULL = "unknown" = force yield as before the
migration, so the existing fleet does not change behaviour on deploy.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0052_task_artifacts_collected"

_SQL_COLUMNS = """
    SELECT column_name, data_type, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_name = 'workers'
       AND column_name = 'last_reported_status'
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0053_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    cols = await _columns(postgres_url)
    assert set(cols) == {"last_reported_status"}, cols
    col = cols["last_reported_status"]
    assert col["data_type"] == "text"
    assert col["is_nullable"] == "YES"
    assert col["column_default"] is None, (
        "no default: NULL = unknown = force yield as before 0053",
        col["column_default"],
    )

    # The new column does not touch the domain of `workers.status`.
    check_def = await _status_check_def(postgres_url)
    assert check_def is not None
    for value in ("online", "paused", "offline"):
        assert value in check_def, check_def
    assert "last_reported_status" not in check_def, check_def

    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr
    assert await _columns(postgres_url) == {}

    # Go back to head so other tests in the session are not broken.
    up = _alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, up.stderr
    assert set(await _columns(postgres_url)) == {"last_reported_status"}


def test_model_declares_last_reported_status() -> None:
    """Anti-drift: the ORM model is the documentary mirror of the DDL."""
    from orcaster_server.models import Worker

    assert "last_reported_status" in Worker.__table__.columns
    col = Worker.__table__.columns["last_reported_status"]
    assert col.nullable is True
    assert col.server_default is None
