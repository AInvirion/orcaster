"""Personal Access Token service.

The plaintext token has the format `orcaster_pat_<base64url(32B)>`: the prefix
makes it identifiable by secret scanners. Only its SHA-256 is
persisted in the DB (same helper as the worker API key). The secret is shown
only once, when issued.

`resolve_pat` resolves the role **dynamically** against `workspace_members`: the
PAT does not freeze the role, so a membership change is reflected immediately.
The invalidation logic (revoked/expired/disabled user) and the non-membership
case are modelled with `PatState`; the caller (`require_pat_workspace`)
translates them to 401/403.

list/revoke/delete mirror the role-asymmetric policy of the enrollment tokens:
the `user_id` filter (member path) limits to the user's own PATs; without the
filter (owner path) it covers all the PATs of the workspace. No function
commits; the caller does.
"""

from __future__ import annotations

import enum
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.config import hash_token
from orcaster_server.auth.models import User
from orcaster_server.auth.rate_limit import BoundedWarning, KeyedBoundedWarning
from orcaster_server.pats.models import PersonalAccessToken
from orcaster_server.workspaces.models import WorkspaceMember

PAT_TOKEN_PREFIX = "orcaster_pat_"
# Unsupported legacy token prefix. Never accepted; it only gets an explicit 401
# message so the holder knows to mint a new token instead of a generic failure.
LEGACY_PAT_TOKEN_PREFIX = "taskor_pat_"
LEGACY_PAT_REJECTION_MESSAGE = (
    f"token prefix '{LEGACY_PAT_TOKEN_PREFIX}' is an unsupported legacy name and is no "
    f"longer accepted; mint a new token (prefix '{PAT_TOKEN_PREFIX}') and run "
    "'orcaster auth login'"
)

logger = logging.getLogger(__name__)

# WARNING for the PAT of a disabled user: at most one line per (PAT id) and
# window, with the count of requests rejected since the previous line (the first
# one carries 1). In-process memory state: it is lost on restart and each
# uvicorn process counts on its own. The cap on remembered PATs bounds memory;
# when exceeded, the least recent one is evicted.
DISABLED_WARNING_INTERVAL_S = 300.0
DISABLED_WARNING_MAX_PATS = 1024
_DISABLED_WARNING_TEMPLATE = (
    "PAT rejected: user %s is disabled (pat %s); %d request(s) since the last line"
)
_disabled_warning = KeyedBoundedWarning(
    logger,
    _DISABLED_WARNING_TEMPLATE,
    interval=DISABLED_WARNING_INTERVAL_S,
    max_keys=DISABLED_WARNING_MAX_PATS,
)


# WARNING for tokens with the unsupported legacy prefix: one line per window with
# the count of rejected requests. Fixed message, never the token.
LEGACY_PREFIX_WARNING_INTERVAL_S = 300.0
_legacy_prefix_warning = BoundedWarning(
    logger,
    "PAT rejected: unsupported legacy token prefix %r; %d request(s) since the last line",
    interval=LEGACY_PREFIX_WARNING_INTERVAL_S,
    args=(LEGACY_PAT_TOKEN_PREFIX,),
)


class PatState(enum.Enum):
    OK = "ok"
    INVALID = "invalid"  # missing/unknown/revoked/expired/disabled owner -> generic 401
    LEGACY_PREFIX = "legacy_prefix"  # unsupported legacy prefix -> explicit 401
    NOT_MEMBER = "not_member"  # valid token but the user is no longer a member -> 403


@dataclass
class PatResolution:
    state: PatState
    user_id: str | None = None
    workspace_id: str | None = None
    role: str | None = None
    # Token metadata (populated only in the OK state), for `GET /auth/whoami`.
    token_label: str | None = None
    # `last_used_at` PRIOR to this resolution: resolve_pat overwrites it with `now`,
    # but whoami shows the previous use (the current one would trivially be "now").
    token_last_used_at: datetime | None = None
    # Token expiry (None = does not expire): whoami consumes it to warn in the CLI.
    token_expires_at: datetime | None = None


def generate_pat_token() -> str:
    """Generates a plaintext token with an identifiable prefix."""
    return PAT_TOKEN_PREFIX + secrets.token_urlsafe(32)


async def mint_pat_returning_id(
    db: AsyncSession,
    *,
    user_id: str,
    workspace_id: str,
    label: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, str]:
    """Like `mint_pat` but returns `(plaintext_token, pat_id)`.

    The id is needed to audit the mint (`record_admin_action` requires
    `target_id`); the `RETURNING` avoids a second round trip. The dashboard
    handler uses it; the remaining callers (tests, API) use `mint_pat`.
    """
    token = generate_pat_token()
    res = await db.execute(
        insert(PersonalAccessToken)
        .values(
            token_hash=hash_token(token),
            user_id=user_id,
            workspace_id=workspace_id,
            label=label,
            expires_at=expires_at,
        )
        .returning(PersonalAccessToken.id)
    )
    return token, str(res.scalar_one())


async def mint_pat(
    db: AsyncSession,
    *,
    user_id: str,
    workspace_id: str,
    label: str | None = None,
    expires_at: datetime | None = None,
) -> str:
    """Issues a PAT and returns the plaintext token (to be shown only once)."""
    token, _ = await mint_pat_returning_id(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
        label=label,
        expires_at=expires_at,
    )
    return token


