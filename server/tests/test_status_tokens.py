"""Status page token service: create/list/revoke/resolve.

Uses the app's live engine (app_client fixture) and its sessionmaker, like the
mint_*_via_app helpers in conftest.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from orcaster_server import db as app_db
from orcaster_server.status.tokens import (
    create_share_token,
    generate_share_token,
    hash_share_token,
    list_share_tokens,
    resolve_share_token,
    revoke_share_token,
)
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

_WS = DEFAULT_WORKSPACE_ID


def test_generate_is_opaque_and_hash_is_sha256_hex() -> None:
    tok = generate_share_token()
    assert len(tok) >= 32
    h = hash_share_token(tok)
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert hash_share_token(tok) == h  # deterministic


@pytest.mark.asyncio
async def test_create_stores_only_hash_and_resolves(app_client) -> None:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token, token_id = await create_share_token(s, workspace_id=_WS, label="Acme")
        await s.commit()
    # Only the hash lives in the DB, never the plaintext secret.
    async with sm() as s:
        row = (
            (
                await s.execute(
                    text("SELECT token_hash, label FROM workspace_share_token WHERE id = :i"),
                    {"i": token_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["token_hash"] == hash_share_token(token)
    assert row["token_hash"] != token
    assert row["label"] == "Acme"
    # resolve returns the workspace and stamps last_seen_at.
    async with sm() as s:
        ws = await resolve_share_token(s, token)
        await s.commit()
    assert ws == _WS
    async with sm() as s:
        seen = (
            await s.execute(
                text("SELECT last_seen_at FROM workspace_share_token WHERE id = :i"),
                {"i": token_id},
            )
        ).scalar_one()
    assert seen is not None


@pytest.mark.asyncio
async def test_resolve_throttle_skips_write_within_window(app_client) -> None:
    """A second immediate resolve must NOT modify last_seen_at (within the threshold)."""
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token, token_id = await create_share_token(s, workspace_id=_WS, label="throttle-test")
        await s.commit()

    # First resolve: last_seen_at was NULL, the UPDATE does write.
    async with sm() as s:
        ws = await resolve_share_token(s, token)
        await s.commit()
    assert ws == _WS

    # Capture last_seen_at after the first resolve.
    async with sm() as s:
        seen_after_first = (
            await s.execute(
                text("SELECT last_seen_at FROM workspace_share_token WHERE id = :i"),
                {"i": token_id},
            )
        ).scalar_one()
    assert seen_after_first is not None

    # Second immediate resolve: within the throttle window → must NOT write.
    async with sm() as s:
        ws2 = await resolve_share_token(s, token)
        await s.commit()
    assert ws2 == _WS  # still resolves even without a touch

    async with sm() as s:
        seen_after_second = (
            await s.execute(
                text("SELECT last_seen_at FROM workspace_share_token WHERE id = :i"),
                {"i": token_id},
            )
        ).scalar_one()

    # last_seen_at did not change because the second resolve fell within the threshold.
    assert seen_after_second == seen_after_first


@pytest.mark.asyncio
async def test_resolve_throttle_writes_when_stale(app_client) -> None:
    """If last_seen_at is old enough, a later resolve does update it."""
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token, token_id = await create_share_token(s, workspace_id=_WS, label="stale-test")
        await s.commit()

    # First resolve to set last_seen_at.
    async with sm() as s:
        ws = await resolve_share_token(s, token)
        await s.commit()
    assert ws == _WS

    # Backdate last_seen_at by 2 minutes (exceeds the 60 s threshold).
    async with sm() as s:
        await s.execute(
            text(
                "UPDATE workspace_share_token"
                "   SET last_seen_at = now() - interval '2 minutes'"
                " WHERE id = :i"
            ),
            {"i": token_id},
        )
        await s.commit()

    async with sm() as s:
        stale_ts = (
            await s.execute(
                text("SELECT last_seen_at FROM workspace_share_token WHERE id = :i"),
                {"i": token_id},
            )
        ).scalar_one()

    # Later resolve: last_seen_at exceeds the threshold → the UPDATE must write.
    async with sm() as s:
        ws2 = await resolve_share_token(s, token)
        await s.commit()
    assert ws2 == _WS

    async with sm() as s:
        updated_ts = (
            await s.execute(
                text("SELECT last_seen_at FROM workspace_share_token WHERE id = :i"),
                {"i": token_id},
            )
        ).scalar_one()

    # last_seen_at must have advanced relative to the backdated value.
    assert updated_ts > stale_ts


@pytest.mark.asyncio
async def test_resolve_revoked_and_nonexistent_return_none(app_client) -> None:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token, token_id = await create_share_token(s, workspace_id=_WS, label=None)
        await s.commit()
    # Nonexistent.
    async with sm() as s:
        assert await resolve_share_token(s, "definitely-not-a-real-token") is None
    # Revoked -> None.
    async with sm() as s:
        ok = await revoke_share_token(s, token_id=token_id, workspace_id=_WS)
        await s.commit()
    assert ok is True
    async with sm() as s:
        assert await resolve_share_token(s, token) is None


@pytest.mark.asyncio
async def test_list_shows_metadata_without_hash(app_client) -> None:
    sm = app_db.get_sessionmaker()
    # Separate inserts to guarantee distinct created_at (ORDER BY created_at DESC).
    async with sm() as s:
        await create_share_token(s, workspace_id=_WS, label="one")
        await s.commit()
    async with sm() as s:
        await create_share_token(s, workspace_id=_WS, label="two")
        await s.commit()
    async with sm() as s:
        rows = await list_share_tokens(s, workspace_id=_WS, keyring=None)
    assert len(rows) == 2
    assert {r["label"] for r in rows} == {"one", "two"}
    # "two" was inserted later, it must appear first (ORDER BY created_at DESC).
    assert rows[0]["label"] == "two"
    for r in rows:
        assert "token_hash" not in r
        assert {"id", "label", "created_at", "last_seen_at", "revoked_at"} <= set(r)


@pytest.mark.asyncio
async def test_resolve_disabled_workspace_returns_none(app_client) -> None:
    """A token of a disabled workspace must resolve to None."""
    sm = app_db.get_sessionmaker()
    # Create a disabled workspace.
    async with sm() as s:
        disabled_ws_id = (
            await s.execute(
                text(
                    """
                    INSERT INTO workspaces (name, slug, disabled_at)
                    VALUES ('Disabled WS', 'disabled-ws-token-test', now())
                    RETURNING id::text
                    """
                )
            )
        ).scalar_one()
        await s.commit()
    # Create a token for that workspace.
    async with sm() as s:
        token, _ = await create_share_token(s, workspace_id=disabled_ws_id, label="test")
        await s.commit()
    # resolve must return None because the workspace is disabled.
    async with sm() as s:
        result = await resolve_share_token(s, token)
    assert result is None
