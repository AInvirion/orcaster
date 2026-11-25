import asyncio

import pytest

from orcaster_server import main as main_module


def test_bus_instance_exists():
    # The global bus must exist even when SSE is off (the endpoint uses it).
    from orcaster_server.sse_bus import SseBus

    assert isinstance(main_module.sse_bus_instance, SseBus)


@pytest.mark.asyncio
async def test_lifespan_starts_listener_when_enabled(monkeypatch):
    # Deterministic synchronization: fake_loop signals the event on startup;
    # the test waits for that event inside the context manager, without relying on
    # any sleep in production code.
    listener_started = asyncio.Event()

    async def fake_loop(database_url, bus, stop_event):
        listener_started.set()
        await stop_event.wait()

    monkeypatch.setattr(main_module, "dashboard_listen_loop", fake_loop)
    monkeypatch.setattr(main_module.settings, "sse_enabled", True)
    monkeypatch.setattr(main_module.settings, "database_url", "postgresql+asyncpg://x/y")
    # migrations/signing off so this unit test does not need real infrastructure
    monkeypatch.setattr(main_module.settings, "migrate_on_startup", False)
    monkeypatch.setattr(main_module.settings, "housekeeping_enabled", False)
    monkeypatch.setattr(main_module.signing, "is_signing_configured", lambda: True)
    monkeypatch.setattr(main_module.signing, "validate_signing_config", lambda: None)
    monkeypatch.setattr(main_module.db, "init_engine", lambda: None)
    monkeypatch.setattr(main_module.db, "get_engine", lambda: object())
    monkeypatch.setattr(main_module.storage, "ensure_artifacts_root", lambda p: None)

    async def fake_dispose():
        return None

    monkeypatch.setattr(main_module.db, "dispose_engine", fake_dispose)

    async with main_module.lifespan(main_module.app):
        await asyncio.wait_for(listener_started.wait(), timeout=1.0)
        assert listener_started.is_set()
