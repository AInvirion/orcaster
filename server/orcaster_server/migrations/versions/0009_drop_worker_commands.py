"""drop_worker_commands: POINT OF NO RETURN. Drops the worker_commands table.

Revision ID: 0009_drop_worker_commands
Revises: 0008_data_migrate_worker_commands
Create Date: 2026-05-18

This migration is applied **at least 7 days after** the data migration of 0008,
with explicit operator GO. Once applied, code written against the old model
cannot operate against the DB because it assumes the table exists in
workers.py / admin_workers.py / commands.py / housekeeping.py.

Downgrade rebuilds the table but **does NOT restore the data** — it was
already migrated to `tasks` by 0008 and kept evolving there. Only useful for
idempotent-cycle tests (upgrade → downgrade → upgrade) in CI.

The DDL of the table and its index is a literal copy of 0001_init.py — any
future historical change to the worker_commands schema should be reflected
here if we still intend to keep this downgrade reversible for tests.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0009_drop_worker_commands"
down_revision = "0008_data_migrate_wc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CASCADE to clean up dependencies (FK from workers, indexes, implicit
    # bigserial sequence). In practice there are no external FKs to
    # worker_commands, but CASCADE is a safeguard in case any object is left.
    op.execute(text("DROP TABLE IF EXISTS worker_commands CASCADE;"))


def downgrade() -> None:
    # Literal DDL from 0001_init.py so that the upgrade→downgrade→upgrade cycle
    # stays idempotent (test_migrations.test_upgrade_is_idempotent_after_downgrade).
    # Data NOT restored — the recreated table starts empty.
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
