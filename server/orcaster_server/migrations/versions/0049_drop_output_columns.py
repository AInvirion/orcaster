"""task_executions: physical drop of stdout/stderr/output_truncated

Revision ID: 0049_drop_output_columns
Revises: 0048_admin_actions_pause_share
Create Date: 2026-07-28

Closes a prior debt: the output lives in `output_events` as the source of truth
and these columns have had no writers since then (NULL/false for every later
execution). The last reader with a visible effect (the `stderr_tail` of the task
listing) was repointed to `output_events` in an earlier change, and the residual
reads of `_SQL_GET_EXECUTION*` are removed in the same change that introduces
this migration.

The downgrade restores the exact pre-drop SCHEMA (stdout/stderr text from 0001,
output_truncated boolean NOT NULL DEFAULT false from 0030) but not the content:
it is not recoverable and there is nothing to recover; they had been empty for
months.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why each DDL goes in its own `op.execute(text(...))`.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0049_drop_output_columns"
down_revision = "0048_admin_actions_pause_share"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE task_executions DROP COLUMN stdout;"))
    op.execute(text("ALTER TABLE task_executions DROP COLUMN stderr;"))
    op.execute(text("ALTER TABLE task_executions DROP COLUMN output_truncated;"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE task_executions ADD COLUMN stdout text;"))
    op.execute(text("ALTER TABLE task_executions ADD COLUMN stderr text;"))
    op.execute(
        text(
            "ALTER TABLE task_executions "
            "ADD COLUMN output_truncated boolean NOT NULL DEFAULT false;"
        )
    )
