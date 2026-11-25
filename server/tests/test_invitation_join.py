"""Tests for the session join: an already authenticated user accepts an invitation without
creating a new account (`POST /auth/accept/{token}/join`).

Covers:
  - join of an existing user: new membership, same user, no new rows
    in `users`/`passkey_credentials`/`sessions`, active = invited workspace.
  - join of someone who is already a member: invitation consumed, role intact.
  - race: two concurrent joins on the same token → one 303 and one 410.
  - invalid / revoked / expired / already used token → 410.
  - without a session (or with a disabled user) → 303 to the invitation
    page, which stays unconsumed.
  - POST from another origin (`Origin` or `Sec-Fetch-Site`) → 403 without consuming.
  - the GET page offers "Join as <name>" only with a session.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.invitation import create_invitation, revoke_invitation
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import _mint_session_for_workspace

CFG = AuthConfig(rp_id="test", rp_name="Test", origin="http://test")


def _sm():
    from orcaster_server import db as app_db

    sm = app_db.get_sessionmaker()
    if sm is None:  # pragma: no cover - defensive
        raise RuntimeError("app engine not initialized; is the app_client fixture missing?")
    return sm


async def _scalar(sql: str, **params):
    async with _sm()() as s:
        return (await s.execute(text(sql), params)).scalar_one_or_none()


async def _user_id_of_session(token: str) -> str:
    from orcaster_server.auth.config import hash_token

    return str(await _scalar("SELECT user_id FROM sessions WHERE id = :h", h=hash_token(token)))


async def _create_workspace(name: str) -> str:
    from orcaster_server.workspaces.service import create_workspace

    async with _sm()() as s:
        ws_id = await create_workspace(s, name=name, slug=f"ws-{uuid.uuid4().hex[:8]}")
        await s.commit()
        return ws_id


async def _invite(workspace_id: str, role: str = "member") -> str:
    async with _sm()() as s:
        token = await create_invitation(
            s,
            CFG,
            payload={"workspace_id": workspace_id, "role": role},
            label="join@acme.cl",
            created_by_user_id=None,
        )
        await s.commit()
        return token


async def _count(table: str) -> int:
    return int(await _scalar(f"SELECT count(*) FROM {table}"))  # noqa: S608 - fixed test table


async def _role(workspace_id: str, user_id: str) -> str | None:
    return await _scalar(
        "SELECT role FROM workspace_members WHERE workspace_id = :w AND user_id = :u",
        w=workspace_id,
        u=user_id,
    )


async def _accepted_by(token: str) -> str | None:
    from orcaster_server.auth.config import hash_token

    val = await _scalar(
        "SELECT accepted_by_user_id FROM invitations WHERE token_hash = :h",
        h=hash_token(token),
    )
    return str(val) if val is not None else None


_SAME_ORIGIN = object()


async def _post_join(
    client: AsyncClient,
    token: str,
    session_token: str | None,
    *,
    origin: object = _SAME_ORIGIN,
    sec_fetch_site: str | None = None,
):
    """POST of the join form. By default it sends the site's own `Origin`,
    like a browser; `origin=None` omits it."""
    from orcaster_server.config import settings

    headers = {"HX-Request": "false"}
    if origin is _SAME_ORIGIN:
        headers["Origin"] = settings.webauthn_origin
    elif origin is not None:
        headers["Origin"] = origin
    elif sec_fetch_site is None:
        # No origin header at all: the test client must not add `Origin`.
        headers["X-Test-No-Origin"] = "1"
    if sec_fetch_site is not None:
        headers["Sec-Fetch-Site"] = sec_fetch_site
    if session_token is not None:
        headers["Cookie"] = f"session={session_token}"
    return await client.post(f"/auth/accept/{token}/join", headers=headers, follow_redirects=False)


# ---------------------------------------------------------------------------
# Join of an existing user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_as_existing_user_adds_membership_without_new_account(
    app_client: AsyncClient,
) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    user_id = await _user_id_of_session(sess)
    ws_b = await _create_workspace("WS_Join")
    token = await _invite(ws_b, role="member")

    users_before = await _count("users")
    passkeys_before = await _count("passkey_credentials")
    sessions_before = await _count("sessions")

    resp = await _post_join(app_client, token, sess)

    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/"
    # No new account, passkey or session.
    assert await _count("users") == users_before
    assert await _count("passkey_credentials") == passkeys_before
    assert await _count("sessions") == sessions_before
    assert "set-cookie" not in resp.headers
    # Same user: member of B and owner of Default intact.
    assert await _role(ws_b, user_id) == "member"
    assert await _role(DEFAULT_WORKSPACE_ID, user_id) == "owner"
    assert await _accepted_by(token) == user_id
    # Lands in the workspace they joined.
    active = await _scalar(
        "SELECT workspace_id FROM user_active_workspace WHERE user_id = :u", u=user_id
    )
    assert str(active) == ws_b

    page = await app_client.get(
        "/admin/", headers={"HX-Request": "false", "Cookie": f"session={sess}"}
    )
    assert page.status_code == 200
    assert "WS_Join" in page.text
    assert "Default" in page.text


@pytest.mark.asyncio
async def test_join_when_already_member_consumes_invitation_and_keeps_role(
    app_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    user_id = await _user_id_of_session(sess)
    token = await _invite(DEFAULT_WORKSPACE_ID, role="member")

    with caplog.at_level(logging.INFO, logger="orcaster_server.workspaces.seam"):
        resp = await _post_join(app_client, token, sess)

    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/"
    assert await _role(DEFAULT_WORKSPACE_ID, user_id) == "owner"
    assert await _accepted_by(token) == user_id
    assert any("already a member" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Race: two different users, same token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_joins_on_same_token_only_one_wins(app_client: AsyncClient) -> None:
    sess_a = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    sess_b = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    ws_b = await _create_workspace("WS_Race")
    token = await _invite(ws_b, role="member")

    r1, r2 = await asyncio.gather(
        _post_join(app_client, token, sess_a),
        _post_join(app_client, token, sess_b),
    )

    assert sorted([r1.status_code, r2.status_code]) == [303, 410]
    members = await _scalar(
        "SELECT count(*) FROM workspace_members WHERE workspace_id = :w", w=ws_b
    )
    assert members == 1
    winner = sess_a if r1.status_code == 303 else sess_b
    assert await _accepted_by(token) == await _user_id_of_session(winner)


# ---------------------------------------------------------------------------
# Invalid tokens → 410
# ---------------------------------------------------------------------------


async def _revoked_token(ws: str) -> str:
    token = await _invite(ws)
    async with _sm()() as s:
        await revoke_invitation(s, token)
        await s.commit()
    return token


async def _expired_token(ws: str) -> str:
    from orcaster_server.auth.config import hash_token

    token = await _invite(ws)
    async with _sm()() as s:
        await s.execute(
            text(
                "UPDATE invitations SET expires_at = now() - interval '1 minute' "
                "WHERE token_hash = :h"
            ),
            {"h": hash_token(token)},
        )
        await s.commit()
    return token


@pytest.mark.parametrize("kind", ["unknown", "revoked", "expired"])
@pytest.mark.asyncio
async def test_join_invalid_token_returns_410(app_client: AsyncClient, kind: str) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    user_id = await _user_id_of_session(sess)
    ws_b = await _create_workspace("WS_Invalid")
    if kind == "unknown":
        token = "does-not-exist"
    elif kind == "revoked":
        token = await _revoked_token(ws_b)
    else:
        token = await _expired_token(ws_b)

    resp = await _post_join(app_client, token, sess)

    assert resp.status_code == 410
    assert "no longer valid" in resp.text
    assert await _role(ws_b, user_id) is None


@pytest.mark.asyncio
async def test_join_already_used_token_returns_410(app_client: AsyncClient) -> None:
    sess_a = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    sess_b = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    ws_b = await _create_workspace("WS_Used")
    token = await _invite(ws_b)

    first = await _post_join(app_client, token, sess_a)
    assert first.status_code == 303
    second = await _post_join(app_client, token, sess_b)

    assert second.status_code == 410
    assert await _role(ws_b, await _user_id_of_session(sess_b)) is None


# ---------------------------------------------------------------------------
# Without a valid session → 303 to the invitation page, which remains available
# ---------------------------------------------------------------------------


async def _assert_unused(token: str, ws: str) -> None:
    assert await _accepted_by(token) is None
    members = await _scalar("SELECT count(*) FROM workspace_members WHERE workspace_id = :w", w=ws)
    assert members == 0


@pytest.mark.asyncio
async def test_join_without_session_redirects_to_invite_and_keeps_invitation(
    app_client: AsyncClient,
) -> None:
    ws_b = await _create_workspace("WS_Anon")
    token = await _invite(ws_b)

    resp = await _post_join(app_client, token, None)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/auth/accept/{token}"
    await _assert_unused(token, ws_b)


@pytest.mark.asyncio
async def test_join_with_disabled_user_redirects_and_keeps_invitation(
    app_client: AsyncClient,
) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    user_id = await _user_id_of_session(sess)
    async with _sm()() as s:
        await s.execute(text("UPDATE users SET disabled_at = now() WHERE id = :u"), {"u": user_id})
        await s.commit()
    ws_b = await _create_workspace("WS_Disabled")
    token = await _invite(ws_b)

    resp = await _post_join(app_client, token, sess)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/auth/accept/{token}"
    await _assert_unused(token, ws_b)


# ---------------------------------------------------------------------------
# Origin of the POST: only the site itself can join the user
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "sec_fetch_site"),
    [
        ("https://evil.example", None),
        ("null", None),
        ("https://evil.example", "same-origin"),
        (None, "same-site"),
        (None, "cross-site"),
        (None, None),
    ],
)
@pytest.mark.asyncio
async def test_join_from_foreign_origin_returns_403_and_keeps_invitation(
    app_client: AsyncClient, origin: str | None, sec_fetch_site: str | None
) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    ws_b = await _create_workspace("WS_Csrf")
    token = await _invite(ws_b)

    resp = await _post_join(app_client, token, sess, origin=origin, sec_fetch_site=sec_fetch_site)

    assert resp.status_code == 403
    await _assert_unused(token, ws_b)


@pytest.mark.parametrize("sec_fetch_site", ["same-origin", "none"])
@pytest.mark.asyncio
async def test_join_without_origin_accepts_same_origin_fetch_metadata(
    app_client: AsyncClient, sec_fetch_site: str
) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    user_id = await _user_id_of_session(sess)
    ws_b = await _create_workspace("WS_FetchMeta")
    token = await _invite(ws_b)

    resp = await _post_join(app_client, token, sess, origin=None, sec_fetch_site=sec_fetch_site)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/"
    assert await _role(ws_b, user_id) == "member"


# ---------------------------------------------------------------------------
# GET page /auth/accept/{token}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_page_logged_in_offers_join_as_current_user(
    app_client: AsyncClient,
) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    ws_b = await _create_workspace("WS_Page")
    token = await _invite(ws_b)

    resp = await app_client.get(f"/auth/accept/{token}", headers={"Cookie": f"session={sess}"})

    assert resp.status_code == 200
    body = resp.text
    assert "Join as Admin" in body
    assert f'action="/auth/accept/{token}/join"' in body
    assert 'method="post"' in body
    # The passkey route remains available as an alternative.
    assert "Or create a separate account with a new passkey" in body
    assert "register-btn" in body


@pytest.mark.asyncio
async def test_accept_page_anonymous_has_no_join_form(app_client: AsyncClient) -> None:
    ws_b = await _create_workspace("WS_PageAnon")
    token = await _invite(ws_b)

    resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    body = resp.text
    assert "Join as" not in body
    assert "/join" not in body
    assert "Create account with passkey" in body
    assert "Already have an account?" in body
    assert "then open this link again." in body


# ---------------------------------------------------------------------------
# The page names workspace and role; it does not use `workspace:<uuid>`
# ---------------------------------------------------------------------------


async def _invite_with_label(workspace_id: str, label: str, role: str = "member") -> str:
    async with _sm()() as s:
        token = await create_invitation(
            s,
            CFG,
            payload={"workspace_id": workspace_id, "role": role},
            label=label,
            created_by_user_id=None,
        )
        await s.commit()
        return token


@pytest.mark.asyncio
async def test_accept_page_names_workspace_and_role_for_legacy_label(
    app_client: AsyncClient,
) -> None:
    """Old invitation (`label=workspace:<uuid>`): name and role are shown, without the uuid,
    and the name field is not prefilled."""
    ws = await _create_workspace("Acme Corp")
    token = await _invite_with_label(ws, f"workspace:{ws}", role="owner")

    resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    body = resp.text
    assert "Acme Corp" in body
    assert "as owner" in body
    # The page carries in a data-* the pattern that rejects the placeholder
    # (`_PLACEHOLDER_NAME.pattern`), so a bare "workspace:" always appears.
    # The assertion is made on the full pattern
    # `workspace:<uuid>` across the whole page and on the visible/prefilled spots.
    assert not re.search(r"workspace:\s*[0-9a-f]{8}-[0-9a-f]{4}-", body, re.IGNORECASE)
    assert ws not in body
    target = re.search(r'<p id="invite-target">(.*?)</p>', body, re.DOTALL)
    assert target is not None
    assert "workspace:" not in target.group(1)
    assert "Acme Corp" in target.group(1)
    name_input = re.search(r'<input id="display-name"[^>]*>', body)
    assert name_input is not None
    assert "value=" not in name_input.group(0)


@pytest.mark.asyncio
async def test_accept_page_logged_in_says_which_workspace_join_refers_to(
    app_client: AsyncClient,
) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    ws = await _create_workspace("Second WS")
    token = await _invite_with_label(ws, f"workspace:{ws}")

    resp = await app_client.get(f"/auth/accept/{token}", headers={"Cookie": f"session={sess}"})

    assert "Join as Admin" in resp.text
    assert "Second WS" in resp.text
    assert "as member" in resp.text
    assert ws not in resp.text


@pytest.mark.asyncio
async def test_accept_page_reflects_workspace_rename(app_client: AsyncClient) -> None:
    from orcaster_server.workspaces.service import rename_workspace

    ws = await _create_workspace("Old Name")
    token = await _invite_with_label(ws, "Old Name")
    async with _sm()() as s:
        await rename_workspace(s, workspace_id=ws, name="New Name")
        await s.commit()

    resp = await app_client.get(f"/auth/accept/{token}")

    assert "New Name" in resp.text
    assert "Old Name" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "workspace:11111111-2222-3333-4444-555555555555",
        "  Workspace:11111111-2222-3333-4444-555555555555",
        "x" * 201,
    ],
)
async def test_register_begin_rejects_blank_or_placeholder_name(
    app_client: AsyncClient, name: str
) -> None:
    ws = await _create_workspace("WS_Name")
    token = await _invite(ws)

    resp = await app_client.post(
        f"/auth/accept/{token}/register/begin", params={"display_name": name}
    )

    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid_display_name"
    assert await _count("users") == 0


# A single definition of the placeholder for the browser and the server.
# Shared list: name -> is it a placeholder? Rule: only the full form
# `workspace:<uuid>` (8-4-4-4-12), case-insensitive.
_PLACEHOLDER_EXAMPLES = [
    ("workspace:deadbeef-ops", False),
    ("workspace:deadbeef-cafe-team", False),
    ("workspace:11111111-2222-3333-4444-555555555555", True),
    ("WORKSPACE: DEADBEEF-CAFE-BABE-0000-123456789ABC", True),
    ("  workspace:11111111-2222-3333-4444-555555555555  ", True),
    ("workspace:11111111-2222-3333-4444-555555555555x", False),
    ("workspace:11111111-2222-3333-4444-55555555555", False),
    ("Workspace: Ana", False),
    ("  Ana  ", False),
]


@pytest.mark.parametrize(("name", "expected"), _PLACEHOLDER_EXAMPLES)
def test_placeholder_name_rule_on_server(name: str, expected: bool) -> None:
    from orcaster_server.auth.router import _is_placeholder_name

    assert _is_placeholder_name(name) is expected


def _page_placeholder_pattern(body: str) -> str:
    """The pattern that accept.js compiles, read from the data-* of the served HTML."""
    import html

    m = re.search(r'data-placeholder-pattern="([^"]*)"', body)
    assert m is not None, "the page does not deliver the server pattern"
    return html.unescape(m.group(1))


@pytest.mark.asyncio
async def test_accept_page_uses_server_placeholder_pattern(app_client: AsyncClient) -> None:
    from orcaster_server.auth.router import _PLACEHOLDER_NAME

    ws = await _create_workspace("WS_Pattern")
    token = await _invite(ws)

    body = (await app_client.get(f"/auth/accept/{token}")).text

    pattern = _page_placeholder_pattern(body)
    assert pattern == _PLACEHOLDER_NAME.pattern
    # No fallback literal regex in the script: a single definition.
    assert "/^workspace" not in body
    browser = re.compile(pattern, re.IGNORECASE)
    for name, expected in _PLACEHOLDER_EXAMPLES:
        assert (browser.search(name.strip()) is not None) is expected, name


@pytest.mark.asyncio
async def test_accept_page_name_input_has_no_inert_required(app_client: AsyncClient) -> None:
    ws = await _create_workspace("WS_Required")
    token = await _invite(ws)

    body = (await app_client.get(f"/auth/accept/{token}")).text

    name_input = re.search(r'<input id="display-name"[^>]*>', body)
    assert name_input is not None
    assert not re.search(r"\srequired(\s|>|=)", name_input.group(0))
    assert 'aria-required="true"' in name_input.group(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "expected"), _PLACEHOLDER_EXAMPLES)
async def test_register_begin_follows_placeholder_rule(
    app_client: AsyncClient, name: str, expected: bool
) -> None:
    ws = await _create_workspace("WS_Rule")
    token = await _invite(ws)

    resp = await app_client.post(
        f"/auth/accept/{token}/register/begin", params={"display_name": name}
    )

    assert resp.status_code == (400 if expected else 200)


@pytest.mark.asyncio
async def test_register_begin_accepts_real_name(app_client: AsyncClient) -> None:
    ws = await _create_workspace("WS_Name2")
    token = await _invite(ws)

    resp = await app_client.post(
        f"/auth/accept/{token}/register/begin", params={"display_name": "  Ana  "}
    )

    assert resp.status_code == 200
    assert "challenge" in resp.json()


@pytest.mark.asyncio
async def test_accept_page_hides_placeholder_display_name_of_existing_user(
    app_client: AsyncClient,
) -> None:
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    uid = await _user_id_of_session(sess)
    legacy = "workspace:11111111-2222-3333-4444-555555555555"
    async with _sm()() as s:
        await s.execute(
            text("UPDATE users SET display_name = :n WHERE id = :i"), {"n": legacy, "i": uid}
        )
        await s.commit()
    ws = await _create_workspace("WS_Legacy")
    token = await _invite(ws)

    resp = await app_client.get(f"/auth/accept/{token}", headers={"Cookie": f"session={sess}"})

    assert "Join with your current account" in resp.text
    assert legacy not in resp.text


# ---------------------------------------------------------------------------
# Invalid payload or missing hook => the page keeps showing
# the saved label, with HTTP 200 (never 500)
# ---------------------------------------------------------------------------

_FALLBACK_LABEL = "Fallback Label"
_REAL_WS_NAME = "Real Workspace Name"

# Each case builds the payload from the id of a workspace that DOES exist, so
# that if the page described it, `_REAL_WS_NAME` would be seen.
_INVALID_PAYLOADS = [
    pytest.param(lambda ws: ["x"], id="list"),
    pytest.param(lambda ws: [{"workspace_id": ws, "role": "member"}], id="list-of-membership"),
    pytest.param(lambda ws: "workspace", id="string"),
    pytest.param(lambda ws: 42, id="number"),
    pytest.param(lambda ws: True, id="boolean"),
    pytest.param(lambda ws: {}, id="empty-object"),
    pytest.param(lambda ws: {"role": "member"}, id="missing-workspace-id"),
    pytest.param(lambda ws: {"workspace_id": ws}, id="missing-role"),
    pytest.param(lambda ws: {"workspace_id": ws, "role": "admin"}, id="unknown-role"),
    pytest.param(lambda ws: {"workspace_id": ws, "role": ["owner"]}, id="non-string-role"),
    pytest.param(lambda ws: {"workspace_id": 123, "role": "member"}, id="non-string-workspace-id"),
    pytest.param(lambda ws: {"workspace_id": "", "role": "member"}, id="empty-workspace-id"),
    pytest.param(lambda ws: {"workspace_id": "not-a-uuid", "role": "member"}, id="not-a-uuid"),
    pytest.param(
        lambda ws: {"workspace_id": f"urn:uuid:{ws}", "role": "member"}, id="urn-uuid-form"
    ),
    pytest.param(lambda ws: {"workspace_id": "{" + ws + "}", "role": "member"}, id="braced-form"),
    pytest.param(
        lambda ws: {"workspace_id": ws.replace("-", ""), "role": "member"}, id="unhyphenated-form"
    ),
    pytest.param(
        lambda ws: {"workspace_id": str(uuid.uuid4()), "role": "member"}, id="unknown-workspace"
    ),
]


async def _invite_with_raw_payload(payload: object, label: str) -> str:
    async with _sm()() as s:
        token = await create_invitation(
            s,
            CFG,
            payload=payload,  # type: ignore[arg-type] - JSONB accepts any JSON; that is the case
            label=label,
            created_by_user_id=None,
        )
        await s.commit()
        return token


def _invite_target_text(body: str) -> str | None:
    m = re.search(r'<p id="invite-target">(.*?)</p>', body, re.DOTALL)
    return m.group(1).strip() if m else None


@pytest.mark.asyncio
@pytest.mark.parametrize("make_payload", _INVALID_PAYLOADS)
async def test_accept_page_falls_back_to_label_for_invalid_payload(
    app_client: AsyncClient, make_payload
) -> None:
    ws = await _create_workspace(_REAL_WS_NAME)
    token = await _invite_with_raw_payload(make_payload(ws), _FALLBACK_LABEL)

    resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    assert _invite_target_text(resp.text) == f"Invitation for: {_FALLBACK_LABEL}"
    assert _REAL_WS_NAME not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [["x"], "workspace", 42], ids=["list", "string", "number"])
async def test_accept_page_warns_about_non_object_payload_without_leaking_token(
    app_client: AsyncClient, caplog: pytest.LogCaptureFixture, payload: object
) -> None:
    token = await _invite_with_raw_payload(payload, _FALLBACK_LABEL)
    from orcaster_server.auth.config import hash_token

    inv_id = await _scalar("SELECT id FROM invitations WHERE token_hash = :h", h=hash_token(token))

    with caplog.at_level(logging.WARNING, logger="orcaster_server.workspaces.seam"):
        resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    messages = [
        r.getMessage() for r in caplog.records if r.name == "orcaster_server.workspaces.seam"
    ]
    assert any(str(inv_id) in m for m in messages), messages
    assert all(token not in m for m in messages)


@pytest.mark.asyncio
async def test_accept_page_describes_uppercase_canonical_workspace_id(
    app_client: AsyncClient,
) -> None:
    ws = await _create_workspace(_REAL_WS_NAME)
    token = await _invite_with_raw_payload({"workspace_id": ws.upper(), "role": "owner"}, "x")

    resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    assert _invite_target_text(resp.text) == f"Invitation for: Workspace “{_REAL_WS_NAME}” as owner"


@pytest.mark.asyncio
async def test_accept_page_without_describe_hook_renders_current_label(
    app_client: AsyncClient,
) -> None:
    """`AuthConfig` without `describe_invitation` (another `auth` host): the page
    behaves as before naming the workspace and shows the saved label."""
    from fastapi import FastAPI

    from orcaster_server.auth.factory import make_auth
    from orcaster_server.auth.models import AuthBase

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test")
    assert cfg.describe_invitation is None
    host = FastAPI()
    host.include_router(make_auth(AuthBase, cfg).router)
    ws = await _create_workspace(_REAL_WS_NAME)
    token = await _invite_with_label(ws, "Plain Label")

    async with AsyncClient(transport=ASGITransport(app=host), base_url="http://test") as client:
        resp = await client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    assert _invite_target_text(resp.text) == "Invitation for: Plain Label"
    assert _REAL_WS_NAME not in resp.text


# ---------------------------------------------------------------------------
# Minimal disclosure; expired or disabled workspace name neither
# workspace nor role
# ---------------------------------------------------------------------------

_NEUTRAL_LINE = "This invitation is no longer valid. Ask for a new link."


async def _expire(token: str) -> None:
    from orcaster_server.auth.config import hash_token

    async with _sm()() as s:
        await s.execute(
            text(
                "UPDATE invitations SET expires_at = now() - interval '1 minute' "
                "WHERE token_hash = :h"
            ),
            {"h": hash_token(token)},
        )
        await s.commit()


async def _disable_workspace(workspace_id: str) -> None:
    async with _sm()() as s:
        await s.execute(
            text("UPDATE workspaces SET disabled_at = now() WHERE id = :i"), {"i": workspace_id}
        )
        await s.commit()


def _assert_nothing_disclosed(body: str, *, ws_name: str, label: str, role: str) -> None:
    assert ws_name not in body
    assert label not in body
    assert f"as {role}" not in body
    assert "Invitation for:" not in body
    assert "will be added to" not in body
    assert _NEUTRAL_LINE in body


@pytest.mark.asyncio
async def test_accept_page_valid_shows_workspace_and_role(app_client: AsyncClient) -> None:
    ws = await _create_workspace("Visible Corp")
    token = await _invite(ws, role="owner")

    resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    assert _invite_target_text(resp.text) == "Invitation for: Workspace “Visible Corp” as owner"
    assert _NEUTRAL_LINE not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("logged_in", [False, True], ids=["anonymous", "session"])
async def test_accept_page_expired_hides_workspace_and_role(
    app_client: AsyncClient, logged_in: bool
) -> None:
    ws = await _create_workspace("Hidden Expired Corp")
    token = await _invite_with_label(ws, "Hidden Expired Corp", role="owner")
    await _expire(token)
    headers = {}
    if logged_in:
        sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
        headers = {"Cookie": f"session={sess}"}

    resp = await app_client.get(f"/auth/accept/{token}", headers=headers)

    assert resp.status_code == 200
    _assert_nothing_disclosed(
        resp.text, ws_name="Hidden Expired Corp", label="Hidden Expired Corp", role="owner"
    )


@pytest.mark.asyncio
async def test_accept_page_expired_submit_still_returns_410(app_client: AsyncClient) -> None:
    ws = await _create_workspace("Expired Submit Corp")
    token = await _invite(ws)
    await _expire(token)
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)

    resp = await _post_join(app_client, token, sess)

    assert resp.status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize("logged_in", [False, True], ids=["anonymous", "session"])
async def test_accept_page_disabled_workspace_hides_workspace_and_role(
    app_client: AsyncClient, logged_in: bool
) -> None:
    ws = await _create_workspace("Hidden Disabled Corp")
    token = await _invite_with_label(ws, "Hidden Disabled Corp", role="member")
    async with _sm()() as s:
        await s.execute(text("UPDATE workspaces SET disabled_at = now() WHERE id = :i"), {"i": ws})
        await s.commit()
    headers = {}
    if logged_in:
        sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
        headers = {"Cookie": f"session={sess}"}

    resp = await app_client.get(f"/auth/accept/{token}", headers=headers)

    assert resp.status_code == 200
    _assert_nothing_disclosed(
        resp.text, ws_name="Hidden Disabled Corp", label="Hidden Disabled Corp", role="member"
    )


@pytest.mark.asyncio
async def test_accept_page_legacy_label_paths_unaffected_when_valid(
    app_client: AsyncClient,
) -> None:
    ws = await _create_workspace("Legacy Corp")
    legacy = await _invite_with_label(ws, f"workspace:{ws}")
    fallback = await _invite_with_raw_payload({"role": "member"}, _FALLBACK_LABEL)

    r1 = await app_client.get(f"/auth/accept/{legacy}")
    r2 = await app_client.get(f"/auth/accept/{fallback}")

    assert _invite_target_text(r1.text) == "Invitation for: Workspace “Legacy Corp” as member"
    assert _invite_target_text(r2.text) == f"Invitation for: {_FALLBACK_LABEL}"


@pytest.mark.asyncio
async def test_accept_page_disabled_workspace_hides_label_even_with_invalid_role(
    app_client: AsyncClient,
) -> None:
    ws = await _create_workspace("Hidden Bad Role Corp")
    token = await _invite_with_raw_payload(
        {"workspace_id": ws.upper(), "role": "admin"}, "Hidden Bad Role Corp"
    )
    async with _sm()() as s:
        await s.execute(text("UPDATE workspaces SET disabled_at = now() WHERE id = :i"), {"i": ws})
        await s.commit()

    resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    assert "Hidden Bad Role Corp" not in resp.text
    assert _NEUTRAL_LINE in resp.text


@pytest.mark.asyncio
async def test_accept_page_expired_hides_distinct_label_and_keeps_join_form(
    app_client: AsyncClient,
) -> None:
    # The payload references the workspace under test: if the page described an
    # expired invitation, it would print its name and the assertions would fail.
    ws = await _create_workspace("Expired Distinct Corp")
    token = await _invite_with_label(ws, "secret-label@acme.cl")
    await _expire(token)
    sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)

    resp = await app_client.get(f"/auth/accept/{token}", headers={"Cookie": f"session={sess}"})

    assert resp.status_code == 200
    _assert_nothing_disclosed(
        resp.text, ws_name="Expired Distinct Corp", label="secret-label@acme.cl", role="member"
    )
    assert f'action="/auth/accept/{token}/join"' in resp.text


# Non-canonical forms of a workspace UUID: `uuid.UUID` parses them, so they
# point to the same workspace even if the string is not `8-4-4-4-12`.
_WS_ID_ALIASES = [
    pytest.param(lambda ws: "{" + ws + "}", id="braced"),
    pytest.param(lambda ws: f"urn:uuid:{ws}", id="urn"),
    pytest.param(lambda ws: ws.replace("-", ""), id="unhyphenated"),
    pytest.param(lambda ws: "{" + ws.upper() + "}", id="braced-upper"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "admin"], ids=["valid-role", "invalid-role"])
@pytest.mark.parametrize("make_id", _WS_ID_ALIASES)
async def test_accept_page_disabled_workspace_withheld_for_non_canonical_id(
    app_client: AsyncClient, make_id, role: str
) -> None:
    """An alias of a disabled workspace's UUID must not make the page fall
    back to the saved label (which in admin invitations is the name)."""
    name = "Alias Secret Corp"
    label = "alias-secret-label@acme.cl"
    ws = await _create_workspace(name)
    token = await _invite_with_raw_payload({"workspace_id": make_id(ws), "role": role}, label)
    await _disable_workspace(ws)

    resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    _assert_nothing_disclosed(resp.text, ws_name=name, label=label, role=role)
    assert _invite_target_text(resp.text) is None


# ---------------------------------------------------------------------------
# An expired invitation does not query `describe_invitation` (nor leave
# warnings of a malformed payload with the role in the log)
# ---------------------------------------------------------------------------


@pytest.fixture
def describe_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count the calls to the host's `describe_invitation` callback."""
    from orcaster_server import main as app_main

    config = app_main._auth.config
    real = config.describe_invitation
    assert real is not None
    calls: list[str] = []

    async def spy(db, inv):
        calls.append(inv.id)
        return await real(db, inv)

    monkeypatch.setattr(config, "describe_invitation", spy)
    return calls


