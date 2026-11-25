"""Worker enrollment tokens, multi-use. The token travels out of band (it is
handed to the operator); only the hash is persisted in the DB. Each token has
`max_uses`/`use_count`; it is considered exhausted when `use_count >= max_uses`
(`max_uses=1` is the single-use case, and it is the default of the mint).
`consume_enrollment_token` uses a conditional UPDATE to be atomic against
races; it mirrors `invitations` (see `orcaster_server/auth/service/invitation.py`).
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.config import generate_token, hash_token
from orcaster_server.workspaces.models import WorkerEnrollmentToken, WorkerEnrollmentTokenUse

DEFAULT_ENROLLMENT_TTL_SECONDS = 7 * 86400


class EnrollmentState(enum.Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    REVOKED = "revoked"
    EXHAUSTED = "exhausted"


async def mint_enrollment_token_returning_id(
    db: AsyncSession,
    *,
    workspace_id: str,
    created_by_user_id: str | None = None,
    label: str | None = None,
    ttl_seconds: int = DEFAULT_ENROLLMENT_TTL_SECONDS,
    max_uses: int = 1,
) -> tuple[str, str]:
    """Like `mint_enrollment_token` but returns `(plaintext_token, token_id)`.

    The id is needed to audit the mint (`record_admin_action` requires
    `target_id`); the `RETURNING` avoids a second round trip. The dashboard
    handler uses it; the remaining callers use `mint_enrollment_token`.
    """
    token = generate_token()
    res = await db.execute(
        insert(WorkerEnrollmentToken)
        .values(
            token_hash=hash_token(token),
            workspace_id=workspace_id,
            created_by_user_id=created_by_user_id,
            label=label,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            max_uses=max_uses,
        )
        .returning(WorkerEnrollmentToken.id)
    )
    return token, str(res.scalar_one())


async def mint_enrollment_token(
    db: AsyncSession,
    *,
    workspace_id: str,
    created_by_user_id: str | None = None,
    label: str | None = None,
    ttl_seconds: int = DEFAULT_ENROLLMENT_TTL_SECONDS,
    max_uses: int = 1,
) -> str:
    """Creates an enrollment token and returns the plaintext token (for the operator)."""
    token, _ = await mint_enrollment_token_returning_id(
        db,
        workspace_id=workspace_id,
        created_by_user_id=created_by_user_id,
        label=label,
        ttl_seconds=ttl_seconds,
        max_uses=max_uses,
    )
    return token


async def peek_enrollment_token(db: AsyncSession, token: str) -> WorkerEnrollmentToken | None:
    """Reads the token without consuming it."""
    if not token:
        return None
    row = await db.execute(
        select(WorkerEnrollmentToken).where(WorkerEnrollmentToken.token_hash == hash_token(token))
    )
    return row.scalar_one_or_none()


async def consume_enrollment_token(
    db: AsyncSession, token: str, *, worker_id: str | None
) -> tuple[EnrollmentState, WorkerEnrollmentToken | None]:
    """Consumes one use of the token atomically. Returns (state, token_row).

    Multi-use: increments `use_count` up to `max_uses`. The conditional UPDATE with
    `RETURNING` serializes concurrent consumers (row lock), so with `max_uses=N`
    exactly the first N win. Each successful consumption leaves a row in
    `worker_enrollment_token_uses` (forensic history).
    """
    tok = await peek_enrollment_token(db, token)
    if tok is None:
        return EnrollmentState.NOT_FOUND, None
    if tok.revoked_at is not None:
        return EnrollmentState.REVOKED, tok
    now = datetime.now(UTC)
    if tok.expires_at <= now:
        return EnrollmentState.EXPIRED, tok
    if tok.use_count >= tok.max_uses:
        return EnrollmentState.EXHAUSTED, tok
    # Conditional UPDATE: it only wins if the token is still not exhausted/revoked/expired.
    res = await db.execute(
        update(WorkerEnrollmentToken)
        .where(
            WorkerEnrollmentToken.id == tok.id,
            WorkerEnrollmentToken.revoked_at.is_(None),
            WorkerEnrollmentToken.expires_at > now,
            WorkerEnrollmentToken.use_count < WorkerEnrollmentToken.max_uses,
        )
        .values(
            use_count=WorkerEnrollmentToken.use_count + 1,
            used_at=now,
            used_by_worker_id=worker_id,
        )
        .returning(WorkerEnrollmentToken.id)
    )
    if res.scalar_one_or_none() is None:
        return EnrollmentState.EXHAUSTED, tok
    db.add(WorkerEnrollmentTokenUse(token_id=tok.id, worker_id=worker_id, used_at=now))
    await db.flush()
    await db.refresh(tok)
    return EnrollmentState.OK, tok


async def list_enrollment_tokens(
    db: AsyncSession,
    workspace_id: str,
    *,
    created_by_user_id: str | None = None,
    active_only: bool = False,
) -> list[WorkerEnrollmentToken]:
    """Lists the enrollment tokens of a workspace (metadata, not the secret).

    The plaintext token only exists at mint time; here we return the rows with
    their metadata (label, dates, use, revocation) for the UI listing. Ordered by
    `expires_at` descending as a proxy for recency (tokens have no `created_at` of
    their own; the TTL sets `expires_at` at creation).

    If `created_by_user_id` is passed, it filters the tokens to the given creator
    (member path: they only see their own). Without a filter it returns all of the
    workspace's tokens (owner path).

    If `active_only` is True, it filters in SQL to active (live) tokens only:
    never revoked, not yet expired and with remaining uses (`revoked_at IS NULL
    AND expires_at > now AND use_count < max_uses`). A partially consumed
    multi-use token (e.g. 1 of 3) is still active. The default False returns
    everything, including the terminal exhausted/revoked/expired tokens.
    """
    q = (
        select(WorkerEnrollmentToken)
        .where(WorkerEnrollmentToken.workspace_id == workspace_id)
        .order_by(WorkerEnrollmentToken.expires_at.desc())
    )
    if created_by_user_id is not None:
        q = q.where(WorkerEnrollmentToken.created_by_user_id == created_by_user_id)
    if active_only:
        q = q.where(
            WorkerEnrollmentToken.revoked_at.is_(None),
            WorkerEnrollmentToken.expires_at > datetime.now(UTC),
            WorkerEnrollmentToken.use_count < WorkerEnrollmentToken.max_uses,
        )
    res = await db.execute(q)
    return list(res.scalars().all())


async def revoke_enrollment_token_by_id(
    db: AsyncSession,
    *,
    token_id: str,
    workspace_id: str,
    created_by_user_id: str | None = None,
) -> bool:
    """Revokes a token by its id, scoped to the workspace. Returns True if it existed.

    The UPDATE filters by `(id, workspace_id)`: a token of another workspace never
    matches, so it returns False and the caller responds 404 (cross-tenant
    isolation). If the token belongs to the workspace, it sets `revoked_at` and
    returns True. The mark is applied without conditioning on the previous state:
    it covers already revoked tokens (idempotent on the mark) and also partially
    used tokens. The latter DOES have a security effect in the multi-use model:
    `consume_enrollment_token` and the pre-check of `register_worker` reject any
    token with `revoked_at IS NOT NULL`, so revoking a token with remaining uses
    (e.g. 1 of 3 consumed) genuinely cuts off future uses; it is the "revocable
    secret" property. It does not commit.

    If `created_by_user_id` is passed, the UPDATE also includes the filter
    `AND created_by_user_id = :uid` so that a member cannot revoke other people's
    tokens (returns False -> 404 in the handler). Without this parameter it works
    as the owner path: it can revoke any token of the workspace.
    """
    conditions = [
        WorkerEnrollmentToken.id == token_id,
        WorkerEnrollmentToken.workspace_id == workspace_id,
    ]
    if created_by_user_id is not None:
        conditions.append(WorkerEnrollmentToken.created_by_user_id == created_by_user_id)
    res = await db.execute(
        update(WorkerEnrollmentToken)
        .where(*conditions)
        .values(revoked_at=datetime.now(UTC))
        .returning(WorkerEnrollmentToken.id)
    )
    return res.scalar_one_or_none() is not None


async def delete_enrollment_token_by_id(
    db: AsyncSession,
    *,
    token_id: str,
    workspace_id: str,
    created_by_user_id: str | None = None,
) -> bool:
    """Physically deletes a dead token, scoped to the workspace. True if it deleted.

    It only removes tokens in a terminal, cleanable state, mirroring the UI's
    `_token_status`:
      - revoked: `revoked_at IS NOT NULL`.
      - expired: never revoked, with remaining uses and already past expiry
        (`expires_at <= now`).
      - exhausted: `use_count >= max_uses` (all permitted uses were consumed).
        It is deletable because the forensic trail of each consumption lives in
        `admin_actions` (`action='consume_enrollment_token'`, see
        `register_worker`) and in `worker_enrollment_token_uses`, so deleting the
        token row does not lose traceability.

    `active` tokens (live: revoked_at NULL, not expired, use_count < max_uses,
    including a partially consumed multi-use token) do NOT match the `deletable`
    condition, so the DELETE affects 0 rows and returns False -> the handler
    responds 404. This enforces the policy in SQL (defense in depth): even if
    someone forges the id of an active token, it is not deleted. The scoping by
    `workspace_id` (and `created_by_user_id` for members) mirrors that of
    `revoke_*`. It does not commit.
    """
    now = datetime.now(UTC)
    deletable = or_(
        WorkerEnrollmentToken.revoked_at.is_not(None),
        WorkerEnrollmentToken.use_count >= WorkerEnrollmentToken.max_uses,
        and_(
            WorkerEnrollmentToken.revoked_at.is_(None),
            WorkerEnrollmentToken.use_count < WorkerEnrollmentToken.max_uses,
            WorkerEnrollmentToken.expires_at <= now,
        ),
    )
    conditions = [
        WorkerEnrollmentToken.id == token_id,
        WorkerEnrollmentToken.workspace_id == workspace_id,
        deletable,
    ]
    if created_by_user_id is not None:
        conditions.append(WorkerEnrollmentToken.created_by_user_id == created_by_user_id)
    res = await db.execute(
        delete(WorkerEnrollmentToken).where(*conditions).returning(WorkerEnrollmentToken.id)
    )
    return res.scalar_one_or_none() is not None


async def list_token_uses(
    db: AsyncSession,
    *,
    token_id: str,
    workspace_id: str,
    created_by_user_id: str | None = None,
) -> list[WorkerEnrollmentTokenUse] | None:
    """Use history of a token, scoped to the workspace (and to the creator for
    members). Returns None if the token does not exist or is not accessible in that
    scope (the handler responds 404); [] if it exists but has had no uses yet.
    """
    conditions = [
        WorkerEnrollmentToken.id == token_id,
        WorkerEnrollmentToken.workspace_id == workspace_id,
    ]
    if created_by_user_id is not None:
        conditions.append(WorkerEnrollmentToken.created_by_user_id == created_by_user_id)
    tok_id = (
        await db.execute(select(WorkerEnrollmentToken.id).where(*conditions))
    ).scalar_one_or_none()
    if tok_id is None:
        return None
    res = await db.execute(
        select(WorkerEnrollmentTokenUse)
        .where(WorkerEnrollmentTokenUse.token_id == token_id)
        .order_by(WorkerEnrollmentTokenUse.used_at.desc())
    )
    return list(res.scalars().all())
