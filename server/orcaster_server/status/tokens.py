"""Opaque, revocable tokens of the public status page.

The secret is generated with `secrets.token_urlsafe`. Resolution looks up by
`sha256(token)` hex on the UNIQUE index and discards revoked tokens or tokens of
disabled workspaces (-> the router translates to a generic 404).

With the `TASK_SECRETS_KEYS_PATH` keyring configured, the mint also stores the
encrypted token in `token_ciphertext`, only so that /admin can show the URL of an
active link again. It is encrypted with `keyring.share_links` (derived keys,
separate from those of the task secrets); revoking deletes the ciphertext.
Without a keyring the column stays NULL and the URL is shown only once. The
plaintext token never goes to logs or to the audit.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.task_secrets.keyring import TaskSecretsKeyring

logger = logging.getLogger(__name__)

# URL state of each row in /admin/share-links.
URL_AVAILABLE = "available"
URL_REVOKED = "revoked"
# Row from before 0058 or minted without a keyring: only the hash exists.
URL_NOT_STORED = "not_stored"
# A ciphertext exists but the server has no keys configured.
URL_KEYS_MISSING = "keys_missing"
# A ciphertext exists but no key in the file decrypts it.
URL_UNDECRYPTABLE = "undecryptable"


def generate_share_token() -> str:
    return secrets.token_urlsafe(32)


def hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_SQL_INSERT = text(
    """
    INSERT INTO workspace_share_token (workspace_id, token_hash, token_ciphertext, label)
    VALUES (:ws, :hash, :ct, :label)
    RETURNING id::text
    """
)

# Join to workspaces: discards tokens of disabled workspaces.
_SQL_RESOLVE = text(
    """
    SELECT t.id::text AS id, t.workspace_id::text AS workspace_id
      FROM workspace_share_token t
      JOIN workspaces w ON w.id = t.workspace_id
     WHERE t.token_hash = :hash
       AND t.revoked_at IS NULL
       AND w.disabled_at IS NULL
    """
)

# Throttle threshold in seconds for the last_seen_at UPDATE: avoids write-on-read
# on every anonymous poll (public endpoint without authentication). The
# last_seen_at value does not need sub-minute precision; updating once a minute is
# enough.
_LAST_SEEN_THROTTLE_SECONDS = 60

_SQL_TOUCH = text(
    """
    UPDATE workspace_share_token
       SET last_seen_at = now()
     WHERE id = :id
       AND (last_seen_at IS NULL OR last_seen_at < now() - make_interval(secs => :throttle))
    """
)

_SQL_LIST = text(
    """
    SELECT id::text AS id, label, created_at, last_seen_at, revoked_at,
           token_hash, token_ciphertext
      FROM workspace_share_token
     WHERE workspace_id = :ws
     ORDER BY created_at DESC
    """
)

_SQL_REVOKE = text(
    """
    UPDATE workspace_share_token
       SET revoked_at = now(),
           token_ciphertext = NULL
     WHERE id = :id
       AND workspace_id = :ws
       AND revoked_at IS NULL
    """
)


async def create_share_token(
    session: AsyncSession,
    *,
    workspace_id: str,
    label: str | None,
    keyring: TaskSecretsKeyring | None = None,
) -> tuple[str, str]:
    """Mints a token; encrypts it with `keyring` if there is one (otherwise, only the hash).

    `None` by default: callers that do not show the URL (status page tests) do not
    need the keyring.
    """
    token = generate_share_token()
    ciphertext = keyring.share_links.encrypt(token.encode("utf-8")) if keyring is not None else None
    token_id = (
        await session.execute(
            _SQL_INSERT,
            {
                "ws": workspace_id,
                "hash": hash_share_token(token),
                "ct": ciphertext,
                "label": label,
            },
        )
    ).scalar_one()
    return token, token_id


def _recover_token(
    row: dict[str, Any], keyring: TaskSecretsKeyring | None
) -> tuple[str, str | None]:
    """`(state, token)` of a row for the UI; the token only if available.

    A revoked link is never decrypted. A ciphertext that cannot be read is
    reported with its own state (and a warning without secret material), never as
    if the row predated 0058.
    """
    if row["revoked_at"] is not None:
        return URL_REVOKED, None
    ciphertext = row["token_ciphertext"]
    if ciphertext is None:
        return URL_NOT_STORED, None
    if keyring is None:
        return URL_KEYS_MISSING, None
    try:
        token = keyring.share_links.decrypt(bytes(ciphertext)).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError):
        logger.warning(
            "share token %s: ciphertext not decryptable with any key in TASK_SECRETS_KEYS_PATH",
            row["id"],
        )
        return URL_UNDECRYPTABLE, None
    # Defense: the ciphertext must correspond to the row's hash; otherwise, a URL
    # that would not open the status page of this link is not shown.
    if hash_share_token(token) != row["token_hash"]:
        logger.warning("share token %s: decrypted token does not match its hash", row["id"])
        return URL_UNDECRYPTABLE, None
    return URL_AVAILABLE, token


async def list_share_tokens(
    session: AsyncSession, *, workspace_id: str, keyring: TaskSecretsKeyring | None
) -> list[dict[str, Any]]:
    """Workspace rows with `url_state` and `url_token`; without the ciphertext."""
    rows = (await session.execute(_SQL_LIST, {"ws": workspace_id})).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        row["url_state"], row["url_token"] = _recover_token(row, keyring)
        del row["token_ciphertext"], row["token_hash"]
        out.append(row)
    return out


async def revoke_share_token(session: AsyncSession, *, token_id: str, workspace_id: str) -> bool:
    result = await session.execute(_SQL_REVOKE, {"id": token_id, "ws": workspace_id})
    return result.rowcount > 0


async def resolve_share_token(session: AsyncSession, token: str) -> str | None:
    token_hash = hash_share_token(token)
    row = (await session.execute(_SQL_RESOLVE, {"hash": token_hash})).mappings().one_or_none()
    if row is None:
        return None
    # The lookup already filtered by hash on the UNIQUE index: if there is a row, it
    # matches. Best effort: stamp last_seen_at. The caller commits.
    await session.execute(_SQL_TOUCH, {"id": row["id"], "throttle": _LAST_SEEN_THROTTLE_SECONDS})
    return row["workspace_id"]
