"""Cookie sessions. The session id is the secret carried in the cookie;
only its hash is stored in the DB. resolve_session validates not-revoked and not-expired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.config import AuthConfig, generate_token, hash_token
from orcaster_server.auth.models import Session, User


async def create_session(
    db: AsyncSession,
    config: AuthConfig,
    *,
    user_id: str,
    user_agent: str | None,
    ip: str | None,
) -> str:
    """Creates a session and returns the plain token (to set in the cookie)."""
    token = generate_token()
    now = datetime.now(UTC)
    await db.execute(
        insert(Session).values(
            id=hash_token(token),
            user_id=user_id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=config.session_ttl_seconds),
            user_agent=user_agent,
            ip=ip,
        )
    )
    return token


async def resolve_session(db: AsyncSession, token: str) -> User | None:
    """Returns the User of a live session (not revoked, not expired), or None."""
    if not token:
        return None
    sid = hash_token(token)
    now = datetime.now(UTC)
    row = await db.execute(
        select(User)
        .join(Session, Session.user_id == User.id)
        .where(
            Session.id == sid,
            Session.revoked_at.is_(None),
            Session.expires_at > now,
            User.disabled_at.is_(None),
        )
    )
    user = row.scalar_one_or_none()
    if user is not None:
        await db.execute(update(Session).where(Session.id == sid).values(last_seen_at=now))
    return user


async def revoke_session(db: AsyncSession, token: str) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == hash_token(token), Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
