"""data_migrate_worker_commands: move worker_commands → tasks + task_executions

Revision ID: 0008_data_migrate_wc
Revises: 0007_schema_poll_endpoint
Create Date: 2026-05-18

Data migration (`data_` prefix). Each row of worker_commands becomes:
  1 row in tasks  (delivery_mode='push', target_worker_id, idempotency_key=wc.uuid,
                    command='__' || wc.command || '__', signed=true, signature=wc.signature)
  1 row in task_executions  (only if wc.delivered_at IS NOT NULL)

State mapped from worker_commands → tasks.status:
  acked_at IS NOT NULL                                   → 'completed'
  delivered_at IS NOT NULL (and not acked)               → 'dispatching'
  no flag                                                → 'queued'

State mapped to task_executions.status (only if delivered_at):
  acked and ack_status IN ('ok','duplicate')             → 'completed'
  acked and ack_status not ok                            → 'failed'
  delivered, not acked                                   → 'running'

`worker_commands` **is not dropped yet**. The DROP TABLE lives in 0009, after
an intermediate 7-day validation window.

If worker_commands is empty, the INSERT ... SELECT inserts 0 rows — it does not fail.

Precondition: there must be no orphan FKs in worker_commands.worker_id; if
there are, the INSERT into task_executions will fail (FK violation against
workers). Orphans must be cleaned up before running this migration.

DOWNGRADE: deletes the rows inserted into tasks (task_executions goes by
CASCADE). Only safe while worker_commands still exists — if 0009 already ran,
that table is gone and the WHERE ... IN (SELECT uuid FROM worker_commands) fails.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0008_data_migrate_wc"
down_revision = "0007_schema_poll_endpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Chained CTE: 1) INSERT into tasks RETURNING id+idempotency_key,
    # 2) INSERT into task_executions joining worker_commands by uuid.
    # The final SELECT count(*) is needed in Postgres to materialize the
    # write CTEs: without an outer SELECT, the INSERT CTEs do not run.
    op.execute(
        text(
            """
            WITH inserted_tasks AS (
                INSERT INTO tasks (
                    queue, priority, status, command, payload,
                    target_mode, target_count, requirements,
                    target_worker_id, delivery_mode, signed, signature,
                    idempotency_key, expires_at, run_at, created_at,
                    completed_at, last_error
                )
                SELECT
                    'admin',
                    100,
                    CASE
                        WHEN wc.acked_at IS NOT NULL THEN 'completed'::task_status
                        WHEN wc.delivered_at IS NOT NULL THEN 'dispatching'::task_status
                        ELSE 'queued'::task_status
                    END,
                    '__' || wc.command || '__',
                    COALESCE(wc.payload, '{}'::jsonb),
                    'count',
                    1,
                    '{}'::jsonb,
                    wc.worker_id,
                    'push',
                    true,
                    wc.signature,
                    wc.uuid,
                    wc.expires_at,
                    wc.issued_at,
                    wc.issued_at,
                    wc.acked_at,
                    CASE
                        WHEN wc.ack_status = 'error' THEN CAST(wc.ack_result AS text)
                        ELSE NULL
                    END
                FROM worker_commands wc
                RETURNING id, idempotency_key
            ),
            inserted_executions AS (
                INSERT INTO task_executions (
                    task_id, worker_id, status,
                    assigned_at, started_at, finished_at
                )
                SELECT
                    it.id,
                    wc.worker_id,
                    CASE
                        WHEN wc.acked_at IS NOT NULL
                             AND wc.ack_status IN ('ok', 'duplicate')
                            THEN 'completed'::execution_status
                        WHEN wc.acked_at IS NOT NULL
                            THEN 'failed'::execution_status
                        ELSE 'running'::execution_status
                    END,
                    wc.issued_at,
                    wc.delivered_at,
                    wc.acked_at
                FROM inserted_tasks it
                JOIN worker_commands wc ON wc.uuid = it.idempotency_key
                WHERE wc.delivered_at IS NOT NULL
                RETURNING task_id
            )
            SELECT count(*) FROM inserted_executions;
            """
        )
    )


def downgrade() -> None:
    # Deletes only the migrated rows. task_executions goes by FK CASCADE.
    # WARNING: if 0009 already ran, worker_commands does not exist and this
    # DELETE fails.
    op.execute(
        text(
            """
            DELETE FROM tasks
            WHERE delivery_mode = 'push'
              AND idempotency_key IS NOT NULL
              AND idempotency_key IN (SELECT uuid FROM worker_commands);
            """
        )
    )
