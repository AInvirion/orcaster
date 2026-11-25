"""Single-use invitations. The token travels outside (link); only its hash is
in the DB. consume_invitation uses a conditional UPDATE to be atomic against races.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.config import AuthConfig, generate_token, hash_token
from orcaster_server.auth.models import Invitation


class InvitationState(enum.Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ALREADY_ACCEPTED = "already_accepted"
    # Decided by the host (`AuthConfig.is_invitation_refused`), not by the row.
    REFUSED = "refused"


async def create_invitation(
    db: AsyncSession,
    config: AuthConfig,
    *,
    payload: dict,
    label: str | None,
    created_by_user_id: str | None,
    ttl_seconds: int | None = None,
) -> str:
    """Creates an invitation and returns the plain token (for the link)."""
    token = generate_token()
    ttl = ttl_seconds if ttl_seconds is not None else config.invitation_ttl_seconds
    await db.execute(
        insert(Invitation).values(
            token_hash=hash_token(token),
            created_by_user_id=created_by_user_id,
            payload=payload,
            label=label,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )
    )
    return token


async def peek_invitation(db: AsyncSession, token: str) -> Invitation | None:
    """Reads the invitation without consuming it (to show the accept page)."""
    if not token:
        return None
    row = await db.execute(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    return row.scalar_one_or_none()


def invitation_state(inv: Invitation | None, now: datetime | None = None) -> InvitationState:
    """State of the row: OK only if it exists and is not revoked, used or expired."""
    if inv is None:
        return InvitationState.NOT_FOUND
    if inv.revoked_at is not None:
        return InvitationState.REVOKED
    if inv.accepted_at is not None:
        return InvitationState.ALREADY_ACCEPTED
    if inv.expires_at <= (now or datetime.now(UTC)):
        return InvitationState.EXPIRED
    return InvitationState.OK


async def consume_invitation(
    db: AsyncSession, token: str, *, user_id: str
) -> tuple[InvitationState, Invitation | None]:
    """Marks the invitation as accepted atomically. Returns (state, inv)."""
    inv = await peek_invitation(db, token)
    if inv is None:
        return InvitationState.NOT_FOUND, None
    if inv.revoked_at is not None:
        return InvitationState.REVOKED, inv
    if inv.accepted_at is not None:
        return InvitationState.ALREADY_ACCEPTED, inv
    now = datetime.now(UTC)
    if inv.expires_at <= now:
        return InvitationState.EXPIRED, inv
    # Conditional UPDATE: it only wins if still not accepted/revoked/expired.
    res = await db.execute(
        update(Invitation)
        .where(
            Invitation.id == inv.id,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > now,
        )
        .values(accepted_at=now, accepted_by_user_id=user_id)
        .returning(Invitation.id)
    )
    if res.scalar_one_or_none() is None:
        return InvitationState.ALREADY_ACCEPTED, inv
    await db.refresh(inv)
    return InvitationState.OK, inv


async def revoke_invitation(db: AsyncSession, token: str) -> None:
    await db.execute(
        update(Invitation)
        .where(Invitation.token_hash == hash_token(token), Invitation.accepted_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
