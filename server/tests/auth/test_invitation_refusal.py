"""Invitation to a disabled workspace and the ordering of `register/complete`.

- Accepting (join with a session, or registering a new user) an invitation
  whose workspace is disabled gives the same 410 as an expired/revoked/used
  one, without consuming it or creating a user or membership.
- `register/complete` inserts no row in `users` if the challenge is invalid
  or the invitation is not acceptable.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import event, text

from orcaster_server.auth.config import AuthConfig, hash_token
from orcaster_server.auth.models import Invitation
from orcaster_server.auth.service.invitation import create_invitation
from orcaster_server.workspaces import service
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from orcaster_server.workspaces.seam import is_invitation_refused
from tests.conftest import _mint_session_for_workspace
from tests.soft_webauthn_device import SoftWebauthnDevice
from tests.test_workspaces_seam import _attestation_to_json, _to_soft_create_options

CFG = AuthConfig(rp_id="test", rp_name="Test", origin="http://test")


def _sm():
    from orcaster_server import db as app_db

    return app_db.get_sessionmaker()


async def _scalar(sql: str, **params):
    async with _sm()() as s:
        return (await s.execute(text(sql), params)).scalar_one_or_none()


async def _workspace(*, disabled: bool = False) -> str:
    async with _sm()() as s:
        ws_id = await service.create_workspace(
            s, name="Acme", slug=f"ws-{uuid.uuid4().hex[:8]}", created_by_user_id=None
        )
        if disabled:
            await s.execute(
                text("UPDATE workspaces SET disabled_at = now() WHERE id = :w"), {"w": ws_id}
            )
        await s.commit()
        return ws_id


async def _disable(ws_id: str) -> None:
    async with _sm()() as s:
        await s.execute(
            text("UPDATE workspaces SET disabled_at = now() WHERE id = :w"), {"w": ws_id}
        )
        await s.commit()


async def _invite(payload: dict) -> str:
    async with _sm()() as s:
        token = await create_invitation(
            s, CFG, payload=payload, label="x@example.com", created_by_user_id=None
        )
        await s.commit()
        return token


async def _accepted_at(token: str):
    return await _scalar(
        "SELECT accepted_at FROM invitations WHERE token_hash = :h", h=hash_token(token)
    )


async def _count(table: str) -> int:
    return int(await _scalar(f"SELECT count(*) FROM {table}"))  # noqa: S608 - tabla fija


def _cookie(challenge_id: str, name: str = "Ada") -> str:
    import base64

    raw = json.dumps({"cid": challenge_id, "name": name}).encode()
    return base64.urlsafe_b64encode(raw).decode()


async def _begin(client, token: str):
    res = await client.post(f"/auth/accept/{token}/register/begin")
    assert res.status_code == 200, res.text
    return res


async def _attest(client, begin_res) -> tuple[str, str]:
    """Valid attestation for the `begin_res` challenge; returns (body, cookie)."""
    from orcaster_server.config import settings

    device = SoftWebauthnDevice()
    att = device.create(
        {"publicKey": _to_soft_create_options(begin_res.json())}, settings.webauthn_origin
    )
    return _attestation_to_json(att), client.cookies["webauthn_challenge"]


class _UserInsertSpy:
    """Count the INSERTs into `users` that reach the DB (even if later rolled back)."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, conn, cursor, statement, *args):  # noqa: ARG002
        if statement.lstrip().upper().startswith("INSERT INTO USERS"):
            self.count += 1


@pytest.fixture
def user_inserts(app_client):
    from orcaster_server import db as app_db

    spy = _UserInsertSpy()
    engine = app_db.get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", spy)
    yield spy
    event.remove(engine, "before_cursor_execute", spy)


# --- host hook -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_refuses_only_invitations_of_a_disabled_workspace(app_client):
    live = await _workspace()
    dead = await _workspace(disabled=True)
    async with _sm()() as s:

        def inv(payload):
            return Invitation(payload=payload)

        assert await is_invitation_refused(s, inv({"workspace_id": dead, "role": "member"})) is True
        # UUID aliases (braces, urn, no hyphens): the same workspace.
        for alias in ("{" + dead + "}", "urn:uuid:" + dead, dead.replace("-", ""), dead.upper()):
            assert await is_invitation_refused(s, inv({"workspace_id": alias})) is True
        assert (
            await is_invitation_refused(s, inv({"workspace_id": live, "role": "member"})) is False
        )
        # Payloads that do not describe a workspace are neither rejected here nor raise.
        for payload in ({}, None, [], "x", 3, {"workspace_id": 5}, {"workspace_id": "no-uuid"}):
            assert await is_invitation_refused(s, inv(payload)) is False


# --- join with a session --------------------------------------------------------


