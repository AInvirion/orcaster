"""output_events: persist execution output as a sequence of events

Revision ID: 0031_output_events
Revises: 0030_task_exec_output_truncated
Create Date: 2026-06-03

Instead of concatenating stdout/stderr into one monolithic text, each chunk
sent by the worker is persisted as an independent event with its timestamp and
a per-execution sequence number. This allows incremental streaming to the
dashboard without reading the whole output, and gives idempotence through
UNIQUE (execution_id, seq) for worker retries.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in one prepared
statement; for that reason each DDL goes in its own `op.execute(text(...))`.

`pgcrypto` is already enabled (0001_init) -> gen_random_uuid() is available.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0031_output_events"
down_revision = "0030_task_exec_output_truncated"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE output_events (
                id           BIGSERIAL PRIMARY KEY,
                execution_id uuid NOT NULL REFERENCES task_executions(id) ON DELETE CASCADE,
                seq          bigint NOT NULL,
                ts           timestamptz NOT NULL,
                stream       text NOT NULL CHECK (stream IN ('stdout','stderr')),
                text         text NOT NULL
            );
            """
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX uq_output_events_execution_seq "
            "ON output_events (execution_id, seq);"
        )
    )


def downgrade() -> None:
    # The UNIQUE INDEX belongs to the table; DROP TABLE removes it implicitly.
    # There is no separate DROP INDEX, consistent with 0024 (which also omits it
    # in downgrade when the index is owned by the table): it drops with the table.
    op.execute(text("DROP TABLE output_events;"))
