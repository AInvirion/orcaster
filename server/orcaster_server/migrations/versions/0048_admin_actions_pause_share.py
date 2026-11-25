"""admin_actions: worker pause/resume + share-token audit

Revision ID: 0048_admin_actions_pause_share
Revises: 0047_tasks_completed_idx
Create Date: 2026-07-25

Extends the two `admin_actions` CHECK constraints in a single migration:

- `action` adds `pause_worker` and `resume_worker` (worker pause/resume control
  from the dashboard) plus `mint_share_token` and `revoke_share_token` (share-link
  audit).
- `target_kind` adds `share_token`, a dedicated value for the public status page
  token. `pat` is not reused.

Same technique as 0003/0013/0017/0019/0024/0034/0038/0042: DROP + recreate the
CHECK with the union of values, because Postgres does not allow extending a CHECK
in place. asyncpg/SQLAlchemy reject multiple statements separated by `;` in a
single prepared statement, so each DDL goes in its own
`op.execute(text(...))`.

The downgrade restores the previous CHECKs; if new actions were already audited,
the ADD CONSTRAINT fails with `check_violation`. This is intentional and
inherited from 0024: audit rows are not silently discarded.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0048_admin_actions_pause_share"
down_revision = "0047_tasks_completed_idx"
branch_labels = None
depends_on = None

# Current set of the `action` CHECK after 0042, copied verbatim from
# 0042_admin_actions_consume_enrollment.py (its _ACTION_OLD plus the value that
# migration added). Omitting any of them here would remove it from the CHECK.
_ACTION_OLD = (
    "'delete_worker','cancel_task','delete_task',"
    "'bulk_cancel_task','bulk_delete_task','retry_task',"
    "'auto_retry_task','fork_task',"
    "'mint_pat','revoke_pat','delete_pat',"
    "'mint_enrollment_token','revoke_enrollment_token','delete_enrollment_token',"
    "'clear_worker_cache','bulk_retry_task','consume_enrollment_token'"
)
_ACTION_NEW = (
    _ACTION_OLD + ",'pause_worker','resume_worker','mint_share_token','revoke_share_token'"
)

# Current set of the `target_kind` CHECK after 0024.
_TARGET_OLD = "'worker','task','pat','enrollment_token'"
_TARGET_NEW = _TARGET_OLD + ",'share_token'"


def upgrade() -> None:
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
