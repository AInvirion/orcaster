"""workers.last_reported_status: availability self-reported in the poll

Revision ID: 0053_workers_reported_status
Revises: 0052_task_artifacts_collected
Create Date: 2026-08-12

The open-claim fairness-yield (`_SQL_POLL_CLAIM_OPEN_LOCK`) makes a worker yield
the claim if there is ANOTHER worker in the same workspace, online, with a fresh
heartbeat, within the pool and with an older `max(assigned_at)`. An earlier fix
excluded from that predicate the workers with an active execution, using the
existence of the execution as a proxy for "this worker is busy and will not
claim now".

The worker's disk gate broke that proxy: a worker under the disk floor reports
`status='busy'` in the poll WITHOUT having any execution row, keeps
`workers.status='online'` (the poll UPDATE forces `online` except when paused),
heartbeats every 5 s and its `max(assigned_at)` only ages. It satisfies every leg
of the predicate and forces the others to yield, but it never claims itself
because steps 5 and 6 of the pick only run with `status='idle'`. Result: tasks
stay `queued` while the whole fleet is idle. The same hole existed in a narrower
form for a worker running a `clear_cache` (it reports `busy` without an
execution until `cache_flush_timeout_seconds`).

This column persists the last `body.status` the worker reported, so the
predicate can also exclude one that declares itself unavailable.

No backfill, deliberately: `NULL` means "unknown" (a registered worker that has
not yet polled after the deploy) and the predicate treats it as it does today,
that is, it does NOT exclude it from the yield. This way the existing fleet does
not change behaviour until each worker makes its first poll, and the new server
works the same against old and new workers: the body's `status` field is already
mandatory in the current contract (`PollRequest.status`), so any worker that
polls populates it without a contract change.

No CHECK: the domain (`idle`/`busy`) is already bounded by the Pydantic
`Literal` at the edge, and the column is independent of `workers.status`, whose
CHECK (`online|paused|offline`) is not touched. No index: `workers` is a small
table and the predicate already scans those rows for other reasons.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why the DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0053_workers_reported_status"
down_revision = "0052_task_artifacts_collected"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE workers ADD COLUMN last_reported_status text;"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE workers DROP COLUMN IF EXISTS last_reported_status;"))
