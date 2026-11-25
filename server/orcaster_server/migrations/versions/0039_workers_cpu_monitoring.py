"""workers_cpu_monitoring: cpu_pct column for per-container telemetry

Revision ID: 0039_workers_cpu_monitoring
Revises: 0038_admin_actions_bulk_retry
Create Date: 2026-06-21

Adds `workers.cpu_pct` (nullable smallint, 0..100): the CPU % attributable to
the worker container, reported in the poll (delta of cgroup `cpu.stat` or, as a
fallback, the sum over the psutil process tree, normalized by cores).

Only CPU adds a column: RAM and disk reuse the existing `ram_*` / `disk_*`
(only their source changes in the client). NULL = no sample yet (the first poll
after startup has no two-point delta) or an old worker.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0039_workers_cpu_monitoring"
down_revision = "0038_admin_actions_bulk_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE workers ADD COLUMN cpu_pct smallint;"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE workers DROP COLUMN cpu_pct;"))
