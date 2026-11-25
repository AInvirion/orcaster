"""Migration 0059: per-worker scope for `task_secrets`.

Checks up (nullable `worker_id` column with an `ON DELETE CASCADE` FK, the
`(workspace_id, name)` UNIQUE replaced by two partial unique indexes, FK
index), both uniqueness rules, cascade deletion with the worker, the downgrade
that fails explicitly if there are worker-scoped rows, the clean downgrade that
restores the original UNIQUE and the ORM model mirror.

Convention since 0060 (which means 0059 is no longer head): each test that
exercises the migration first leaves it as the current revision (`_en_0059`, a
`downgrade` of whatever comes later) and asserts against `_REV`, not `head`;
when done, the `_restaura_head` fixture takes the DB back to `head` for the
other tests in the session. That way the locks and downgrades measured are
those of 0059 and not those of a later migration that runs earlier in the chain.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV = "0058_share_token_ciphertext"
_REV = "0059_task_secrets_worker_scope"

_WIDE_UK = "task_secrets_workspace_name_wide_uk"
_WORKER_UK = "task_secrets_workspace_name_worker_uk"
_WORKER_IDX = "task_secrets_worker_idx"


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


@pytest.fixture
def _restore_head(migrated_db_url: str) -> Iterator[None]:
    """Leave the DB at `head` when done, even if the test fails midway through the chain.

    The tests of this migration move it to 0058/0059; without this, the next
    test in the session would inherit a schema without the later migrations.
    """
    yield
    up = _alembic(["upgrade", "head"], migrated_db_url)
    assert up.returncode == 0, up.stderr


async def _fetch(url: str, sql: str, params: dict | None = None) -> list[dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params or {})).mappings()]
    finally:
        await engine.dispose()


async def _version(url: str) -> list[dict]:
    return await _fetch(url, "SELECT version_num FROM alembic_version")


async def _at_0059(url: str) -> None:
    """Leave 0059 as the current revision: revert the later migrations."""
    down = _alembic(["downgrade", _REV], url)
    assert down.returncode == 0, down.stderr
    assert await _version(url) == [{"version_num": _REV}]


async def _indexes(url: str) -> dict[str, str]:
    rows = await _fetch(
        url,
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'task_secrets'",
    )
    return {r["indexname"]: r["indexdef"] for r in rows}


async def _constraints(url: str) -> dict[str, str]:
    rows = await _fetch(
        url,
        "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'task_secrets'::regclass",
    )
    return {r["conname"]: r["def"] for r in rows}


_SQL_WORKER = """
    INSERT INTO workers (workspace_id, api_key_hash, hostname, os, arch)
    VALUES (:ws, :h, :h, 'linux', 'x86_64') RETURNING id
"""

_SQL_SECRET = """
    INSERT INTO task_secrets (workspace_id, name, ciphertext, fingerprint, worker_id)
    VALUES (:ws, :name, '\\x00'::bytea, 'fp', :wid)
