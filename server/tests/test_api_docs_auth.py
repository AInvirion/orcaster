"""Tests for `/openapi.json`, `/docs` and `/redoc` behind auth.

JSON: Bearer PAT or session cookie, clean 401 without credentials. HTML: session
cookie only, 303 to the login page without it. `API_DOCS_PUBLIC=true` reopens them.
Also covers credential selection by `Authorization` scheme, HEAD,
`Cache-Control: no-store` and the invalidation of sessions and PATs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from orcaster_server.config import Settings, settings
from tests.conftest import mint_pat_via_app

_HTML_ROUTES = ["/docs", "/redoc"]
_ALL_ROUTES = ["/openapi.json", "/docs", "/redoc"]
# Expected response of each route without valid credentials.
_DENIED_STATUS = {"/openapi.json": 401, "/docs": 303, "/redoc": 303}


async def _session_cookie() -> str:
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    async with app_db.get_sessionmaker()() as s:
        row = await s.execute(text("INSERT INTO users (display_name) VALUES ('Docs') RETURNING id"))
        token = await create_session(
            s, cfg, user_id=str(row.scalar_one()), user_agent=None, ip=None
        )
        await s.commit()
    return token


def _cookie_client(app_client: AsyncClient, cookie: str) -> AsyncClient:
    """Client over the same app as `app_client`, with the given session cookie."""
    return AsyncClient(
        transport=ASGITransport(app=app_client._transport.app),  # type: ignore[attr-defined]
        base_url="http://test",
        cookies={"session": cookie},
    )


async def _execute(statement: str, **params: object) -> None:
    """Run a direct UPDATE/DELETE on the app's live engine."""
    from orcaster_server import db as app_db

    async with app_db.get_sessionmaker()() as s:
        await s.execute(text(statement), params)
        await s.commit()


@pytest.mark.asyncio
async def test_openapi_anonymous_is_401_json(app_client: AsyncClient) -> None:
    r = await app_client.get("/openapi.json")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _HTML_ROUTES)
async def test_html_docs_anonymous_redirect_to_login(app_client: AsyncClient, path: str) -> None:
    r = await app_client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"