async def resolve_pat(db: AsyncSession, token: str) -> PatResolution:
    """Resolves a Bearer token to (user, workspace, role) or a failure state.

    Order of the checks: unknown or revoked token, expired token and disabled
    owner give `INVALID`; only then the membership (`NOT_MEMBER`). A disabled
    owner who also stopped being a member gets `INVALID`, not `NOT_MEMBER`.
    Generic message for all invalidity modes (which check failed is not
    disclosed). A rejection writes nothing to the PAT. `last_used_at` is
    updated best effort without commit (the caller commits it) only in the OK
    state.
    """
    if token.startswith(LEGACY_PAT_TOKEN_PREFIX):
        # Never looked up: the prefix alone decides. The explicit state lets the
        # caller tell the holder to mint a new token instead of a generic 401.
        _legacy_prefix_warning.hit()
        return PatResolution(PatState.LEGACY_PREFIX)
    if not token or not token.startswith(PAT_TOKEN_PREFIX):
        return PatResolution(PatState.INVALID)
    row = await db.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.token_hash == hash_token(token))
    )
    pat = row.scalar_one_or_none()
    if pat is None or pat.revoked_at is not None:
        return PatResolution(PatState.INVALID)
    now = datetime.now(UTC)
    if pat.expires_at is not None and pat.expires_at <= now:
        return PatResolution(PatState.INVALID)

    # Disabled user: like the session and the passkey login, its PAT stops being
    # valid. It is evaluated on every request (no frozen data), so re-enabling the
    # user restores it. Same generic 401; the log (bounded per PAT and window)
    # carries the user and PAT ids, never the token.
    disabled_at = (
        await db.execute(select(User.disabled_at).where(User.id == pat.user_id))
    ).scalar_one_or_none()
    if disabled_at is not None:
        _disabled_warning.hit(str(pat.id), pat.user_id, pat.id)
        return PatResolution(PatState.INVALID)

    # Dynamic role against workspace_members: it is not frozen in the token.
    role = (
        await db.execute(
            select(WorkspaceMember.role).where(
                WorkspaceMember.workspace_id == pat.workspace_id,
                WorkspaceMember.user_id == pat.user_id,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        return PatResolution(PatState.NOT_MEMBER)

    previous_last_used_at = pat.last_used_at
    await db.execute(
        update(PersonalAccessToken).where(PersonalAccessToken.id == pat.id).values(last_used_at=now)
    )
    return PatResolution(
        PatState.OK,
        user_id=pat.user_id,
        workspace_id=pat.workspace_id,
        role=role,
        token_label=pat.label,
        token_last_used_at=previous_last_used_at,
        token_expires_at=pat.expires_at,
    )


async def list_pats(
    db: AsyncSession,
    workspace_id: str,
    *,
    user_id: str | None = None,
) -> list[PersonalAccessToken]:
    """Lists the PATs of a workspace (metadata, never the secret).

    With `user_id` it filters to the given owner (member path: only their own).
    Without the filter it returns all of the workspace's PATs (owner path).
    Ordered by `created_at` descending.
    """
    q = (
        select(PersonalAccessToken)
        .where(PersonalAccessToken.workspace_id == workspace_id)
        .order_by(PersonalAccessToken.created_at.desc())
    )
    if user_id is not None:
        q = q.where(PersonalAccessToken.user_id == user_id)
    res = await db.execute(q)
    return list(res.scalars().all())


async def revoke_pat_by_id(
    db: AsyncSession,
    *,
    token_id: str,
    workspace_id: str,
    user_id: str | None = None,
) -> bool:
    """Revokes a PAT by id, scoped to the workspace. True if a row matched.

    The UPDATE filters by `(id, workspace_id)`; with `user_id` it adds the owner
    filter (a member cannot revoke other people's PATs -> False -> 404 in the
    handler). It sets `revoked_at` without conditioning on the previous state
    (idempotent). It does not commit.
    """
    conditions = [
        PersonalAccessToken.id == token_id,
        PersonalAccessToken.workspace_id == workspace_id,
    ]
    if user_id is not None:
        conditions.append(PersonalAccessToken.user_id == user_id)
    res = await db.execute(
        update(PersonalAccessToken)
        .where(*conditions)
        .values(revoked_at=datetime.now(UTC))
        .returning(PersonalAccessToken.id)
    )
    return res.scalar_one_or_none() is not None


async def delete_pat_by_id(
    db: AsyncSession,
    *,
    token_id: str,
    workspace_id: str,
    user_id: str | None = None,
) -> bool:
    """Physically deletes a dead PAT, scoped to the workspace. True if it deleted.

    It only removes PATs in a terminal, cleanable state:
      - revoked: `revoked_at IS NOT NULL`.
      - expired: not revoked and already past expiry
        (`expires_at IS NOT NULL AND expires_at <= now`).

    A live PAT (not revoked and not expired, or without expiration) does NOT
    match, so the DELETE affects 0 rows and returns False -> the handler
    responds 404 (defense in depth enforced in SQL). The scoping by
    `workspace_id` (and `user_id` for members) mirrors that of
    `revoke_pat_by_id`. It does not commit.
    """
    now = datetime.now(UTC)
    deletable = or_(
        PersonalAccessToken.revoked_at.is_not(None),
        and_(
            PersonalAccessToken.revoked_at.is_(None),
            PersonalAccessToken.expires_at.is_not(None),
            PersonalAccessToken.expires_at <= now,
        ),
    )
    conditions = [
        PersonalAccessToken.id == token_id,
        PersonalAccessToken.workspace_id == workspace_id,
        deletable,
    ]
    if user_id is not None:
        conditions.append(PersonalAccessToken.user_id == user_id)
    res = await db.execute(
        delete(PersonalAccessToken).where(*conditions).returning(PersonalAccessToken.id)
    )
    return res.scalar_one_or_none() is not None
