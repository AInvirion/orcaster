"""public_base_url config (status page share links)."""

from orcaster_server.config import Settings


def test_public_base_url_default():
    """Without env, public_base_url has the same default as webauthn_origin."""
    s = Settings()
    assert s.public_base_url == "http://localhost:8000"


def test_public_base_url_override_env(monkeypatch):
    """PUBLIC_BASE_URL overrides the default."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://status.example.com")
    s = Settings()
    assert s.public_base_url == "https://status.example.com"
