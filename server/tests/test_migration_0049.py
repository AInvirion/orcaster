"""Migration 0049: physical drop of the deprecated output columns.

Output moved to `output_events`, leaving `task_executions.stdout`,
`.stderr` and `.output_truncated` deprecated (no writers; the last reader
was repointed). This migration physically removes them.

Checks up (the 3 columns do not exist at head), down (exact return to the 0048
schema: they come back with the right type and default; the CONTENT is not
recoverable and that is accepted: it was NULL/false for every execution after
that move) and the mirror anti-drift check: neither the model nor the server's
raw SQL may keep mentioning the dropped columns.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0048_admin_actions_pause_share"
_COLUMNS = ("stdout", "stderr", "output_truncated")

_SQL_COLUMN = """
    SELECT data_type, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_name = 'task_executions' AND column_name = :col
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


async def _column_row(url: str, col: str):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(_SQL_COLUMN), {"col": col})).first()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0049_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    # Head (includes 0049): none of the 3 columns exist.
    for col in _COLUMNS:
        assert await _column_row(postgres_url, col) is None, (
            f"task_executions.{col} should be dropped at head"
        )

    # Down to 0048: the 3 come back with the exact pre-drop schema (0001 for
    # stdout/stderr, 0030 for output_truncated).
    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr

    stdout_row = await _column_row(postgres_url, "stdout")
    stderr_row = await _column_row(postgres_url, "stderr")
    trunc_row = await _column_row(postgres_url, "output_truncated")
    assert stdout_row is not None and stdout_row.data_type == "text"
    assert stdout_row.is_nullable == "YES"
    assert stderr_row is not None and stderr_row.data_type == "text"
    assert stderr_row.is_nullable == "YES"
    assert trunc_row is not None and trunc_row.data_type == "boolean"
    assert trunc_row.is_nullable == "NO"
    assert trunc_row.column_default == "false"

    # Go back to head so other tests in the session are not broken.
    up = _alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, up.stderr
    for col in _COLUMNS:
        assert await _column_row(postgres_url, col) is None


def test_mirrors_without_dropped_columns() -> None:
    """Anti-drift (pattern of test_migration_0048): after the drop, neither the ORM
    model nor the app's raw SQL may mention the removed columns.
    Catches accidental reintroductions without needing a DB."""
    models_src = (SERVER_DIR / "orcaster_server" / "models.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*stdout:", models_src, re.M)
    assert not re.search(r"^\s*stderr:", models_src, re.M)
    assert "output_truncated" not in models_src

    admin_ui_src = (SERVER_DIR / "orcaster_server" / "api" / "admin_ui.py").read_text(
        encoding="utf-8"
    )
    # Column references in raw SQL under ANY alias (the file already uses
    # e/ex/te/ae/ae2 for task_executions; hardcoding the list would let a new
    # alias through). output_events streams such as the 'stderr' literal
    # remain valid; what is forbidden is dotted access.
    hits = re.findall(r"\b\w+\.(?:stdout|stderr|output_truncated)\b", admin_ui_src)
    assert not hits, f"admin_ui.py still references dropped columns: {hits}"
