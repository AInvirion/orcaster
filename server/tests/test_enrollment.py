"""Tests for single-use enrollment tokens + `POST /v1/workers/register`.

Covers:
- A registered worker inherits the token's `workspace_id` (non-Default workspace).
- Invalid / expired / revoked / already used token -> 401 (generic message).
- Atomic consumption under concurrency: two `register` calls with the same token ->
  exactly one wins; the other gets 401; a single worker row consumed the token.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orcaster_server.auth.config import hash_token
from orcaster_server.workspaces import service
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from orcaster_server.workspaces.enrollment import (
    mint_enrollment_token,
)

REGISTER = {
    "hostname": "w",
    "os": "linux",
    "arch": "x86_64",
    "ram_mb": 1024,
    "disk_mb": 10000,
    "tags": [],
    "capabilities": {},
}


def _body(token: str, **overrides) -> dict:
    return {**REGISTER, "enrollment_token": token, **overrides}


@pytest.mark.asyncio
async def test_register_sets_workspace_from_token(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """The worker ends up with the token's workspace_id (not the Default)."""
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            ws_id = await service.create_workspace(s, name="Acme", slug="acme")
            token = await mint_enrollment_token(s, workspace_id=ws_id)
            await s.commit()
    finally:
        await engine.dispose()

    assert ws_id != DEFAULT_WORKSPACE_ID
    resp = await app_client.post("/v1/workers/register", json=_body(token))
    assert resp.status_code == 200, resp.text
    worker_id = resp.json()["worker_id"]

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT w.workspace_id, t.used_by_worker_id FROM workers w "
                        "JOIN worker_enrollment_tokens t ON t.used_by_worker_id = w.id "
                        "WHERE w.id = :wid"
                    ),
                    {"wid": worker_id},
                )
            ).first()
    finally:
        await engine.dispose()
    assert row is not None
    assert str(row[0]) == ws_id
    assert str(row[1]) == worker_id


