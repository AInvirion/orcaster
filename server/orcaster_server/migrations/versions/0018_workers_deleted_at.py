"""workers: soft-delete via deleted_at + partial index of active rows

Revision ID: 0018_workers_deleted_at
Revises: 0017_tasks_auto_retry
Create Date: 2026-05-24

Implements soft-delete of workers (to preserve the task_executions history):

- `workers.deleted_at` (TIMESTAMPTZ NULL): soft-delete mark. NULL = active
  worker. Needs no backfill: all existing rows stay NULL, which is exactly
  the right state (active) for back-compat.
- Partial index `ix_workers_active` on `(id) WHERE deleted_at IS NULL` to
  speed up the typical "active workers" read. It is cheap because most rows
  fit in it, but it lets Postgres quickly skip the soft-deleted rows in
  queries that filter by `deleted_at IS NULL`.

The FK `task_executions.worker_id -> workers.id ON DELETE CASCADE` is kept
intact: since this is a soft-delete, the row is not deleted, so the execution
history keeps referencing an existing worker.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0018_workers_deleted_at"
down_revision = "0017_tasks_auto_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Soft-delete column; NULL = active, no backfill needed.
    op.execute(text("ALTER TABLE workers ADD COLUMN deleted_at TIMESTAMPTZ NULL;"))
    # Partial index to speed up the "active workers" filter.
    op.execute(text("CREATE INDEX ix_workers_active ON workers (id) WHERE deleted_at IS NULL;"))


def downgrade() -> None:
    # Reverse order: first the index, then the column it relies on.
    op.execute(text("DROP INDEX ix_workers_active;"))
    op.execute(text("ALTER TABLE workers DROP COLUMN deleted_at;"))
