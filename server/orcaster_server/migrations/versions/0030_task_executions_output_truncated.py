"""task_executions: add output_truncated (streaming output, 10 MB cap)

Revision ID: 0030_task_exec_output_truncated
Revises: 0024_personal_access_tokens
Create Date: 2026-06-02

Incremental output streaming (POST /v1/tasks/{id}/output) caps each stream
(stdout/stderr) at a server-side maximum (`settings.output_max_bytes`, default
10 MB). When an append exceeds the cap, the SQL sets `output_truncated=true`
so the dashboard shows a banner. It replaces the `truncated` flag that the
client used to compute (it truncated to 16 KiB before sending).

asyncpg/SQLAlchemy reject multiple `;`-separated statements in one prepared
statement; for that reason each DDL goes in its own `op.execute(text(...))`.

Note: the revision ID was shortened to <=32 chars for alembic_version's VARCHAR(32).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0030_task_exec_output_truncated"
down_revision = "0024_personal_access_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE task_executions "
            "ADD COLUMN output_truncated boolean NOT NULL DEFAULT false;"
        )
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE task_executions DROP COLUMN output_truncated;"))
