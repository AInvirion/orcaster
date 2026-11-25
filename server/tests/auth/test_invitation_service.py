from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.invitation import (
    InvitationState,
    consume_invitation,
    create_invitation,
    peek_invitation,
    revoke_invitation,
)

CFG = AuthConfig(rp_id="t", rp_name="T", origin="http://t", invitation_ttl_seconds=3600)


async def _make_user(db_session) -> str:
    row = await db_session.execute(
        text("INSERT INTO users (display_name) VALUES ('Inviter') RETURNING id")
    )
    uid = str(row.scalar_one())
    await db_session.commit()
    return uid


@pytest.mark.asyncio
async def test_create_then_peek_returns_payload(db_session):
    inviter = await _make_user(db_session)
    token = await create_invitation(
        db_session,
        CFG,
        payload={"workspace_id": "ws1"},
        label="x@y.cl",
        created_by_user_id=inviter,
    )
    await db_session.commit()
    inv = await peek_invitation(db_session, token)
    assert inv is not None
    assert inv.payload == {"workspace_id": "ws1"}


@pytest.mark.asyncio
async def test_peek_unknown_returns_none(db_session):
    assert await peek_invitation(db_session, "nope") is None


@pytest.mark.asyncio
async def test_consume_marks_accepted_and_is_single_use(db_session):
    acceptor = await _make_user(db_session)
    token = await create_invitation(
        db_session, CFG, payload={}, label=None, created_by_user_id=None
    )
    await db_session.commit()
    state1, inv = await consume_invitation(db_session, token, user_id=acceptor)
    await db_session.commit()
    assert state1 == InvitationState.OK
    assert inv.accepted_by_user_id == acceptor
    state2, _ = await consume_invitation(db_session, token, user_id=acceptor)
    assert state2 == InvitationState.ALREADY_ACCEPTED


@pytest.mark.asyncio
async def test_consume_rejects_revoked(db_session):
    acceptor = await _make_user(db_session)
    token = await create_invitation(
        db_session, CFG, payload={}, label=None, created_by_user_id=None
    )
    await db_session.commit()
    await revoke_invitation(db_session, token)
    await db_session.commit()
    state, _ = await consume_invitation(db_session, token, user_id=acceptor)
    assert state == InvitationState.REVOKED


@pytest.mark.asyncio
async def test_consume_rejects_expired(db_session):
    acceptor = await _make_user(db_session)
    token = await create_invitation(
        db_session, CFG, payload={}, label=None, created_by_user_id=None
    )
    await db_session.execute(
        text("UPDATE invitations SET expires_at = :e"),
        {"e": datetime.now(UTC) - timedelta(seconds=1)},
    )
    await db_session.commit()
    state, _ = await consume_invitation(db_session, token, user_id=acceptor)
    assert state == InvitationState.EXPIRED
