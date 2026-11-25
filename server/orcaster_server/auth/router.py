"""HTTP endpoints of the auth module. The HTML pages serve the passkey flow;
the JSON endpoints handle the begin/complete ceremonies via fetch from webauthn.js.
"""

import base64
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.auth.config import INVITATION_WITHHELD, AuthConfig, hash_token
from orcaster_server.auth.models import Invitation, Session, User
from orcaster_server.auth.rate_limit import BeginRateLimiter, limiter_from_config
from orcaster_server.auth.service import invitation as inv_service
from orcaster_server.auth.service import passkey as passkey_service
from orcaster_server.auth.service import session as session_service
from orcaster_server.db import get_session

logger = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Ephemeral cookie that ties the begin step to the complete step of a ceremony.
_CHALLENGE_COOKIE = "webauthn_challenge"

_DISPLAY_NAME_MAX = 200
# Placeholder that the host used as `invitations.label` (`workspace:<uuid>`) and that
# the page prefilled as the name: it is never a valid name. Only the
# full form, UUID 8-4-4-4-12, case-insensitive.
# A single definition: the page receives `.pattern` and the script builds its RegExp
# with the `i` flag, so the pattern uses only syntax common to Python and JS.
_PLACEHOLDER_NAME = re.compile(
    r"^\s*workspace:\s*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s*$",
    re.IGNORECASE,
)


def _is_placeholder_name(value: str) -> bool:
    return _PLACEHOLDER_NAME.match(value) is not None


def _clean_display_name(raw: str) -> str | None:
    """Trimmed name, or None if it is empty, is the placeholder or is too long."""
    name = raw.strip()
    if not name or len(name) > _DISPLAY_NAME_MAX or _is_placeholder_name(name):
        return None
    return name


def _safe_next(raw: str | None) -> str | None:
    """Validates that `raw` is a local path under /admin that is safe to redirect to.

    Prevents open redirect: only accepts paths that start with /admin
    (which already rules out `//host` and absolute URLs), without backslash, without
    control characters. Returns the clean value or None if it does not validate.
    """
    if not raw:
        return None
    if not raw.startswith("/admin"):
        return None
    if "\\" in raw:
        return None
    # No control characters (leading/trailing spaces, newlines, tabs)
    if raw != raw.strip():
        return None
    if "\n" in raw or "\r" in raw or "\t" in raw:
        return None
    return raw


