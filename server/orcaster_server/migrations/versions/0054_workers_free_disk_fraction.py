"""workers.free_disk_fraction: self-reported free disk fraction

Revision ID: 0054_workers_free_disk_fraction
Revises: 0053_workers_reported_status
Create Date: 2026-08-22

Server-side disk gate. The worker will send in the poll the free disk fraction
(`free/total` of the cache volume, in bytes). The server applies the central
threshold `worker_claim_min_free_fraction` and decides whether to:
  - hold new tasks for that worker (`storage_paused` pattern),
  - exclude it from the open-claim fairness-yield (so as not to reopen the yield
    deadlock when the worker reports `idle` while gated), and
  - skip it in the `all_available` snapshot so as not to leave a stuck
    `assigned` pre-assignment.

This column persists that fraction. It is INDEPENDENT of `disk_used_mb`, which is
display telemetry: the fraction is measured in bytes and excludes the
filesystem's reserved blocks, so it is not derived from `total_mb - used_mb`.

No backfill, deliberately (same criterion as 0053_workers_reported_status):
`NULL` means "unknown": an old worker that does not send the field, or a volume
that could not be measured. The gate, the fairness-yield and the snapshot treat
`NULL` as "do not gate / do not exclude / do not skip", so the existing fleet
does not change behaviour and the new server works the same against old and new
workers. This is the safe server-first rollout: the server stays inert until the
workers start reporting the field (separate worker change).

No CHECK: the domain (0.0..1.0) is normalized by the Pydantic schema at the edge
with a lenient validator (out of range -> NULL = fail-open, no 422 on the hot
path), and a nullable column without constraint survives any value that would
degrade to "do not gate" anyway. No index: `workers` is a small table and the
predicates that read it already scan those rows for other reasons (same
justification as 0053).

double precision (not numeric): the fraction is a telemetry float, not a
monetary value that requires decimal exactness.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why the DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0054_workers_free_disk_fraction"
down_revision = "0053_workers_reported_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE workers ADD COLUMN free_disk_fraction double precision;"))


def downgrade() -> None:
    op.execute(text("ALTER TABLE workers DROP COLUMN IF EXISTS free_disk_fraction;"))
