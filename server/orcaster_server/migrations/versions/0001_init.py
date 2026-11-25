"""init: enums + workers + tasks + task_executions + worker_commands

Revision ID: 0001_init
Revises:
Create Date: 2026-05-13

Authoritative schema DDL. Pure SQL (no autogenerate). Any schema change
goes in a later migration, never by editing this file.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto;"))

    op.execute(
        text(
            """
            CREATE TYPE task_status AS ENUM (
                'queued','dispatching','paused','completed','cancelled','failed'
            );
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TYPE execution_status AS ENUM (
                'assigned','claimed','running','completed','failed','expired','cancelled'
            );
            """
        )
    )

    op.execute(
        text(
            """
            CREATE TABLE workers (
                id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                api_key_hash       text NOT NULL,
                hostname           text NOT NULL,
                os                 text NOT NULL,
                arch               text NOT NULL,
                ram_mb             int,
                disk_mb            int,
                version            text,
                tags               text[] NOT NULL DEFAULT '{}',
                capabilities       jsonb NOT NULL DEFAULT '{}'::jsonb,
                status             text NOT NULL DEFAULT 'online',
                registered_at      timestamptz NOT NULL DEFAULT now(),
                last_heartbeat_at  timestamptz NOT NULL DEFAULT now()
            ) WITH (fillfactor = 80);
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE workers ADD CONSTRAINT workers_status_chk
                CHECK (status IN ('online','paused','offline'));
            """
        )
    )
    op.execute(text("CREATE UNIQUE INDEX workers_api_key_hash_idx ON workers (api_key_hash);"))
    op.execute(text("CREATE INDEX workers_heartbeat_idx ON workers (last_heartbeat_at);"))
    op.execute(text("CREATE INDEX workers_online_idx ON workers (status) WHERE status='online';"))

    op.execute(
        text(
            """
            CREATE TABLE tasks (
                id                    bigserial PRIMARY KEY,
                queue                 text NOT NULL DEFAULT 'default',
                priority              smallint NOT NULL DEFAULT 0,
                status                task_status NOT NULL DEFAULT 'queued',
                command               text NOT NULL,
                payload               jsonb NOT NULL DEFAULT '{}'::jsonb,
                target_mode           text NOT NULL DEFAULT 'count',
                target_count          int NOT NULL DEFAULT 1,
                requirements          jsonb NOT NULL DEFAULT '{}'::jsonb,
                claim_timeout_seconds int,
                run_at                timestamptz NOT NULL DEFAULT now(),
                created_at            timestamptz NOT NULL DEFAULT now(),
                created_by            text,
                completed_at          timestamptz,
                cancelled_reason      text,
                last_error            text
            ) WITH (fillfactor = 80);
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE tasks ADD CONSTRAINT tasks_target_mode_chk
                CHECK (target_mode IN ('count','all_available'));
            """
        )
    )
    op.execute(
        text(
            """
            ALTER TABLE tasks ADD CONSTRAINT tasks_target_count_chk
                CHECK (target_count >= 1);
            """
        )
    )
    op.execute(
        text(
            """
            CREATE INDEX tasks_dispatch_idx ON tasks (priority DESC, run_at)
                WHERE status IN ('queued','dispatching');
            """
        )
    )

    op.execute(
        text(
            """
            CREATE TABLE task_executions (
                task_id           bigint NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                worker_id         uuid NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
                status            execution_status NOT NULL DEFAULT 'assigned',
                lease_token       uuid,
                lease_expires_at  timestamptz,
                assigned_at       timestamptz NOT NULL DEFAULT now(),
                started_at        timestamptz,
                finished_at       timestamptz,
                last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
                progress          smallint,
                progress_message  text,
                exit_code         int,
                stdout            text,
                stderr            text,
                attempts          int NOT NULL DEFAULT 1,
                PRIMARY KEY (task_id, worker_id)
            ) WITH (fillfactor = 70);
            """
        )
    )
    op.execute(
        text(
            """
            CREATE INDEX te_lease_idx ON task_executions (lease_expires_at)
                WHERE status IN ('claimed','running');
            """
        )
    )
    op.execute(
        text(
            """
            CREATE INDEX te_assigned_idx ON task_executions (worker_id, status)
                WHERE status = 'assigned';
            """
        )
    )
    op.execute(text("CREATE INDEX te_worker_idx ON task_executions (worker_id, status);"))

    op.execute(
        text(
            """
            CREATE TABLE worker_commands (
                id             bigserial PRIMARY KEY,
                uuid           uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
                worker_id      uuid NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
                command        text NOT NULL,
                payload        jsonb,
                signature      bytea NOT NULL,
                expires_at     timestamptz NOT NULL,
                issued_at      timestamptz NOT NULL DEFAULT now(),
                delivered_at   timestamptz,
                acked_at       timestamptz,
                ack_status     text,
                ack_result     jsonb
            ) WITH (fillfactor = 80);
            """
        )
    )
    op.execute(
        text(
            """
            CREATE INDEX worker_commands_pending_idx ON worker_commands (worker_id, issued_at)
                WHERE delivered_at IS NULL;
            """
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS worker_commands CASCADE;"))
    op.execute(text("DROP TABLE IF EXISTS task_executions CASCADE;"))
    op.execute(text("DROP TABLE IF EXISTS tasks CASCADE;"))
    op.execute(text("DROP TABLE IF EXISTS workers CASCADE;"))
    op.execute(text("DROP TYPE IF EXISTS execution_status;"))
    op.execute(text("DROP TYPE IF EXISTS task_status;"))
    # pgcrypto is kept: the extension is shared and may be used by other schemas.
