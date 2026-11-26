"""The dashboard `/admin/secrets` page.

Covers:
  - Owner: table + "Set secret" form + Delete with `data-confirm`.
  - Member: read-only table, no forms; their POSTs answer 403.
  - Without `TASK_SECRETS_KEYS_PATH`: notice, no forms; POSTs answer 503.
  - Set / replace / delete flows with Post/Redirect/Get and a names-only flash.
  - The value never appears in a body, in `Set-Cookie` or in `Location`.
  - Audit (`set_task_secret`, `delete_task_secret`) with the actor.
  - Inline validation errors with 400 and an empty value field.
  - CSRF: POSTs from another origin (`Origin` or `Sec-Fetch-Site`) answer 403
    without changing anything, with the same rule as the `/auth` join.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.config import settings
from orcaster_server.task_secrets.keyring import TaskSecretsKeyring
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

VALUE = "hf_page_secret_value_QWERTY"
OTHER_VALUE = "hf_page_other_value_ZXCVBN"
NO_HTMX = {"HX-Request": "false"}


@pytest.fixture
def keys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from orcaster_server.task_secrets import keyring

    p = tmp_path / "task-secrets-keys"
    p.write_text(Fernet.generate_key().decode() + "\n")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))
    keyring.reset_keyring_cache()
    yield p
    keyring.reset_keyring_cache()


@pytest.fixture
def no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from orcaster_server.task_secrets import keyring

    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()


async def _member_client() -> tuple[AsyncClient, str]:
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
            text("INSERT INTO users (display_name) VALUES ('MemberSecrets') RETURNING id")
        )
        user_id = str(row.scalar_one())
        await add_member(s, workspace_id=DEFAULT_WORKSPACE_ID, user_id=user_id, role="member")
        token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
        await s.commit()
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.cookies.set("session", token)
    return client, user_id


async def _fetch(sql: str, **params) -> list[dict]:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
            return [dict(r) for r in rows]
    finally:
        await engine.dispose()


async def _secret_rows() -> list[dict]:
    return await _fetch("SELECT name, fingerprint FROM task_secrets ORDER BY name")


async def _audit(action: str) -> list[dict]:
    return await _fetch(
        "SELECT action, target_kind, actor::text AS actor, payload "
        "FROM admin_actions WHERE action = :a AND workspace_id = :w ORDER BY ts, id",
        a=action,
        w=DEFAULT_WORKSPACE_ID,
    )


async def _owner_id() -> str:
    rows = await _fetch(
        "SELECT user_id::text AS id FROM workspace_members "
        "WHERE workspace_id = :w AND role = 'owner'",
        w=DEFAULT_WORKSPACE_ID,
    )
    assert len(rows) == 1
    return rows[0]["id"]


def _assert_no_value(resp: Response, *values: str) -> None:
    """No value in the body or in the `Set-Cookie` / `Location` headers."""
    headers = " ".join(resp.headers.get_list("set-cookie") + resp.headers.get_list("location"))
    for v in values:
        assert v not in resp.text
        assert v not in headers
    for r in resp.history:
        _assert_no_value(r, *values)


_SAME_ORIGIN = object()


def _post_headers(origin: object = _SAME_ORIGIN, sec_fetch_site: str | None = None) -> dict:
    """Headers of a form POST. By default it sends the site's own `Origin`, like
    a browser; `origin=None` omits it."""
    headers = dict(NO_HTMX)
    if origin is _SAME_ORIGIN:
        headers["Origin"] = settings.webauthn_origin
    elif origin is not None:
        headers["Origin"] = origin
    elif sec_fetch_site is None:
        # No origin header at all: the test client must not add `Origin`.
        headers["X-Test-No-Origin"] = "1"
    if sec_fetch_site is not None:
        headers["Sec-Fetch-Site"] = sec_fetch_site
    return headers


async def _set(
    client: AsyncClient,
    name: str,
    value: str,
    *,
    origin: object = _SAME_ORIGIN,
    sec_fetch_site: str | None = None,
    **kw,
) -> Response:
    return await client.post(
        "/admin/secrets",
        data={"name": name, "value": value},
        headers=_post_headers(origin, sec_fetch_site),
        **kw,
    )


async def _delete(
    client: AsyncClient,
    name: str,
    *,
    origin: object = _SAME_ORIGIN,
    sec_fetch_site: str | None = None,
) -> Response:
    return await client.post(
        f"/admin/secrets/{name}/delete",
        headers=_post_headers(origin, sec_fetch_site),
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Page and roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_sees_empty_state_form_and_nav(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    resp = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert resp.status_code == 200, resp.text
    assert '<a href="/admin/secrets">Secrets</a>' in resp.text
    assert "No task secrets yet." in resp.text
    assert 'action="/admin/secrets"' in resp.text
    assert 'id="secret-value"' in resp.text
    # An <input type=password> strips line breaks from the value.
    assert re.search(r'<textarea id="secret-value"[^>]*name="value"', resp.text)
    assert 'type="password"' not in resp.text
    assert 'autocomplete="off"' in resp.text
    assert 'spellcheck="false"' in resp.text
    assert "Set secret" in resp.text
    assert "Task secrets are disabled" not in resp.text


@pytest.mark.asyncio
async def test_page_requires_auth(app_client: AsyncClient) -> None:
    resp = await app_client.get(
        "/admin/secrets",
        headers={"HX-Request": "false", "Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?next=%2Fadmin%2Fsecrets"


@pytest.mark.asyncio
async def test_member_read_only_and_post_forbidden(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    assert (await _set(authed_admin_client, "API_TOKEN", VALUE)).status_code == 303
    member, _ = await _member_client()
    async with member:
        page = await member.get("/admin/secrets", headers=NO_HTMX)
        assert page.status_code == 200, page.text
        assert "API_TOKEN" in page.text
        assert 'data-confirm-url="/admin/secrets' not in page.text
        assert 'action="/admin/secrets' not in page.text
        assert 'id="secret-value"' not in page.text
        _assert_no_value(page, VALUE)

        setr = await _set(member, "OTHER", OTHER_VALUE, follow_redirects=False)
        assert setr.status_code == 403
        _assert_no_value(setr, OTHER_VALUE)
        delr = await _delete(member, "API_TOKEN")
        assert delr.status_code == 403
    assert [r["name"] for r in await _secret_rows()] == ["API_TOKEN"]


@pytest.mark.asyncio
async def test_unconfigured_shows_notice_and_rejects_posts(
    authed_admin_client: AsyncClient, no_keys: None
) -> None:
    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert (
        "Task secrets are disabled on this server: "
        "<code>TASK_SECRETS_KEYS_PATH</code> is not configured."
    ) in page.text
    assert 'action="/admin/secrets' not in page.text
    assert 'id="secret-value"' not in page.text

    setr = await _set(authed_admin_client, "API_TOKEN", VALUE, follow_redirects=False)
    assert setr.status_code == 503
    _assert_no_value(setr, VALUE)
    assert await _secret_rows() == []


@pytest.mark.asyncio
async def test_unconfigured_still_lists_existing_secrets(
    authed_admin_client: AsyncClient, keys_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing does not decrypt: with no keys file the table shows stored names, no actions."""
    from orcaster_server.task_secrets import keyring

    setr = await _set(authed_admin_client, "API_TOKEN", VALUE, follow_redirects=False)
    assert setr.status_code == 303
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()

    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert "<code>TASK_SECRETS_KEYS_PATH</code> is not configured." in page.text
    assert "API_TOKEN" in page.text
    assert 'action="/admin/secrets"' not in page.text
    _assert_no_value(page, VALUE)