@pytest.mark.asyncio
async def test_accept_page_valid_invitation_calls_describe_once(
    app_client: AsyncClient, describe_spy: list[str]
) -> None:
    """Spy control: without it, `calls == []` in the following tests would be vacuous."""
    ws = await _create_workspace("Spy Control Corp")
    token = await _invite(ws)

    resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    assert len(describe_spy) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"workspace_id": None, "role": "owner"}, id="null-workspace-id"),
        pytest.param({"role": "owner"}, id="missing-workspace-id"),
        pytest.param({"workspace_id": "not-a-uuid", "role": "owner"}, id="not-a-uuid"),
        pytest.param(["owner"], id="non-object"),
    ],
)
async def test_accept_page_expired_skips_describe_and_logs_nothing(
    app_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
    describe_spy: list[str],
    payload: object,
) -> None:
    token = await _invite_with_raw_payload(payload, "expired-malformed@acme.cl")
    await _expire(token)

    with caplog.at_level(logging.WARNING, logger="orcaster_server.workspaces.seam"):
        resp = await app_client.get(f"/auth/accept/{token}")

    assert resp.status_code == 200
    assert _NEUTRAL_LINE in resp.text
    assert "expired-malformed@acme.cl" not in resp.text
    assert describe_spy == []
    assert [
        r.getMessage() for r in caplog.records if r.name == "orcaster_server.workspaces.seam"
    ] == []


