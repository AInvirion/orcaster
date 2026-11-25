"""Tests for the helper that exposes the server's Ed25519 public key.

`get_public_key_pem()` must return exactly the same SubjectPublicKeyInfo PEM
that `openssl pkey -pubout` produces from the configured private key, so the
client can read it unchanged.

NOTE: the `app_client` fixture in conftest only sets
`settings.ed25519_private_key_path` for tests that use it. These are pure unit
tests (no DB), so they generate their own temporary key and monkeypatch
settings directly.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orcaster_server import signing
from orcaster_server.config import settings


@pytest.fixture
def configured_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    """Generate a temporary Ed25519 private key and configure it in settings."""
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "server.pem"
    path.write_bytes(pem)
    monkeypatch.setattr(settings, "ed25519_private_key_path", str(path))
    signing.reset_key_cache()
    yield priv
    signing.reset_key_cache()


def test_get_public_key_pem_matches_configured_private_key(
    configured_key: Ed25519PrivateKey,
) -> None:
    pem = signing.get_public_key_pem()

    assert pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert pem.rstrip().endswith("-----END PUBLIC KEY-----")

    expected = (
        configured_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    assert pem == expected


def test_get_public_key_pem_raises_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ed25519_private_key_path", "")
    signing.reset_key_cache()
    with pytest.raises(RuntimeError):
        signing.get_public_key_pem()
    signing.reset_key_cache()
