"""Bootstrap of the first owner: issues an owner invitation WITHOUT a public route.

Same pattern as `orcaster_server/cli/dev_invite.py`: it inserts the invitation
by hand against the DB and prints the acceptance link. There is no public
bootstrap route; the operator accepts the link by passkey at
`/auth/accept/{token}` and the `owner` membership is created by the
invitation -> membership seam.

Usage:
    orcaster-bootstrap-owner
    orcaster-bootstrap-owner --name "Acme"
    orcaster-bootstrap-owner --name "Acme" --label ada@acme.cl

Without `--name` it adopts the `Default` workspace (slug `default`); with
`--name` it creates a new one, deriving the slug from the name.

**Idempotence:** the path without `--name` (Default) IS idempotent: re-running
adopts the same workspace and only issues a fresh invitation. The path with
`--name` is NOT idempotent **by design**: each run creates a NEW workspace; if
the derived slug already exists, a short suffix is added (`acme`, `acme-1a2b`,
...) to avoid a collision. Re-running with the same `--name` therefore produces
different workspaces (each with its pending owner invitation). To re-issue the
invitation of an existing named workspace, do it through the invitations UI/seam,
not by re-running this script.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.invitation import create_invitation
from orcaster_server.config import settings
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from orcaster_server.workspaces.models import Workspace
from orcaster_server.workspaces.service import create_workspace

DEFAULT_LABEL = "bootstrap owner"


def slugify(name: str) -> str:
    """Derive a slug: lowercase, non-alphanumerics -> `-`, collapse and trim."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


async def _slug_exists(db: AsyncSession, slug: str) -> bool:
    res = await db.execute(select(Workspace.id).where(Workspace.slug == slug))
    return res.scalar_one_or_none() is not None


async def _resolve_unique_slug(db: AsyncSession, name: str) -> str:
    """Slug derived from `name`, with a short suffix if it already exists."""
    base = slugify(name) or "workspace"
    slug = base
    while await _slug_exists(db, slug):
        slug = f"{base}-{secrets.token_hex(2)}"
    return slug


async def bootstrap(
    db: AsyncSession,
    cfg: AuthConfig,
    *,
    name: str | None = None,
    label: str = DEFAULT_LABEL,
) -> tuple[str, str, str]:
    """Resolve the workspace and issue the owner invitation.

    Without `name` it adopts the `Default` workspace (idempotent). With `name` it
    creates a NEW one every time (slug suffixed on collision), NOT idempotent by
    design; see the module docstring. Returns
    `(workspace_id, workspace_name, token)`. Does not commit (the caller decides).
    """
    if name is not None:
        slug = await _resolve_unique_slug(db, name)
        ws_id = await create_workspace(db, name=name, slug=slug, created_by_user_id=None)
        ws_name = name
    else:
        res = await db.execute(
            select(Workspace.id, Workspace.name).where(Workspace.id == DEFAULT_WORKSPACE_ID)
        )
        row = res.one_or_none()
        if row is None:
            raise SystemExit(
                "The Default workspace does not exist. Run the migrations first "
                "(orcaster-migrate) or pass --name to create a new one."
            )
        ws_id, ws_name = str(row[0]), str(row[1])

    token = await create_invitation(
        db,
        cfg,
        payload={"workspace_id": ws_id, "role": "owner"},
        label=label,
        created_by_user_id=None,
        ttl_seconds=cfg.bootstrap_invitation_ttl_seconds,
    )
    return ws_id, ws_name, token


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the first owner of a workspace")
    parser.add_argument(
        "--name",
        default=None,
        help=(
            "create a NEW workspace with this name (not idempotent: re-running "
            "creates another with a suffixed slug); without it, adopts 'Default' (idempotent)"
        ),
    )
    parser.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        help="informational label of the invitation",
    )
    args = parser.parse_args()

    if not settings.database_url:
        raise SystemExit(
            "DATABASE_URL is not set (environment variable or .env in the working directory)"
        )

    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    cfg = AuthConfig(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        origin=settings.webauthn_origin,
    )
    async with maker() as db:
        ws_id, ws_name, token = await bootstrap(db, cfg, name=args.name, label=args.label)
        await db.commit()
    await engine.dispose()

    print(f"\n  Workspace: {ws_name} ({ws_id})")
    print("\n  Open this link in the browser and register with a passkey:\n")
    print(f"    {settings.webauthn_origin}/auth/accept/{token}\n")
    print("  Accepting this link with your passkey makes you the owner of the workspace.\n")


def main() -> None:
    """Console entry point."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()
