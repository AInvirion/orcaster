"""WebAuthn ceremonies (registration and authentication) on top of py_webauthn v2.

The challenge is persisted in `webauthn_challenges` and referenced by id (which
the router puts in an ephemeral cookie). begin_* returns (options_json, challenge_id);
complete_* validates the authenticator response against the stored challenge.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import webauthn
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers import options_to_json, parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
)
from webauthn.registration.verify_registration_response import VerifiedRegistration

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.models import PasskeyCredential, User, WebauthnChallenge
from orcaster_server.auth.rate_limit import BoundedWarning

logger = logging.getLogger(__name__)

# Cap on the py_webauthn message that reaches the log. The message may quote
# client values (origin, type of clientDataJSON) with no length limit;
# without a cap, a client that knows a credential id would write a log line
# the size of the body per request.
_LOG_REASON_MAX_CHARS = 200


# Evictions caused by the `challenge_max_pending` cap: with the cap full each
# `begin` evicts pending challenges that may belong to other users. One
# bounded WARNING with the count, not one per request.
_evictions_warning = BoundedWarning(
    logger,
    "passkey: evicted %d pending challenge(s) to stay under challenge_max_pending "
    "(a flood of anonymous begin requests, or a cap too low for the real load; "
    "evicted users must restart their login)",
)


def _bounded_reason(exc: Exception) -> str:
    reason = str(exc)
    if len(reason) > _LOG_REASON_MAX_CHARS:
        reason = f"{reason[:_LOG_REASON_MAX_CHARS]}... ({len(reason)} chars)"
    return reason


async def _store_challenge(
    db: AsyncSession,
    config: AuthConfig,
    *,
    challenge: bytes,
    kind: str,
    user_id: str | None,
) -> str:
    # `login/begin` is anonymous: without a bound, a client fills the table with
    # POSTs. Cheap bound at insert time, with no migration or separate task: (1)
    # expired rows are purged and (2) at most `challenge_max_pending - 1`
    # pending rows of the same `kind` are kept, evicting the oldest, so that
    # the new insert leaves the total at the cap. Evicting (instead of rejecting)
    # bounds table growth and avoids a total block of new
    # logins, but does NOT protect other users' challenges: a sustained anonymous
    # flood of `login/begin` (on the order of a thousand requests, seconds locally)
    # can evict another user's pending challenge, who must retry.
    # The per-client and per-network limit on `begin` calls
    # (`auth.rate_limit`), which rejects before reaching here, slows this down but does not
    # eliminate it: with the default values a /24 reaches a thousand `begin` calls in about
    # 180 s, under the TTL. Each eviction is logged. Under
    # concurrency the cap can be exceeded by up to the number of `begin` calls in
    # flight; the next insertion corrects it.
    now = datetime.now(UTC)
    await db.execute(delete(WebauthnChallenge).where(WebauthnChallenge.expires_at <= now))
    # `challenge_max_pending >= 1` is guaranteed by AuthConfig: with cap 1 the LIMIT
    # is 0 and everything pending of that kind is evicted before the insert.
    keep = (
        select(WebauthnChallenge.id)
        .where(WebauthnChallenge.kind == kind)
        .order_by(WebauthnChallenge.created_at.desc(), WebauthnChallenge.id)
        .limit(config.challenge_max_pending - 1)
        .scalar_subquery()
    )
    evicted = await db.execute(
        delete(WebauthnChallenge).where(
            WebauthnChallenge.kind == kind, WebauthnChallenge.id.not_in(keep)
        )
    )
    # `rowcount` is -1 when the driver does not know the count: it only counts if > 0.
    if evicted.rowcount > 0:
        _evictions_warning.hit(evicted.rowcount)
    res = await db.execute(
        insert(WebauthnChallenge)
        .values(
            challenge=challenge,
            kind=kind,
            user_id=user_id,
            expires_at=now + timedelta(seconds=config.challenge_ttl_seconds),
        )
        .returning(WebauthnChallenge.id)
    )
    return res.scalar_one()


def _is_canonical_uuid(value: object) -> bool:
    """True only for the form we emit: `str` UUID lowercase with hyphens.

    `uuid.UUID()` alone is not enough: it accepts `{<uuid>}`, `urn:uuid:<uuid>`, uppercase
    and no hyphens, forms that asyncpg rejects (500) or that the server never emits.
    """
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


async def _take_challenge(db: AsyncSession, challenge_id: str, kind: str) -> bytes | None:
    """Takes and deletes the challenge (single-use). None if it does not exist or expired.

    `DELETE ... RETURNING` is atomic: of two concurrent requests with the
    same id, the second waits for the row lock and finds nothing.
    The challenge is consumed even if it has expired.
    """
    # The id comes from a cookie the client controls (in registration, from a
    # JSON inside the cookie: it may not even be a `str`). A non-canonical id is
    # rejected as an unknown challenge, with a single bounded WARNING that does not
    # quote the value, instead of reaching the DB and ending in a 500.
    if not _is_canonical_uuid(challenge_id):
        logger.warning("passkey %s: malformed challenge id", kind)
        return None
    res = await db.execute(
        delete(WebauthnChallenge)
        .where(WebauthnChallenge.id == challenge_id, WebauthnChallenge.kind == kind)
        .returning(WebauthnChallenge.challenge, WebauthnChallenge.expires_at)
    )
    row = res.one_or_none()
    if row is None or row.expires_at <= datetime.now(UTC):
        # Fixed text, without the id (it comes from the client's cookie). Covers
        # a nonexistent challenge, an expired one and one of another `kind`.
        logger.info("passkey %s: challenge missing, expired or of another kind", kind)
        return None
    return row.challenge


async def begin_registration(
    db: AsyncSession, config: AuthConfig, *, user_id: str, user_name: str
) -> tuple[str, str]:
    options = webauthn.generate_registration_options(
        rp_id=config.rp_id,
        rp_name=config.rp_name,
        user_id=str(user_id).encode("utf-8"),
        user_name=user_name,
        user_display_name=user_name,
        # Discoverable credential required: login is usernameless
        # (empty allow_credentials), so a non-resident credential
        # (typical of security keys) would not work. user_verification is
        # left untouched.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED
        ),
    )
    # The challenge is stored with user_id=None: registration may happen before
    # the row in `users` exists (accept-invite flow), and the column
    # webauthn_challenges.user_id has an FK to users(id). The WebAuthn handle is only
    # used as `user.id` in the options; it is not a DB id.
    challenge_id = await _store_challenge(
        db, config, challenge=options.challenge, kind="registration", user_id=None
    )
    return options_to_json(options), challenge_id


async def verify_registration(
    db: AsyncSession, config: AuthConfig, *, challenge_id: str, response: str
) -> VerifiedRegistration | None:
    """Consumes the challenge and verifies the attestation, without touching `users` or
    `passkey_credentials`: the caller creates the user only if this passes.
    It does not commit: the consumption stays in the caller's transaction."""
    expected = await _take_challenge(db, challenge_id, "registration")
    if expected is None:
        return None
    try:
        # verify_registration_response acepta str | dict | RegistrationCredential.
        return webauthn.verify_registration_response(
            credential=response,
            expected_challenge=expected,
            expected_rp_id=config.rp_id,
            expected_origin=config.origin,
        )
    except (WebAuthnException, ValueError, RecursionError) as exc:
        # Invalid attestation or a body that is not a credential (broken JSON,
        # `{}`, missing fields, invalid base64) = registration rejected with the
        # generic `400`, not a `500`. Same as in login: type and
        # truncated message, with %r; never the challenge or the body.
        logger.warning(
            "passkey registration: attestation rejected: %s: %r",
            type(exc).__name__,
            _bounded_reason(exc),
        )
        return None


