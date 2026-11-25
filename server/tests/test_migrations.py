"""Migration tests: upgrade head + downgrade base, against an ephemeral Postgres.

Goals:
- `alembic upgrade head` applies the complete schema DDL.
- `alembic downgrade base` reverts cleanly.
- The 4 tables and the 2 ENUMs exist after the upgrade and disappear after the downgrade.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

SERVER_DIR = Path(__file__).resolve().parent.parent

# All tests in this module do `downgrade base`: without rows inherited from
# earlier tests, the CHECKs restored by the downgrades do not fail depending on
# execution order (see `clean_db_for_downgrade` in conftest).
pytestmark = pytest.mark.usefixtures("clean_db_for_downgrade")

# After the unified poll endpoint migrations (0007/0008/0009), `worker_commands`
# no longer exists at head: 0009 drops it as a point of no return. The 4 core
# tables are workers/tasks/task_executions/admin_actions.
EXPECTED_TABLES = {
    "workers",
    "tasks",
    "task_executions",
    "admin_actions",
    "workspaces",
    "workspace_members",
    "worker_enrollment_tokens",
    "user_active_workspace",
    # Migration 0031: execution output events.
    "output_events",
    # Migration 0035: metadata of artifacts produced by the task.
    "task_artifacts",
    # Migration 0036: aggregate artifact byte counter per workspace.
    "workspace_artifact_usage",
    # Migration 0043: usage history of multi-use enrollment tokens.
    "worker_enrollment_token_uses",
}
# Table that must NOT exist at head (dropped by 0009_drop_worker_commands).
DROPPED_TABLES = {"worker_commands"}
EXPECTED_ENUMS = {"task_status", "execution_status"}


def _run_alembic(args: list[str], database_url: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def _list_tables(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            )
            return {row[0] for row in result.fetchall()}
    finally:
        await engine.dispose()


async def _list_enums(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT t.typname FROM pg_type t
                    JOIN pg_namespace n ON n.oid = t.typnamespace
                    WHERE t.typtype = 'e' AND n.nspname = 'public'
                    """
                )
            )
            return {row[0] for row in result.fetchall()}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_then_downgrade(postgres_url: str) -> None:
    up = _run_alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, f"upgrade head failed:\nSTDOUT:\n{up.stdout}\nSTDERR:\n{up.stderr}"

    tables = await _list_tables(postgres_url)
    assert EXPECTED_TABLES.issubset(tables), (
        f"Missing tables after upgrade. Expected {EXPECTED_TABLES}, found {tables}"
    )
    assert tables.isdisjoint(DROPPED_TABLES), (
        f"Tables that should be dropped at head are still present: {tables & DROPPED_TABLES}"
    )

    enums = await _list_enums(postgres_url)
    assert EXPECTED_ENUMS.issubset(enums), (
        f"Missing enums after upgrade. Expected {EXPECTED_ENUMS}, found {enums}"
    )

    down = _run_alembic(["downgrade", "base"], postgres_url)
    assert down.returncode == 0, (
        f"downgrade base failed:\nSTDOUT:\n{down.stdout}\nSTDERR:\n{down.stderr}"
    )

    tables_after = await _list_tables(postgres_url)
    assert tables_after.isdisjoint(EXPECTED_TABLES), (
        f"Tables left after downgrade: {tables_after & EXPECTED_TABLES}"
    )

    enums_after = await _list_enums(postgres_url)
    assert enums_after.isdisjoint(EXPECTED_ENUMS), (
        f"Enums left after downgrade: {enums_after & EXPECTED_ENUMS}"
    )


