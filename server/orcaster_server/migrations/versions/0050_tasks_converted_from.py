"""tasks.converted_from: target_mode provenance after the sticky conversion

Revision ID: 0050_tasks_converted_from
Revises: 0049_drop_output_columns
Create Date: 2026-07-29

The sticky auto-retry converts an `all_available` task to
`target_mode='count'` to re-route it to the worker of the last attempt. That
conversion lost the epoch scope of the close: the count-mode close aggregates
over the WHOLE history, so the `completed` rows of already closed epochs could
reach `target_count` and close as `completed` a task whose worker was still
failing in the current epoch.

`converted_from` records the provenance: `'all_available'` when the conversion
came from that mode, NULL in any other case (native count). The count-mode close
reads the mark to decide the counting window (current epoch vs full history).

No backfill, deliberately: past conversions are not identifiable (the conversion
left no trace; `target_mode='count'` + `snapshot_epoch>0` is a heuristic that
also matches count tasks retained through other paths), and NULL = current
behaviour, so the default is exact backward compatibility.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0050_tasks_converted_from"
down_revision = "0049_drop_output_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ADD COLUMN converted_from text;"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS converted_from;"))