async def _seed_task(*, status: str, task_env: list[str]) -> None:
    """Task inserted directly in the Default workspace with the given `payload.task_env`."""
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO tasks (workspace_id, command, payload, target_mode,
                                       target_count, priority, status, run_at,
                                       signed, signature)
                    VALUES (:ws, 'secret-command', CAST(:p AS jsonb), 'count', 1, 0,
                            :status, now(), true, decode(repeat('00', 64), 'hex'))
                    """
                ),
                {
                    "ws": DEFAULT_WORKSPACE_ID,
                    "p": json.dumps({"task_env": task_env}),
                    "status": status,
                },
            )
    finally:
        await engine.dispose()


# Without a keys file, delete remains available to the owner.
@pytest.mark.asyncio
async def test_unconfigured_owner_sees_delete_but_not_set(
    authed_admin_client: AsyncClient, keys_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server.task_secrets import keyring

    assert (await _set(authed_admin_client, "API_TOKEN", VALUE)).status_code == 303
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()

    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert 'data-confirm-url="/admin/secrets/API_TOKEN/delete"' in page.text
    assert 'action="/admin/secrets"' not in page.text
    assert 'id="secret-value"' not in page.text
    _assert_no_value(page, VALUE)

    resp = await _delete(authed_admin_client, "API_TOKEN")
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/secrets"
    assert await _secret_rows() == []
    assert len(await _audit("delete_task_secret")) == 1


@pytest.mark.asyncio
async def test_unconfigured_member_sees_no_delete_button(
    authed_admin_client: AsyncClient, keys_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server.task_secrets import keyring

    assert (await _set(authed_admin_client, "API_TOKEN", VALUE)).status_code == 303
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    member, _ = await _member_client()
    async with member:
        page = await member.get("/admin/secrets", headers=NO_HTMX)
        assert page.status_code == 200, page.text
        assert "API_TOKEN" in page.text
        assert 'data-confirm-url="/admin/secrets' not in page.text
        assert 'action="/admin/secrets' not in page.text


@pytest.mark.asyncio
async def test_unconfigured_banner_counts_queued_tasks_with_stored_secrets(
    authed_admin_client: AsyncClient, keys_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banner counts only `queued` tasks that declare a stored name."""
    from orcaster_server.task_secrets import keyring

    assert (await _set(authed_admin_client, "API_TOKEN", VALUE)).status_code == 303
    await _seed_task(status="queued", task_env=["API_TOKEN"])
    await _seed_task(status="queued", task_env=["API_TOKEN", "OTHER_VAR"])
    await _seed_task(status="queued", task_env=["OTHER_VAR"])
    await _seed_task(status="dispatching", task_env=["API_TOKEN"])
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()

    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert "2 queued tasks declare a stored secret name." in page.text
    assert "stay queued" in page.text
    assert "fall back to the worker" in page.text
    _assert_no_value(page, VALUE)


