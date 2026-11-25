"""Startup notice when secrets are stored without `TASK_SECRETS_KEYS_PATH`.

The server starts anyway (boot is not refused): the lifespan counts the `task_secrets`
rows and, if there are any, logs an ERROR saying those tasks are not delivered until
the file is configured. A failure of the count does not stop startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orcaster_server import main as main_module
from orcaster_server.config import settings
from orcaster_server.task_secrets import keyring, service
from orcaster_server.task_secrets.keyring import TaskSecretsKeyring
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID


@pytest.fixture
def lifespan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    monkeypatch.setattr(settings, "migrate_on_startup", False)
    monkeypatch.setattr(settings, "housekeeping_enabled", False)
    monkeypatch.setattr(settings, "sse_enabled", False)
    monkeypatch.setattr(main_module.signing, "is_signing_configured", lambda: True)
    monkeypatch.setattr(main_module.signing, "validate_signing_config", lambda: None)
    monkeypatch.setattr(main_module.storage, "ensure_artifacts_root", lambda p: None)
    keyring.reset_keyring_cache()


async def _clear_secrets(db_url: str) -> None:
    """The test database is shared between tests: the count starts from zero."""
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM task_secrets"))
    finally:
        await engine.dispose()


async def _store_secret(db_url: str) -> None:
    engine = create_async_engine(db_url)
    try:
        sm = async_sessionmaker(engine)
        async with sm() as s:
            uid = (
                await s.execute(
                    text("INSERT INTO users (display_name) VALUES ('Owner') RETURNING id")
                )
            ).scalar_one()
            await service.set_secret(
                s,
                TaskSecretsKeyring([Fernet.generate_key()]),
                workspace_id=DEFAULT_WORKSPACE_ID,
                name="API_TOKEN",
                value="hf_startup_value_must_not_leak",
                actor_user_id=str(uid),
            )
            await s.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_startup_errors_when_secrets_stored_without_keys(
    migrated_db_url: str,
    lifespan_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings, "database_url", migrated_db_url)
    await _clear_secrets(migrated_db_url)
    await _store_secret(migrated_db_url)
    with caplog.at_level(logging.INFO, logger="orcaster_server.main"):
        async with main_module.lifespan(FastAPI()):
            pass
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        "1 task secret(s) are stored but TASK_SECRETS_KEYS_PATH is not configured" in m
        for m in errors
    ), errors
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("TASK_SECRETS_KEYS_PATH not configured" in m for m in warnings)
    # Without keys, setting a secret returns 503, deleting works and tasks that declare
    # stored secrets stay queued: the notice says so.
    assert any(
        "(setting a secret returns 503; tasks that declare stored secrets stay queued)" in m
        for m in warnings
    ), warnings
    assert all("management returns 503" not in m for m in warnings)
    assert all("hf_startup_value_must_not_leak" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_has_no_error_without_stored_secrets(
    migrated_db_url: str,
    lifespan_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings, "database_url", migrated_db_url)
    await _clear_secrets(migrated_db_url)
    with caplog.at_level(logging.INFO, logger="orcaster_server.main"):
        async with main_module.lifespan(FastAPI()):
            pass
    assert not [r for r in caplog.records if "are stored but" in r.getMessage()]


@pytest.mark.asyncio
async def test_startup_survives_failed_secret_count(
    lifespan_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Closed port: the count cannot connect and startup continues.
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@127.0.0.1:1/none")
    reached = False
    with caplog.at_level(logging.INFO, logger="orcaster_server.main"):
        async with main_module.lifespan(FastAPI()):
            reached = True
    assert reached
    assert any("could not count stored task secrets" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_log_names_the_loaded_key_ids(
    migrated_db_url: str,
    lifespan_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """The startup line carries the non-secret ids of the keys, to compare
    them with the one `rotate --keys-file` prints."""
    k1, k2 = Fernet.generate_key(), Fernet.generate_key()
    keys_file = tmp_path / "task-secrets-keys"
    keys_file.write_text(k1.decode() + "\n" + k2.decode() + "\n")
    monkeypatch.setattr(settings, "database_url", migrated_db_url)
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(keys_file))
    keyring.reset_keyring_cache()
    try:
        with caplog.at_level(logging.INFO, logger="orcaster_server.main"):
            async with main_module.lifespan(FastAPI()):
                pass
    finally:
        keyring.reset_keyring_cache()
    id1, id2 = keyring.key_id(k1), keyring.key_id(k2)
    assert (
        "lifespan: task secrets keys loaded; central task secrets enabled "
        f"(primary key id={id1}, key ids={id1},{id2})"
    ) in [r.getMessage() for r in caplog.records]
    for r in caplog.records:
        assert k1.decode() not in r.getMessage() and k2.decode() not in r.getMessage()