def build_router(
    config: AuthConfig,
    require_session,
    require_session_optional,
    begin_limiter: BeginRateLimiter | None = None,
    *,
    complete_limiter: BeginRateLimiter | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"], include_in_schema=False)
    # Two instances with the same settings: `begin` and `complete`
    # do not share buckets, so an admitted `begin` always has its `complete`
    # token and several logins behind the same NAT are not rejected midway through
    # the ceremony, after the user has already touched their authenticator.
    if begin_limiter is None:
        begin_limiter = limiter_from_config(config)
    if complete_limiter is None:
        complete_limiter = limiter_from_config(config)

    def _rate_limited(
        request: Request, limiter: BeginRateLimiter = begin_limiter
    ) -> JSONResponse | None:
        """429 if the client exceeded the limit of `limiter`; None if it passes.

        Runs before touching the DB. The bounded WARNING is emitted by the limiter; neither it nor
        the body carries an IP or cookies."""
        retry_after = limiter.check(request.client.host if request.client else None)
        if retry_after is None:
            return None
        return JSONResponse(
            {"ok": False, "reason": "rate_limited", "retry_after": retry_after},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        )

    async def _refused(db: AsyncSession, inv: Invitation) -> bool:
        """True if the host asks to refuse the invitation (e.g. workspace
        disabled): same 410 as expired, revoked or used."""
        return config.is_invitation_refused is not None and await config.is_invitation_refused(
            db, inv
        )

    def _set_session_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(
            config.cookie_name,
            token,
            max_age=config.session_ttl_seconds,
            httponly=True,
            secure=config.cookie_secure,
            samesite=config.cookie_samesite,
            path="/",
        )

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        raw = request.query_params.get("next")
        safe_next = _safe_next(raw)
        return _TEMPLATES.TemplateResponse(
            request, "login.html", {"rp_id": config.rp_id, "next": safe_next}
        )

    @router.post("/login/begin")
    async def login_begin(request: Request, db: Annotated[AsyncSession, Depends(get_session)]):
        if (limited := _rate_limited(request)) is not None:
            return limited
        options_json, challenge_id = await passkey_service.begin_authentication(db, config)
        await db.commit()
        resp = JSONResponse(content=json.loads(options_json))
        resp.set_cookie(
            _CHALLENGE_COOKIE,
            challenge_id,
            max_age=config.challenge_ttl_seconds,
            httponly=True,
            secure=config.cookie_secure,
            samesite=config.cookie_samesite,
            path="/",
        )
        return resp

    @router.post("/login/complete")
    async def login_complete(request: Request, db: Annotated[AsyncSession, Depends(get_session)]):
        if (limited := _rate_limited(request, complete_limiter)) is not None:
            return limited
        challenge_id = request.cookies.get(_CHALLENGE_COOKIE)
        if not challenge_id:
            # Fixed text; there is no cookie to quote.
            logger.info("passkey login: complete without challenge cookie")
            return JSONResponse({"ok": False}, status_code=status.HTTP_400_BAD_REQUEST)
        # errors="replace": a body that is not UTF-8 must not break before
        # consuming the challenge; later parsing rejects it as malformed.
        body = (await request.body()).decode(errors="replace")
        user_id = await passkey_service.complete_authentication(
            db, config, challenge_id=challenge_id, response=body
        )
        if user_id is None:
            await db.commit()
            return JSONResponse({"ok": False}, status_code=status.HTTP_401_UNAUTHORIZED)
        token = await session_service.create_session(
            db,
            config,
            user_id=user_id,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        await db.commit()
        resp = JSONResponse({"ok": True, "redirect": "/"})
        _set_session_cookie(resp, token)
        resp.delete_cookie(_CHALLENGE_COOKIE, path="/")
        return resp

    @router.post("/logout")
    async def logout(request: Request, db: Annotated[AsyncSession, Depends(get_session)]):
        # Classic form POST (no JS): revokes the session, deletes the cookie and
        # redirects to /login with 303 (See Other → the browser does a GET).
        token = request.cookies.get(config.cookie_name)
        if token:
            await session_service.revoke_session(db, token)
            await db.commit()
        resp = RedirectResponse("/auth/login", status_code=status.HTTP_303_SEE_OTHER)
        resp.delete_cookie(config.cookie_name, path="/")
        return resp

    @router.get("/accept/{token}", response_class=HTMLResponse)
    async def accept_page(
        token: str,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_session)],
        user: Annotated[User | None, Depends(require_session_optional)],
    ):
        inv = await inv_service.peek_invitation(db, token)
        if inv is None or inv.revoked_at is not None or inv.accepted_at is not None:
            return _TEMPLATES.TemplateResponse(
                request,
                "accept_invite.html",
                {"invalid": True, "token": token, "logged_in": user is not None},
                status_code=status.HTTP_410_GONE,
            )
        # Minimal disclosure: an expired invitation, or one the host
        # asks to hide (disabled workspace), names no workspace, role or
        # label; the page shows a neutral line and keeps the rest of the flow.
        # Expiry is evaluated first: for an expired one the callback is not
        # called (no extra queries and no warnings with the role of a malformed
        # payload in the log).
        expired = inv.expires_at <= datetime.now(UTC)
        description = (
            None
            if expired or config.describe_invitation is None
            else await config.describe_invitation(db, inv)
        )
        withheld = expired or description == INVITATION_WITHHELD
        if withheld:
            description = None
        label = None if withheld else inv.label
        # Old invitations store `workspace:<uuid>` as label: it is not text
        # for people, so it is not shown (nor used as a name).
        if label is not None and _is_placeholder_name(label):
            label = None
        return _TEMPLATES.TemplateResponse(
            request,
            "accept_invite.html",
            {
                "invalid": False,
                "withheld": withheld,
                "token": token,
                "label": label,
                "description": description,
                "logged_in": user is not None,
                # With a session, the page offers to join as the current user.
                # Users created before the fix may be named `workspace:<uuid>`:
                # it is not shown as a name.
                "display_name": (
                    user.display_name
                    if user is not None and not _is_placeholder_name(user.display_name)
                    else None
                ),
                "rp_id": config.rp_id,
                "placeholder_pattern": _PLACEHOLDER_NAME.pattern,
            },
        )

    @router.post("/accept/{token}/join")
    async def accept_join(
        token: str,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_session)],
        user: Annotated[User | None, Depends(require_session_optional)],
    ):
        """An already authenticated user accepts the invitation with their account.

        No new user, passkey or session: it consumes the invitation on behalf
        of the current user and runs the host callback, all in a single
        transaction. The conditional UPDATE of `consume_invitation` resolves the
        race against another acceptance of the same token.
        """
        # The origin of the POST is validated by `AuthOriginCheckMiddleware`.
        # Without a valid session (absent, expired, revoked or user disabled,
        # see `resolve_session`) it returns to the invitation page, which
        # asks to log in; the invitation stays unconsumed.
        if user is None:
            return RedirectResponse(f"/auth/accept/{token}", status_code=status.HTTP_303_SEE_OTHER)
        # `get_session` is cached per request: `user` already belongs to this same
        # DB session, so it is passed as is to the callback. Only the HTTP
        # session row is missing, which the callback contract also receives.
        sess = await db.get(Session, hash_token(request.cookies[config.cookie_name]))
        state, inv = await inv_service.consume_invitation(db, token, user_id=user.id)
        if state is inv_service.InvitationState.OK and await _refused(db, inv):
            state = inv_service.InvitationState.REFUSED
        if state is not inv_service.InvitationState.OK:
            # The rollback also undoes the consumption if the host refused it.
            await db.rollback()
            return _TEMPLATES.TemplateResponse(
                request,
                "accept_invite.html",
                {"invalid": True, "token": token, "logged_in": True},
                status_code=status.HTTP_410_GONE,
            )
        if config.on_invitation_accepted is not None:
            await config.on_invitation_accepted(db, user, inv, sess)
        await db.commit()
        return RedirectResponse("/admin/", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/accept/{token}/register/begin")
    async def accept_register_begin(
        token: str,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_session)],
    ):
        if (limited := _rate_limited(request)) is not None:
            return limited
        inv = await inv_service.peek_invitation(db, token)
        if (
            inv is None
            or inv.revoked_at is not None
            or inv.accepted_at is not None
            or await _refused(db, inv)
        ):
            return JSONResponse({"ok": False}, status_code=status.HTTP_410_GONE)
        raw_name = request.query_params.get("display_name")
        # Compat: without the parameter the default name is kept; with an empty
        # parameter or the `workspace:<uuid>` placeholder it is rejected.
        display_name = "New user" if raw_name is None else _clean_display_name(raw_name)
        if display_name is None:
            return JSONResponse(
                {"ok": False, "reason": "invalid_display_name"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        # Ephemeral WebAuthn handle: NOT a row in `users`. The real user is
        # created only in /complete, within a single transaction that is committed
        # only if the whole ceremony succeeds. This avoids orphan users.
        handle = str(uuid.uuid4())
        options_json, challenge_id = await passkey_service.begin_registration(
            db, config, user_id=handle, user_name=display_name
        )
        # We only persist the challenge (with user_id=None); the handle is discarded.
        await db.commit()
        # The cookie carries challenge_id + display_name so that /complete can
        # create the user. base64(JSON) avoids problems with delimiters.
        payload = base64.urlsafe_b64encode(
            json.dumps({"cid": challenge_id, "name": display_name}).encode()
        ).decode()
        resp = JSONResponse(content=json.loads(options_json))
        resp.set_cookie(
            _CHALLENGE_COOKIE,
            payload,
            max_age=config.challenge_ttl_seconds,
            httponly=True,
            secure=config.cookie_secure,
            samesite=config.cookie_samesite,
            path="/",
        )
        return resp

    @router.post("/accept/{token}/register/complete")
    async def accept_register_complete(
        token: str,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_session)],
    ):
        if (limited := _rate_limited(request, complete_limiter)) is not None:
            return limited
        raw = request.cookies.get(_CHALLENGE_COOKIE, "")
        try:
            data = json.loads(base64.urlsafe_b64decode(raw.encode()))
            challenge_id = data["cid"]
            display_name = _clean_display_name(data["name"])
            if display_name is None:
                raise ValueError("invalid display name")
        except Exception:  # noqa: BLE001 — missing/corrupt cookie = bad request
            return JSONResponse({"ok": False}, status_code=status.HTTP_400_BAD_REQUEST)
        # errors="replace": a body that is not UTF-8 reaches verification, which
        # rejects it as an invalid attestation (400), instead of a 500.
        body = (await request.body()).decode(errors="replace")
        # Everything in a single transaction without commit; any early return
        # rolls back when the get_session context closes (no auto-commit).
        # Nothing is inserted into `users` until the challenge, the attestation
        # and the invitation pass; a forged cookie or an invalid invitation no longer
        # inserts (and reverts) a row.
        # 1. Challenge + attestation (consumes the challenge, without touching `users`).
        verification = await passkey_service.verify_registration(
            db, config, challenge_id=challenge_id, response=body
        )
        if verification is None:
            return JSONResponse({"ok": False}, status_code=status.HTTP_400_BAD_REQUEST)
        # 2. Invitation state, before creating anything. Same 410 and `reason`
        #    as the consumption below.
        inv = await inv_service.peek_invitation(db, token)
        pre_state = inv_service.invitation_state(inv)
        if pre_state is inv_service.InvitationState.OK and await _refused(db, inv):
            pre_state = inv_service.InvitationState.REFUSED
        if pre_state is not inv_service.InvitationState.OK:
            return JSONResponse(
                {"ok": False, "reason": pre_state.value}, status_code=status.HTTP_410_GONE
            )
        # 3. User and credential (no commit).
        res = await db.execute(insert(User).values(display_name=display_name).returning(User.id))
        uid = res.scalar_one()
        await passkey_service.store_credential(
            db, user_id=uid, verification=verification, label=None
        )
        # 4. Consume the invitation. The conditional UPDATE loses the race if
        #    another ceremony already accepted it → early return rolls back user+cred.
        state, inv = await inv_service.consume_invitation(db, token, user_id=uid)
        if state is not inv_service.InvitationState.OK:
            return JSONResponse(
                {"ok": False, "reason": state.value}, status_code=status.HTTP_410_GONE
            )
        # 5. Create the session, run the callback and commit everything together.
        token_sess = await session_service.create_session(
            db,
            config,
            user_id=uid,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        if config.on_invitation_accepted is not None:
            user_obj = await db.get(User, uid)
            sess = await db.get(Session, hash_token(token_sess))
            await config.on_invitation_accepted(db, user_obj, inv, sess)
        await db.commit()
        resp = JSONResponse({"ok": True, "redirect": "/"})
        _set_session_cookie(resp, token_sess)
        resp.delete_cookie(_CHALLENGE_COOKIE, path="/")
        return resp

    return router
