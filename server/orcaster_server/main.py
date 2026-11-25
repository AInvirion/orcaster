import asyncio
import logging
import math
import os
import re
import sys
import traceback
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import (
    http_exception_handler as _default_http_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse, Response

from orcaster_server import __version__, db, plugins, signing, storage
from orcaster_server.api.admin_tasks import router as admin_tasks_router
from orcaster_server.api.admin_ui import router as admin_ui_router
from orcaster_server.api.admin_workers import router as admin_workers_router
from orcaster_server.api.poll import router as poll_router
from orcaster_server.api.status_ui import router as status_ui_router
from orcaster_server.api.tasks import router as tasks_router
from orcaster_server.api.v1 import router as api_v1_router
from orcaster_server.api.workers import router as workers_router
from orcaster_server.api_docs import install_api_docs
from orcaster_server.auth import AuthConfig, make_auth
from orcaster_server.auth.models import AuthBase
from orcaster_server.auth.origin import AuthOriginCheckMiddleware
from orcaster_server.body_limit import BodyLimitMiddleware, BodyLimitRule
from orcaster_server.config import settings
from orcaster_server.deps import SESSION_COOKIE
from orcaster_server.housekeeping import housekeeping_loop
from orcaster_server.sse_bus import SseBus
from orcaster_server.sse_listener import dashboard_listen_loop
from orcaster_server.task_secrets import keyring as task_secrets_keyring

# Registers the workspaces ORM models in `orcaster_server.models.Base.metadata`.
# Needed so that the FK string `workers/tasks.workspace_id -> workspaces.id`
# (migration 0021) resolves its target table on flush. Import for its side effect:
# `orcaster_server/models.py` cannot import `orcaster_server/workspaces/`
# (circular dependency).
from orcaster_server.workspaces import models as _workspaces_models  # noqa: F401
from orcaster_server.workspaces.seam import (
    describe_invitation,
    is_invitation_refused,
    on_invitation_accepted,
)

_PACKAGE_DIR = Path(__file__).resolve().parent

# Dashboard SSE bus. One instance per uvicorn process; the listener publishes
# and the /admin/events endpoint subscribes. It always exists (the endpoint
# references it), but it is only fed when the listener runs (sse_enabled).
sse_bus_instance = SseBus(coalesce_ms=settings.sse_output_coalesce_ms)


def _apply_migrations() -> None:
    """Apply `alembic upgrade head` against `settings.database_url`.

    Synchronous on purpose: `env.py` starts its own `asyncio.run`, and re-entering
    the current loop would fail. The caller must wrap it in `asyncio.to_thread` so
    as not to block the lifespan's main loop.
    """
    cfg = Config(str(_PACKAGE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PACKAGE_DIR / "migrations"))
    # The server process keeps its own logging config: without this, `env.py` runs
    # `fileConfig(alembic.ini)` and disables the app and uvicorn loggers created
    # before the upgrade.
    cfg.attributes["configure_logger"] = False
    if settings.database_url:
        # `env.py` reads DATABASE_URL from the environment. We force the app's URL
        # as the source of truth so that it matches what the engine uses.
        os.environ["DATABASE_URL"] = settings.database_url
    command.upgrade(cfg, "head")


_APP_LOG_FORMAT = "%(levelname)s [%(name)s] %(message)s"
# `alembic`: with `fileConfig` turned off in-process, its `Running upgrade X -> Y`
# lines only reach the journal if they are configured here.
_APP_LOGGERS = ("orcaster_server", "alembic")


def _has_output_handler(logger: logging.Logger) -> bool:
    """`hasHandlers()` without counting `NullHandler`.

    Libraries (alembic included) attach a `NullHandler` to their root logger so as
    not to emit without a config; `hasHandlers()` counts it and would treat as
    configured a logger whose records go nowhere.
    """
    current: logging.Logger | None = logger
    while current is not None:
        if any(not isinstance(h, logging.NullHandler) for h in current.handlers):
            return True
        if not current.propagate:
            return False
        current = current.parent
    return False


def _configure_app_logging() -> None:
    """Make the INFO logs of `app.*` and `alembic` visible on stderr (the journal).

    uvicorn's default config only configures the `uvicorn.*` loggers: the root stays
    at WARNING with no handler, so the app's INFO lines (`lifespan: ... loaded`,
    housekeeping, `task_secrets.delivered`) were discarded. The helper only acts on
    a logger whose records would reach no handler (not counting `NullHandler`) along
    its propagation chain: in that case it adds a stderr handler to it and, if the
    logger has no level of its own (`NOTSET`), sets `INFO` as a fallback level. If
    some handler in the chain already receives its records (explicit operator
    config, e.g. `uvicorn --log-config` configuring only the root), it touches
    neither the handler nor the level: the logger keeps its inherited effective
    level. Idempotent: on a second call the logger already has its own handler and
    is not modified; the lifespan can run several times in the same process.
    """
    for name in _APP_LOGGERS:
        logger = logging.getLogger(name)
        if _has_output_handler(logger):
            continue
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_APP_LOG_FORMAT))
        logger.addHandler(handler)


