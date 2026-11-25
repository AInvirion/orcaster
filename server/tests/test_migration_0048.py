"""Migration 0048 extends the two CHECK constraints of `admin_actions`.

The file has five tests: two verify that the migration added the new values to
each CHECK, and the other three verify synchronisation, because the CHECK is
duplicated in two code mirrors (`models.py` for documentary parity and
`admin_ui.py` as the universe of audit log filters). When a mirror falls
behind, actions are recorded and shown in the table but cannot be filtered.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.ext.asyncio import create_async_engine

# pg_get_constraintdef returns the literals between single quotes
# (`'pause_worker'::text`); the pattern captures only those.
_LITERAL_RE = re.compile(r"'([a-z_]+)'")

_NEW_ACTIONS = ("pause_worker", "resume_worker", "mint_share_token", "revoke_share_token")
# Values that were already present and that a badly copied DROP + recreate would remove.
_PRESERVED_ACTIONS = (
    "delete_worker",
    "cancel_task",
    "delete_task",
    "bulk_cancel_task",
    "bulk_delete_task",
    "retry_task",
    "auto_retry_task",
    "fork_task",
    "mint_pat",
    "revoke_pat",
    "delete_pat",
    "mint_enrollment_token",
    "revoke_enrollment_token",
    "delete_enrollment_token",
    "clear_worker_cache",
    "bulk_retry_task",
    "consume_enrollment_token",
)


async def _constraintdef(db_url: str, name: str) -> str:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            # conname is not globally unique: scope it by conrelid to admin_actions.
            return (
                await conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = :n AND conrelid = 'admin_actions'::regclass"
                    ),
                    {"n": name},
                )
            ).scalar_one()
    finally:
        await engine.dispose()


def _models_constraint_sqltext(name: str) -> str:
    from orcaster_server.models import AdminAction

    for c in AdminAction.__table__.constraints:
        if isinstance(c, CheckConstraint) and c.name == name:
            return str(c.sqltext)
    raise AssertionError(f"AdminAction does not declare the CheckConstraint {name}")


@pytest.mark.asyncio
async def test_0048_action_check_gains_new_values(migrated_db_url: str) -> None:
    definition = await _constraintdef(migrated_db_url, "admin_actions_action_check")
    for value in _NEW_ACTIONS:
        assert f"'{value}'" in definition, value
    for value in _PRESERVED_ACTIONS:
        assert f"'{value}'" in definition, value


@pytest.mark.asyncio
async def test_0048_target_kind_check_gains_share_token(migrated_db_url: str) -> None:
    definition = await _constraintdef(migrated_db_url, "admin_actions_target_kind_check")
    assert "'share_token'" in definition
    for value in ("worker", "task", "pat", "enrollment_token"):
        assert f"'{value}'" in definition, value


@pytest.mark.asyncio
async def test_action_filter_values_match_db_check(migrated_db_url: str) -> None:
    from orcaster_server.api.admin_ui import _ACTION_VALUES

    definition = await _constraintdef(migrated_db_url, "admin_actions_action_check")
    assert set(_ACTION_VALUES) == set(_LITERAL_RE.findall(definition))


@pytest.mark.asyncio
async def test_target_kind_filter_values_match_db_check(migrated_db_url: str) -> None:
    from orcaster_server.api.admin_ui import _TARGET_KIND_VALUES

    definition = await _constraintdef(migrated_db_url, "admin_actions_target_kind_check")
    assert set(_TARGET_KIND_VALUES) == set(_LITERAL_RE.findall(definition))


@pytest.mark.asyncio
async def test_models_admin_action_checks_match_db(migrated_db_url: str) -> None:
    for name in ("admin_actions_action_check", "admin_actions_target_kind_check"):
        db_values = set(_LITERAL_RE.findall(await _constraintdef(migrated_db_url, name)))
        model_values = set(_LITERAL_RE.findall(_models_constraint_sqltext(name)))
        assert model_values == db_values, name
