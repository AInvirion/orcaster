"""Pure helpers for per-domain egress control.

The real enforcement is the Podman ``--internal`` network + the proxy sidecar;
these helpers are the shared matching logic and the per-task allowlist file
that the worker writes and the proxy reads.

Signed allowlist. The worker and the task subprocess run with the same uid in
the same container, so any file the worker can write, the task can write too:
the egress volume cannot be read-only for the task without being so for the
worker. The boundary, then, is not the file permission but a key the task
cannot read. The worker signs the allowlist with HMAC-SHA256 under
``ORCH_EGRESS_KEY`` (a Podman secret that reaches only the initial environment
of the worker and the proxy; the worker removes it from ``os.environ`` at
startup and its process is not dumpable) and the proxy discards the per-task
entries of a file without a valid signature. A task can delete or corrupt the
file (it only removes egress from itself), but it cannot add a host.

Format v1: a first line ``#orcaster-egress-mac v1 <hex>`` followed by one entry
per line. The MAC covers the exact bytes that follow the first line. A proxy
that predates signing reads the header as an entry that matches no host (``#``
does not pass the proxy's character guard), so the new format is backward
compatible.

Known limit: within the same worker the key does not change between tasks, so a
task that saved the signed allowlist of a previous task can restore it. It only
recovers hosts the server already authorized for that worker; it cannot name a
new one.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path


def _norm_host(host: str) -> str:
    h = host.strip().lower()
    if h.startswith("[") and "]" in h:  # IPv6 literal [::1]:443
        return h[1 : h.index("]")]
    return h.split(":", 1)[0]  # strip the port


def host_allowed(host: str, allow: Iterable[str]) -> bool:
    """True if ``host`` matches any entry of ``allow``.

    Exact entry ("data.example.org") -> exact match. Entry with a "." or "*."
    prefix ("*.example.net", ".example.net") -> subdomain match on a label boundary.
    """
    h = _norm_host(host)
    if not h:
        return False
    for entry in allow:
        e = entry.strip().lower()
        if not e:
            continue
        if e.startswith("*."):
            e = e[1:]  # "*.example.net" -> ".example.net"
        if e.startswith("."):
            suffix = e  # ".example.net"
            if h == suffix[1:] or h.endswith(suffix):
                return True
        elif h == e:
            return True
    return False


# Name of the key variable in the worker and in the proxy. `ORCH_` prefix:
# `task_env` never hands it to a task, neither declared (denylist) nor in strict
# mode.
EGRESS_KEY_ENV = "ORCH_EGRESS_KEY"
# 32 bytes in hex: what `make egress-net` generates (Podman secret).
_MIN_KEY_BYTES = 32
MAC_HEADER_PREFIX = "#orcaster-egress-mac v1 "
# MAC domain separation: the same key cannot be used to sign anything else.
_MAC_CONTEXT = b"orcaster-egress-allowlist-v1\n"


class AllowlistRefused(Exception):
    """The per-task allowlist file does not carry a valid signature."""


def parse_egress_key(raw: str) -> bytes:
    """Decode the hex key of ``ORCH_EGRESS_KEY``; ``ValueError`` if unusable.

    The message never includes the value.
    """
    value = raw.strip()
    try:
        key = bytes.fromhex(value)
    except ValueError:
        raise ValueError(f"{EGRESS_KEY_ENV} is not a hex string") from None
    if len(key) < _MIN_KEY_BYTES:
        raise ValueError(f"{EGRESS_KEY_ENV} is shorter than {_MIN_KEY_BYTES} bytes")
    return key


def _mac(key: bytes, body: bytes) -> str:
    return hmac.new(key, _MAC_CONTEXT + body, hashlib.sha256).hexdigest()


def _entries(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def write_allowlist(path: Path, domains: list[str], key: bytes | None = None) -> None:
    """Write the allowlist (one per line) atomically.

    With ``key`` it prepends the header with the HMAC of the body; without it
    it writes the unsigned format, which only a keyless proxy accepts.
    """
    body = "\n".join(domains)
    if domains:
        body += "\n"
    body_bytes = body.encode("utf-8")
    header = f"{MAC_HEADER_PREFIX}{_mac(key, body_bytes)}\n".encode("ascii") if key else b""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".allow-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(header + body_bytes)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_allowlist(path: Path) -> list[str]:
    """Read the entries without verifying the signature; missing or empty -> [].

    Ignores comment lines (``#``), including the MAC header. It is the keyless
    proxy's read and the worker's (sweep at boot); the keyed proxy uses
    ``read_signed_allowlist``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return _entries(text)


def read_signed_allowlist(path: Path, key: bytes) -> list[str]:
    """Entries of a file signed with ``key``; missing or empty -> [].

    Raises ``AllowlistRefused`` (with the reason, never the content) if the file
    has entries without a header, with a malformed header or with a MAC that
    does not match. A file without entries needs no signature: it widens
    nothing.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    header, sep, body = data.partition(b"\n")
    if not header.startswith(MAC_HEADER_PREFIX.encode("ascii")):
        try:
            unsigned = _entries(data.decode("utf-8"))
        except UnicodeDecodeError:
            raise AllowlistRefused("unsigned file is not valid UTF-8") from None
        if unsigned:
            raise AllowlistRefused("file has entries but no signature header")
        return []
    if not sep:
        body = b""
    given = header[len(MAC_HEADER_PREFIX) :].strip()
    if not hmac.compare_digest(given, _mac(key, body).encode("ascii")):
        raise AllowlistRefused("signature does not match (file written without the worker key)")
    try:
        return _entries(body.decode("utf-8"))
    except UnicodeDecodeError:
        raise AllowlistRefused("signed body is not valid UTF-8") from None
