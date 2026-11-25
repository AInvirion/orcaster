import json
import logging

import pytest
import webauthn
from sqlalchemy import text

from orcaster_server.auth.config import AuthConfig
from orcaster_server.auth.service.passkey import (
    begin_authentication,
    begin_registration,
    complete_authentication,
    complete_registration,
)
from tests.auth.test_passkey_registration import (
    _attestation_to_json,
    _to_soft_create_options,
)
from tests.soft_webauthn_device import SoftWebauthnDevice

CFG = AuthConfig(rp_id="localhost", rp_name="Test", origin="https://localhost")


async def _register(db_session, device: SoftWebauthnDevice) -> str:
    row = await db_session.execute(
        text("INSERT INTO users (display_name) VALUES ('Ada') RETURNING id")
    )
    uid = str(row.scalar_one())
    await db_session.commit()
    opts_json, cid = await begin_registration(db_session, CFG, user_id=uid, user_name="Ada")
    await db_session.commit()
    att = device.create({"publicKey": _to_soft_create_options(json.loads(opts_json))}, CFG.origin)
    await complete_registration(
        db_session,
        CFG,
        challenge_id=cid,
        user_id=uid,
        response=_attestation_to_json(att),
        label=None,
    )
    await db_session.commit()
    return uid


def _assertion_to_json(ass: dict) -> str:
    import base64

    def b(x: bytes) -> str:
        return base64.urlsafe_b64encode(x).decode().rstrip("=")

    user_handle = ass["response"]["userHandle"]
    return json.dumps(
        {
            "id": b(ass["rawId"]),
            "rawId": b(ass["rawId"]),
            "type": ass["type"],
            "response": {
                "clientDataJSON": b(ass["response"]["clientDataJSON"]),
                "authenticatorData": b(ass["response"]["authenticatorData"]),
                "signature": b(ass["response"]["signature"]),
                "userHandle": b(user_handle) if user_handle is not None else None,
            },
            "clientExtensionResults": {},
        }
    )


def _to_soft_get_options(options: dict) -> dict:
    import base64

    def b64url_to_bytes(s: str) -> bytes:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad)

    opts = dict(options)
    opts["challenge"] = b64url_to_bytes(options["challenge"])
    return opts


@pytest.mark.asyncio
async def test_authentication_returns_user_id(db_session):
    device = SoftWebauthnDevice()
    uid = await _register(db_session, device)

    opts_json, cid = await begin_authentication(db_session, CFG)
    await db_session.commit()
    assertion = device.get({"publicKey": _to_soft_get_options(json.loads(opts_json))}, CFG.origin)

    result = await complete_authentication(
        db_session, CFG, challenge_id=cid, response=_assertion_to_json(assertion)
    )
    await db_session.commit()
    assert result == uid


@pytest.mark.asyncio
async def test_authentication_bad_challenge_returns_none(db_session):
    result = await complete_authentication(
        db_session,
        CFG,
        challenge_id="00000000-0000-0000-0000-000000000000",
        response="{}",
    )
    assert result is None


async def _assertion_for(db_session, device: SoftWebauthnDevice, origin: str):
    """Start a ceremony and sign the assertion with the given origin."""
    opts_json, cid = await begin_authentication(db_session, CFG)
    await db_session.commit()
    options = json.loads(opts_json)
    assertion = device.get({"publicKey": _to_soft_get_options(options)}, origin)
    return cid, options["challenge"], _assertion_to_json(assertion)


def _passkey_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "orcaster_server.auth.service.passkey"]


