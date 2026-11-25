"""Tests — Personal Access Tokens UI in the dashboard +
closing the audit gap for enrollment tokens (partial supersession of the
previous enrollment policy).

Covers:
  - `/admin/pats` page: render, empty state.
  - Self-service mint (any member), PRG + flash that reveals the secret once.
  - Optional expiry: dropdown No expiration / 30 / 60 / 90 days.
  - Role asymmetry (mirrors enrollment): a member sees/revokes their own; an owner
    sees/revokes the whole workspace. Revoking someone else's token (member) or
    cross-tenant -> 404.
  - Delete only for terminal PATs (revoked/expired); active -> 404.
  - Audit: mint/revoke/delete of PATs and enrollment tokens write to
    `admin_actions`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import browser_origin_hook


def _db_url() -> str:
    from orcaster_server.config import settings

    return settings.database_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_member_client_with_id(
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    *,
    display_name: str = "MemberPAT",
) -> tuple[AsyncClient, str]:
    """AsyncClient authenticated as `member`; returns (client, user_id)."""
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.main import app
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"),
            {"n": display_name},
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


async def _pat_id_by_label(label: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> str:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            return str(
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM personal_access_tokens "
                            "WHERE workspace_id = :w AND label = :l "
                            "ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"w": workspace_id, "l": label},
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


async def _pat_row(pat_id: str) -> dict:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, revoked_at, expires_at "
                            "FROM personal_access_tokens WHERE id = :i"
                        ),
                        {"i": pat_id},
                    )
                )
                .mappings()
                .one()
            )
            return dict(row)
    finally:
        await engine.dispose()


async def _audit_rows(action: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> list[dict]:
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT action, target_kind, target_id, actor, workspace_id "
                            "FROM admin_actions "
                            "WHERE action = :a AND workspace_id = :w"
                        ),
                        {"a": action, "w": workspace_id},
                    )
                )
                .mappings()
                .all()
            )
            return [dict(r) for r in rows]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Page and empty state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pats_page_renders_empty(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/pats", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    assert "Personal access tokens" in resp.text
    assert "<code>orcaster</code> CLI" in resp.text


# === Timezone note gated by data (no id/hx-preserve) ========================
# {% include "_tz_note.html" %} in pats.html is gated by `{% if pats %}`
# (the table does not live behind any polling, so it carries no tz_note_id).
# Without this test nobody would notice if the include moved outside the gate (orphan
# note with an empty table) or disappeared when minting a PAT.


@pytest.mark.asyncio
async def test_pats_page_empty_omits_tz_note(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.get("/admin/pats", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    assert "js-tz-note" not in resp.text


@pytest.mark.asyncio
async def test_pats_page_with_pat_includes_tz_note(authed_admin_client: AsyncClient) -> None:
    mint = await authed_admin_client.post(
        "/admin/pats",
        data={"label": "tz-note-pat"},
        headers={"HX-Request": "false"},
        follow_redirects=True,
    )
    assert mint.status_code == 200, mint.text
    assert "js-tz-note" in mint.text


@pytest.mark.asyncio
async def test_pats_page_requires_auth(app_client: AsyncClient) -> None:
    """HTML navigation without a session redirects to login (303), like the rest of /admin."""
    resp = await app_client.get(
        "/admin/pats",
        headers={"HX-Request": "false", "Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/auth/login?next=%2Fadmin%2Fpats"


# ---------------------------------------------------------------------------
# Mint + PRG + reveal-once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_pat_prg_reveals_secret_once(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/pats",
        data={"label": "my-laptop"},
        headers={"HX-Request": "false"},
        follow_redirects=True,
    )
    assert resp.status_code == 200, resp.text
    assert "orcaster_pat_" in resp.text
    assert "<code>orcaster auth login</code>" in resp.text

    again = await authed_admin_client.get("/admin/pats", headers={"HX-Request": "false"})
    assert again.status_code == 200, again.text
    assert "orcaster_pat_" not in again.text


@pytest.mark.asyncio
async def test_mint_pat_no_expiration(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/pats",
        data={"label": "no-exp", "expires_in_days": ""},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    row = await _pat_row(await _pat_id_by_label("no-exp"))
    assert row["expires_at"] is None


@pytest.mark.asyncio
async def test_mint_pat_with_expiration_days(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/pats",
        data={"label": "exp-30", "expires_in_days": "30"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    row = await _pat_row(await _pat_id_by_label("exp-30"))
    assert row["expires_at"] is not None
    delta = row["expires_at"] - datetime.now(UTC)
    assert 29 <= delta.days <= 30


@pytest.mark.asyncio
async def test_mint_pat_invalid_expiration_rejected(authed_admin_client: AsyncClient) -> None:
    """An expiry value outside the allowed set -> 400 (not minted)."""
    resp = await authed_admin_client.post(
        "/admin/pats",
        data={"label": "bad-exp", "expires_in_days": "9999"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Role asymmetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_sees_only_own_pats(authed_admin_client: AsyncClient) -> None:
    # Owner mints one.
    await authed_admin_client.post(
        "/admin/pats",
        data={"label": "owner-pat"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    # Member mints their own and only sees their own.
    client, _uid = await _make_member_client_with_id()
    try:
        await client.post(
            "/admin/pats",
            data={"label": "member-pat"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        page = await client.get("/admin/pats", headers={"HX-Request": "false"})
        assert page.status_code == 200, page.text
        assert "member-pat" in page.text
        assert "owner-pat" not in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_owner_sees_all_pats(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/pats",
        data={"label": "owner-pat"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    client, _uid = await _make_member_client_with_id()
    try:
        await client.post(
            "/admin/pats",
            data={"label": "member-pat"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
    finally:
        await client.aclose()
    page = await authed_admin_client.get("/admin/pats", headers={"HX-Request": "false"})
    assert page.status_code == 200, page.text
    assert "owner-pat" in page.text
    assert "member-pat" in page.text


@pytest.mark.asyncio
async def test_member_revoke_foreign_pat_404(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/pats",
        data={"label": "owner-unrevokable"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    pat_id = await _pat_id_by_label("owner-unrevokable")
    client, _uid = await _make_member_client_with_id()
    try:
        resp = await client.post(
            f"/admin/pats/{pat_id}/revoke",
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 404, resp.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_owner_revokes_any_pat(authed_admin_client: AsyncClient) -> None:
    client, _uid = await _make_member_client_with_id()
    try:
        await client.post(
            "/admin/pats",
            data={"label": "member-revokable-by-owner"},
            headers={"HX-Request": "false"},
            follow_redirects=False,
        )
    finally:
        await client.aclose()
    pat_id = await _pat_id_by_label("member-revokable-by-owner")
    resp = await authed_admin_client.post(
        f"/admin/pats/{pat_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert (await _pat_row(pat_id))["revoked_at"] is not None


@pytest.mark.asyncio
async def test_revoke_malformed_uuid_404(authed_admin_client: AsyncClient) -> None:
    resp = await authed_admin_client.post(
        "/admin/pats/not-a-uuid/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_revoke_urn_uuid_404(authed_admin_client: AsyncClient) -> None:
    """`urn:uuid:<hex>` passes Python's `UUID()` but NOT the Postgres CAST:
    without normalizing the path param, the endpoint returned 500 (DataError)."""
    resp = await authed_admin_client.post(
        "/admin/pats/urn:uuid:00000000-0000-0000-0000-000000000000/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cross_tenant_revoke_404(authed_admin_client: AsyncClient) -> None:
    """A PAT from another workspace never matches -> 404 (workspace isolation)."""
    ws_b = await _create_workspace("WS-B", "ws-b")
    from orcaster_server import db as app_db
    from orcaster_server.pats.service import mint_pat
    from orcaster_server.workspaces.service import add_member

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES ('Foreign') RETURNING id")
        )
        uid = str(row.scalar_one())
        await add_member(s, workspace_id=ws_b, user_id=uid, role="owner")
        await mint_pat(s, user_id=uid, workspace_id=ws_b, label="foreign-pat")
        await s.commit()
    pat_id = await _pat_id_by_label("foreign-pat", workspace_id=ws_b)

    resp = await authed_admin_client.post(
        f"/admin/pats/{pat_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Delete (terminal only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_active_pat_404(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/pats",
        data={"label": "active-undeletable"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    pat_id = await _pat_id_by_label("active-undeletable")
    resp = await authed_admin_client.post(
        f"/admin/pats/{pat_id}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_revoke_then_delete(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/pats",
        data={"label": "to-revoke-then-delete"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    pat_id = await _pat_id_by_label("to-revoke-then-delete")
    await authed_admin_client.post(
        f"/admin/pats/{pat_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    # The list now offers delete (not revoke) for the revoked token.
    page = await authed_admin_client.get("/admin/pats", headers={"HX-Request": "false"})
    assert f"/admin/pats/{pat_id}/delete" in page.text
    resp = await authed_admin_client.post(
        f"/admin/pats/{pat_id}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest.mark.asyncio
async def test_delete_urn_uuid_404(authed_admin_client: AsyncClient) -> None:
    """`urn:uuid:<hex>` passes Python's `UUID()` but NOT the Postgres CAST:
    without normalizing the path param, the endpoint returned 500 (DataError)."""
    resp = await authed_admin_client.post(
        "/admin/pats/urn:uuid:00000000-0000-0000-0000-000000000000/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Audit — PATs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_pat_audited(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/pats",
        data={"label": "audited-mint"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    pat_id = await _pat_id_by_label("audited-mint")
    rows = await _audit_rows("mint_pat")
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "pat"
    assert rows[0]["target_id"] == pat_id
    assert rows[0]["actor"] is not None


@pytest.mark.asyncio
async def test_revoke_pat_audited(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/pats",
        data={"label": "audited-revoke"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    pat_id = await _pat_id_by_label("audited-revoke")
    await authed_admin_client.post(
        f"/admin/pats/{pat_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    rows = await _audit_rows("revoke_pat")
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "pat"
    assert rows[0]["target_id"] == pat_id


@pytest.mark.asyncio
async def test_delete_pat_audited(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/pats",
        data={"label": "audited-delete"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    pat_id = await _pat_id_by_label("audited-delete")
    await authed_admin_client.post(
        f"/admin/pats/{pat_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    await authed_admin_client.post(
        f"/admin/pats/{pat_id}/delete",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    rows = await _audit_rows("delete_pat")
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "pat"
    assert rows[0]["target_id"] == pat_id


# ---------------------------------------------------------------------------
# Audit — enrollment tokens (closing the audit gap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_enrollment_token_audited(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "audited-enroll-mint"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    rows = await _audit_rows("mint_enrollment_token")
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "enrollment_token"
    assert rows[0]["actor"] is not None
    assert rows[0]["target_id"]  # id of the minted row


@pytest.mark.asyncio
async def test_audit_log_offers_credential_filters(authed_admin_client: AsyncClient) -> None:
    """The Audit log exposes credential actions as filter options.

    Without this, the mint/revoke/delete rows for PAT and enrollment render
    but cannot be filtered (the selectors are filled from a hardcoded list).
    """
    page = await authed_admin_client.get("/admin/actions", headers={"HX-Request": "false"})
    assert page.status_code == 200, page.text
    for value in (
        "mint_pat",
        "revoke_pat",
        "delete_pat",
        "mint_enrollment_token",
        "revoke_enrollment_token",
        "delete_enrollment_token",
    ):
        assert f'value="{value}"' in page.text, value
    assert 'value="pat"' in page.text
    assert 'value="enrollment_token"' in page.text


@pytest.mark.asyncio
async def test_revoke_enrollment_token_audited(authed_admin_client: AsyncClient) -> None:
    await authed_admin_client.post(
        "/admin/workspaces/enrollment-tokens",
        data={"label": "audited-enroll-revoke"},
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    engine = create_async_engine(_db_url())
    try:
        async with engine.connect() as conn:
            token_id = str(
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM worker_enrollment_tokens "
                            "WHERE workspace_id = :w AND label = 'audited-enroll-revoke'"
                        ),
                        {"w": DEFAULT_WORKSPACE_ID},
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()
    await authed_admin_client.post(
        f"/admin/workspaces/enrollment-tokens/{token_id}/revoke",
        headers={"HX-Request": "false"},
        follow_redirects=False,
    )
    rows = await _audit_rows("revoke_enrollment_token")
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "enrollment_token"
    assert rows[0]["target_id"] == token_id
