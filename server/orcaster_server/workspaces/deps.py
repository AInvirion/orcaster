"""Dependency `require_workspace`: resolves the active workspace of the
authenticated user and exposes it as `WorkspaceContext`.

It is built with a factory that receives `require_session` (a closure of the auth
module, see `orcaster_server/auth/factory.py`), mirroring the style of
`make_auth`: `require_session` is not a symbol importable at module level, it
lives in the `Auth` object assembled in `orcaster_server/main.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.models import User
from orcaster_server.db import get_session
from orcaster_server.workspaces.service import resolve_active_workspace


@dataclass
class WorkspaceContext:
    user: User
    workspace_id: str
    role: str


def require_owner(ctx: WorkspaceContext) -> None:
    """Raises 403 if the active role is not owner.

    Multi-tenant rule for changing the workspace configuration: rename,
    invitations, share links and task secrets are owner-only.
    The dashboard (`/admin`) and the public API (`/api/v1`) share it.
    """
    if ctx.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="owner role required")


def make_require_workspace(require_session):
    """Builds the `require_workspace` dependency on top of `require_session`.

    The `Depends(...)` go as the parameter's default VALUE (not in `Annotated`).
    This is deliberate: with `from __future__ import annotations` active, the
    annotations are lazy strings and FastAPI needs to resolve them with
    `get_type_hints`, which fails for `require_session` because it is a closure
    variable (it does not live in the module namespace). By passing the `Depends`
    as the default, FastAPI reads it directly from the marker object without
    evaluating the annotation; it works with any `require_session` injected by the
    host.
    """

    async def require_workspace(
        user: User = Depends(require_session),  # noqa: B008 (FastAPI pattern; see docstring)
        db: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> WorkspaceContext:
        resolved = await resolve_active_workspace(db, user.id)
        if resolved is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="no workspace membership",
            )
        workspace_id, role = resolved
        return WorkspaceContext(user=user, workspace_id=workspace_id, role=role)

    return require_workspace
