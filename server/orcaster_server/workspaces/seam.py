"""Lifecycle bridge from auth to workspaces.

`on_invitation_accepted` is the handler that the host wires into `AuthConfig`
(see `orcaster_server/main.py`). It runs INSIDE the uncommitted transaction of the
invitation acceptance ceremony (`orcaster_server/auth/router.py`), which is why it
receives the active `db`: the newly created user row is only visible in that
session.

The user can be new (passkey ceremony `register/complete`) or one that already
exists with a session (`POST /auth/accept/{token}/join`). In both cases the
invitation's workspace becomes the user's active one.

Best-effort policy: an invitation with a malformed payload (operator error) must
not abort the user's registration nor break the dev flow with an empty payload
(`orcaster_server/cli/dev_invite.py` mints `payload={}`). If the payload does not
carry a valid `workspace_id` and a known `role`, a warning is logged and the user
simply does not join any workspace.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orcaster_server.auth.config import INVITATION_WITHHELD
from orcaster_server.workspaces import service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from orcaster_server.auth.models import Invitation, Session, User

_log = logging.getLogger(__name__)

_VALID_ROLES = ("owner", "member")


async def describe_invitation(db: AsyncSession, inv: Invitation) -> str | None:
    """Text for the acceptance page: workspace and role that the invitation grants.

    It is derived from the payload and the current workspace name, not from
    `invitations.label`: older invitations store `workspace:<uuid>` there and must
    look just as good.

    Returns None, and the page falls back to the stored label, when the payload
    does not describe a valid membership (same criterion as
    `on_invitation_accepted`): it is not a JSON object, `workspace_id` or `role` is
    missing, the role is unknown, the id is not a canonical UUID or the workspace
    does not exist. It never raises because of the payload content: JSONB admits
    any JSON, and this function runs on a public page. If the workspace is disabled
    it returns `INVITATION_WITHHELD`: neither name nor role nor label, even if the
    id comes in a non-canonical form (`{...}`, `urn:uuid:...`, without hyphens) or
    the payload is invalid. Each invalid case, except the empty payload of the dev
    flow, leaves a warning with the invitation id (never the token).
    """
    payload = inv.payload
    if payload is None or payload == {}:
        # Dev flow (`orcaster_server/cli/dev_invite.py` mints `payload={}`): there is
        # nothing to describe and it is not an operator error either.
        return None
    if not isinstance(payload, dict):
        _log.warning(
            "invitation %s has a non-object payload (%s); not describing it",
            inv.id,
            type(payload).__name__,
        )
        return None
    workspace_id = payload.get("workspace_id")
    role = payload.get("role")
    if isinstance(workspace_id, str) and await service.is_workspace_disabled(db, workspace_id):
        # A disabled workspace is not named on a public page, not even through the
        # label that the page falls back to with an invalid payload.
        return INVITATION_WITHHELD
    if not isinstance(workspace_id, str) or not workspace_id or role not in _VALID_ROLES:
        _log.warning(
            "invitation %s has an invalid membership payload (workspace_id=%r, role=%r); "
            "not describing it",
            inv.id,
            workspace_id,
            role,
        )
        return None
    name = await service.get_workspace_name(db, workspace_id)
    if name is None:
        _log.warning(
            "invitation %s points to workspace %r, which does not exist or has a "
            "non-canonical id; not describing it",
            inv.id,
            workspace_id,
        )
        return None
    return f"Workspace \u201c{name}\u201d as {role}"


async def is_invitation_refused(db: AsyncSession, inv: Invitation) -> bool:
    """True if the invitation points to a disabled workspace.

    Same rule as `describe_invitation`: `is_workspace_disabled` accepts aliases of
    the UUID and never raises because of the payload content. A payload that does
    not describe a workspace (e.g. the `{}` of the dev flow) is not refused here:
    `on_invitation_accepted` handles that.
    """
    payload = inv.payload
    if not isinstance(payload, dict):
        return False
    workspace_id = payload.get("workspace_id")
    return isinstance(workspace_id, str) and await service.is_workspace_disabled(db, workspace_id)


async def on_invitation_accepted(
    db: AsyncSession,
    user: User,
    inv: Invitation,
    sess: Session,  # noqa: ARG001 - part of the generic contract; this handler does not use it.
) -> None:
    """Creates the membership described by the invitation payload and leaves that
    workspace as the user's active one (new or existing).

    Both cases are logged at info level: a new membership, or a user who was
    already a member (the invitation is still consumed by the caller and the role
    does not change).

    It does not commit: the auth ceremony commits the whole transaction.
    """
    payload = inv.payload or {}
    workspace_id = payload.get("workspace_id")
    role = payload.get("role")

    if workspace_id and role in _VALID_ROLES:
        inserted = await service.add_member(
            db, workspace_id=workspace_id, user_id=user.id, role=role
        )
        if inserted:
            _log.info(
                "invitation %s accepted by user %s; joined workspace %s as %s",
                inv.id,
                user.id,
                workspace_id,
                role,
            )
        else:
            _log.info(
                "invitation %s accepted by user %s who is already a member of "
                "workspace %s; invitation consumed, role unchanged",
                inv.id,
                user.id,
                workspace_id,
            )
        # The user lands in the workspace they joined. They are always a member
        # here (just inserted or pre-existing), so it cannot return False.
        await service.set_active_workspace(db, user_id=user.id, workspace_id=workspace_id)
        return

    _log.warning(
        "invitation %s accepted by user %s with invalid membership payload "
        "(workspace_id=%r, role=%r); no membership created",
        inv.id,
        user.id,
        workspace_id,
        role,
    )
