"""task_secrets: task secrets encrypted per workspace

Revision ID: 0056_task_secrets
Revises: 0055_workspaces_artifact_quota
Create Date: 2026-10-26

Table `task_secrets`: one encrypted value (`MultiFernet`) per name and workspace.
The `/poll` delivers the intersection between `payload.task_env` and these names
as `secrets`, a sibling of `payload` (outside the signature and the plaintext
DB). The fingerprint (`fingerprint`) is an HMAC of the plaintext value with a
key derived from the primary one; it is computed on save and is the only thing
shown of the value.

FKs: `workspace_id ON DELETE CASCADE` (a deleted workspace leaves no orphan
secrets); `updated_by_user_id ON DELETE SET NULL` (deleting the user does not
delete the secret, it only loses the author).

The same migration extends the two `admin_actions` CHECK constraints with the
actions `set_task_secret`, `delete_task_secret`, `deliver_task_secrets` and the
`target_kind` `task_secret`. Same technique as 0048: DROP + recreate the CHECK
with the union of values. The downgrade restores the previous CHECKs; if new
actions were already audited, the ADD CONSTRAINT fails with `check_violation`.
This is intentional and inherited from 0024: audit rows are not silently
discarded.

asyncpg/SQLAlchemy reject multiple `;`-separated statements in a single
prepared statement; that is why each DDL goes in its own op.execute(text(...)).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0056_task_secrets"
down_revision = "0055_workspaces_artifact_quota"
branch_labels = None
depends_on = None

# Current set of the `action` CHECK after 0048, copied verbatim from
# 0048_admin_actions_pause_share.py (its _ACTION_NEW). Omitting any of them here
# would remove it from the CHECK.
_ACTION_OLD = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task','retry_task',"
    "'auto_retry_task','fork_task',"
    "'mint_pat','revoke_pat','delete_pat',"
    "'mint_enrollment_token','revoke_enrollment_token','delete_enrollment_token',"
    "'clear_worker_cache','bulk_retry_task','consume_enrollment_token',"
    "'pause_worker','resume_worker','mint_share_token','revoke_share_token'"
)
_ACTION_NEW = _ACTION_OLD + ",'set_task_secret','delete_task_secret','deliver_task_secrets'"

# Current set of the `target_kind` CHECK after 0048.
_TARGET_OLD = "'worker','task','pat','enrollment_token','share_token'"
_TARGET_NEW = _TARGET_OLD + ",'task_secret'"


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE task_secrets (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                name text NOT NULL,
                ciphertext bytea NOT NULL,
                fingerprint text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                updated_by_user_id uuid NULL REFERENCES users(id) ON DELETE SET NULL,
                CONSTRAINT task_secrets_workspace_name_uk UNIQUE (workspace_id, name)
            );
            """
        )
    )
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
    op.execute(text("DROP TABLE IF EXISTS task_secrets;"))
