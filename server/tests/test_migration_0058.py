"""Migration 0058: `workspace_share_token.token_ciphertext`.

Share-link token encrypted with the task secrets keyring, only to show the URL
again in /admin. Checks up (nullable column without default, `bytea`),
down (it disappears when going back to 0057 and links keep resolving by hash)
and the ORM model mirror.

No backfill by design: for earlier rows only the hash is known.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0057_retired_variant_backfill"

_SQL_COLUMNS = """
    SELECT column_name, data_type, is_nullable, column_default
      FROM information_schema.columns
     WHERE table_name = 'workspace_share_token'
       AND column_name = 'token_ciphertext'
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


# Deadline for the `upgrade` with the table locked: with `lock_timeout` it ends
# in seconds; without it, the ALTER TABLE would wait for the holder, which only
# releases after the `upgrade`.
_BLOCKED_UPGRADE_TIMEOUT_S = 60


def _alembic_bounded(args: list[str], url: str, timeout: float) -> subprocess.CompletedProcess:
    """`_alembic` with a deadline: on expiry, kill `uv` and its alembic child and fail the test.

    `subprocess.run(timeout=...)` only kills the direct process (`uv`); the
    alembic child would stay alive waiting for the lock. That is why it runs in
    its own process group and the whole group is killed.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    proc = subprocess.Popen(
        ["uv", "run", "alembic", *args],
        cwd=SERVER_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        pytest.fail(
            f"alembic {' '.join(args)} did not finish in {timeout}s with the table locked: "
            "is SET LOCAL lock_timeout missing in 0058?"
        )
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


async def _columns(url: str) -> dict[str, object]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(_SQL_COLUMNS))).mappings().all()
        return {r["column_name"]: r for r in rows}
    finally:
        await engine.dispose()


async def _execute(url: str, sql: str, params: dict | None = None) -> list:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _head(url: str) -> str:
    rows = await _execute(url, "SELECT version_num FROM alembic_version")
    assert rows
    return rows[0][0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0058_up_and_down(migrated_db_url: str) -> None:
    postgres_url = migrated_db_url

    cols = await _columns(postgres_url)
    assert set(cols) == {"token_ciphertext"}, cols
    col = cols["token_ciphertext"]
    assert col["data_type"] == "bytea", col["data_type"]
    assert col["is_nullable"] == "YES"
    assert col["column_default"] is None, col["column_default"]

    # A row with ciphertext: the downgrade keeps it (only the column is dropped).
    await _execute(
        postgres_url,
        "INSERT INTO workspace_share_token (workspace_id, token_hash, token_ciphertext) "
        "VALUES (:ws, 'mig0058-hash', '\\x00'::bytea)",
        {"ws": DEFAULT_WORKSPACE_ID},
    )
    try:
        down = _alembic(["downgrade", _PREV], postgres_url)
        assert down.returncode == 0, down.stderr
        assert await _columns(postgres_url) == {}
        assert await _head(postgres_url) == _PREV
        rows = await _execute(
            postgres_url,
            "SELECT 1 FROM workspace_share_token WHERE token_hash = 'mig0058-hash'",
        )
        assert len(rows) == 1

        # Go back to head so other tests in the session are not broken; the
        # earlier row ends up without ciphertext (NULL), like a legacy row.
        up = _alembic(["upgrade", "head"], postgres_url)
        assert up.returncode == 0, up.stderr
        assert set(await _columns(postgres_url)) == {"token_ciphertext"}
        rows = await _execute(
            postgres_url,
            "SELECT token_ciphertext FROM workspace_share_token WHERE token_hash = 'mig0058-hash'",
        )
        assert rows == [(None,)]
    finally:
        await _execute(
            postgres_url, "DELETE FROM workspace_share_token WHERE token_hash = 'mig0058-hash'"
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0058_fails_fast_with_the_table_locked(migrated_db_url: str) -> None:
    """`SET LOCAL lock_timeout`: with another transaction on the table, the
    ALTER TABLE fails within seconds instead of queueing the /status lookups."""
    postgres_url = migrated_db_url
    down = _alembic(["downgrade", _PREV], postgres_url)
    assert down.returncode == 0, down.stderr
    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as holder:
            await holder.execute(text("BEGIN"))
            await holder.execute(text("LOCK TABLE workspace_share_token IN ACCESS SHARE MODE"))
            started = time.monotonic()
            up = await asyncio.to_thread(
                _alembic_bounded, ["upgrade", "head"], postgres_url, _BLOCKED_UPGRADE_TIMEOUT_S
            )
            elapsed = time.monotonic() - started
            await holder.execute(text("ROLLBACK"))
        assert up.returncode != 0
        assert "lock timeout" in up.stderr, up.stderr
        assert elapsed < _BLOCKED_UPGRADE_TIMEOUT_S, elapsed
        assert await _head(postgres_url) == _PREV
    finally:
        await engine.dispose()
        up = _alembic(["upgrade", "head"], postgres_url)
        assert up.returncode == 0, up.stderr
    assert set(await _columns(postgres_url)) == {"token_ciphertext"}


def test_model_declares_token_ciphertext() -> None:
    """Anti-drift: the ORM model is the documentary mirror of the DDL."""
    from orcaster_server.models import WorkspaceShareToken

    assert "token_ciphertext" in WorkspaceShareToken.__table__.columns
    col = WorkspaceShareToken.__table__.columns["token_ciphertext"]
    assert col.nullable is True
    assert col.server_default is None
