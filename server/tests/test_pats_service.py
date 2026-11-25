"""Tests for the Personal Access Token service.

They cover the contract of `orcaster_server/pats/service.py`:
- mint: plaintext token with the `orcaster_pat_` prefix, only the hash in the DB.
- resolve: dynamic role against `workspace_members`; invalid/non-member states;
  best-effort `last_used_at`.
- list/revoke/delete: role-asymmetric policy (mirrors enrollment).

They use the `db_session` fixture (no HTTP). The `Default` workspace is already seeded.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from orcaster_server.auth.config import hash_token
from orcaster_server.auth.rate_limit import KeyedBoundedWarning
from orcaster_server.pats import service as pats_service
from orcaster_server.pats.service import (
    PAT_TOKEN_PREFIX,
    PatState,
    delete_pat_by_id,
    list_pats,
    mint_pat,
    resolve_pat,
    revoke_pat_by_id,
)
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from orcaster_server.workspaces.service import add_member

pytestmark = pytest.mark.asyncio


async def _make_user(db, display_name: str = "Member") -> str:
    row = await db.execute(
        text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"),
        {"n": display_name},
    )
    return str(row.scalar_one())


async def _make_member(db, *, role: str = "member", display_name: str = "Member") -> str:
    user_id = await _make_user(db, display_name)
    await add_member(db, workspace_id=DEFAULT_WORKSPACE_ID, user_id=user_id, role=role)
    return user_id


async def _set_disabled(db, user_id: str, disabled: bool) -> None:
    """Disables (`disabled_at = now()`) or re-enables (`NULL`) the user and commits."""
    await db.execute(
        text("UPDATE users SET disabled_at = :d WHERE id = :u"),
        {"d": datetime.now(UTC) if disabled else None, "u": user_id},
    )
    await db.commit()


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def disabled_warning(monkeypatch):
    """Replaces the module's bounded WARNING with one using a controlled clock."""
    clock = _FakeClock()
    warn = KeyedBoundedWarning(
        pats_service.logger,
        pats_service._DISABLED_WARNING_TEMPLATE,
        interval=pats_service.DISABLED_WARNING_INTERVAL_S,
        max_keys=pats_service.DISABLED_WARNING_MAX_PATS,
        clock=clock,
    )
    monkeypatch.setattr(pats_service, "_disabled_warning", warn)
    yield clock, warn
    warn.reset()


def test_new_token_has_orcaster_prefix():
    from orcaster_server.pats.service import generate_pat_token

    assert PAT_TOKEN_PREFIX == "orcaster_pat_"
    assert generate_pat_token().startswith("orcaster_pat_")


# --- mint -----------------------------------------------------------------


async def test_mint_returns_prefixed_token_and_persists_only_hash(db_session):
    user_id = await _make_member(db_session, role="owner")
    token = await mint_pat(
        db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID, label="laptop"
    )
    await db_session.commit()

    assert token.startswith(PAT_TOKEN_PREFIX)
    # Only the hash lives in the DB, never the plaintext token.
    row = await db_session.execute(
        text("SELECT token_hash, label, expires_at FROM personal_access_tokens")
    )
    stored = row.one()
    assert stored.token_hash == hash_token(token)
    assert stored.token_hash != token
    assert stored.label == "laptop"
    assert stored.expires_at is None  # no expiry by default


async def test_mint_with_expiry_persists_expires_at(db_session):
    user_id = await _make_member(db_session, role="owner")
    exp = datetime.now(UTC) + timedelta(days=30)
    await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID, expires_at=exp)
    await db_session.commit()
    row = await db_session.execute(text("SELECT expires_at FROM personal_access_tokens"))
    assert row.scalar_one() is not None


# --- resolve --------------------------------------------------------------


async def test_resolve_ok_returns_user_workspace_and_dynamic_role(db_session):
    user_id = await _make_member(db_session, role="owner")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()

    res = await resolve_pat(db_session, token)
    assert res.state is PatState.OK
    assert res.user_id == user_id
    assert res.workspace_id == DEFAULT_WORKSPACE_ID
    assert res.role == "owner"


