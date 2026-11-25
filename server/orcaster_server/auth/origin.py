"""Origin check for requests authenticated by session cookie (CSRF defence).

The session cookie is `SameSite=Lax`: a form POST from a sibling site
(another subdomain of the same registrable domain) carries it. Every request
that changes state and authenticates with that cookie also compares its origin
with its own: `orcaster_server.deps.require_session` for `/admin/*` and the rest
of the host, and `AuthOriginCheckMiddleware` for the auth library routes.
"""

from __future__ import annotations

from fastapi import Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def same_origin(request: Request, expected_origin: str) -> bool:
    """True if the POST comes from a page of the same origin.

    Looks at `Origin` first; without it, falls back to `Sec-Fetch-Site` and accepts
    only `same-origin` or `none` (direct user navigation). With neither of the
    two headers there is no way to tell the origin and the request is rejected.

    `Origin` is compared by exact equality: `expected_origin` must arrive
    already canonical. The host guarantees this when building `Settings`
    (`orcaster_server/config.py::canonical_origin`), which lowercases scheme and
    host, drops the default port and the trailing slash, and fails startup
    on a value with path, query, fragment, userinfo or another scheme. Nothing
    is fixed here: an `rstrip("/")` covered only one of the non-canonical forms and
    made it seem that the comparison is tolerant when it is not.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        return origin == expected_origin
    return request.headers.get("sec-fetch-site") in ("same-origin", "none")


# Methods that change state: only these are checked.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Message of the 403. Shared by the `orcaster_server.deps.require_session` dependency and the
# `/auth/*` middleware. On `/admin/*` the exception handler of `orcaster_server/main.py`
# may rewrite the 403 (HTMX/HTML); the middleware always responds with JSON.
CROSS_ORIGIN_DETAIL = "cross-origin request refused"


def _cookie_names(headers: Headers) -> set[str]:
    names: set[str] = set()
    for raw in headers.getlist("cookie"):
        for part in raw.split(";"):
            name, sep, _ = part.partition("=")
            if sep:
                names.add(name.strip())
    return names


def _route_path(scope: Scope) -> str:
    """Path relative to the app: subtracts `root_path` if the server prepends it."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        return path[len(root_path) :]
    return path


class AuthOriginCheckMiddleware:
    """Rejects with 403 a POST/PUT/PATCH/DELETE to `/auth/*` with a cross-origin session cookie.

    The general check lives in `orcaster_server.deps.require_session`: it applies exactly
    when the cookie is the credential that authenticates the request, and not before. The
    auth library routes (`/auth/logout`, `/auth/accept/{token}/join`)
    do not use that dependency: they read the cookie on their own or via
    `require_session_optional`. This middleware covers them.

    It is a middleware scoped to the `/auth/` prefix (and not a per-route
    dependency) because every route of that router is a browser route: none accepts
    `X-API-Key` or PAT, so requiring the same origin when there is a cookie does not
    break any programmatic client, and a new route of the router is covered
    without remembering anything. Outside `/auth/` it does not act: the presence of
    the cookie changes nothing on `/v1/*` or `/api/v1/*`. It does not look at
    `Authorization`: no `/auth/*` route accepts Bearer, so a Bearer
    authenticates nothing here and cannot exempt the check.
    """

    def __init__(
        self, app: ASGIApp, *, expected_origin: str, cookie_name: str, path_prefix: str = "/auth/"
    ) -> None:
        self.app = app
        self.expected_origin = expected_origin
        self.cookie_name = cookie_name
        self.path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in UNSAFE_METHODS
            or not _route_path(scope).startswith(self.path_prefix)
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if self.cookie_name not in _cookie_names(request.headers) or same_origin(
            request, self.expected_origin
        ):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"detail": CROSS_ORIGIN_DETAIL}, status_code=403)
        await response(scope, receive, send)
