"""Factory of the auth module: assembles router + deps from an AuthConfig.

`base` is received as a portability contract (another host would pass its Base).
In this repo the models already inherit from AuthBase; the parameter is validated but
the real DDL is provided by the migration.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.models import User
from orcaster_server.auth.rate_limit import BeginRateLimiter, limiter_from_config
from orcaster_server.auth.service.session import resolve_session
from orcaster_server.db import get_session


@dataclass
class Auth:
    config: AuthConfig
    router: APIRouter
    require_session: object
    require_session_optional: object
    # Limiters for anonymous `begin` and `complete` calls (separate buckets, same
    # settings); exposed so tests can reset them.
    begin_limiter: BeginRateLimiter | None = None
    complete_limiter: BeginRateLimiter | None = None


def make_auth(base: type[DeclarativeBase], config: AuthConfig) -> Auth:
    async def require_session_optional(
        request: Request,
        db: Annotated[AsyncSession, Depends(get_session)],
    ) -> User | None:
        token = request.cookies.get(config.cookie_name)
        if not token:
            return None
        user = await resolve_session(db, token)
        if user is not None:
            await db.commit()
        return user

    async def require_session(
        user: Annotated[User | None, Depends(require_session_optional)],
    ) -> User:
        if user is None:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
            )
        return user

    from orcaster_server.auth.router import build_router

    # `complete` has its own instance, with the same settings,
    # so that an admitted `begin` always has its `complete` token.
    begin_limiter = limiter_from_config(config)
    complete_limiter = limiter_from_config(config)
    router = build_router(
        config,
        require_session,
        require_session_optional,
        begin_limiter,
        complete_limiter=complete_limiter,
    )
    return Auth(
        config=config,
        router=router,
        require_session=require_session,
        require_session_optional=require_session_optional,
        begin_limiter=begin_limiter,
        complete_limiter=complete_limiter,
    )
