"""Regression: the accept-invite flow must not create orphan users.

`begin` inserts no rows in `users`: it only persists the challenge and leaves
user creation to `complete` (single commit on full success). Verified over
HTTP (app_client) plus direct access to the same DB the app uses.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.invitation import create_invitation

CFG = AuthConfig(rp_id="localhost", rp_name="Test", origin="https://localhost")


@pytest.mark.asyncio
async def test_register_begin_does_not_create_user(app_client, migrated_db_url):
    """begin with a valid invitation -> 200 + cookie, but 0 users and 0 sessions.

    The invitation is created against the SAME DB the app uses
    (`migrated_db_url`). app_client already did its TRUNCATE in the fixture
    setup, so the insert happens afterwards, in the test body, on a fresh engine.
    """
    engine = create_async_engine(migrated_db_url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            token = await create_invitation(s, CFG, payload={}, label="QA", created_by_user_id=None)
            await s.commit()

        res = await app_client.post(f"/auth/accept/{token}/register/begin?display_name=Test")
        assert res.status_code == 200
        assert "challenge" in res.json()
        assert "webauthn_challenge" in res.headers.get("set-cookie", "")

        async with maker() as s:
            users = (await s.execute(text("SELECT count(*) FROM users"))).scalar_one()
            sessions = (await s.execute(text("SELECT count(*) FROM sessions"))).scalar_one()
            challenges = (
                await s.execute(text("SELECT count(*) FROM webauthn_challenges"))
            ).scalar_one()
        # begin creates no user (nor session); only the challenge persists.
        assert users == 0
        assert sessions == 0
        assert challenges == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_register_begin_invalid_token_returns_410(app_client):
    res = await app_client.post("/auth/accept/does-not-exist/register/begin?display_name=Test")
    assert res.status_code == 410
