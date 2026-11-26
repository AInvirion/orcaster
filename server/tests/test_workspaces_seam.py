"""Tests of the invitation → membership seam (`orcaster_server/workspaces/seam.py`).

Covers the handler directly against a real DB (valid/invalid payload) and an
end-to-end integration test that runs the full passkey ceremony against the
real app of `orcaster_server/main.py` (which already has the handler wired),
also exercising the call-site that passes the active `db`.
"""

import base64
import json

import pytest
from sqlalchemy import text

from orcaster_server.auth.config import INVITATION_WITHHELD, AuthConfig
from orcaster_server.auth.models import Invitation, User
from orcaster_server.auth.service.invitation import create_invitation
from orcaster_server.workspaces import service
from orcaster_server.workspaces.seam import describe_invitation, on_invitation_accepted
from tests.soft_webauthn_device import SoftWebauthnDevice


async def _make_user(db_session, display_name: str = "Ada") -> str:
    row = await db_session.execute(
        text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"),
        {"n": display_name},
    )
    uid = str(row.scalar_one())
    await db_session.commit()
    return uid


async def _make_workspace(db_session, slug: str = "acme") -> str:
    ws_id = await service.create_workspace(
        db_session, name="Acme", slug=slug, created_by_user_id=None
    )
    await db_session.commit()
    return ws_id


async def _membership_role(db_session, ws_id: str, user_id: str) -> str | None:
    return (
        await db_session.execute(
            text("SELECT role FROM workspace_members WHERE workspace_id = :w AND user_id = :u"),
            {"w": ws_id, "u": user_id},
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Test 1 — a valid payload creates a membership with the correct role.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "member"])
@pytest.mark.asyncio
async def test_valid_payload_creates_membership(db_session, role, caplog):
    import logging

    uid = await _make_user(db_session)
    ws_id = await _make_workspace(db_session)
    user = await db_session.get(User, uid)
    inv = Invitation(payload={"workspace_id": ws_id, "role": role})

    with caplog.at_level(logging.INFO, logger="orcaster_server.workspaces.seam"):
        await on_invitation_accepted(db_session, user, inv, None)
    await db_session.commit()

    assert await _membership_role(db_session, ws_id, uid) == role
    assert any(
        rec.levelno == logging.INFO and f"joined workspace {ws_id} as {role}" in rec.getMessage()
        for rec in caplog.records
    ), caplog.records


