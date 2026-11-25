"""task_executions: UUID PK + attempt_index to preserve retry history

Revision ID: 0014_task_executions_uuid_pk
Revises: 0013_admin_actions_retry
Create Date: 2026-05-21

`task_executions` used a composite PK `(task_id, worker_id)`. That forces the
Retry endpoint (`POST /admin/tasks/{id}/retry`) to DELETE the previous row of
the same worker before the retry — the next push-delivery INSERT in /poll
would collide with the PK. The stdout/stderr/exit_code of every previous
attempt on that worker is lost (history loss documented in admin_tasks.py).

This migration:

1. Adds `id UUID NOT NULL DEFAULT gen_random_uuid()` — safe backfill
   through DEFAULT.
2. Adds `attempt_index INT NOT NULL DEFAULT 1` — consistent backfill
   because there is at most 1 row per (task_id, worker_id) pair.
3. Replaces the composite PK with `id` as PK.
4. UNIQUE `(task_id, worker_id, attempt_index)` so that each attempt is
   stably identifiable.
5. PARTIAL UNIQUE INDEX `(task_id, worker_id) WHERE status IN
   ('assigned','claimed','running')` to keep the critical invariant: at
   most ONE live execution per task/worker pair. Once the execution is in
   a terminal state (completed/failed/expired/cancelled), a new retry can
   insert another row for the same worker without collision.

`pgcrypto` is already enabled (0001_init.py:24 → `CREATE EXTENSION IF
NOT EXISTS pgcrypto`); there is no need to create it again.

`downgrade()` reverts the schema to the composite PK. NOTE: if history
accumulated between upgrade and downgrade (multiple rows per task/worker
pair, a condition the new schema allows), the
ALTER ADD PRIMARY KEY (task_id, worker_id) WILL FAIL with
`unique_violation`. In that case the operator must consolidate manually
before the downgrade — intentional behaviour: we do not destroy history
silently.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0014_task_executions_uuid_pk"
down_revision = "0013_admin_actions_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add columns with DEFAULT for a safe backfill.
    op.execute(
        text("ALTER TABLE task_executions ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid();")
    )
    op.execute(text("ALTER TABLE task_executions ADD COLUMN attempt_index int NOT NULL DEFAULT 1;"))

    # 2. Rebuild the PK: drop the composite one, set `id` as PK.
    op.execute(text("ALTER TABLE task_executions DROP CONSTRAINT task_executions_pkey;"))
    op.execute(
        text("ALTER TABLE task_executions ADD CONSTRAINT task_executions_pkey PRIMARY KEY (id);")
    )

    # 3. UNIQUE (task_id, worker_id, attempt_index) — stable identity
    # of each attempt. Allows looking up "attempt N of worker W in task T"
    # without ambiguity.
    op.execute(
        text(
            "ALTER TABLE task_executions "
            "ADD CONSTRAINT task_executions_attempt_uk "
            "UNIQUE (task_id, worker_id, attempt_index);"
        )
    )

    # 4. PARTIAL UNIQUE: at most one ACTIVE execution per (task_id,
    # worker_id). This replaces the guarantee the old PK gave
    # implicitly, but only over non-terminal states. Terminal rows
    # (history) do not compete.
    op.execute(
        text(
            "CREATE UNIQUE INDEX task_executions_active_uk "
            "ON task_executions (task_id, worker_id) "
            "WHERE status IN ('assigned', 'claimed', 'running');"
        )
    )


def downgrade() -> None:
    # Revert in reverse order. If history has accumulated (multiple rows
    # per task/worker pair), the ALTER ADD PRIMARY KEY will fail and the
    # operator must consolidate manually — intentional, see the module docstring.
    op.execute(text("DROP INDEX IF EXISTS task_executions_active_uk;"))
    op.execute(
        text("ALTER TABLE task_executions DROP CONSTRAINT IF EXISTS task_executions_attempt_uk;")
    )
    op.execute(text("ALTER TABLE task_executions DROP CONSTRAINT task_executions_pkey;"))
    op.execute(
        text(
            "ALTER TABLE task_executions "
            "ADD CONSTRAINT task_executions_pkey PRIMARY KEY (task_id, worker_id);"
        )
    )
    op.execute(text("ALTER TABLE task_executions DROP COLUMN attempt_index;"))
    op.execute(text("ALTER TABLE task_executions DROP COLUMN id;"))
