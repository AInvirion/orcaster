import pytest


@pytest.mark.asyncio
async def test_login_page_renders(app_client):
    res = await app_client.get("/auth/login", headers={"HX-Request": "false"})
    assert res.status_code == 200
    assert "Sign in with passkey" in res.text


@pytest.mark.asyncio
async def test_login_begin_sets_challenge_cookie(app_client):
    res = await app_client.post("/auth/login/begin")
    assert res.status_code == 200
    assert "challenge" in res.json()
    assert "webauthn_challenge" in res.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_accept_unknown_token_returns_410(app_client):
    res = await app_client.get("/auth/accept/does-not-exist", headers={"HX-Request": "false"})
    assert res.status_code == 410
    assert "no longer valid" in res.text


@pytest.mark.asyncio
async def test_logout_redirects_and_clears_cookie(app_client):
    # Classic form POST: logout redirects to the login page (no JS) and
    # clears the session cookie in the same response.
    res = await app_client.post("/auth/logout", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/auth/login"
    set_cookie = res.headers.get("set-cookie", "")
    assert "session=" in set_cookie
    assert ("Max-Age=0" in set_cookie) or ("expires=" in set_cookie.lower())
