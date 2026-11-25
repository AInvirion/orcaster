"""Startup grace config."""

from orcaster_server.config import Settings


def test_startup_grace_default_is_150():
    """Without env, the default startup grace is 150 s (lease 120 + one poll margin)."""
    s = Settings()
    assert s.startup_grace_seconds == 150


def test_startup_grace_override_env(monkeypatch):
    """STARTUP_GRACE_SECONDS overrides the default."""
    monkeypatch.setenv("STARTUP_GRACE_SECONDS", "200")
    s = Settings()
    assert s.startup_grace_seconds == 200
