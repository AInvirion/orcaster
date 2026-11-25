"""Ed25519 signing + canonical_json for push commands.

The server keeps a single Ed25519 private key loaded from
`ED25519_PRIVATE_KEY_PATH` (PEM, no password). It is cached lazily.
The signature is computed over the UTF-8-encoded `canonical_json({uuid,
worker_id, command, payload, expires_at})` and serialized as standard base64
(not url-safe) for the wire; the client decodes before verifying.
The database stores the raw bytes (`bytea`).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from orcaster_server.config import settings


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization (the canonical form that is signed).

    `sort_keys=True` for lexicographic order, `separators=(",", ":")` without
    spaces, `ensure_ascii=False` to preserve literal UTF-8. Signer and verifier
    must use exactly this form or the signature will not validate.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# Lazy singleton cache to avoid reading/parsing the PEM on every request.
_PRIV_KEY_CACHE: tuple[str, Ed25519PrivateKey] | None = None


def load_private_key(path: str) -> Ed25519PrivateKey:
    """Loads an Ed25519PrivateKey from a PEM on disk (no password)."""
    with open(path, "rb") as fh:
        data = fh.read()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"key at {path} is not an Ed25519 private key")
    return key


def get_private_key() -> Ed25519PrivateKey:
    """Returns the cached private key or loads it from settings.

    If `ED25519_PRIVATE_KEY_PATH` is not configured or the file does not exist,
    it raises `RuntimeError` with a clear message (the caller should translate
    it to HTTP 500 in admin endpoints).
    """
    global _PRIV_KEY_CACHE
    path = settings.ed25519_private_key_path
    if not path:
        raise RuntimeError("ED25519_PRIVATE_KEY_PATH is not configured: commands cannot be signed")
    if _PRIV_KEY_CACHE is not None and _PRIV_KEY_CACHE[0] == path:
        return _PRIV_KEY_CACHE[1]
    key = load_private_key(path)
    _PRIV_KEY_CACHE = (path, key)
    return key


def get_public_key_pem() -> str:
    """Server public key as a PEM SubjectPublicKeyInfo.

    Derives the public key from the cached private key (`get_private_key`). The
    result is byte-identical to `openssl pkey -in <priv> -pubout`, so the worker
    mounts it at `ED25519_PUBLIC_KEY_PATH` without conversion.
    Propagates `RuntimeError` if `ED25519_PRIVATE_KEY_PATH` is not configured.
    """
    pub = get_private_key().public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def reset_key_cache() -> None:
    """Invalidates the cache (use: tests that monkeypatch the path)."""
    global _PRIV_KEY_CACHE
    _PRIV_KEY_CACHE = None


def is_signing_configured() -> bool:
    """`True` if a private key path is configured in settings.

    It does not open the file; it only checks that the env var is set and not
    empty. To validate that the key is actually loadable, use
    `validate_signing_config()` (typically in the server lifespan).
    """
    path = settings.ed25519_private_key_path
    return bool(path)


def validate_signing_config() -> bool:
    """Loads and caches the key if configured. Returns `True` if it is ready.

    Behavior:
    - If `ED25519_PRIVATE_KEY_PATH` is not set: returns `False`, no error.
      The push commands feature stays disabled; the endpoint will answer
      503 when invoked.
    - If it is set but the file does not exist or is not a valid Ed25519 PEM:
      `raise` (RuntimeError / FileNotFoundError / OSError / ValueError) to
      force fail-fast in the lifespan; the operator prefers to see the error at
      startup rather than discover it when an admin command fails.
    """
    if not is_signing_configured():
        return False
    # `get_private_key` already validates the format and populates the cache.
    get_private_key()
    return True


def build_command_message(
    *,
    uuid: str,
    worker_id: str,
    command: str,
    payload: dict | None,
    expires_at_iso: str,
) -> bytes:
    """Builds the canonical `msg` to sign (the layout of the signed message).

    Fields in the dict (ordered by sort_keys=True when serializing):
    `command`, `expires_at`, `payload`, `uuid`, `worker_id`. `payload`
    may be None; it is included explicitly so the signature matches what the
    client reconstructs.
    """
    msg = canonical_json(
        {
            "uuid": uuid,
            "worker_id": worker_id,
            "command": command,
            "payload": payload,
            "expires_at": expires_at_iso,
        }
    )
    return msg.encode("utf-8")


def sign_command(
    priv: Ed25519PrivateKey,
    *,
    uuid: str,
    worker_id: str,
    command: str,
    payload: dict | None,
    expires_at_iso: str,
) -> bytes:
    """Signs the canonical `msg` and returns the raw signature bytes."""
    msg = build_command_message(
        uuid=uuid,
        worker_id=worker_id,
        command=command,
        payload=payload,
        expires_at_iso=expires_at_iso,
    )
    return priv.sign(msg)


def build_task_message(
    *,
    task_id: int,
    worker_id: str,
    command: str,
    payload: dict,
    idempotency_key: str | None,
    created_at_iso: str,
) -> bytes:
    """Builds the canonical msg to sign a task.

    Fields in lexicographic order (sort_keys=True): `command`, `created_at`,
    `idempotency_key`, `payload`, `task_id`, `worker_id`. `idempotency_key`
    may be `None`; it is included explicitly so the client reconstructs the same
    dict when verifying.

    The `worker_id` that is signed is the task's `target_worker_id` (the
    recipient), not the creator. Tasks with `delivery_mode='claim'` are not
    signed; this helper only applies when `signed=true`.
    """
    msg = canonical_json(
        {
            "command": command,
            "created_at": created_at_iso,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "task_id": task_id,
            "worker_id": worker_id,
        }
    )
    return msg.encode("utf-8")


def sign_task(
    priv: Ed25519PrivateKey,
    *,
    task_id: int,
    worker_id: str,
    command: str,
    payload: dict,
    idempotency_key: str | None,
    created_at_iso: str,
) -> bytes:
    """Signs the canonical msg of a task. Returns raw bytes."""
    return priv.sign(
        build_task_message(
            task_id=task_id,
            worker_id=worker_id,
            command=command,
            payload=payload,
            idempotency_key=idempotency_key,
            created_at_iso=created_at_iso,
        )
    )


def verify_task_signature(
    pub: Ed25519PublicKey,
    sig: bytes,
    *,
    task_id: int,
    worker_id: str,
    command: str,
    payload: dict,
    idempotency_key: str | None,
    created_at_iso: str,
) -> bool:
    """Verifies the signature of a task (used in tests)."""
    msg = build_task_message(
        task_id=task_id,
        worker_id=worker_id,
        command=command,
        payload=payload,
        idempotency_key=idempotency_key,
        created_at_iso=created_at_iso,
    )
    try:
        pub.verify(sig, msg)
        return True
    except Exception:
        return False


def signature_to_b64(sig: bytes) -> str:
    """Encodes a signature as standard base64 (with padding) for the wire."""
    return base64.b64encode(sig).decode("ascii")


def verify_signature(
    pub: Ed25519PublicKey,
    sig: bytes,
    *,
    uuid: str,
    worker_id: str,
    command: str,
    payload: dict | None,
    expires_at_iso: str,
) -> bool:
    """Verifies the signature (used in tests; the real client does the same)."""
    msg = build_command_message(
        uuid=uuid,
        worker_id=worker_id,
        command=command,
        payload=payload,
        expires_at_iso=expires_at_iso,
    )
    try:
        pub.verify(sig, msg)
        return True
    except Exception:
        return False
