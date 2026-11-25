"""workspace_share_token.token_ciphertext: recoverable share-link URL

Revision ID: 0058_share_token_ciphertext
Revises: 0057_retired_variant_backfill
Create Date: 2026-10-30

Nullable column `token_ciphertext bytea`: the plaintext token encrypted with the
keyring from `TASK_SECRETS_KEYS_PATH`, so that /admin/share-links can show and
copy the URL of an active link after the mint. Public resolution still uses only
`token_hash`.

No backfill by design: for existing rows only the hash is known, so they stay
NULL ("URL not recoverable") and keep resolving the same. No default. The
downgrade drops the column; links keep working by hash, only the ability to show
their URL again is lost.

`SET LOCAL lock_timeout` (precedent: 0047): the ALTER TABLE takes an
ACCESS EXCLUSIVE lock on `workspace_share_token`; if an open transaction holds
it, the migration fails fast instead of queueing the /status lookups and the
startup behind it. `SET LOCAL` lasts until the end of the current transaction,
and `env.py` runs all the migrations of an `alembic upgrade` (or `downgrade`) in
a single transaction: the 5 s limit also applies to the migrations applied after
this one in the same run, not only to this ALTER TABLE (same as in 0047).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0058_share_token_ciphertext"
down_revision = "0057_retired_variant_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s';"))
    op.execute(text("ALTER TABLE workspace_share_token ADD COLUMN token_ciphertext bytea NULL;"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s';"))  # see upgrade
    op.execute(text("ALTER TABLE workspace_share_token DROP COLUMN token_ciphertext;"))
