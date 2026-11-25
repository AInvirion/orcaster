"""enrollment tokens: multi-use (max_uses/use_count) + uses table

Revision ID: 0043_enrollment_token_multiuse
Revises: 0042_admin_actions_consume
Create Date: 2026-07-03

Adds to worker_enrollment_tokens the ceiling `max_uses` (NOT NULL, default 1:
no unlimited option) and the materialized counter `use_count` (concurrency
control point of the consumption). Backfill: existing rows stay exactly
single-use (max_uses=1; use_count=1 if already used, 0 if not). Creates the
history table `worker_enrollment_token_uses` (one row per consumption).

The downgrade drops the table and both columns.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0043_enrollment_token_multiuse"
down_revision = "0042_admin_actions_consume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text("ALTER TABLE worker_enrollment_tokens ADD COLUMN max_uses int NOT NULL DEFAULT 1;")
    )
    op.execute(
        text("ALTER TABLE worker_enrollment_tokens ADD COLUMN use_count int NOT NULL DEFAULT 0;")
    )
    # Backfill: tokens already used count as 1 consumption (historical single-use).
    op.execute(text("UPDATE worker_enrollment_tokens SET use_count = 1 WHERE used_at IS NOT NULL;"))
    op.execute(
        text(
            """
            CREATE TABLE worker_enrollment_token_uses (
                id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                token_id   uuid NOT NULL REFERENCES worker_enrollment_tokens(id) ON DELETE CASCADE,
                worker_id  uuid NULL REFERENCES workers(id) ON DELETE SET NULL,
                used_at    timestamptz NOT NULL
            );
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX worker_enrollment_token_uses_token_idx "
            "ON worker_enrollment_token_uses (token_id);"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE worker_enrollment_token_uses;"))
    op.execute(text("ALTER TABLE worker_enrollment_tokens DROP COLUMN use_count;"))
    op.execute(text("ALTER TABLE worker_enrollment_tokens DROP COLUMN max_uses;"))
