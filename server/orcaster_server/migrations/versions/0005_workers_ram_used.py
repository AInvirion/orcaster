"""workers: add `ram_used_mb` column (heartbeat telemetry)

Revision ID: 0005_workers_ram_used
Revises: 0004_workers_name
Create Date: 2026-05-15

Adds `workers.ram_used_mb INTEGER NULL`. The worker reports it on every
heartbeat (htop style: `total - available` as seen by the container). It is
overwritten on every heartbeat — no time series are kept.

Nullable because (a) workers registered before this migration do not have
it, and (b) workers of earlier versions can still heartbeat without
reporting it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_workers_ram_used"
down_revision = "0004_workers_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("ram_used_mb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "ram_used_mb")
