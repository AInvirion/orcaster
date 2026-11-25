"""tasks.target_worker_pool: restricted pool of worker IDs for targeting

Revision ID: 0010_tasks_target_worker_pool
Revises: 0009_drop_worker_commands
Create Date: 2026-05-19

Adds the `target_worker_pool jsonb NULL` column to `tasks`, which materializes
the extended syntax of the `target` field:

  - `"N"` (integer ≥1)                 → target_worker_pool NULL
  - `"all-available"`                  → target_worker_pool NULL
  - `"worker:<uuid>"`                  → target_worker_pool = ['<uuid>']
  - `"workers:<u1>,<u2>,...,<un>"`     → target_worker_pool = ['<u1>',...,'<un>']
  - `"all:<u1>,...,<un>"`              → snapshot at creation restricted to the subset;
                                          target_worker_pool reflects the pool of the
                                          actual snapshot (list of effective UUIDs).

The claim predicate in `server/orcaster_server/api/poll.py` filters:

    (t.target_worker_pool IS NULL
     OR :worker_id::text IN (SELECT jsonb_array_elements_text(t.target_worker_pool)))

Reversible: `downgrade()` does DROP COLUMN. The column does not take part in
existing CHECK constraints; the DROP is direct.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0010_tasks_target_worker_pool"
down_revision = "0009_drop_worker_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ADD COLUMN target_worker_pool jsonb NULL;"))
    op.execute(
        text(
            "COMMENT ON COLUMN tasks.target_worker_pool IS "
            "'NULL=no restriction; array of worker UUIDs = restricted pool (v0.3.0)';"
        )
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS target_worker_pool;"))
