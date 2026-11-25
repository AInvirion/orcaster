"""Share-link management from /admin (owner-only).

Create → the token is revealed after the PRG (flash); the lookup uses the hash.
Without a keyring (`TASK_SECRETS_KEYS_PATH`) the URL is shown ONCE; with a
keyring it is stored encrypted and the table shows it again with a copy button.
List → label/status/URL; revoke → revoked_at set, the status page turns 404 and
the URL is no longer shown. Members (non-owners) get 403 on every endpoint.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from orcaster_server import db as app_db
from orcaster_server.config import settings
from orcaster_server.status.tokens import hash_share_token
from orcaster_server.task_secrets import keyring as task_secrets_keyring
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID
from tests.conftest import browser_origin_hook

_NOT_RECOVERABLE = "URL not recoverable; mint a new link"
_NOT_DECRYPTABLE = "URL not decryptable with the configured keys; mint a new link"
_ONLY_ONCE_NOTICE = "is shown only once, right after creating it"
# The mint and revoke POSTs require the same origin (`require_session`).
_SAME_ORIGIN = {"Origin": settings.webauthn_origin}


def _use_keys(monkeypatch: pytest.MonkeyPatch, path: str | None) -> None:
    monkeypatch.setattr(settings, "task_secrets_keys_path", path)
    task_secrets_keyring.reset_keyring_cache()


def _write_keys(tmp_path: Path, *keys: bytes, name: str = "share-keys") -> str:
    p = tmp_path / name
    p.write_text("".join(k.decode() + "\n" for k in keys))
    return str(p)


@pytest.fixture
def no_keys(monkeypatch: pytest.MonkeyPatch):
    _use_keys(monkeypatch, None)
    yield
    task_secrets_keyring.reset_keyring_cache()


@pytest.fixture
def keys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = _write_keys(tmp_path, Fernet.generate_key())
    _use_keys(monkeypatch, path)
    yield path
    task_secrets_keyring.reset_keyring_cache()


async def _make_member_client() -> AsyncClient:
    """AsyncClient authenticated as a `member` of the Default workspace."""
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.main import app
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"),
            {"n": "MemberShareLinks"},
        )
        user_id = str(row.scalar_one())
        await add_member(s, workspace_id=DEFAULT_WORKSPACE_ID, user_id=user_id, role="member")
        token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
        await s.commit()

    client = AsyncClient(
        event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"HX-Request": "true"},
    )
    client.cookies.set("session", token)
    return client


def _extract_token(html: str) -> str:
    # The template reveals the secret in an element with id="share-token-secret".
    m = re.search(r'id="share-token-secret"[^>]*>([^<]+)<', html)
    assert m, "revealed secret not found in the HTML"
    return m.group(1).strip()


@pytest.mark.asyncio
async def test_mint_reveals_once_and_stores_only_hash(authed_admin_client, no_keys) -> None:
    client = authed_admin_client
    # The POST mints and does a PRG (303) to the page, which reveals the secret once.
    resp = await client.post(
        "/admin/share-links", data={"label": "Acme"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    assert resp.status_code == 200
    token = _extract_token(resp.text)
    assert len(token) >= 32
    # Only the hash lives in the DB.
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        row = (
            (await s.execute(text("SELECT token_hash, label FROM workspace_share_token")))
            .mappings()
            .one()
        )
    assert row["token_hash"] == hash_share_token(token)
    assert row["label"] == "Acme"
    # The listing page (GET) no longer reveals the secret.
    page = await client.get("/admin/share-links")
    assert token not in page.text
    assert "Acme" in page.text
    # Without a keyring the page warns that URLs are not recoverable.
    assert _ONLY_ONCE_NOTICE in page.text


@pytest.mark.asyncio
async def test_share_url_no_double_slash_when_base_url_has_trailing_slash(
    authed_admin_client, monkeypatch
) -> None:
    """PUBLIC_BASE_URL with a trailing slash must not produce `//status/` in the shown URL."""
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://status.example.com/")
    client = authed_admin_client
    resp = await client.post(
        "/admin/share-links", data={"label": "Slash"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    assert resp.status_code == 200
    token = _extract_token(resp.text)
    assert "//status/" not in resp.text
    assert f"https://status.example.com/status/{token}" in resp.text


@pytest.mark.asyncio
async def test_revoke_makes_status_page_404(authed_admin_client) -> None:
    client = authed_admin_client
    resp = await client.post(
        "/admin/share-links", data={"label": "temp"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    token = _extract_token(resp.text)
    # The public link works before revoking.
    assert (await client.get(f"/status/{token}")).status_code == 200
    # Get the id to revoke.
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        token_id = (
            await s.execute(text("SELECT id::text FROM workspace_share_token"))
        ).scalar_one()
    revoke = await client.post(f"/admin/share-links/{token_id}/revoke", headers=_SAME_ORIGIN)
    assert revoke.status_code in (200, 303)
    # After revoking, the status page gives 404.
    assert (await client.get(f"/status/{token}")).status_code == 404


# === Timezone note gated by `{% if tokens %}` ================
# share_links.html includes _tz_note.html only if there are tokens (it does not
# live behind polling, so it carries no tz_note_id). Without this test nobody
# would notice if the note appeared orphaned above the empty table, or stopped
# appearing once the first link is created.


@pytest.mark.asyncio
async def test_share_links_empty_omits_tz_note(authed_admin_client) -> None:
    resp = await authed_admin_client.get("/admin/share-links")
    assert resp.status_code == 200
    assert "js-tz-note" not in resp.text


@pytest.mark.asyncio
async def test_share_links_with_token_includes_tz_note(authed_admin_client) -> None:
    client = authed_admin_client
    resp = await client.post(
        "/admin/share-links", data={"label": "tz-note"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    assert resp.status_code == 200
    assert "js-tz-note" in resp.text


@pytest.mark.asyncio
async def test_share_links_requires_auth(app_client) -> None:
    # No session: the HTMX header is set by default on app_client → 401.
    resp = await app_client.get("/admin/share-links")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_member_share_links_403(app_client) -> None:
    """A member (non-owner) gets 403 on every share-links endpoint."""
    client = await _make_member_client()
    try:
        get_resp = await client.get(
            "/admin/share-links",
            headers={"HX-Request": "false", **_SAME_ORIGIN},
        )
        assert get_resp.status_code == 403, get_resp.text

        post_resp = await client.post(
            "/admin/share-links",
            data={"label": "forbidden"},
            headers={"HX-Request": "false", **_SAME_ORIGIN},
            follow_redirects=False,
        )
        assert post_resp.status_code == 403, post_resp.text

        revoke_resp = await client.post(
            f"/admin/share-links/{uuid.uuid4()}/revoke",
            headers={"HX-Request": "false", **_SAME_ORIGIN},
            follow_redirects=False,
        )
        assert revoke_resp.status_code == 403, revoke_resp.text
    finally:
        await client.aclose()


# === Audit of share tokens in admin_actions ============================
# The CHECK on admin_actions does not restrict target_kind to
# ('worker','task'): migration 0024 lifted that and 0048 added 'share_token'.


async def _audit_rows(action: str) -> list[dict]:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        result = await s.execute(
            text(
                "SELECT action, target_kind, target_id, actor, payload "
                "FROM admin_actions WHERE action = :a ORDER BY id"
            ),
            {"a": action},
        )
        return [dict(r) for r in result.mappings().all()]


async def _only_share_token_id() -> str:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        return (await s.execute(text("SELECT id::text FROM workspace_share_token"))).scalar_one()


@pytest.mark.asyncio
async def test_mint_share_link_audited(authed_admin_client) -> None:
    resp = await authed_admin_client.post(
        "/admin/share-links",
        data={"label": "audited-mint"},
        headers=_SAME_ORIGIN,
        follow_redirects=True,
    )
    assert resp.status_code == 200
    token_id = await _only_share_token_id()

    rows = await _audit_rows("mint_share_token")
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "share_token"
    assert rows[0]["target_id"] == token_id
    assert rows[0]["payload"] == {"label": "audited-mint"}
    # Same assertion as test_mint_enrollment_token_audited
    # (tests/test_pats_page.py:547), the test this one mirrors.
    assert rows[0]["actor"] is not None


@pytest.mark.asyncio
async def test_revoke_share_link_audited(authed_admin_client) -> None:
    await authed_admin_client.post(
        "/admin/share-links",
        data={"label": "audited-revoke"},
        headers=_SAME_ORIGIN,
        follow_redirects=True,
    )
    token_id = await _only_share_token_id()

    revoke = await authed_admin_client.post(
        f"/admin/share-links/{token_id}/revoke", headers=_SAME_ORIGIN
    )
    assert revoke.status_code in (200, 303)

    rows = await _audit_rows("revoke_share_token")
    assert len(rows) == 1
    assert rows[0]["target_kind"] == "share_token"
    assert rows[0]["target_id"] == token_id
    assert rows[0]["actor"] is not None


@pytest.mark.asyncio
async def test_revoke_missing_share_link_not_audited(authed_admin_client) -> None:
    """A revoke that answers 404 leaves no trace: the audit log records facts,
    not attempts."""
    resp = await authed_admin_client.post(
        f"/admin/share-links/{uuid.uuid4()}/revoke",
        headers=_SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 404

    assert await _audit_rows("revoke_share_token") == []


# === URL recoverable with the secrets keyring ============================


async def _share_row() -> dict:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        return dict(
            (
                await s.execute(
                    text(
                        "SELECT id::text AS id, token_hash, token_ciphertext "
                        "FROM workspace_share_token"
                    )
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.asyncio
async def test_mint_with_keys_lists_url_after_reload(authed_admin_client, keys_file) -> None:
    client = authed_admin_client
    resp = await client.post(
        "/admin/share-links", data={"label": "Acme"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    assert resp.status_code == 200
    token = _extract_token(resp.text)
    url = f"{settings.public_base_url.rstrip('/')}/status/{token}"

    # In the DB: hash for the lookup and the encrypted token, never in clear.
    row = await _share_row()
    assert row["token_hash"] == hash_share_token(token)
    assert row["token_ciphertext"] is not None
    assert token.encode() not in bytes(row["token_ciphertext"])

    # After reloading (no flash) the table shows the URL with its copy button.
    page = await client.get("/admin/share-links")
    assert page.status_code == 200
    assert "share-token-secret" not in page.text
    assert f'data-copy="{url}"' in page.text
    assert _ONLY_ONCE_NOTICE not in page.text
    # The public lookup still works.
    assert (await client.get(f"/status/{token}")).status_code == 200


@pytest.mark.asyncio
async def test_legacy_row_without_ciphertext_shows_fallback(authed_admin_client, keys_file) -> None:
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await s.execute(
            text(
                "INSERT INTO workspace_share_token (workspace_id, token_hash, label) "
                "VALUES (:ws, :h, 'legacy')"
            ),
            {"ws": DEFAULT_WORKSPACE_ID, "h": hash_share_token("legacy-token")},
        )
        await s.commit()
    page = await authed_admin_client.get("/admin/share-links")
    assert page.status_code == 200
    assert _NOT_RECOVERABLE in page.text
    assert "/status/legacy-token" not in page.text


@pytest.mark.asyncio
async def test_revoked_row_never_shows_url(authed_admin_client, keys_file) -> None:
    client = authed_admin_client
    resp = await client.post(
        "/admin/share-links", data={"label": "gone"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    token = _extract_token(resp.text)
    token_id = await _only_share_token_id()
    assert (
        await client.post(f"/admin/share-links/{token_id}/revoke", headers=_SAME_ORIGIN)
    ).status_code in (200, 303)

    page = await client.get("/admin/share-links")
    assert page.status_code == 200
    assert token not in page.text
    assert "revoked" in page.text


@pytest.mark.asyncio
async def test_ciphertext_with_keys_missing_is_explicit(
    authed_admin_client, keys_file, monkeypatch
) -> None:
    client = authed_admin_client
    resp = await client.post(
        "/admin/share-links", data={"label": "k"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    token = _extract_token(resp.text)
    _use_keys(monkeypatch, None)

    page = await client.get("/admin/share-links")
    assert page.status_code == 200
    assert token not in page.text
    assert "encryption keys are not configured on this server" in page.text
    assert _NOT_RECOVERABLE not in page.text


@pytest.mark.asyncio
async def test_ciphertext_with_wrong_key_is_explicit(
    authed_admin_client, keys_file, tmp_path, monkeypatch, caplog
) -> None:
    client = authed_admin_client
    resp = await client.post(
        "/admin/share-links", data={"label": "w"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    token = _extract_token(resp.text)
    token_id = await _only_share_token_id()
    _use_keys(monkeypatch, _write_keys(tmp_path, Fernet.generate_key(), name="other-keys"))

    with caplog.at_level("WARNING", logger="orcaster_server.status.tokens"):
        page = await client.get("/admin/share-links")
    assert page.status_code == 200
    assert token not in page.text
    assert _NOT_DECRYPTABLE in page.text
    # The warning names the row, never the token.
    assert token_id in caplog.text
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_broken_keys_file_fails_explicitly(
    authed_admin_client, tmp_path, monkeypatch
) -> None:
    bad = tmp_path / "bad-keys"
    bad.write_text("not-a-fernet-key\n")
    _use_keys(monkeypatch, str(bad))
    try:
        with pytest.raises(task_secrets_keyring.KeyringError):
            await authed_admin_client.get("/admin/share-links")
    finally:
        task_secrets_keyring.reset_keyring_cache()


@pytest.mark.asyncio
async def test_rotated_keyring_still_decrypts(authed_admin_client, tmp_path, monkeypatch) -> None:
    """New key first and the old one behind it (rotation procedure): it keeps working."""
    old = Fernet.generate_key()
    _use_keys(monkeypatch, _write_keys(tmp_path, old, name="k1"))
    try:
        resp = await authed_admin_client.post(
            "/admin/share-links", data={"label": "rot"}, headers=_SAME_ORIGIN, follow_redirects=True
        )
        token = _extract_token(resp.text)
        _use_keys(monkeypatch, _write_keys(tmp_path, Fernet.generate_key(), old, name="k2"))
        page = await authed_admin_client.get("/admin/share-links")
        assert f"/status/{token}" in page.text
    finally:
        task_secrets_keyring.reset_keyring_cache()


@pytest.mark.asyncio
async def test_plaintext_token_not_in_audit(authed_admin_client, keys_file) -> None:
    resp = await authed_admin_client.post(
        "/admin/share-links", data={"label": "audit"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    token = _extract_token(resp.text)
    rows = await _audit_rows("mint_share_token")
    assert len(rows) == 1
    assert token not in repr(rows[0])


# === Revoke deletes the ciphertext, domain separation, origin ============


@pytest.mark.asyncio
async def test_revoke_nulls_ciphertext(authed_admin_client, keys_file) -> None:
    client = authed_admin_client
    await client.post(
        "/admin/share-links", data={"label": "n"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    assert (await _share_row())["token_ciphertext"] is not None
    token_id = await _only_share_token_id()
    resp = await client.post(f"/admin/share-links/{token_id}/revoke", headers=_SAME_ORIGIN)
    assert resp.status_code in (200, 303)
    assert (await _share_row())["token_ciphertext"] is None


@pytest.mark.asyncio
async def test_share_ciphertext_uses_derived_keys(authed_admin_client, keys_file) -> None:
    """The share-link ciphertext cannot be opened by the secrets keyring (raw keys)."""
    from cryptography.fernet import InvalidToken

    resp = await authed_admin_client.post(
        "/admin/share-links", data={"label": "d"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    token = _extract_token(resp.text)
    ct = bytes((await _share_row())["token_ciphertext"])
    ring = task_secrets_keyring.load_keyring(keys_file)
    assert ring.share_links.decrypt(ct).decode() == token
    with pytest.raises(InvalidToken):
        ring.decrypt(ct)


@pytest.mark.asyncio
async def test_task_secret_ciphertext_in_share_row_not_shown(
    authed_admin_client, keys_file, caplog
) -> None:
    """A task-secret ciphertext copied into the row is not shown as a URL.

    The derived keys cannot open it: it fails at decryption, not at the hash
    check. With `share_links` on the raw keys the ciphertext would open and
    only the hash would stop it; the direct assertions and the warning detect
    it.
    """
    from cryptography.fernet import InvalidToken

    ring = task_secrets_keyring.load_keyring(keys_file)
    secret_ct = ring.encrypt(b"secret-value-xyz")
    with pytest.raises(InvalidToken):
        ring.share_links.decrypt(secret_ct)
    await authed_admin_client.post(
        "/admin/share-links", data={"label": "x"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    with pytest.raises(InvalidToken):
        ring.decrypt(bytes((await _share_row())["token_ciphertext"]))
    token_id = await _only_share_token_id()
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await s.execute(
            text(
                "UPDATE workspace_share_token SET token_ciphertext = :ct "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"ct": secret_ct, "id": token_id},
        )
        await s.commit()
    with caplog.at_level(logging.WARNING, logger="orcaster_server.status.tokens"):
        page = await authed_admin_client.get("/admin/share-links")
    assert page.status_code == 200
    assert "secret-value-xyz" not in page.text
    assert _NOT_DECRYPTABLE in page.text
    warnings = [r.getMessage() for r in caplog.records if token_id in r.getMessage()]
    assert warnings == [
        f"share token {token_id}: ciphertext not decryptable with any key in TASK_SECRETS_KEYS_PATH"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"X-Test-No-Origin": "1"},
    ],
    ids=["foreign-origin", "cross-site", "no-headers"],
)
async def test_mint_and_revoke_refuse_cross_origin(authed_admin_client, keys_file, headers) -> None:
    client = authed_admin_client
    # Without HTMX, like the page form (same as test_secrets_page).
    headers = {"HX-Request": "false", **headers}
    mint = await client.post(
        "/admin/share-links", data={"label": "csrf"}, headers=headers, follow_redirects=False
    )
    assert mint.status_code == 403
    sm = app_db.get_sessionmaker()
    async with sm() as s:
        count = (await s.execute(text("SELECT count(*) FROM workspace_share_token"))).scalar_one()
    assert count == 0

    await client.post(
        "/admin/share-links", data={"label": "ok"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    token_id = await _only_share_token_id()
    revoke = await client.post(
        f"/admin/share-links/{token_id}/revoke", headers=headers, follow_redirects=False
    )
    assert revoke.status_code == 403
    async with sm() as s:
        revoked_at = (
            await s.execute(
                text("SELECT revoked_at FROM workspace_share_token WHERE id = CAST(:id AS uuid)"),
                {"id": token_id},
            )
        ).scalar_one()
    assert revoked_at is None
    assert await _audit_rows("revoke_share_token") == []


# === Share links re-read the keys file if it changed on disk ===========


@pytest.mark.asyncio
async def test_mint_encrypts_with_the_keys_file_as_it_is_on_disk(
    authed_admin_client, keys_file, tmp_path
) -> None:
    """The process loaded `[old]` and the file becomes `[new, old]` at the same
    path, without a restart and without any poll having rejected a delivery: the
    mint encrypts the URL with `new`, so retiring `old` does not make it
    unreadable."""
    path = Path(keys_file)
    old = path.read_text().strip()
    # The process already has the keys loaded, like a running uvicorn.
    assert task_secrets_keyring.get_keyring() is not None
    new = Fernet.generate_key()
    path.write_text(new.decode() + "\n" + old + "\n")

    resp = await authed_admin_client.post(
        "/admin/share-links", data={"label": "fresh"}, headers=_SAME_ORIGIN, follow_redirects=True
    )
    assert resp.status_code == 200
    token = _extract_token(resp.text)
    ciphertext = bytes((await _share_row())["token_ciphertext"])

    only_new = task_secrets_keyring.load_keyring(_write_keys(tmp_path, new, name="only-new"))
    assert only_new.share_links.decrypt(ciphertext).decode() == token
    only_old = task_secrets_keyring.load_keyring(
        _write_keys(tmp_path, old.encode(), name="only-old")
    )
    with pytest.raises(InvalidToken):
        only_old.share_links.decrypt(ciphertext)


@pytest.mark.asyncio
async def test_page_reads_a_url_encrypted_with_a_key_added_after_startup(
    authed_admin_client, keys_file, tmp_path
) -> None:
    """The row was encrypted by `rotate` with a key this process had not loaded.
    The page re-reads the file and shows the URL, instead of warning that it
    cannot be decrypted."""
    from orcaster_server.status.tokens import generate_share_token

    path = Path(keys_file)
    old = path.read_text().strip()
    assert task_secrets_keyring.get_keyring() is not None
    new = Fernet.generate_key()
    rotated = task_secrets_keyring.load_keyring(_write_keys(tmp_path, new, name="rotated"))
    token = generate_share_token()

    sm = app_db.get_sessionmaker()
    async with sm() as s:
        await s.execute(
            text(
                "INSERT INTO workspace_share_token "
                "(workspace_id, token_hash, token_ciphertext, label) "
                "VALUES (:ws, :h, :ct, 'rotated')"
            ),
            {
                "ws": DEFAULT_WORKSPACE_ID,
                "h": hash_share_token(token),
                "ct": rotated.share_links.encrypt(token.encode("utf-8")),
            },
        )
        await s.commit()

    path.write_text(new.decode() + "\n" + old + "\n")
    page = await authed_admin_client.get("/admin/share-links")
    assert page.status_code == 200
    url = f"{settings.public_base_url.rstrip('/')}/status/{token}"
    assert f'data-copy="{url}"' in page.text
    assert _NOT_DECRYPTABLE not in page.text
