"""Tests for the bootstrap script (`orcaster_server/cli/bootstrap.py`).

They exercise the importable `bootstrap()` function directly against `db_session`
(adopting the Default workspace and creating one with `--name`), plus an end-to-end
test that runs the real passkey ceremony on the issued token and verifies that an
`owner` membership is created.
"""

import base64
import json

import pytest
from sqlalchemy import text

from orcaster_server.auth.config import AuthConfig
from orcaster_server.cli.bootstrap import bootstrap, slugify
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.soft_webauthn_device import SoftWebauthnDevice


def _cfg(rp_id: str = "localhost", origin: str = "http://localhost:8000") -> AuthConfig:
    return AuthConfig(rp_id=rp_id, rp_name="T", origin=origin)


async def _invitation_payload(db_session, token_hash_of):
    from orcaster_server.auth.config import hash_token

    row = await db_session.execute(
        text("SELECT payload FROM invitations WHERE token_hash = :h"),
        {"h": hash_token(token_hash_of)},
    )
    return row.scalar_one()


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme", "acme"),
        ("Acme Corp", "acme-corp"),
        ("  Acme   Corp!! ", "acme-corp"),
        ("Hello -- World", "hello-world"),
        ("Café Münchën", "caf-m-nch-n"),
    ],
)
def test_slugify(name, expected):
    assert slugify(name) == expected


# ---------------------------------------------------------------------------
# Test 1 — without --name it adopts the Default workspace.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_adopts_default(db_session):
    ws_id, ws_name, token = await bootstrap(db_session, _cfg(), name=None)
    await db_session.commit()

    assert ws_id == DEFAULT_WORKSPACE_ID
    assert ws_name == "Default"
    payload = await _invitation_payload(db_session, token)
    assert payload == {"workspace_id": DEFAULT_WORKSPACE_ID, "role": "owner"}


# ---------------------------------------------------------------------------
# Test 2 — with --name it creates a new workspace with a derived slug.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_creates_named_workspace(db_session):
    ws_id, ws_name, token = await bootstrap(db_session, _cfg(), name="Acme Corp")
    await db_session.commit()

    assert ws_id != DEFAULT_WORKSPACE_ID
    assert ws_name == "Acme Corp"

    row = (
        await db_session.execute(
            text("SELECT name, slug FROM workspaces WHERE id = :id"),
            {"id": ws_id},
        )
    ).one()
    assert row[0] == "Acme Corp"
    assert row[1] == "acme-corp"

    payload = await _invitation_payload(db_session, token)
    assert payload == {"workspace_id": ws_id, "role": "owner"}


# ---------------------------------------------------------------------------
# Test 3 — a duplicate slug gets a unique suffix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_unique_slug_on_collision(db_session):
    ws1, _, _ = await bootstrap(db_session, _cfg(), name="Acme")
    ws2, _, _ = await bootstrap(db_session, _cfg(), name="Acme")
    await db_session.commit()

    slugs = (
        (
            await db_session.execute(
                text("SELECT slug FROM workspaces WHERE id IN (:a, :b)"),
                {"a": ws1, "b": ws2},
            )
        )
        .scalars()
        .all()
    )
    assert "acme" in slugs
    assert len(set(slugs)) == 2  # ambos distintos


# ---------------------------------------------------------------------------
# Test 4 — adopting Default when it does not exist → clear error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_default_missing_errors(db_session):
    await db_session.execute(
        text("DELETE FROM workspaces WHERE id = :id"), {"id": DEFAULT_WORKSPACE_ID}
    )
    await db_session.commit()

    with pytest.raises(SystemExit, match="Default"):
        await bootstrap(db_session, _cfg(), name=None)


# ---------------------------------------------------------------------------
# Test 5 — e2e: token issued by bootstrap → passkey ceremony → owner.
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
async def test_bootstrap_token_creates_owner_membership(app_client, migrated_db_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from orcaster_server.config import settings

    rp_id = settings.webauthn_rp_id
    origin = settings.webauthn_origin

    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            ws_id, ws_name, token = await bootstrap(
                s, _cfg(rp_id=rp_id, origin=origin), name="Bootstrapped"
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
