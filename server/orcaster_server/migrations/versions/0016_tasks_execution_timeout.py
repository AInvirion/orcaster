"""tasks: add execution_timeout_seconds (optional outer timeout per task)

Revision ID: 0016_tasks_execution_timeout
Revises: 0015_tasks_command_nullable
Create Date: 2026-05-22

Layer orthogonal to the per-command timeout_seconds of the payload.
NULL = no outer timeout (previous behaviour preserved).
No DEFAULT or server_default: NULL is an explicit absence of restriction.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0016_tasks_execution_timeout"
down_revision = "0015_tasks_command_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ADD COLUMN execution_timeout_seconds INTEGER NULL;"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE tasks DROP COLUMN execution_timeout_seconds;"))