# ---------------------------------------------------------------------------
# Test 2 — invalid payload: no membership is created, no exception is raised.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_payloads_create_no_membership(db_session):
    uid = await _make_user(db_session)
    ws_id = await _make_workspace(db_session)
    user = await db_session.get(User, uid)

    bad_payloads = [
        {},  # empty (dev flow)
        {"workspace_id": ws_id, "role": "admin"},  # unknown role
        {"role": "owner"},  # missing workspace_id
        {"workspace_id": ws_id},  # missing role
        {"workspace_id": "", "role": "owner"},  # empty workspace_id
    ]
    for payload in bad_payloads:
        inv = Invitation(payload=payload)
        # Must not raise.
        await on_invitation_accepted(db_session, user, inv, None)
    await db_session.commit()

    count = (
        await db_session.execute(
            text("SELECT count(*) FROM workspace_members WHERE user_id = :u"),
            {"u": uid},
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_none_payload_is_tolerated(db_session):
    uid = await _make_user(db_session)
    user = await db_session.get(User, uid)
    inv = Invitation(payload=None)
    await on_invitation_accepted(db_session, user, inv, None)  # no raise
    await db_session.commit()


# ---------------------------------------------------------------------------
# Test 3 — end-to-end integration through the real passkey ceremony.
# Exercises the main.py wiring + the call-site that passes `db` to the callback.
# ---------------------------------------------------------------------------


def _to_soft_create_options(options: dict) -> dict:
    def b64url_to_bytes(s: str) -> bytes:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad)

    opts = dict(options)
    opts["challenge"] = b64url_to_bytes(options["challenge"])
    opts["user"] = dict(options["user"])
    opts["user"]["id"] = b64url_to_bytes(options["user"]["id"])
    return opts


def _attestation_to_json(att: dict) -> str:
    def b(x: bytes) -> str:
        return base64.urlsafe_b64encode(x).decode().rstrip("=")

    return json.dumps(
        {
            "id": b(att["rawId"]),
            "rawId": b(att["rawId"]),
            "type": att["type"],
            "response": {
                "clientDataJSON": b(att["response"]["clientDataJSON"]),
                "attestationObject": b(att["response"]["attestationObject"]),
            },
            "clientExtensionResults": {},
        }
    )


@pytest.mark.asyncio
async def test_register_ceremony_creates_membership(app_client, migrated_db_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from orcaster_server.config import settings

    # The app uses rp_id/origin from settings; align the device with them.
    rp_id = settings.webauthn_rp_id
    origin = settings.webauthn_origin

    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            ws_id = await service.create_workspace(
                s, name="Acme", slug="acme-int", created_by_user_id=None
            )
            token = await create_invitation(
                s,
                AuthConfig(rp_id=rp_id, rp_name="T", origin=origin),
                payload={"workspace_id": ws_id, "role": "owner"},
                label="new@example.com",
                created_by_user_id=None,
            )
            await s.commit()

        begin = await app_client.post(f"/auth/accept/{token}/register/begin")
        assert begin.status_code == 200, begin.text
        options = begin.json()

        device = SoftWebauthnDevice()
        attestation = device.create({"publicKey": _to_soft_create_options(options)}, origin)
        complete = await app_client.post(
            f"/auth/accept/{token}/register/complete",
            content=_attestation_to_json(attestation),
        )
        assert complete.status_code == 200, complete.text
        assert complete.json()["ok"] is True

        async with maker() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT wm.role FROM workspace_members wm "
                        "JOIN invitations i ON i.accepted_by_user_id = wm.user_id "
                        "WHERE wm.workspace_id = :w"
                    ),
                    {"w": ws_id},
                )
            ).scalar_one_or_none()
            assert row == "owner"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# The seam leaves the workspace the user joins as active, and logs
# (info) when the user was already a member.
# ---------------------------------------------------------------------------


async def _active_workspace(db_session, user_id: str) -> str | None:
    val = (
        await db_session.execute(
            text("SELECT workspace_id FROM user_active_workspace WHERE user_id = :u"),
            {"u": user_id},
        )
    ).scalar_one_or_none()
    return str(val) if val is not None else None


@pytest.mark.asyncio
async def test_valid_payload_sets_active_workspace(db_session):
    uid = await _make_user(db_session)
    ws_a = await _make_workspace(db_session, slug="acme-a")
    ws_b = await _make_workspace(db_session, slug="acme-b")
    await service.add_member(db_session, workspace_id=ws_a, user_id=uid, role="owner")
    await service.set_active_workspace(db_session, user_id=uid, workspace_id=ws_a)
    await db_session.commit()
    user = await db_session.get(User, uid)

    inv = Invitation(payload={"workspace_id": ws_b, "role": "member"})
    await on_invitation_accepted(db_session, user, inv, None)
    await db_session.commit()

    assert await _membership_role(db_session, ws_b, uid) == "member"
    assert await _active_workspace(db_session, uid) == ws_b


@pytest.mark.asyncio
async def test_existing_member_is_logged_and_role_unchanged(db_session, caplog):
    import logging

    uid = await _make_user(db_session)
    ws_id = await _make_workspace(db_session)
    await service.add_member(db_session, workspace_id=ws_id, user_id=uid, role="owner")
    await db_session.commit()
    user = await db_session.get(User, uid)

    inv = Invitation(payload={"workspace_id": ws_id, "role": "owner"})
    with caplog.at_level(logging.INFO, logger="orcaster_server.workspaces.seam"):
        await on_invitation_accepted(db_session, user, inv, None)
    await db_session.commit()

    assert await _membership_role(db_session, ws_id, uid) == "owner"
    assert await _active_workspace(db_session, uid) == ws_id
    assert any(
        rec.levelno == logging.INFO and "already a member" in rec.getMessage()
        for rec in caplog.records
    ), caplog.records


