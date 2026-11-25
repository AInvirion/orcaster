"""Tests of self-service enrollment tokens for members, scoped by creator.

Covers the authorization policy:
  - a member can mint their own token.
  - a member sees only their own tokens on the page; not those of another
    member nor those of the owner.
  - the owner still sees all tokens of the workspace.
  - a member revokes their own token (ok) and gets 404 when trying to revoke
    someone else's.
  - the owner can revoke any token of the workspace.
  - (regression) cross-workspace still gives 404 for both roles.
  - list_enrollment_tokens with a created_by_user_id filter (service layer).
  - revoke_enrollment_token_by_id with a created_by_user_id filter (service
    layer).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: F401 — used in helpers

from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import browser_origin_hook


def _db_url() -> str:
    from orcaster_server.config import settings

    return settings.database_url


# ---------------------------------------------------------------------------
# Session fixture helpers
# ---------------------------------------------------------------------------


async def _make_member_client_with_id(
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> tuple[AsyncClient, str]:
    """AsyncClient authenticated as a `member`; returns (client, user_id)."""
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.main import app
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES ('MemberSS') RETURNING id")
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
    return client, user_id


async def _create_workspace(name: str, slug: str) -> str:
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.service import create_workspace

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        ws_id = await create_workspace(s, name=name, slug=slug)
        await s.commit()
        return ws_id


async def _mint_token_for_user(workspace_id: str, user_id: str, label: str) -> str:
    """Mint a token scoped to the given user directly in the DB; returns token_id."""
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await mint_enrollment_token(
            s,
            workspace_id=workspace_id,
            created_by_user_id=user_id,
            label=label,
        )
        await s.commit()

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :w AND label = :l AND created_by_user_id = :u "
                        "ORDER BY expires_at DESC LIMIT 1"
                    ),
                    {"w": workspace_id, "l": label, "u": user_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    return str(token_id)


# ---------------------------------------------------------------------------
# 1. A member can mint a token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_can_mint_token(app_client: AsyncClient) -> None:
    """A member can generate an enrollment token for their workspace."""
    client, _uid = await _make_member_client_with_id()
    try:
        # PRG: the POST redirects (303); we follow with the GET to see the reveal.
        resp = await client.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "member-ci"},
            headers={"HX-Request": "false"},
            follow_redirects=True,
        )
        assert resp.status_code == 200, resp.text
        # The onboarding panel ("Add a worker") shows the newly issued token
        # inside the ready-to-paste .env block.
        assert "ORCH_ENROLLMENT_TOKEN=" in resp.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_member_mint_token_persisted_with_creator(app_client: AsyncClient) -> None:
    """A token minted by a member is stored with their created_by_user_id."""
    client, user_id = await _make_member_client_with_id()
    try:
        resp = await client.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "member-token-owner"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
    finally:
        await client.aclose()

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT created_by_user_id FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :w AND label = 'member-token-owner'"
                    ),
                    {"w": DEFAULT_WORKSPACE_ID},
                )
            ).first()
    finally:
        await engine.dispose()
    assert row is not None
    assert str(row[0]) == user_id


# ---------------------------------------------------------------------------
# 2. A member sees only their own tokens; not another member's nor the owner's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_sees_only_own_tokens_in_page(
    authed_admin_client: AsyncClient,
    app_client: AsyncClient,
) -> None:
    """A member sees only their tokens; the owner's do not appear in their list."""
    # Owner mints a token
    owner_resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "owner-token"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert owner_resp.status_code == 303, owner_resp.text

    # Member mints their own token
    client, _uid = await _make_member_client_with_id()
    try:
        member_resp = await client.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "member-own-token"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert member_resp.status_code == 303, member_resp.text

        # Member loads the page: must see "member-own-token" but NOT "owner-token"
        page = await client.get("/admin/workspaces", headers={"HX-Request": "false"})
        assert page.status_code == 200, page.text
        body = page.text
        assert "member-own-token" in body
        assert "owner-token" not in body
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_member_does_not_see_other_member_tokens(app_client: AsyncClient) -> None:
    """Two members do not see each other's tokens."""
    client_a, user_a = await _make_member_client_with_id()
    client_b, _user_b = await _make_member_client_with_id()
    try:
        # A mints a token
        resp_a = await client_a.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "token-of-member-a"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert resp_a.status_code == 303, resp_a.text

        # B loads the page: must not see A's token
        page_b = await client_b.get("/admin/workspaces", headers={"HX-Request": "false"})
        assert page_b.status_code == 200, page_b.text
        assert "token-of-member-a" not in page_b.text
    finally:
        await client_a.aclose()
        await client_b.aclose()


