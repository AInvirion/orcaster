"""Alembic env.py — async (asyncpg).

The database URL is read from the `DATABASE_URL` environment variable, which
takes priority over `sqlalchemy.url` in alembic.ini (that one only works as a
fallback, with `${DATABASE_URL}` interpolation). The URL must use the
`postgresql+asyncpg://...` driver.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from orcaster_server.migration_lock import MIGRATION_LOCK_KEY

config = context.config

# `fileConfig` only when alembic runs as a CLI. Called inside the server process
# (lifespan, `orcaster_server.main._apply_migrations`) it applies `alembic.ini` over
# uvicorn's logging config: with its default `disable_existing_loggers=True`
# it disables every logger already created (`orcaster_server.main`, `uvicorn.error`) and
# replaces the root handlers and level. The programmatic caller turns it off with
# `Config.attributes["configure_logger"] = False`; the default keeps the CLI
# behaviour.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# Load .env from the working directory if it exists (for local development
# without exporting the var).
_env_file = Path.cwd() / ".env"
if _env_file.exists() and not os.environ.get("DATABASE_URL"):
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Allow override through the env var (preferred in tests/CI/local).
database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

# Raw SQL: no metadata is needed for autogenerate.
target_metadata = None


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Serializes concurrent `upgrade head` runs (e.g. uvicorn --workers N that
        # migrate at startup at the same time): without this, the one that loses the
        # race collides with the DDL just applied by the winner (DuplicateColumnError)
        # and the process dies. pg_advisory_xact_lock is exclusive and is released only
        # at the COMMIT of this transaction; the rest block here until the winner
        # finishes and, on continuing (READ COMMITTED), already see alembic_version at head.
        connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_KEY})")
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    """Offline mode (pure SQL) is not used. If invoked, fail explicitly."""
    raise RuntimeError(
        "Offline mode not supported: migrations require an async connection to Postgres."
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
