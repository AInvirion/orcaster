"""task_artifacts time index for the filtered reconcile

Revision ID: 0051_task_artifacts_created_idx
Revises: 0050_tasks_converted_from
Create Date: 2026-07-31

Just an index, no data changes. The `workspace_artifact_usage` reconcile no
longer recomputes every workspace on each pass: it selects those with recent
activity starting from `task_artifacts WHERE created_at >= :since`. Without this
index that filter would be a seq scan of the largest table in the schema, which
is exactly the cost that change set out to eliminate; with it, the traversal is
bounded to the rows in the window.

Plain `CREATE INDEX` (not CONCURRENTLY), consistent with 0037/0047 and with the
migrations that run under MIGRATE_ON_STARTUP. asyncpg/SQLAlchemy reject multiple
statements in a single prepared statement; each DDL goes in its own
op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0051_task_artifacts_created_idx"
down_revision = "0050_tasks_converted_from"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS task_artifacts_created_at_idx "
            "ON task_artifacts (created_at);"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS task_artifacts_created_at_idx;"))
