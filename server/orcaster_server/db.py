from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from orcaster_server.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> None:
    global _engine, _sessionmaker
    if settings.database_url is None:
        _engine = None
        _sessionmaker = None
        return
    _engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def get_engine() -> AsyncEngine | None:
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession] | None:
    return _sessionmaker


async def session() -> AsyncIterator[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialized (DATABASE_URL missing)")
    async with _sessionmaker() as s:
        yield s


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield an `AsyncSession` with explicit commit.

    The handler decides whether to `await session.commit()`. If it raises, the
    `async with` block rolls back automatically.
    """
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialized (DATABASE_URL missing)")
    async with _sessionmaker() as s:
        yield s


async def ping() -> bool:
    if _engine is None:
        return False
    async with _engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