@pytest.mark.asyncio
async def test_unconfigured_banner_without_queued_tasks_has_no_count(
    authed_admin_client: AsyncClient, keys_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server.task_secrets import keyring

    assert (await _set(authed_admin_client, "API_TOKEN", VALUE)).status_code == 303
    await _seed_task(status="queued", task_env=["OTHER_VAR"])
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()

    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert "queued task" not in page.text
    assert "stay queued" in page.text


@pytest.mark.asyncio
async def test_unconfigured_banner_without_secrets_has_no_queued_note(
    authed_admin_client: AsyncClient, no_keys: None
) -> None:
    await _seed_task(status="queued", task_env=["API_TOKEN"])
    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert "stay queued" not in page.text
    assert "queued task" not in page.text


# ---------------------------------------------------------------------------
# Notice about secrets the poll could not decrypt
# ---------------------------------------------------------------------------

_UNDECRYPTABLE_BANNER = "could not decrypt stored secret"


@pytest.fixture
def clean_refusals():
    """Rejections live in process memory: cleared before and after."""
    from orcaster_server.task_secrets import blocked

    blocked._REFUSALS.clear()
    yield blocked
    blocked._REFUSALS.clear()


def _refuse(
    blocked,
    *,
    kind: str,
    names: list[str],
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    task_id: int = 1,
) -> None:
    blocked.record_refusal(
        workspace_id=workspace_id, worker_id="w1", task_id=task_id, kind=kind, names=names
    )


@pytest.mark.asyncio
async def test_undecryptable_refusal_shows_banner_with_sorted_names(
    authed_admin_client: AsyncClient, keys_file: Path, clean_refusals
) -> None:
    _refuse(clean_refusals, kind="undecryptable", names=["ZED_TOKEN", "API_TOKEN"], task_id=1)
    _refuse(clean_refusals, kind="undecryptable", names=["API_TOKEN", "API_KEY"], task_id=2)

    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert (
        "The server could not decrypt stored secret(s) "
        "<code>API_KEY</code>, <code>API_TOKEN</code>, <code>ZED_TOKEN</code> "
        "in the last 15 minutes: no key in <code>TASK_SECRETS_KEYS_PATH</code> "
        "decrypts them."
    ) in " ".join(page.text.split())
    assert "Tasks that declare them stay queued and are not delivered." in " ".join(
        page.text.split()
    )
    assert 'class="ws-error" role="status"' in page.text


@pytest.mark.asyncio
async def test_undecryptable_banner_is_visible_to_members_and_escapes_names(
    authed_admin_client: AsyncClient, keys_file: Path, clean_refusals
) -> None:
    _refuse(clean_refusals, kind="undecryptable", names=["<b>X</b>"])
    member, _ = await _member_client()
    async with member:
        page = await member.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert _UNDECRYPTABLE_BANNER in page.text
    assert "<code>&lt;b&gt;X&lt;/b&gt;</code>" in page.text
    assert "<b>X</b>" not in page.text


@pytest.mark.asyncio
async def test_no_refusals_shows_no_undecryptable_banner(
    authed_admin_client: AsyncClient, keys_file: Path, clean_refusals
) -> None:
    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert _UNDECRYPTABLE_BANNER not in page.text


@pytest.mark.asyncio
async def test_refusal_of_another_workspace_shows_no_undecryptable_banner(
    authed_admin_client: AsyncClient, keys_file: Path, clean_refusals
) -> None:
    _refuse(
        clean_refusals,
        kind="undecryptable",
        names=["FOREIGN_SECRET"],
        workspace_id="00000000-0000-0000-0000-0000000000ff",
    )
    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert _UNDECRYPTABLE_BANNER not in page.text
    assert "FOREIGN_SECRET" not in page.text


@pytest.mark.asyncio
async def test_keys_missing_refusal_does_not_show_undecryptable_banner(
    authed_admin_client: AsyncClient, no_keys: None, clean_refusals
) -> None:
    _refuse(clean_refusals, kind="keys_missing", names=["API_TOKEN"])
    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert _UNDECRYPTABLE_BANNER not in page.text
    # The case is covered by the "not configured" notice.
    assert "<code>TASK_SECRETS_KEYS_PATH</code> is not configured." in page.text


# ---------------------------------------------------------------------------
# Set / replace / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_secret_prg_flash_and_audit(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    resp = await _set(authed_admin_client, "API_TOKEN", VALUE, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/secrets"
    _assert_no_value(resp, VALUE)
    assert any("task_secrets_flash" in c for c in resp.headers.get_list("set-cookie"))

    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200
    assert "Secret API_TOKEN saved." in page.text
    expected = TaskSecretsKeyring([keys_file.read_text().strip().encode()]).fingerprint(VALUE)
    assert expected in page.text
    assert VALUE[-4:] not in page.text
    # updated_by: the owner's display_name (no email).
    assert '<td class="muted">Admin</td>' in page.text
    _assert_no_value(page, VALUE)

    # The flash is single-use.
    again = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert "Secret API_TOKEN saved." not in again.text

    audit = await _audit("set_task_secret")
    assert len(audit) == 1
    assert audit[0]["target_kind"] == "task_secret"
    assert audit[0]["actor"] == await _owner_id()
    assert audit[0]["payload"] == {"name": "API_TOKEN", "worker_id": None, "scope": "workspace"}


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_set_multiline_pem_keeps_line_breaks(
    authed_admin_client: AsyncClient, keys_file: Path, newline: str
) -> None:
    # The browser sends a textarea's line breaks as CRLF; they are stored as LF,
    # which is what the operator pasted.
    lines = [
        "-----BEGIN PRIVATE KEY-----",
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
        "-----END PRIVATE KEY-----",
    ]
    resp = await _set(authed_admin_client, "SSH_KEY", newline.join(lines), follow_redirects=False)
    assert resp.status_code == 303, resp.text
    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    ring = TaskSecretsKeyring([keys_file.read_text().strip().encode()])
    assert ring.fingerprint("\n".join(lines)) in page.text
    assert ring.fingerprint("\r\n".join(lines)) not in page.text
    _assert_no_value(page, "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC")


@pytest.mark.asyncio
async def test_replace_secret_keeps_one_row_new_fingerprint(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    await _set(authed_admin_client, "API_TOKEN", VALUE)
    before = await _secret_rows()
    resp = await _set(authed_admin_client, "API_TOKEN", OTHER_VALUE, follow_redirects=True)
    assert resp.status_code == 200
    assert "Secret API_TOKEN saved." in resp.text
    _assert_no_value(resp, VALUE, OTHER_VALUE)
    after = await _secret_rows()
    assert [r["name"] for r in after] == ["API_TOKEN"]
    assert after[0]["fingerprint"] != before[0]["fingerprint"]
    assert len(await _audit("set_task_secret")) == 2


@pytest.mark.asyncio
async def test_fingerprint_is_not_plain_sha256(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    short = "s3cr3tPW"
    resp = await _set(authed_admin_client, "SHORT_ONE", short, follow_redirects=True)
    assert resp.status_code == 200
    assert hashlib.sha256(short.encode()).hexdigest()[:12] not in resp.text
    assert "hmac-sha256:" in resp.text
    _assert_no_value(resp, short)


@pytest.mark.asyncio
async def test_delete_secret_prg_flash_and_audit(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    await _set(authed_admin_client, "API_TOKEN", VALUE)
    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert 'data-confirm-url="/admin/secrets/API_TOKEN/delete"' in page.text

    resp = await _delete(authed_admin_client, "API_TOKEN")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/secrets"
    after = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert "Secret API_TOKEN deleted." in after.text
    assert "No task secrets yet." in after.text
    assert await _secret_rows() == []

    audit = await _audit("delete_task_secret")
    assert len(audit) == 1
    assert audit[0]["actor"] == await _owner_id()
    assert audit[0]["payload"] == {"name": "API_TOKEN", "worker_id": None, "scope": "workspace"}

    missing = await _delete(authed_admin_client, "API_TOKEN")
    assert missing.status_code == 404
    assert len(await _audit("delete_task_secret")) == 1


@pytest.mark.asyncio
async def test_dir_name_stored_before_the_reservation_is_listed_and_deletable(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    """A row stored under a worker-owned directory name before the reservation
    shows in the table with its delete action, and the owner can delete it."""
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO task_secrets (workspace_id, name, ciphertext, fingerprint) "
                    "VALUES (:ws, 'TASKORCH_CACHE_DIR', '\\x00', 'hmac-sha256:000000000000')"
                ),
                {"ws": DEFAULT_WORKSPACE_ID},
            )
    finally:
        await engine.dispose()

    page = await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)
    assert page.status_code == 200, page.text
    assert 'data-confirm-url="/admin/secrets/TASKORCH_CACHE_DIR/delete"' in page.text

    resp = await _delete(authed_admin_client, "TASKORCH_CACHE_DIR")
    assert resp.status_code == 303, resp.text
    assert await _secret_rows() == []


@pytest.mark.asyncio
async def test_fetch_follow_does_not_consume_flash(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    """The button with `data-confirm-url` uses fetch, which follows the 303 with a
    non-navigational GET; the flash must survive until the page reload."""
    await _set(authed_admin_client, "API_TOKEN", VALUE)
    await authed_admin_client.get("/admin/secrets", headers=NO_HTMX)  # consumes the set one
    await _delete(authed_admin_client, "API_TOKEN")
    followed = await authed_admin_client.get(
        "/admin/secrets", headers={**NO_HTMX, "Sec-Fetch-Mode": "cors"}
    )
    assert followed.status_code == 200
    assert "Secret API_TOKEN deleted." not in followed.text
    reloaded = await authed_admin_client.get(
        "/admin/secrets", headers={**NO_HTMX, "Sec-Fetch-Mode": "navigate"}
    )
    assert "Secret API_TOKEN deleted." in reloaded.text


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("1BAD", VALUE, "invalid secret name"),
        ("HAS-DASH", VALUE, "invalid secret name"),
        ("ORCH_TOKEN", VALUE, "ORCH_"),
        ("PATH", VALUE, "reserved"),
        ("LD_PRELOAD", VALUE, "reserved"),
        ("orcaster_cache_dir", VALUE, "reserved"),
        ("TASKORCH_ARTIFACTS_DIR", VALUE, "reserved"),
        ("", VALUE, "non-empty"),
        ("A" * 129, VALUE, "at most 128"),
        ("API_TOKEN", "", "non-empty"),
        ("API_TOKEN", "x" * (64 * 1024 + 1), "at most"),
        ("API_TOKEN", "-----BEGIN PRIVATE KEY-----MIIEvQ-----END PRIVATE KEY-----", "line breaks"),
    ],
    ids=[
        "leading-digit",
        "dash",
        "orch-prefix",
        "reserved-path",
        "reserved-ld-prefix",
        "reserved-dir-name-lowercase",
        "reserved-legacy-dir-alias",
        "empty-name",
        "long-name",
        "empty-value",
        "long-value",
        "pem-without-line-breaks",
    ],
)
async def test_validation_errors_inline_400(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    name: str,
    value: str,
    fragment: str,
) -> None:
    resp = await _set(authed_admin_client, name, value, follow_redirects=False)
    assert resp.status_code == 400, resp.text
    assert 'role="alert"' in resp.text
    assert fragment in resp.text
    assert 'id="secret-value"' in resp.text
    # The value field is re-rendered empty.
    assert 'name="value" value=' not in resp.text
    assert re.search(r'<textarea id="secret-value"[^>]*></textarea>', resp.text)
    if value:
        _assert_no_value(resp, value)
    assert resp.headers.get("location") is None
    assert await _secret_rows() == []
    assert await _audit("set_task_secret") == []


@pytest.mark.asyncio
async def test_validation_error_keeps_name(
    authed_admin_client: AsyncClient, keys_file: Path
) -> None:
    resp = await _set(authed_admin_client, "API_TOKEN", "", follow_redirects=False)
    assert resp.status_code == 400
    assert 'value="API_TOKEN"' in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["hf_", "ghp_", "github_pat_", "glpat-"])