# ---------------------------------------------------------------------------
# Revoked or used remains 410 and without workspace data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("logged_in", [False, True], ids=["anonymous", "session"])
async def test_accept_page_revoked_returns_410_without_workspace_data(
    app_client: AsyncClient, logged_in: bool
) -> None:
    ws = await _create_workspace("Revoked Corp")
    token = await _invite_with_label(ws, "revoked-label@acme.cl", role="owner")
    async with _sm()() as s:
        await revoke_invitation(s, token)
        await s.commit()
    headers = {}
    if logged_in:
        sess = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
        headers = {"Cookie": f"session={sess}"}

    resp = await app_client.get(f"/auth/accept/{token}", headers=headers)

    assert resp.status_code == 410
    assert "This invite is no longer valid" in resp.text
    assert "Revoked Corp" not in resp.text
    assert "revoked-label@acme.cl" not in resp.text
    assert "owner" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("logged_in", [False, True], ids=["anonymous", "session"])
async def test_accept_page_used_returns_410_without_workspace_data(
    app_client: AsyncClient, logged_in: bool
) -> None:
    ws = await _create_workspace("Used Corp")
    token = await _invite_with_label(ws, "used-label@acme.cl", role="owner")
    joiner = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    assert (await _post_join(app_client, token, joiner)).status_code == 303
    headers = {"Cookie": f"session={joiner}"} if logged_in else {}

    resp = await app_client.get(f"/auth/accept/{token}", headers=headers)

    assert resp.status_code == 410
    assert "This invite is no longer valid" in resp.text
    assert "Used Corp" not in resp.text
    assert "used-label@acme.cl" not in resp.text
    assert "owner" not in resp.text
