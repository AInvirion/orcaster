"""Migration 0047: time-range index for the outcomes hot path.

Creates `tasks_terminal_completed_idx` on `(workspace_id, status, completed_at)`
to enable a range scan of the time filter in `_SQL_TERMINAL_TASKS` /
`_SQL_TERMINAL_VARIANT_COUNTS` and to correct the row estimate that triggers an
expensive nested loop. It also drops the old
`tasks_workspace_status_idx (workspace_id, status)` (migration 0021), which is
subsumed by prefix. Purely additive/idempotent DDL, no data backfill.

Checks up (the new index exists at head, is NOT partial, covers the 3 columns
in order, and the old one no longer exists) and down (exact return to 0046: the
new one disappears and the old one exists again).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SERVER_DIR = Path(__file__).resolve().parent.parent
_REV = "0047_tasks_completed_idx"
_PREV = "0046_task_outcomes"
_INDEX = "tasks_terminal_completed_idx"
_OLD_INDEX = "tasks_workspace_status_idx"


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


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0047_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    # Head (includes 0047): the new index exists...
    assert (
        await _scalar(
            postgres_url,
            "SELECT COUNT(*) FROM pg_indexes WHERE indexname = :n",
            n=_INDEX,
        )
        == 1
    )
    # ...and the old (workspace_id, status) one does not: it is subsumed by prefix.
    assert (
        await _scalar(
            postgres_url,
            "SELECT COUNT(*) FROM pg_indexes WHERE indexname = :n",
            n=_OLD_INDEX,
        )
        == 0
    )

    # It is the right index: btree on (workspace_id, status, completed_at)
    # and NOT partial (no WHERE); it covers the status=ALL case of the hot path.
    indexdef = await _scalar(
        postgres_url,
        "SELECT indexdef FROM pg_indexes WHERE indexname = :n",
        n=_INDEX,
    )
    assert indexdef is not None
    normalized = indexdef.lower()
    assert "on public.tasks" in normalized
    assert "(workspace_id, status, completed_at)" in normalized
    assert " where " not in normalized, f"the index must not be partial: {indexdef}"

    # Down to 0046: the new one disappears and the old one exists again (the
    # downgrade recreates it, identical to 0021).
    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr
    assert (
        await _scalar(
            postgres_url,
            "SELECT COUNT(*) FROM pg_indexes WHERE indexname = :n",
            n=_INDEX,
        )
        == 0
    )
    assert (
        await _scalar(
            postgres_url,
            "SELECT COUNT(*) FROM pg_indexes WHERE indexname = :n",
            n=_OLD_INDEX,
        )
        == 1
    )

    # Go back to head so other tests in the session are not broken: only the
    # new index again, the old one is dropped again.
    up = _alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, up.stderr
    assert (
        await _scalar(
            postgres_url,
            "SELECT COUNT(*) FROM pg_indexes WHERE indexname = :n",
            n=_INDEX,
        )
        == 1
    )
    assert (
        await _scalar(
            postgres_url,
            "SELECT COUNT(*) FROM pg_indexes WHERE indexname = :n",
            n=_OLD_INDEX,
        )
        == 0
    )
