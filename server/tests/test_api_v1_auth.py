"""Contract tests: public `/api/v1` API, Bearer auth + `GET /auth/whoami`.

`require_pat_workspace` authenticates via
`Authorization: Bearer orcaster_pat_…`, resolves the
PAT to a `WorkspaceContext` (dynamic role) and inherits multi-tenant isolation by
workspace. Unlike `/admin`, `/api/v1` **always responds with JSON** (never a 303
redirect): the 401 is generic for every way a token can be invalid; the 403
distinguishes "valid token but you are no longer a member".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import mint_pat_via_app


def _db_url() -> str:
    from orcaster_server.config import settings

    return settings.database_url


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _mint_pat(
    *,
    role: str = "owner",
    add_to_workspace: bool = True,
    display_name: str = "Pat Owner",
    email: str | None = "pat-owner@example.com",
    label: str | None = "cli-laptop",
    expires_at: datetime | None = None,
) -> tuple[str, str]:
    return await mint_pat_via_app(
        role=role,
        add_to_workspace=add_to_workspace,
        display_name=display_name,
        email=email,
        label=label,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# whoami: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_happy_path(app_client: AsyncClient) -> None:
    """Valid owner PAT -> 200 with identity, workspace, role and token metadata."""
    token, user_id = await _mint_pat(role="owner")
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["id"] == user_id
    assert body["user"]["display_name"] == "Pat Owner"
    assert body["user"]["email"] == "pat-owner@example.com"
    assert body["workspace"]["id"] == DEFAULT_WORKSPACE_ID
    assert body["workspace"]["slug"] == "default"
    assert body["workspace"]["name"] == "Default"
    assert body["role"] == "owner"
    assert body["token"]["label"] == "cli-laptop"
    # last_used_at: a freshly minted PAT was never used before this request.
    assert body["token"]["last_used_at"] is None


@pytest.mark.asyncio
async def test_whoami_reflects_dynamic_role(app_client: AsyncClient) -> None:
    """The role is not frozen in the token: it is resolved against workspace_members."""
    token, _uid = await _mint_pat(role="member")
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "member"


@pytest.mark.asyncio
async def test_whoami_updates_last_used_at(app_client: AsyncClient) -> None:
    """resolve_pat sets last_used_at (best-effort): the 2nd call already sees it populated."""
    token, _uid = await _mint_pat()
    first = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert first.status_code == 200, first.text
    assert first.json()["token"]["last_used_at"] is None

    second = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert second.status_code == 200, second.text
    assert second.json()["token"]["last_used_at"] is not None


@pytest.mark.asyncio
async def test_whoami_exposes_expires_at(app_client: AsyncClient) -> None:
    """PAT with a future expiry -> whoami exposes that `expires_at` in token."""
    expires_at = datetime.now(UTC) + timedelta(days=30)
    token, _uid = await _mint_pat(expires_at=expires_at)
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    got = resp.json()["token"]["expires_at"]
    assert got is not None
    # Compare the instant (both in UTC), independent of the serialization format.
    assert datetime.fromisoformat(got) == expires_at


@pytest.mark.asyncio
async def test_whoami_expires_at_null_when_no_expiry(app_client: AsyncClient) -> None:
    """PAT without expiry -> `token.expires_at` is null (does not expire)."""
    token, _uid = await _mint_pat(expires_at=None)
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"]["expires_at"] is None


# ---------------------------------------------------------------------------
# 401: missing / invalid / revoked / expired token (generic message)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_no_token_401_json(app_client: AsyncClient) -> None:
    """No Authorization header -> JSON 401 (never a 303 redirect, that is /admin)."""
    resp = await app_client.get("/api/v1/auth/whoami")
    assert resp.status_code == 401, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_whoami_malformed_header_401(app_client: AsyncClient) -> None:
    """Header without the Bearer scheme -> generic 401."""
    resp = await app_client.get(
        "/api/v1/auth/whoami", headers={"Authorization": "orcaster_pat_abc"}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_whoami_unknown_token_401(app_client: AsyncClient) -> None:
    """Token with the right prefix but unknown -> generic 401."""
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer("orcaster_pat_deadbeef"))
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


async def _rekey_pat(user_id: str, new_token: str) -> None:
    """Replace the user's stored PAT hash, as the service would store it."""
    from orcaster_server.auth.config import hash_token

    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE personal_access_tokens SET token_hash = :h WHERE user_id = :u"),
                {"h": hash_token(new_token), "u": user_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_pat_has_orcaster_prefix_and_authenticates(app_client: AsyncClient) -> None:
    token, _uid = await _mint_pat()
    assert token.startswith("orcaster_pat_")
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["other_pat_", "pat_", "ORCASTER_PAT_", ""])
async def test_unknown_prefix_pat_rejected_even_if_hash_matches(
    app_client: AsyncClient, prefix: str
) -> None:
    """Only `orcaster_pat_` tokens authenticate: any other prefix (or none) gets
    401 even when the stored hash matches the presented token."""
    token, uid = await _mint_pat()
    other = prefix + token.removeprefix("orcaster_pat_")
    await _rekey_pat(uid, other)
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(other))
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


