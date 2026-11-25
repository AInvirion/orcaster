"""Encryption keys for task secrets.

The `TASK_SECRETS_KEYS_PATH` file holds one Fernet key per line; the first is
the primary (it encrypts), all of them decrypt (`MultiFernet`). Empty lines and
lines starting with `#` are ignored. The path expands environment variables to
allow `$CREDENTIALS_DIRECTORY` (systemd `LoadCredentialEncrypted=`).

States:
- unset (or empty): `get_keyring()` returns `None`; setting a secret answers
  503, deleting works and the poll leaves queued the tasks that declare stored
  secrets;
- set but unreadable, empty or with an invalid key: `KeyringError`, which stops
  the server at startup (same criterion as the Ed25519 key);
- set and valid: `TaskSecretsKeyring` (a `MultiFernet`) cached by path and file
  signature (inode, mtime, ctime and size); `reload_keyring_if_changed` reloads
  it when it changed on disk, and `get_keyring_fresh` checks that signature in
  the uses that are not the poll.

The fingerprint of a value is an HMAC-SHA256 with a key derived from the primary
by HKDF: without the key, a DB dump does not allow guessing the value offline.
It changes with the primary, so `rotate` recomputes it together with the
re-encryption.

No error message includes key material.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
from collections.abc import Sequence
from typing import NamedTuple

from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from orcaster_server.config import settings

logger = logging.getLogger(__name__)

# Unexpanded variable that `os.path.expandvars` leaves literal.
_UNEXPANDED_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


class KeyringError(RuntimeError):
    """Keys file configured but unusable."""


# FROZEN WIRE CONSTANTS: the three HKDF labels below derive keys for data
# already persisted (stored fingerprints,
# `workspace_share_token.token_ciphertext`, the key ids that
# `rotate --keys-file` compares). Changing any of them changes every derived key
# and silently breaks that data; `tests/test_task_secrets_hkdf_vectors.py` pins
# them. A new label needs a new version suffix and a migration.

# Label of the fingerprint key derivation. Changing it changes all the
# fingerprints: it is versioned.
_FINGERPRINT_HKDF_INFO = b"orcaster task secret fingerprint v1"
FINGERPRINT_PREFIX = "hmac-sha256:"
_FINGERPRINT_HEX_LEN = 12

# Domain separation for share links: their tokens are encrypted with keys
# derived by HKDF from each key in the file, not with the raw keys. A share-link
# ciphertext is not opened by the secrets keyring nor vice versa, so copying one
# into the other's column does not yield its value. It is versioned.
_SHARE_LINK_HKDF_INFO = b"orcaster share link token v1"


# Non-secret id of a key: it allows comparing the file used by
# `rotate --keys-file` with the keys the server loaded. It is versioned.
_KEY_ID_HKDF_INFO = b"orcaster task secret key id v1"
_KEY_ID_HEX_LEN = 12


def key_id(key: bytes) -> str:
    """Non-secret id of a Fernet key: 12 hexadecimal characters.

    It is the beginning of an HKDF-SHA256 derivation of the key with its own
    label: it identifies the key in logs and messages without revealing anything
    usable about it, and it does not coincide with the fingerprint key nor with
    the share-link ones.
    """
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_ID_HKDF_INFO).derive(
        base64.urlsafe_b64decode(key)
    )
    return raw.hex()[:_KEY_ID_HEX_LEN]


def _derive_fernet_key(key: bytes, info: bytes) -> bytes:
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        base64.urlsafe_b64decode(key)
    )
    return base64.urlsafe_b64encode(raw)


class TaskSecretsKeyring(MultiFernet):
    """`MultiFernet` (primary first) that also computes the fingerprint of a value.

    `share_links` is a separate `MultiFernet`, with keys derived in the same
    order, for the share-link token. `key_ids` holds the non-secret id of each
    key in file order.
    """

    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys:
            raise KeyringError("task secrets keyring needs at least one key")
        super().__init__([Fernet(k) for k in keys])
        self._fingerprint_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=_FINGERPRINT_HKDF_INFO,
        ).derive(base64.urlsafe_b64decode(keys[0]))
        self.share_links = MultiFernet(
            [Fernet(_derive_fernet_key(k, _SHARE_LINK_HKDF_INFO)) for k in keys]
        )
        self.key_ids: tuple[str, ...] = tuple(key_id(k) for k in keys)

    @property
    def primary_key_id(self) -> str:
        """Non-secret id of the primary key (the first in the file)."""
        return self.key_ids[0]

    def describe_key_ids(self) -> str:
        """`primary key id=<id>, key ids=<id1>,<id2>,...` for the logs."""
        return f"primary key id={self.primary_key_id}, key ids={','.join(self.key_ids)}"

    def fingerprint(self, value: str) -> str:
        """`hmac-sha256:<12 hex>` of the plaintext value, with the fingerprint key.

        It includes no character of the value. Two equal values have the same
        fingerprint as long as the primary key does not change.
        """
        digest = hmac.new(self._fingerprint_key, value.encode("utf-8"), hashlib.sha256)
        return FINGERPRINT_PREFIX + digest.hexdigest()[:_FINGERPRINT_HEX_LEN]


def expand_keys_path(raw: str) -> str:
    """Expand environment variables in the path; fail if any is not defined.

    An undefined variable would stay literal in the path and the error would be a
    misleading "file not found"; the variable is named instead.
    """
    expanded = os.path.expandvars(raw)
    missing = _UNEXPANDED_VAR_RE.search(expanded)
    if missing is not None:
        raise KeyringError(
            f"TASK_SECRETS_KEYS_PATH references ${missing.group(1)}, which is not set"
        )
    return expanded


def load_keyring(raw_path: str) -> TaskSecretsKeyring:
    """Read the keys file and build the keyring (primary first)."""
    path = expand_keys_path(raw_path)
    try:
        with open(path, encoding="ascii") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyringError(
            f"cannot read TASK_SECRETS_KEYS_PATH file {path}: {type(exc).__name__}"
        ) from None
    keys: list[bytes] = []
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            Fernet(stripped.encode("ascii"))
            keys.append(stripped.encode("ascii"))
        except (ValueError, TypeError):
            # `from None`: the original exception could carry the line.
            raise KeyringError(
                f"TASK_SECRETS_KEYS_PATH file {path}: line {lineno} is not a valid Fernet key"
            ) from None
    if not keys:
        raise KeyringError(f"TASK_SECRETS_KEYS_PATH file {path} has no keys")
    return TaskSecretsKeyring(keys)


# Signature of a file to detect changes: (`st_ino`, `st_mtime_ns`,
# `st_ctime_ns`, `st_size`). All Fernet keys have the same length, so the size
# almost never changes, and `cp -p` or `rsync -t` preserve the mtime: such a
# replacement is only revealed by the inode (the new file is renamed over the
# old one) or by the ctime, which the kernel sets on modification and cannot be
# restored with `utime`. A change of only permissions or owner also moves the
# ctime and causes a harmless reload: it loads the same content. A file that
# cannot be queried has the signature `_MISSING_SIG`, which no real file
# produces (its size is not negative).
_FileSig = tuple[int, int, int, int]
_MISSING_SIG: _FileSig = (-1, -1, -1, -1)


class _CachedKeyring(NamedTuple):
    raw: str
    ring: TaskSecretsKeyring
    sig: _FileSig


# Lazy cache. It is invalidated if the path changes, or if
# `reload_keyring_if_changed` finds the file changed.
_KEYRING_CACHE: _CachedKeyring | None = None
# Signature of the last file that `reload_keyring_if_changed` could not load. It
# avoids repeating the ERROR on every poll while the file stays the same.
_REJECTED_SIG: _FileSig | None = None


def _stat_sig(raw: str) -> _FileSig:
    """Signature of the keys file; `_MISSING_SIG` if it cannot be queried."""
    try:
        st = os.stat(expand_keys_path(raw))
    except (OSError, KeyringError):
        return _MISSING_SIG
    return (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def is_configured() -> bool:
    """`True` if `TASK_SECRETS_KEYS_PATH` is set and non-empty."""
    return bool(settings.task_secrets_keys_path)


def get_keyring() -> TaskSecretsKeyring | None:
    """Cached keyring, or `None` if the store is not configured.

    Configured but invalid raises `KeyringError`; startup already validates it
    with `validate_keys_config`, so in steady state it does not happen.
    """
    global _KEYRING_CACHE
    raw = settings.task_secrets_keys_path
    if not raw:
        return None
    if _KEYRING_CACHE is not None and _KEYRING_CACHE.raw == raw:
        return _KEYRING_CACHE.ring
    # Signature before reading: if the file changes between both steps, the
    # cached signature is stale and the next check reloads it.
    sig = _stat_sig(raw)
    ring = load_keyring(raw)
    _KEYRING_CACHE = _CachedKeyring(raw, ring, sig)
    return ring


def get_keyring_fresh() -> TaskSecretsKeyring | None:
    """Keyring after checking whether the file changed on disk, or `None` if unconfigured.

    For the uses that are not the poll: setting a secret (the service's
    `require_keyring`) and the share-link URLs. They are rare and the check
    costs one `stat`, so they do not wait for a poll to refuse a delivery to
    notice a changed file: a process that did not reload would encrypt with the
    previous primary, and once that key is retired the row would stop
    decrypting. The poll's hot path keeps using `get_keyring()` (cache, no
    `stat`).

    `reload_keyring_if_changed` does not raise and keeps the loaded keyring if
    the new file is unreadable or invalid; configured and invalid with nothing
    cached raises `KeyringError` like `get_keyring`.
    """
    reload_keyring_if_changed()
    return get_keyring()


def reset_keyring_cache() -> None:
    """Invalidate the cache (tests, or after rotating the file with the server running)."""
    global _KEYRING_CACHE, _REJECTED_SIG
    _KEYRING_CACHE = None
    _REJECTED_SIG = None


def reload_keyring_if_changed() -> bool:
    """Reload the keys file if it changed on disk since it was loaded.

    In the poll it is called only when a delivery is refused because of a secret
    that does not decrypt, or while it remembers such a refusal: never in a poll
    without a refusal. Outside the poll `get_keyring_fresh` calls it (setting a
    secret and the share-link URLs). Returns `True` only if a new, valid file
    replaced the cached keyring. If the file cannot be read or is not valid, it
    keeps the previous keyring, logs an ERROR (without key material) and returns
    `False`; it never raises. An invalid file already reported is not logged
    again while its signature does not change.
    """
    global _KEYRING_CACHE, _REJECTED_SIG
    raw = settings.task_secrets_keys_path
    if not raw or _KEYRING_CACHE is None or _KEYRING_CACHE.raw != raw:
        return False
    sig = _stat_sig(raw)
    if sig == _KEYRING_CACHE.sig or sig == _REJECTED_SIG:
        return False
    try:
        ring = load_keyring(raw)
    except KeyringError as exc:
        _REJECTED_SIG = sig
        logger.error(
            "task_secrets.keys_reload_failed: TASK_SECRETS_KEYS_PATH changed on disk but "
            "cannot be loaded (%s); keeping the keyring already loaded",
            exc,
        )
        return False
    _KEYRING_CACHE = _CachedKeyring(raw, ring, sig)
    _REJECTED_SIG = None
    logger.info(
        "task_secrets.keys_reloaded: TASK_SECRETS_KEYS_PATH changed on disk; keyring reloaded; %s",
        ring.describe_key_ids(),
    )
    return True


def validate_keys_config() -> bool:
    """Load and cache the keys if configured (lifespan).

    Returns `False` if unset; raises `KeyringError` if set and unusable.
    """
    return get_keyring() is not None
