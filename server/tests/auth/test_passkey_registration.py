import json

import pytest
from sqlalchemy import text

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.passkey import (
    begin_registration,
    complete_registration,
)
from tests.soft_webauthn_device import SoftWebauthnDevice

CFG = AuthConfig(rp_id="localhost", rp_name="Test", origin="https://localhost")


async def _make_user(db_session) -> str:
    row = await db_session.execute(
        text("INSERT INTO users (display_name) VALUES ('Ada') RETURNING id")
    )
    uid = str(row.scalar_one())
    await db_session.commit()
    return uid


@pytest.mark.asyncio
async def test_full_registration_persists_credential(db_session):
    uid = await _make_user(db_session)
    options_json, challenge_id = await begin_registration(
        db_session, CFG, user_id=uid, user_name="Ada"
    )
    await db_session.commit()

    device = SoftWebauthnDevice()
    options = json.loads(options_json)
    attestation = device.create({"publicKey": _to_soft_create_options(options)}, CFG.origin)

    cred = await complete_registration(
        db_session,
        CFG,
        challenge_id=challenge_id,
        user_id=uid,
        response=_attestation_to_json(attestation),
        label="MacBook",
    )
    await db_session.commit()
    assert cred is not None

    count = (
        await db_session.execute(
            text("SELECT count(*) FROM passkey_credentials WHERE user_id = :u"),
            {"u": uid},
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_registration_options_require_discoverable_credential(db_session):
    """Login is usernameless, so the credential must be discoverable."""
    uid = await _make_user(db_session)
    options_json, _ = await begin_registration(db_session, CFG, user_id=uid, user_name="Ada")
    await db_session.commit()

    selection = json.loads(options_json)["authenticatorSelection"]
    assert selection["residentKey"] == "required"
    assert selection["requireResidentKey"] is True
    # user_verification is unchanged: it is the py_webauthn default.
    assert selection["userVerification"] == "preferred"


def _to_soft_create_options(options: dict) -> dict:
    """Adapt the py_webauthn JSON to the shape SoftWebauthnDevice consumes."""
    import base64

    def b64url_to_bytes(s: str) -> bytes:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad)

    opts = dict(options)
    opts["challenge"] = b64url_to_bytes(options["challenge"])
    opts["user"] = dict(options["user"])
    opts["user"]["id"] = b64url_to_bytes(options["user"]["id"])
    return opts


def _attestation_to_json(att: dict) -> str:
    """Serialize the SoftWebauthnDevice attestation to the JSON py_webauthn expects."""
    import base64

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
