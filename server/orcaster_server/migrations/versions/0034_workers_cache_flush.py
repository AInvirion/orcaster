"""workers_cache_flush: on-demand worker cache cleanup

Revision ID: 0034_workers_cache_flush
Revises: 0033_snapshot_epoch
Create Date: 2026-06-09

An operator can force the cleanup of a worker's disk cache (task scratch +
UV cache) from the dashboard. The order travels as a control signal in the poll
response (a lane separate from the task pipeline) and is deferred until the
worker reports `idle`.

Two NULLABLE timestamptz columns on `workers` model the request cycle:
- `cache_flush_requested_at`: the admin stamps now() when requesting.
- `cache_flush_done_at`: the worker acks and the server stamps now() on sealing.

Derived state (not persisted): pending when requested_at IS NOT NULL and
(done_at IS NULL OR done_at < requested_at); done when done_at >= requested_at.

Backfill: NULL in both for every existing row (= never requested), which is correct.

Extends the CHECK `admin_actions_action_check` (DROP+ADD technique of
0013/0017/0019/0024) to audit the new action `clear_worker_cache`. The previous
set is that of 0024.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0034_workers_cache_flush"
down_revision = "0033_snapshot_epoch"
branch_labels = None
depends_on = None

# Set in force for the CHECK after 0024 (do not assume 0013).
_ACTION_OLD = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task','retry_task',"
    "'auto_retry_task','fork_task',"
    "'mint_pat','revoke_pat','delete_pat',"
    "'mint_enrollment_token','revoke_enrollment_token','delete_enrollment_token'"
)
# New set: the previous one + clear_worker_cache.
_ACTION_NEW = _ACTION_OLD + ",'clear_worker_cache'"


def upgrade() -> None:
    op.execute(text("ALTER TABLE workers ADD COLUMN cache_flush_requested_at timestamptz;"))
    op.execute(text("ALTER TABLE workers ADD COLUMN cache_flush_done_at timestamptz;"))
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_ACTION_NEW}));"
        )
    )


def downgrade() -> None:
    # Restore the previous CHECK. If 'clear_worker_cache' rows remain, the
    # ADD CONSTRAINT fails (intended: do not silently discard audit rows).
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_ACTION_OLD}));"
        )
    )
    op.execute(text("ALTER TABLE workers DROP COLUMN cache_flush_done_at;"))
    op.execute(text("ALTER TABLE workers DROP COLUMN cache_flush_requested_at;"))
