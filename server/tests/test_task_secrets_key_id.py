"""Non-secret id of the task secret keys.

`key_id` identifies a key without revealing anything usable about it: it lets
`rotate --keys-file` compare its file with the keys the server loaded (startup
and reload log lines).
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from orcaster_server.config import settings
from orcaster_server.task_secrets import keyring
from orcaster_server.task_secrets.keyring import TaskSecretsKeyring, key_id

_ID_RE = re.compile(r"[0-9a-f]{12}")


def test_key_id_is_stable_12_lowercase_hex() -> None:
    key = Fernet.generate_key()
    assert _ID_RE.fullmatch(key_id(key))
    assert key_id(key) == key_id(bytes(key))


def test_key_id_differs_between_keys() -> None:
    assert len({key_id(Fernet.generate_key()) for _ in range(20)}) == 20


def test_key_id_contains_no_key_text() -> None:
    key = Fernet.generate_key()
    kid = key_id(key)
    assert kid not in key.decode()
    assert key.decode() not in kid
    # Nor is it a prefix of the key bytes in hexadecimal.
    assert kid not in base64.urlsafe_b64decode(key).hex()


def test_key_id_is_not_the_fingerprint_derivation() -> None:
    """Own domain: it matches the fingerprint HMAC of no trivial value."""
    key = Fernet.generate_key()
    ring = TaskSecretsKeyring([key])
    assert key_id(key) not in ring.fingerprint("")


def test_keyring_key_ids_follow_file_order() -> None:
    k1, k2, k3 = (Fernet.generate_key() for _ in range(3))
    ring = TaskSecretsKeyring([k1, k2, k3])
    assert ring.key_ids == (key_id(k1), key_id(k2), key_id(k3))
    assert ring.primary_key_id == key_id(k1)
    assert TaskSecretsKeyring([k2, k1]).primary_key_id == key_id(k2)


def test_reload_log_line_names_the_key_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    k1, k2 = Fernet.generate_key(), Fernet.generate_key()
    p = tmp_path / "task-secrets-keys"
    p.write_text(k1.decode() + "\n")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))
    keyring.reset_keyring_cache()
    try:
        assert keyring.get_keyring() is not None
        p.write_text(k2.decode() + "\n" + k1.decode() + "\n")
        with caplog.at_level(logging.INFO):
            assert keyring.reload_keyring_if_changed() is True
    finally:
        keyring.reset_keyring_cache()
    messages = [r.getMessage() for r in caplog.records if "keys_reloaded" in r.getMessage()]
    assert len(messages) == 1
    assert f"primary key id={key_id(k2)}, key ids={key_id(k2)},{key_id(k1)}" in messages[0]
    for r in caplog.records:
        assert k1.decode() not in r.getMessage() and k2.decode() not in r.getMessage()
