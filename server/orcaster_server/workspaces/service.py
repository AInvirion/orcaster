"""Service of the workspaces module: creation of workspaces/memberships and
resolution of a user's active workspace.

`resolve_active_workspace` implements the fallback policy: it honors the active
workspace stored in `user_active_workspace` only if the user is still a member;
otherwise, it falls back to the first membership by `created_at`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.models import User
from orcaster_server.workspaces.models import UserActiveWorkspace, Workspace, WorkspaceMember

logger = logging.getLogger(__name__)


async def create_workspace(
    db: AsyncSession,
    *,
    name: str,
    slug: str,
    created_by_user_id: str | None = None,
) -> str:
    """Creates a workspace and returns its id."""
    res = await db.execute(
        insert(Workspace)
        .values(name=name, slug=slug, created_by_user_id=created_by_user_id)
        .returning(Workspace.id)
    )
    return str(res.scalar_one())


async def add_member(
    db: AsyncSession,
    *,
    workspace_id: str,
    user_id: str,
    role: str,
) -> bool:
    """Adds a member to the workspace. Idempotent on the composite PK.

    Returns `True` if it inserted the membership and `False` if the user was
    already a member (the invitations seam uses it to log the case).

    **It does not promote roles**: a second invitation with a role different from
    the existing one does NOT update the membership (`on_conflict_do_nothing`
    preserves the original role). To make that silent case visible, a `warning` is
    emitted when the requested role differs from the one already recorded.
    Explicit promotion, if needed, should be a dedicated endpoint, not a side
    effect of re-inviting.
    """
    existing = await db.execute(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    existing_role = existing.scalar_one_or_none()
    if existing_role is not None and existing_role != role:
        logger.warning(
            "add_member: user %s is already '%s' in workspace %s; the requested role "
            "'%s' is ignored (promotion via re-invitation is not allowed)",
            user_id,
            existing_role,
            workspace_id,
            role,
        )
    res = await db.execute(
        pg_insert(WorkspaceMember)
        .values(workspace_id=workspace_id, user_id=user_id, role=role)
        .on_conflict_do_nothing(index_elements=["workspace_id", "user_id"])
        .returning(WorkspaceMember.user_id)
    )
    # With ON CONFLICT DO NOTHING, RETURNING only returns a row if there was an INSERT.
    return res.scalar_one_or_none() is not None


async def list_memberships(db: AsyncSession, user_id: str) -> list[WorkspaceMember]:
    """Memberships of the user, ordered by `created_at` (stable order used as the
    fallback in `resolve_active_workspace`)."""
    res = await db.execute(
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(WorkspaceMember.created_at, WorkspaceMember.workspace_id)
    )
    return list(res.scalars().all())


async def list_membership_workspaces(db: AsyncSession, user_id: str) -> list[tuple[str, str, str]]:
    """Workspaces the user belongs to, with their name, for the switcher.

    Returns `(workspace_id, name, role)` for each membership, ordered by
    `workspace_members.created_at` (same stable order as `list_memberships`, used
    as the fallback in `resolve_active_workspace`). It JOINs `workspaces` to fetch
    the readable name that the UI shows in the dropdown.
    """
    res = await db.execute(
        select(WorkspaceMember.workspace_id, Workspace.name, WorkspaceMember.role)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(WorkspaceMember.created_at, WorkspaceMember.workspace_id)
    )
    return [(str(r[0]), str(r[1]), str(r[2])) for r in res.all()]


async def get_workspace_name(db: AsyncSession, workspace_id: str) -> str | None:
    """Name of the workspace, or None if it does not exist or the id is not a canonical UUID.

    Only the `8-4-4-4-12` form is accepted (upper or lower case). `uuid.UUID` also
    parses `urn:uuid:...`, `{...}` and the form without hyphens, but asyncpg
    rejects some of them with an exception when executing the query; it is parsed
    once and the query uses the canonical form, never the original string.
    """
    try:
        parsed = uuid.UUID(workspace_id)
    except ValueError:
        return None
    if str(parsed) != workspace_id.lower():
        return None
    res = await db.execute(select(Workspace.name).where(Workspace.id == str(parsed)))
    return res.scalar_one_or_none()


async def is_workspace_disabled(db: AsyncSession, workspace_id: str) -> bool:
    """True if the workspace exists and is disabled; False if it does not exist or
    the id is not a UUID.

    Unlike `get_workspace_name`, it does NOT require the canonical form: `{...}`,
    `urn:uuid:...` and the form without hyphens point to the same workspace and
    count as it. It is a privacy check, so being permissive is the safe direction:
    an alias of a disabled workspace must not let the page fall back to the stored
    label. The query uses the canonical form (`str(parsed)`), never the original
    string.
    """
    try:
        parsed = uuid.UUID(workspace_id)
    except ValueError:
        return False
    res = await db.execute(select(Workspace.disabled_at).where(Workspace.id == str(parsed)))
    return res.scalar_one_or_none() is not None


async def rename_workspace(db: AsyncSession, *, workspace_id: str, name: str) -> None:
    """Renames a workspace. It does not commit (the caller decides).

    The caller validates that `name` is not empty before calling; here we trust the
    contract. The scope (that `workspace_id` is the user's active one) is
    guaranteed by the handler via `ctx.workspace_id`.
    """
    await db.execute(update(Workspace).where(Workspace.id == workspace_id).values(name=name))


async def list_members(db: AsyncSession, workspace_id: str) -> list[tuple[str, str, datetime]]:
    """Members of the workspace with their readable name, role and join date.

    JOINs `workspace_members` -> `users` to fetch the `display_name`. Ordered by
    role (owner first) and then by `created_at` for a stable listing. Returns
    `(display_name, role, created_at)` per member.
    """
    res = await db.execute(
        select(User.display_name, WorkspaceMember.role, WorkspaceMember.created_at)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.role, WorkspaceMember.created_at)
    )
    return [(str(r[0]), str(r[1]), r[2]) for r in res.all()]


async def set_active_workspace(db: AsyncSession, *, user_id: str, workspace_id: str) -> bool:
    """Sets the user's active workspace via UPSERT into `user_active_workspace`.

    It first validates that the user is a member of `workspace_id`; if not, it
    returns `False` WITHOUT writing (the caller responds 404 so as not to disclose
    the existence of the workspace). If the user is a member, it UPSERTs on the PK
    `user_id` (`ON CONFLICT DO UPDATE`) and returns `True`. It does not commit:
    the caller decides.
    """
    member = await db.execute(
        select(WorkspaceMember.role).where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    if member.scalar_one_or_none() is None:
        return False

    stmt = pg_insert(UserActiveWorkspace).values(
        user_id=user_id, workspace_id=workspace_id, updated_at=func.now()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id"],
        set_={"workspace_id": workspace_id, "updated_at": func.now()},
    )
    await db.execute(stmt)
    return True


async def resolve_active_workspace(db: AsyncSession, user_id: str) -> tuple[str, str] | None:
    """Resolves `(workspace_id, role)` for the user.

    1. If there is a row in `user_active_workspace` and the user is still a member
       of that workspace, it returns `(workspace_id, role)` with the membership's role.
    2. Otherwise, it falls back to the first membership by `created_at`.
    3. With no memberships, it returns `None`.
    """
    active = await db.execute(
        select(UserActiveWorkspace.workspace_id).where(UserActiveWorkspace.user_id == user_id)
    )
    active_ws_id = active.scalar_one_or_none()

    if active_ws_id is not None:
        res = await db.execute(
            select(WorkspaceMember.role).where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.workspace_id == active_ws_id,
            )
        )
        role = res.scalar_one_or_none()
        if role is not None:
            return (active_ws_id, role)

    memberships = await list_memberships(db, user_id)
    if not memberships:
        return None
    first = memberships[0]
    return (first.workspace_id, first.role)