@pytest.mark.asyncio
async def test_rejected_assertion_is_logged_without_secrets(db_session, caplog):
    # An invalid assertion still returns None (the router answers the same
    # generic 401), but the reason is logged at WARNING.
    device = SoftWebauthnDevice()
    await _register(db_session, device)
    cid, challenge, response = await _assertion_for(db_session, device, "https://evil.example")

    with caplog.at_level(logging.WARNING, logger="orcaster_server.auth.service.passkey"):
        result = await complete_authentication(db_session, CFG, challenge_id=cid, response=response)

    assert result is None
    records = _passkey_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.WARNING
    assert rec.exc_info is None
    msg = rec.getMessage()
    assert "InvalidAuthenticationResponse" in msg
    assert "origin" in msg
    # Never ceremony material: neither the challenge nor the raw response.
    parsed = json.loads(response)
    for secret in (
        challenge,
        parsed["rawId"],
        parsed["response"]["clientDataJSON"],
        parsed["response"]["authenticatorData"],
        parsed["response"]["signature"],
    ):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_unexpected_verify_error_propagates(db_session, caplog, monkeypatch):
    # An exception unrelated to py_webauthn is not mistaken for an invalid
    # assertion: it propagates (the request ends in 500) and leaves a single
    # short ERROR line, without exc_info (uvicorn records the traceback).
    device = SoftWebauthnDevice()
    await _register(db_session, device)
    cid, _, response = await _assertion_for(db_session, device, CFG.origin)

    def boom(**_kwargs):
        raise RuntimeError("broken stored key")

    monkeypatch.setattr(webauthn, "verify_authentication_response", boom)
    with (
        caplog.at_level(logging.WARNING, logger="orcaster_server.auth.service.passkey"),
        pytest.raises(RuntimeError, match="broken stored key"),
    ):
        await complete_authentication(db_session, CFG, challenge_id=cid, response=response)

    records = _passkey_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is None
    assert "broken stored key" not in records[0].getMessage()


@pytest.mark.asyncio
async def test_login_complete_unexpected_verify_error_returns_generic_500(
    app_client, db_session, monkeypatch
):
    # Over HTTP: the unexpected error comes out as a 500 with Starlette's generic
    # body, with no traceback or exception text. The challenge was already
    # consumed (own commit after the take): the request rollback does not
    # resurrect it.
    from httpx import ASGITransport, AsyncClient

    from orcaster_server.main import app

    device = SoftWebauthnDevice()
    await _register(db_session, device)

    begin = await app_client.post("/auth/login/begin")
    assert begin.status_code == 200
    cid = begin.cookies["webauthn_challenge"]
    options = begin.json()
    assertion = device.get({"publicKey": _to_soft_get_options(options)}, CFG.origin)
    body = _assertion_to_json(assertion)

    def boom(**_kwargs):
        raise RuntimeError("SECRET-INTERNAL-DETAIL")

    monkeypatch.setattr(webauthn, "verify_authentication_response", boom)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies={"webauthn_challenge": cid}
    ) as client:
        res = await client.post("/auth/login/complete", content=body)

    assert res.status_code == 500
    assert res.text == "Internal Server Error"
    assert "SECRET-INTERNAL-DETAIL" not in res.text
    assert "Traceback" not in res.text
    assert "RuntimeError" not in res.text
    assert "session=" not in res.headers.get("set-cookie", "")

    left = await db_session.execute(
        text("SELECT count(*) FROM webauthn_challenges WHERE id = :id"), {"id": cid}
    )
    assert left.scalar_one() == 0


@pytest.mark.asyncio
async def test_valid_assertion_logs_nothing(db_session, caplog):
    device = SoftWebauthnDevice()
    uid = await _register(db_session, device)
    cid, _, response = await _assertion_for(db_session, device, CFG.origin)

    with caplog.at_level(logging.WARNING, logger="orcaster_server.auth.service.passkey"):
        result = await complete_authentication(db_session, CFG, challenge_id=cid, response=response)

    assert result == uid
    assert not _passkey_records(caplog)


@pytest.mark.asyncio
async def test_rejected_assertion_log_is_bounded_and_single_line(db_session, caplog):
    # The py_webauthn message quotes the client origin: a huge origin with a
    # newline cannot inflate the log or forge a new line.
    device = SoftWebauthnDevice()
    await _register(db_session, device)
    hostile = "https://evil.example/" + "a" * 100_000 + "\nCRITICAL forged line"
    cid, _, response = await _assertion_for(db_session, device, hostile)

    with caplog.at_level(logging.WARNING, logger="orcaster_server.auth.service.passkey"):
        result = await complete_authentication(db_session, CFG, challenge_id=cid, response=response)

    assert result is None
    records = _passkey_records(caplog)
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "InvalidAuthenticationResponse" in msg
    assert "\n" not in msg
    assert "forged line" not in msg
    assert len(msg) < 600