async def _post_join(client, token: str, sess: str):
    from orcaster_server.config import settings

    return await client.post(
        f"/auth/accept/{token}/join",
        headers={
            "HX-Request": "false",
            "Origin": settings.webauthn_origin,
            "Cookie": f"session={sess}",
        },
        follow_redirects=False,
    )


@pytest.mark.asyncio
async def test_join_of_a_disabled_workspace_is_410_and_consumes_nothing(app_client):
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    ws = await _workspace()
    token = await _invite({"workspace_id": ws, "role": "member"})
    await _disable(ws)
    members = await _count("workspace_members")

    res = await _post_join(app_client, token, sess)

    assert res.status_code == 410
    assert "no longer valid" in res.text
    assert await _accepted_at(token) is None
    assert await _count("workspace_members") == members


@pytest.mark.asyncio
async def test_join_of_an_enabled_workspace_still_works(app_client):
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    ws = await _workspace()
    token = await _invite({"workspace_id": ws, "role": "member"})

    res = await _post_join(app_client, token, sess)

    assert res.status_code == 303
    assert await _accepted_at(token) is not None


@pytest.mark.asyncio
async def test_join_with_a_non_canonical_id_of_a_disabled_workspace_is_410(app_client):
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    ws = await _workspace(disabled=True)
    token = await _invite({"workspace_id": "{" + ws + "}", "role": "member"})

    res = await _post_join(app_client, token, sess)

    assert res.status_code == 410
    assert await _accepted_at(token) is None


# --- registering a new user --------------------------------------------------------


@pytest.mark.asyncio
async def test_register_begin_of_a_disabled_workspace_is_410(app_client):
    ws = await _workspace(disabled=True)
    token = await _invite({"workspace_id": ws, "role": "member"})

    res = await app_client.post(f"/auth/accept/{token}/register/begin")

    assert res.status_code == 410
    assert await _count("webauthn_challenges") == 0


@pytest.mark.asyncio
async def test_register_complete_of_a_workspace_disabled_after_begin_is_410(
    app_client, user_inserts
):
    ws = await _workspace()
    token = await _invite({"workspace_id": ws, "role": "owner"})
    begin = await _begin(app_client, token)
    body, _ = await _attest(app_client, begin)
    await _disable(ws)

    res = await app_client.post(f"/auth/accept/{token}/register/complete", content=body)

    assert res.status_code == 410
    assert res.json() == {"ok": False, "reason": "refused"}
    assert await _count("users") == 0
    assert await _count("sessions") == 0
    assert await _count("workspace_members") == 0
    assert await _accepted_at(token) is None
    assert user_inserts.count == 0
    assert "session=" not in res.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_register_complete_of_an_enabled_workspace_still_creates_the_user(
    app_client, user_inserts
):
    ws = await _workspace()
    token = await _invite({"workspace_id": ws, "role": "owner"})
    begin = await _begin(app_client, token)
    body, _ = await _attest(app_client, begin)

    res = await app_client.post(f"/auth/accept/{token}/register/complete", content=body)

    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert user_inserts.count == 1
    assert await _count("users") == 1
    assert await _count("passkey_credentials") == 1
    assert await _accepted_at(token) is not None


# --- nothing is inserted before validation -----------------------------------------


@pytest.mark.asyncio
async def test_register_complete_with_a_forged_cookie_never_inserts_a_user(
    app_client, user_inserts
):
    ws = await _workspace()
    token = await _invite({"workspace_id": ws, "role": "member"})
    res = await app_client.post(
        f"/auth/accept/{token}/register/complete",
        content=b"{}",
        headers={"Cookie": f"webauthn_challenge={_cookie(str(uuid.uuid4()))}"},
    )
    assert res.status_code == 400
    assert res.json() == {"ok": False}
    assert user_inserts.count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["not_found", "revoked", "already_accepted", "expired"])
async def test_register_complete_of_an_unusable_invitation_is_410_without_inserting(
    app_client, user_inserts, state
):
    ws = await _workspace()
    token = await _invite({"workspace_id": ws, "role": "member"})
    begin = await _begin(app_client, token)
    body, _ = await _attest(app_client, begin)
    h = hash_token(token)
    sql = {
        "revoked": "UPDATE invitations SET revoked_at = now() WHERE token_hash = :h",
        "already_accepted": "UPDATE invitations SET accepted_at = now() WHERE token_hash = :h",
        "expired": (
            "UPDATE invitations SET expires_at = now() - interval '1 hour' WHERE token_hash = :h"
        ),
        "not_found": "DELETE FROM invitations WHERE token_hash = :h",
    }[state]
    async with _sm()() as s:
        await s.execute(text(sql), {"h": h})
        await s.commit()

    res = await app_client.post(f"/auth/accept/{token}/register/complete", content=body)

    assert res.status_code == 410
    assert res.json() == {"ok": False, "reason": state}
    assert user_inserts.count == 0
    assert await _count("users") == 0
