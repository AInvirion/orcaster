"""Security headers on the `/admin` and `/auth` planes.

`PlaneSecurityHeadersMiddleware` (`orcaster_server/main.py`) stamps
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: same-origin` and a CSP without `'unsafe-inline'` on every response from
`/admin` and `/auth` (including redirects and errors), without touching other planes or
overwriting a header the response already carries. In addition, a scan test
fails if a template of those planes reintroduces inline code or style.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.responses import PlainTextResponse

from orcaster_server.main import _PLANE_CSP, _STATUS_CSP, PlaneSecurityHeadersMiddleware

EXPECTED = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "content-security-policy": _PLANE_CSP,
}


def _assert_plane_headers(r: httpx.Response) -> None:
    for name, value in EXPECTED.items():
        assert r.headers.get(name) == value, (name, r.status_code, dict(r.headers))


@pytest.mark.asyncio
async def test_admin_page_carries_the_headers(authed_admin_client: AsyncClient) -> None:
    r = await authed_admin_client.get("/admin/tasks")
    assert r.status_code == 200, r.text
    _assert_plane_headers(r)


@pytest.mark.asyncio
async def test_admin_redirect_without_session_carries_the_headers(
    app_client: AsyncClient,
) -> None:
    r = await app_client.get("/admin/", headers={"accept": "text/html", "HX-Request": "false"})
    assert r.status_code == 303
    _assert_plane_headers(r)


@pytest.mark.asyncio
async def test_admin_401_carries_the_headers(app_client: AsyncClient) -> None:
    r = await app_client.get("/admin/tasks", headers={"accept": "application/json"})
    assert r.status_code == 401
    _assert_plane_headers(r)


@pytest.mark.asyncio
async def test_auth_login_page_carries_the_headers(app_client: AsyncClient) -> None:
    r = await app_client.get("/auth/login")
    assert r.status_code == 200, r.text
    _assert_plane_headers(r)


@pytest.mark.asyncio
async def test_auth_unknown_route_carries_the_headers(app_client: AsyncClient) -> None:
    r = await app_client.get("/auth/no-such-route")
    assert r.status_code == 404
    _assert_plane_headers(r)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/healthz", "/static/auth/webauthn.js", "/admin-x", "/authx", "/api/v1/tasks"],
)
async def test_other_planes_are_not_touched(app_client: AsyncClient, path: str) -> None:
    r = await app_client.get(path)
    assert "x-content-type-options" not in r.headers, path
    assert "x-frame-options" not in r.headers, path
    assert "content-security-policy" not in r.headers, path


@pytest.mark.asyncio
async def test_status_plane_keeps_its_own_csp(app_client: AsyncClient) -> None:
    r = await app_client.get("/status/no-such-token")
    assert r.headers["content-security-policy"] == _STATUS_CSP
    assert "x-content-type-options" not in r.headers


@pytest.mark.asyncio
async def test_existing_header_is_not_overwritten() -> None:
    mini = FastAPI()

    @mini.get("/admin/own")
    async def own() -> PlainTextResponse:
        return PlainTextResponse(
            "x", headers={"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"}
        )

    mini.add_middleware(PlaneSecurityHeadersMiddleware)
    async with AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://t") as c:
        r = await c.get("/admin/own")
    assert r.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'none'"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_csp_closes_script_and_style_sources() -> None:
    directives = dict(d.strip().split(" ", 1) for d in _PLANE_CSP.split(";"))
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert "unsafe-inline" not in _PLANE_CSP
    assert "unsafe-eval" not in _PLANE_CSP
    assert "nonce-" not in _PLANE_CSP
    for name, value in {
        "default-src": "'self'",
        "img-src": "'self' data:",
        "connect-src": "'self'",
        "form-action": "'self'",
        "base-uri": "'none'",
        "object-src": "'none'",
        "frame-ancestors": "'none'",
    }.items():
        assert directives[name] == value, name


@pytest.mark.asyncio
async def test_admin_403_and_405_carry_the_headers(authed_admin_client: AsyncClient) -> None:
    r = await authed_admin_client.get("/admin/tasks/does-not-exist-route/x")
    assert r.status_code == 404
    _assert_plane_headers(r)
    r = await authed_admin_client.put("/admin/tasks")
    assert r.status_code == 405
    _assert_plane_headers(r)


@pytest.mark.asyncio
async def test_response_referrer_policy_is_kept_when_set() -> None:
    mini = FastAPI()

    @mini.get("/auth/own")
    async def own() -> PlainTextResponse:
        return PlainTextResponse("x", headers={"Referrer-Policy": "no-referrer"})

    mini.add_middleware(PlaneSecurityHeadersMiddleware)
    async with AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://t") as c:
        r = await c.get("/auth/own")
    assert r.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/tasks", "/healthz", "/static/app.js"])
async def test_other_planes_get_no_referrer_policy(app_client: AsyncClient, path: str) -> None:
    r = await app_client.get(path)
    assert "referrer-policy" not in r.headers, path


# --- Template scan: nothing inline in /admin or /auth -----------------------

_PKG = Path(__file__).resolve().parent.parent / "orcaster_server"
# `status*.html` and the partials used only by the status page have their own CSP.
_STATUS_ONLY = {"status.html", "status_404.html", "_status_summary.html", "_variant_options.html"}
_PLANE_TEMPLATES = sorted(
    p
    for p in [*(_PKG / "templates").glob("*.html"), *(_PKG / "auth" / "templates").glob("*.html")]
    if p.name not in _STATUS_ONLY
)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
# `_progress_hero.html` shares a template with the status page, which does allow
# `style=`: that block is only emitted with `inline_pct` (never in /admin).
_STATUS_ONLY_BLOCK = re.compile(r"\{%-?\s*if inline_pct\s*-?%\}.*?\{%-?\s*endif\s*-?%\}", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FORBIDDEN = {
    "<script> inline": re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE),
    "<style>": re.compile(r"<style\b", re.IGNORECASE),
    "on* attribute": re.compile(r"\son[a-z]+\s*=", re.IGNORECASE),
    "style= attribute": re.compile(r"\sstyle\s*=", re.IGNORECASE),
    "hx-on": re.compile(r"\shx-on[:\-]", re.IGNORECASE),
    "javascript: URL": re.compile(r"javascript:", re.IGNORECASE),
    "js: in hx-vals/hx-headers": re.compile(
        r"hx-(?:vals|headers)\s*=\s*[\"']\s*js:", re.IGNORECASE
    ),
    "hx-trigger filter (eval)": re.compile(r"hx-trigger\s*=\s*\"[^\"]*\[", re.IGNORECASE),
}


def test_template_scan_finds_the_templates() -> None:
    names = {p.name for p in _PLANE_TEMPLATES}
    assert {"base.html", "login.html", "accept_invite.html", "dashboard.html"} <= names


@pytest.mark.parametrize("path", _PLANE_TEMPLATES, ids=lambda p: p.name)
def test_plane_templates_have_nothing_inline(path: Path) -> None:
    text = _STATUS_ONLY_BLOCK.sub(
        "", _HTML_COMMENT.sub("", _JINJA_COMMENT.sub("", path.read_text()))
    )
    for label, rx in _FORBIDDEN.items():
        m = rx.search(text)
        assert m is None, f"{path.name}: {label} reintroduced ({m.group(0)!r})"
