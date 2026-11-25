"""Task variant derived from the payload: (variant_kind, variant_label)

Revision ID: 0045_tasks_variant
Revises: 0044_workspace_share_token
Create Date: 2026-07-17

Adds two derived columns to segment the dashboard by the task's *variant*: the
pair ``(tool, version)`` that lives only as a substring inside ``tasks.payload``
(JSONB). The derivation (at task creation) is done by the extractors that
plugins register (``orcaster_server.task_variant``, ``orcaster_server.plugins``).

- ``variant_kind``  = tool (kind of an extractor; NULL = ``other``/unrecognized).
- ``variant_label`` = version (NULL = other).
- Index ``(workspace_id, variant_kind, variant_label, status)``: serves the
  filter by variant and the GROUP BY of the current counts (hot path of the
  dashboard poll).

This revision also used to include a data backfill based on the contract of one
specific extractor. The core does not know that contract: the backfill was
removed and only the DDL and its downgrade remain, unchanged. A database that
already went through this revision keeps what the backfill wrote; a new database
has no rows to backfill.

Plain CREATE INDEX (not CONCURRENTLY), consistent with MIGRATE_ON_STARTUP; each
statement in its own op.execute (asyncpg rejects multiple statements per prepared
statement).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0045_tasks_variant"
down_revision = "0044_workspace_share_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ADD COLUMN variant_kind text;"))
    op.execute(text("ALTER TABLE tasks ADD COLUMN variant_label text;"))

    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS tasks_variant_idx "
            "ON tasks (workspace_id, variant_kind, variant_label, status);"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS tasks_variant_idx;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS variant_label;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS variant_kind;"))
