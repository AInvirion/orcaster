"""tasks: sticky auto-retry policy + attempts counter

Revision ID: 0017_tasks_auto_retry
Revises: 0016_tasks_execution_timeout
Create Date: 2026-05-23

Implements sticky auto-retry on throttling:

- `tasks.auto_retry_policy` (JSONB nullable): serialized policy with shape
  `{max_attempts, sticky, retryable_exit_codes, retryable_cancelled_reasons,
   backoff_seconds, sticky_offline_timeout_seconds}`. NULL = back-compat
  (no auto-retry; previous behaviour preserved).
- `tasks.auto_retry_attempts_used` (INT NOT NULL DEFAULT 0): counter of
  attempts already consumed by auto-retry. It is compared against
  `policy.max_attempts` in `task_complete` to decide whether to retry or
  close as terminal.
- `admin_actions.action` is extended with `auto_retry_task` so that
  `record_admin_action()` can audit every server-driven auto-retry
  (same technique as 0013_admin_actions_retry: DROP + recreate of the
  CHECK constraint).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0017_tasks_auto_retry"
down_revision = "0016_tasks_execution_timeout"
branch_labels = None
depends_on = None


_OLD_ACTION_VALUES = (
    "'delete_worker','cancel_task','delete_task','bulk_cancel_task','bulk_delete_task','retry_task'"
)
_NEW_ACTION_VALUES = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task',"
    "'retry_task','auto_retry_task'"
)


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ADD COLUMN auto_retry_policy JSONB NULL;"))
    op.execute(
        text("ALTER TABLE tasks ADD COLUMN auto_retry_attempts_used INTEGER NOT NULL DEFAULT 0;")
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
    op.execute(text("ALTER TABLE tasks DROP COLUMN auto_retry_attempts_used;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN auto_retry_policy;"))
