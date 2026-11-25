"""admin_actions: extend CHECK action with bulk_cancel_task / bulk_delete_task

Revision ID: 0003_admin_actions_bulk
Revises: 0002_admin_actions
Create Date: 2026-05-14

Two bulk endpoints are introduced (`POST /admin/tasks/bulk/cancel` and
`POST /admin/tasks/bulk/delete`) that write one row per affected task into
`admin_actions`. The values `bulk_cancel_task` and `bulk_delete_task` are not
in the original CHECK (0002), so any INSERT from the new endpoints would
fail with `check_violation`.

Postgres cannot "extend" a CHECK in place: it is DROPped and recreated with
the union of values. The operation is metadata-only on a modest append-only
table, so the catalog rewrite is effectively instantaneous.

`downgrade` restores the original CHECK. If rows with the new values exist
at downgrade time, the ADD CONSTRAINT would fail with `check_violation`.
This is intentional: rollback must be explicit.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0003_admin_actions_bulk"
down_revision = "0002_admin_actions"
branch_labels = None
depends_on = None


_OLD_VALUES = "'delete_worker','cancel_task','delete_task'"
_NEW_VALUES = "'delete_worker','cancel_task','delete_task','bulk_cancel_task','bulk_delete_task'"


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