@pytest.mark.asyncio
async def test_0008_migrates_worker_commands_data(postgres_url: str) -> None:
    """Data in worker_commands survives migration 0008 -> tasks + task_executions.

    Flow:
    1. upgrade to 0007 (new schema, worker_commands still alive).
    2. seed 3 worker_commands covering the 3 states (queued, delivered, acked).
    3. upgrade to 0008.
    4. check that 3 tasks with delivery_mode='push' appear and that the mapping
       of status / signature / idempotency_key is correct.
    5. clean up via downgrade to base.
    """
    # 1. Base schema + new columns, without touching data yet.
    up = _run_alembic(["upgrade", "0007_schema_poll_endpoint"], postgres_url)
    assert up.returncode == 0, f"upgrade 0007 failed:\n{up.stdout}\n{up.stderr}"

    # 2. Insert 1 worker and 3 worker_commands in different states.
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.begin() as conn:
            worker_id_row = await conn.execute(
                text(
                    """
                    INSERT INTO workers (api_key_hash, hostname, os, arch)
                    VALUES ('hash-test-0008', 'host-test', 'linux', 'x86_64')
                    RETURNING id;
                    """
                )
            )
            worker_id = worker_id_row.scalar_one()

            # cmd1 - pending: neither delivered nor acked
            # cmd2 - delivered, not acked
            # cmd3 - acked ok
            # SQLAlchemy `text()` interprets `:foo` as a bind parameter, so
            # JSON literals containing `:` cannot go inline. We pass the
            # payloads / ack_result as parameters with a CAST to jsonb.
            await conn.execute(
                text(
                    """
                    INSERT INTO worker_commands
                        (uuid, worker_id, command, payload, signature, expires_at,
                         issued_at, delivered_at, acked_at, ack_status, ack_result)
                    VALUES
                        (gen_random_uuid(), :wid, 'run_batch',
                         CAST(:p1 AS jsonb),
                         '\\x01'::bytea, now() + interval '5 min',
                         now() - interval '1 min', NULL, NULL, NULL, NULL),
                        (gen_random_uuid(), :wid, 'self_update',
                         CAST(:p2 AS jsonb),
                         '\\x02'::bytea, now() + interval '5 min',
                         now() - interval '2 min', now() - interval '90 sec',
                         NULL, NULL, NULL),
                        (gen_random_uuid(), :wid, 'install_dep',
                         CAST(:p3 AS jsonb),
                         '\\x03'::bytea, now() + interval '5 min',
                         now() - interval '3 min', now() - interval '2 min',
                         now() - interval '1 min', 'ok', CAST(:r3 AS jsonb));
                    """
                ),
                {
                    "wid": worker_id,
                    "p1": '{"args":["echo","a"]}',
                    "p2": "{}",
                    "p3": '{"name":"requests"}',
                    "r3": '{"installed":true}',
                },
            )
    finally:
        await engine.dispose()

    # 3. Apply the data migration.
    up_data = _run_alembic(["upgrade", "0008_data_migrate_wc"], postgres_url)
    assert up_data.returncode == 0, f"upgrade 0008 failed:\n{up_data.stdout}\n{up_data.stderr}"

    # 4. Check the mapping.
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            total = (
                await conn.execute(text("SELECT count(*) FROM tasks WHERE delivery_mode='push'"))
            ).scalar_one()
            assert total == 3, f"expected 3 push tasks, found {total}"

            # Command mapping: '__' + wc.command + '__'
            commands = (
                await conn.execute(
                    text("SELECT command FROM tasks WHERE delivery_mode='push' ORDER BY command")
                )
            ).fetchall()
            cmd_set = {row[0] for row in commands}
            assert cmd_set == {"__install_dep__", "__run_batch__", "__self_update__"}, (
                f"unexpected commands: {cmd_set}"
            )

            # Status mapping
            statuses = dict(
                (
                    await conn.execute(
                        text("SELECT command, status::text FROM tasks WHERE delivery_mode='push'")
                    )
                ).fetchall()
            )
            assert statuses["__run_batch__"] == "queued"
            assert statuses["__self_update__"] == "dispatching"
            assert statuses["__install_dep__"] == "completed"

            # signed + signature present
            unsigned = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM tasks "
                        "WHERE delivery_mode='push' "
                        "  AND (signed=false OR signature IS NULL)"
                    )
                )
            ).scalar_one()
            assert unsigned == 0, "all migrated tasks must carry signed=true + signature"

            # idempotency_key NOT NULL on all of them
            no_key = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM tasks "
                        "WHERE delivery_mode='push' AND idempotency_key IS NULL"
                    )
                )
            ).scalar_one()
            assert no_key == 0, "idempotency_key must survive the migration"

            # task_executions: 2 rows (those with delivered_at)
            exec_count = (
                await conn.execute(text("SELECT count(*) FROM task_executions"))
            ).scalar_one()
            assert exec_count == 2, (
                f"expected 2 task_executions (cmd delivered+acked), found {exec_count}"
            )

            # task_executions of the acked-ok one must be 'completed'
            ok_status = (
                await conn.execute(
                    text(
                        """
                        SELECT te.status::text FROM task_executions te
                        JOIN tasks t ON t.id = te.task_id
                        WHERE t.command = '__install_dep__'
                        """
                    )
                )
            ).scalar_one()
            assert ok_status == "completed"

            # task_executions of the delivered-not-acked one must be 'running'
            run_status = (
                await conn.execute(
                    text(
                        """
                        SELECT te.status::text FROM task_executions te
                        JOIN tasks t ON t.id = te.task_id
                        WHERE t.command = '__self_update__'
                        """
                    )
                )
            ).scalar_one()
            assert run_status == "running"
    finally:
        await engine.dispose()

    # 5. Cleanup: downgrade to base so the next test is not contaminated.
    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


