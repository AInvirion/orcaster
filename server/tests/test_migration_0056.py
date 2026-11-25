"""Migration 0056: `task_secrets` table + `admin_actions` CHECK.

Checks up (table, columns, FKs, UNIQUE, new actions and `target_kind`),
down (the table disappears and the CHECKs go back to the 0055 set) and the
ORM model mirror.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0055_workspaces_artifact_quota"
_LITERAL_RE = re.compile(r"'([a-z_]+)'")
_NEW_ACTIONS = {"set_task_secret", "delete_task_secret", "deliver_task_secrets"}


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


async def _fetch(url: str, sql: str, params: dict | None = None) -> list[dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params or {})).mappings()]
    finally:
        await engine.dispose()


async def _columns(url: str) -> dict[str, dict]:
    rows = await _fetch(
        url,
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = 'task_secrets'",
    )
    return {r["column_name"]: r for r in rows}


async def _check_values(url: str, name: str) -> set[str]:
    rows = await _fetch(
        url,
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conname = :n AND conrelid = 'admin_actions'::regclass",
        {"n": name},
    )
    return set(_LITERAL_RE.findall(rows[0]["def"]))


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade")
async def test_migration_0056_up_and_down(migrated_db_url: str) -> None:
    url = migrated_db_url

    cols = await _columns(url)
    assert set(cols) == {
        "id",
        "workspace_id",
        "name",
        "ciphertext",
        "fingerprint",
        "created_at",
        "updated_at",
        "updated_by_user_id",
        # 0059: per-worker scope; this test's DB is at head.
        "worker_id",
    }
    assert cols["ciphertext"]["data_type"] == "bytea"
    assert cols["updated_by_user_id"]["is_nullable"] == "YES"
    for not_null in ("workspace_id", "name", "ciphertext", "fingerprint", "created_at"):
        assert cols[not_null]["is_nullable"] == "NO", not_null
    assert "gen_random_uuid" in (cols["id"]["column_default"] or "")

    fks = await _fetch(
        url,
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'task_secrets'::regclass AND contype = 'f'",
    )
    defs = sorted(r["def"] for r in fks)
    assert any("workspaces(id) ON DELETE CASCADE" in d for d in defs), defs
    assert any("users(id) ON DELETE SET NULL" in d for d in defs), defs

    actions = await _check_values(url, "admin_actions_action_check")
    assert actions >= _NEW_ACTIONS
    # Previous values preserved (a badly copied DROP + recreate would remove them).
    assert {"pause_worker", "mint_share_token", "auto_retry_task"} <= actions
    kinds = await _check_values(url, "admin_actions_target_kind_check")
    assert kinds == {"worker", "task", "pat", "enrollment_token", "share_token", "task_secret"}

    # UNIQUE (workspace_id, name).
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, slug) VALUES (:id, 'Default', 'default') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": DEFAULT_WORKSPACE_ID},
            )
            await conn.execute(text("DELETE FROM task_secrets"))
            await conn.execute(
                text(
                    "INSERT INTO task_secrets (workspace_id, name, ciphertext, fingerprint) "
                    "VALUES (:ws, 'API_TOKEN', '\\x00'::bytea, 'fp')"
                ),
                {"ws": DEFAULT_WORKSPACE_ID},
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO task_secrets (workspace_id, name, ciphertext, fingerprint) "
                        "VALUES (:ws, 'API_TOKEN', '\\x01'::bytea, 'fp2')"
                    ),
                    {"ws": DEFAULT_WORKSPACE_ID},
                )
        # The audit rows inherited from other tests were already emptied by
        # `clean_db_for_downgrade`.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM task_secrets"))
    finally:
        await engine.dispose()

    down = _alembic(["downgrade", _PREV], url)
    assert down.returncode == 0, down.stderr
    assert await _columns(url) == {}
    actions_down = await _check_values(url, "admin_actions_action_check")
    assert actions_down.isdisjoint(_NEW_ACTIONS)
    assert "revoke_share_token" in actions_down
    kinds_down = await _check_values(url, "admin_actions_target_kind_check")
    assert "task_secret" not in kinds_down

    up = _alembic(["upgrade", "head"], url)
    assert up.returncode == 0, up.stderr
    assert "ciphertext" in await _columns(url)


def test_model_declares_task_secrets() -> None:
    """Anti-drift: the ORM model is the documentary mirror of the DDL."""
    from orcaster_server.task_secrets.models import TaskSecret

    cols = TaskSecret.__table__.columns
    assert set(cols.keys()) == {
        "id",
        "workspace_id",
        "name",
        "ciphertext",
        "fingerprint",
        "created_at",
        "updated_at",
        "updated_by_user_id",
        # 0059: per-worker scope; this test's DB is at head.
        "worker_id",
    }
    assert cols["updated_by_user_id"].nullable is True
    assert cols["workspace_id"].nullable is False