async def test_token_prefixed_name_is_400_without_echo(
    authed_admin_client: AsyncClient, keys_file: Path, prefix: str
) -> None:
    """A token pasted in the Name field is neither stored nor echoed back in the HTML."""
    mark = "SECRETMARK123"
    resp = await _set(authed_admin_client, f"{prefix}{mark}", "v", follow_redirects=False)
    assert resp.status_code == 400, resp.text
    assert "known token prefix" in resp.text
    assert mark not in resp.text
    assert await _secret_rows() == []
    assert await _audit("set_task_secret") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "API_TOKEN=hf_SECRETMARK123",
        "Bearer hf_SECRETMARK123",
        "ORCH_SECRETMARK123",
        "LD_SECRETMARK123",
    ],
)
async def test_rejected_name_is_not_rerendered(
    authed_admin_client: AsyncClient, keys_file: Path, name: str
) -> None:
    """Every rejected name, not only one with a token prefix, stays out of the
    400 HTML: it may be a `NAME=value` containing the secret."""
    resp = await _set(authed_admin_client, name, "v", follow_redirects=False)
    assert resp.status_code == 400, resp.text
    assert "SECRETMARK123" not in resp.text
    assert await _secret_rows() == []
    assert await _audit("set_task_secret") == []


# ---------------------------------------------------------------------------
# CSRF: Origin check
# ---------------------------------------------------------------------------