async def _report_stored_secrets_without_keys(log: logging.Logger) -> None:
    """Stored secrets without `TASK_SECRETS_KEYS_PATH` produce an ERROR.

    The server starts anyway: tasks that declare them stay `queued` until the file
    is configured and the server is restarted. A failure of the count (e.g. no
    reachable database) is logged and does not stop startup.
    """
    sessionmaker = db.get_sessionmaker()
    if sessionmaker is None:
        return
    try:
        async with sessionmaker() as session:
            count = (await session.execute(text("SELECT count(*) FROM task_secrets"))).scalar_one()
    except Exception:
        log.exception("lifespan: could not count stored task secrets")
        return
    if count:
        log.error(
            "lifespan: %d task secret(s) are stored but TASK_SECRETS_KEYS_PATH is not configured: "
            "tasks that declare them stay queued and are not delivered until the keys file is "
            "configured and the server restarted",
            count,
        )


def _report_boot_error(label: str, exc: BaseException) -> None:
    """Print a banner + traceback to stderr on any boot failure.

    uvicorn silently swallows exceptions from the lifespan (the worker is left
    defunct with no log and no "Application startup failed"). This helper
    guarantees that the operator sees the reason for the crash at least on stderr,
    even if the logging config is not propagating the app loggers.
    """
    banner = f"\n[startup error] {label}\n"
    sys.stderr.write(banner)
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    sys.stderr.flush()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _configure_app_logging()
    log = logging.getLogger(__name__)
    housekeeping_task: asyncio.Task[None] | None = None
    housekeeping_stop: asyncio.Event | None = None
    listener_task: asyncio.Task[None] | None = None
    listener_stop: asyncio.Event | None = None
    try:
        if settings.migrate_on_startup and settings.database_url:
            log.info("lifespan: applying alembic migrations to head")
            try:
                await asyncio.to_thread(_apply_migrations)
            except Exception:
                log.exception("lifespan: alembic upgrade head failed")
                raise
        # Fail-fast on the signing key (universal task signing): every task is
        # signed at INSERT, and there is no "unsigned task" branch in the server.
        # If `ED25519_PRIVATE_KEY_PATH` is not configured or the key does not load,
        # the process does NOT start: the operator prefers to see the error at boot
        # rather than discover it when the first admin_create_task fails.
        if not signing.is_signing_configured():
            hint = (
                "ED25519_PRIVATE_KEY_PATH not configured: server cannot sign "
                "tasks. Set the ED25519_PRIVATE_KEY_PATH environment variable "
                "(or a .env file in the working directory) to a PEM Ed25519 "
                "private key. For local dev:\n"
                "  openssl genpkey -algorithm Ed25519 "
                "-out .dev-ed25519.pem\n"
                "  echo 'ED25519_PRIVATE_KEY_PATH=.dev-ed25519.pem' "
                ">> .env"
            )
            log.error("lifespan: %s", hint)
            raise RuntimeError(hint)
        try:
            signing.validate_signing_config()
        except Exception:
            log.exception(
                "lifespan: failed to load ed25519 signing key from %s",
                settings.ed25519_private_key_path,
            )
            raise
        log.info("lifespan: ed25519 signing key loaded; task signing enabled")
        # Task secrets keys. Unset is a valid state (a single WARNING); set but
        # invalid stops startup, like the Ed25519 key: a server that started
        # without being able to decrypt would silently deliver tasks without their
        # secrets.
        if not task_secrets_keyring.is_configured():
            log.warning(
                "lifespan: TASK_SECRETS_KEYS_PATH not configured: central task secrets "
                "are disabled (setting a secret returns 503; "
                "tasks that declare stored secrets stay queued)"
            )
        else:
            try:
                task_secrets_keyring.validate_keys_config()
            except Exception:
                log.exception("lifespan: failed to load TASK_SECRETS_KEYS_PATH")
                raise
            # Non-secret ids, to compare against `rotate --keys-file`.
            loaded_ring = task_secrets_keyring.get_keyring()
            log.info(
                "lifespan: task secrets keys loaded; central task secrets enabled (%s)",
                loaded_ring.describe_key_ids() if loaded_ring is not None else "no keys",
            )
        # Plugins: the extractor registry is loaded here so that a broken plugin or
        # a configured name that does not exist stops startup, like the keys.
        # Without plugins the registry is left empty and `load_registry` says so in
        # the log: the core neither extracts variants nor classifies exit codes by
        # default.
        try:
            plugin_registry = plugins.get_registry()
        except Exception:
            log.exception("lifespan: failed to load plugins (ORCASTER_PLUGINS / entry points)")
            raise
        log.info(
            "lifespan: plugins loaded; variant kinds: %s; outcome defaults for: %s; "
            "legacy status aliases: %s",
            sorted(plugin_registry.kinds),
            sorted(plugin_registry.outcome_defaults),
            sorted(plugin_registry.legacy_status_aliases),
        )
        # Creating ARTIFACTS_ROOT once here takes the mkdir out of the poll hot path
        # (disk_free_bytes runs on every poll of every worker).
        storage.ensure_artifacts_root(settings.artifacts_root)
        db.init_engine()
        engine = db.get_engine()
        if not task_secrets_keyring.is_configured():
            await _report_stored_secrets_without_keys(log)
        if settings.housekeeping_enabled and engine is not None:
            housekeeping_stop = asyncio.Event()
            housekeeping_task = asyncio.create_task(
                housekeeping_loop(engine, settings, housekeeping_stop)
            )
        if settings.sse_enabled and settings.database_url:
            listener_stop = asyncio.Event()
            listener_task = asyncio.create_task(
                dashboard_listen_loop(settings.database_url, sse_bus_instance, listener_stop)
            )
    except BaseException as exc:
        _report_boot_error("server lifespan failed before yield", exc)
        raise
    try:
        yield
    finally:
        if housekeeping_task is not None and housekeeping_stop is not None:
            housekeeping_stop.set()
            try:
                await asyncio.wait_for(housekeeping_task, timeout=5.0)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception(
                    "housekeeping: task did not shut down cleanly"
                )
                housekeeping_task.cancel()
        if listener_task is not None and listener_stop is not None:
            listener_stop.set()
            try:
                await asyncio.wait_for(listener_task, timeout=5.0)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception(
                    "sse_listener: task did not shut down cleanly"
                )
                listener_task.cancel()
        await db.dispose_engine()


