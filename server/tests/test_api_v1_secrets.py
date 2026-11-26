"""`/api/v1/secrets` API: list, set and delete task secrets."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server.config import settings
from tests.conftest import mint_pat_via_app


@pytest.fixture
def keys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from orcaster_server.task_secrets import keyring

    p = tmp_path / "task-secrets-keys"
    p.write_text(Fernet.generate_key().decode() + "\n")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))
    keyring.reset_keyring_cache()
    return p


# `worker_id` and `worker_name` (null for workspace-level ones) are additive.
_ITEM_KEYS = {
    "name",
    "fingerprint",
    "created_at",
    "updated_at",
    "updated_by",
    "worker_id",
    "worker_name",
}


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_owner_sets_lists_and_deletes(
    app_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    token, uid = await mint_pat_via_app(role="owner", email="owner@example.com")

    put = await app_client.put(
        "/api/v1/secrets/API_TOKEN", json={"value": "supersecret_value_ABCD"}, headers=_h(token)
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert set(body) == _ITEM_KEYS
    assert body["name"] == "API_TOKEN"
    assert body["worker_id"] is None and body["worker_name"] is None
    assert body["fingerprint"].startswith("hmac-sha256:")
    assert "ABCD" not in body["fingerprint"]
    assert body["updated_by"] == "owner@example.com"
    assert "supersecret_value_ABCD" not in put.text

    listed = await app_client.get("/api/v1/secrets", headers=_h(token))
    assert listed.status_code == 200
    assert set(listed.json()) == {"items", "keys_configured", "supports_worker_scope"}
    assert listed.json()["supports_worker_scope"] is True
    assert listed.json()["keys_configured"] is True
    items = listed.json()["items"]
    assert [i["name"] for i in items] == ["API_TOKEN"]
    assert set(items[0]) == _ITEM_KEYS
    assert "supersecret_value_ABCD" not in listed.text

    deleted = await app_client.delete("/api/v1/secrets/API_TOKEN", headers=_h(token))
    assert deleted.status_code == 204
    assert deleted.content == b""

    missing = await app_client.delete("/api/v1/secrets/API_TOKEN", headers=_h(token))
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT action, actor, payload FROM admin_actions "
                        "WHERE target_kind = 'task_secret' ORDER BY id"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("set_task_secret", uid, {"name": "API_TOKEN", "worker_id": None, "scope": "workspace"}),
        ("delete_task_secret", uid, {"name": "API_TOKEN", "worker_id": None, "scope": "workspace"}),
    ]


@pytest.mark.asyncio
async def test_member_lists_but_cannot_write(app_client: AsyncClient, keys_file: Path) -> None:
    owner, _ = await mint_pat_via_app(role="owner", email="o@example.com")
    member, _ = await mint_pat_via_app(role="member", email="m@example.com")
    assert (
        await app_client.put("/api/v1/secrets/A", json={"value": "v"}, headers=_h(owner))
    ).status_code == 200

    listed = await app_client.get("/api/v1/secrets", headers=_h(member))
    assert listed.status_code == 200
    assert [i["name"] for i in listed.json()["items"]] == ["A"]

    put = await app_client.put("/api/v1/secrets/A", json={"value": "x"}, headers=_h(member))
    assert put.status_code == 403
    assert put.json()["error"] == "forbidden"
    delete = await app_client.delete("/api/v1/secrets/A", headers=_h(member))
    assert delete.status_code == 403


@pytest.mark.asyncio
async def test_requires_pat(app_client: AsyncClient, keys_file: Path) -> None:
    resp = await app_client.get("/api/v1/secrets")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "1ABC",
        "HF-TOKEN",
        "ORCH_API_KEY",
        "orch_token",
        "Orch_X",
        "A" * 129,
        "LD_PRELOAD",
        "PATH",
        "X_CA_BUNDLE",
        "CARGO_HOME",
        "pager",
        "HF_ENDPOINT",
        "sslkeylogfile",
    ],
)
async def test_invalid_name_is_422(app_client: AsyncClient, keys_file: Path, name: str) -> None:
    token, _ = await mint_pat_via_app(role="owner")
    resp = await app_client.put(f"/api/v1/secrets/{name}", json={"value": "v"}, headers=_h(token))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "unprocessable_entity"
    if name in (
        "LD_PRELOAD",
        "PATH",
        "X_CA_BUNDLE",
        "CARGO_HOME",
        "pager",
        "HF_ENDPOINT",
        "sslkeylogfile",
    ):
        assert "reserved name" in resp.json()["message"]
        # The message gives the reason without repeating the name.
        assert name not in resp.text
    if name.upper().startswith("ORCH_"):
        assert "ORCH_" in resp.json()["message"]
        assert name not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{"value": ""}, {"value": "a\x00b"}, {"value": "a" * 65537}, {}, {"value": 3}],
    ids=["empty", "nul", "too-long", "missing", "not-a-string"],
)
async def test_invalid_value_is_422(app_client: AsyncClient, keys_file: Path, body: dict) -> None:
    token, _ = await mint_pat_via_app(role="owner")
    resp = await app_client.put("/api/v1/secrets/A", json=body, headers=_h(token))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "unprocessable_entity"
    # The 422 validation response does not return the rejected value.
    if isinstance(body.get("value"), str) and body["value"]:
        assert body["value"] not in resp.text


@pytest.mark.asyncio
async def test_unconfigured_lists_metadata_without_keys(
    app_client: AsyncClient, keys_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing does not decrypt: without a keys file it responds 200 with the stored metadata
    and `keys_configured: false`."""
    from orcaster_server.task_secrets import keyring

    token, _ = await mint_pat_via_app(role="owner", email="owner@example.com")
    put = await app_client.put(
        "/api/v1/secrets/API_TOKEN", json={"value": "supersecret_value_ABCD"}, headers=_h(token)
    )
    assert put.status_code == 200, put.text

    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    listed = await app_client.get("/api/v1/secrets", headers=_h(token))
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["keys_configured"] is False
    assert [i["name"] for i in body["items"]] == ["API_TOKEN"]
    assert body["items"][0]["fingerprint"] == put.json()["fingerprint"]
    assert body["items"][0]["updated_by"] == "owner@example.com"
    assert "supersecret_value_ABCD" not in listed.text


