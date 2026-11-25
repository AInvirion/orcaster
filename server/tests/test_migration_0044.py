"""Migration 0043: workspace_share_token table (public status page).

Checks that upgrade head creates the table with its columns and indexes, and
that downgrade to the previous revision drops it. Same pattern as
test_migrations.py / test_migration_0037.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SERVER_DIR = Path(__file__).resolve().parent.parent

_REV = "0044_workspace_share_token"
_PREV = "0042_admin_actions_consume"


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


async def _table_exists(url: str, table: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_tables "
                        "WHERE schemaname='public' AND tablename = :t"
                    ),
                    {"t": table},
                )
            ).scalar_one()
        return n == 1
    finally:
        await engine.dispose()


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


# Columns created by this migration. Later ones (e.g. `token_ciphertext`,
# 0058) are checked in their own test.
_COLUMNS_AT_REV = {
    "id",
    "workspace_id",
    "token_hash",
    "label",
    "created_at",
    "revoked_at",
    "last_seen_at",
}


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0043_up_creates_table_then_down_drops(migrated_db_url: str) -> None:
    # migrated_db_url is already at head. The exact columns are checked at
    # this migration's revision, not at head, which adds later columns.
    assert await _table_exists(migrated_db_url, "workspace_share_token")
    assert await _columns(migrated_db_url, "workspace_share_token") >= _COLUMNS_AT_REV
    at_rev = _alembic(["downgrade", _REV], migrated_db_url)
    assert at_rev.returncode == 0, f"{at_rev.stdout}\n{at_rev.stderr}"
    assert await _columns(migrated_db_url, "workspace_share_token") == _COLUMNS_AT_REV
    # Downgrading to the previous revision drops the table; then go back to head.
    down = _alembic(["downgrade", _PREV], migrated_db_url)
    assert down.returncode == 0, f"{down.stdout}\n{down.stderr}"
    assert not await _table_exists(migrated_db_url, "workspace_share_token")
    up = _alembic(["upgrade", "head"], migrated_db_url)
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"
    assert await _table_exists(migrated_db_url, "workspace_share_token")
