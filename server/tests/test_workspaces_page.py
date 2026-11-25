"""Tests — workspace management page (/admin/workspaces).

Covers:
  - owner sees rename/invite/enrollment-token forms; member sees read-only.
  - rename (owner) changes the name in the DB and on the page; empty name → 400.
  - invite (owner) creates an invitation with payload {workspace_id, role} and
    shows the /auth/accept/<token> URL; invalid role → 400.
  - mint enrollment token (owner) creates a row for Default and shows the token,
    which also works against POST /v1/workers/register.
  - revoke (owner) sets revoked_at; revoking another workspace's token → 404.
  - member hitting any owner-only POST → 403.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import browser_origin_hook

REGISTER = {
    "hostname": "w",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


def _db_url() -> str:
    from orcaster_server.config import settings

    return settings.database_url


async def _make_member_client(workspace_id: str = DEFAULT_WORKSPACE_ID) -> AsyncClient:
    """AsyncClient authenticated as a `member` (not owner) of the given workspace."""
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.main import app
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES ('Member') RETURNING id")
        )
        user_id = str(row.scalar_one())
        await add_member(s, workspace_id=workspace_id, user_id=user_id, role="member")
        token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
        await s.commit()

    client = AsyncClient(
        event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"HX-Request": "true"},
    )
    client.cookies.set("session", token)
    return client


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


# --- GET page: owner controls vs member read-only --------------------------


@pytest.mark.asyncio
async def test_owner_get_renders_owner_forms(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/workspaces", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'action="/admin/workspaces/rename"' in body
    assert 'action="/admin/workspaces/invite"' in body
    assert 'action="/admin/workspaces/enrollment-tokens"' in body
    assert "Default" in body


@pytest.mark.asyncio
async def test_member_get_is_read_only(app_client: AsyncClient) -> None:
    client = await _make_member_client()
    try:
        resp = await client.get("/admin/workspaces", headers={"HX-Request": "false"})
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Owner-only forms remain hidden from members.
        assert 'action="/admin/workspaces/rename"' not in body
        assert 'action="/admin/workspaces/invite"' not in body
        # The enrollment tokens form is available to all members.
        assert 'action="/admin/workspaces/enrollment-tokens"' in body
        # Read-only label still present.
        assert "Members" in body
    finally:
        await client.aclose()


# --- Rename ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_rename_changes_name(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/workspaces/rename",
        data={"name": "Renamed WS"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/workspaces"

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            name = (
                await conn.execute(
                    text("SELECT name FROM workspaces WHERE id = :id"),
                    {"id": DEFAULT_WORKSPACE_ID},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert name == "Renamed WS"

    page = await authed_admin_client.get("/admin/workspaces", headers={"HX-Request": "false"})
    assert "Renamed WS" in page.text


@pytest.mark.asyncio
async def test_owner_rename_blank_rejected(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/workspaces/rename",
        data={"name": "   "},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 400, resp.text

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            name = (
                await conn.execute(
                    text("SELECT name FROM workspaces WHERE id = :id"),
                    {"id": DEFAULT_WORKSPACE_ID},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert name == "Default"


# --- Invite ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_invite_creates_invitation(authed_admin_client: AsyncClient) -> None:
    # PRG: the POST redirects (303) and the secret is revealed after following the GET.
    resp = await authed_admin_client.post(
        "/admin/workspaces/invite",
        data={"role": "member"},
        headers={"HX-Request": "false"},
        follow_redirects=True,
    )
    assert resp.status_code == 200, resp.text
    assert "/auth/accept/" in resp.text

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT payload, label FROM invitations "
                            "ORDER BY expires_at DESC LIMIT 1"
                        )
                    )
                )
                .mappings()
                .first()
            )
    finally:
        await engine.dispose()
    assert row is not None
    assert row["payload"] == {"workspace_id": DEFAULT_WORKSPACE_ID, "role": "member"}
    # The label is the workspace name, not `workspace:<uuid>`.
    assert row["label"] == "Default"


@pytest.mark.asyncio
async def test_owner_invite_invalid_role_400(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/workspaces/invite",
        data={"role": "superadmin"},
        headers={"HX-Request": "false"},
    )
    assert resp.status_code == 400, resp.text


# --- Enrollment tokens -----------------------------------------------------


@pytest.mark.asyncio
async def test_owner_mint_token_and_register(authed_admin_client: AsyncClient) -> None:
    # PRG: the POST redirects (303); follow the GET to see the one-time reveal.
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "ci-runner"},
        headers={"HX-Request": "false"},
        follow_redirects=True,
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The one-time secret reveal (blocking modal) is shown once after minting.
    assert "Worker enrollment token created" in body

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :id AND label = 'ci-runner'"
                    ),
                    {"id": DEFAULT_WORKSPACE_ID},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert count == 1

    # Extract the token value from the env-block pre and use it to register.
    import re

    match = re.search(r"ORCH_ENROLLMENT_TOKEN=([^\n<]+)", body)
    assert match is not None, "ORCH_ENROLLMENT_TOKEN not found in page"
    token = match.group(1).strip()

    reg = await authed_admin_client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": "enrolled", "enrollment_token": token},
    )
    assert reg.status_code == 200, reg.text


@pytest.mark.asyncio
async def test_owner_mint_multiuse_token(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "fleet", "max_uses": "5", "ttl": "30d"},
        headers={"HX-Request": "false"},
        follow_redirects=True,
    )
    assert resp.status_code == 200, resp.text

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT max_uses FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :id AND label = 'fleet'"
                    ),
                    {"id": DEFAULT_WORKSPACE_ID},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert row == 5


@pytest.mark.asyncio
async def test_mint_rejects_invalid_max_uses(authed_admin_client: AsyncClient) -> None:
    # Out-of-range and non-numeric values land in the same 400 re-render (max_uses
    # is received as str to avoid a coercion 422 instead of the graceful 400).
    for bad in ("0", "-1", "1001", "abc"):
        resp = await authed_admin_client.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "bad", "max_uses": bad, "ttl": "7d"},
            headers={"HX-Request": "false"},
        )
        assert resp.status_code == 400, (bad, resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_mint_empty_max_uses_defaults_single_use(
    authed_admin_client: AsyncClient,
) -> None:
    """An empty `max_uses` uses the single-use default (303), not a coercion 422."""
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "empty-mu", "max_uses": "", "ttl": "7d"},
        headers={"HX-Request": "false"},
    )
    assert resp.status_code == 303, resp.text


@pytest.mark.asyncio
async def test_mint_rejects_invalid_ttl(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "bad", "max_uses": "1", "ttl": "999y"},
        headers={"HX-Request": "false"},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_owner_revoke_token(authed_admin_client: AsyncClient) -> None:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID, label="to-revoke")
        await s.commit()

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :id AND label = 'to-revoke'"
                    ),
                    {"id": DEFAULT_WORKSPACE_ID},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            revoked = (
                await conn.execute(
                    text("SELECT revoked_at FROM worker_enrollment_tokens WHERE id = :id"),
                    {"id": token_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert revoked is not None


@pytest.mark.asyncio
async def test_owner_revoke_foreign_token_404(authed_admin_client: AsyncClient) -> None:
    ws_b = await _create_workspace("WS_B", f"ws-b-{uuid.uuid4().hex[:8]}")
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await mint_enrollment_token(s, workspace_id=ws_b, label="foreign")
        await s.commit()

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = (
                await conn.execute(
                    text("SELECT id FROM worker_enrollment_tokens WHERE workspace_id = :id"),
                    {"id": ws_b},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_page_shows_use_counter_and_exhausted(
    authed_admin_client: AsyncClient,
) -> None:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        # Single-use token (max_uses=1) that we exhaust via register.
        token = await mint_enrollment_token(
            s, workspace_id=DEFAULT_WORKSPACE_ID, max_uses=1, label="one-shot"
        )
        await s.commit()
    reg = await authed_admin_client.post(
        "/v1/workers/register",
        json={**REGISTER, "hostname": "exh", "enrollment_token": token},
    )
    assert reg.status_code == 200, reg.text

    page = await authed_admin_client.get("/admin/workspaces?show=all")
    body = page.text
    assert "1 / 1" in body  # contador
    assert "badge-exhausted" in body  # estado agotado


# --- Server pubkey download ------------------------------------------------


@pytest.mark.asyncio
async def test_pubkey_download_returns_pem(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/workspaces/pubkey")
    assert resp.status_code == 200, resp.text
    assert "-----BEGIN PUBLIC KEY-----" in resp.text
    assert resp.headers["content-disposition"] == 'attachment; filename="server-ed25519.pub"'


@pytest.mark.asyncio
async def test_pubkey_download_available_to_member(app_client: AsyncClient) -> None:
    client = await _make_member_client()
    try:
        resp = await client.get("/admin/workspaces/pubkey")
        assert resp.status_code == 200, resp.text
        assert "-----BEGIN PUBLIC KEY-----" in resp.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pubkey_503_when_key_not_configured(
    authed_admin_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orcaster_server import signing
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "ed25519_private_key_path", "")
    signing.reset_key_cache()
    resp = await authed_admin_client.get("/admin/workspaces/pubkey")
    assert resp.status_code == 503, resp.text
    assert resp.headers.get("retry-after") == "60"


# --- Member 403 on owner POST endpoints ------------------------------------


@pytest.mark.asyncio
async def test_member_owner_actions_403(app_client: AsyncClient) -> None:
    """Rename and invite remain owner-only (403). Mint is not.

    Revoking a random token (which does not exist in the member's workspace)
    returns 404 — the member cannot revoke other people's or nonexistent tokens.
    """
    client = await _make_member_client()
    try:
        rename = await client.post(
            "/admin/workspaces/rename",
            data={"name": "X"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert rename.status_code == 403, rename.text

        invite = await client.post(
            "/admin/workspaces/invite",
            data={"role": "member"},
            headers={"HX-Request": "false"},
        )
        assert invite.status_code == 403, invite.text

        # Mint is available to members (no more 403).
        # PRG: a successful mint redirects (303); it no longer returns 200 inline.
        mint = await client.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "x"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert mint.status_code == 303, mint.text

        # Revoking someone else's token (random nonexistent UUID) → 404, not 403.
        revoke = await client.post(
            f"/admin/workspaces/enrollment-tokens/{uuid.uuid4()}/revoke",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert revoke.status_code == 404, revoke.text
    finally:
        await client.aclose()


# --- Onboarding panel ------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_shows_onboarding_env_block(
    authed_admin_client: AsyncClient,
) -> None:
    # PRG: the POST redirects (303); follow the GET to see the .env block.
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "ci"},
        headers={"HX-Request": "false"},
        follow_redirects=True,
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The .env block carries the three variables the client expects...
    assert "ORCH_BASE_URL=" in body
    assert "ORCH_ENROLLMENT_TOKEN=" in body
    assert "ORCH_WORKER_NAME=" in body
    # ...and the panel offers to download the pubkey.
    assert 'href="/admin/workspaces/pubkey"' in body


@pytest.mark.asyncio
async def test_workspaces_page_has_persistent_pubkey_download(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.get("/admin/workspaces", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    # The download button is available without minting a token.
    assert 'href="/admin/workspaces/pubkey"' in resp.text


# --- Drill-down "Workers enrolled" -----------------------------------------


@pytest.mark.asyncio
async def test_token_uses_drilldown_lists_workers(
    authed_admin_client: AsyncClient,
) -> None:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token, peek_enrollment_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token = await mint_enrollment_token(
            s, workspace_id=DEFAULT_WORKSPACE_ID, max_uses=2, label="drill"
        )
        await s.commit()
    for i in range(2):
        r = await authed_admin_client.post(
            "/v1/workers/register",
            json={**REGISTER, "hostname": f"d{i}", "enrollment_token": token},
        )
        assert r.status_code == 200, r.text
    async with sm() as s:
        tok = await peek_enrollment_token(s, token)
        token_id = str(tok.id)

    resp = await authed_admin_client.get(f"/admin/workspaces/enrollment-tokens/{token_id}/uses")
    assert resp.status_code == 200, resp.text
    # Two usage entries in the fragment.
    assert resp.text.count("token-use-row") == 2


@pytest.mark.asyncio
async def test_token_uses_drilldown_unknown_id_404(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.get(
        "/admin/workspaces/enrollment-tokens/00000000-0000-0000-0000-000000000000/uses"
    )
    assert resp.status_code == 404, resp.text


async def _make_member_client_with_id(
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> tuple[AsyncClient, str]:
    """AsyncClient authenticated as a `member`; returns (client, user_id).

    Same pattern as `_make_member_client_with_id` in test_selfservice_tokens.py
    — here we need the user_id to mint tokens with an explicit
    `created_by_user_id` and so tell apart two members of the same workspace.
    """
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES ('MemberUses') RETURNING id")
        )
        user_id = str(row.scalar_one())
        await add_member(s, workspace_id=workspace_id, user_id=user_id, role="member")
        token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
        await s.commit()

    from orcaster_server.main import app

    client = AsyncClient(
        event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"HX-Request": "true"},
    )
    client.cookies.set("session", token)
    return client, user_id


@pytest.mark.asyncio
async def test_token_uses_drilldown_member_scoped_by_creator(
    app_client: AsyncClient,
) -> None:
    """A member sees the drill-down of THEIR token; another member's gives 404.

    Exercises the `creator_filter = None if ctx.role == "owner" else ctx.user.id`
    of the `admin_workspace_token_uses` handler (and the `created_by_user_id`
    that `list_token_uses` applies) on the MEMBER path, which had no coverage:
    the existing tests of this drill-down only use `authed_admin_client`
    (owner, `creator_filter=None`). If someone inverts or removes the filter and
    two members end up seeing each other's tokens, this test must fail.
    """
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    client_a, user_a = await _make_member_client_with_id()
    client_b, user_b = await _make_member_client_with_id()
    try:
        sm = app_db.get_sessionmaker()
        async with sm() as s:
            await mint_enrollment_token(
                s,
                workspace_id=DEFAULT_WORKSPACE_ID,
                created_by_user_id=user_a,
                label="uses-scope-a",
            )
            await mint_enrollment_token(
                s,
                workspace_id=DEFAULT_WORKSPACE_ID,
                created_by_user_id=user_b,
                label="uses-scope-b",
            )
            await s.commit()

        engine = create_async_engine(_db_url())
        try:
            async with engine.connect() as conn:
                token_a_id = (
                    await conn.execute(
                        text(
                            "SELECT id FROM worker_enrollment_tokens "
                            "WHERE workspace_id = :w AND label = 'uses-scope-a'"
                        ),
                        {"w": DEFAULT_WORKSPACE_ID},
                    )
                ).scalar_one()
                token_b_id = (
                    await conn.execute(
                        text(
                            "SELECT id FROM worker_enrollment_tokens "
                            "WHERE workspace_id = :w AND label = 'uses-scope-b'"
                        ),
                        {"w": DEFAULT_WORKSPACE_ID},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

        # A sees the drill-down of their own token.
        resp_own = await client_a.get(f"/admin/workspaces/enrollment-tokens/{token_a_id}/uses")
        assert resp_own.status_code == 200, resp_own.text

        # A does NOT see the drill-down of B's token (another member) -> 404.
        resp_foreign = await client_a.get(f"/admin/workspaces/enrollment-tokens/{token_b_id}/uses")
        assert resp_foreign.status_code == 404, resp_foreign.text
    finally:
        await client_a.aclose()
        await client_b.aclose()


# === tz notes gated per section (Members and ==============================
# Worker enrollment tokens) ==================================================
# workspaces.html includes _tz_note.html TWICE, once per section, each gated by
# its own `{% if %}` (members / tokens) and without tz_note_id (the partial
# cannot set a fixed id here because it is included more than once in the same
# page; see the comment in _tz_note.html itself). Without these tests nobody
# would notice if an include moved out of its gate, or if someone gave it a
# fixed tz_note_id, producing duplicate ids.


@pytest.mark.asyncio
async def test_workspaces_page_shows_tz_note_for_members_only_by_default(
    authed_admin_client: AsyncClient,
) -> None:
    # authed_admin_client is already an owner-member of the Default workspace
    # (see conftest._mint_session_for_workspace), so Members is never empty;
    # enrollment tokens is, until one is minted.
    resp = await authed_admin_client.get("/admin/workspaces", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert body.count("js-tz-note") == 1
    idx_members = body.index("<h2>Members</h2>")
    idx_note = body.index("js-tz-note")
    idx_enrollment = body.index("<h2>Worker enrollment tokens</h2>")
    assert idx_members < idx_note < idx_enrollment


@pytest.mark.asyncio
async def test_workspaces_page_shows_tz_note_for_both_sections_with_token(
    authed_admin_client: AsyncClient,
) -> None:
    mint = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "tz-note-token"},
        headers={"HX-Request": "false"},
        follow_redirects=True,
    )
    assert mint.status_code == 200, mint.text
    # Now Members AND enrollment tokens have data -> two notes, no ids.
    assert mint.text.count("js-tz-note") == 2
