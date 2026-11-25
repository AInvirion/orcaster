"""Migration 0057: retired data step, no-op.

The revision is kept only to preserve the alembic chain. Checks:
  - the revision id, its ``down_revision`` and that 0058 revises it;
  - that ``upgrade`` 0056 -> 0057 and ``downgrade`` 0057 -> 0056 change no data:
    neither rows without a variant nor rows that already have one.

Same pattern as test_migration_0045.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import EXAMPLE_KIND, example_payload

SERVER_DIR = Path(__file__).resolve().parent.parent

_REV = "0057_retired_variant_backfill"
_PREV = "0056_task_secrets"
_NEXT = "0058_share_token_ciphertext"


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


async def _snapshot(url: str, ids: list[int]) -> list[tuple]:
    """Rows `(id, payload, variant_kind, variant_label)` of the given tasks, by id."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, payload, variant_kind, variant_label FROM tasks "
                        "WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"
                    ),
                    {"ids": ids},
                )
            ).all()
    finally:
        await engine.dispose()
    return [tuple(r) for r in rows]


def test_0057_revision_keeps_the_chain() -> None:
    """The retired revision stays in the chain between 0056 and 0058."""
    script = ScriptDirectory(str(SERVER_DIR / "orcaster_server" / "migrations"))
    rev = script.get_revision(_REV)
    assert rev is not None
    assert rev.down_revision == _PREV
    nxt = script.get_revision(_NEXT)
    assert nxt is not None
    assert nxt.down_revision == _REV


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_0057_upgrade_and_downgrade_change_no_data(migrated_db_url: str) -> None:
    down = _alembic(["downgrade", _PREV], migrated_db_url)
    assert down.returncode == 0, f"{down.stdout}\n{down.stderr}"

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            without_variant = await _insert_task(conn, example_payload())
            with_variant = await _insert_task(conn, example_payload(version="1.10.0"))
            await conn.execute(
                text(
                    "UPDATE tasks SET variant_kind = :kind, variant_label = '1.10.0' WHERE id = :id"
                ),
                {"kind": EXAMPLE_KIND, "id": with_variant},
            )
    finally:
        await engine.dispose()
    ids = [without_variant, with_variant]

    before = await _snapshot(migrated_db_url, ids)
    assert [r[2:] for r in before] == [(None, None), (EXAMPLE_KIND, "1.10.0")]

    up = _alembic(["upgrade", _REV], migrated_db_url)
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"
    assert await _snapshot(migrated_db_url, ids) == before

    down = _alembic(["downgrade", _PREV], migrated_db_url)
    assert down.returncode == 0, f"{down.stdout}\n{down.stderr}"
    assert await _snapshot(migrated_db_url, ids) == before

    # Restore head so the shared DB is not left off head.
    up = _alembic(["upgrade", "head"], migrated_db_url)
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"
    assert await _snapshot(migrated_db_url, ids) == before
