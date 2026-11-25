"""Host-level dependencies for auth + workspace enforcement on `/admin/*`.

The `workspaces` module exposes a FACTORY `make_require_workspace(require_session)`
that does not decide HOW the session is resolved: that is the host's responsibility.
Here we build that host-level `require_session` (it reads the session cookie and
resolves it through `orcaster_server.auth.service.session.resolve_session`) and the
module-level `require_workspace` that the admin routers inject.

Allowed boundary: host (`orcaster_server/deps.py`) -> auth
(`orcaster_server/auth/service/session`) and
host -> workspaces (`orcaster_server/workspaces/deps`). It mirrors the wiring of
`orcaster_server/auth/factory.py::require_session` but at module level, so that the
admin handlers (declared with `@router.post/get`) can depend on it without a
refactor to a router factory.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.models import User
from orcaster_server.auth.origin import CROSS_ORIGIN_DETAIL, UNSAFE_METHODS, same_origin
from orcaster_server.auth.service.session import resolve_session
from orcaster_server.config import settings
from orcaster_server.db import get_session
from orcaster_server.workspaces.deps import (
    WorkspaceContext,
    make_require_workspace,
    require_owner,
)

# Name of the session cookie. `orcaster_server/main.py` passes it to `AuthConfig` and to
# `AuthOriginCheckMiddleware`: a single source for the three uses.
SESSION_COOKIE = "session"

__all__ = [
    "SESSION_COOKIE",
    "WorkspaceContext",
    "require_session",
    "require_workspace",
    "require_workspace_owner",
]


async def require_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Resolve the user of the cookie session, or raise 401.

    Minimal on purpose: it reuses `resolve_session` (which checks that the session is
    not revoked or expired and that the user is not disabled). The 401 is intercepted
    by the exception handler in `orcaster_server/main.py` to tell HTML (303) apart
    from API (401) on `/admin` routes.

    CSRF defence: on POST/PUT/PATCH/DELETE the cookie is the credential that
    authenticates the request, so the request must come from the same origin or it is
    rejected with 403. The check lives here, and not in an app-wide middleware, so
    that it applies exactly when the cookie authenticates: no `Authorization` header
    exempts it, and a stale cookie does not affect routes authenticated with
    `X-API-Key` or a PAT. Every `require_workspace` inherits it.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    if request.method in UNSAFE_METHODS and not same_origin(request, settings.webauthn_origin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CROSS_ORIGIN_DETAIL)
    user = await resolve_session(db, token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    await db.commit()
    return user


# `require_workspace`: raises 403 if the user has no memberships; otherwise it
# returns `WorkspaceContext(user, workspace_id, role)`.
require_workspace = make_require_workspace(require_session)


async def require_workspace_owner(
    ctx: Annotated[WorkspaceContext, Depends(require_workspace)],
) -> WorkspaceContext:
    """`require_workspace` plus the owner role, or raise 403.

    For routes that act on the whole workspace (worker control): the check runs
    before the handler, so a member is refused before any lookup or write.
    """
    require_owner(ctx)
    return ctx