async def test_resolve_unknown_token_is_invalid(db_session):
    res = await resolve_pat(db_session, "orcaster_pat_does-not-exist")
    assert res.state is PatState.INVALID


async def test_resolve_garbage_without_prefix_is_invalid(db_session):
    res = await resolve_pat(db_session, "not-a-pat")
    assert res.state is PatState.INVALID


async def test_resolve_unsupported_legacy_prefix_has_its_own_state(db_session):
    """The unsupported legacy prefix is rejected with a distinct state, so the
    caller can say why; it is never resolved against the database."""
    res = await resolve_pat(db_session, "taskor_pat_does-not-exist")
    assert res.state is PatState.LEGACY_PREFIX
    assert res.user_id is None


async def test_resolve_revoked_is_invalid(db_session):
    user_id = await _make_member(db_session, role="member")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    await db_session.execute(text("UPDATE personal_access_tokens SET revoked_at = now()"))
    await db_session.commit()
    res = await resolve_pat(db_session, token)
    assert res.state is PatState.INVALID


async def test_resolve_expired_is_invalid(db_session):
    user_id = await _make_member(db_session, role="member")
    past = datetime.now(UTC) - timedelta(seconds=1)
    token = await mint_pat(
        db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID, expires_at=past
    )
    await db_session.commit()
    res = await resolve_pat(db_session, token)
    assert res.state is PatState.INVALID


async def test_resolve_non_member_is_forbidden(db_session):
    # User with a PAT but no current membership (removed from the workspace).
    user_id = await _make_member(db_session, role="member")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    await db_session.execute(
        text("DELETE FROM workspace_members WHERE user_id = :u"), {"u": user_id}
    )
    await db_session.commit()
    res = await resolve_pat(db_session, token)
    assert res.state is PatState.NOT_MEMBER


@pytest.mark.parametrize("role", ["owner", "member"])
async def test_resolve_disabled_owner_is_invalid_and_leaves_pat_untouched(db_session, role):
    # A disabled owner invalidates the PAT whatever its role; the rejection does not
    # write anything (it neither revokes the token nor overwrites `last_used_at`).
    user_id = await _make_member(db_session, role=role)
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    await _set_disabled(db_session, user_id, True)

    res = await resolve_pat(db_session, token)
    assert res.state is PatState.INVALID
    assert res.user_id is None and res.workspace_id is None and res.role is None

    await db_session.commit()
    row = (
        await db_session.execute(
            text("SELECT revoked_at, last_used_at FROM personal_access_tokens")
        )
    ).one()
    assert row.revoked_at is None
    assert row.last_used_at is None


async def test_resolve_disabled_owner_logs_one_bounded_warning_per_window(
    db_session, disabled_warning, caplog
):
    # A client using the PAT of a disabled user does not produce one line
    # per request: one per (PAT id) and window, and the suppressed count goes out in the
    # next one. Both ids, never the token.
    clock, _ = disabled_warning
    user_id = await _make_member(db_session, role="member")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    pat_id = str(
        (await db_session.execute(text("SELECT id FROM personal_access_tokens"))).scalar_one()
    )
    await _set_disabled(db_session, user_id, True)

    with caplog.at_level(logging.WARNING, logger="orcaster_server.pats.service"):
        for _ in range(50):
            assert (await resolve_pat(db_session, token)).state is PatState.INVALID
        first = [r for r in caplog.records if r.name == "orcaster_server.pats.service"]
        assert len(first) == 1
        msg = first[0].getMessage()
        assert user_id in msg and pat_id in msg
        assert token not in msg and hash_token(token) not in msg
        assert "1 request" in msg

        # Within the window nothing more is emitted, even as requests keep arriving.
        clock.now += pats_service.DISABLED_WARNING_INTERVAL_S - 1
        await resolve_pat(db_session, token)
        assert len([r for r in caplog.records if r.name == "orcaster_server.pats.service"]) == 1

        # Once the window closes, the next request emits the suppressed count
        # (49 earlier + the one at the window boundary + this one = 51).
        clock.now += 2
        await resolve_pat(db_session, token)
    lines = [r.getMessage() for r in caplog.records if r.name == "orcaster_server.pats.service"]
    assert len(lines) == 2
    assert "51 request" in lines[1]
    assert user_id in lines[1] and pat_id in lines[1] and token not in lines[1]


