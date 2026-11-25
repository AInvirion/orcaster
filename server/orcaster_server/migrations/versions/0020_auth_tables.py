"""auth: users, passkey_credentials, sessions, invitations, webauthn_challenges

Revision ID: 0020_auth_tables
Revises: 0019_tasks_fork_lineage_retry
Create Date: 2026-05-29

Human identity tables of the portable module `orcaster_server/auth/`. They do not know the
concept of a workspace (the invitations payload is opaque jsonb that the host
fills in). Sensitive credentials (invitation token, session id carried in
the cookie) are persisted only as a SHA-256 hex hash, like workers.api_key_hash.

`pgcrypto` is already enabled (0001_init.py → CREATE EXTENSION IF NOT EXISTS
pgcrypto); gen_random_uuid() does not require installing it again.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0020_auth_tables"
down_revision = "0019_tasks_fork_lineage_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE users (
                id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                display_name  text NOT NULL,
                email         text NULL,
                created_at    timestamptz NOT NULL DEFAULT now(),
                disabled_at   timestamptz NULL
            );
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE passkey_credentials (
                id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                credential_id     bytea NOT NULL UNIQUE,
                public_key_cose   bytea NOT NULL,
                sign_count        bigint NOT NULL DEFAULT 0,
                transports        text[] NOT NULL DEFAULT '{}',
                aaguid            uuid NULL,
                backup_eligible   boolean NOT NULL DEFAULT false,
                backup_state      boolean NOT NULL DEFAULT false,
                label             text NULL,
                created_at        timestamptz NOT NULL DEFAULT now(),
                last_used_at      timestamptz NULL
            );
            """
        )
    )
    op.execute(text("CREATE INDEX passkey_credentials_user_idx ON passkey_credentials (user_id);"))
    op.execute(
        text(
            """
            CREATE TABLE sessions (
                id            text PRIMARY KEY,
                user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at    timestamptz NOT NULL DEFAULT now(),
                last_seen_at  timestamptz NOT NULL DEFAULT now(),
                expires_at    timestamptz NOT NULL,
                user_agent    text NULL,
                ip            text NULL,
                revoked_at    timestamptz NULL
            );
            """
        )
    )
    op.execute(text("CREATE INDEX sessions_user_idx ON sessions (user_id);"))
    op.execute(
        text(
            """
            CREATE TABLE invitations (
                id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                token_hash           text NOT NULL UNIQUE,
                created_by_user_id   uuid NULL REFERENCES users(id) ON DELETE SET NULL,
                payload              jsonb NOT NULL DEFAULT '{}',
                label                text NULL,
                expires_at           timestamptz NOT NULL,
                accepted_at          timestamptz NULL,
                accepted_by_user_id  uuid NULL REFERENCES users(id) ON DELETE SET NULL,
                revoked_at           timestamptz NULL
            );
            """
        )
    )
    op.execute(
        text(
            """
            CREATE TABLE webauthn_challenges (
                id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                challenge   bytea NOT NULL,
                kind        text NOT NULL,
                user_id     uuid NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at  timestamptz NOT NULL DEFAULT now(),
                expires_at  timestamptz NOT NULL,
                CONSTRAINT webauthn_challenges_kind_chk
                    CHECK (kind IN ('registration', 'authentication'))
            );
            """
        )
    )


def downgrade() -> None:
    op.execute(text("DROP TABLE webauthn_challenges;"))
    op.execute(text("DROP TABLE invitations;"))
    op.execute(text("DROP TABLE sessions;"))
    op.execute(text("DROP TABLE passkey_credentials;"))
    op.execute(text("DROP TABLE users;"))