"""


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade", "_restore_head")
async def test_migration_0059_up_uniqueness_cascade_and_down(migrated_db_url: str) -> None:
    url = migrated_db_url
    await _at_0059(url)

    cols = {
        r["column_name"]: r
        for r in await _fetch(
            url,
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = 'task_secrets'",
        )
    }
    assert cols["worker_id"]["data_type"] == "uuid"
    assert cols["worker_id"]["is_nullable"] == "YES"
    assert cols["worker_id"]["column_default"] is None

    cons = await _constraints(url)
    assert "task_secrets_workspace_name_uk" not in cons
    assert any("workers(id) ON DELETE CASCADE" in d for d in cons.values()), cons

    idx = await _indexes(url)
    assert "UNIQUE" in idx[_WIDE_UK] and "(workspace_id, name)" in idx[_WIDE_UK]
    assert "WHERE (worker_id IS NULL)" in idx[_WIDE_UK]
    assert "UNIQUE" in idx[_WORKER_UK]
    assert "(workspace_id, name, worker_id)" in idx[_WORKER_UK]
    assert "WHERE (worker_id IS NOT NULL)" in idx[_WORKER_UK]
    assert "(worker_id)" in idx[_WORKER_IDX] and "UNIQUE" not in idx[_WORKER_IDX]

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            w1 = await conn.execute(text(_SQL_WORKER), {"ws": DEFAULT_WORKSPACE_ID, "h": "w1"})
            w1 = str(w1.scalar_one())
            w2 = await conn.execute(text(_SQL_WORKER), {"ws": DEFAULT_WORKSPACE_ID, "h": "w2"})
            w2 = str(w2.scalar_one())
            # A workspace row and one per worker with the same name coexist.
            for wid in (None, w1, w2):
                await conn.execute(
                    text(_SQL_SECRET), {"ws": DEFAULT_WORKSPACE_ID, "name": "GH", "wid": wid}
                )
        # Second workspace row with the same name: rejected.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(_SQL_SECRET), {"ws": DEFAULT_WORKSPACE_ID, "name": "GH", "wid": None}
                )
        # Second row of the same worker with the same name: rejected.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(_SQL_SECRET), {"ws": DEFAULT_WORKSPACE_ID, "name": "GH", "wid": w1}
                )
        # Deleting the worker deletes its rows, not the workspace's nor another worker's.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM workers WHERE id = :id"), {"id": w1})
        rows = await _fetch(url, "SELECT worker_id::text AS w FROM task_secrets ORDER BY 1")
        assert sorted(r["w"] or "" for r in rows) == sorted(["", w2])

        # Downgrade with a worker row: fails explicitly and names the count.
        down = _alembic(["downgrade", _PREV], url)
        assert down.returncode != 0
        assert "1 worker-scoped" in down.stderr, down.stderr
        assert "worker_id" in {
            r["column_name"]
            for r in await _fetch(
                url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'task_secrets'",
            )
        }

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM task_secrets WHERE worker_id IS NOT NULL"))
    finally:
        await engine.dispose()

    # Clean downgrade: the original UNIQUE is back and the workspace row stays.
    down = _alembic(["downgrade", _PREV], url)
    assert down.returncode == 0, down.stderr
    cons = await _constraints(url)
    assert cons["task_secrets_workspace_name_uk"] == "UNIQUE (workspace_id, name)"
    assert set(await _indexes(url)).isdisjoint({_WIDE_UK, _WORKER_UK, _WORKER_IDX})
    assert len(await _fetch(url, "SELECT 1 FROM task_secrets")) == 1

    # `upgrade _REV` and not `head`: this asserts the up of 0059, not of later ones.
    up = _alembic(["upgrade", _REV], url)
    assert up.returncode == 0, up.stderr
    assert await _version(url) == [{"version_num": _REV}]

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM task_secrets"))
    finally:
        await engine.dispose()


def test_model_declares_worker_id_and_partial_indexes() -> None:
    """Anti-drift: the ORM model mirrors the 0059 DDL."""
    from orcaster_server.task_secrets.models import TaskSecret

    table = TaskSecret.__table__
    col = table.columns["worker_id"]
    assert col.nullable is True
    [fk] = col.foreign_keys
    assert fk.target_fullname == "workers.id"
    assert fk.ondelete == "CASCADE"
    by_name = {i.name: i for i in table.indexes}
    assert set(by_name) >= {_WIDE_UK, _WORKER_UK, _WORKER_IDX}
    assert by_name[_WIDE_UK].unique is True
    assert [c.name for c in by_name[_WIDE_UK].columns] == ["workspace_id", "name"]
    assert str(by_name[_WIDE_UK].dialect_options["postgresql"]["where"]) == "worker_id IS NULL"
    assert by_name[_WORKER_UK].unique is True
    assert [c.name for c in by_name[_WORKER_UK].columns] == ["workspace_id", "name", "worker_id"]
    assert (
        str(by_name[_WORKER_UK].dialect_options["postgresql"]["where"]) == "worker_id IS NOT NULL"
    )
    assert not by_name[_WORKER_IDX].unique
    # The 0056 UNIQUE no longer exists.
    assert all(
        getattr(c, "name", None) != "task_secrets_workspace_name_uk" for c in table.constraints
    )


def _alembic_bg(args: list[str], url: str) -> subprocess.Popen:
    """`alembic` in the background, to run it against an open transaction."""
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    return subprocess.Popen(
        ["uv", "run", "alembic", *args],
        cwd=SERVER_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def _wait_blocked(url: str, proc: subprocess.Popen, *, timeout: float = 60.0) -> None:
    """Wait until another session is waiting on a lock (the blocked migration)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        assert proc.poll() is None, proc.communicate()
        waiting = await _fetch(url, "SELECT 1 FROM pg_locks WHERE NOT granted")
        if waiting:
            return
        await asyncio.sleep(0.1)
    proc.kill()
    pytest.fail("the migration never ended up waiting on a lock")


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade", "_restore_head")
async def test_downgrade_locks_the_table_before_counting(migrated_db_url: str) -> None:
    """A worker-scoped insert not yet committed is invisible to the count.

    The downgrade takes `ACCESS EXCLUSIVE` on `task_secrets` before counting,
    so it waits for that insert, sees it and fails explicitly. Counting first,
    the later `DROP COLUMN` would turn the row into a workspace secret.
    """
    url = migrated_db_url
    await _at_0059(url)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            wid = str(
                (
                    await conn.execute(text(_SQL_WORKER), {"ws": DEFAULT_WORKSPACE_ID, "h": "w1"})
                ).scalar_one()
            )
        async with engine.connect() as writer:
            await writer.begin()
            await writer.execute(
                text(_SQL_SECRET), {"ws": DEFAULT_WORKSPACE_ID, "name": "GH", "wid": wid}
            )
            down = _alembic_bg(["downgrade", _PREV], url)
            await _wait_blocked(url, down)
            await writer.commit()
        _, stderr = down.communicate(timeout=120)
        assert down.returncode != 0, stderr
        assert "1 worker-scoped" in stderr, stderr
        assert await _version(url) == [{"version_num": _REV}]
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM task_secrets"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade", "_restore_head")
async def test_upgrade_takes_the_locks_in_the_order_of_the_poll(migrated_db_url: str) -> None:
    """The poll updates `workers` and then reads `task_secrets` in the same
    transaction. The upgrade locks `workers` before touching `task_secrets`
    (same order), so with a poll in flight it waits instead of deadlocking.
    The other way round (ACCESS EXCLUSIVE on `task_secrets` and then the FK on
    `workers`), the poll and the migration wait for each other: deadlock.
    """
    url = migrated_db_url
    down = _alembic(["downgrade", _PREV], url)
    assert down.returncode == 0, down.stderr
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_SQL_WORKER), {"ws": DEFAULT_WORKSPACE_ID, "h": "w1"})
        async with engine.connect() as poll:
            await poll.begin()
            await poll.execute(text("UPDATE workers SET last_heartbeat_at = now()"))
            # `upgrade _REV` and not `head`: later migrations would take their own
            # locks on `workers` and the measured wait would no longer be 0059's.
            up = _alembic_bg(["upgrade", _REV], url)
            await _wait_blocked(url, up)
            await poll.execute(text("SET LOCAL statement_timeout = '15s'"))
            await poll.execute(text("SELECT count(*) FROM task_secrets"))
            await poll.commit()
        _, stderr = up.communicate(timeout=120)
        assert up.returncode == 0, stderr
        assert await _version(url) == [{"version_num": _REV}]
        [fk] = await _fetch(
            url,
            "SELECT conname, convalidated FROM pg_constraint "
            "WHERE conrelid = 'task_secrets'::regclass AND contype = 'f' "
            "AND confrelid = 'workers'::regclass",
        )
        assert fk == {"conname": "task_secrets_worker_id_fkey", "convalidated": True}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_db_for_downgrade", "_restore_head")