async def test_resolve_disabled_owner_warning_is_per_pat(db_session, disabled_warning, caplog):
    # The window is per PAT id: two tokens of the same disabled user
    # yield one line each (the operator sees how many clients are still in use).
    user_id = await _make_member(db_session, role="member")
    t1 = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    t2 = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    await _set_disabled(db_session, user_id, True)

    with caplog.at_level(logging.WARNING, logger="orcaster_server.pats.service"):
        for _ in range(5):
            await resolve_pat(db_session, t1)
            await resolve_pat(db_session, t2)
    lines = [r.getMessage() for r in caplog.records if r.name == "orcaster_server.pats.service"]
    assert len(lines) == 2
    assert len({line for line in lines}) == 2  # distinct PAT ids


async def test_resolve_disabled_owner_warning_state_is_bounded(
    db_session, disabled_warning, caplog, monkeypatch
):
    # The bounded WARNING's memory has a cap of distinct PATs (LRU).
    clock, _ = disabled_warning
    small = KeyedBoundedWarning(
        pats_service.logger,
        pats_service._DISABLED_WARNING_TEMPLATE,
        interval=pats_service.DISABLED_WARNING_INTERVAL_S,
        max_keys=2,
        clock=clock,
    )
    monkeypatch.setattr(pats_service, "_disabled_warning", small)
    user_id = await _make_member(db_session, role="member")
    tokens = [
        await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
        for _ in range(5)
    ]
    await db_session.commit()
    await _set_disabled(db_session, user_id, True)

    with caplog.at_level(logging.WARNING, logger="orcaster_server.pats.service"):
        for token in tokens:
            await resolve_pat(db_session, token)
            assert len(small._entries) <= 2
    small.reset()


async def test_resolve_enabled_owner_and_unknown_pat_write_no_warning(
    db_session, disabled_warning, caplog
):
    # Only the rejection for a disabled owner writes the line.
    user_id = await _make_member(db_session, role="member")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    with caplog.at_level(logging.WARNING, logger="orcaster_server.pats.service"):
        assert (await resolve_pat(db_session, token)).state is PatState.OK
        assert (await resolve_pat(db_session, PAT_TOKEN_PREFIX + "nope")).state is PatState.INVALID
    assert not [r for r in caplog.records if r.name == "orcaster_server.pats.service"]


async def test_resolve_disabled_non_member_is_invalid_not_forbidden(db_session):
    # The disabled-user check goes BEFORE the membership one. A disabled owner
    # who is also no longer a member gets INVALID (generic 401), not
    # NOT_MEMBER (403); the same case with an enabled user is still NOT_MEMBER.
    user_id = await _make_member(db_session, role="member")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    await db_session.execute(
        text("DELETE FROM workspace_members WHERE user_id = :u"), {"u": user_id}
    )
    await db_session.commit()

    await _set_disabled(db_session, user_id, True)
    assert (await resolve_pat(db_session, token)).state is PatState.INVALID

    await _set_disabled(db_session, user_id, False)
    assert (await resolve_pat(db_session, token)).state is PatState.NOT_MEMBER