app = FastAPI(
    title="Orcaster",
    # Version of the installed package (app/__init__.py), not a fixed literal.
    # __version__ never raises and is never empty; it falls back to
    # VERSION_UNKNOWN ("0.0.0+unknown") if the package is not installed.
    version=__version__,
    lifespan=lifespan,
    # The documentation is served from `orcaster_server.api_docs`, behind auth.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
install_api_docs(app)
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_AUTH_STATIC_DIR = Path(__file__).resolve().parent / "auth" / "static"
# Starlette resolves mounts by prefix in registration order: the most specific one
# (`/static/auth`) MUST be registered before `/static`, or a GET to
# `/static/auth/webauthn.js` would match `/static` and return 404.
app.mount("/static/auth", StaticFiles(directory=str(_AUTH_STATIC_DIR)), name="auth-static")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_auth = make_auth(
    AuthBase,
    AuthConfig(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        origin=settings.webauthn_origin,
        cookie_name=SESSION_COOKIE,
        cookie_secure=settings.session_cookie_secure,
        begin_rate_per_minute=settings.auth_begin_rate_per_minute,
        begin_rate_burst=settings.auth_begin_rate_burst,
        begin_group_rate_per_minute=settings.auth_begin_group_rate_per_minute,
        begin_group_burst=settings.auth_begin_group_burst,
        on_invitation_accepted=on_invitation_accepted,
        describe_invitation=describe_invitation,
        is_invitation_refused=is_invitation_refused,
    ),
)

app.include_router(_auth.router)

# CSRF check: `orcaster_server.deps.require_session` applies it to every
# cookie-authenticated route; this middleware covers the `/auth/*` routes, which do
# not use that dependency. It replaces the per-route checks of `admin_ui` and of
# the `/auth` join.
app.add_middleware(
    AuthOriginCheckMiddleware,
    expected_origin=settings.webauthn_origin,
    cookie_name=SESSION_COOKIE,
)

# Body limit. Stack order, from the outside in:
#
#   _admin_cache_control > PlaneSecurityHeadersMiddleware
#   > StatusSecurityHeadersMiddleware > BodyLimitMiddleware
#   > AuthOriginCheckMiddleware > rutas
#
# It is registered AFTER `AuthOriginCheckMiddleware` (so it sits outside it: it
# wraps the app and every middleware that may read the body, so it cuts the request
# off before any of them reads a byte) and BEFORE the ones that stamp headers (so
# it sits inside them: its early 413/400 goes through
# `StatusSecurityHeadersMiddleware`, `PlaneSecurityHeadersMiddleware` and
# `_admin_cache_control` and leaves with their headers).
# `tests/test_body_limit_stack.py` pins this order. The two larger limits are those
# of the routes that really upload data.
app.add_middleware(
    BodyLimitMiddleware,
    default_max_bytes=settings.max_request_body_bytes,
    rules=(
        BodyLimitRule(
            re.compile(r"/v1/tasks/[0-9]+/artifacts"), settings.max_artifact_upload_body_bytes
        ),
        BodyLimitRule(re.compile(r"/v1/tasks/[0-9]+/output"), settings.max_output_body_bytes),
    ),
)

app.include_router(workers_router)
app.include_router(poll_router)
app.include_router(tasks_router)
app.include_router(admin_tasks_router)
app.include_router(admin_workers_router)
app.include_router(admin_ui_router)
app.include_router(api_v1_router)
app.include_router(status_ui_router)


# --- Hardening of the public /status/* plane --------------------------------
#
# The share token is a bearer secret that travels in the URL, so EVERY response
# under `/status/*` must carry these headers — including error routes that do not
# go through the router's handlers (404 for a nonexistent token, 405
# method-not-allowed, 422 validation, unexpected 500). Stamping them in a
# middleware scoped to the `/status` prefix guarantees the uniform coverage that
# per-handler stamping did not provide.
#
#   - Referrer-Policy: no-referrer  -> avoids leaking the token via the Referer
#     header if a cross-origin resource is ever added to the page.
#   - X-Frame-Options: DENY         -> prevents framing the page (clickjacking);
#     it is read-only, but there is no reason to allow the frame.
#   - Content-Security-Policy       -> restricts which origins the anonymous plane
#     may load resources from. See `_STATUS_CSP` for the detail of each directive.
#
# The `Cache-Control: public, max-age=10` of the `/summary` endpoint does NOT live
# here: it is handler logic (cacheability of that partial), not plane logic.
#
# CSP of the public /status/* plane.
#
# The anonymous plane only loads resources from ITS OWN ORIGIN: the htmx bundle
# (`/static/htmx.min.js`), the counter script (`/static/status.js`) and a GET poll
# to `/status/{token}/summary`. There are no images or third-party resources. So
# the CSP is strict and has no external origins:
#
#   - default-src 'self'        -> every resource not covered by a more specific
#     directive must come from the same origin (it covers connect-src for the htmx
#     polling, img-src, font-src, etc.).
#   - script-src 'self'         -> same-origin scripts only. We do NOT use
#     'unsafe-inline' or hashes: the counter's inline <script> was externalized to
#     /static/status.js precisely so script-src could be closed to 'self' (an
#     inline hash breaks on any whitespace change).
#   - style-src 'self' 'unsafe-inline' -> status.html and status_404.html carry a
#     large inline <style> and _status_summary.html uses `style="width: N%"` on
#     the progress bar. 'unsafe-inline' for STYLES is low risk (it does not allow
#     code execution) and pragmatic here.
#   - img-src 'self'            -> explicit for clarity; there are no images today.
#   - frame-ancestors 'none'    -> modern and stronger equivalent of
#     X-Frame-Options: DENY (current browsers honour it).
#   - base-uri 'none'           -> the page does not use <base>; closing it keeps an
#     injection from rewriting the resolution of relative URLs.
#   - form-action 'self'        -> the only form is the version selector, a
#     <form method="get"> that navigates to the SAME origin. It started as 'none'
#     (when the page had no forms) and became outdated once the form was added:
#     the browser blocked the submit and the select had no effect.
_STATUS_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)
_STATUS_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": _STATUS_CSP,
}


class StatusSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp the security headers on every response under `/status`.

    It runs after the handler (or the exception handler) produced the response, so
    it also covers error routes. Scoped by path prefix: exactly `/status` or
    `/status/...`; any other route (`/admin`, `/api/v1`, `/status-x`, ...) passes
    through untouched.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        path = request.url.path
        if path == "/status" or path.startswith("/status/"):
            response.headers.update(_STATUS_SECURITY_HEADERS)
            # Default no-store for the whole public plane. Only the responses that
            # handlers explicitly bless with `public, max-age=10` are cacheable by
            # the edge (the Cloudflare Cache Rule respects the origin). It covers
            # the 404 for an invalid token, the 200+HX-Redirect for a revoked token
            # and any future error route, without touching each handler (mirroring
            # the _admin_cache_control pattern below).
            if "cache-control" not in response.headers:
                response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(StatusSecurityHeadersMiddleware)


# --- Security headers on /admin and /auth ----------------------------------
#
# The `/admin/*` (session-cookie dashboard) and `/auth/*` (login, passkeys,
# invitations) planes carry these headers on EVERY response (including
# 303/401/403/404/405/422/413/400/500, because the middleware runs after the
# handler and the exception handler):
#
#   - X-Frame-Options: DENY          -> browsers that do not read frame-ancestors.
#   - X-Content-Type-Options: nosniff -> the browser honours the Content-Type.
#   - Referrer-Policy: same-origin   -> neither `/auth/accept/<token>` nor the
#     `/admin` URLs leak in the `Referer` to another origin, but the browser
#     STILL sends the real origin on same-site POSTs. It is NOT `no-referrer` on
#     purpose: the CSRF check (`auth/origin.py::same_origin`) compares the `Origin`
#     header, and with `no-referrer` the browser sends `Origin: null` on native
#     form POSTs (there are several in /admin: logout, workspace switch, PATs,
#     secrets, share links, join), which the check would reject with 403.
#   - Content-Security-Policy        -> see `_PLANE_CSP`.
#
# `_PLANE_CSP`. No directive carries 'unsafe-inline' or a nonce: the inline JS and
# CSS of the templates were moved to /static, and what used to be
# `style="--pct: .."`, `onclick=` and `hx-on::` is now `data-*` that app.js
# interprets.
#   - default-src 'self'      -> base for everything not listed (fonts, frames...).
#   - script-src 'self'       -> /static/*.js only. htmx runs with
#     `allowEval=false` (the `htmx-config` meta in base.html), which also avoids
#     `unsafe-eval`.
#   - style-src 'self'        -> /static stylesheets only. htmx does not inject its
#     <style> (`includeIndicatorStyles=false`) and there is no `style=` in the
#     markup. Assigning `el.style.*` through the CSSOM from app.js does not fall
#     under style-src.
#   - img-src 'self' data:    -> the `data:` SVGs in the CSS (skeleton, chevron).
#   - connect-src 'self'      -> fetch/XHR and the /admin/events SSE.
#   - form-action 'self'      -> native forms post to the same origin.
#   - base-uri 'none', object-src 'none', frame-ancestors 'none'.
#
# It does not overwrite a header that the response already carries. Scope by
# segment: `/admin`, `/admin/...`, `/auth`, `/auth/...`; it does not touch
# `/status/*` (which has its own CSP, `StatusSecurityHeadersMiddleware`),
# `/static/auth/*`, `/api/v1/*`, `/v1/*`, `/healthz` or `/readyz`. It sits outside
# `BodyLimitMiddleware`, so the early 413/400 also carries these headers
# (`tests/test_body_limit_stack.py`).
_PLANE_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'"
)
_PLANE_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": _PLANE_CSP,
}
_PLANE_SECURITY_PREFIXES = ("/admin", "/auth")


