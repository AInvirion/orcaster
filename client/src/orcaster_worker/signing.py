"""Ed25519 verification + canonical_json.

This module is the client-side mirror of `server/orcaster_server/signing.py`.
The `canonical_json` function MUST produce bytes identical to the server's:
same key order (`sort_keys=True`), same separator (`(",", ":")`), and
`ensure_ascii=False` to preserve literal UTF-8. Any divergence makes legitimate
signatures get rejected in production.

Loads the public key from PEM with a lazy cache (per path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON → UTF-8 bytes serialization.

    Byte-exact mirror of `server/orcaster_server/signing.py::canonical_json`.
    Here `bytes` (UTF-8) are returned directly since the caller always uses
    them that way to verify signatures.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def build_command_message(
    *,
    uuid: str,
    worker_id: str,
    command: str,
    payload: dict | None,
    expires_at_iso: str,
) -> bytes:
    """Build the canonical `msg` to verify.

    Fields in the dict (ordered by sort_keys=True when serialized):
    `command`, `expires_at`, `payload`, `uuid`, `worker_id`.

    NOTE: this format (5 fields with `uuid`/`expires_at`) was the contract of
    the legacy push-command path. The client keeps it for test compatibility
    but the server does NOT emit signatures with this shape — all signatures
    now travel as `build_task_message` (6 fields).
    """
    return canonical_json(
        {
            "uuid": uuid,
            "worker_id": worker_id,
            "command": command,
            "payload": payload,
            "expires_at": expires_at_iso,
        }
    )


def build_task_message(
    *,
    task_id: int,
    worker_id: str,
    command: str,
    payload: dict,
    idempotency_key: str | None,
    created_at_iso: str,
) -> bytes:
    """Build the canonical msg to sign/verify a task from `/poll`.

    Byte-identical mirror of `server/orcaster_server/signing.py::build_task_message`.
    The 6 fields go in lexicographic order when serialized (sort_keys=True):
    `command`, `created_at`, `idempotency_key`, `payload`, `task_id`, `worker_id`.

    Important:
    - `created_at_iso` is passed exactly as it comes from the wire (the
      signature was computed over that same string with the "Z" suffix). Do not
      re-format it.
    - `idempotency_key` may be None: it is included explicitly as `null`.
    - `payload` is included literally; `sort_keys=True` applies recursively.
    """
    return canonical_json(
        {
            "command": command,
            "created_at": created_at_iso,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "task_id": task_id,
            "worker_id": worker_id,
        }
    )


def verify_signature(
    pub: Ed25519PublicKey,
    payload_bytes: bytes,
    signature: bytes,
) -> bool:
    """Verify an Ed25519 signature. Returns True/False (does not raise)."""
    try:
        pub.verify(signature, payload_bytes)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


# Lazy singleton cache per path.
_PUB_KEY_CACHE: tuple[str, Ed25519PublicKey] | None = None


def load_public_key(path: Path) -> Ed25519PublicKey:
    """Load an Ed25519PublicKey from a PEM on disk. Lazy cache."""
    global _PUB_KEY_CACHE
    key_path = str(path)
    if _PUB_KEY_CACHE is not None and _PUB_KEY_CACHE[0] == key_path:
        return _PUB_KEY_CACHE[1]
    with open(path, "rb") as fh:
        data = fh.read()
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"key at {path} is not an Ed25519 public key")
    _PUB_KEY_CACHE = (key_path, key)
    return key


def reset_key_cache() -> None:
    """Invalidate the cache (used by tests)."""
    global _PUB_KEY_CACHE
    _PUB_KEY_CACHE = None