# ---------------------------------------------------------------------------
# `describe_invitation` asks to hide a disabled workspace
# (`INVITATION_WITHHELD`), whatever the form of the UUID in the payload.
# ---------------------------------------------------------------------------

# `uuid.UUID` parsea todas estas formas al mismo workspace.
_ID_FORMS = [
    pytest.param(lambda ws: ws, id="canonical"),
    pytest.param(lambda ws: ws.upper(), id="uppercase"),
    pytest.param(lambda ws: "{" + ws + "}", id="braced"),
    pytest.param(lambda ws: f"urn:uuid:{ws}", id="urn"),
    pytest.param(lambda ws: ws.replace("-", ""), id="unhyphenated"),
]


async def _disable_workspace(db_session, ws_id: str) -> None:
    await db_session.execute(
        text("UPDATE workspaces SET disabled_at = now() WHERE id = :i"), {"i": ws_id}
    )
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("make_id", _ID_FORMS)
async def test_describe_invitation_withholds_disabled_workspace(db_session, make_id):
    ws_id = await _make_workspace(db_session)
    await _disable_workspace(db_session, ws_id)
    inv = Invitation(payload={"workspace_id": make_id(ws_id), "role": "owner"})

    assert await describe_invitation(db_session, inv) == INVITATION_WITHHELD


@pytest.mark.asyncio
@pytest.mark.parametrize("make_id", _ID_FORMS)
async def test_describe_invitation_withholds_disabled_workspace_with_invalid_role(
    db_session, make_id
):
    ws_id = await _make_workspace(db_session)
    await _disable_workspace(db_session, ws_id)
    inv = Invitation(payload={"workspace_id": make_id(ws_id), "role": "admin"})

    assert await describe_invitation(db_session, inv) == INVITATION_WITHHELD


def test_invitation_withheld_is_distinct_from_none():
    # The page distinguishes "hide" (`""`) from "nothing to describe" (`None`,
    # falls back to the label): distinct values even though both are falsy.
    assert INVITATION_WITHHELD is not None
    assert INVITATION_WITHHELD == ""


@pytest.mark.parametrize("role", ["owner", "member"])
@pytest.mark.asyncio
async def test_describe_invitation_describes_enabled_workspace(db_session, role):
    ws_id = await _make_workspace(db_session)
    inv = Invitation(payload={"workspace_id": ws_id, "role": role})

    assert await describe_invitation(db_session, inv) == f"Workspace \u201cAcme\u201d as {role}"
    inv_upper = Invitation(payload={"workspace_id": ws_id.upper(), "role": role})
    assert (
        await describe_invitation(db_session, inv_upper) == f"Workspace \u201cAcme\u201d as {role}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_workspace_id",
    [
        pytest.param(lambda ws: "11111111-2222-3333-4444-555555555555", id="missing-workspace"),
        pytest.param(lambda ws: "{11111111-2222-3333-4444-555555555555}", id="missing-braced"),
        pytest.param(lambda ws: "not-a-uuid", id="not-a-uuid"),
        pytest.param(lambda ws: "", id="empty-string"),
        pytest.param(lambda ws: 123, id="int"),
        pytest.param(lambda ws: True, id="bool"),
        pytest.param(lambda ws: None, id="null"),
        pytest.param(lambda ws: [ws], id="list"),
        pytest.param(lambda ws: {"id": ws}, id="object"),
        pytest.param(lambda ws: "{" + ws + "}", id="enabled-braced"),
    ],
)
async def test_describe_invitation_returns_none_without_describable_workspace(
    db_session, make_workspace_id
):
    ws_id = await _make_workspace(db_session)
    inv = Invitation(payload={"workspace_id": make_workspace_id(ws_id), "role": "member"})

    # Never raises on the payload content; returns None (the page falls back to the label).
    assert await describe_invitation(db_session, inv) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, ["x"], "workspace", 42])
async def test_describe_invitation_returns_none_for_non_membership_payload(db_session, payload):
    inv = Invitation(payload=payload)

    assert await describe_invitation(db_session, inv) is None