async def _count_challenges(db_session) -> int:
    await db_session.rollback()
    res = await db_session.execute(text("SELECT count(*) FROM webauthn_challenges"))
    return res.scalar_one()


@pytest.mark.asyncio
async def test_concurrent_take_challenge_has_single_winner(db_session):
    # An unchecked SELECT + DELETE let two requests with the same challenge both
    # receive it. With DELETE ... RETURNING only one wins.
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from orcaster_server.auth.service.passkey import _take_challenge

    _, cid = await begin_authentication(db_session, CFG)
    await db_session.commit()

    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def take() -> bytes | None:
        async with maker() as s:
            got = await _take_challenge(s, cid, "authentication")
            await asyncio.sleep(0.2)  # keeps the tx open: the other waits for the lock
            await s.commit()
            return got

    results = await asyncio.gather(take(), take())
    assert sum(r is not None for r in results) == 1


@pytest.mark.asyncio
async def test_take_challenge_expired_is_consumed_and_rejected(db_session):
    _, cid = await begin_authentication(db_session, CFG)
    await db_session.execute(
        text("UPDATE webauthn_challenges SET expires_at = now() - interval '1 second'")
    )
    await db_session.commit()

    from orcaster_server.auth.service.passkey import _take_challenge

    assert await _take_challenge(db_session, cid, "authentication") is None
    await db_session.commit()
    assert await _count_challenges(db_session) == 0


async def _post_complete(app, cid: str, content: bytes):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies={"webauthn_challenge": cid}
    ) as client:
        return await client.post("/auth/login/complete", content=content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'{"id": 1}',
        b'{"id": "x", "rawId": "x", "type": "public-key", "response": {}}',
        b"\xff\xfe\x00 not utf-8",
        b"[" * 200_000,
    ],
)
async def test_login_complete_malformed_body_is_401_and_consumes_challenge(
    app_client, db_session, caplog, body
):
    # A malformed body must give the same 401 as any rejected login, with a
    # single bounded WARNING and no traceback; the challenge is left consumed.
    from orcaster_server.main import app

    _, cid = await begin_authentication(db_session, CFG)
    await db_session.commit()

    with caplog.at_level(logging.WARNING):
        res = await _post_complete(app, cid, body)

    assert res.status_code == 401
    assert res.json() == {"ok": False}
    assert "session=" not in res.headers.get("set-cookie", "")
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    assert records[0].name == "orcaster_server.auth.service.passkey"
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert len(records[0].getMessage()) < 600
    assert await _count_challenges(db_session) == 0


@pytest.mark.asyncio
async def test_login_complete_huge_malformed_body_log_is_bounded(app_client, db_session, caplog):
    from orcaster_server.main import app

    _, cid = await begin_authentication(db_session, CFG)
    await db_session.commit()
    with caplog.at_level(logging.WARNING):
        res = await _post_complete(app, cid, b"{" + b"a" * 100_000 + b"\nCRITICAL forged")

    assert res.status_code == 401
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(msgs) == 1
    assert "\n" not in msgs[0]
    assert len(msgs[0]) < 600


@pytest.mark.asyncio
async def test_begin_purges_expired_challenges(db_session):
    # Table bound: begin purges the expired ones on insert.
    for _ in range(3):
        await begin_authentication(db_session, CFG)
    await db_session.execute(
        text("UPDATE webauthn_challenges SET expires_at = now() - interval '1 second'")
    )
    await db_session.commit()

    await begin_authentication(db_session, CFG)
    await db_session.commit()
    assert await _count_challenges(db_session) == 1


@pytest.mark.asyncio
async def test_begin_caps_pending_challenges_evicting_oldest(db_session):
    cfg = AuthConfig(
        rp_id="localhost", rp_name="Test", origin="https://localhost", challenge_max_pending=5
    )
    ids = []
    for _ in range(12):
        _, cid = await begin_authentication(db_session, cfg)
        await db_session.commit()
        ids.append(cid)

    assert await _count_challenges(db_session) == 5
    rows = await db_session.execute(text("SELECT id::text FROM webauthn_challenges"))
    assert {r[0] for r in rows} == set(ids[-5:])