@pytest.mark.asyncio
async def test_openapi_with_pat(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    r = await app_client.get("/openapi.json", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "paths" in r.json()


@pytest.mark.asyncio
async def test_openapi_with_invalid_pat_is_401(app_client: AsyncClient) -> None:
    r = await app_client.get("/openapi.json", headers={"Authorization": "Bearer orcaster_pat_nope"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_openapi_disabled_user_pat_is_401_until_reenabled(app_client: AsyncClient) -> None:
    """A disabled user's PAT does not open /openapi.json; after re-enabling it does."""
    token, uid = await mint_pat_via_app()
    headers = {"Authorization": f"Bearer {token}"}
    await _execute("UPDATE users SET disabled_at = now() WHERE id = :u", u=uid)
    assert (await app_client.get("/openapi.json", headers=headers)).status_code == 401
    await _execute("UPDATE users SET disabled_at = NULL WHERE id = :u", u=uid)
    assert (await app_client.get("/openapi.json", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_pat_does_not_open_html_docs(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    r = await app_client.get(
        "/docs", headers={"Authorization": f"Bearer {token}"}, follow_redirects=False
    )
    assert r.status_code == 303


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
async def test_session_cookie_opens_all_three(app_client: AsyncClient, path: str) -> None:
    cookie = await _session_cookie()
    async with _cookie_client(app_client, cookie) as c:
        r = await c.get(path, follow_redirects=False)
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
async def test_public_setting_reopens_docs(
    app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(settings, "api_docs_public", True)
    r = await app_client.get(path)
    assert r.status_code == 200


# --- Credential selection in /openapi.json (Authorization scheme) ----


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["Basic dXNlcjpwYXNz", ""], ids=["basic", "empty"])
async def test_openapi_non_bearer_authorization_falls_back_to_session(
    app_client: AsyncClient, authorization: str
) -> None:
    """Only `Bearer ` triggers PAT validation; Basic or empty fall back to the cookie.

    A proxy or edge layer may inject `Authorization: Basic`; the browser's valid
    session must not be rejected because of it.
    """
    cookie = await _session_cookie()
    async with _cookie_client(app_client, cookie) as c:
        r = await c.get("/openapi.json", headers={"Authorization": authorization})
    assert r.status_code == 200
    assert "paths" in r.json()


@pytest.mark.asyncio
async def test_openapi_invalid_bearer_never_falls_back_to_session_cookie(
    app_client: AsyncClient,
) -> None:
    """Policy: a `Bearer ` header declares a PAT; if invalid, the cookie is not used."""
    cookie = await _session_cookie()
    async with _cookie_client(app_client, cookie) as c:
        r = await c.get("/openapi.json", headers={"Authorization": "Bearer x"})
    assert r.status_code == 401


# --- HEAD (FastAPI served it by default) ------------------------------------


@pytest.mark.asyncio
async def test_head_openapi_with_pat(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    r = await app_client.head("/openapi.json", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _HTML_ROUTES)
async def test_head_html_docs_with_session(app_client: AsyncClient, path: str) -> None:
    cookie = await _session_cookie()
    async with _cookie_client(app_client, cookie) as c:
        r = await c.head(path, follow_redirects=False)
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _ALL_ROUTES)
async def test_head_anonymous_is_denied(app_client: AsyncClient, path: str) -> None:
    r = await app_client.head(path, follow_redirects=False)
    assert r.status_code == _DENIED_STATUS[path]


# --- Cache-Control: no-store on 200, 303 and 401 -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _ALL_ROUTES)
async def test_no_store_on_authenticated_200(app_client: AsyncClient, path: str) -> None:
    cookie = await _session_cookie()
    async with _cookie_client(app_client, cookie) as c:
        r = await c.get(path, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_no_store_on_openapi_200_with_pat(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app()
    r = await app_client.get("/openapi.json", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _HTML_ROUTES)
async def test_no_store_on_html_login_redirect_303(app_client: AsyncClient, path: str) -> None:
    r = await app_client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_no_store_on_openapi_401(app_client: AsyncClient) -> None:
    anonymous = await app_client.get("/openapi.json")
    bad_pat = await app_client.get("/openapi.json", headers={"Authorization": "Bearer nope"})
    assert anonymous.status_code == bad_pat.status_code == 401
    assert anonymous.headers["cache-control"] == "no-store"
    assert bad_pat.headers["cache-control"] == "no-store"


# --- Invalidated credentials ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["revoked", "expired"])
@pytest.mark.parametrize("path", _ALL_ROUTES)
async def test_invalidated_session_is_denied(app_client: AsyncClient, path: str, how: str) -> None:
    from orcaster_server.auth.config import hash_token

    cookie = await _session_cookie()
    if how == "revoked":
        await _execute(
            "UPDATE sessions SET revoked_at = now() WHERE id = :sid", sid=hash_token(cookie)
        )
    else:
        await _execute(
            "UPDATE sessions SET expires_at = now() - interval '1 hour' WHERE id = :sid",
            sid=hash_token(cookie),
        )
    async with _cookie_client(app_client, cookie) as c:
        r = await c.get(path, follow_redirects=False)
    assert r.status_code == _DENIED_STATUS[path]
    assert "paths" not in r.text


@pytest.mark.asyncio
async def test_openapi_revoked_pat_is_401(app_client: AsyncClient) -> None:
    token, user_id = await mint_pat_via_app()
    await _execute(
        "UPDATE personal_access_tokens SET revoked_at = now() WHERE user_id = :u", u=user_id
    )
    r = await app_client.get("/openapi.json", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "paths" not in r.json()


@pytest.mark.asyncio
async def test_openapi_expired_pat_is_401(app_client: AsyncClient) -> None:
    token, _ = await mint_pat_via_app(expires_at=datetime.now(UTC) - timedelta(hours=1))
    r = await app_client.get("/openapi.json", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "paths" not in r.json()


@pytest.mark.asyncio
async def test_openapi_pat_of_removed_workspace_member_is_403(app_client: AsyncClient) -> None:
    """Valid PAT whose owner left the workspace: 403 (not 401), as in `/api/v1`.

    Deliberate asymmetry: a session without membership does open `/openapi.json`
    (`require_session` does not check memberships); the PAT does require it.
    """
    token, user_id = await mint_pat_via_app()
    await _execute("DELETE FROM workspace_members WHERE user_id = :u", u=user_id)
    r = await app_client.get("/openapi.json", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert "paths" not in r.json()


# --- Default value -------------------------------------------------------


def test_api_docs_public_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `API_DOCS_PUBLIC` (or `.env`) the documentation stays closed."""
    monkeypatch.delenv("API_DOCS_PUBLIC", raising=False)
    assert Settings(_env_file=None).api_docs_public is False  # type: ignore[call-arg]


# --- Case-insensitive `Bearer` scheme (RFC 7235 §2.1) ----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    ["bearer {token}", "BEARER {token}", "bEaReR {token}", "Bearer  {token}"],
    ids=["lower", "upper", "mixed", "two-spaces"],
)
async def test_openapi_accepts_the_bearer_scheme_in_any_case(
    app_client: AsyncClient, authorization: str
) -> None:
    """Credential selection uses the same parser as `/api/v1`.

    Without a session cookie: if the header were not recognised as a PAT, the
    session would be validated and the response would be 401.
    """
    token, _ = await mint_pat_via_app()
    r = await app_client.get(
        "/openapi.json", headers={"Authorization": authorization.format(token=token)}
    )
    assert r.status_code == 200, r.text
    assert "paths" in r.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["bearer x", "BEARER x"], ids=["lower", "upper"])
async def test_openapi_invalid_bearer_in_any_case_never_falls_back_to_session(
    app_client: AsyncClient, authorization: str
) -> None:
    """The scheme declared in any letter case declares a PAT: invalid is 401 even with a cookie."""
    cookie = await _session_cookie()
    async with _cookie_client(app_client, cookie) as c:
        r = await c.get("/openapi.json", headers={"Authorization": authorization})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    ["Bearerx {token}", "xBearer {token}", "Bearer{token}"],
    ids=["bearerx", "xbearer", "no-separator"],
)
async def test_openapi_near_miss_scheme_falls_back_to_session(
    app_client: AsyncClient, authorization: str
) -> None:
    """A scheme that merely resembles `bearer` is not a PAT: the cookie is validated."""
    cookie = await _session_cookie()
    token, _ = await mint_pat_via_app()
    async with _cookie_client(app_client, cookie) as c:
        r = await c.get(
            "/openapi.json", headers={"Authorization": authorization.format(token=token)}
        )
    assert r.status_code == 200, r.text
    assert "paths" in r.json()
