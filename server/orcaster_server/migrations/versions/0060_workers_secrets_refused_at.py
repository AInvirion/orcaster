"""workers.task_secrets_refused_at: last secrets refusal for a worker

Revision ID: 0060_workers_secrets_refused_at
Revises: 0059_task_secrets_worker_scope
Create Date: 2026-11-12

The poll no longer closes as `failed` the execution of a task whose stored
secret it cannot deliver (no keys, or no key decrypts it): it undoes the claim
in a savepoint and the task stays `queued`. Since no execution is left, that
worker's `max(assigned_at)` does not advance and, for the open-claim
fairness-yield (`_SQL_POLL_CLAIM_OPEN_LOCK`), it remains the "least recently
assigned" worker: the others would yield to it forever on tasks it cannot
receive. It is the same deadlock already covered by the exclusion for an active
execution and by the columns of 0053 and 0054, with a new cause (e.g. a worker
whose own row does not decrypt, while the workspace row works for the others).

This column stores the instant of the last refusal. The worker's own poll writes
it, in the poll transaction and outside the savepoint, and the yield predicate
excludes the worker while the mark is recent and the candidate task uses stored
secrets. The mark expires on its own: when the refusal stops happening, the
worker competes again with no action.

It lives in the database and not in memory because the server can run with
several processes (`uvicorn --workers`): the poll of the refused worker and that
of the one that would yield can land in different processes.

No backfill, deliberately: `NULL` means "never refused" and the predicate does
not exclude it, which is the behaviour before the migration. No index:
`workers` is a small table and the predicate already scans those rows.

`ADD COLUMN` of a nullable column without default does not rewrite the table
(https://www.postgresql.org/docs/current/sql-altertable.html, "Notes").
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0060_workers_secrets_refused_at"
down_revision = "0059_task_secrets_worker_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE workers ADD COLUMN task_secrets_refused_at timestamptz;"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE workers DROP COLUMN IF EXISTS task_secrets_refused_at;"))