# --- non-canonical challenge ids (client-controlled cookie) -------------------


def _cookie_variants(cid: str) -> dict[str, str]:
    """Forms that `uuid.UUID()` accepts (or not) but the server never emits."""
    return {
        "braces": "{" + cid + "}",
        "urn": "urn:uuid:" + cid,
        "uppercase": cid.upper(),
        "no_hyphens": cid.replace("-", ""),
        "not_a_uuid": "abc",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["braces", "urn", "uppercase", "no_hyphens", "not_a_uuid"])
async def test_login_complete_non_canonical_challenge_id_is_401(
    app_client, db_session, caplog, variant
):
    # `{<uuid>}` and `urn:uuid:<uuid>` would pass the `uuid.UUID()` guard and
    # asyncpg would reject them (anonymous 500). Only the form we emit is valid:
    # lowercase hyphenated UUID. The rejection is the generic 401, with a single
    # bounded WARNING that does not quote the cookie, and it leaves the real
    # challenge untouched.
    from orcaster_server.main import app

    _, cid = await begin_authentication(db_session, CFG)
    await db_session.commit()
    raw = _cookie_variants(cid)[variant]

    with caplog.at_level(logging.WARNING):
        res = await _post_complete(app, raw, b"{}")

    assert res.status_code == 401
    assert res.json() == {"ok": False}
    assert "session=" not in res.headers.get("set-cookie", "")
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    assert records[0].name == "orcaster_server.auth.service.passkey"
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert "malformed challenge id" in records[0].getMessage()
    assert len(records[0].getMessage()) < 200
    assert raw not in caplog.text
    assert await _count_challenges(db_session) == 1


@pytest.mark.asyncio
async def test_login_complete_empty_challenge_cookie_is_400_like_missing(app_client, caplog):
    # An empty cookie is the same as a missing one: the earlier branch (400),
    # before touching the DB and without a log.
    from orcaster_server.main import app

    with caplog.at_level(logging.WARNING):
        res = await _post_complete(app, "", b"{}")

    assert res.status_code == 400
    assert res.json() == {"ok": False}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def _registration_cookie(cid) -> str:
    import base64

    raw = json.dumps({"cid": cid, "name": "Ada"}).encode()
    return base64.urlsafe_b64encode(raw).decode()


async def _post_register_complete(app, cookie: str):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies={"webauthn_challenge": cookie}
    ) as client:
        return await client.post("/auth/accept/no-such-token/register/complete", content=b"{}")


@pytest.mark.asyncio
async def test_register_complete_unknown_challenge_is_400(app_client, db_session):
    # Baseline: a canonical UUID challenge that does not exist gives 400 and
    # leaves no user (the single transaction is rolled back).
    import uuid

    from orcaster_server.main import app

    res = await _post_register_complete(app, _registration_cookie(str(uuid.uuid4())))

    assert res.status_code == 400
    assert res.json() == {"ok": False}
    users = await db_session.execute(text("SELECT count(*) FROM users"))
    assert users.scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cid",
    [None, 123, ["x"], {"a": 1}, True, "abc", "{12345678-1234-1234-1234-123456789abc}"],
    ids=["null", "int", "list", "dict", "bool", "not_a_uuid", "braces"],
)
async def test_register_complete_malformed_cid_is_400_like_unknown(
    app_client, db_session, caplog, cid
):
    # `cid: null` would give 400 and the `uuid.UUID(None)` guard turned it into
    # a 500 (TypeError); the other non-str values and `{<uuid>}` gave 500 from a
    # DB error. All are an unknown challenge: the same 400, with a single bounded
    # WARNING, no 500 and no user left behind.
    from orcaster_server.main import app

    with caplog.at_level(logging.WARNING):
        res = await _post_register_complete(app, _registration_cookie(cid))

    assert res.status_code == 400
    assert res.json() == {"ok": False}
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    assert records[0].name == "orcaster_server.auth.service.passkey"
    assert records[0].getMessage() == "passkey registration: malformed challenge id"
    users = await db_session.execute(text("SELECT count(*) FROM users"))
    assert users.scalar_one() == 0


