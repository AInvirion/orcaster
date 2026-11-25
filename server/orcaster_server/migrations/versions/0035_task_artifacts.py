"""task_artifacts: real files produced by the task

Revision ID: 0035_task_artifacts
Revises: 0034_workers_cache_flush
Create Date: 2026-06-09

A concept distinct from the log (output_events). The log is the on-demand
transcript of stdout/stderr; an artifact is a file that the task writes to
ORCASTER_ARTIFACTS_DIR and that the worker uploads through
POST /v1/tasks/{id}/artifacts. The bytes live on the filesystem
(ARTIFACTS_ROOT/<execution_id>/<rel_path>); this table stores only the
metadata, associated per execution (like output_events).

UNIQUE(execution_id, rel_path) -> idempotence: resending the same file of the
same execution does not duplicate rows. ON DELETE CASCADE -> deleting the
execution deletes the metadata (the bytes are cleaned up in the same deletion
path of the execution).

asyncpg/SQLAlchemy reject multiple `;`-separated statements in one prepared
statement; for that reason each DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0035_task_artifacts"
down_revision = "0034_workers_cache_flush"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE task_artifacts (
                id           BIGSERIAL PRIMARY KEY,
                execution_id uuid NOT NULL REFERENCES task_executions(id) ON DELETE CASCADE,
                rel_path     text NOT NULL,
                size_bytes   bigint NOT NULL,
                sha256       text NOT NULL,
                content_type text NOT NULL,
                created_at   timestamptz NOT NULL DEFAULT now()
            );
            """
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX uq_task_artifacts_execution_relpath "
            "ON task_artifacts (execution_id, rel_path);"
        )
    )


def downgrade() -> None:
    # The UNIQUE INDEX drops with the table (same rule as 0031).
    op.execute(text("DROP TABLE task_artifacts;"))