@pytest.mark.asyncio
async def test_register_audits_token_consumption(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Consuming the token leaves a row in admin_actions (forensic trail)."""
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            ws_id = await service.create_workspace(s, name="AuditWS", slug="audit-ws")
            token = await mint_enrollment_token(s, workspace_id=ws_id, label="ci-runner")
            await s.commit()
    finally:
        await engine.dispose()

    resp = await app_client.post("/v1/workers/register", json=_body(token))
    assert resp.status_code == 200, resp.text
    worker_id = resp.json()["worker_id"]

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT a.action, a.target_kind, a.target_id, "
                        "a.workspace_id, a.actor, a.payload->>'worker_id' AS pworker, "
                        "a.payload->>'label' AS plabel, t.id AS token_id "
                        "FROM admin_actions a "
                        "JOIN worker_enrollment_tokens t ON t.token_hash = :h "
                        "WHERE a.action = 'consume_enrollment_token'"
                    ),
                    {"h": hash_token(token)},
                )
            ).first()
    finally:
        await engine.dispose()

    assert row is not None, "the audit row for the consumption was not recorded"
    action, target_kind, target_id, ws, actor, pworker, plabel, token_id = row
    assert action == "consume_enrollment_token"
    assert target_kind == "enrollment_token"
    assert str(target_id) == str(token_id)
    assert str(ws) == ws_id
    assert actor is None
    assert pworker == worker_id
    assert plabel == "ci-runner"


@pytest.mark.asyncio
async def test_register_invalid_token_is_401(app_client: AsyncClient) -> None:
    resp = await app_client.post("/v1/workers/register", json=_body("never-minted"))
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_register_missing_token_is_422(app_client: AsyncClient) -> None:
    resp = await app_client.post("/v1/workers/register", json=REGISTER)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_register_expired_token_is_401(app_client: AsyncClient, migrated_db_url: str) -> None:
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            # Negative TTL -> expires in the past.
            token = await mint_enrollment_token(
                s, workspace_id=DEFAULT_WORKSPACE_ID, ttl_seconds=-60
            )
            await s.commit()
    finally:
        await engine.dispose()

    resp = await app_client.post("/v1/workers/register", json=_body(token))
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_register_revoked_token_is_401(app_client: AsyncClient, migrated_db_url: str) -> None:
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID)
            # Revoke the token with a direct UPDATE (sets revoked_at). The
            # endpoint must answer 401 for a revoked token.
            await s.execute(
                text(
                    "UPDATE worker_enrollment_tokens SET revoked_at = now() WHERE token_hash = :h"
                ),
                {"h": hash_token(token)},
            )
            await s.commit()
    finally:
        await engine.dispose()

    resp = await app_client.post("/v1/workers/register", json=_body(token))
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_register_already_used_token_is_401(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID)
            await s.commit()
    finally:
        await engine.dispose()

    first = await app_client.post("/v1/workers/register", json=_body(token, hostname="a"))
    assert first.status_code == 200, first.text
    second = await app_client.post("/v1/workers/register", json=_body(token, hostname="b"))
    assert second.status_code == 401, second.text


@pytest.mark.asyncio
async def test_register_concurrent_same_token_single_winner(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """Two concurrent register calls with the same token: exactly one wins.

    Consumption is an atomic conditional UPDATE (mirrors invitations), so
    even if both pass the peek, only one wins the RETURNING; the loser gets 401 and
    its freshly flushed worker is rolled back.
    """
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID)
            await s.commit()
    finally:
        await engine.dispose()

    r1, r2 = await asyncio.gather(
        app_client.post("/v1/workers/register", json=_body(token, hostname="c1")),
        app_client.post("/v1/workers/register", json=_body(token, hostname="c2")),
    )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 401], (r1.status_code, r1.text, r2.status_code, r2.text)

    # Only one worker was persisted for this race's hostnames, and the token
    # points to that single worker.
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            n_workers = (
                await conn.execute(
                    text("SELECT count(*) FROM workers WHERE hostname IN ('c1', 'c2')")
                )
            ).scalar_one()
            used_by = (
                await conn.execute(
                    text(
                        "SELECT used_by_worker_id FROM worker_enrollment_tokens "
                        "WHERE token_hash = :h"
                    ),
                    {"h": hashlib.sha256(token.encode()).hexdigest()},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert n_workers == 1, f"expected 1 worker created, found {n_workers}"
    assert used_by is not None


@pytest.mark.asyncio
async def test_future_expiry_is_not_expired(app_client: AsyncClient, migrated_db_url: str) -> None:
    """A token expiring in the near future is still valid (boundary sanity check)."""
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(
                s, workspace_id=DEFAULT_WORKSPACE_ID, ttl_seconds=3600
            )
            await s.commit()
    finally:
        await engine.dispose()

    # Sanity: the PERSISTED expires_at is in the future (not trivial arithmetic).
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            expires_at = (
                await conn.execute(
                    text("SELECT expires_at FROM worker_enrollment_tokens WHERE token_hash = :h"),
                    {"h": hashlib.sha256(token.encode()).hexdigest()},
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    assert expires_at > datetime.now(UTC)
    resp = await app_client.post("/v1/workers/register", json=_body(token))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_mint_defaults_single_use(migrated_db_url: str) -> None:
    """A mint without arguments leaves max_uses=1, use_count=0 (single-use)."""
    from orcaster_server.workspaces.enrollment import peek_enrollment_token

    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID)
            await s.commit()
        async with maker() as s:
            tok = await peek_enrollment_token(s, token)
            assert tok.max_uses == 1
            assert tok.use_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mint_with_max_uses(migrated_db_url: str) -> None:
    from orcaster_server.workspaces.enrollment import peek_enrollment_token

    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID, max_uses=5)
            await s.commit()
        async with maker() as s:
            tok = await peek_enrollment_token(s, token)
            assert tok.max_uses == 5
            assert tok.use_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_consume_multiuse_until_exhausted(migrated_db_url: str) -> None:
    from sqlalchemy import func, select

    from orcaster_server.workspaces.enrollment import EnrollmentState, consume_enrollment_token
    from orcaster_server.workspaces.models import WorkerEnrollmentTokenUse

    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID, max_uses=2)
            await s.commit()
        # Two consumptions OK, the third EXHAUSTED. worker_id=None: the column is
        # nullable (FK to workers ON DELETE SET NULL); using None avoids having to
        # create real worker rows in this unit test of counting/state.
        # The path with real workers is covered by the register tests over HTTP.
        async with maker() as s:
            st1, tok1 = await consume_enrollment_token(s, token, worker_id=None)
            st2, _ = await consume_enrollment_token(s, token, worker_id=None)
            st3, _ = await consume_enrollment_token(s, token, worker_id=None)
            await s.commit()
            assert st1 is EnrollmentState.OK
            assert st2 is EnrollmentState.OK
            assert st3 is EnrollmentState.EXHAUSTED
        # Exactly 2 usage rows were recorded for THIS token. Filter
        # by token_id (instead of counting the whole table) because `migrated_db_url`
        # is session-scoped and other tests in this file (those using
        # `app_client`) leave their own usage rows between tests; a global
        # COUNT(*) would be fragile against the suite's execution order.
        async with maker() as s:
            n = (
                await s.execute(
                    select(func.count())
                    .select_from(WorkerEnrollmentTokenUse)
                    .where(WorkerEnrollmentTokenUse.token_id == tok1.id)
                )
            ).scalar_one()
            assert n == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_register_multiuse_token_n_workers(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """A max_uses=3 token enrolls 3 workers; the 4th -> 401."""
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID, max_uses=3)
            await s.commit()
    finally:
        await engine.dispose()

    for i in range(3):
        r = await app_client.post("/v1/workers/register", json=_body(token, hostname=f"m{i}"))
        assert r.status_code == 200, r.text
    over = await app_client.post("/v1/workers/register", json=_body(token, hostname="over"))
    assert over.status_code == 401, over.text


@pytest.mark.asyncio
async def test_register_multiuse_concurrent_exactly_n_winners(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    """4 concurrent register calls with a max_uses=2 token -> exactly 2 win."""
    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID, max_uses=2)
            await s.commit()
    finally:
        await engine.dispose()

    results = await asyncio.gather(
        *[
            app_client.post("/v1/workers/register", json=_body(token, hostname=f"cc{i}"))
            for i in range(4)
        ]
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 200, 401, 401], [r.status_code for r in results]


@pytest.mark.asyncio
async def test_partial_token_still_active_and_not_deletable(
    app_client: AsyncClient, migrated_db_url: str
) -> None:
    from orcaster_server.workspaces.enrollment import (
        delete_enrollment_token_by_id,
        list_enrollment_tokens,
        peek_enrollment_token,
    )

    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(
                s, workspace_id=DEFAULT_WORKSPACE_ID, max_uses=3, label="fleet"
            )
            await s.commit()
        # Consume 1 of 3 over HTTP (real worker).
        r = await app_client.post("/v1/workers/register", json=_body(token, hostname="p1"))
        assert r.status_code == 200, r.text
        async with maker() as s:
            tok = await peek_enrollment_token(s, token)
            active = await list_enrollment_tokens(s, DEFAULT_WORKSPACE_ID, active_only=True)
            assert tok.id in {t.id for t in active}  # partially used but still live
            deleted = await delete_enrollment_token_by_id(
                s, token_id=tok.id, workspace_id=DEFAULT_WORKSPACE_ID
            )
            assert deleted is False  # live -> not deletable
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_token_uses_scoped(app_client: AsyncClient, migrated_db_url: str) -> None:
    from orcaster_server.workspaces import service
    from orcaster_server.workspaces.enrollment import list_token_uses, peek_enrollment_token

    engine = create_async_engine(migrated_db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            token = await mint_enrollment_token(s, workspace_id=DEFAULT_WORKSPACE_ID, max_uses=2)
            other_ws = await service.create_workspace(s, name="Other", slug="other")
            await s.commit()
        await app_client.post("/v1/workers/register", json=_body(token, hostname="u1"))
        await app_client.post("/v1/workers/register", json=_body(token, hostname="u2"))
        async with maker() as s:
            tok = await peek_enrollment_token(s, token)
            uses = await list_token_uses(s, token_id=tok.id, workspace_id=DEFAULT_WORKSPACE_ID)
            assert uses is not None and len(uses) == 2
            # Scope of another workspace -> None (404 in the handler).
            cross = await list_token_uses(s, token_id=tok.id, workspace_id=other_ws)
            assert cross is None
    finally:
        await engine.dispose()
