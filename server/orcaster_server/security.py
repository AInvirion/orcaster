"""Credential utilities for workers.

`api_key` is delivered in plain text only once, when the worker is registered. The
server persists only the hex `sha256(api_key)` in `workers.api_key_hash` and compares
with `hmac.compare_digest` to avoid timing side channels.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(api_key), stored_hash)