@pytest.mark.asyncio
async def test_0021_backfills_existing_rows_into_default_workspace(postgres_url: str) -> None:
    """The 0021 backfill leaves pre-existing workers/tasks rows in `Default`.

    Flow:
    1. upgrade to 0020 (workers/tasks without workspace_id yet).
    2. seed 1 pre-existing worker and 1 pre-existing task.
    3. upgrade to 0021.
    4. assert: both rows have workspace_id == id of the workspace with slug='default',
       and the columns became NOT NULL.
    5. cleanup via downgrade base.
    """
    # 1. Schema up to 0020 (before adding workspace_id).
    up = _run_alembic(["upgrade", "0020_auth_tables"], postgres_url)
    assert up.returncode == 0, f"upgrade 0020 failed:\n{up.stdout}\n{up.stderr}"

    # 2. Insert 1 pre-existing worker and 1 pre-existing task.
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO workers (api_key_hash, hostname, os, arch)
                    VALUES ('hash-test-0021', 'host-test-0021', 'linux', 'x86_64');
                    """
                )
            )
            # tasks_signed_requires_signature_chk: signed=true requires signature NOT NULL.
            await conn.execute(
                text(
                    """
                    INSERT INTO tasks (signed, signature)
                    VALUES (true, '\\x00'::bytea);
                    """
                )
            )
    finally:
        await engine.dispose()

    # 3. Apply the migration with backfill.
    up_ws = _run_alembic(["upgrade", "0021_workspaces"], postgres_url)
    assert up_ws.returncode == 0, f"upgrade 0021 failed:\n{up_ws.stdout}\n{up_ws.stderr}"

    # 4. Check backfill + NOT NULL.
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            default_id = (
                await conn.execute(text("SELECT id FROM workspaces WHERE slug = 'default'"))
            ).scalar_one()

            worker_ws = (await conn.execute(text("SELECT workspace_id FROM workers"))).scalar_one()
            assert str(worker_ws) == str(default_id), (
                f"worker backfill: expected {default_id}, found {worker_ws}"
            )

            task_ws = (await conn.execute(text("SELECT workspace_id FROM tasks"))).scalar_one()
            assert str(task_ws) == str(default_id), (
                f"task backfill: expected {default_id}, found {task_ws}"
            )

            # Columns must end up NOT NULL (is_nullable = 'NO').
            nullability = dict(
                (
                    await conn.execute(
                        text(
                            """
                            SELECT table_name, is_nullable
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND column_name = 'workspace_id'
                              AND table_name IN ('workers', 'tasks')
                            """
                        )
                    )
                ).fetchall()
            )
            assert nullability.get("workers") == "NO", "workers.workspace_id must be NOT NULL"
            assert nullability.get("tasks") == "NO", "tasks.workspace_id must be NOT NULL"
    finally:
        await engine.dispose()

    # 5. Cleanup.
    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


@pytest.mark.asyncio
async def test_workspace_models_import_and_query(postgres_url: str) -> None:
    """The workspaces ORM models import and can query the table.

    After upgrade head, `SELECT` on `Workspace` returns the Default row
    (slug='default'). Cleans up with downgrade base.
    """
    from sqlalchemy import select

    from orcaster_server.workspaces.models import (  # noqa: F401  (import is part of the test)
        UserActiveWorkspace,
        WorkerEnrollmentToken,
        Workspace,
        WorkspaceMember,
    )

    up = _run_alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, f"upgrade head failed:\n{up.stdout}\n{up.stderr}"

    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(select(Workspace).where(Workspace.slug == "default"))).all()
            assert len(rows) == 1, f"expected 1 Default workspace, found {len(rows)}"
            assert rows[0].slug == "default"
            assert rows[0].name == "Default"
    finally:
        await engine.dispose()

    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


@pytest.mark.asyncio
async def test_concurrent_upgrade_serialized_by_advisory_lock(postgres_url: str) -> None:
    """`alembic upgrade head` is serialised with a Postgres advisory lock.

    Regression for the boot crash with `uvicorn --workers N`: each worker ran
    `upgrade head` at startup and two at once collided (DuplicateColumnError from
    an ADD COLUMN applied by the one that won the race). `env.py` now takes
    `pg_advisory_xact_lock(MIGRATION_LOCK_KEY)` at the start of the transaction.

    Deterministic design (not relying on fine timing): we hold the SAME
    advisory lock from a separate connection and launch `upgrade head` in a
    separate process. While the lock is held, the upgrade MUST block (not
    finish). Once released, it completes with returncode 0. Without the lock in
    env.py the upgrade would finish immediately and the `wait_for` would not
    time out, so the test fails.
    """
    import asyncio

    from orcaster_server.migration_lock import MIGRATION_LOCK_KEY

    # Clean state and, incidentally, a warm-up of `uv run alembic` (it caches the
    # environment so a cold start is not mistaken for "blocked by the lock").
    down = _run_alembic(["downgrade", "base"], postgres_url)
    assert down.returncode == 0, down.stderr

    env = os.environ.copy()
    env["DATABASE_URL"] = postgres_url

    # 1) Hold the advisory lock from an AUTOCOMMIT connection (session-level:
    #    it persists across statements until unlock/close, not tied to a txn).
    holder = create_async_engine(postgres_url, poolclass=None)
    conn = await holder.connect()
    await conn.execution_options(isolation_level="AUTOCOMMIT")
    await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
    try:
        # 2) Launch `upgrade head` in a separate process (like another uvicorn worker).
        proc = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "alembic",
            "upgrade",
            "head",
            cwd=str(SERVER_DIR),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # 3) With the lock held, the upgrade must stay BLOCKED (not finish).
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=6)
        assert proc.returncode is None, "the upgrade must not complete while the lock is held"
    finally:
        # 4) Release the lock, so the upgrade can proceed.
        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
        await conn.close()
        await holder.dispose()

    # 5) After the release, it completes without error.
    out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode == 0, (
        f"upgrade head should complete after the lock is released:\n"
        f"STDOUT:\n{out.decode()}\nSTDERR:\n{err.decode()}"
    )

    # Cleanup.
    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


@pytest.mark.asyncio
async def test_upgrade_is_idempotent_after_downgrade(postgres_url: str) -> None:
    """Check that the chain can be applied twice (clean state after downgrade)."""
    up1 = _run_alembic(["upgrade", "head"], postgres_url)
    assert up1.returncode == 0, up1.stderr

    down = _run_alembic(["downgrade", "base"], postgres_url)
    assert down.returncode == 0, down.stderr

    up2 = _run_alembic(["upgrade", "head"], postgres_url)
    assert up2.returncode == 0, f"second upgrade head failed:\n{up2.stderr}"

    # Cleanup so no state is left between tests if the order changes.
    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


# Literal of the Default workspace id (self-contained in migrations 0022/0023).
# Current equivalent: orcaster_server.workspaces.constants.DEFAULT_WORKSPACE_ID.
_DEFAULT_WS_ID = "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_0022_drops_workspace_id_default(postgres_url: str) -> None:
    """0022 drops the column DEFAULT of workspace_id on workers and tasks.

    Flow:
    1. upgrade to 0021 (workspace_id already exists, with a DEFAULT to the Default workspace).
    2. seed 1 worker and 1 task WITHOUT workspace_id; the DEFAULT lands them in Default.
    3. assert: both rows were stamped with Default (proves the DEFAULT existed).
    4. upgrade to 0022 (DROP DEFAULT).
    5. assert: an INSERT without workspace_id now FAILS (NOT NULL without DEFAULT).
    6. cleanup via downgrade base.
    """
    from sqlalchemy.exc import IntegrityError

    # 1. Schema up to 0021 (DEFAULT in force).
    up = _run_alembic(["upgrade", "0021_workspaces"], postgres_url)
    assert up.returncode == 0, f"upgrade 0021 failed:\n{up.stdout}\n{up.stderr}"

    # 2. Seed without workspace_id: the 0021 DEFAULT must fill it in.
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO workers (api_key_hash, hostname, os, arch) "
                    "VALUES ('hash-0022-seed', 'host-0022-seed', 'linux', 'x86_64');"
                )
            )
            await conn.execute(
                text("INSERT INTO tasks (signed, signature) VALUES (true, '\\x00'::bytea);")
            )
    finally:
        await engine.dispose()

    # 3. The seed rows landed in Default (the DEFAULT existed at 0021).
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            default_id = (
                await conn.execute(text("SELECT id FROM workspaces WHERE slug = 'default'"))
            ).scalar_one()
            assert str(default_id) == _DEFAULT_WS_ID
            worker_ws = (await conn.execute(text("SELECT workspace_id FROM workers"))).scalar_one()
            task_ws = (await conn.execute(text("SELECT workspace_id FROM tasks"))).scalar_one()
            assert str(worker_ws) == _DEFAULT_WS_ID
            assert str(task_ws) == _DEFAULT_WS_ID
    finally:
        await engine.dispose()

    # 4. Aplicar 0022 (DROP DEFAULT).
    up22 = _run_alembic(["upgrade", "0022_drop_workspace_id_default"], postgres_url)
    assert up22.returncode == 0, f"upgrade 0022 failed:\n{up22.stdout}\n{up22.stderr}"

    # 5. An INSERT without workspace_id now violates NOT NULL (no DEFAULT anymore).
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO workers (api_key_hash, hostname, os, arch) "
                        "VALUES ('hash-0022-fail', 'host-0022-fail', 'linux', 'x86_64');"
                    )
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO tasks (signed, signature) VALUES (true, '\\x00'::bytea);")
                )
    finally:
        await engine.dispose()

    # 6. Cleanup.
    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


@pytest.mark.asyncio
async def test_0023_admin_actions_workspace_id_backfill_and_not_null(postgres_url: str) -> None:
    """0023 adds admin_actions.workspace_id, backfills to Default and enforces NOT NULL.

    Flow:
    1. upgrade to 0022 (admin_actions still WITHOUT a workspace_id column).
    2. seed 1 pre-existing admin_actions row.
    3. upgrade to 0023.
    4. assert: the row was backfilled to Default; the column is NOT NULL;
       the `admin_actions_workspace_ts_idx` index exists; an INSERT without
       workspace_id fails (NOT NULL without DEFAULT).
    5. cleanup via downgrade base.
    """
    from sqlalchemy.exc import IntegrityError

    # 1. Schema up to 0022 (admin_actions without workspace_id).
    up = _run_alembic(["upgrade", "0022_drop_workspace_id_default"], postgres_url)
    assert up.returncode == 0, f"upgrade 0022 failed:\n{up.stdout}\n{up.stderr}"

    # 2. Seed a pre-existing audit row (without workspace_id: the column does
    #    not exist yet).
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_actions (action, target_kind, target_id) "
                    "VALUES ('cancel_task', 'task', '42');"
                )
            )
    finally:
        await engine.dispose()

    # 3. Apply 0023 (ADD COLUMN + backfill + NOT NULL + index).
    up23 = _run_alembic(["upgrade", "0023_admin_actions_workspace_id"], postgres_url)
    assert up23.returncode == 0, f"upgrade 0023 failed:\n{up23.stdout}\n{up23.stderr}"

    # 4. Backfill + NOT NULL + index + rejection of an INSERT without scope.
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            ws = (await conn.execute(text("SELECT workspace_id FROM admin_actions"))).scalar_one()
            assert str(ws) == _DEFAULT_WS_ID, f"backfill: expected Default, found {ws}"

            nullable = (
                await conn.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='admin_actions' "
                        "AND column_name='workspace_id'"
                    )
                )
            ).scalar_one()
            assert nullable == "NO", "admin_actions.workspace_id must be NOT NULL"

            idx = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM pg_indexes WHERE schemaname='public' "
                        "AND indexname='admin_actions_workspace_ts_idx'"
                    )
                )
            ).scalar_one_or_none()
            assert idx == 1, "the admin_actions_workspace_ts_idx index is missing"
    finally:
        await engine.dispose()

    # An INSERT without workspace_id fails (NOT NULL, no DEFAULT).
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO admin_actions (action, target_kind, target_id) "
                        "VALUES ('cancel_task', 'task', '43');"
                    )
                )
    finally:
        await engine.dispose()

    # 5. Cleanup.
    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


@pytest.mark.asyncio
async def test_0031_output_events_schema(postgres_url: str) -> None:
    """0031 creates output_events with the right columns and a unique index (execution_id, seq).

    Flow:
    1. upgrade head.
    2. assert: the output_events table exists with the required columns.
    3. assert: the unique index uq_output_events_execution_seq exists.
    4. assert: the stream CHECK (stdout/stderr) exists in pg_constraint.
    5. downgrade base: the table disappears cleanly.
    """
    # 1. Upgrade to head.
    up = _run_alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, f"upgrade head failed:\n{up.stdout}\n{up.stderr}"

    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            # 2. The required columns exist in output_events.
            cols = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = 'output_events'
                            """
                        )
                    )
                ).fetchall()
            }
            for expected_col in ("id", "execution_id", "seq", "ts", "stream", "text"):
                assert expected_col in cols, (
                    f"missing column '{expected_col}' in output_events; columns: {cols}"
                )

            # 3. The unique index (execution_id, seq) exists.
            idx = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM pg_indexes "
                        "WHERE schemaname='public' "
                        "  AND indexname='uq_output_events_execution_seq'"
                    )
                )
            ).scalar_one_or_none()
            assert idx == 1, "the unique index uq_output_events_execution_seq is missing"

            # 4. The stream CHECK (stdout/stderr) is present in pg_constraint.
            check_exists = (
                await conn.execute(
                    text(
                        """
                        SELECT 1 FROM pg_constraint c
                        JOIN pg_class r ON r.oid = c.conrelid
                        WHERE r.relname = 'output_events'
                          AND c.contype = 'c'
                          AND pg_get_constraintdef(c.oid) LIKE '%stdout%'
                          AND pg_get_constraintdef(c.oid) LIKE '%stderr%'
                        """
                    )
                )
            ).scalar_one_or_none()
            assert check_exists == 1, (
                "the stream CHECK constraint (stdout/stderr) is missing on output_events"
            )
    finally:
        await engine.dispose()

    # 5. Downgrade base: output_events must disappear.
    down = _run_alembic(["downgrade", "base"], postgres_url)
    assert down.returncode == 0, f"downgrade base failed:\n{down.stdout}\n{down.stderr}"

    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.connect() as conn:
            tables = {
                row[0]
                for row in (
                    await conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                    )
                ).fetchall()
            }
            assert "output_events" not in tables, "output_events must be dropped on downgrade"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_0034_clear_worker_cache_action_check(postgres_url: str) -> None:
    """0034 widens admin_actions_action_check to include 'clear_worker_cache'.

    Verifies:
    - INSERT with action='clear_worker_cache' passes the CHECK.
    - INSERT with action='__invented__' violates the CHECK (IntegrityError).
    """
    from sqlalchemy.exc import IntegrityError

    up = _run_alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, f"upgrade head failed:\n{up.stdout}\n{up.stderr}"

    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        # The two new columns exist on workers (catches an omitted ADD COLUMN).
        async with engine.connect() as conn:
            cols = (
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'workers' "
                            "AND column_name IN "
                            "('cache_flush_requested_at', 'cache_flush_done_at');"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert set(cols) == {"cache_flush_requested_at", "cache_flush_done_at"}, cols
    finally:
        await engine.dispose()

    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        # 'clear_worker_cache' must pass the CHECK.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_actions (action, target_kind, target_id, workspace_id) "
                    "VALUES ('clear_worker_cache', 'worker', '1', "
                    "'00000000-0000-0000-0000-000000000001');"
                )
            )
    finally:
        await engine.dispose()

    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        # An invented action must violate the CHECK.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO admin_actions (action, target_kind, target_id, workspace_id) "
                        "VALUES ('__invented__', 'worker', '2', "
                        "'00000000-0000-0000-0000-000000000001');"
                    )
                )

        # Clean up the 'clear_worker_cache' row so the downgrade (which restores
        # the previous CHECK) does not fail by violating the old constraint.
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM admin_actions WHERE action = 'clear_worker_cache';")
            )
    finally:
        await engine.dispose()

    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


