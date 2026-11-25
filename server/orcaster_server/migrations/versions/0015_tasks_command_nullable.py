"""tasks: make command nullable with default 'run_batch'

Revision ID: 0015_tasks_command_nullable
Revises: 0014_task_executions_uuid_pk
Create Date: 2026-05-22

Work tasks no longer carry an explicit `command` in the external contract.
The column stays in the schema (used by the signing canonical message and
by control ops which use reserved command names). Work tasks always store
the value 'run_batch'; the column default makes that the behavior for any
INSERT that omits the column.

downgrade() restores NOT NULL by backfilling any NULL rows with 'run_batch'
before re-applying the constraint.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0015_tasks_command_nullable"
down_revision = "0014_task_executions_uuid_pk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ALTER COLUMN command DROP NOT NULL;"))
    op.execute(text("ALTER TABLE tasks ALTER COLUMN command SET DEFAULT 'run_batch';"))


def downgrade() -> None:
    op.execute(text("UPDATE tasks SET command = 'run_batch' WHERE command IS NULL;"))
    op.execute(text("ALTER TABLE tasks ALTER COLUMN command SET NOT NULL;"))
    op.execute(text("ALTER TABLE tasks ALTER COLUMN command DROP DEFAULT;"))
