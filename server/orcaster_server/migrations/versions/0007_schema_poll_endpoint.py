"""tasks + task_executions: columns for the unified poll endpoint and targeted push delivery

Revision ID: 0007_schema_poll_endpoint
Revises: 0006_workers_disk_used
Create Date: 2026-05-18

Adds six columns to tasks, four CHECK constraints, two partial indexes and
one column to task_executions that enable targeted push tasks and
exactly-once delivery of cancel signals without a separate worker_commands
table.

Columns added to tasks:
  target_worker_id  uuid NULL       — destination worker of a push task
  delivery_mode     text NOT NULL DEFAULT 'claim'  — 'claim'|'push'
  signed            boolean NOT NULL DEFAULT false  — carries an Ed25519 signature
  signature         bytea NULL      — raw signature bytes
  idempotency_key   uuid NULL       — deduplication; in the data migration = wc.uuid
  expires_at        timestamptz NULL — TTL of the push command; NULL = no expiry

Column added to task_executions:
  cancel_notified_at timestamptz NULL — delivery mark of the cancel signal to the worker
                                       (UPDATE ... WHERE cancel_notified_at IS NULL
                                       RETURNING guarantees exactly-once delivery)

`idempotency_key` carries a partial UNIQUE index: two tasks with the same
idempotency_key cannot coexist. It enables UPSERT by key in the unified admin
endpoint.

Reversible: downgrade drops indexes, constraints and columns in reverse order.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0007_schema_poll_endpoint"
down_revision = "0006_workers_disk_used"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tasks: 6 new columns. ADD COLUMN NOT NULL DEFAULT '<const>' is
    # metadata-only in Postgres 11+ (no table rewrite).
    op.execute(text("ALTER TABLE tasks ADD COLUMN target_worker_id uuid NULL;"))
    op.execute(text("ALTER TABLE tasks ADD COLUMN delivery_mode text NOT NULL DEFAULT 'claim';"))
    op.execute(text("ALTER TABLE tasks ADD COLUMN signed boolean NOT NULL DEFAULT false;"))
    op.execute(text("ALTER TABLE tasks ADD COLUMN signature bytea NULL;"))
    op.execute(text("ALTER TABLE tasks ADD COLUMN idempotency_key uuid NULL;"))
    op.execute(text("ALTER TABLE tasks ADD COLUMN expires_at timestamptz NULL;"))

    # FK separate from the ADD COLUMN for readability and drop ordering.
    op.execute(
        text(
            """
            ALTER TABLE tasks
                ADD CONSTRAINT tasks_target_worker_id_fkey
                FOREIGN KEY (target_worker_id) REFERENCES workers(id) ON DELETE CASCADE;
            """
        )
    )

    # CHECK constraints: delivery_mode domain, push requires a target, a target
    # requires target_mode='count' and target_count=1, signed requires signature.
    op.execute(
        text(
            """
            ALTER TABLE tasks ADD CONSTRAINT tasks_delivery_mode_chk
                CHECK (delivery_mode IN ('claim', 'push'));
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE tasks ADD CONSTRAINT tasks_push_requires_target_worker_chk
                CHECK (
                    delivery_mode = 'claim'
                    OR (delivery_mode = 'push' AND target_worker_id IS NOT NULL)
                );
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE tasks ADD CONSTRAINT tasks_target_worker_exclusive_chk
                CHECK (
                    target_worker_id IS NULL
                    OR (target_mode = 'count' AND target_count = 1)
                );
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE tasks ADD CONSTRAINT tasks_signed_requires_signature_chk
                CHECK (
                    signed = false
                    OR (signed = true AND signature IS NOT NULL)
                );
            """
        )
    )

    # Partial index to dispatch push tasks by destination worker.
    op.execute(
        text(
            """
            CREATE INDEX tasks_push_pending_idx ON tasks (target_worker_id, created_at)
                WHERE delivery_mode = 'push'
                  AND status IN ('queued', 'dispatching');
            """
        )
    )

    # Partial UNIQUE index on idempotency_key when NOT NULL. Serves two
    # roles: (a) UPSERT by key in the unified admin endpoint, (b) fast lookup
    # to validate duplicates.
    op.execute(
        text(
            """
            CREATE UNIQUE INDEX tasks_idempotency_key_idx ON tasks (idempotency_key)
                WHERE idempotency_key IS NOT NULL;
            """
        )
    )

    # task_executions: exactly-once delivery mark for cancel signals.
    op.execute(text("ALTER TABLE task_executions ADD COLUMN cancel_notified_at timestamptz NULL;"))
    op.execute(
        text(
            """
            CREATE INDEX task_executions_cancel_notified_idx
                ON task_executions (cancel_notified_at)
                WHERE cancel_notified_at IS NOT NULL;
            """
        )
    )


def downgrade() -> None:
    # Strict reverse order: indexes, task_executions columns, tasks
    # constraints, FK, tasks columns. IF EXISTS as a safeguard.
    op.execute(text("DROP INDEX IF EXISTS task_executions_cancel_notified_idx;"))
    op.execute(text("ALTER TABLE task_executions DROP COLUMN IF EXISTS cancel_notified_at;"))

    op.execute(text("DROP INDEX IF EXISTS tasks_idempotency_key_idx;"))
    op.execute(text("DROP INDEX IF EXISTS tasks_push_pending_idx;"))

    op.execute(
        text("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_signed_requires_signature_chk;")
    )
    op.execute(
        text("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_target_worker_exclusive_chk;")
    )
    op.execute(
        text("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_push_requires_target_worker_chk;")
    )
    op.execute(text("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_delivery_mode_chk;"))
    op.execute(text("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_target_worker_id_fkey;"))

    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS expires_at;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS idempotency_key;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS signature;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS signed;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS delivery_mode;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN IF EXISTS target_worker_id;"))
