"""`/openapi.json`, `/docs` and `/redoc` behind auth.

FastAPI serves them anonymously by default; `orcaster_server/main.py` disables that
and they are registered here with the same credentials as the rest of the service:

  - `/openapi.json`: Bearer PAT or session cookie; without either, 401. The
    credential is chosen by the `Authorization` header: only if it declares the
    Bearer scheme is it validated as a PAT, and in that case it NEVER falls back to
    the cookie (an invalid PAT is a 401 even if there is a valid session). That is
    decided by `orcaster_server/pats/deps.py::has_bearer_scheme`, the same parser
    that extracts the token in `/api/v1`, so the scheme name is case-insensitive
    (RFC 7235 section 2.1). Any other value (absent, empty, `Basic`, other schemes)
    is ignored and the session cookie is validated.
  - `/docs` and `/redoc`: session cookie only (a PAT does not open browser pages);
    without it, 303 to `/auth/login`.

All three routes accept GET and HEAD (like FastAPI's defaults) and all their
responses (200, 303, 401, 403) carry `Cache-Control: no-store`: they are
authenticated, and a shared proxy must not serve them again to an anonymous client.

`API_DOCS_PUBLIC=true` (local development only) leaves them open.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, RedirectResponse, Response

from orcaster_server.config import settings
from orcaster_server.db import get_session
from orcaster_server.deps import require_session
from orcaster_server.pats.deps import has_bearer_scheme, require_pat_workspace

_LOGIN_URL = "/auth/login"
_DOCS_PATHS = frozenset({"/openapi.json", "/docs", "/redoc"})
_METHODS = ["GET", "HEAD"]
_DbSession = Annotated[AsyncSession, Depends(get_session)]


def install_api_docs(app: FastAPI) -> None:
    """Register the three routes on `app` (created with `docs_url=None`, etc.)."""

    # Mirror of `_admin_cache_control` (orcaster_server/main.py): it injects `no-store`
    # only if the handler did not set its own `Cache-Control`. Being registered here
    # (before the other middlewares) it ends up as the innermost one; the others are
    # scoped to `/admin`, `/auth` and `/status`, so they do not overlap.
    @app.middleware("http")
    async def _docs_cache_control(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in _DOCS_PATHS and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.api_route("/openapi.json", methods=_METHODS, include_in_schema=False)
    async def openapi_json(request: Request, db: _DbSession) -> JSONResponse:
        if not settings.api_docs_public:
            if has_bearer_scheme(request):
                await require_pat_workspace(request, db)
            else:
                await require_session(request, db)
        return JSONResponse(app.openapi())

    async def _session_or_login(request: Request, db: AsyncSession) -> RedirectResponse | None:
        if settings.api_docs_public:
            return None
        try:
            await require_session(request, db)
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                return RedirectResponse(url=_LOGIN_URL, status_code=303)
            raise
        return None

    @app.api_route("/docs", methods=_METHODS, include_in_schema=False)
    async def swagger_ui(request: Request, db: _DbSession) -> Response:
        denied = await _session_or_login(request, db)
        if denied is not None:
            return denied
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")

    @app.api_route("/redoc", methods=_METHODS, include_in_schema=False)
    async def redoc(request: Request, db: _DbSession) -> Response:
        denied = await _session_or_login(request, db)
        if denied is not None:
            return denied
        return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")