@pytest.mark.asyncio
async def test_0048_pause_share_downgrade_cycle(postgres_url: str) -> None:
    """0048 widens the two admin_actions CHECKs (pause/resume + share_token).

    Exercises the full cycle that the migration docstring declares:
    1. INSERT with the new values passes both CHECKs.
    2. `downgrade` with the new rows present fails with check_violation
       (intentional: audit rows are not silently discarded) and leaves
       the DB intact at 0048 (env.py runs the migration in a transaction).
    3. After deleting the new rows, the downgrade runs cleanly.
    """
    up = _run_alembic(["upgrade", "head"], postgres_url)
    assert up.returncode == 0, f"upgrade head failed:\n{up.stdout}\n{up.stderr}"

    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        # 1. The new values of both CHECKs accept INSERTs.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_actions (action, target_kind, target_id, workspace_id) "
                    "VALUES ('pause_worker', 'worker', '1', "
                    "'00000000-0000-0000-0000-000000000001'), "
                    "('mint_share_token', 'share_token', '2', "
                    "'00000000-0000-0000-0000-000000000001');"
                )
            )
    finally:
        await engine.dispose()

    # 2. Downgrade with the new rows present: the rollback's ADD CONSTRAINT
    # must fail with check_violation naming the constraint.
    blocked = _run_alembic(["downgrade", "0047_tasks_completed_idx"], postgres_url)
    assert blocked.returncode != 0, (
        f"the downgrade should have failed with the new rows present:\n{blocked.stdout}"
    )
    combined = blocked.stdout + blocked.stderr
    assert (
        "admin_actions_target_kind_check" in combined or "admin_actions_action_check" in combined
    ), f"the failure does not mention the violated CHECK:\n{combined}"

    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        # The failure rolled back the whole transaction: the DB is still at 0048
        # (the CHECK in force still accepts the new values) and the rows are alive.
        async with engine.connect() as conn:
            definition = (
                await conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'admin_actions_action_check' "
                        "AND conrelid = 'admin_actions'::regclass"
                    )
                )
            ).scalar_one()
            assert "'pause_worker'" in definition, definition
            remaining = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM admin_actions "
                        "WHERE action IN ('pause_worker', 'mint_share_token');"
                    )
                )
            ).scalar_one()
            assert remaining == 2, remaining

        # 3. Without the new rows, the downgrade runs cleanly.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM admin_actions "
                    "WHERE action IN ('pause_worker', 'mint_share_token');"
                )
            )
    finally:
        await engine.dispose()

    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, (
        f"downgrade base failed:\n{final_down.stdout}\n{final_down.stderr}"
    )


