"""User menu in the dashboard navbar.

It covers that a full page (extending base.html) renders the account menu
with the logged-in user display_name and the Sign out form pointing
to `POST /auth/logout`. The menu is only shown when authenticated.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_dashboard_renders_user_menu_with_logout(
    authed_admin_client: AsyncClient,
) -> None:
    resp = await authed_admin_client.get("/admin/", headers={"HX-Request": "false"})
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The user menu control and the name of the test user ('Admin').
    assert "user-menu" in body
    assert "Admin" in body
    # Classic logout POST form (no JS) + the Sign out item.
    assert 'action="/auth/logout"' in body
    assert "Sign out" in body