async def complete_registration(
    db: AsyncSession,
    config: AuthConfig,
    *,
    challenge_id: str,
    user_id: str,
    response: str,
    label: str | None,
) -> PasskeyCredential | None:
    verification = await verify_registration(
        db, config, challenge_id=challenge_id, response=response
    )
    if verification is None:
        return None
    return await store_credential(db, user_id=user_id, verification=verification, label=label)


async def store_credential(
    db: AsyncSession,
    *,
    user_id: str,
    verification: VerifiedRegistration,
    label: str | None,
) -> PasskeyCredential:
    # aaguid is a str in py_webauthn 2.7.1; may be all-zeros for software authenticators
    aaguid = verification.aaguid if verification.aaguid else None
    res = await db.execute(
        insert(PasskeyCredential)
        .values(
            user_id=str(user_id),
            credential_id=verification.credential_id,
            public_key_cose=verification.credential_public_key,
            sign_count=verification.sign_count,
            aaguid=aaguid,
            backup_eligible=bool(verification.credential_backed_up),
            backup_state=bool(verification.credential_backed_up),
            label=label,
        )
        .returning(PasskeyCredential.id)
    )
    new_id = res.scalar_one()
    out = await db.execute(select(PasskeyCredential).where(PasskeyCredential.id == new_id))
    return out.scalar_one()


