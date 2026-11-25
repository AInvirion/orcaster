"""CONNECT sidecar proxy with a domain allowlist.

Runs in its own container connected to the worker's internal network (where the
traffic enters) and to a network with outbound access (where it tunnels). The
worker only has a route to this proxy (``--internal`` network), so an ``unset
HTTPS_PROXY`` or a direct connection by IP in the task has NO way out: the
default-deny is at the network level, not an environment variable.

With ``ORCH_EGRESS_KEY`` (injected by ``make up-proxy`` from a Podman secret)
the proxy only accepts per-task entries from an allowlist signed by the worker:
a task, which runs with the worker uid and can write the file, cannot add a
host. Without the key it keeps the previous behavior (unsigned file) and warns
at startup.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import sys
from pathlib import Path

from orcaster_worker.egress import (
    EGRESS_KEY_ENV,
    AllowlistRefused,
    host_allowed,
    parse_egress_key,
    read_allowlist,
    read_signed_allowlist,
)

log = logging.getLogger("orcaster.worker.egress_proxy")

_ALLOWED_PORTS = frozenset({80, 443})

# Valid characters in a CONNECT host (hostname, IPv4, or IPv6 literal in
# brackets). Any other character (spaces, control chars, etc.) is rejected
# before trying to match against the allowlist: it keeps an "odd" host (e.g.
# with spaces or an embedded CRLF) from slipping through a lax string
# comparison. `host_allowed` does not normalize IDNA/punycode; a unicode
# hostname simply does not match an ASCII allowlist and is denied (fail-closed),
# so no extra normalization is needed here.
_HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]-]+$")


class ProxyPolicy:
    """Decide whether (host, port) is allowed. Re-reads the per-task file on every
    call (the worker's single slot makes the per-task policy trivial).

    With ``key`` only the entries of a file signed with that key count; one
    without a valid signature leaves only the base allowlist (fail closed) and a
    WARNING is logged for each distinct version of the file, not per request.
    """

    def __init__(self, base: list[str], allow_file: Path, key: bytes | None = None) -> None:
        self._base = list(base)
        self._allow_file = allow_file
        self._key = key
        self._last_refusal: tuple[object, str] | None = None

    @property
    def signed(self) -> bool:
        return self._key is not None

    def _task_entries(self) -> list[str]:
        if self._key is None:
            return read_allowlist(self._allow_file)
        try:
            entries = read_signed_allowlist(self._allow_file, self._key)
        except AllowlistRefused as e:
            self._log_refusal(str(e))
            return []
        self._last_refusal = None
        return entries

    def _log_refusal(self, reason: str) -> None:
        try:
            st = self._allow_file.stat()
            version: object = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            version = None
        if self._last_refusal == (version, reason):
            return
        self._last_refusal = (version, reason)
        log.warning(
            "egress allowlist refused: %s; per-task entries ignored, base allowlist only (file %s)",
            reason,
            self._allow_file,
        )

    def allows(self, host: str, port: int) -> bool:
        if port not in _ALLOWED_PORTS:
            return False
        allow = self._base + self._task_entries()
        return host_allowed(host, allow)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _handle(policy: ProxyPolicy, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
    try:
        request_line = await cr.readline()
        if not request_line:
            cw.close()
            return
        parts = request_line.decode("latin-1").split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            cw.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        # discard headers up to the blank line
        while (await cr.readline()) not in (b"\r\n", b"\n", b""):
            pass
        host, _, port_s = parts[1].rpartition(":")
        port = int(port_s) if port_s.isdigit() else 443
        if not host or not _HOST_RE.match(host):
            log.warning("DENY CONNECT malformed host %r", host)
            cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        if not policy.allows(host, port):
            log.warning("DENY CONNECT %s:%s", host, port)
            cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        try:
            ur, uw = await asyncio.open_connection(host, port)
        except OSError:
            cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await cw.drain()
        log.info("ALLOW CONNECT %s:%s", host, port)
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
    except Exception as e:  # noqa: BLE001 - a proxy must not go down over one connection
        log.debug("proxy handler error: %s", e)
        with contextlib.suppress(Exception):
            cw.close()


async def _serve(port: int, policy: ProxyPolicy) -> None:
    server = await asyncio.start_server(lambda r, w: _handle(policy, r, w), "0.0.0.0", port)
    log.info(
        "egress proxy listening on :%s base=%s per-task allowlist=%s",
        port,
        policy._base,
        "signed" if policy.signed else "unsigned",
    )
    async with server:
        await server.serve_forever()


def _load_key() -> bytes | None:
    """Key from ``ORCH_EGRESS_KEY``; malformed -> exit 1, missing -> WARNING."""
    raw = os.environ.get(EGRESS_KEY_ENV, "")
    if not raw.strip():
        log.warning(
            "%s not set: the per-task egress allowlist is accepted unsigned, so code "
            "running in the worker container can add hosts to it. "
            "Recreate the proxy with `make up client` to provision the key.",
            EGRESS_KEY_ENV,
        )
        return None
    try:
        return parse_egress_key(raw)
    except ValueError as e:
        log.error("refusing to start: %s", e)
        sys.exit(1)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("ORCASTER_PROXY_PORT", "3128")))
    ap.add_argument(
        "--allow-file",
        default=os.environ.get("ORCASTER_EGRESS_ALLOW_FILE", "/var/lib/orcaster-egress/allow.txt"),
    )
    args = ap.parse_args()
    base_env = os.environ.get("ORCASTER_PROXY_BASE_ALLOW", "")
    base = [d.strip() for d in base_env.split(",") if d.strip()]
    key = _load_key()
    policy = ProxyPolicy(base=base, allow_file=Path(args.allow_file), key=key)
    asyncio.run(_serve(args.port, policy))


if __name__ == "__main__":
    main()
