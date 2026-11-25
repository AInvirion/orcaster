"""admin_actions: extend the action CHECK with bulk_retry_task

Revision ID: 0038_admin_actions_bulk_retry
Revises: 0037_artifact_filter_indexes
Create Date: 2026-06-11

The endpoint `POST /admin/tasks/bulk/retry` audits one row per retried task
with `action='bulk_retry_task'`. Without this migration the INSERT raises
`check_violation` (the CHECK in force is that of 0034).

Same technique as 0003/0013/0017/0019/0024/0034: DROP + recreate the CHECK
with the union of values (Postgres does not allow extending a CHECK in place).

The downgrade restores the previous CHECK; if 'bulk_retry_task' rows remain,
the ADD CONSTRAINT fails with `check_violation`. This is intentional: an
explicit rollback (do not silently discard audit rows).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0038_admin_actions_bulk_retry"
down_revision = "0037_artifact_filter_indexes"
branch_labels = None
depends_on = None

# Set in force for the CHECK after 0034 (do not assume earlier sets).
_ACTION_OLD = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task','retry_task',"
    "'auto_retry_task','fork_task',"
    "'mint_pat','revoke_pat','delete_pat',"
    "'mint_enrollment_token','revoke_enrollment_token','delete_enrollment_token',"
    "'clear_worker_cache'"
)
# New set: the previous one + bulk_retry_task.
_ACTION_NEW = _ACTION_OLD + ",'bulk_retry_task'"


def upgrade() -> None:
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_ACTION_NEW}));"
        )
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_ACTION_OLD}));"
        )
    )
