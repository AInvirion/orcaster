"""workspace_artifact_usage: aggregate counter of artifact bytes

Revision ID: 0036_workspace_artifact_usage
Revises: 0035_task_artifacts
Create Date: 2026-06-10

Per-workspace aggregate counter for O(1) enforcement of the artifact quota
(ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES) in the hot path of the poll and the upload,
without recomputing the task_artifacts->executions->tasks->workspaces join on
every request. It is maintained incrementally (upload adds, purge subtracts) and
reconciled periodically in housekeeping. The backfill computes the real SUM of
the existing rows; workspaces without artifacts stay at 0.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in one prepared
statement; for that reason each DDL/DML goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0036_workspace_artifact_usage"
down_revision = "0035_task_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE workspace_artifact_usage (
                workspace_id uuid PRIMARY KEY
                    REFERENCES workspaces(id) ON DELETE CASCADE,
                total_bytes  bigint NOT NULL DEFAULT 0
                    CHECK (total_bytes >= 0),
                updated_at   timestamptz NOT NULL DEFAULT now()
            );
            """
        )
    )
    op.execute(
        text(
            """
            INSERT INTO workspace_artifact_usage (workspace_id, total_bytes)
            SELECT w.id, COALESCE(SUM(ta.size_bytes), 0)
              FROM workspaces w
              LEFT JOIN tasks t ON t.workspace_id = w.id
              LEFT JOIN task_executions e ON e.task_id = t.id
              LEFT JOIN task_artifacts ta ON ta.execution_id = e.id
             GROUP BY w.id;
            """
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE workspace_artifact_usage;"))
