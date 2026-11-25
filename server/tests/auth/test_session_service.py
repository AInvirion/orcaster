from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.session import create_session, resolve_session, revoke_session

CFG = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", session_ttl_seconds=3600)


async def _make_user(db_session) -> str:
    row = await db_session.execute(
        text("INSERT INTO users (display_name) VALUES ('Ada') RETURNING id")
    )
    uid = str(row.scalar_one())
    await db_session.commit()
    return uid


@pytest.mark.asyncio
async def test_create_and_resolve_session(db_session):
    uid = await _make_user(db_session)
    token = await create_session(db_session, CFG, user_id=uid, user_agent="UA", ip="1.2.3.4")
    await db_session.commit()
    user = await resolve_session(db_session, token)
    assert user is not None
    assert user.id == uid


@pytest.mark.asyncio
async def test_resolve_rejects_unknown_token(db_session):
    assert await resolve_session(db_session, "not-a-real-token") is None


@pytest.mark.asyncio
async def test_resolve_rejects_revoked(db_session):
    uid = await _make_user(db_session)
    token = await create_session(db_session, CFG, user_id=uid, user_agent=None, ip=None)
    await db_session.commit()
    await revoke_session(db_session, token)
    await db_session.commit()
    assert await resolve_session(db_session, token) is None


@pytest.mark.asyncio
async def test_resolve_rejects_expired(db_session):
    uid = await _make_user(db_session)
    token = await create_session(db_session, CFG, user_id=uid, user_agent=None, ip=None)
    # Force expiry into the past.
    await db_session.execute(
        text("UPDATE sessions SET expires_at = :e"),
        {"e": datetime.now(UTC) - timedelta(seconds=1)},
    )
    await db_session.commit()
    assert await resolve_session(db_session, token) is None