# ---------------------------------------------------------------------------
# 3. The owner sees all tokens of the workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_sees_all_tokens(
    authed_admin_client: AsyncClient,
    app_client: AsyncClient,
) -> None:
    """The owner sees their own tokens and those of any member."""
    # Member mints a token
    client, _uid = await _make_member_client_with_id()
    try:
        resp = await client.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "member-visible-to-owner"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
    finally:
        await client.aclose()

    # Owner mints their own
    owner_resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "owner-also-visible"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert owner_resp.status_code == 303, owner_resp.text

    # Owner loads the page: sees both
    page = await authed_admin_client.get("/admin/workspaces", headers={"HX-Request": "false"})
    assert page.status_code == 200, page.text
    body = page.text
    assert "member-visible-to-owner" in body
    assert "owner-also-visible" in body


# ---------------------------------------------------------------------------
# 4. A member revokes their own token (ok) and someone else's gives 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_can_revoke_own_token(app_client: AsyncClient) -> None:
    """A member can revoke a token they created themselves."""
    client, user_id = await _make_member_client_with_id()
    try:
        # Mint their own token
        mint_resp = await client.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "member-to-revoke"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert mint_resp.status_code == 303, mint_resp.text

        # Get the id of the newly created token
        engine = create_async_engine(_db_url())
        try:
            async with engine.connect() as conn:
                token_id = (
                    await conn.execute(
                        text(
                            "SELECT id FROM worker_enrollment_tokens "
                            "WHERE workspace_id = :w AND label = 'member-to-revoke' "
                            "AND created_by_user_id = :u"
                        ),
                        {"w": DEFAULT_WORKSPACE_ID, "u": user_id},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

        # Revoke their own token
        revoke_resp = await client.post(
            f"/admin/workspaces/enrollment-tokens/{token_id}/revoke",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert revoke_resp.status_code == 303, revoke_resp.text

        # Verify it was revoked in the DB
        engine = create_async_engine(_db_url())
        try:
            async with engine.connect() as conn:
                revoked_at = (
                    await conn.execute(
                        text("SELECT revoked_at FROM worker_enrollment_tokens WHERE id = :id"),
                        {"id": token_id},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        assert revoked_at is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_member_revoke_foreign_token_404(
    authed_admin_client: AsyncClient,
    app_client: AsyncClient,
) -> None:
    """A member who tries to revoke an owner's token gets 404."""
    # Owner creates a token
    mint_resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "owner-unrevokable-by-member"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert mint_resp.status_code == 303, mint_resp.text

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :w AND label = 'owner-unrevokable-by-member'"
                    ),
                    {"w": DEFAULT_WORKSPACE_ID},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    # Member tries to revoke the owner's token → 404
    client, _uid = await _make_member_client_with_id()
    try:
        revoke_resp = await client.post(
            f"/admin/workspaces/enrollment-tokens/{token_id}/revoke",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert revoke_resp.status_code == 404, revoke_resp.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_member_revoke_other_member_token_404(app_client: AsyncClient) -> None:
    """Member A who tries to revoke member B's token gets 404."""
    client_a, user_a = await _make_member_client_with_id()
    client_b, _user_b = await _make_member_client_with_id()
    try:
        # A mints a token
        mint_resp = await client_a.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "token-a-to-protect"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert mint_resp.status_code == 303, mint_resp.text

        engine = create_async_engine(_db_url())
        try:
            async with engine.connect() as conn:
                token_id = (
                    await conn.execute(
                        text(
                            "SELECT id FROM worker_enrollment_tokens "
                            "WHERE workspace_id = :w AND label = 'token-a-to-protect' "
                            "AND created_by_user_id = :u"
                        ),
                        {"w": DEFAULT_WORKSPACE_ID, "u": user_a},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

        # B tries to revoke A's token → 404
        revoke_resp = await client_b.post(
            f"/admin/workspaces/enrollment-tokens/{token_id}/revoke",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert revoke_resp.status_code == 404, revoke_resp.text
    finally:
        await client_a.aclose()
        await client_b.aclose()


# ---------------------------------------------------------------------------
# 5. The owner can revoke any token of the workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_can_revoke_member_token(
    authed_admin_client: AsyncClient,
    app_client: AsyncClient,
) -> None:
    """The owner can revoke a token created by a member."""
    client, user_id = await _make_member_client_with_id()
    try:
        mint_resp = await client.post(
            "/admin/workspaces/enrollment-tokens",
            data={"label": "member-token-for-owner-revoke"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert mint_resp.status_code == 303, mint_resp.text
    finally:
        await client.aclose()

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :w AND label = 'member-token-for-owner-revoke'"
                    ),
                    {"w": DEFAULT_WORKSPACE_ID},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    revoke_resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert revoke_resp.status_code == 303, revoke_resp.text

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            revoked_at = (
                await conn.execute(
                    text("SELECT revoked_at FROM worker_enrollment_tokens WHERE id = :id"),
                    {"id": token_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert revoked_at is not None


# ---------------------------------------------------------------------------
# 6. Regression: cross-workspace still gives 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_revoke_cross_workspace_token_404(app_client: AsyncClient) -> None:
    """A member cannot revoke a token of another workspace (cross-tenant)."""
    ws_b = await _create_workspace("WS_B_SS", f"ws-b-ss-{uuid.uuid4().hex[:8]}")

    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await mint_enrollment_token(s, workspace_id=ws_b, label="foreign-ws-b")
        await s.commit()

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :w AND label = 'foreign-ws-b'"
                    ),
                    {"w": ws_b},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    # Member of the Default workspace tries to revoke a ws_b token → 404
    client, _uid = await _make_member_client_with_id(DEFAULT_WORKSPACE_ID)
    try:
        revoke_resp = await client.post(
            f"/admin/workspaces/enrollment-tokens/{token_id}/revoke",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert revoke_resp.status_code == 404, revoke_resp.text
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 7. Service layer: list_enrollment_tokens with a created_by_user_id filter
# ---------------------------------------------------------------------------


async def _make_db_user(db_session, name: str) -> str:
    """Insert a user in the DB and return its id (str)."""
    row = await db_session.execute(
        text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"),
        {"n": name},
    )
    uid = str(row.scalar_one())
    await db_session.commit()
    return uid


@pytest.mark.asyncio
async def test_list_tokens_filtered_by_creator(db_session) -> None:
    """list_enrollment_tokens filters by created_by_user_id when it is passed."""
    from orcaster_server.workspaces.enrollment import list_enrollment_tokens, mint_enrollment_token
    from orcaster_server.workspaces.service import create_workspace

    ws_id = await create_workspace(db_session, name="TestWS", slug="test-ws-list")
    await db_session.commit()

    # Mint tokens for two different users (real DB ids because of the FK)
    user_a = await _make_db_user(db_session, "UserA-list")
    user_b = await _make_db_user(db_session, "UserB-list")
    await mint_enrollment_token(
        db_session,
        workspace_id=ws_id,
        created_by_user_id=user_a,
        label="tok-a",
    )
    await mint_enrollment_token(
        db_session,
        workspace_id=ws_id,
        created_by_user_id=user_b,
        label="tok-b",
    )
    await db_session.commit()

    # Without a filter: both tokens
    all_tokens = await list_enrollment_tokens(db_session, ws_id)
    assert len(all_tokens) == 2  # noqa: PLR2004

    # With the user_a filter: only their token
    tokens_a = await list_enrollment_tokens(db_session, ws_id, created_by_user_id=user_a)
    assert len(tokens_a) == 1  # noqa: PLR2004
    assert tokens_a[0].label == "tok-a"

    # With the user_b filter: only their token
    tokens_b = await list_enrollment_tokens(db_session, ws_id, created_by_user_id=user_b)
    assert len(tokens_b) == 1  # noqa: PLR2004
    assert tokens_b[0].label == "tok-b"


@pytest.mark.asyncio
async def test_list_tokens_active_only(db_session) -> None:
    """list_enrollment_tokens(active_only=True) excluye exhausted/revoked/expired."""
    from orcaster_server.workspaces.enrollment import (
        list_enrollment_tokens,
        mint_enrollment_token,
        revoke_enrollment_token_by_id,
    )
    from orcaster_server.workspaces.service import create_workspace

    ws_id = await create_workspace(db_session, name="ActiveWS", slug="active-only-ws")
    await db_session.commit()

    async def _mint(label: str) -> str:
        await mint_enrollment_token(db_session, workspace_id=ws_id, label=label)
        await db_session.commit()
        row = (
            await db_session.execute(
                text(
                    "SELECT id FROM worker_enrollment_tokens WHERE workspace_id = :w AND label = :l"
                ),
                {"w": ws_id, "l": label},
            )
        ).first()
        return str(row[0])

    active_id = await _mint("active-tok")
    used_id = await _mint("used-tok")
    revoked_id = await _mint("revoked-tok")
    expired_id = await _mint("expired-tok")

    # Exhaust the use quota (multi-use model: the discriminant is
    # `use_count >= max_uses`, not `used_at`, see `_token_status`).
    await db_session.execute(
        text(
            "UPDATE worker_enrollment_tokens "
            "SET use_count = max_uses, used_at = now() WHERE id = :id"
        ),
        {"id": used_id},
    )
    await revoke_enrollment_token_by_id(db_session, token_id=revoked_id, workspace_id=ws_id)
    await db_session.execute(
        text(
            "UPDATE worker_enrollment_tokens "
            "SET expires_at = now() - interval '1 hour' WHERE id = :id"
        ),
        {"id": expired_id},
    )
    await db_session.commit()

    # Without a filter: all 4.
    all_tokens = await list_enrollment_tokens(db_session, ws_id)
    assert len(all_tokens) == 4  # noqa: PLR2004

    # active_only: only the active one.
    active = await list_enrollment_tokens(db_session, ws_id, active_only=True)
    assert [str(t.id) for t in active] == [active_id]


# ---------------------------------------------------------------------------
# 8. Service layer: revoke_enrollment_token_by_id with a created_by_user_id filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_token_scoped_by_creator(db_session) -> None:
    """revoke_enrollment_token_by_id fails (False) if the creator does not match."""
    from orcaster_server.workspaces.enrollment import (
        mint_enrollment_token,
        revoke_enrollment_token_by_id,
    )
    from orcaster_server.workspaces.service import create_workspace

    ws_id = await create_workspace(db_session, name="TestWS2", slug="test-ws-revoke")
    await db_session.commit()

    user_a = await _make_db_user(db_session, "UserA-revoke")
    user_b = await _make_db_user(db_session, "UserB-revoke")
    await mint_enrollment_token(
        db_session,
        workspace_id=ws_id,
        created_by_user_id=user_a,
        label="revoke-scope-test",
    )
    await db_session.commit()

    # Get the token id
    from sqlalchemy import text as _text

    row = (
        await db_session.execute(
            _text(
                "SELECT id FROM worker_enrollment_tokens "
                "WHERE workspace_id = :w AND label = 'revoke-scope-test'"
            ),
            {"w": ws_id},
        )
    ).first()
    token_id = str(row[0])

    # user_b tries to revoke user_a's token → False
    ok_wrong = await revoke_enrollment_token_by_id(
        db_session,
        token_id=token_id,
        workspace_id=ws_id,
        created_by_user_id=user_b,
    )
    assert ok_wrong is False

    # user_a revokes their own token → True
    ok_own = await revoke_enrollment_token_by_id(
        db_session,
        token_id=token_id,
        workspace_id=ws_id,
        created_by_user_id=user_a,
    )
    assert ok_own is True


@pytest.mark.asyncio
async def test_revoke_token_without_creator_filter(db_session) -> None:
    """revoke_enrollment_token_by_id without a creator filter still works (owner path)."""
    from orcaster_server.workspaces.enrollment import (
        mint_enrollment_token,
        revoke_enrollment_token_by_id,
    )
    from orcaster_server.workspaces.service import create_workspace

    ws_id = await create_workspace(db_session, name="TestWS3", slug="test-ws-revoke-no-filter")
    await db_session.commit()

    user_a = await _make_db_user(db_session, "UserA-nofilter")
    await mint_enrollment_token(
        db_session,
        workspace_id=ws_id,
        created_by_user_id=user_a,
        label="revoke-no-filter",
    )
    await db_session.commit()

    from sqlalchemy import text as _text

    row = (
        await db_session.execute(
            _text(
                "SELECT id FROM worker_enrollment_tokens "
                "WHERE workspace_id = :w AND label = 'revoke-no-filter'"
            ),
            {"w": ws_id},
        )
    ).first()
    token_id = str(row[0])

    # Without a creator filter (owner path): returns True
    ok = await revoke_enrollment_token_by_id(
        db_session,
        token_id=token_id,
        workspace_id=ws_id,
    )
    assert ok is True


# ---------------------------------------------------------------------------
# 9. Template: the generate-token form appears for members
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_sees_generate_token_form(app_client: AsyncClient) -> None:
    """A member sees the 'Generate enrollment token' form (not only owners)."""
    client, _uid = await _make_member_client_with_id()
    try:
        resp = await client.get("/admin/workspaces", headers={"HX-Request": "false"})
        assert resp.status_code == 200, resp.text
        assert 'action="/admin/workspaces/enrollment-tokens"' in resp.text
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 10. State helpers for the delete tests
# ---------------------------------------------------------------------------


async def _mint_default_token(label: str) -> str:
    """Mint a token (owner path, no creator) in the Default workspace."""
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID, label=label)
        await s.commit()

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM worker_enrollment_tokens "
                        "WHERE workspace_id = :w AND label = :l ORDER BY expires_at DESC LIMIT 1"
                    ),
                    {"w": DEFAULT_WORKSPACE_ID, "l": label},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    return str(token_id)


async def _force_token_state(
    token_id: str, *, revoked: bool = False, used: bool = False, expired: bool = False
) -> None:
    """Force a token's terminal state directly in the DB.

    `used` forces the "exhausted" state of the multi-use model: `use_count` is
    set equal to `max_uses` (`used_at` is not enough, as it is now only
    metadata of the last consumption; the state discriminant is
    `use_count >= max_uses`, see `_token_status` in
    `orcaster_server/api/admin_ui.py`).
    """
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            if revoked:
                await conn.execute(
                    text("UPDATE worker_enrollment_tokens SET revoked_at = now() WHERE id = :id"),
                    {"id": token_id},
                )
            if used:
                await conn.execute(
                    text(
                        "UPDATE worker_enrollment_tokens "
                        "SET use_count = max_uses, used_at = now() WHERE id = :id"
                    ),
                    {"id": token_id},
                )
            if expired:
                await conn.execute(
                    text(
                        "UPDATE worker_enrollment_tokens "
                        "SET expires_at = now() - interval '1 hour' WHERE id = :id"
                    ),
                    {"id": token_id},
                )
    finally:
        await engine.dispose()


async def _token_exists(token_id: str) -> bool:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT 1 FROM worker_enrollment_tokens WHERE id = :id"),
                    {"id": token_id},
                )
            ).first()
    finally:
        await engine.dispose()
    return row is not None


# ---------------------------------------------------------------------------
# 11. Deleting dead tokens (revoked / expired); active gives 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_can_delete_revoked_token(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """The owner deletes a revoked token: 303 and it disappears from the DB."""
    token_id = await _mint_default_token("to-delete-revoked")
    await _force_token_state(token_id, revoked=True)

    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert await _token_exists(token_id) is False


@pytest.mark.asyncio
async def test_owner_can_delete_expired_token(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """The owner deletes an expired token (never used nor revoked): 303 and it disappears."""
    token_id = await _mint_default_token("to-delete-expired")
    await _force_token_state(token_id, expired=True)

    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert await _token_exists(token_id) is False


@pytest.mark.asyncio
async def test_cannot_delete_active_token(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """An active token is not deletable: 404 and it stays in the DB (enforced in SQL)."""
    token_id = await _mint_default_token("active-undeletable")

    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text
    assert await _token_exists(token_id) is True


@pytest.mark.asyncio
async def test_can_delete_used_token(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """A used token IS now deletable: 303 and it disappears from the DB.

    The forensic trail of the consumption lives in admin_actions, so deleting
    the token row does not lose traceability.
    """
    token_id = await _mint_default_token("used-deletable")
    await _force_token_state(token_id, used=True)

    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert await _token_exists(token_id) is False


@pytest.mark.asyncio
async def test_delete_redirect_preserves_show_all(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """The delete redirect preserves `?show=all`.

    Deletable tokens are only visible under `?show=all`; the no-JS fallback
    does a native POST → 303, so the handler must return the operator to the
    same filtered view instead of the Active one (which no longer shows the
    deleted token).
    """
    with_all = await _mint_default_token("redir-with-all")
    await _force_token_state(with_all, used=True)
    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{with_all}/delete?show=all",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/workspaces?show=all"

    # Without the filter, the redirect falls to the default view (Active).
    without_all = await _mint_default_token("redir-no-all")
    await _force_token_state(without_all, used=True)
    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{without_all}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/workspaces"


@pytest.mark.asyncio
async def test_revoke_redirect_preserves_show_all(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """The revoke redirect preserves `?show=all`.

    Revoke applies to active tokens (visible in the default view), but the
    operator may be on `?show=all`; the no-JS fallback does a native POST →
    303, so the handler must return them to the same filter instead of
    dropping them out of it.
    """
    with_all = await _mint_default_token("revoke-redir-with-all")
    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{with_all}/revoke?show=all",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/workspaces?show=all"

    # Without the filter, the redirect falls to the default view.
    without_all = await _mint_default_token("revoke-redir-no-all")
    resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{without_all}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/workspaces"


@pytest.mark.asyncio
async def test_delete_malformed_uuid_404(authed_admin_client: AsyncClient) -> None:
    """Malformed UUID → 404 (not 500 from a pg parse error)."""
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens/not-a-uuid/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_revoke_urn_uuid_404(authed_admin_client: AsyncClient) -> None:
    """`urn:uuid:<hex>` passes Python's `UUID()` but NOT the Postgres CAST:
    without normalizing the path param, the endpoint returned 500 (DataError)."""
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens/urn:uuid:00000000-0000-0000-0000-000000000000/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_delete_urn_uuid_404(authed_admin_client: AsyncClient) -> None:
    """`urn:uuid:<hex>` passes Python's `UUID()` but NOT the Postgres CAST:
    without normalizing the path param, the endpoint returned 500 (DataError)."""
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens/urn:uuid:00000000-0000-0000-0000-000000000000/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_member_can_delete_own_revoked_token(app_client: AsyncClient) -> None:
    """A member deletes their own revoked token: 303 and it disappears."""
    client, user_id = await _make_member_client_with_id()
    try:
        token_id = await _mint_token_for_user(DEFAULT_WORKSPACE_ID, user_id, "member-own-del")
        await _force_token_state(token_id, revoked=True)

        resp = await client.post(
            f"/admin/workspaces/enrollment-tokens/{token_id}/delete",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        assert await _token_exists(token_id) is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_member_cannot_delete_foreign_revoked_token(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """A member cannot delete someone else's revoked token (the owner's): 404, it stays."""
    token_id = await _mint_default_token("owner-revoked-foreign")
    await _force_token_state(token_id, revoked=True)

    client, _uid = await _make_member_client_with_id()
    try:
        resp = await client.post(
            f"/admin/workspaces/enrollment-tokens/{token_id}/delete",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 404, resp.text
        assert await _token_exists(token_id) is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_delete_cross_workspace_token_404(app_client: AsyncClient) -> None:
    """A member cannot delete a (revoked) token of another workspace: 404."""
    ws_b = await _create_workspace("WS_B_DEL", f"ws-b-del-{uuid.uuid4().hex[:8]}")

    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await mint_enrollment_token(s, workspace_id=ws_b, label="foreign-del")
        await s.commit()

    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = str(
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM worker_enrollment_tokens "
                            "WHERE workspace_id = :w AND label = 'foreign-del'"
                        ),
                        {"w": ws_b},
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()
    await _force_token_state(token_id, revoked=True)

    client, _uid = await _make_member_client_with_id(DEFAULT_WORKSPACE_ID)
    try:
        resp = await client.post(
            f"/admin/workspaces/enrollment-tokens/{token_id}/delete",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 404, resp.text
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 12. Service layer: delete_enrollment_token_by_id only deletes dead states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_service_only_dead_states(db_session) -> None:
    """delete_enrollment_token_by_id borra revoked/expired/exhausted; rechaza active."""
    from orcaster_server.workspaces.enrollment import (
        delete_enrollment_token_by_id,
        mint_enrollment_token,
        revoke_enrollment_token_by_id,
    )
    from orcaster_server.workspaces.service import create_workspace

    ws_id = await create_workspace(db_session, name="WSDel", slug="ws-del-svc")
    await db_session.commit()

    async def _mint(label: str) -> str:
        await mint_enrollment_token(db_session, workspace_id=ws_id, label=label)
        await db_session.commit()
        row = (
            await db_session.execute(
                text(
                    "SELECT id FROM worker_enrollment_tokens WHERE workspace_id = :w AND label = :l"
                ),
                {"w": ws_id, "l": label},
            )
        ).first()
        return str(row[0])

    async def _delete(token_id: str) -> bool:
        return await delete_enrollment_token_by_id(
            db_session, token_id=token_id, workspace_id=ws_id
        )

    # active → not deletable
    active_id = await _mint("svc-active")
    assert await _delete(active_id) is False

    # revoked → deletable
    revoked_id = await _mint("svc-revoked")
    await revoke_enrollment_token_by_id(db_session, token_id=revoked_id, workspace_id=ws_id)
    await db_session.commit()
    assert await _delete(revoked_id) is True
    await db_session.commit()

    # exhausted (not revoked) → deletable (forensic trail in admin_actions).
    # Multi-use model: it is exhausted via `use_count >= max_uses`, not `used_at`.
    used_id = await _mint("svc-used")
    await db_session.execute(
        text(
            "UPDATE worker_enrollment_tokens "
            "SET use_count = max_uses, used_at = now() WHERE id = :id"
        ),
        {"id": used_id},
    )
    await db_session.commit()
    assert await _delete(used_id) is True
    await db_session.commit()

    # expired (never used nor revoked) → deletable
    expired_id = await _mint("svc-expired")
    await db_session.execute(
        text(
            "UPDATE worker_enrollment_tokens "
            "SET expires_at = now() - interval '1 hour' WHERE id = :id"
        ),
        {"id": expired_id},
    )
    await db_session.commit()
    assert await _delete(expired_id) is True


# ---------------------------------------------------------------------------
# 13. Template: Delete button only on revoked / used rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_button_only_for_dead_tokens(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """The Delete action appears for revoked and used tokens, not for an active one.

    Terminal tokens (revoked/used) only appear in the `?show=all` view (the
    default filters to active ones); the active one shows in both and offers
    revoke, never delete. The `used` case is the central behaviour: a used
    token is deletable, so its row must offer the Delete button (the forensic
    trail lives in admin_actions, not in the token).
    """
    revoked_id = await _mint_default_token("tmpl-revoked")
    await _force_token_state(revoked_id, revoked=True)
    used_id = await _mint_default_token("tmpl-used")
    await _force_token_state(used_id, used=True)
    active_id = await _mint_default_token("tmpl-active")

    resp = await authed_admin_client.get(
        "/admin/workspaces?show=all", headers={"HX-Request": "false"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert f"/admin/workspaces/enrollment-tokens/{revoked_id}/delete" in body
    # The used token now offers delete, not revoke.
    assert f"/admin/workspaces/enrollment-tokens/{used_id}/delete" in body
    assert f"/admin/workspaces/enrollment-tokens/{used_id}/revoke" not in body
    # The active token offers revoke, never delete.
    assert f"/admin/workspaces/enrollment-tokens/{active_id}/delete" not in body
    assert f"/admin/workspaces/enrollment-tokens/{active_id}/revoke" in body


@pytest.mark.asyncio
async def test_workspaces_page_active_only_by_default(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """The default view lists only active tokens; `?show=all` lists all of them."""
    active_id = await _mint_default_token("page-active")
    revoked_id = await _mint_default_token("page-revoked")
    await _force_token_state(revoked_id, revoked=True)

    # Default: only the active one appears.
    default = await authed_admin_client.get("/admin/workspaces", headers={"HX-Request": "false"})
    assert default.status_code == 200, default.text
    assert f"/admin/workspaces/enrollment-tokens/{active_id}/" in default.text
    assert f"/admin/workspaces/enrollment-tokens/{revoked_id}/" not in default.text

    # show=all: both appear.
    all_view = await authed_admin_client.get(
        "/admin/workspaces?show=all", headers={"HX-Request": "false"}
    )
    assert all_view.status_code == 200, all_view.text
    assert f"/admin/workspaces/enrollment-tokens/{active_id}/" in all_view.text
    assert f"/admin/workspaces/enrollment-tokens/{revoked_id}/" in all_view.text


# ---------------------------------------------------------------------------
# 13b. Invariant: deleting a `used` token keeps its audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_used_token_keeps_audit_row(
    authed_admin_client: AsyncClient, app_client: AsyncClient
) -> None:
    """Core property: the forensic trail survives deletion.

    Registering a worker consumes and audits the token (admin_actions with
    action='consume_enrollment_token'). Deleting the token in `used` state
    removes the worker_enrollment_tokens row but KEEPS the audit row, because
    `target_id` is Text (not a FK to the token).
    """
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import hash_token
    from orcaster_server.workspaces.enrollment import mint_enrollment_token

    # 1. Mint a token in the admin's workspace (Default).
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token = await mint_enrollment_token(
            s, workspace_id=DEFAULT_WORKSPACE_ID, label="used-keeps-audit"
        )
        await s.commit()

    # 2. Register a worker with that token: it marks it `used` and creates the
    #    consumption audit row. We keep the worker_id from the response.
    resp = await app_client.post(
        "/v1/workers/register",
        json={
            "hostname": "audit-keeper",
            "os": "linux",
            "arch": "x86_64",
            "ram_mb": 1024,
            "disk_mb": 10000,
            "tags": [],
            "capabilities": {},
            "enrollment_token": token,
        },
    )
    assert resp.status_code == 200, resp.text
    worker_id = resp.json()["worker_id"]

    # 3. Capture the token_id BEFORE deleting (after the delete it is no longer
    #    possible to JOIN against worker_enrollment_tokens).
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = str(
                (
                    await conn.execute(
                        text("SELECT id FROM worker_enrollment_tokens WHERE token_hash = :h"),
                        {"h": hash_token(token)},
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()

    # 4. Delete the token (`used` state): 303.
    delete_resp = await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert delete_resp.status_code == 303, delete_resp.text

    # 5. The token no longer exists in worker_enrollment_tokens.
    assert await _token_exists(token_id) is False

    # 6. The consumption audit row SURVIVES (target_id is Text, not a FK).
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT action, payload->>'worker_id' AS pworker "
                        "FROM admin_actions "
                        "WHERE action = 'consume_enrollment_token' AND target_id = :tid"
                    ),
                    {"tid": token_id},
                )
            ).first()
    finally:
        await engine.dispose()
    assert row is not None, "the consumption audit row did not survive the deletion"
    action, pworker = row
    assert action == "consume_enrollment_token"
    assert pworker == worker_id


# ---------------------------------------------------------------------------
# 14. PRG regression: the flash secret is revealed only once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_flash_revealed_once(authed_admin_client: AsyncClient) -> None:
    """After the PRG the token is revealed once; a later GET no longer shows it.

    Regression of the "revoke/F5 creates a new token" bug: the mint redirects
    (303) instead of leaving the page in POST state, and the secret travels in
    a single-use flash cookie that the GET consumes and deletes.
    """
    resp = await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "flash-once"},
        headers={"HX-Request": "false"},
        follow_redirects=True,
    )
    assert resp.status_code == 200, resp.text
    assert "ORCH_ENROLLMENT_TOKEN=" in resp.text

    # Second GET (equivalent to a reload): the cookie was already consumed.
    again = await authed_admin_client.get("/admin/workspaces", headers={"HX-Request": "false"})
    assert again.status_code == 200, again.text
    assert "ORCH_ENROLLMENT_TOKEN=" not in again.text
