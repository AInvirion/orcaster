"""Time-range index for the outcomes/failures hot path

Revision ID: 0047_tasks_completed_idx
Revises: 0046_task_outcomes
Create Date: 2026-07-23

The outcomes endpoint (`/api/v1/insights/outcomes` and the admin page
`/outcomes`) scans `tasks` filtering by `workspace_id` + `status` + a
`completed_at` range (`_SQL_TERMINAL_TASKS.sel` and
`_SQL_TERMINAL_VARIANT_COUNTS`). No index included `completed_at`:
`tasks_workspace_status_idx (workspace_id, status)` covers the prefix but not the
time range. Without it, Postgres underestimates the rows and picks a nested loop
that discards a huge number of them (in production: minutes in the no-window
case). This composite index enables a range scan for the time filter AND fixes
the row estimate that triggers that catastrophic plan.

Consolidation: `tasks_terminal_completed_idx (workspace_id, status,
completed_at)` subsumes by prefix the old `tasks_workspace_status_idx
(workspace_id, status)` (migration 0021): any plan that used the old one can use
the new one equally well. An audit confirmed that nothing references it by name
(no index hints, no existence tests, no coupled queries), so 0047 also drops it:
this avoids keeping two redundant indexes, which would be worse for write
amplification than consolidating into one. Note: this does NOT imply a net
reduction compared to the state before this migration (only
`tasks_workspace_status_idx`). The new index adds `completed_at`, so the closing
UPDATE (the one that sets `completed_at`) now touches it too, and that field
changes on every close. The net effect on HOT updates/write amplification
against the previous state was not verified, only against the alternative of
adding the new index and keeping the old one. The `downgrade` recreates it
identically to 0021 to return exactly to the state of 0046.

Purely additive/idempotent DDL: `CREATE INDEX IF NOT EXISTS` / `DROP INDEX IF
EXISTS`, no data backfill. No `CONCURRENTLY` (consistent with
MIGRATE_ON_STARTUP, which runs inside a transaction). One statement per
op.execute (asyncpg). No imports from `orcaster_server` (self-contained).

Lock hygiene: both the `CREATE INDEX` (SHARE, blocks writes) and the `DROP
INDEX` (ACCESS EXCLUSIVE) take locks on `tasks`, a hot table. In the normal
deploy (atomic restart of a single instance, migration at startup before
serving) there is no contention and the locks are acquired instantly. But if
there were an in-flight query on `tasks` from another source (an external cron,
a manual admin session), the DDL would wait for the lock and queue all the
remaining `tasks` traffic behind it (pileup). `SET LOCAL lock_timeout` bounds
that wait: instead of hanging, the migration fails fast and the deploy is
retried. `SET LOCAL` scopes the timeout to the migration transaction (it does
not touch the rest of the session).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0047_tasks_completed_idx"
down_revision = "0046_task_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fail fast instead of hanging (and queueing traffic) if `tasks` is held
    # by another query; scoped to the migration transaction.
    op.execute(text("SET LOCAL lock_timeout = '5s';"))
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS tasks_terminal_completed_idx "
            "ON tasks (workspace_id, status, completed_at);"
        )
    )
    # The new index subsumes the old one (workspace_id, status) by prefix:
    # dropping it avoids keeping two redundant indexes. It does not imply a net
    # reduction in write amplification against the previous state (the new
    # index adds `completed_at`, which the closing UPDATE also touches).
    op.execute(text("DROP INDEX IF EXISTS tasks_workspace_status_idx;"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s';"))  # see upgrade
    # Exact return to 0046: first recreate the old one (identical to 0021),
    # then drop the new one.
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS tasks_workspace_status_idx ON tasks (workspace_id, status);"
        )
    )
    op.execute(text("DROP INDEX IF EXISTS tasks_terminal_completed_idx;"))
