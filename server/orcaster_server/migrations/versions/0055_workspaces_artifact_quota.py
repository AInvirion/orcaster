"""workspaces.artifact_quota_bytes: per-workspace artifact quota

Revision ID: 0055_workspaces_artifact_quota
Revises: 0054_workers_free_disk_fraction
Create Date: 2026-10-08

Per-workspace override of `ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES`. Until now a
single global value applied to every workspace; with several workspaces each
one may need its own budget.

Backfill: `NULL` on all existing rows, deliberately. `NULL` = "use the global
default", so the effective quota of every pre-existing workspace is exactly what
it was before and behaviour does not change until the host sets an override.
`ADD COLUMN` without `DEFAULT` leaves all rows at `NULL` without rewriting the
table.

CHECK `> 0`: a quota of 0 or negative would pause the workspace forever (the
poll pauses at `usage >= quota`). Whoever wants to go back to the default sets
`NULL`; there is no sentinel value.

`bigint`: the quota is expressed in bytes and comfortably exceeds the range of
`integer` (10 GiB > 2^31).

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why each DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0055_workspaces_artifact_quota"
down_revision = "0054_workers_free_disk_fraction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE workspaces ADD COLUMN artifact_quota_bytes bigint;"))
    op.execute(
        text(
            "ALTER TABLE workspaces ADD CONSTRAINT workspaces_artifact_quota_bytes_chk "
            "CHECK (artifact_quota_bytes IS NULL OR artifact_quota_bytes > 0);"
        )
    )


def downgrade() -> None:
    op.execute(
        text(
            "ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS workspaces_artifact_quota_bytes_chk;"
        )
    )
    op.execute(text("ALTER TABLE workspaces DROP COLUMN IF EXISTS artifact_quota_bytes;"))