@pytest.mark.asyncio
async def test_unconfigured_empty_list(
    app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server.task_secrets import keyring

    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    token, _ = await mint_pat_via_app(role="member")
    listed = await app_client.get("/api/v1/secrets", headers=_h(token))
    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "items": [],
        "keys_configured": False,
        "supports_worker_scope": True,
    }


@pytest.mark.asyncio
async def test_unconfigured_set_is_503(
    app_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server.task_secrets import keyring

    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    token, _ = await mint_pat_via_app(role="owner")
    resp = await app_client.put("/api/v1/secrets/A", json={"value": "v"}, headers=_h(token))
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"] == "task_secrets_unconfigured"


@pytest.mark.asyncio
async def test_delete_without_keys_removes_secret_and_audits(
    app_client: AsyncClient, keys_file: Path, monkeypatch: pytest.MonkeyPatch, migrated_db_url: str
) -> None:
    """Deleting does not decrypt, so it does not need the keys file.
    Without keys, DELETE responds 204, the row goes away and the audit record remains."""
    from orcaster_server.task_secrets import keyring

    token, _ = await mint_pat_via_app(role="owner", email="owner@example.com")
    put = await app_client.put("/api/v1/secrets/API_TOKEN", json={"value": "v"}, headers=_h(token))
    assert put.status_code == 200, put.text

    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    resp = await app_client.delete("/api/v1/secrets/API_TOKEN", headers=_h(token))
    assert resp.status_code == 204, resp.text

    listed = await app_client.get("/api/v1/secrets", headers=_h(token))
    assert listed.json()["items"] == []
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            audit = (
                (
                    await conn.execute(
                        text(
                            "SELECT payload FROM admin_actions "
                            "WHERE action = 'delete_task_secret' ORDER BY ts"
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    assert audit == [{"name": "API_TOKEN", "worker_id": None, "scope": "workspace"}]


@pytest.mark.asyncio
async def test_cross_workspace_isolation(app_client: AsyncClient, keys_file: Path) -> None:
    from orcaster_server import db as app_db

    sm = app_db.get_sessionmaker()
    assert sm is not None
    async with sm() as s:
        other = (
            await s.execute(
                text("INSERT INTO workspaces (name, slug) VALUES ('Other', 'other') RETURNING id")
            )
        ).scalar_one()
        await s.commit()
    tok_default, _ = await mint_pat_via_app(role="owner", email="a@example.com")
    tok_other, _ = await mint_pat_via_app(
        workspace_id=str(other), role="owner", email="b@example.com"
    )
    assert (
        await app_client.put("/api/v1/secrets/A", json={"value": "v"}, headers=_h(tok_default))
    ).status_code == 200
    listed = await app_client.get("/api/v1/secrets", headers=_h(tok_other))
    assert listed.json()["items"] == []
    resp = await app_client.delete("/api/v1/secrets/A", headers=_h(tok_other))
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["GLIBC_TUNABLES", "LD_PRELOAD", "orch_old", "ORCASTER_CACHE_DIR", "taskorch_cache_dir"]
)
async def test_delete_accepts_name_reserved_after_it_was_stored(
    app_client: AsyncClient, keys_file: Path, migrated_db_url: str, name: str
) -> None:
    """A row whose name became reserved after it was stored can be
    deleted: DELETE only requires the name pattern, not the denylist."""
    from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

    token, _ = await mint_pat_via_app(role="owner")
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO task_secrets (workspace_id, name, ciphertext, fingerprint) "
                    "VALUES (:ws, :name, '\\x00', 'hmac-sha256:000000000000')"
                ),
                {"ws": DEFAULT_WORKSPACE_ID, "name": name},
            )
    finally:
        await engine.dispose()
    resp = await app_client.delete(f"/api/v1/secrets/{name}", headers=_h(token))
    assert resp.status_code == 204, resp.text


# --- Worker-owned directory names ---------------------------------------------

_DIR_NAMES = (
    "ORCASTER_CACHE_DIR",
    "ORCASTER_ARTIFACTS_DIR",
    "TASKORCH_CACHE_DIR",
    "TASKORCH_ARTIFACTS_DIR",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        variant
        for base in _DIR_NAMES
        for variant in (base, base.lower(), base[:1] + base[1:].lower())
    ],
)
async def test_put_worker_owned_dir_name_is_422(
    app_client: AsyncClient, keys_file: Path, migrated_db_url: str, name: str
) -> None:
    """The worker sets the cache and artifacts dir names, legacy aliases included;
    a secret cannot take them in any letter case."""
    token, _ = await mint_pat_via_app(role="owner")
    resp = await app_client.put(f"/api/v1/secrets/{name}", json={"value": "v"}, headers=_h(token))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "unprocessable_entity"
    assert "reserved name" in resp.json()["message"]
    assert name not in resp.text
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM task_secrets"))).scalar_one()
    finally:
        await engine.dispose()
    assert n == 0


@pytest.mark.asyncio
async def test_dir_name_stored_before_the_reservation_is_listed_and_deletable(
    app_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """Rows stored under the directory names before they were reserved stay
    visible to the owner, so they can be found and deleted."""
    from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

    token, _ = await mint_pat_via_app(role="owner")
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            for name in _DIR_NAMES:
                await conn.execute(
                    text(
                        "INSERT INTO task_secrets (workspace_id, name, ciphertext, fingerprint) "
                        "VALUES (:ws, :name, '\\x00', 'hmac-sha256:000000000000')"
                    ),
                    {"ws": DEFAULT_WORKSPACE_ID, "name": name},
                )
    finally:
        await engine.dispose()

    listed = await app_client.get("/api/v1/secrets", headers=_h(token))
    assert listed.status_code == 200, listed.text
    assert sorted(i["name"] for i in listed.json()["items"]) == sorted(_DIR_NAMES)

    for name in _DIR_NAMES:
        resp = await app_client.delete(f"/api/v1/secrets/{name}", headers=_h(token))
        assert resp.status_code == 204, resp.text
    listed = await app_client.get("/api/v1/secrets", headers=_h(token))
    assert listed.json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["1BAD", "a-b", "A" * 129])
async def test_delete_still_rejects_invalid_pattern(
    app_client: AsyncClient, keys_file: Path, name: str
) -> None:
    token, _ = await mint_pat_via_app(role="owner")
    resp = await app_client.delete(f"/api/v1/secrets/{name}", headers=_h(token))
    assert resp.status_code == 422, resp.text


# --- Token prefix in the name ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix", ["hf_", "ghp_", "gho_", "ghs_", "ghu_", "ghr_", "github_pat_", "glpat-", "sk-"]
)
async def test_put_token_prefixed_name_is_422_without_echo(
    app_client: AsyncClient, keys_file: Path, migrated_db_url: str, prefix: str
) -> None:
    token, _ = await mint_pat_via_app(role="owner")
    mark = "SECRETMARK123"
    resp = await app_client.put(
        f"/api/v1/secrets/{prefix}{mark}", json={"value": "v"}, headers=_h(token)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "unprocessable_entity"
    assert "known token prefix" in resp.json()["message"]
    # No byte of the name anywhere in the body.
    assert mark not in resp.text
    assert prefix not in resp.text
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM task_secrets"))).scalar_one()
    finally:
        await engine.dispose()
    assert n == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["HF_TOKEN", "GITHUB_TOKEN", "OPENROUTER_API_KEY"])