async def _set_disabled(user_id: str, disabled: bool) -> None:
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET disabled_at = :d WHERE id = :u"),
                {"d": datetime.now(UTC) if disabled else None, "u": user_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_user_pat_401_and_reenable_restores(
    app_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A disabled user's PAT is 401 (like any rejected PAT),
    with a WARNING that omits the token; after re-enabling the user it works again."""
    from orcaster_server import db as app_db

    token, uid = await _mint_pat()
    assert (await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))).status_code == 200
    async with app_db.get_sessionmaker()() as s:
        pat_id = str(
            (
                await s.execute(
                    text("SELECT id FROM personal_access_tokens WHERE user_id = :u"), {"u": uid}
                )
            ).scalar_one()
        )

    await _set_disabled(uid, True)
    with caplog.at_level("WARNING", logger="orcaster_server.pats.service"):
        for path in ("/api/v1/auth/whoami", "/api/v1/tasks"):
            resp = await app_client.get(path, headers=_bearer(token))
            assert resp.status_code == 401, resp.text
            assert resp.json()["error"] == "unauthorized"
            # Same body as an unknown token: it does not leak that the owner is
            # disabled (neither in `message` nor in any other key).
            unknown = await app_client.get(path, headers=_bearer("orcaster_pat_deadbeef"))
            assert unknown.status_code == 401, unknown.text
            assert resp.json() == unknown.json()
            assert "disabled" not in resp.text.lower()
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and r.name == "orcaster_server.pats.service"
    ]
    # Two rejected requests with the same PAT within the window, a single line.
    assert len(warnings) == 1
    assert all(token not in r.getMessage() for r in warnings)
    assert uid in warnings[0].getMessage()
    assert pat_id in warnings[0].getMessage()

    await _set_disabled(uid, False)
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_whoami_revoked_token_401(app_client: AsyncClient) -> None:
    """Revoked PAT -> generic 401 (does not leak that it was revoked)."""
    token, _uid = await _mint_pat()
    engine = create_async_engine(_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE personal_access_tokens SET revoked_at = now() WHERE user_id = :u"),
                {"u": _uid},
            )
    finally:
        await engine.dispose()
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_whoami_expired_token_401(app_client: AsyncClient) -> None:
    """Expired PAT -> generic 401."""
    token, _uid = await _mint_pat(expires_at=datetime.now(UTC) - timedelta(hours=1))
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# 403: valid token but the user is no longer a member of the workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_not_member_403(app_client: AsyncClient) -> None:
    """Valid PAT whose owner is no longer a member of the workspace -> 403."""
    token, _uid = await _mint_pat(add_to_workspace=False)
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == "forbidden"


# ---------------------------------------------------------------------------
# Case-insensitive `Bearer` scheme (RFC 7235 §2.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [
        "bearer {token}",
        "BEARER {token}",
        "bEaReR {token}",
        "Bearer {token}",
        "Bearer  {token}",
        "bearer   {token}",
    ],
    ids=["lower", "upper", "mixed", "canonical", "two-spaces", "lower-three-spaces"],
)
async def test_whoami_accepts_the_bearer_scheme_in_any_case(
    app_client: AsyncClient, authorization: str
) -> None:
    """RFC 7235 §2.1: the scheme name is a case-insensitive token, and between
    the scheme and the credentials goes `1*SP` (one or more spaces)."""
    token, user_id = await _mint_pat()
    resp = await app_client.get(
        "/api/v1/auth/whoami", headers={"Authorization": authorization.format(token=token)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["id"] == user_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [
        "Basic dXNlcjpwYXNz",
        "",
        "Bearer",
        "bearer",
        "Bearer ",
        "Bearerx {token}",
        "xBearer {token}",
        "Bearer{token}",
        "{token}",
    ],
    ids=[
        "basic",
        "empty",
        "scheme-only",
        "scheme-only-lower",
        "scheme-and-space-only",
        "bearerx",
        "xbearer",
        "no-separator",
        "no-scheme",
    ],
)
async def test_whoami_rejects_anything_that_is_not_the_bearer_scheme_401(
    app_client: AsyncClient, authorization: str
) -> None:
    """Only the `bearer` scheme (in any letter case) followed by credentials authenticates.

    A case-insensitive scheme relaxes nothing else: another scheme, an empty value,
    the scheme without credentials or a scheme that merely *starts with*
    `bearer` are still a generic 401.
    """
    token, _uid = await _mint_pat()
    resp = await app_client.get(
        "/api/v1/auth/whoami", headers={"Authorization": authorization.format(token=token)}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_whoami_token_itself_stays_case_sensitive_401(app_client: AsyncClient) -> None:
    """Only the *scheme* letter case changes: the token remains case-sensitive."""
    token, _uid = await _mint_pat()
    assert token != token.upper()
    resp = await app_client.get(
        "/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token.upper()}"}
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# 401: unsupported legacy token prefix (explicit message, never accepted)
# ---------------------------------------------------------------------------

_LEGACY_PREFIX = "taskor_pat_"


@pytest.mark.asyncio
async def test_legacy_prefix_pat_401_names_the_prefix_and_asks_for_a_new_token(
    app_client: AsyncClient,
) -> None:
    """A token with the unsupported legacy prefix gets a 401 that says the prefix
    is no longer accepted and that a new token must be minted."""
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(_LEGACY_PREFIX + "deadbeef"))
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["error"] == "unauthorized"
    assert _LEGACY_PREFIX in body["message"]
    assert "no longer accepted" in body["message"]
    assert "new token" in body["message"]
    assert "deadbeef" not in resp.text


@pytest.mark.asyncio
async def test_legacy_prefix_pat_rejected_even_if_hash_matches(
    app_client: AsyncClient,
) -> None:
    """The legacy prefix is never accepted, even when the stored hash matches."""
    token, uid = await _mint_pat()
    legacy = _LEGACY_PREFIX + token.removeprefix("orcaster_pat_")
    await _rekey_pat(uid, legacy)
    resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(legacy))
    assert resp.status_code == 401, resp.text
    assert "no longer accepted" in resp.json()["message"]


@pytest.mark.asyncio
async def test_legacy_prefix_pat_logs_one_bounded_warning_without_the_token(
    app_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The rejection leaves a WARNING naming the prefix (never the token), at most
    one line per window."""
    token = _LEGACY_PREFIX + "s3cr3tvalue"
    with caplog.at_level("WARNING", logger="orcaster_server.pats.service"):
        for _ in range(2):
            resp = await app_client.get("/api/v1/auth/whoami", headers=_bearer(token))
            assert resp.status_code == 401, resp.text
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and r.name == "orcaster_server.pats.service"
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert _LEGACY_PREFIX in message
    assert "s3cr3tvalue" not in message
