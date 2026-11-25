"""Reload of the task secrets key file.

`reload_keyring_if_changed` only rereads the file when it changed on disk since the
cache was loaded; a valid new file replaces the keyring, and an invalid or unreadable
one keeps the previous keyring and logs an ERROR without key material.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from orcaster_server.config import settings
from orcaster_server.task_secrets import keyring, service


@pytest.fixture
def keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, bytes]:
    p = tmp_path / "task-secrets-keys"
    k1 = Fernet.generate_key()
    p.write_text(k1.decode() + "\n")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))
    keyring.reset_keyring_cache()
    return p, k1


def test_unchanged_file_is_not_reloaded(keys: tuple[Path, bytes]) -> None:
    ring = keyring.get_keyring()
    assert ring is not None
    assert keyring.reload_keyring_if_changed() is False
    assert keyring.get_keyring() is ring


def test_changed_valid_file_replaces_keyring(
    keys: tuple[Path, bytes], caplog: pytest.LogCaptureFixture
) -> None:
    path, k1 = keys
    old_ring = keyring.get_keyring()
    assert old_ring is not None
    k2 = Fernet.generate_key()
    token = Fernet(k2).encrypt(b"value")
    path.write_text(k2.decode() + "\n" + k1.decode() + "\n")

    with caplog.at_level(logging.INFO):
        assert keyring.reload_keyring_if_changed() is True
    new_ring = keyring.get_keyring()
    assert new_ring is not old_ring and new_ring is not None
    assert new_ring.decrypt(token) == b"value"
    assert new_ring.decrypt(Fernet(k1).encrypt(b"old")) == b"old"
    assert any(
        r.levelno == logging.INFO and "task_secrets.keys_reloaded" in r.getMessage()
        for r in caplog.records
    )
    # Already reloaded: a new check without changes does not reload.
    assert keyring.reload_keyring_if_changed() is False


def test_changed_invalid_file_keeps_cached_ring_and_logs_error(
    keys: tuple[Path, bytes], caplog: pytest.LogCaptureFixture
) -> None:
    path, k1 = keys
    old_ring = keyring.get_keyring()
    path.write_text("not-a-fernet-key\n")

    with caplog.at_level(logging.DEBUG):
        assert keyring.reload_keyring_if_changed() is False
    assert keyring.get_keyring() is old_ring
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "task_secrets.keys_reload_failed" in errors[0]
    # The keyring that is kept is the one already loaded (it may come from an earlier
    # reload), not necessarily the one from startup.
    assert "keeping the keyring already loaded" in errors[0]
    for r in caplog.records:
        assert k1.decode() not in r.getMessage()


def test_same_invalid_file_is_reported_once(
    keys: tuple[Path, bytes], caplog: pytest.LogCaptureFixture
) -> None:
    path, _ = keys
    keyring.get_keyring()
    path.write_text("not-a-fernet-key\n")
    with caplog.at_level(logging.DEBUG):
        assert keyring.reload_keyring_if_changed() is False
        assert keyring.reload_keyring_if_changed() is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1


def test_unreadable_file_keeps_cached_ring(
    keys: tuple[Path, bytes], caplog: pytest.LogCaptureFixture
) -> None:
    path, _ = keys
    old_ring = keyring.get_keyring()
    path.unlink()
    with caplog.at_level(logging.DEBUG):
        assert keyring.reload_keyring_if_changed() is False
    assert keyring.get_keyring() is old_ring
    assert any(
        r.levelno == logging.ERROR and "task_secrets.keys_reload_failed" in r.getMessage()
        for r in caplog.records
    )


def test_replacement_preserving_size_and_mtime_is_reloaded(keys: tuple[Path, bytes]) -> None:
    """`cp -p` and `rsync -t` preserve the mtime and all Fernet keys have the same
    length: neither the mtime nor the size detects the replacement. The inode (the
    new file is renamed over the old one) and the ctime do change."""
    path, k1 = keys
    old_ring = keyring.get_keyring()
    assert old_ring is not None
    old = os.stat(path)
    k2 = Fernet.generate_key()
    assert len(k2) == len(k1)
    staged = path.with_name("staged")
    staged.write_text(k2.decode() + "\n")
    os.utime(staged, ns=(old.st_atime_ns, old.st_mtime_ns))
    os.replace(staged, path)
    now = os.stat(path)
    assert (now.st_mtime_ns, now.st_size) == (old.st_mtime_ns, old.st_size)

    assert keyring.reload_keyring_if_changed() is True
    new_ring = keyring.get_keyring()
    assert new_ring is not old_ring and new_ring is not None
    assert new_ring.decrypt(Fernet(k2).encrypt(b"v")) == b"v"


def test_in_place_rewrite_preserving_size_and_mtime_is_reloaded(
    keys: tuple[Path, bytes],
) -> None:
    """Same inode, same size, mtime restored with `utime`: only the ctime (which the
    kernel updates and `utime` cannot set) gives the change away."""
    path, k1 = keys
    old_ring = keyring.get_keyring()
    assert old_ring is not None
    old = os.stat(path)
    k2 = Fernet.generate_key()
    path.write_text(k2.decode() + "\n")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    now = os.stat(path)
    assert (now.st_ino, now.st_mtime_ns, now.st_size) == (old.st_ino, old.st_mtime_ns, old.st_size)

    assert keyring.reload_keyring_if_changed() is True
    new_ring = keyring.get_keyring()
    assert new_ring is not old_ring and new_ring is not None
    assert new_ring.decrypt(Fernet(k2).encrypt(b"v")) == b"v"


def test_unset_path_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    assert keyring.reload_keyring_if_changed() is False


# === Uses other than the poll check the file signature =======================
#
# `get_keyring_fresh` is the getter for the write paths (setting a secret via
# `require_keyring`) and for share links: without it, a process that never refused
# a delivery keeps encrypting with the primary key it loaded at startup.


def test_get_keyring_fresh_sees_a_changed_file_without_a_poll_refusal(
    keys: tuple[Path, bytes],
) -> None:
    path, k1 = keys
    old_ring = keyring.get_keyring()
    assert old_ring is not None
    k2 = Fernet.generate_key()
    path.write_text(k2.decode() + "\n" + k1.decode() + "\n")

    ring = keyring.get_keyring_fresh()
    assert ring is not None and ring is not old_ring
    # Encrypts with the new primary and still decrypts what the old one encrypted.
    assert Fernet(k2).decrypt(ring.encrypt(b"v")) == b"v"
    assert ring.decrypt(Fernet(k1).encrypt(b"old")) == b"old"


def test_plain_get_keyring_keeps_the_cached_ring_on_a_changed_file(
    keys: tuple[Path, bytes],
) -> None:
    """The poll hot path does not pay for a `stat`: `get_keyring` keeps serving the
    cache even if the file changed. It is the counterpart of the test above: it tells
    which of the two getters checks the signature."""
    path, k1 = keys
    old_ring = keyring.get_keyring()
    assert old_ring is not None
    k2 = Fernet.generate_key()
    path.write_text(k2.decode() + "\n" + k1.decode() + "\n")

    assert keyring.get_keyring() is old_ring
    # And its primary key is still the old one.
    assert Fernet(k1).decrypt(old_ring.encrypt(b"v")) == b"v"


def test_require_keyring_encrypts_and_fingerprints_with_the_new_primary(
    keys: tuple[Path, bytes],
) -> None:
    """`set_secret` takes the keyring from `require_keyring`: after the file changes
    it encrypts and fingerprints with the new primary key, without a poll having
    refused a delivery first."""
    path, k1 = keys
    old_ring = keyring.get_keyring()
    assert old_ring is not None
    k2 = Fernet.generate_key()
    path.write_text(k2.decode() + "\n" + k1.decode() + "\n")

    ring = service.require_keyring()
    ciphertext = ring.encrypt(b"value")
    # What is encrypted opens with the new key alone: once the old one is retired it
    # still decrypts, which is the failure this guards against.
    assert Fernet(k2).decrypt(ciphertext) == b"value"
    with pytest.raises(InvalidToken):
        Fernet(k1).decrypt(ciphertext)
    # The fingerprint also follows the primary key.
    fresh = keyring.load_keyring(str(path))
    assert ring.fingerprint("value") == fresh.fingerprint("value")
    assert ring.fingerprint("value") != old_ring.fingerprint("value")


def test_require_keyring_keeps_the_loaded_ring_on_an_invalid_file(
    keys: tuple[Path, bytes], caplog: pytest.LogCaptureFixture
) -> None:
    """A changed, invalid file does not break setting a secret: it keeps the loaded
    keyring and logs the usual ERROR, without key material."""
    path, k1 = keys
    old_ring = keyring.get_keyring()
    path.write_text("not-a-fernet-key\n")

    with caplog.at_level(logging.DEBUG):
        assert service.require_keyring() is old_ring
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "task_secrets.keys_reload_failed" in errors[0]
    for r in caplog.records:
        assert k1.decode() not in r.getMessage()


def test_require_keyring_without_a_path_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    with pytest.raises(service.TaskSecretsUnconfigured):
        service.require_keyring()
