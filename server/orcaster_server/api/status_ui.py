"""Public router of the status page.

Mounted at /status/*, WITHOUT require_workspace and OUTSIDE the login
redirect block of main.py (that branch only touches /admin). An invalid,
revoked or nonexistent token, or a disabled workspace → generic 404 (does
not reveal existence).

Fully read-only: no actions, no payloads, no per-task data beyond the count
by status. Refresh by HTMX polling (every 10s) with a cacheable response
(Cache-Control: public, max-age=10), not SSE. The rest of the /status/* plane
is no-store by default (middleware).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.config import settings
from orcaster_server.db import get_session
from orcaster_server.status.cache import snapshot_cache
from orcaster_server.status.snapshot import build_status_snapshot
from orcaster_server.status.tokens import resolve_share_token
from orcaster_server.task_variant import VARIANT_TOKEN_MAX_LEN

router = APIRouter(prefix="/status", tags=["status-public"], include_in_schema=False)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# The `variant` query param feeds the snapshot cache key, so its cardinality
# and size are memory retained by the process. Since the /status plane is
# anonymous, the value is unvalidated client input: we bound it to
# `VARIANT_TOKEN_MAX_LEN` characters (real labels like
# "example-tool~1.2.3 / b4.5.6" fit easily; anything longer → automatic 422
# from FastAPI) and normalize it with strip() before use, so `?variant=all`
# and `?variant=%20all%20` collapse into a single entry instead of two.
# `resolve_variant_filter` already strips, so normalizing here also keeps the
# key and the snapshot looking at exactly the same value.
#
# The limit is NOT declared as a literal. The same constant bounds what is
# PERSISTED in `variant_kind`/`variant_label` (`clamp_variant`, applied at
# ingestion), so the `kind~label` token that this page re-emits in the poll's
# `hx-get` and in the selector's `<option>` elements always fits here: the
# invariant has a single owner (`orcaster_server/task_variant.py`) instead of
# two numbers that can drift apart.
VariantParam = Annotated[str, Query(max_length=VARIANT_TOKEN_MAX_LEN)]

# The hardening headers of the public plane (Referrer-Policy/X-Frame-Options)
# are NOT stamped here: `StatusSecurityHeadersMiddleware`
# (orcaster_server/main.py) applies them to EVERY response under `/status/*`,
# including error routes that do not go through these handlers.


async def _resolve(session: AsyncSession, token: str) -> str | None:
    """Resolve a share token to its workspace_id, or None if it does not apply.

    None = nonexistent / revoked / disabled (generic 404: the reason is not
    distinguished). The caller decides how to materialize the 404.
    """
    workspace_id = await resolve_share_token(session, token)
    if workspace_id is None:
        return None
    await session.commit()  # persists last_seen_at (best-effort)
    return workspace_id


@router.get("/{token}", response_class=HTMLResponse)
async def status_page(
    request: Request, session: SessionDep, token: str, variant: VariantParam = ""
) -> Response:
    variant = variant.strip()
    workspace_id = await _resolve(session, token)
    if workspace_id is None:
        # Standalone HTML 404 page (external client): generic 404 that does
        # not reveal existence. The /summary partial stays a plain 404.
        return templates.TemplateResponse(
            request,
            "status_404.html",
            {},
            status_code=404,
        )
    # `variant`: the version lens (current by default). Rescales the
    # snapshot's task aggregates; fleet and "Active now" stay global.
    # TTL + single-flight cache keyed by (workspace, variant). Token
    # resolution happens above, outside the cache: immediate revocation. The
    # closure is evaluated only on a miss.
    snap = await snapshot_cache.get_or_build(
        (workspace_id, variant),
        settings.status_snapshot_cache_ttl_s,
        lambda: build_status_snapshot(session, workspace_id, variant),
    )
    resp = templates.TemplateResponse(
        request,
        "status.html",
        {"snap": snap, "token": token},
    )
    # The full page is cacheable with the same TTL as the summary. Required
    # by the Cloudflare Cache Rule: without an explicit header, the edge
    # would apply its default. The no-store of the other routes is stamped by
    # StatusSecurityHeadersMiddleware (main.py).
    resp.headers["Cache-Control"] = "public, max-age=10"
    return resp


@router.get("/{token}/summary", response_class=HTMLResponse)
async def status_summary(
    request: Request, session: SessionDep, token: str, variant: VariantParam = ""
) -> Response:
    variant = variant.strip()
    workspace_id = await _resolve(session, token)
    if workspace_id is None:
        # Token revoked/nonexistent while the client has the page open.
        # htmx 2.x does NOT process HX-Redirect on 4xx/5xx responses (it only
        # fires htmx:responseError and neither swaps nor follows the
        # redirect); that is why we use status 200 so htmx processes it
        # reliably. The browser navigates to /status/{token}, which renders
        # the full HTML 404 "Status unavailable": no leak (generic 404,
        # reason not revealed).
        #
        # Defense in depth: the `token` is unvalidated user input; we
        # percent-encode it before reflecting it in the header so raw
        # characters are not reflected.
        # - A valid token is urlsafe base64 (A-Za-z0-9_-), which
        #   `quote(..., safe="")` leaves intact → unchanged behaviour for the
        #   normal case.
        # - Any unexpected character (spaces, controls, CRLF) ends up
        #   percent-encoded, neutralizing the risk of header injection /
        #   control characters.
        # - The destination is still same-origin (/status/<encoded>) and
        #   resolves to the same generic 404: the UX does not change.
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/status/{quote(token, safe='')}"},
        )
    # TTL + single-flight cache keyed by (workspace, variant). Token
    # resolution happens above, outside the cache: immediate revocation. The
    # closure is evaluated only on a miss.
    snap = await snapshot_cache.get_or_build(
        (workspace_id, variant),
        settings.status_snapshot_cache_ttl_s,
        lambda: build_status_snapshot(session, workspace_id, variant),
    )
    resp = templates.TemplateResponse(
        request,
        "_status_summary.html",
        {"snap": snap, "token": token},
    )
    # Cacheable response: the same response serves everyone sharing the token
    # during the 10s window (Cloudflare/browser absorb bursts).
    resp.headers["Cache-Control"] = "public, max-age=10"
    return resp