# --- challenge_max_pending ----------------------------------------------------


@pytest.mark.parametrize("bad", [0, -5, True, 2.5, "5", None])
def test_challenge_max_pending_must_be_a_positive_int(bad):
    with pytest.raises(ValueError, match="challenge_max_pending"):
        AuthConfig(
            rp_id="localhost", rp_name="Test", origin="https://localhost", challenge_max_pending=bad
        )


@pytest.mark.asyncio
async def test_challenge_cap_of_one_keeps_only_the_newest_row_per_kind(db_session):
    cfg = AuthConfig(
        rp_id="localhost", rp_name="Test", origin="https://localhost", challenge_max_pending=1
    )
    _, reg_id = await begin_registration(db_session, cfg, user_id="u1", user_name="Ada")
    await db_session.commit()
    _, first = await begin_authentication(db_session, cfg)
    await db_session.commit()
    _, second = await begin_authentication(db_session, cfg)
    await db_session.commit()

    rows = await db_session.execute(text("SELECT id::text, kind FROM webauthn_challenges"))
    got = {(r[0], r[1]) for r in rows}
    # Only one pending per kind: the newest of each; kinds do not evict each other.
    assert got == {(reg_id, "registration"), (second, "authentication")}
    assert first not in {r[0] for r in got}


# --- sign_count: the stored counter never decreases ---------------------------


class _ZeroCounterDevice(SoftWebauthnDevice):
    """Synced passkey: signs every assertion with the counter at 0."""

    def get(self, options: dict, origin: str) -> dict:
        # `SoftWebauthnDevice.get` increments before packing the counter;
        # leaving it at -1 makes the assertion carry 0, like a synced
        # credential that keeps no count.
        self.sign_count = -1
        return super().get(options, origin)


async def _credential_row(db_session):
    await db_session.rollback()
    res = await db_session.execute(text("SELECT sign_count, last_used_at FROM passkey_credentials"))
    return res.one()


@pytest.mark.asyncio
async def test_concurrent_logins_never_move_sign_count_backwards(db_session):
    # A plain `SET sign_count = <new>` let the login that commits last write its
    # counter even if lower, and a stored count that goes backwards accepts an
    # already-seen assertion again.
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    device = SoftWebauthnDevice()
    uid = await _register(db_session, device)
    # An authenticator with a real counter (a security key) sitting at 5.
    await db_session.execute(text("UPDATE passkey_credentials SET sign_count = 5"))
    await db_session.commit()
    device.sign_count = 5

    # Two ceremonies: the first assertion carries 6 and the second 7. Both
    # verify against 5, the stored count both of them see.
    opts_low, cid_low = await begin_authentication(db_session, CFG)
    opts_high, cid_high = await begin_authentication(db_session, CFG)
    await db_session.commit()
    resp_low = _assertion_to_json(
        device.get({"publicKey": _to_soft_get_options(json.loads(opts_low))}, CFG.origin)
    )
    resp_high = _assertion_to_json(
        device.get({"publicKey": _to_soft_get_options(json.loads(opts_high))}, CFG.origin)
    )

    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with maker() as s_high, maker() as s_low:
        # The 7 verifies and leaves its UPDATE uncommitted: it holds the row lock.
        assert (
            await complete_authentication(s_high, CFG, challenge_id=cid_high, response=resp_high)
            == uid
        )
        # The 6 reads the old count (nobody has committed yet), verifies, and its
        # UPDATE waits for that lock.
        low = asyncio.create_task(
            complete_authentication(s_low, CFG, challenge_id=cid_low, response=resp_low)
        )
        await asyncio.sleep(0.3)
        assert not low.done()  # the mechanism: it is blocked on the lock, not finished
        await s_high.commit()  # the 7 is stored
        assert await low == uid  # the 6 login is still valid
        await s_low.commit()  # and its UPDATE lands afterwards

    sign_count, last_used_at = await _credential_row(db_session)
    assert sign_count == 7
    assert last_used_at is not None


