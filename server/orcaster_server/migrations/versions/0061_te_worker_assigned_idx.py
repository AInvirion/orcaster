"""Index `task_executions (worker_id, assigned_at)` for the fairness-yield

Revision ID: 0061_te_worker_assigned_idx
Revises: 0060_workers_secrets_refused_at
Create Date: 2026-11-12

The fairness-yield of `_SQL_POLL_CLAIM_OPEN_LOCK` (`orcaster_server/api/poll.py`)
compares the `max(assigned_at)` of each worker that is a candidate to yield with
that of the polling worker. The only per-worker index was `te_worker_idx
(worker_id, status)`, which is not ordered by `assigned_at`: each `max()` read
all the worker's executions (Bitmap Heap Scan), on every idle poll, and the cost
grew with the history. With `(worker_id, assigned_at)` each `max()` is a
backward Index Only Scan with `LIMIT 1`, which does not depend on how many
executions the worker has.

Not partial: the yield looks at all the worker's executions, terminal ones
included. `te_worker_idx` is kept: it is used by the filters on
`(worker_id, status)` and is not subsumed (the second column is different).

Plain `CREATE INDEX`, not `CONCURRENTLY`, consistent with 0037/0045/0047/0051.
`CONCURRENTLY` cannot run inside a transaction, and `env.py` runs the whole
chain inside one that holds the `pg_advisory_xact_lock` that serializes the
concurrent `upgrade head` of `uvicorn --workers N`. Moving it to an
`autocommit_block` would COMMIT that transaction and release the lock in the
middle of the chain. The `CREATE INDEX` takes SHARE on `task_executions` (blocks
writes, not reads) while it builds: measured on Postgres 18 with 125,000
executions (10x the production volume), the build takes ~55 ms. With
MIGRATE_ON_STARTUP it runs before serving traffic. `SET LOCAL lock_timeout`
bounds the lock wait if another session holds the table: the migration fails
fast instead of queueing the traffic behind it.

One statement per op.execute (asyncpg). No imports from `orcaster_server`.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0061_te_worker_assigned_idx"
down_revision = "0060_workers_secrets_refused_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s';"))  # see docstring
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS te_worker_assigned_idx "
            "ON task_executions (worker_id, assigned_at);"
        )
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s';"))  # see upgrade
    op.execute(text("DROP INDEX IF EXISTS te_worker_assigned_idx;"))
