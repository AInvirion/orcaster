"""tasks: fork lineage (parent_task_id) + retry_count (operator retries)

Revision ID: 0019_tasks_fork_lineage_retry
Revises: 0018_workers_deleted_at
Create Date: 2026-05-26

Adds to `tasks`:
- `parent_task_id` (BIGINT NULL REFERENCES tasks(id) ON DELETE SET NULL):
  `task_id` of the parent of a fork. NULL for non-forked tasks. ON DELETE
  SET NULL so that deleting the parent (existing bulk delete) neither blocks
  nor deletes the child — the child survives with parent_task_id=NULL. The
  durable trace of the lineage stays in admin_actions (action='fork_task').
- `retry_count` (INT NOT NULL DEFAULT 0): in-place operator retries via
  POST /admin/tasks/{id}/retry. Independent of auto_retry_attempts_used
  (server-driven auto-retries).

Partial index `tasks_parent_task_id_idx` for child/lineage queries.

`admin_actions.action` is extended with `fork_task` (DROP + recreate of the
CHECK, same technique as 0013/0017).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0019_tasks_fork_lineage_retry"
down_revision = "0018_workers_deleted_at"
branch_labels = None
depends_on = None


_OLD_ACTION_VALUES = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task',"
    "'retry_task','auto_retry_task'"
)
_NEW_ACTION_VALUES = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task',"
    "'retry_task','auto_retry_task','fork_task'"
)


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE tasks ADD COLUMN parent_task_id BIGINT NULL "
            "REFERENCES tasks(id) ON DELETE SET NULL;"
        )
    )
    op.execute(text("ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;"))
    op.execute(
        text(
            "CREATE INDEX tasks_parent_task_id_idx ON tasks (parent_task_id) "
            "WHERE parent_task_id IS NOT NULL;"
        )
    )
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            f"ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_NEW_ACTION_VALUES}));"
        )
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            f"ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_OLD_ACTION_VALUES}));"
        )
    )
    op.execute(text("DROP INDEX tasks_parent_task_id_idx;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN retry_count;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN parent_task_id;"))
