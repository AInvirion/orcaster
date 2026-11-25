"""workspaces: workspaces, workspace_members, worker_enrollment_tokens, user_active_workspace

Revision ID: 0021_workspaces
Revises: 0020_auth_tables
Create Date: 2026-05-29

Workspace multi-tenancy. Adds the 4 workspace domain tables and the
`workspace_id` columns (FK ON DELETE RESTRICT) to `workers` and `tasks`. The
backfill creates a `Default` workspace (slug `default`) and reassigns all
pre-existing rows to it before enforcing NOT NULL.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why each CREATE TABLE / CREATE INDEX / ALTER goes
in its own `op.execute(text(...))`.

`pgcrypto` is already enabled (0001_init.py → CREATE EXTENSION IF NOT EXISTS
pgcrypto); gen_random_uuid() does not require installing it again.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0021_workspaces"
down_revision = "0020_auth_tables"
branch_labels = None
depends_on = None

# Deterministic UUID of the `Default` workspace. Fixed (not gen_random_uuid()) so
# that the column DEFAULT of workers/tasks and the seeded row always
# match, even after repeated downgrade/upgrade cycles in tests.
# Frozen migration: this literal is the historical anchor and is kept
# self-contained on purpose. The equivalent live constant lives in
# `orcaster_server/workspaces/constants.py::DEFAULT_WORKSPACE_ID` and both must match.
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    # 1. Create the 4 new tables (one statement per op.execute).
    op.execute(
        text(
            """
            CREATE TABLE workspaces (
                id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                name                text NOT NULL,
                slug                text NOT NULL UNIQUE,
                created_by_user_id  uuid NULL REFERENCES users(id) ON DELETE SET NULL,
                created_at          timestamptz NOT NULL DEFAULT now(),
                disabled_at         timestamptz NULL
            );
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE workspace_members (
                workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role          text NOT NULL,
                created_at    timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (workspace_id, user_id),
                CONSTRAINT workspace_members_role_chk
                    CHECK (role IN ('owner', 'member'))
            );
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE worker_enrollment_tokens (
                id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                token_hash           text NOT NULL UNIQUE,
                workspace_id         uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                created_by_user_id   uuid NULL REFERENCES users(id) ON DELETE SET NULL,
                label                text NULL,
                expires_at           timestamptz NOT NULL,
                used_at              timestamptz NULL,
                used_by_worker_id    uuid NULL REFERENCES workers(id) ON DELETE SET NULL,
                revoked_at           timestamptz NULL
            );
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE user_active_workspace (
                user_id       uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                updated_at    timestamptz NOT NULL DEFAULT now()
            );
            """
        )
    )

    # 2. Insert the default workspace with a deterministic id.
    op.execute(
        text(
            "INSERT INTO workspaces (id, name, slug) "
            f"VALUES ('{DEFAULT_WORKSPACE_ID}', 'Default', 'default');"
        )
    )

    # 3. Add workspace_id as NULLABLE to workers and tasks (FK RESTRICT).
    #    DEFAULT = Default workspace: compatibility bridge for app code that
    #    is not workspace-aware yet (the register-worker / create-task routes
    #    do not set workspace_id yet; that comes in a later step, which will
    #    remove this DEFAULT once the INSERT passes the value explicitly).
    #    PostgreSQL does not allow subqueries in column defaults, so the
    #    deterministic UUID DEFAULT_WORKSPACE_ID is used as a literal.
    op.execute(
        text(
            "ALTER TABLE workers ADD COLUMN workspace_id uuid "
            f"DEFAULT '{DEFAULT_WORKSPACE_ID}' REFERENCES workspaces(id) ON DELETE RESTRICT;"
        )
    )
    op.execute(
        text(
            "ALTER TABLE tasks ADD COLUMN workspace_id uuid "
            f"DEFAULT '{DEFAULT_WORKSPACE_ID}' REFERENCES workspaces(id) ON DELETE RESTRICT;"
        )
    )

    # 4. Backfill: reassign pre-existing rows to the Default workspace.
    #    (ADD COLUMN ... DEFAULT already populated the existing rows; this UPDATE is
    #    defensive / explicit for the clarity of the migration.)
    op.execute(text(f"UPDATE workers SET workspace_id = '{DEFAULT_WORKSPACE_ID}';"))
    op.execute(text(f"UPDATE tasks SET workspace_id = '{DEFAULT_WORKSPACE_ID}';"))

    # 5. Enforce NOT NULL now that no rows with workspace_id NULL remain.
    op.execute(text("ALTER TABLE workers ALTER COLUMN workspace_id SET NOT NULL;"))
    op.execute(text("ALTER TABLE tasks ALTER COLUMN workspace_id SET NOT NULL;"))

    # 6. Indexes (one statement per op.execute).
    op.execute(text("CREATE INDEX workers_workspace_idx ON workers (workspace_id);"))
    op.execute(text("CREATE INDEX tasks_workspace_status_idx ON tasks (workspace_id, status);"))


def downgrade() -> None:
    op.execute(text("DROP INDEX tasks_workspace_status_idx;"))
    op.execute(text("DROP INDEX workers_workspace_idx;"))
    op.execute(text("ALTER TABLE tasks DROP COLUMN workspace_id;"))
    op.execute(text("ALTER TABLE workers DROP COLUMN workspace_id;"))
    op.execute(text("DROP TABLE user_active_workspace;"))
    op.execute(text("DROP TABLE worker_enrollment_tokens;"))
    op.execute(text("DROP TABLE workspace_members;"))
    op.execute(text("DROP TABLE workspaces;"))
