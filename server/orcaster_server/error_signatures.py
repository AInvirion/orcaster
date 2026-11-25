"""Error signature heuristic for aggregating recurring failures.

Pure functions (no DB) that derive a normalized "signature" from the stderr tail
of a failed execution and trim stdout/stderr tails. The signature deduplicates
equivalent errors (same message with different numbers/paths/UUIDs); it does NOT
classify the nature of the error.
"""

from __future__ import annotations

import hashlib
import re

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?:[a-z]:\\|/)[^\s:]+")
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

_SIGNATURE_MAX_CHARS = 200
_SIGNATURE_HASH_CHARS = 16
# Shape of a well-formed `signature_hash`: exactly `_SIGNATURE_HASH_CHARS`
# LOWERCASE hex characters (hexdigest is always lowercase). It lets a selector
# with a typo be rejected before scanning, instead of scanning and silently
# returning `[]`.
_SIGNATURE_HASH_RE = re.compile(rf"^[0-9a-f]{{{_SIGNATURE_HASH_CHARS}}}$")


def last_nonempty_line(text: str | None) -> str | None:
    """Last non-empty line (after `strip`) of `text`, or None if there is none."""
    if not text:
        return None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def normalize_signature(line: str) -> str:
    """Normalize an error line into a stable signature for grouping.

    Order: lowercase + collapse whitespace; then UUID -> `<uuid>`, path ->
    `<path>`, hex (`0x...`) -> `<hex>`, number -> `<n>`; truncate to
    `_SIGNATURE_MAX_CHARS`.
    """
    # The substitutions below assume the string is already lowercase
    # (e.g. _PATH_RE uses [a-z] for the Windows drive letter).
    s = " ".join(line.lower().split())
    s = _UUID_RE.sub("<uuid>", s)
    s = _PATH_RE.sub("<path>", s)
    s = _HEX_RE.sub("<hex>", s)
    s = _NUM_RE.sub("<n>", s)
    return s[:_SIGNATURE_MAX_CHARS]


def signature_hash(signature: str) -> str:
    """Short, stable digest of a normalized signature, for referencing it in URLs.

    The normalized signature can reach `_SIGNATURE_MAX_CHARS` and contain spaces
    and symbols; its hash gives a compact, URL-safe key for the drill-down.
    Deterministic and DB-free: the same text always produces the same hash, so
    the server can recompute the signature per task and compare hashes without
    reversing anything. It also covers the fallback signature (`no-stderr:exit=...`).
    """
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:_SIGNATURE_HASH_CHARS]


def is_valid_signature_hash(value: str) -> bool:
    """True if `value` has the shape of a `signature_hash` (16 lowercase hex).

    Distinguishes a malformed selector (client typo -> 422 on the endpoint) from a
    well-formed signature that simply does not exist in the window (-> 200 with an
    empty list). Without this validation, a malformed hash triggers a full scan of
    the window and silently returns `[]`, hiding the client's error.
    """
    return bool(_SIGNATURE_HASH_RE.match(value))


def fallback_signature(exit_code: int | None) -> str:
    """Signature used when there is no usable stderr."""
    code = exit_code if exit_code is not None else "null"
    return f"no-stderr:exit={code}"


def cap_bytes(text: str | None, *, max_bytes: int) -> str:
    """Trim `text` to its last `max_bytes`, keeping the end.

    The line count is owned by the SQL (`_SQL_EXAMPLE_TAILS` already returns the
    last N non-empty lines); only the byte cap is applied here, because the most
    relevant part of an error lives at the end.
    """
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")
