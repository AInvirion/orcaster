"""personal_access_tokens + credential auditing in admin_actions

Revision ID: 0024_personal_access_tokens
Revises: 0023_admin_actions_workspace_id
Create Date: 2026-06-01

User Personal Access Tokens: portable credential for machines, scoped to ONE
workspace, with a SHA-256 hash (same scheme as the worker API key). The
plaintext token (`orcaster_pat_<base64url>`) travels out-of-band and only its
hash is persisted. `expires_at NULL` = no expiry. The role is NOT persisted:
it is resolved dynamically against `workspace_members`.

It also widens the CHECKs of `admin_actions` to audit issuing/revoking/deleting
credentials: it closes the previous limitation (minting/revoking enrollment
tokens was not audited) and enables PAT auditing. Postgres cannot extend a
CHECK in place: it is DROPped and recreated with the union of values,
mirroring the technique of 0003_admin_actions_bulk / 0013_admin_actions_retry.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why each DDL goes in its own `op.execute(text(...))`.
`pgcrypto` is already enabled (0001_init) → gen_random_uuid() available.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0024_personal_access_tokens"
down_revision = "0023_admin_actions_workspace_id"
branch_labels = None
depends_on = None

# CHECK of `admin_actions.action` (ACTUAL state after 0019 — includes auto_retry_task
# from 0017 and fork_task from 0019; omitting them in the DROP+recreate would drop
# them) and its extension for personal access tokens.
_ACTION_OLD = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task','retry_task',"
    "'auto_retry_task','fork_task'"
)
_ACTION_NEW = (
    _ACTION_OLD + ",'mint_pat','revoke_pat','delete_pat',"
    "'mint_enrollment_token','revoke_enrollment_token','delete_enrollment_token'"
)

# CHECK of `admin_actions.target_kind` (original state from 0002) and its extension.
_TARGET_OLD = "'worker','task'"
_TARGET_NEW = _TARGET_OLD + ",'pat','enrollment_token'"


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE personal_access_tokens (
                id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                token_hash    text NOT NULL UNIQUE,
                user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                label         text NULL,
                created_at    timestamptz NOT NULL DEFAULT now(),
                expires_at    timestamptz NULL,
                last_used_at  timestamptz NULL,
                revoked_at    timestamptz NULL
            );
            """
        )
    )
    # Listing by workspace (owner) and filtering by owner (member).
    op.execute(
        text(
            "CREATE INDEX personal_access_tokens_ws_user_idx "
            "ON personal_access_tokens (workspace_id, user_id);"
        )
    )

    # Widen the admin_actions CHECKs to audit credentials.
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_ACTION_NEW}));"
        )
    )
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_target_kind_check;"))
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_target_kind_check "
            f"CHECK (target_kind IN ({_TARGET_NEW}));"
        )
    )


def downgrade() -> None:
    # Restore the original CHECKs. If rows with the new values remain, the
    # downgrade fails (desired: do not silently discard audit rows).
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_target_kind_check;"))
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_target_kind_check "
            f"CHECK (target_kind IN ({_TARGET_OLD}));"
        )
    )
    op.execute(text("ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;"))
    op.execute(
        text(
            "ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check "
            f"CHECK (action IN ({_ACTION_OLD}));"
        )
    )
    op.execute(text("DROP TABLE personal_access_tokens;"))
