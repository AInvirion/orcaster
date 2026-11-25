"""Size limit for each request body, enforced in the app (it does not depend on the proxy).

Without a limit, an anonymous route (`/auth/*`, `/healthz`, worker enrollment)
would read a body of any size into memory. The middleware cuts the request off with
413 and it never reaches the route:

- with a `Content-Length` above the limit, before reading a single byte;
- without `Content-Length` (chunked) or with a lying one, as soon as the limit is
  exceeded while the stream is read.

There is one default limit and, per route, the limit of the matching
`BodyLimitRule`, which replaces it (higher or lower; with several matching rules,
the highest applies). An invalid `Content-Length` (non-numeric or negative) is a 400.
The 413 message does not repeat anything from the request.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass(frozen=True)
class BodyLimitRule:
    """Own limit, instead of the default, for the paths (without `root_path`) matching `pattern`."""

    pattern: re.Pattern[str]
    max_bytes: int


class _BodyTooLarge(Exception):
    pass


def _route_path(scope: Scope) -> str:
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        return path[len(root_path) :]
    return path


class BodyLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, default_max_bytes: int, rules: tuple[BodyLimitRule, ...] = ()
    ) -> None:
        self.app = app
        self.default_max_bytes = default_max_bytes
        self.rules = rules

    def _limit_for(self, scope: Scope) -> int:
        """Limit for this request: that of the matching rules, or the default.

        The rule REPLACES the default (it can be higher or lower), like a Caddy
        `handle` with its own `max_size`. If several match, the highest applies;
        if none does, the default.
        """
        path = _route_path(scope)
        matching = [r.max_bytes for r in self.rules if r.pattern.fullmatch(path) is not None]
        return max(matching) if matching else self.default_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope)
        for name, value in scope["headers"]:
            if name != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                declared = -1
            if declared < 0:
                await _respond(send, 400, "invalid Content-Length")
                return
            if declared > limit:
                await _respond(send, 413, f"request body exceeds {limit} bytes")
                return
            break

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                # FastAPI turns the read exception into a 400 when parsing the
                # body: here it is replaced by the 413 and the rest is discarded.
                if not started and message["type"] == "http.response.start":
                    started = True
                    await _respond(send, 413, f"request body exceeds {limit} bytes")
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        with contextlib.suppress(_BodyTooLarge):
            await self.app(scope, limited_receive, tracking_send)
        if exceeded and not started:
            await _respond(send, 413, f"request body exceeds {limit} bytes")


async def _respond(send: Send, status_code: int, message: str) -> None:
    body = json.dumps({"detail": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