async def test_resolve_reenabled_user_restores_pat_and_sets_last_used_at(db_session):
    # Disabling leaves no trace on the PAT, so when the user is re-enabled
    # the same token resolves OK and only then is `last_used_at` recorded.
    user_id = await _make_member(db_session, role="member")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()

    await _set_disabled(db_session, user_id, True)
    assert (await resolve_pat(db_session, token)).state is PatState.INVALID

    await _set_disabled(db_session, user_id, False)
    res = await resolve_pat(db_session, token)
    assert res.state is PatState.OK
    assert res.user_id == user_id
    assert res.token_last_used_at is None  # no previous use (the rejection did not write it)
    await db_session.commit()
    row = await db_session.execute(text("SELECT last_used_at FROM personal_access_tokens"))
    assert row.scalar_one() is not None


async def test_resolve_reflects_role_demotion_dynamically(db_session):
    # The role is not frozen; lowering owner->member shows up instantly.
    user_id = await _make_member(db_session, role="owner")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    await db_session.execute(
        text("UPDATE workspace_members SET role = 'member' WHERE user_id = :u"),
        {"u": user_id},
    )
    await db_session.commit()
    res = await resolve_pat(db_session, token)
    assert res.state is PatState.OK
    assert res.role == "member"


async def test_resolve_updates_last_used_at(db_session):
    user_id = await _make_member(db_session, role="member")
    token = await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    res = await resolve_pat(db_session, token)
    assert res.state is PatState.OK
    await db_session.commit()
    row = await db_session.execute(text("SELECT last_used_at FROM personal_access_tokens"))
    assert row.scalar_one() is not None


# --- list / revoke / delete (role asymmetry) ------------------------------


async def test_list_member_sees_only_own_owner_sees_all(db_session):
    owner_id = await _make_member(db_session, role="owner", display_name="Owner")
    member_id = await _make_member(db_session, role="member", display_name="Member")
    await mint_pat(db_session, user_id=owner_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await mint_pat(db_session, user_id=member_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()

    # Member: filters by their user_id -> sees only their own.
    member_view = await list_pats(db_session, DEFAULT_WORKSPACE_ID, user_id=member_id)
    assert len(member_view) == 1
    assert member_view[0].user_id == member_id

    # Owner: no filter -> sees all of the workspace's.
    owner_view = await list_pats(db_session, DEFAULT_WORKSPACE_ID)
    assert len(owner_view) == 2


async def test_revoke_member_cannot_revoke_others(db_session):
    owner_id = await _make_member(db_session, role="owner", display_name="Owner")
    member_id = await _make_member(db_session, role="member", display_name="Member")
    await mint_pat(db_session, user_id=owner_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    pat_id = (await db_session.execute(text("SELECT id FROM personal_access_tokens"))).scalar_one()

    # Member tries to revoke the owner's PAT -> False (handler responds 404).
    ok = await revoke_pat_by_id(
        db_session,
        token_id=str(pat_id),
        workspace_id=DEFAULT_WORKSPACE_ID,
        user_id=member_id,
    )
    assert ok is False

    # Owner can revoke any in the workspace.
    ok_owner = await revoke_pat_by_id(
        db_session, token_id=str(pat_id), workspace_id=DEFAULT_WORKSPACE_ID
    )
    assert ok_owner is True


async def test_delete_only_revoked_pat(db_session):
    user_id = await _make_member(db_session, role="member")
    await mint_pat(db_session, user_id=user_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    pat_id = (await db_session.execute(text("SELECT id FROM personal_access_tokens"))).scalar_one()

    # Live PAT (not revoked, not expired) is NOT deletable -> False.
    assert (
        await delete_pat_by_id(
            db_session, token_id=str(pat_id), workspace_id=DEFAULT_WORKSPACE_ID, user_id=user_id
        )
        is False
    )

    # After revoking, it is deletable.
    await db_session.execute(text("UPDATE personal_access_tokens SET revoked_at = now()"))
    await db_session.commit()
    assert (
        await delete_pat_by_id(
            db_session, token_id=str(pat_id), workspace_id=DEFAULT_WORKSPACE_ID, user_id=user_id
        )
        is True
    )
