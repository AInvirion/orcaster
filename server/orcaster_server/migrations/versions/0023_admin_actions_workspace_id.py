"""admin_actions: add workspace_id (closes a cross-tenant leak of the audit log)

Revision ID: 0023_admin_actions_workspace_id
Revises: 0022_drop_workspace_id_default
Create Date: 2026-05-29

The `admin_actions` table (audit log of dashboard admin actions) predates
multi-tenancy and had no `workspace_id` column. As a consequence,
`/admin/actions` and `/admin/actions/partial` showed audit rows (with
`target_id`s of tasks/workers) from ALL workspaces to any authenticated
member — a cross-tenant leak.

This migration adds `workspace_id` (FK ON DELETE RESTRICT) and backfills the
pre-existing rows to the deterministic Default workspace
('00000000-0000-0000-0000-000000000001'), which is the only workspace that
existed before multi-tenancy. After the backfill NOT NULL is enforced. No
column DEFAULT is added: after removing the bridge (0022) we stamp
`workspace_id` explicitly on every INSERT, so an audit write without scope
must blow up instead of silently landing in Default.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why each ALTER / CREATE INDEX goes in its own
`op.execute(text(...))`.

Frozen migration: the Default UUID literal is kept self-contained on purpose
(`orcaster_server/workspaces/constants.py` is not imported); the equivalent
live constant lives in `orcaster_server/workspaces/constants.py::DEFAULT_WORKSPACE_ID`.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0023_admin_actions_workspace_id"
down_revision = "0022_drop_workspace_id_default"
branch_labels = None
depends_on = None

# Historical literal of the Default workspace (see docstring). Self-contained on
# purpose; the backfill of pre-multi-tenancy rows reassigns them to this id.
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    # 1. Add workspace_id as NULLABLE (FK RESTRICT). No DEFAULT: stamping is
    #    explicit on every INSERT (see docstring).
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD COLUMN workspace_id uuid "
            "REFERENCES workspaces(id) ON DELETE RESTRICT;"
        )
    )
    # 2. Backfill: pre-multi-tenancy rows belong to the Default workspace.
    op.execute(
        text(
            "UPDATE admin_actions SET workspace_id = "
            f"'{DEFAULT_WORKSPACE_ID}' WHERE workspace_id IS NULL;"
        )
    )
    # 3. Enforce NOT NULL now that no rows with workspace_id NULL remain.
    op.execute(text("ALTER TABLE admin_actions ALTER COLUMN workspace_id SET NOT NULL;"))
    # 4. Index for the scoped listing query (ORDER BY ts DESC).
    op.execute(
        text("CREATE INDEX admin_actions_workspace_ts_idx ON admin_actions (workspace_id, ts);")
    )


def downgrade() -> None:
    op.execute(text("DROP INDEX admin_actions_workspace_ts_idx;"))
    op.execute(text("ALTER TABLE admin_actions DROP COLUMN workspace_id;"))