@pytest.mark.asyncio
async def test_zero_sign_count_login_succeeds_and_always_writes_last_used_at(db_session):
    # A synced passkey leaves the counter at 0 on every login: the login must
    # keep succeeding and `last_used_at` must advance all the same, so the
    # UPDATE cannot be conditioned on the counter increasing.
    device = _ZeroCounterDevice()
    uid = await _register(db_session, device)

    cid, _, response = await _assertion_for(db_session, device, CFG.origin)
    assert (
        await complete_authentication(db_session, CFG, challenge_id=cid, response=response) == uid
    )
    await db_session.commit()
    first_count, first_used = await _credential_row(db_session)
    assert first_count == 0
    assert first_used is not None

    cid, _, response = await _assertion_for(db_session, device, CFG.origin)
    assert (
        await complete_authentication(db_session, CFG, challenge_id=cid, response=response) == uid
    )
    await db_session.commit()
    second_count, second_used = await _credential_row(db_session)
    assert second_count == 0
    assert second_used > first_used


# --- every rejected login leaves a line in the log ----------------------------


def _client_values(response: str, cid: str) -> list[str]:
    parsed = json.loads(response)
    return [cid, parsed["rawId"], parsed["id"]]


@pytest.mark.asyncio
async def test_login_disabled_user_logs_warning_with_db_ids(app_client, db_session, caplog):
    from orcaster_server.main import app

    device = SoftWebauthnDevice()
    uid = await _register(db_session, device)
    cred_row = await db_session.execute(
        text("SELECT id::text FROM passkey_credentials WHERE user_id = :u"), {"u": uid}
    )
    cred_id = cred_row.scalar_one()
    await db_session.execute(text("UPDATE users SET disabled_at = now() WHERE id = :u"), {"u": uid})
    await db_session.commit()
    cid, _, response = await _assertion_for(db_session, device, CFG.origin)

    with caplog.at_level(logging.INFO, logger="orcaster_server.auth.service.passkey"):
        res = await _post_complete(app, cid, response.encode())

    assert res.status_code == 401
    assert res.json() == {"ok": False}
    records = _passkey_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.WARNING
    msg = rec.getMessage()
    assert cred_id in msg
    assert uid in msg
    for value in _client_values(response, cid):
        assert value not in caplog.text


@pytest.mark.asyncio
async def test_login_unknown_credential_logs_info_without_client_values(db_session, caplog):
    device = SoftWebauthnDevice()
    await _register(db_session, device)
    await db_session.execute(text("DELETE FROM passkey_credentials"))
    await db_session.commit()
    cid, _, response = await _assertion_for(db_session, device, CFG.origin)

    with caplog.at_level(logging.INFO, logger="orcaster_server.auth.service.passkey"):
        result = await complete_authentication(db_session, CFG, challenge_id=cid, response=response)

    assert result is None
    records = _passkey_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "unknown credential" in records[0].getMessage()
    for value in _client_values(response, cid):
        assert value not in caplog.text


@pytest.mark.asyncio
async def test_login_expired_challenge_logs_info(app_client, db_session, caplog):
    from orcaster_server.main import app

    device = SoftWebauthnDevice()
    await _register(db_session, device)
    cid, _, response = await _assertion_for(db_session, device, CFG.origin)
    await db_session.execute(
        text("UPDATE webauthn_challenges SET expires_at = now() - interval '1 second'")
    )
    await db_session.commit()

    with caplog.at_level(logging.INFO, logger="orcaster_server.auth.service.passkey"):
        res = await _post_complete(app, cid, response.encode())

    assert res.status_code == 401
    assert res.json() == {"ok": False}
    records = _passkey_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "challenge" in records[0].getMessage()
    for value in _client_values(response, cid):
        assert value not in caplog.text


@pytest.mark.asyncio
async def test_login_complete_without_cookie_logs_info(app_client, caplog):
    from httpx import ASGITransport, AsyncClient

    from orcaster_server.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    with caplog.at_level(logging.INFO, logger="orcaster_server.auth.router"):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post("/auth/login/complete", content=b"{}")

    assert res.status_code == 400
    assert res.json() == {"ok": False}
    records = [r for r in caplog.records if r.name == "orcaster_server.auth.router"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