async def test_put_variable_names_still_accepted(
    app_client: AsyncClient, keys_file: Path, name: str
) -> None:
    token, _ = await mint_pat_via_app(role="owner")
    resp = await app_client.put(f"/api/v1/secrets/{name}", json={"value": "v"}, headers=_h(token))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["hf_x", "ghp_x", "github_pat_x"])
async def test_delete_accepts_token_prefixed_name_stored_before_the_guard(
    app_client: AsyncClient, keys_file: Path, migrated_db_url: str, name: str
) -> None:
    """A row stored before the guard with a token-like name can be deleted."""
    from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

    token, _ = await mint_pat_via_app(role="owner")
    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO task_secrets (workspace_id, name, ciphertext, fingerprint) "
                    "VALUES (:ws, :name, '\\x00', 'hmac-sha256:000000000000')"
                ),
                {"ws": DEFAULT_WORKSPACE_ID, "name": name},
            )
    finally:
        await engine.dispose()
    resp = await app_client.delete(f"/api/v1/secrets/{name}", headers=_h(token))
    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_set_encrypts_with_the_keys_file_as_it_is_on_disk(
    app_client: AsyncClient, keys_file: Path, migrated_db_url: str
) -> None:
    """The process loaded `[old]`, the file changes to `[new, old]` without a restart
    and without any poll having rejected a delivery. The PUT must encrypt and
    fingerprint with `new`: if it encrypted with `old`, retiring that key (the last step of
    the rotation) would leave the row unreadable."""
    from cryptography.fernet import InvalidToken

    from orcaster_server.task_secrets import keyring

    token, _ = await mint_pat_via_app(role="owner", email="owner@example.com")
    old = keys_file.read_text().strip().encode()
    # The process already has the keys loaded, like a running uvicorn.
    assert keyring.get_keyring() is not None
    new = Fernet.generate_key()
    keys_file.write_text(new.decode() + "\n" + old.decode() + "\n")

    try:
        put = await app_client.put(
            "/api/v1/secrets/API_TOKEN", json={"value": "v-837"}, headers=_h(token)
        )
        assert put.status_code == 200, put.text

        engine = create_async_engine(migrated_db_url)
        try:
            async with engine.connect() as conn:
                ciphertext = bytes(
                    (
                        await conn.execute(
                            text("SELECT ciphertext FROM task_secrets WHERE name = 'API_TOKEN'")
                        )
                    ).scalar_one()
                )
        finally:
            await engine.dispose()

        assert Fernet(new).decrypt(ciphertext) == b"v-837"
        with pytest.raises(InvalidToken):
            Fernet(old).decrypt(ciphertext)
        # The fingerprint also follows the new primary.
        assert put.json()["fingerprint"] == keyring.load_keyring(str(keys_file)).fingerprint(
            "v-837"
        )
    finally:
        keyring.reset_keyring_cache()