async def test_downgrade_takes_the_locks_in_the_order_of_the_poll(migrated_db_url: str) -> None:
    """Mirror of the upgrade test, for the downgrade.

    Dropping the FK (via `DROP COLUMN worker_id`) also takes `ACCESS EXCLUSIVE`
    on `workers` (measured in `pg_locks`; the ALTER TABLE documentation does not
    detail it). If the downgrade took `task_secrets` first and `workers` after,
    a poll in flight (`UPDATE workers` and then a read of `task_secrets`) and the
    migration would wait for each other. The downgrade locks `workers` first:
    it waits for the poll without holding `task_secrets`, the poll finishes and
    the migration continues.
    """
    url = migrated_db_url
    # With 0059 as the current revision: if the downgrade also ran that of a
    # later migration, the wait on `workers` would be that migration's.
    await _at_0059(url)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_SQL_WORKER), {"ws": DEFAULT_WORKSPACE_ID, "h": "w1"})
        async with engine.connect() as poll:
            await poll.begin()
            await poll.execute(text("UPDATE workers SET last_heartbeat_at = now()"))
            poll_pid = (await poll.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            down = _alembic_bg(["downgrade", _PREV], url)
            await _wait_blocked(url, down)
            # The migration waits on `workers` and does not yet hold `task_secrets`.
            locks = await _fetch(
                url,
                "SELECT relation::regclass::text AS rel, mode, granted FROM pg_locks "
                "WHERE locktype = 'relation' AND pid NOT IN (:poll, pg_backend_pid()) "
                "AND relation IN ('workers'::regclass, 'task_secrets'::regclass)",
                {"poll": poll_pid},
            )
            assert {(r["rel"], r["granted"]) for r in locks} == {("workers", False)}, locks
            await poll.execute(text("SET LOCAL statement_timeout = '15s'"))
            await poll.execute(text("SELECT count(*) FROM task_secrets"))
            await poll.commit()
        _, stderr = down.communicate(timeout=120)
        assert down.returncode == 0, stderr
        assert await _version(url) == [{"version_num": _PREV}]
    finally:
        await engine.dispose()
