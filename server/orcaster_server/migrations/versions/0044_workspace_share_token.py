"""workspace_share_token: opaque revocable tokens for the public status page

Revision ID: 0044_workspace_share_token
Revises: 0043_enrollment_token_multiuse
Create Date: 2026-06-22

Public status page per workspace: an operator gives a client a link
`/status/{token}`; anyone with the URL sees a read-only aggregated summary; the
operator cuts off access by revoking the token.

The opaque token is NEVER persisted in clear: only its hex `sha256` in
`token_hash` (UNIQUE for O(1) lookup). `revoked_at` NOT NULL => unusable
(soft revoke, the row is kept for audit). `last_seen_at` is updated on every
valid hit. FK ON DELETE CASCADE: deleting the workspace removes its tokens.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0044_workspace_share_token"
down_revision = "0043_enrollment_token_multiuse"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE workspace_share_token (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id uuid NOT NULL
                    REFERENCES workspaces(id) ON DELETE CASCADE,
                token_hash text NOT NULL UNIQUE,
                label text,
                created_at timestamptz NOT NULL DEFAULT now(),
                revoked_at timestamptz,
                last_seen_at timestamptz
            );
            """
        )
    )
    op.execute(
        text(
            "CREATE INDEX workspace_share_token_workspace_idx "
            "ON workspace_share_token (workspace_id);"
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE workspace_share_token;"))
