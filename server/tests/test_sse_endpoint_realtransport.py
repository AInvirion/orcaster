"""Test of the SSE endpoint GET /admin/events over a real HTTP transport.

The other endpoint tests (`test_admin_events_endpoint.py`) call the handler
directly because `httpx.ASGITransport` buffers the response and
`client.stream()` hangs on an infinite SSE. This test closes that gap: it
starts uvicorn in a thread, connects with an `AsyncClient` WITHOUT
ASGITransport (real HTTP transport) and confirms the stream contract (status
200, `content-type: text/event-stream`, first chunk `: connected`) with a
genuinely authenticated session.

Depends on `migrated_db_url` (ephemeral postgres); it is skipped when there is
neither a container runtime nor `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _mint_session_own_loop(database_url: str, workspace_id: str) -> str:
    """Mint a User + owner membership + session using the test's OWN engine.

    Unlike the conftest helper (`_mint_session_for_workspace`), this does NOT
    reuse the app's live engine: that engine belongs to the uvicorn thread's
    event loop, and using its asyncpg connections from the test loop fails with
    "got Future attached to a different loop". We create an ephemeral engine in
    the test loop against the SAME postgres; the session row stays persisted
    and the server (in its own loop) reads it without problems.
    """
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    engine = create_async_engine(database_url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            row = await s.execute(
                text("INSERT INTO users (display_name) VALUES ('Admin') RETURNING id")
            )
            user_id = str(row.scalar_one())
            await add_member(s, workspace_id=workspace_id, user_id=user_id, role="owner")
            token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
            await s.commit()
            return token
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_events_endpoint_real_transport_streams(
    migrated_db_url: str,
    _session_ed25519_key,
    monkeypatch: pytest.MonkeyPatch,
):
    """The SSE endpoint answers 200 + text/event-stream over a real transport."""
    from orcaster_server import signing as app_signing
    from orcaster_server.config import settings
    from orcaster_server.main import app

    # Configure BEFORE starting the server: the lifespan fails fast if signing
    # is not configured, and the DB is already migrated by the fixture.
    monkeypatch.setattr(settings, "database_url", migrated_db_url)
    monkeypatch.setattr(settings, "sse_enabled", True)
    monkeypatch.setattr(settings, "migrate_on_startup", False)
    monkeypatch.setattr(settings, "ed25519_private_key_path", str(_session_ed25519_key))
    app_signing.reset_key_cache()

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        # Wait for the server to start (wait on a condition, not a fixed sleep).
        async def _wait_started() -> None:
            while not server.started:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_started(), timeout=10.0)

        # Mint the session with the test's OWN engine (in the test loop), not
        # the app's (which lives in the uvicorn thread's loop). See the
        # docstring of `_mint_session_own_loop`.
        token = await _mint_session_own_loop(migrated_db_url, DEFAULT_WORKSPACE_ID)

        async with (
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                cookies={"session": token},
            ) as client,
            client.stream("GET", "/admin/events?topics=output") as resp,
        ):
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")

            # Read the first chunk (the stream is infinite: abort after it).
            async def _first_chunk() -> bytes:
                async for chunk in resp.aiter_raw():
                    return chunk
                return b""

            first = await asyncio.wait_for(_first_chunk(), timeout=10.0)
            assert b": connected" in first
    finally:
        # The server closes its own engine (db.dispose_engine) in its lifespan
        # on exit; we do not dispose it from this loop (it belongs to another).
        server.should_exit = True
        thread.join(timeout=10.0)
        app_signing.reset_key_cache()
