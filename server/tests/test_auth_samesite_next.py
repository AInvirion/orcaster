"""Tests for SameSite=Lax and preserving next= in the login redirect.

Covers:
  - Unauthenticated HTML GET to /admin/tasks?status=failed → 303 with correct next=.
  - Unauthenticated htmx with HX-Current-URL=/admin/workspaces → HX-Redirect with next=.
  - SameSite=lax in AuthConfig and in the /auth/login/begin challenge cookie.
  - Open redirect security: malicious next= values are rejected (empty data-next).
  - A legitimate next= value is injected safely into the template.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

# --- 1. HTML redirect with query string ------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_html_with_query_preserves_next(app_client: AsyncClient) -> None:
    """HTML GET to /admin/tasks?status=failed → 303 with encoded next=."""
    resp = await app_client.get(
        "/admin/tasks?status=failed",
        headers={"Accept": "text/html", "HX-Request": "false"},
    )
    assert resp.status_code == 303
    # next= must include the path and the query string, both percent-encoded.
    # urllib.parse.quote("/admin/tasks?status=failed", safe="") →
    # %2Fadmin%2Ftasks%3Fstatus%3Dfailed
    assert resp.headers["location"] == "/auth/login?next=%2Fadmin%2Ftasks%3Fstatus%3Dfailed"


# --- 2. htmx with HX-Current-URL ------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_hx_with_current_url_preserves_next(
    app_client: AsyncClient,
) -> None:
    """Unauthenticated htmx with HX-Current-URL pointing to /admin/* → HX-Redirect with next=."""
    resp = await app_client.get(
        "/admin/tasks",
        headers={
            "HX-Request": "true",
            "HX-Current-URL": "http://test/admin/workspaces",
        },
    )
    assert resp.status_code == 401
    # /admin/workspaces percent-encoded → %2Fadmin%2Fworkspaces
    assert resp.headers.get("HX-Redirect") == "/auth/login?next=%2Fadmin%2Fworkspaces"


@pytest.mark.asyncio
async def test_unauthenticated_hx_current_url_non_admin_no_next(
    app_client: AsyncClient,
) -> None:
    """htmx with HX-Current-URL outside /admin → HX-Redirect without next=."""
    resp = await app_client.get(
        "/admin/tasks",
        headers={
            "HX-Request": "true",
            "HX-Current-URL": "http://test/some/other/page",
        },
    )
    assert resp.status_code == 401
    assert resp.headers.get("HX-Redirect") == "/auth/login"


# --- 3. SameSite=lax -------------------------------------------------------


def test_auth_config_default_samesite_is_lax() -> None:
    """AuthConfig with minimal parameters must have cookie_samesite='lax'."""
    from orcaster_server.auth.config import AuthConfig

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test")
    assert cfg.cookie_samesite == "lax"


@pytest.mark.asyncio
async def test_login_begin_challenge_cookie_has_samesite_lax(app_client: AsyncClient) -> None:
    """POST /auth/login/begin emits a challenge cookie with SameSite=Lax."""
    resp = await app_client.post("/auth/login/begin")
    # begin always emits the challenge cookie after generating the options.
    # Assert unconditionally: if it stopped emitting it, this must fail.
    set_cookie = resp.headers.get("set-cookie", "")
    assert set_cookie, "begin must emit a challenge Set-Cookie"
    assert "samesite=lax" in set_cookie.lower()


# --- 4. Open redirect security (login page) --------------------------------


@pytest.mark.asyncio
async def test_open_redirect_double_slash_rejected(app_client: AsyncClient) -> None:
    """next=//evil.com → the template leaves data-next empty."""
    resp = await app_client.get(
        "/auth/login?next=//evil.com",
        headers={"Accept": "text/html", "HX-Request": "false"},
    )
    assert resp.status_code == 200
    assert 'data-next=""' in resp.text


@pytest.mark.asyncio
async def test_open_redirect_absolute_url_rejected(app_client: AsyncClient) -> None:
    """next=https://evil.com → the template leaves data-next empty."""
    resp = await app_client.get(
        "/auth/login?next=https://evil.com",
        headers={"Accept": "text/html", "HX-Request": "false"},
    )
    assert resp.status_code == 200
    assert 'data-next=""' in resp.text


@pytest.mark.asyncio
async def test_open_redirect_non_admin_path_rejected(app_client: AsyncClient) -> None:
    """next=/etc/passwd (does not start with /admin) → empty data-next."""
    resp = await app_client.get(
        "/auth/login?next=/etc/passwd",
        headers={"Accept": "text/html", "HX-Request": "false"},
    )
    assert resp.status_code == 200
    assert 'data-next=""' in resp.text


@pytest.mark.asyncio
async def test_next_missing_renders_null(app_client: AsyncClient) -> None:
    """No ?next= → empty data-next (does not try to redirect anywhere)."""
    resp = await app_client.get(
        "/auth/login",
        headers={"Accept": "text/html", "HX-Request": "false"},
    )
    assert resp.status_code == 200
    assert 'data-next=""' in resp.text


@pytest.mark.asyncio
async def test_valid_admin_next_rendered_correctly(app_client: AsyncClient) -> None:
    """next=/admin/tasks → the template renders data-next="/admin/tasks" (escaped by Jinja)."""
    resp = await app_client.get(
        "/auth/login?next=/admin/tasks",
        headers={"Accept": "text/html", "HX-Request": "false"},
    )
    assert resp.status_code == 200
    assert 'data-next="/admin/tasks"' in resp.text
    # Verify that null does NOT appear for a valid value
    assert 'data-next=""' not in resp.text