async def begin_authentication(db: AsyncSession, config: AuthConfig) -> tuple[str, str]:
    """Discoverable login: we do not enumerate credentials (empty allow_credentials)."""
    options = webauthn.generate_authentication_options(rp_id=config.rp_id)
    challenge_id = await _store_challenge(
        db, config, challenge=options.challenge, kind="authentication", user_id=None
    )
    return options_to_json(options), challenge_id


async def complete_authentication(
    db: AsyncSession, config: AuthConfig, *, challenge_id: str, response: str
) -> str | None:
    """Verifies the assertion. Returns user_id if valid, None if not."""
    expected = await _take_challenge(db, challenge_id, "authentication")
    # Only in login: the consumption is committed now, before verifying, so that
    # a later error (the rollback of a 500) does not return the challenge to the
    # table until its TTL. Registration keeps its single transaction on
    # purpose. At this point the session has no other pending changes.
    await db.commit()
    if expected is None:
        return None
    try:
        parsed = parse_authentication_credential_json(response)
    except (WebAuthnException, ValueError, RecursionError) as exc:
        # Body that is not a valid credential (broken JSON, missing fields,
        # invalid base64, JSON nested until RecursionError) = login rejected,
        # same as a bad assertion. No DB ids: there is no credential yet.
        logger.warning(
            "passkey login: malformed credential body: %s: %r",
            type(exc).__name__,
            _bounded_reason(exc),
        )
        return None
    row = await db.execute(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == parsed.raw_id)
    )
    cred = row.scalar_one_or_none()
    if cred is None:
        # Fixed text and no ids; the credential id is sent by the client.
        # A rise in these lines = old passkeys of deleted accounts.
        logger.info("passkey login: unknown credential")
        return None
    # I2: a disabled user must not be able to authenticate (no session is minted).
    user = await db.get(User, cred.user_id)
    if user is None or user.disabled_at is not None:
        # WARNING with DB ids only, so that whoever disabled the
        # account sees the attempts.
        logger.warning(
            "passkey login: rejected for credential %s (user %s): %s",
            cred.id,
            cred.user_id,
            "user row missing" if user is None else "user disabled",
        )
        return None
    try:
        verification = webauthn.verify_authentication_response(
            credential=parsed,
            expected_challenge=expected,
            expected_rp_id=config.rp_id,
            expected_origin=config.origin,
            credential_public_key=cred.public_key_cose,
            credential_current_sign_count=cred.sign_count,
        )
    except WebAuthnException as exc:
        # Invalid assertion = failed login. The client only gets the generic
        # 401; the reason stays in the server log. The exception type and
        # message and the DB ids are logged; never the
        # challenge, the credential or the raw clientDataJSON. The
        # py_webauthn message may quote client values (e.g. the origin), so
        # it goes through %r: it escapes line breaks and does not allow forging lines.
        # It is also truncated to _LOG_REASON_MAX_CHARS so that the client cannot
        # inflate the log.
        reason = _bounded_reason(exc)
        logger.warning(
            "passkey login: assertion rejected for credential %s (user %s): %s: %r",
            cred.id,
            cred.user_id,
            type(exc).__name__,
            reason,
        )
        return None
    except Exception:
        # Error not anticipated by py_webauthn (bug, corrupt stored key, changed
        # library API): this is not a rejected login but a server
        # failure, so it propagates and the request ends in a 500.
        # The traceback is logged by uvicorn ("Exception in ASGI application");
        # here only a short line is left, without exc_info, with the DB ids
        # that the traceback does not carry.
        logger.error(
            "passkey login: unexpected error verifying assertion for credential %s (user %s)",
            cred.id,
            cred.user_id,
        )
        raise
    await db.execute(
        update(PasskeyCredential)
        .where(PasskeyCredential.id == cred.id)
        .values(
            # The stored counter can never go down. `cred.sign_count` was
            # read without a lock, so two concurrent logins of the same
            # credential both verify against the same old value; with a plain
            # `SET sign_count = <new>` the one that commits last writes its
            # own, and if it is the lower one the stored count goes backwards and
            # accepts an already seen assertion again. `GREATEST` resolves it inside the
            # statement: the UPDATE takes the row lock and rereads the value the
            # other one already committed, without `SELECT ... FOR UPDATE` and without serializing
            # the logins of one credential.
            sign_count=func.greatest(PasskeyCredential.sign_count, verification.new_sign_count),
            # `last_used_at` is always written, even when the counter does not
            # move: a synced passkey leaves it at 0 on every login, so
            # conditioning the UPDATE on the counter rising would leave those
            # credentials without a last-used date.
            last_used_at=datetime.now(UTC),
        )
    )
    return cred.user_id
