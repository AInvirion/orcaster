"""output_events: allow stream='command' (command echo)

Revision ID: 0040_output_events_cmd_stream
Revises: 0039_workers_cpu_monitoring
Create Date: 2026-06-24

The worker emits a third event type, stream='command' ($ <cmd>), before
running each command of the batch. The inline CHECK of 0031 only allowed
('stdout','stderr'); it is recreated to include 'command'. Non-breaking:
existing rows stay valid. The inline CHECK of 0031 was auto-named by
PostgreSQL as output_events_stream_check.

asyncpg/SQLAlchemy reject multiple ;-separated statements in one prepared
statement; for that reason each DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0040_output_events_cmd_stream"
down_revision = "0039_workers_cpu_monitoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE output_events DROP CONSTRAINT output_events_stream_check;"))
    op.execute(
        text(
            "ALTER TABLE output_events ADD CONSTRAINT output_events_stream_check "
            "CHECK (stream IN ('stdout','stderr','command'));"
        )
    )


def downgrade() -> None:
    # SAFETY PRECONDITION: this downgrade is only safe after stopping all
    # workers that emit stream='command' AND purging/migrating the existing rows
    # with that value. If rows with stream='command' exist, the ADD CONSTRAINT
    # fails when validating against the existing data. And if a worker keeps
    # emitting stream='command' against an already downgraded server, the INSERT
    # hits a CHECK violation (500), which the worker treats as a transient error
    # and retries indefinitely (only the 409 is treated as an abort signal).
    op.execute(text("ALTER TABLE output_events DROP CONSTRAINT output_events_stream_check;"))
    op.execute(
        text(
            "ALTER TABLE output_events ADD CONSTRAINT output_events_stream_check "
            "CHECK (stream IN ('stdout','stderr'));"
        )
    )
