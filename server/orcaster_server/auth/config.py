"""Injectable configuration and credential helpers of the auth module.

Tokens (invitation, session id) are generated with `secrets` and persisted
only as SHA-256 hex, the same pattern as `orcaster_server/security.py` for api keys.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from orcaster_server.auth.models import Invitation, Session, User


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def _default_redirect(request: Request, user: User) -> str:  # noqa: ARG001
    return "/"


# `describe_invitation` result that asks to hide workspace, role and label.
INVITATION_WITHHELD = ""


@dataclass
class AuthConfig:
    rp_id: str
    rp_name: str
    origin: str
    session_ttl_seconds: int = 30 * 86400
    invitation_ttl_seconds: int = 7 * 86400
    bootstrap_invitation_ttl_seconds: int = 15 * 60
    challenge_ttl_seconds: int = 300
    # Cap on pending challenges per `kind` (begin is anonymous): see
    # `passkey._store_challenge`.
    challenge_max_pending: int = 1000
    # Per-client limit on anonymous `begin` calls: in-memory token bucket.
    # `begin_rate_per_minute` is the sustained rate (0 disables it) and
    # `begin_rate_burst` the bucket capacity. See `auth.rate_limit`. Anonymous
    # `complete` calls use these same four values in their own limiter,
    # with buckets separate from the `begin` ones.
    begin_rate_per_minute: int = 30
    begin_rate_burst: int = 10
    # Coarse level: bucket shared per network (IPv4 /24, IPv6 /48), so that
    # rotating addresses within one network does not evade the per-client bucket.
    # 0 disables it.
    begin_group_rate_per_minute: int = 300
    begin_group_burst: int = 100
    cookie_name: str = "session"
    cookie_secure: bool = True
    cookie_samesite: str = "lax"
    require_min_passkeys: int = 1
    post_login_redirect: Callable[[Request, User], str] = _default_redirect
    on_invitation_accepted: (
        Callable[[AsyncSession, User, Invitation, Session], Awaitable[None]] | None
    ) = None
    # Describes the invitation for the acceptance page (e.g. which
    # workspace it joins and with what role). Returns None if there is nothing to
    # show and `INVITATION_WITHHELD` if the invitation must reveal nothing: the
    # page does not fall back to the stored label either.
    describe_invitation: Callable[[AsyncSession, Invitation], Awaitable[str | None]] | None = None
    # Host hook: True if the invitation must not be acceptable even though
    # it is still valid (e.g. its workspace is disabled). It is consulted on
    # accept (join and register); True gives the same 410 as an expired,
    # revoked or used invitation. `auth` does not import `workspaces`.
    is_invitation_refused: Callable[[AsyncSession, Invitation], Awaitable[bool]] | None = None

    def __post_init__(self) -> None:
        # Without this, 0 or a negative value silently degraded to "one pending per
        # kind" and each begin invalidated the previous one. Explicit > implicit.
        cap = self.challenge_max_pending
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            raise ValueError(f"AuthConfig.challenge_max_pending must be an int >= 1, got {cap!r}")
        for name, minimum in (
            ("begin_rate_per_minute", 0),
            ("begin_rate_burst", 1),
            ("begin_group_rate_per_minute", 0),
            ("begin_group_burst", 1),
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"AuthConfig.{name} must be an int >= {minimum}, got {value!r}")
