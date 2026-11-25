"""snapshot_epoch: snapshot epoch for closing all_available tasks

Revision ID: 0033_snapshot_epoch
Revises: 0032_tasks_attempt_budget
Create Date: 2026-06-06

Success of `all_available` requires the complete snapshot
(`completed = total`). But the admin retry re-snapshots while keeping the
terminal executions of earlier epochs (decision of migration 0014, full
history), so a close that aggregates over the WHOLE history can never reach
`completed = total` after a retry: the task was doomed to `failed` even if the
new epoch completed at 100%.

`snapshot_epoch` identifies the CURRENT snapshot: it starts at 0 in both tables,
the `all_available` retry bumps it +1 in `tasks`, and the re-snapshot stamps the
current value on the new rows of `task_executions`. The `all_available` close
evaluates only the executions of the current epoch
(`e.snapshot_epoch = t.snapshot_epoch`); count mode stays cumulative over the
whole history (epoch-free) and does not use the column.

Backfill: 0 in both tables. Correct for every existing task: none distinguishes
epochs today (the previous `completed >= 1` semantics tolerated the history),
and the historical executions of a retried task become "epoch 0"; the next
retry bumps to 1 and the close is scoped.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0033_snapshot_epoch"
down_revision = "0032_tasks_attempt_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE tasks ADD COLUMN snapshot_epoch integer NOT NULL DEFAULT 0;"))
    op.execute(
        text("ALTER TABLE task_executions ADD COLUMN snapshot_epoch integer NOT NULL DEFAULT 0;")
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE task_executions DROP COLUMN snapshot_epoch;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN snapshot_epoch;"))
