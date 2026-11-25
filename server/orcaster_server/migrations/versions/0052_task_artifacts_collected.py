"""task_artifacts.collected_at/collected_note: collection evidence

Revision ID: 0052_task_artifacts_collected
Revises: 0051_task_artifacts_created_idx
Create Date: 2026-07-31

The sync-aware operational routine purges from disk the bytes of artifacts
already backed up in a remote store. Until now that purge also deleted the
`task_artifacts` row, and with it the evidence that the task DID produce the
file: the task became indistinguishable from one that never generated anything,
and the download attempt returned a generic `404`.

`collected_at` marks the row instead of deleting it (soft-mark): the metadata
(rel_path, size_bytes, sha256, content_type) survives as evidence and the
download can explain the cause (`410 Gone`) instead of lying with a `404`.
`collected_note` is optional free text so the routine can leave its
justification (e.g. the reference of the remote push that backs the artifact).

No backfill, deliberately: nothing is collected at migration time, and `NULL` =
artifact available = current behaviour. Past purges already deleted their rows
and are not recoverable.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why each DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0052_task_artifacts_collected"
down_revision = "0051_task_artifacts_created_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE task_artifacts ADD COLUMN collected_at timestamptz;"))
    op.execute(text("ALTER TABLE task_artifacts ADD COLUMN collected_note text;"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE task_artifacts DROP COLUMN IF EXISTS collected_note;"))
    op.execute(text("ALTER TABLE task_artifacts DROP COLUMN IF EXISTS collected_at;"))