def _in_hardened_plane(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _PLANE_SECURITY_PREFIXES)


class PlaneSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp CSP, Referrer-Policy, anti-framing and nosniff on `/admin` and `/auth`."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        if _in_hardened_plane(request.url.path):
            for name, value in _PLANE_SECURITY_HEADERS.items():
                if name.lower() not in response.headers:
                    response.headers[name] = value
        return response


app.add_middleware(PlaneSecurityHeadersMiddleware)


# --- Cache-Control on the admin plane ---------------------------------------
#
# The `/admin/*` plane is authenticated by session cookie and serves sensitive
# per-workspace content (execution output, PATs, enrollment tokens). A properly
# configured intermediate proxy should not cache cookie-authenticated responses,
# but the absence of the header leaves the behaviour implicit. `no-store` makes it
# explicit across the board (global middleware).
#
# It preserves any `Cache-Control` that the endpoint declares on its own (the
# `/admin/events` SSE stream sets `no-cache` on purpose): it only injects it when
# missing. It touches nothing outside `/admin` (worker API `/v1/*`, `/api/v1/*`,
# `/auth/*`, `/healthz`, `/readyz`, static files).
@app.middleware("http")
async def _admin_cache_control(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/admin") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


# --- Auth enforcement on /admin/* -------------------------------------------
#
# The admin handlers depend on `require_workspace` (orcaster_server/deps.py), which
# raises 401 (no session) or 403 (valid session but no membership). This exception
# handler translates those errors ONLY for `/admin` routes into a coherent UX:
#
#   - HTML navigation (Accept: text/html, not HX) -> 303 to /auth/login
#   - HTMX request (header HX-Request: true)      -> 401 with HX-Redirect to login
#     (htmx makes the browser navigate when it sees that header)
#   - rest (JSON APIs, fetch without Accept html) -> 401/403 JSON (default)
#
# Everything that does NOT start with /admin (including /healthz, /readyz,
# /auth/*, the /v1/* worker API) falls to FastAPI's default rendering untouched,
# except `/api/v1/*` (the public JSON API), which has its own error model.
_LOGIN_URL = "/auth/login"

# Uniform error model of `/api/v1`: `{"error": <code>, "message": ...}`.
# The `code` is derived from the HTTP status; there is never a 303 redirect (that is
# HTML navigation in `/admin`). The 401 carries a generic message (it does not leak
# which check failed).
_API_V1_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "unprocessable_entity",
    503: "service_unavailable",
}


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept.lower()


