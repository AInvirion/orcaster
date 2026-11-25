"""admin_actions: audit log of dashboard admin operations

Revision ID: 0002_admin_actions
Revises: 0001_init
Create Date: 2026-05-14

Audit table: every destructive action run from the dashboard (delete worker,
cancel task, delete task) leaves a row here. No auth yet — actor stays NULL
for now, reserved for when an admin session is added.

Type decision: text + CHECK instead of ENUM. Reason: new values are expected
over the life of the project (pause/resume/drain/decommission), and evolving
a CHECK is cheaper than ALTER TYPE ADD VALUE.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0002_admin_actions"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE admin_actions (
                id           bigserial PRIMARY KEY,
                ts           timestamptz NOT NULL DEFAULT now(),
                action       text NOT NULL
                             CHECK (action IN ('delete_worker','cancel_task','delete_task')),
                target_kind  text NOT NULL
                             CHECK (target_kind IN ('worker','task')),
                target_id    text NOT NULL,
                reason       text,
                actor        text,
                payload      jsonb
            ) WITH (fillfactor = 90);
            """
        )
    )
    op.execute(
        text("CREATE INDEX admin_actions_target_idx ON admin_actions (target_kind, target_id);")
    )
    op.execute(text("CREATE INDEX admin_actions_ts_idx ON admin_actions (ts DESC);"))


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS admin_actions CASCADE;"))
