"""Supporting indexes for filtering tasks by artifact

Revision ID: 0037_artifact_filter_indexes
Revises: 0036_workspace_artifact_usage
Create Date: 2026-06-11

Indexes only, no data changes. They enable the dashboard's artifact filter
(correlated EXISTS over tasks -> task_executions -> task_artifacts):

- pg_trgm + GIN on rel_path  -> ILIKE with a wildcard at any position (glob).
- btree on content_type      -> exact MIME equality.
- btree on sha256            -> equality and prefix match (LIKE '<hex>%';
  the server lowercases the prefix; text_pattern_ops does not speed up ILIKE).

The EXISTS join (ae.task_id = t.id) is served by the btree index of the UNIQUE
(task_id, worker_id, attempt_index) created in 0014 (task_id is the leading
column), so no dedicated index on task_executions(task_id) is created.

Plain CREATE INDEX (not CONCURRENTLY), consistent with the migrations that run
under MIGRATE_ON_STARTUP. asyncpg/SQLAlchemy reject multiple statements in one
prepared statement; each DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0037_artifact_filter_indexes"
down_revision = "0036_workspace_artifact_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS task_artifacts_rel_path_trgm_idx "
            "ON task_artifacts USING gin (rel_path gin_trgm_ops);"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS task_artifacts_content_type_idx "
            "ON task_artifacts (content_type);"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS task_artifacts_sha256_idx "
            "ON task_artifacts (sha256 text_pattern_ops);"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS task_artifacts_sha256_idx;"))
    op.execute(text("DROP INDEX IF EXISTS task_artifacts_content_type_idx;"))
    op.execute(text("DROP INDEX IF EXISTS task_artifacts_rel_path_trgm_idx;"))
    # The pg_trgm extension is NOT dropped: other objects may use it and
    # DROP EXTENSION is destructive across features. Leaving it is harmless.