def _is_hx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _render_api_v1_error(exc: HTTPException) -> JSONResponse:
    # A handler can set its own code (`error_code`), e.g.
    # `task_secrets_unconfigured`; otherwise it comes from the HTTP status.
    code = getattr(exc, "error_code", None) or _API_V1_ERROR_CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else code
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": code, "message": message},
        headers=getattr(exc, "headers", None),
    )


async def _http_exception_handler(request: Request, exc: HTTPException) -> Response:
    path = request.url.path
    if path.startswith("/api/v1"):
        # Public API: always JSON with the uniform error model, never a redirect.
        return _render_api_v1_error(exc)
    if exc.status_code == 403 and path.startswith("/admin"):
        # The caller is signed in (or the request was refused before the session
        # mattered, e.g. cross-origin): sending them to sign-in would hide the
        # reason and land them on the same refusal. htmx gets the bare 403 (no
        # HX-Redirect); a browser navigation gets an error page.
        if _wants_html(request) and not _is_hx(request):
            from orcaster_server.api.admin_ui import templates as admin_templates

            detail = exc.detail if isinstance(exc.detail, str) else "forbidden"
            return admin_templates.TemplateResponse(
                request,
                "admin_forbidden.html",
                {"detail": detail},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        return await _default_http_exception_handler(request, exc)
    if exc.status_code == 401 and path.startswith("/admin"):
        if _is_hx(request):
            # htmx: 401 + HX-Redirect makes the browser navigate to the login.
            # We try to preserve the deep link via HX-Current-URL if it is present
            # and points to an /admin route.
            hx_current = request.headers.get("HX-Current-URL")
            redirect_target = _LOGIN_URL
            if hx_current:
                parsed = urllib.parse.urlsplit(hx_current)
                hx_path = parsed.path
                if hx_path.startswith("/admin"):
                    target = hx_path
                    if parsed.query:
                        target = target + "?" + parsed.query
                    redirect_target = _LOGIN_URL + "?next=" + urllib.parse.quote(target, safe="")
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"HX-Redirect": redirect_target},
            )
        if _wants_html(request):
            # Direct navigation to an HTML page -> redirect to the login with next=.
            target = path
            if request.url.query:
                target = target + "?" + request.url.query
            login_url = _LOGIN_URL + "?next=" + urllib.parse.quote(target, safe="")
            return RedirectResponse(url=login_url, status_code=303)
    # Rest: FastAPI default rendering (JSON with {"detail": ...}).
    return await _default_http_exception_handler(request, exc)


