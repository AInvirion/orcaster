"""admin_actions: extend the action CHECK with consume_enrollment_token

Revision ID: 0042_admin_actions_consume
Revises: 0041_terminal_status_enum_value
Create Date: 2026-06-26

The endpoint `POST /v1/workers/register` audits the consumption of the
enrollment token with `action='consume_enrollment_token'` (forensic trail of
the enrolled worker). Without this migration the INSERT raises `check_violation`
(the CHECK in force is that of 0038).

Same technique as 0003/0013/0017/0019/0024/0034/0038: DROP + recreate the
CHECK with the union of values (Postgres does not allow extending a CHECK in
place).

The downgrade restores the previous CHECK; if 'consume_enrollment_token' rows
remain, the ADD CONSTRAINT fails with `check_violation`. This is intentional:
an explicit rollback (do not silently discard audit rows).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0042_admin_actions_consume"
down_revision = "0041_terminal_status_enum_value"
branch_labels = None
depends_on = None

# Set in force for the CHECK after 0038 (includes bulk_retry_task).
_ACTION_OLD = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task','retry_task',"
    "'auto_retry_task','fork_task',"
    "'mint_pat','revoke_pat','delete_pat',"
    "'mint_enrollment_token','revoke_enrollment_token','delete_enrollment_token',"
    "'clear_worker_cache','bulk_retry_task'"
)
# New set: the previous one + consume_enrollment_token.
_ACTION_NEW = _ACTION_OLD + ",'consume_enrollment_token'"


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
