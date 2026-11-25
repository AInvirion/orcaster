"""Dependency `require_pat_workspace`: authenticates the public `/api/v1` API with a
Personal Access Token and resolves the `WorkspaceContext`.

It is the Bearer counterpart of `require_workspace` (session cookie,
`orcaster_server/deps.py`): it returns the **same**
`WorkspaceContext{user, workspace_id, role}` enriched with the token metadata
(`PatContext`), so multi-tenant isolation (cross-tenant -> 404) is inherited
for free; only *how it authenticates* changes.

The scheme rule lives in a single function, `_bearer_credentials`, behind
`extract_bearer` (the token) and `has_bearer_scheme` (does the header declare a
PAT?, used by `orcaster_server/api_docs.py`). The scheme name is compared
case-insensitively, as RFC 7235 section 2.1 requires.

Mapping of `resolve_pat` states to HTTP:
  - `INVALID`    -> generic 401 (token missing, unknown, revoked, expired or owned by
                   a disabled user; which check failed is not disclosed).
  - `LEGACY_PREFIX` -> 401 whose message says the token prefix is an unsupported
                   legacy name and a new token must be minted (never accepted).
  - `NOT_MEMBER` -> 403 (valid token but the owner is no longer a member).
  - `OK`         -> commits `last_used_at` (best effort) and returns the context.

Rendering these errors as uniform JSON (`{"error","message"}`) is done by the
`/api/v1/*` exception handler in `orcaster_server/main.py`; this module only
raises the `HTTPException` with the right status.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.models import User
from orcaster_server.db import get_session
from orcaster_server.pats.service import LEGACY_PAT_REJECTION_MESSAGE, PatState, resolve_pat
from orcaster_server.workspaces.deps import WorkspaceContext

_BEARER_SCHEME = "bearer"


@dataclass
class PatContext(WorkspaceContext):
    """`WorkspaceContext` + metadata of the PAT that authenticated the request.

    It inherits `user/workspace_id/role`, so task handlers consume it the same
    way as the `/admin` context; only `GET /auth/whoami` uses `token_*`.
    """

    token_label: str | None = None
    token_last_used_at: datetime | None = None
    token_expires_at: datetime | None = None


def _bearer_credentials(header: str) -> str | None:
    """Single source of the `Bearer` scheme rule (RFC 7235 section 2.1).

    Returns `None` if the header does not declare the Bearer scheme, and the
    credentials (possibly "") if it does. The scheme name is compared with
    `casefold()` because the RFC defines it as a case-insensitive token:
    `bearer`, `BEARER` and `bEaReR` are the same scheme. The RFC allows `1*SP`
    between the scheme and the credentials, so the header is split at the first
    space and the rest is `strip()`ped.

    What does NOT change: the separator is required (`Bearer` alone -> `None`),
    the name must be the complete scheme (`Bearerx ...` -> `None`) and the token
    remains case-sensitive.
    """
    scheme, separator, credentials = header.partition(" ")
    if not separator or scheme.casefold() != _BEARER_SCHEME:
        return None
    return credentials.strip()


def extract_bearer(request: Request) -> str:
    """Extracts the token from the `Authorization: <bearer> ...` header.

    Returns "" if the header is missing or does not use the Bearer scheme;
    `resolve_pat` treats the empty string as INVALID -> generic 401.
    """
    credentials = _bearer_credentials(request.headers.get("Authorization", ""))
    return credentials if credentials is not None else ""


def has_bearer_scheme(request: Request) -> bool:
    """`True` if the `Authorization` header declares the Bearer scheme.

    `api_docs.openapi_json` uses it to choose the credential (PAT vs. cookie)
    with the same rule as `extract_bearer`, instead of repeating the prefix test.
    Declaring the scheme without credentials (`Bearer `) counts as declaring it:
    it is an empty PAT, 401, and does not fall back to the cookie.
    """
    return _bearer_credentials(request.headers.get("Authorization", "")) is not None


async def require_pat_workspace(
    request: Request,
    db: AsyncSession = Depends(get_session),  # noqa: B008 (FastAPI pattern)
) -> PatContext:
    token = extract_bearer(request)
    resolution = await resolve_pat(db, token)
    if resolution.state is PatState.LEGACY_PREFIX:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=LEGACY_PAT_REJECTION_MESSAGE
        )
    if resolution.state is PatState.INVALID:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    if resolution.state is PatState.NOT_MEMBER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a workspace member")
    # OK: persist the `last_used_at` that resolve_pat left pending.
    await db.commit()
    user = await db.get(User, resolution.user_id)
    if user is None:  # pragma: no cover - FK CASCADE makes this almost impossible
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    return PatContext(
        user=user,
        workspace_id=resolution.workspace_id,
        role=resolution.role,
        token_label=resolution.token_label,
        token_last_used_at=resolution.token_last_used_at,
        token_expires_at=resolution.token_expires_at,
    )
