"""drop workspace_id column DEFAULT on workers and tasks (removes the compatibility bridge)

Revision ID: 0022_drop_workspace_id_default
Revises: 0021_workspaces
Create Date: 2026-05-29

Removes the compatibility bridge. 0021 added to workers.workspace_id and
tasks.workspace_id a column DEFAULT pointing to the deterministic Default
workspace ('00000000-0000-0000-0000-000000000001') so that app code that was
not yet workspace-aware kept working. Now every production INSERT path sets
workspace_id explicitly (register_worker via ORM, and the admin task-creation
routes via SQL), so the DEFAULT is dead code and a footgun: a future INSERT
without scope would silently land in Default. We remove it so that the DB
enforces "every INSERT must specify workspace_id".

NOT NULL and the FK are kept; only the DEFAULT is dropped. asyncpg/SQLAlchemy
reject multiple `;`-separated statements in a single prepared statement; that
is why each ALTER goes in its own `op.execute(text(...))`.

Frozen migration: the Default UUID literal is kept self-contained on purpose
(`orcaster_server/workspaces/constants.py` is not imported); the downgrade
re-adds that DEFAULT so the migration is reversible.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0022_drop_workspace_id_default"
down_revision = "0021_workspaces"
branch_labels = None
depends_on = None

# Historical literal of the Default workspace (see docstring). Self-contained
# on purpose: the equivalent live constant lives in
# `orcaster_server/workspaces/constants.py::DEFAULT_WORKSPACE_ID`.
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute(text("ALTER TABLE workers ALTER COLUMN workspace_id DROP DEFAULT;"))
    op.execute(text("ALTER TABLE tasks ALTER COLUMN workspace_id DROP DEFAULT;"))


def downgrade() -> None:
    op.execute(
        text(f"ALTER TABLE workers ALTER COLUMN workspace_id SET DEFAULT '{DEFAULT_WORKSPACE_ID}';")
    )
    op.execute(
        text(f"ALTER TABLE tasks ALTER COLUMN workspace_id SET DEFAULT '{DEFAULT_WORKSPACE_ID}';")
    )
