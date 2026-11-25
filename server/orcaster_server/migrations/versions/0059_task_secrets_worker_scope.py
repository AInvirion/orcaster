"""task_secrets.worker_id: per-worker scope for task secrets

Revision ID: 0059_task_secrets_worker_scope
Revises: 0058_share_token_ciphertext
Create Date: 2026-11-02

Extends the `task_secrets` table from 0056. Nullable column `worker_id uuid` with
FK to `workers(id) ON DELETE CASCADE`: `NULL` is a workspace-wide secret (the
previous behaviour) and a value is a secret that only that worker receives, with
precedence over the workspace secret of the same name. Deleting the worker (hard
delete) deletes its secrets; the soft delete (`deleted_at`) does not touch them.

Backfill: none beyond the default. All existing rows stay with
`worker_id = NULL`, that is, workspace-wide, which is exactly what they were.

The `UNIQUE (workspace_id, name)` from 0056 is replaced by two partial unique
indexes, portable to any supported Postgres version (it does not depend on
`NULLS NOT DISTINCT`, which requires 15+):

- `(workspace_id, name) WHERE worker_id IS NULL`: one workspace secret per name,
  as before.
- `(workspace_id, name, worker_id) WHERE worker_id IS NOT NULL`: one per name
  and worker.

The service upserts infer each index with `ON CONFLICT (...) WHERE ...`
(https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT). A
non-unique index on `worker_id` covers the FK (the `ON DELETE CASCADE` looks up
by that column).

Downgrade: fails explicitly with `RuntimeError` (naming the count) if any
worker-scoped row exists. Restoring the original UNIQUE with those rows would
fail or, if they were deleted, secrets would be lost silently; the operator
decides to delete them first. Without worker rows, it drops the indexes and the
column and restores `task_secrets_workspace_name_uk`. Before counting it takes
`LOCK TABLE task_secrets IN ACCESS EXCLUSIVE MODE`: a worker-scoped insert not
yet committed is invisible to the count, and without the lock the later `DROP
COLUMN` would turn it into a workspace-wide secret. Before that lock it takes
`LOCK TABLE workers IN ACCESS EXCLUSIVE MODE`, in the same order as the upgrade
(see "Locks and order"): the `DROP COLUMN worker_id` drops the FK and that takes
`ACCESS EXCLUSIVE` on `workers` as well (measured in `pg_locks` with Postgres
18; the ALTER TABLE page only says "An ACCESS EXCLUSIVE lock is acquired unless
explicitly noted"). The count still comes after the `task_secrets` lock.

Locks and order. Alembic runs the whole upgrade in one transaction
(`alembic/env.py`) and "Once acquired, a lock is normally held until the end of
the transaction" (https://www.postgresql.org/docs/current/explicit-locking.html).
The `/poll` updates `workers` and then reads `task_secrets` in the same
transaction; if the migration first took the ACCESS EXCLUSIVE lock on
`task_secrets` and then the FK lock on `workers`, an in-flight poll and the
migration would wait for each other (deadlock). That is why the upgrade first
takes `SHARE ROW EXCLUSIVE` on `workers`, the same lock the FK requests ("Note
that ADD FOREIGN KEY also acquires a SHARE ROW EXCLUSIVE lock on the referenced
table", https://www.postgresql.org/docs/current/sql-altertable.html), and only
then touches `task_secrets`: the same order as the poll. The column is added
without FK and the FK goes separately with `NOT VALID` and `VALIDATE CONSTRAINT`
(the lowest-impact form shown on that page); since everything runs in one
transaction, this does not shorten the lock on `workers`, which lasts until the
COMMIT. Polls that arrive in the meantime wait on `UPDATE workers`; the
migration is metadata plus three indexes on a small table.

`SET LOCAL lock_timeout` (precedent: 0047; see 0058) bounds each wait: if a poll
holds `workers` or `task_secrets` for more than 5 s, the migration fails with
`lock_timeout` and the startup is retried, instead of queueing the traffic.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why each DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0059_task_secrets_worker_scope"
down_revision = "0058_share_token_ciphertext"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s';"))
    # `workers` first, like the /poll (see docstring): no deadlock with an in-flight poll.
    op.execute(text("LOCK TABLE workers IN SHARE ROW EXCLUSIVE MODE;"))
    op.execute(text("ALTER TABLE task_secrets ADD COLUMN worker_id uuid NULL;"))
    op.execute(
        text(
            "ALTER TABLE task_secrets ADD CONSTRAINT task_secrets_worker_id_fkey "
            "FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE CASCADE NOT VALID;"
        )
    )
    op.execute(text("ALTER TABLE task_secrets VALIDATE CONSTRAINT task_secrets_worker_id_fkey;"))
    op.execute(text("ALTER TABLE task_secrets DROP CONSTRAINT task_secrets_workspace_name_uk;"))
    op.execute(
        text(
            "CREATE UNIQUE INDEX task_secrets_workspace_name_wide_uk "
            "ON task_secrets (workspace_id, name) WHERE worker_id IS NULL;"
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX task_secrets_workspace_name_worker_uk "
            "ON task_secrets (workspace_id, name, worker_id) WHERE worker_id IS NOT NULL;"
        )
    )
    op.execute(text("CREATE INDEX task_secrets_worker_idx ON task_secrets (worker_id);"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s';"))  # see upgrade
    # `workers` first, like the upgrade and the /poll (see docstring). Dropping
    # the FK takes ACCESS EXCLUSIVE on `workers`; requesting a lower mode and
    # upgrading it later would reopen the deadlock, so that mode is taken up
    # front.
    op.execute(text("LOCK TABLE workers IN ACCESS EXCLUSIVE MODE;"))
    # Before counting: excludes writers, so an in-flight worker-scoped insert
    # finishes before the count and is counted (see docstring).
    op.execute(text("LOCK TABLE task_secrets IN ACCESS EXCLUSIVE MODE;"))
    scoped = (
        op.get_bind()
        .execute(text("SELECT count(*) FROM task_secrets WHERE worker_id IS NOT NULL"))
        .scalar_one()
    )
    if scoped:
        raise RuntimeError(
            f"cannot downgrade 0059: {scoped} worker-scoped task secret(s) exist; "
            "delete them first (orcaster secret delete NAME --worker WORKER) or they "
            "would be lost"
        )
    op.execute(text("DROP INDEX task_secrets_worker_idx;"))
    op.execute(text("DROP INDEX task_secrets_workspace_name_worker_uk;"))
    op.execute(text("DROP INDEX task_secrets_workspace_name_wide_uk;"))
    op.execute(text("ALTER TABLE task_secrets DROP COLUMN worker_id;"))
    op.execute(
        text(
            "ALTER TABLE task_secrets ADD CONSTRAINT task_secrets_workspace_name_uk "
            "UNIQUE (workspace_id, name);"
        )
    )