async def _enum_labels(database_url: str, type_name: str) -> set[str]:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                sa.text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = :tn"
                ),
                {"tn": type_name},
            )
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_0041_adds_completed_no_fingerprint_enum(postgres_url: str) -> None:
    """Migration 0041: adds the terminal value `completed_no_fingerprint` to
    both enums. Verified at revision 0041 itself, not at head: migration 0046
    rebuilds both enums WITHOUT this value (final removal, the nuance now
    lives in `outcome`), so from 0046 onwards it is no longer present; see
    test_migration_0046.py."""
    up = _run_alembic(["upgrade", "0041_terminal_status_enum_value"], postgres_url)
    assert up.returncode == 0, up.stderr
    task_labels = await _enum_labels(postgres_url, "task_status")
    exec_labels = await _enum_labels(postgres_url, "execution_status")
    assert "completed_no_fingerprint" in task_labels
    assert "completed_no_fingerprint" in exec_labels
    final_down = _run_alembic(["downgrade", "base"], postgres_url)
    assert final_down.returncode == 0, final_down.stderr


@pytest.mark.asyncio
async def test_0036_backfills_workspace_artifact_usage(postgres_url: str) -> None:
    """The 0036 backfill reads existing task_artifacts and fills workspace_artifact_usage.

    Flow:
    1. upgrade to 0035_task_artifacts (before workspace_artifact_usage exists).
    2. Insert fixture: workspace + worker + task + execution + 123-byte artifact.
       The data fits the 0035 schema, valid for the INSERT.
    3. upgrade to 0036_workspace_artifact_usage; here the migration's backfill
       must populate the row with total_bytes = 123.
    4. assert: total_bytes == 123, set by THE MIGRATION, without re-running the aggregate
       in the test.
    5. cleanup via downgrade base.
    """
    # 1. Schema up to 0035 (task_artifacts exists; workspace_artifact_usage does not yet).
    up35 = _run_alembic(["upgrade", "0035_task_artifacts"], postgres_url)
    assert up35.returncode == 0, f"upgrade 0035 failed:\n{up35.stdout}\n{up35.stderr}"

    # 2. Insert a fixture with pre-migration data: a workspace with a 123-byte artifact.
    ws_id = "00000000-0000-0000-0000-0000000000aa"
    engine = create_async_engine(postgres_url, poolclass=None)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO workspaces (id, name, slug) VALUES (:i, 'WS', 'ws-0036')"),
                {"i": ws_id},
            )
            # task_executions.worker_id is NOT NULL, so we need a real worker.
            wkr_id = (
                await conn.execute(
                    text(
                        "INSERT INTO workers "
                        "(workspace_id, api_key_hash, hostname, os, arch) "
                        "VALUES (:w, 'hash-0036', 'host-0036', 'linux', 'x86_64') "
                        "RETURNING id"
                    ),
                    {"w": ws_id},
                )
            ).scalar_one()
            tid = (
                await conn.execute(
                    text(
                        "INSERT INTO tasks (workspace_id, command, payload, "
                        "target_mode, target_count, priority, status, run_at, "
                        "signed, signature) VALUES (:w, 'echo', '{}'::jsonb, "
                        "'count', 1, 0, 'queued', now(), true, "
                        "decode(repeat('00',64),'hex')) RETURNING id"
                    ),
                    {"w": ws_id},
                )
            ).scalar_one()
            eid = (
                await conn.execute(
                    text(
                        "INSERT INTO task_executions (task_id, worker_id, status) "
                        "VALUES (:t, :wkr, 'completed') RETURNING id::text"
                    ),
                    {"t": tid, "wkr": wkr_id},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO task_artifacts (execution_id, rel_path, "
                    "size_bytes, sha256, content_type) VALUES "
                    "(CAST(:e AS uuid), 'a.txt', 123, 'deadbeef', 'text/plain')"
                ),
                {"e": eid},
            )
    finally:
        await engine.dispose()

    # 3. Apply 0036: the migration's backfill must read the artifacts and populate the table.
    try:
        up36 = _run_alembic(["upgrade", "0036_workspace_artifact_usage"], postgres_url)
        assert up36.returncode == 0, f"upgrade 0036 failed:\n{up36.stdout}\n{up36.stderr}"

        # 4. The migration, not the test, must have written total_bytes = 123.
        engine = create_async_engine(postgres_url, poolclass=None)
        try:
            async with engine.connect() as conn:
                total = (
                    await conn.execute(
                        text(
                            "SELECT total_bytes FROM workspace_artifact_usage "
                            "WHERE workspace_id = :w"
                        ),
                        {"w": ws_id},
                    )
                ).scalar_one()
                assert total == 123, f"the 0036 backfill should write 123 bytes, found {total}"
        finally:
            await engine.dispose()
    finally:
        # 5. Cleanup: downgrade base regardless of whether the assert failed.
        _run_alembic(["downgrade", "base"], postgres_url)


@pytest.mark.asyncio
async def test_0051_task_artifacts_created_at_idx(postgres_url: str) -> None:
    """0051 creates the time index that serves the reconcile filter.

    Without `task_artifacts_created_at_idx`, the `WHERE created_at >= :since` of
    the filtered reconcile pass would be a seq scan of the largest table in the
    schema. The downgrade drops it.
    """
    try:
        up = _run_alembic(["upgrade", "head"], postgres_url)
        assert up.returncode == 0, f"upgrade head failed:\n{up.stdout}\n{up.stderr}"

        engine = create_async_engine(postgres_url, poolclass=None)
        try:
            async with engine.connect() as conn:
                idx = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM pg_indexes WHERE schemaname='public' "
                            "AND indexname='task_artifacts_created_at_idx'"
                        )
                    )
                ).scalar_one_or_none()
                assert idx == 1, "the task_artifacts_created_at_idx index is missing"
        finally:
            await engine.dispose()

        down = _run_alembic(["downgrade", "0050_tasks_converted_from"], postgres_url)
        assert down.returncode == 0, f"downgrade 0050 failed:\n{down.stdout}\n{down.stderr}"

        engine = create_async_engine(postgres_url, poolclass=None)
        try:
            async with engine.connect() as conn:
                idx = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM pg_indexes WHERE schemaname='public' "
                            "AND indexname='task_artifacts_created_at_idx'"
                        )
                    )
                ).scalar_one_or_none()
                assert idx is None, "the 0051 downgrade should have dropped the index"
        finally:
            await engine.dispose()
    finally:
        _run_alembic(["downgrade", "base"], postgres_url)


