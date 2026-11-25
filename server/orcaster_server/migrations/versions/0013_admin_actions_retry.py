"""admin_actions: extend CHECK action with retry_task

Revision ID: 0013_admin_actions_retry
Revises: 0012_rename_tasks_requirements_to_eligibility
Create Date: 2026-05-21

The server-side endpoint `POST /admin/tasks/{id}/retry` is introduced,
distinct from the current "clone" (which creates a new task with a new
task_id). Retry reuses the same task_id so the `ORCASTER_CACHE_DIR` on the
worker can be reused (key = idempotency_key or task_id). Every retry is
audited in `admin_actions` with `action='retry_task'`.

Same technique as `0003_admin_actions_bulk`: DROP + recreate of the CHECK
constraint with the union of values (Postgres cannot extend a CHECK
in place). Metadata-only operation on an append-only table.

`downgrade` restores the previous set; if `retry_task` rows remained, the
ADD CONSTRAINT would fail with `check_violation` — intentional, rollback
must be explicit.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0013_admin_actions_retry"
down_revision = "0012_tasks_rename_eligibility"
branch_labels = None
depends_on = None


_OLD_VALUES = "'delete_worker','cancel_task','delete_task','bulk_cancel_task','bulk_delete_task'"
_NEW_VALUES = (
    "'delete_worker','cancel_task','delete_task','bulk_cancel_task','bulk_delete_task','retry_task'"
)


def upgrade() -> None:
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            f"ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_NEW_VALUES}));"
        )
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            f"ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_OLD_VALUES}));"
        )
    )
