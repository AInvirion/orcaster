"""Generic terminal outcomes: outcome column + removal of a terminal status

Revision ID: 0046_task_outcomes
Revises: 0045_tasks_variant
Create Date: 2026-07-21

Complete migration. The terminal status that
`0041_terminal_status_enum_value` added stops being a status and becomes
`completed` + a row with a non-null `outcome`; the nuance of a terminal state
lives in the generic `outcome` column (NULL = ordinary terminal).

Literals kept on purpose: `'completed_no_fingerprint'` is the enum value that
`0041` added and that this migration converts and retires (and that the
downgrade re-adds), and `'no_fingerprint'` is the outcome under which those rows
were stored. They are the values the SQL needs to find the rows and rebuild the
types; changing them would change the applied DDL and data. The meaning of that
outcome is defined today by a plugin's outcome profile, not by the core.

Postgres does not support DROP VALUE on enums: the removal is done by REBUILD
(create a new type without the value, ALTER COLUMN ... TYPE ... USING, drop the
old one). The column's DEFAULT is dropped before the retype and restored after
(the implicit cast of the old default references the old type). The downgrade is
the inverse rebuild (re-adds the value) + UPDATE back; it uses a rebuild and not
`ALTER TYPE ... ADD VALUE` because a value added in the same transaction cannot
be used in the following UPDATE (Postgres limitation).

One statement per op.execute (asyncpg). No imports from `orcaster_server`
(self-contained).

Note: the rebuild is NOT enough with RENAME+CREATE+ALTER COLUMN TYPE alone.
`tasks`/`task_executions` have PARTIAL indexes whose predicate compares the
`status` column against literals (`tasks_dispatch_idx` and
`tasks_push_pending_idx` of 0001/0007; `te_lease_idx`/`te_assigned_idx`/
`task_executions_active_uk` of 0001/0014). Those literals stay bound to the OID
of the OLD enum; even if the new type has the same name
(`task_status`/`execution_status`), it is a different OID, and the
`ALTER COLUMN ... TYPE ... USING` fails with "operator does not exist:
task_status = task_status_old" when it tries to reconcile the partial index
against the already retyped column. They are dropped before the rebuild and
recreated after (exact same DDL as the original; checked against `\d tasks` /
`\d task_executions` on a test DB at 0045).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0046_task_outcomes"
down_revision = "0045_tasks_variant"
branch_labels = None
depends_on = None

_TASK_STATUS_NEW = "('queued','dispatching','paused','completed','cancelled','failed')"
_TASK_STATUS_OLD = (
    "('queued','dispatching','paused','completed','cancelled','failed','completed_no_fingerprint')"
)
_EXEC_STATUS_NEW = "('assigned','claimed','running','completed','failed','expired','cancelled')"
_EXEC_STATUS_OLD = (
    "('assigned','claimed','running','completed','failed','expired','cancelled',"
    "'completed_no_fingerprint')"
)

# Partial indexes that reference `status` in their predicate, per table.
# (name, exact recreation DDL; see 0001_init.py / 0014_task_executions_uuid_pk.py).
_TASKS_STATUS_INDEXES = [
    (
        "tasks_dispatch_idx",
        "CREATE INDEX tasks_dispatch_idx ON tasks (priority DESC, run_at) "
        "WHERE status IN ('queued','dispatching');",
    ),
    (
        "tasks_push_pending_idx",
        "CREATE INDEX tasks_push_pending_idx ON tasks (target_worker_id, created_at) "
        "WHERE delivery_mode = 'push' AND status IN ('queued', 'dispatching');",
    ),
]
_EXEC_STATUS_INDEXES = [
    (
        "te_lease_idx",
        "CREATE INDEX te_lease_idx ON task_executions (lease_expires_at) "
        "WHERE status IN ('claimed','running');",
    ),
    (
        "te_assigned_idx",
        "CREATE INDEX te_assigned_idx ON task_executions (worker_id, status) "
        "WHERE status = 'assigned';",
    ),
    (
        "task_executions_active_uk",
        "CREATE UNIQUE INDEX task_executions_active_uk ON task_executions (task_id, worker_id) "
        "WHERE status IN ('assigned', 'claimed', 'running');",
    ),
]


def _rebuild_enum(
    enum: str,
    table: str,
    column: str,
    values: str,
    default: str,
    dependent_indexes: list[tuple[str, str]],
) -> None:
    for name, _create_sql in dependent_indexes:
        op.execute(text(f"DROP INDEX IF EXISTS {name};"))
    op.execute(text(f"ALTER TYPE {enum} RENAME TO {enum}_old;"))
    op.execute(text(f"CREATE TYPE {enum} AS ENUM {values};"))
    op.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT;"))
    op.execute(
        text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {enum} USING {column}::text::{enum};")
    )
    op.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}';"))
    op.execute(text(f"DROP TYPE {enum}_old;"))
    for _name, create_sql in dependent_indexes:
        op.execute(text(create_sql))


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ADD COLUMN outcome text;"))
    op.execute(text("ALTER TABLE task_executions ADD COLUMN outcome text;"))
    # Direct UPDATE in a single transaction, no batching: the number of affected
    # rows is bounded (only those that had the retired status).
    op.execute(
        text(
            "UPDATE tasks SET status = 'completed', outcome = 'no_fingerprint' "
            "WHERE status = 'completed_no_fingerprint';"
        )
    )
    op.execute(
        text(
            "UPDATE task_executions SET status = 'completed', outcome = 'no_fingerprint' "
            "WHERE status = 'completed_no_fingerprint';"
        )
    )
    _rebuild_enum(
        "task_status", "tasks", "status", _TASK_STATUS_NEW, "queued", _TASKS_STATUS_INDEXES
    )
    _rebuild_enum(
        "execution_status",
        "task_executions",
        "status",
        _EXEC_STATUS_NEW,
        "assigned",
        _EXEC_STATUS_INDEXES,
    )
    # Partial index: the vast majority of rows have a NULL outcome.
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS tasks_outcome_idx "
            "ON tasks (workspace_id, outcome) WHERE outcome IS NOT NULL;"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS tasks_outcome_idx;"))
    _rebuild_enum(
        "task_status", "tasks", "status", _TASK_STATUS_OLD, "queued", _TASKS_STATUS_INDEXES
    )
    _rebuild_enum(
        "execution_status",
        "task_executions",
        "status",
        _EXEC_STATUS_OLD,
        "assigned",
        _EXEC_STATUS_INDEXES,
    )
    op.execute(
        text(
            "UPDATE tasks SET status = 'completed_no_fingerprint' "
            "WHERE status = 'completed' AND outcome = 'no_fingerprint';"
        )
    )
    op.execute(
        text(
            "UPDATE task_executions SET status = 'completed_no_fingerprint' "
            "WHERE status = 'completed' AND outcome = 'no_fingerprint';"
        )
    )
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS outcome;"))
    op.execute(text("ALTER TABLE task_executions DROP COLUMN IF EXISTS outcome;"))