@pytest.mark.asyncio
async def test_0055_workspaces_artifact_quota(postgres_url: str) -> None:
    """0055 adds `workspaces.artifact_quota_bytes` with a NULL backfill.

    A pre-existing workspace stays NULL (= global default, previous behaviour
    intact); the CHECK rejects 0; the downgrade drops the column.
    """
    try:
        up = _run_alembic(["upgrade", "0054_workers_free_disk_fraction"], postgres_url)
        assert up.returncode == 0, f"upgrade 0054 failed:\n{up.stdout}\n{up.stderr}"
        engine = create_async_engine(postgres_url, poolclass=None)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO workspaces (name, slug) VALUES ('Pre', 'pre-0055')")
                )
        finally:
            await engine.dispose()

        up = _run_alembic(["upgrade", "0055_workspaces_artifact_quota"], postgres_url)
        assert up.returncode == 0, f"upgrade 0055 failed:\n{up.stdout}\n{up.stderr}"
        engine = create_async_engine(postgres_url, poolclass=None)
        try:
            async with engine.connect() as conn:
                quotas = (
                    (await conn.execute(text("SELECT artifact_quota_bytes FROM workspaces")))
                    .scalars()
                    .all()
                )
                assert quotas and all(q is None for q in quotas)
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE workspaces SET artifact_quota_bytes = 0 WHERE slug = 'pre-0055'"
                        )
                    )
        finally:
            await engine.dispose()

        down = _run_alembic(["downgrade", "0054_workers_free_disk_fraction"], postgres_url)
        assert down.returncode == 0, f"downgrade 0054 failed:\n{down.stdout}\n{down.stderr}"
        engine = create_async_engine(postgres_url, poolclass=None)
        try:
            async with engine.connect() as conn:
                col = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = 'workspaces' "
                            "AND column_name = 'artifact_quota_bytes'"
                        )
                    )
                ).scalar_one_or_none()
                assert col is None, "the 0055 downgrade should have dropped the column"
        finally:
            await engine.dispose()
    finally:
        _run_alembic(["downgrade", "base"], postgres_url)
