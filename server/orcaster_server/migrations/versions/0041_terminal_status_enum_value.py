"""New terminal value in task_status and execution_status

Revision ID: 0041_terminal_status_enum_value
Revises: 0040_output_events_cmd_stream
Create Date: 2026-06-28

Adds to the ``task_status`` and ``execution_status`` enums a dedicated terminal
value for a successful close with an empty result, distinct from ``completed``
and ``failed``. Migration ``0046_task_outcomes`` later retired that value: that
close is now ``completed`` plus a row in the generic ``outcome`` column.

The value literal (``'completed_no_fingerprint'``) is kept verbatim in the SQL:
it is the name that ``0046`` looks for in order to convert and retire it, and
changing it would change the applied DDL.

This migration ONLY adds the values to the enum. Postgres forbids using a value
just added with ALTER TYPE ADD VALUE inside the SAME transaction, and env.py runs
all migrations in a single transaction. For that reason the ADD VALUE goes in an
autocommit_block (it is committed separately) and the value is NOT referenced in
any other statement of this migration.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0041_terminal_status_enum_value"
down_revision = "0040_output_events_cmd_stream"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # autocommit_block: ALTER TYPE ADD VALUE is committed outside the main
    # transaction of the migration so the value is usable afterwards. IF NOT
    # EXISTS makes the step idempotent on retries.
    with op.get_context().autocommit_block():
        op.execute(
            text("ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'completed_no_fingerprint';")
        )
        op.execute(
            text("ALTER TYPE execution_status ADD VALUE IF NOT EXISTS 'completed_no_fingerprint';")
        )


def downgrade() -> None:
    # Postgres does not support DROP VALUE on an enum. The downgrade is a
    # documented no-op: the value is additive and low risk (an accepted risk by
    # design). Removing it would require recreating the type and rewriting columns.
    pass
