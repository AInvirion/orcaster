"""Dev helper: creates an invitation and shows the acceptance link.

There is no HTTP endpoint to issue invitations. To try the passkey registration
flow locally, this script inserts an invitation by hand against the dev DB and
prints the link.

Usage:
    orcaster-dev-invite
    orcaster-dev-invite --label someone@example.com
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.invitation import create_invitation
from orcaster_server.config import settings


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Create a dev invitation")
    parser.add_argument("--label", default="dev@local", help="informational label")
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
        token = await create_invitation(
            db, cfg, payload={}, label=args.label, created_by_user_id=None
        )
        await db.commit()
    await engine.dispose()

    print("\n  Invitation created. Open this link in the browser:\n")
    print(f"    {settings.webauthn_origin}/auth/accept/{token}\n")


def main() -> None:
    """Console entry point."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()