# Same matrix as the `/auth` join (`test_invitation_join.py`): without `Origin`
# only `Sec-Fetch-Site` same-origin or none passes; with neither, 403.
_FOREIGN = [
    ("https://evil.example", None),
    ("null", None),
    ("https://evil.example", "same-origin"),
    (None, "same-site"),
    (None, "cross-site"),
    (None, None),
]


@pytest.mark.parametrize(("origin", "sec_fetch_site"), _FOREIGN)
@pytest.mark.asyncio
async def test_set_from_foreign_origin_returns_403_and_changes_nothing(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    origin: str | None,
    sec_fetch_site: str | None,
) -> None:
    assert (await _set(authed_admin_client, "API_TOKEN", VALUE)).status_code == 303
    before = await _secret_rows()

    resp = await _set(
        authed_admin_client,
        "API_TOKEN",
        OTHER_VALUE,
        origin=origin,
        sec_fetch_site=sec_fetch_site,
        follow_redirects=False,
    )

    assert resp.status_code == 403
    _assert_no_value(resp, OTHER_VALUE)
    assert await _secret_rows() == before
    assert len(await _audit("set_task_secret")) == 1


@pytest.mark.parametrize(("origin", "sec_fetch_site"), _FOREIGN)
@pytest.mark.asyncio
async def test_delete_from_foreign_origin_returns_403_and_keeps_secret(
    authed_admin_client: AsyncClient,
    keys_file: Path,
    origin: str | None,
    sec_fetch_site: str | None,
) -> None:
    assert (await _set(authed_admin_client, "API_TOKEN", VALUE)).status_code == 303

    resp = await _delete(
        authed_admin_client, "API_TOKEN", origin=origin, sec_fetch_site=sec_fetch_site
    )

    assert resp.status_code == 403
    assert [r["name"] for r in await _secret_rows()] == ["API_TOKEN"]
    assert await _audit("delete_task_secret") == []


@pytest.mark.parametrize("sec_fetch_site", ["same-origin", "none"])
@pytest.mark.asyncio
async def test_without_origin_accepts_same_origin_fetch_metadata(
    authed_admin_client: AsyncClient, keys_file: Path, sec_fetch_site: str
) -> None:
    setr = await _set(
        authed_admin_client,
        "API_TOKEN",
        VALUE,
        origin=None,
        sec_fetch_site=sec_fetch_site,
        follow_redirects=False,
    )
    assert setr.status_code == 303
    assert [r["name"] for r in await _secret_rows()] == ["API_TOKEN"]

    delr = await _delete(
        authed_admin_client, "API_TOKEN", origin=None, sec_fetch_site=sec_fetch_site
    )
    assert delr.status_code == 303
    assert await _secret_rows() == []