def _json_safe(value: object) -> object:
    """Replace non-finite floats with their `repr` so the 422 can be serialized.

    `json.loads` accepts `NaN`/`Infinity`, and pydantic returns the offending
    `input` inside each error. `JSONResponse` serializes with `allow_nan=False`, so
    without this a body with `NaN` turned the validation 422 into a 500.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


# Routes whose body carries the value of a secret.
_SECRET_VALUE_PATHS = ("/api/v1/secrets",)
# Creating and forking a task (dashboard and API v1) and queueing a push command or
# a control-op for a worker: the payload may carry a `NAME=value` with the value of
# a secret in `task_env`.
_TASK_CREATE_PATH = re.compile(
    r"/(?:admin|api/v1)/tasks(?:/[^/]+/fork)?|/admin/workers/[^/]+/(?:command|control-op)"
)


def _echoes_no_input(path: str) -> bool:
    """True if the 422 for `path` must not return what the client sent.

    `path` must be the path with which the routes are DECLARED, the one returned by
    `_app_relative_path`, not the raw `request.url.path`.
    """
    return path.startswith(_SECRET_VALUE_PATHS) or _TASK_CREATE_PATH.fullmatch(path) is not None


def _app_relative_path(request: Request) -> str:
    """The path with which the routes are declared, without the proxy prefix.

    If the app is served under a `root_path` (`--root-path /prefix`, `strip_prefix`,
    `handle_path`) and the server leaves the prefix in the scope's path,
    `request.url.path` is `/prefix/admin/tasks`. Starlette strips the prefix to
    route, so it runs exactly the same route; a prefix or pattern check done on the
    raw path, on the other hand, stops matching **silently and in the unsafe
    direction**: `_echoes_no_input` returns False and the 422 again brings back the
    whole payload in `detail[].input`, including the `task_env` entry. Measured with
    Starlette 1.3.1 and FastAPI 0.162.2.

    Both ways a proxy can deliver the request end up at the same path: if it already
    stripped the prefix (relative `path`), there is nothing to strip and this is the
    identity. Nothing in the repo sets a `root_path` today, so for every current
    deployment this is simply the identity; it is a future deployment behind a
    prefix that this covers.

    The `root_path` is normalized by removing its trailing slash before comparing.
    With `--root-path /prefix/` (a slash that the Starlette docs ask not to add, and
    that nothing validates) the prefix could not be stripped and the check would
    again fail silently. Measured: today that form makes Starlette not route and the
    request dies with a 404 before reaching here — but that is a fact about this
    Starlette version, not an invariant on which to base a statement.
    """
    path = request.url.path
    root = (request.scope.get("root_path") or "").rstrip("/")
    if root and path.startswith(root):
        rest = path[len(root) :]
        # It is only a prefix of SEGMENTS: with `root_path="/pre"` a path
        # `/prefix/x` starts with the prefix but is not under it.
        if rest.startswith("/"):
            return rest
        if rest == "":
            return "/"
    return path


def _drop_echoed_input(errors: list[dict]) -> list[dict]:
    """Remove from each pydantic error the `input` (the value sent, here the whole
    payload) and the `error` in `ctx` (the validator's exception, whose text is
    already in `msg`). The rest of `ctx` (`ge`, `max_length`...) is kept.
    """
    out = []
    for err in errors:
        err = {k: v for k, v in err.items() if k != "input"}
        ctx = {k: v for k, v in (err.get("ctx") or {}).items() if k != "error"}
        if ctx:
            err["ctx"] = ctx
        else:
            err.pop("ctx", None)
        out.append(err)
    return out


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    """Validation 422 for body/query: uniform error model under `/api/v1`.

    Outside `/api/v1` FastAPI's default shape (`{"detail": [...]}`) is kept, which
    the worker API and the dashboard already consume. It is built here instead of
    delegating to the default handler so that it goes through `_json_safe`.

    Both decisions (whether to redact, and which error model to use) are made on
    `_app_relative_path` and not on `request.url.path`: under a `root_path` the raw
    path carries the proxy prefix and neither check would match.

    What DOES remain in the 422 of a redacted route are `loc` and `msg`, and they may
    repeat text that the caller put in ANOTHER field of the body — an unknown key
    (the models are `extra="forbid"`) appears in `loc`, and the `target` validator
    names the received value. This is a decision, not an oversight: the 422 has to
    say which field failed and why. What never comes back is the `payload.task_env`
    entry (its messages carry only the index), the `input` and `ctx["error"]`. This
    is documented in `docs/user-manual.md` and pinned by
    `tests/test_task_env_create_validation.py`.
    """
    errors = exc.errors()
    path = _app_relative_path(request)
    if _echoes_no_input(path):
        # The value of a secret is write-only; a 422 does not return it inside the
        # `input` that pydantic attaches to each error. The same applies to the
        # payload of a new task.
        errors = _drop_echoed_input(errors)
    detail = _json_safe(jsonable_encoder(errors))
    if path.startswith("/api/v1"):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": "unprocessable_entity",
                "message": "request validation failed",
                "detail": detail,
            },
        )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": detail}
    )


app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
app.add_exception_handler(RequestValidationError, _validation_exception_handler)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Convenience shortcut: the root leads to the dashboard.

    The dashboard lives under `/admin` (separation of planes: worker API under
    `/v1`, auth under `/auth`, human UI under `/admin`). Temporary (307) redirect on
    purpose, not permanent: it keeps the browser from caching the destination if the
    root changes role later.
    """
    return RedirectResponse(url="/admin/", status_code=307)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    if db.get_engine() is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "DATABASE_URL not configured"},
        )
    try:
        await db.ping()
    except Exception:
        # Anonymous route: the driver's text may carry host, port, user or database.
        # The response carries a fixed reason; the detail goes to the log.
        logging.getLogger(__name__).exception("readyz: db ping failed")
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "db unreachable"},
        )
    return JSONResponse(status_code=200, content={"status": "ready"})
