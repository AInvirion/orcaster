"""workers: add `disk_used_mb` column (heartbeat telemetry)

Revision ID: 0006_workers_disk_used
Revises: 0005_workers_ram_used
Create Date: 2026-05-15

Adds `workers.disk_used_mb INTEGER NULL`. The worker reports the used space
of the container's rootfs (what limits the pip cache, /tmp, git clone, ad-hoc
task downloads). It is overwritten on every heartbeat — no series are kept.

Nullable because (a) workers registered before this migration do not have
it, and (b) earlier client versions can still heartbeat without reporting it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_workers_disk_used"
down_revision = "0005_workers_ram_used"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("disk_used_mb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "disk_used_mb")
