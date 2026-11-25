"""workers: add optional `name` column (dashboard label)

Revision ID: 0004_workers_name
Revises: 0003_admin_actions_bulk
Create Date: 2026-05-15

Adds `workers.name TEXT NULL`. The name is an optional label that the
operator sets in `client/.env` (`ORCH_WORKER_NAME`) and the worker sends on
every `register` and `heartbeat`. The server persists it and shows it in the
dashboard. Changing the name in `.env` and restarting the container shows up
in the dashboard on the next heartbeat.

Nullable because (a) workers registered before this migration have no name,
and (b) `ORCH_WORKER_NAME` remains optional.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_workers_name"
down_revision = "0003_admin_actions_bulk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "name")
