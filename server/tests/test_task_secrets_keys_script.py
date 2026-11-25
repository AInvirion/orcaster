"""`orcaster_server/cli/task_secrets_keys.py`: `generate` and `rotate`.

`rotate` also re-encrypts the token of active share links.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from orcaster_server.task_secrets.keyring import TaskSecretsKeyring, key_id
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID


def test_generate_prints_a_valid_key(capsys: pytest.CaptureFixture[str]) -> None:
    from orcaster_server.cli import task_secrets_keys

    assert task_secrets_keys.run(["generate"]) == 0
    out = capsys.readouterr().out.strip()
    Fernet(out.encode())  # does not raise: it is a valid Fernet key


async def _seed(session: AsyncSession, ring: TaskSecretsKeyring, names: list[str]) -> None:
    from orcaster_server.task_secrets import service

    uid = (
        await session.execute(text("INSERT INTO users (display_name) VALUES ('O') RETURNING id"))
    ).scalar_one()
    for name in names:
        await service.set_secret(
            session,
            ring,
            workspace_id=DEFAULT_WORKSPACE_ID,
            name=name,
            value=f"value-{name}-0123456789",
            actor_user_id=str(uid),
        )
    await session.commit()


@pytest.mark.asyncio
async def test_rotate_reencrypts_with_primary(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    from orcaster_server.cli.task_secrets_keys import RotateResult, rotate_all

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    await _seed(db_session, TaskSecretsKeyring([old_key]), ["A", "B"])
    before = dict(
        (await db_session.execute(text("SELECT name, fingerprint FROM task_secrets"))).all()
    )

    engine = create_async_engine(migrated_db_url)
    try:
        result = await rotate_all(engine, TaskSecretsKeyring([new_key, old_key]))
    finally:
        await engine.dispose()
    assert result == RotateResult(task_secrets=2, share_links=0)

    rows = (
        await db_session.execute(text("SELECT name, ciphertext, fingerprint FROM task_secrets"))
    ).all()
    only_new = Fernet(new_key)
    for name, ct, fp in rows:
        value = f"value-{name}-0123456789"
        assert only_new.decrypt(bytes(ct)).decode() == value
        # The fingerprint is recomputed with the new primary key.
        assert fp != before[name]
        assert fp == TaskSecretsKeyring([new_key]).fingerprint(value)


@pytest.mark.asyncio
async def test_rotate_aborts_whole_transaction_on_undecryptable(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    from orcaster_server.cli.task_secrets_keys import RotateError, rotate_all

    k1, k2, k3 = (Fernet.generate_key() for _ in range(3))
    await _seed(db_session, TaskSecretsKeyring([k1]), ["A"])
    await _seed(db_session, TaskSecretsKeyring([k2]), ["B"])
    before = (
        await db_session.execute(text("SELECT name, ciphertext, fingerprint FROM task_secrets"))
    ).all()

    engine = create_async_engine(migrated_db_url)
    try:
        with pytest.raises(RotateError) as exc:
            await rotate_all(engine, TaskSecretsKeyring([k3, k1]))
    finally:
        await engine.dispose()
    assert "B" in str(exc.value)
    assert "value-" not in str(exc.value)
    # With no affected share links, the message does not suggest revoking any.
    assert "share link" not in str(exc.value)
    after = (
        await db_session.execute(text("SELECT name, ciphertext, fingerprint FROM task_secrets"))
    ).all()
    assert sorted((n, bytes(c), f) for n, c, f in after) == sorted(
        (n, bytes(c), f) for n, c, f in before
    )


@pytest.mark.asyncio
async def test_rotate_reencrypts_worker_scoped_rows_and_names_their_scope(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    """Worker rows are re-encrypted the same way and, if they fail, the message
    tells the scope apart for a repeated name."""
    from orcaster_server.cli.task_secrets_keys import RotateError, RotateResult, rotate_all
    from orcaster_server.task_secrets import service

    k1, k2, k3 = (Fernet.generate_key() for _ in range(3))
    uid = (
        await db_session.execute(text("INSERT INTO users (display_name) VALUES ('O') RETURNING id"))
    ).scalar_one()
    wid = str(
        (
            await db_session.execute(
                text(
                    "INSERT INTO workers (workspace_id, api_key_hash, hostname, os, arch) "
                    "VALUES (:ws, 'h', 'w1', 'linux', 'x86_64') RETURNING id"
                ),
                {"ws": DEFAULT_WORKSPACE_ID},
            )
        ).scalar_one()
    )
    for ring, worker_id in ((TaskSecretsKeyring([k1]), None), (TaskSecretsKeyring([k2]), wid)):
        await service.set_secret(
            db_session,
            ring,
            workspace_id=DEFAULT_WORKSPACE_ID,
            name="GH",
            value="value-GH-0123456789",
            actor_user_id=str(uid),
            worker_id=worker_id,
        )
    await db_session.commit()

    engine = create_async_engine(migrated_db_url)
    try:
        with pytest.raises(RotateError) as exc:
            await rotate_all(engine, TaskSecretsKeyring([k3, k1]))
        assert f"GH (worker {wid})" in str(exc.value)
        result = await rotate_all(engine, TaskSecretsKeyring([k3, k1, k2]))
    finally:
        await engine.dispose()
    assert result == RotateResult(task_secrets=2, share_links=0)


def test_rotate_requires_configured_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    with pytest.raises(SystemExit, match="TASK_SECRETS_KEYS_PATH"):
        task_secrets_keys.run(["rotate"])


def test_rotate_prints_count_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    p = tmp_path / "keys"
    p.write_text(Fernet.generate_key().decode() + "\n")
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))

    async def _fake_rotate(engine, ring):
        return task_secrets_keys.RotateResult(task_secrets=7, share_links=5)

    monkeypatch.setattr(task_secrets_keys, "rotate_all", _fake_rotate)
    assert task_secrets_keys.run(["rotate"]) == 0
    out = capsys.readouterr().out
    assert "rotated 7 task secret(s) and 5 share link URL(s)" in out
    assert p.read_text().strip() not in out
    # Without `--keys-file` there is no guard; only the id line is added.
    kid = key_id(p.read_text().strip().encode())
    assert f"keys file primary key id: {kid} (1 key(s): {kid})" in out


def test_rotate_keys_file_overrides_unexpandable_env_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """With `$CREDENTIALS_DIRECTORY` in the environment (or `.env`), `rotate` from a
    shell cannot expand the path; `--keys-file` replaces it."""
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    key = Fernet.generate_key()
    p = tmp_path / "keys"
    p.write_text(key.decode() + "\n")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(
        settings, "task_secrets_keys_path", "$CREDENTIALS_DIRECTORY/task-secrets-keys"
    )
    seen: dict[str, str] = {}

    async def _fake_rotate(engine, ring):
        seen["fp"] = ring.fingerprint("probe-value-123")
        return task_secrets_keys.RotateResult(task_secrets=3, share_links=0)

    monkeypatch.setattr(task_secrets_keys, "rotate_all", _fake_rotate)

    with pytest.raises(SystemExit, match="CREDENTIALS_DIRECTORY"):
        task_secrets_keys.run(["rotate"])

    assert (
        task_secrets_keys.run(
            ["rotate", "--keys-file", str(p), "--confirm-server-key-id", key_id(key)]
        )
        == 0
    )
    assert seen["fp"] == TaskSecretsKeyring([key]).fingerprint("probe-value-123")
    out = capsys.readouterr().out
    assert "3" in out and key.decode() not in out


def test_rotate_keys_file_without_env_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    key = Fernet.generate_key()
    p = tmp_path / "keys"
    p.write_text(key.decode() + "\n")
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)

    async def _fake_rotate(engine, ring):
        return task_secrets_keys.RotateResult(task_secrets=0, share_links=0)

    monkeypatch.setattr(task_secrets_keys, "rotate_all", _fake_rotate)
    # Without a server path there is nothing to verify against: confirmation is required.
    with pytest.raises(SystemExit, match="--confirm-server-key-id"):
        task_secrets_keys.run(["rotate", "--keys-file", str(p)])
    assert (
        task_secrets_keys.run(
            ["rotate", "--keys-file", str(p), "--confirm-server-key-id", key_id(key)]
        )
        == 0
    )


# === Share links with a recoverable URL ======================================


async def _mint_link(
    session: AsyncSession, ring: TaskSecretsKeyring | None, label: str
) -> tuple[str, str]:
    from orcaster_server.status.tokens import create_share_token

    token, token_id = await create_share_token(
        session, workspace_id=DEFAULT_WORKSPACE_ID, label=label, keyring=ring
    )
    await session.commit()
    return token, token_id


async def _link_ciphertexts(session: AsyncSession) -> dict[str, bytes | None]:
    rows = (
        await session.execute(text("SELECT label, token_ciphertext FROM workspace_share_token"))
    ).all()
    return {label: (bytes(ct) if ct is not None else None) for label, ct in rows}


_UNDECRYPTABLE = "no key in TASK_SECRETS_KEYS_PATH decrypts: "
_UNFIXABLE = "share link URLs that no key can fix: "
_RESTORE_HINT = "(restore the old key, or revoke the share links listed)"
_REVOKE_HINT = "(revoke these share links)"


def _rotate_groups(message: str) -> tuple[str, str]:
    """`(undecryptable, unfixable)` from the `RotateError` message; "" if a group is missing."""
    groups = {"undecryptable": "", "unfixable": ""}
    for part in message.split("; "):
        if part.startswith(_UNDECRYPTABLE):
            groups["undecryptable"] = part
        elif part.startswith(_UNFIXABLE):
            groups["unfixable"] = part
    return groups["undecryptable"], groups["unfixable"]


@pytest.mark.asyncio
async def test_rotate_reencrypts_active_share_links(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    from orcaster_server.cli.task_secrets_keys import RotateResult, rotate_all
    from orcaster_server.status.tokens import list_share_tokens, revoke_share_token

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    old_ring = TaskSecretsKeyring([old_key])
    await _seed(db_session, old_ring, ["A"])
    active_token, _ = await _mint_link(db_session, old_ring, "active")
    await _mint_link(db_session, None, "legacy")
    _, revoked_id = await _mint_link(db_session, old_ring, "revoked")
    await revoke_share_token(db_session, token_id=revoked_id, workspace_id=DEFAULT_WORKSPACE_ID)
    await db_session.commit()
    before = await _link_ciphertexts(db_session)

    engine = create_async_engine(migrated_db_url)
    try:
        result = await rotate_all(engine, TaskSecretsKeyring([new_key, old_key]))
    finally:
        await engine.dispose()
    assert result == RotateResult(task_secrets=1, share_links=1)

    after = await _link_ciphertexts(db_session)
    # Active: re-encrypted with the new primary (derived keys), the token does not change.
    only_new = TaskSecretsKeyring([new_key]).share_links
    assert only_new.decrypt(after["active"]).decode() == active_token
    # Legacy and revoked: no ciphertext (revoke deletes it).
    assert after["legacy"] is None
    assert before["revoked"] is None
    assert after["revoked"] is None

    # With the old key retired, the active link's URL is still recoverable.
    rows = await list_share_tokens(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, keyring=TaskSecretsKeyring([new_key])
    )
    by_label = {r["label"]: r for r in rows}
    assert by_label["active"]["url_state"] == "available"
    assert by_label["active"]["url_token"] == active_token
    assert by_label["revoked"]["url_state"] == "revoked"
    assert by_label["legacy"]["url_state"] == "not_stored"


@pytest.mark.asyncio
async def test_rotate_aborts_on_undecryptable_share_link(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    from orcaster_server.cli.task_secrets_keys import RotateError, rotate_all

    k1, k2, k3 = (Fernet.generate_key() for _ in range(3))
    await _seed(db_session, TaskSecretsKeyring([k1]), ["A"])
    token, token_id = await _mint_link(db_session, TaskSecretsKeyring([k2]), "lost")
    secrets_before = (
        await db_session.execute(text("SELECT name, ciphertext FROM task_secrets"))
    ).all()
    links_before = await _link_ciphertexts(db_session)

    engine = create_async_engine(migrated_db_url)
    try:
        with pytest.raises(RotateError) as exc:
            await rotate_all(engine, TaskSecretsKeyring([k3, k1]))
    finally:
        await engine.dispose()
    message = str(exc.value)
    undecryptable, unfixable = _rotate_groups(message)
    # No key opens it, but it is a Fernet token: restoring the old key helps.
    assert f"share-link:{token_id}" in undecryptable
    assert undecryptable.endswith(_RESTORE_HINT)
    assert unfixable == ""
    assert _REVOKE_HINT not in message
    assert token not in message
    # All or nothing: neither the secret nor the link changed.
    secrets_after = (
        await db_session.execute(text("SELECT name, ciphertext FROM task_secrets"))
    ).all()
    assert [(n, bytes(c)) for n, c in secrets_after] == [(n, bytes(c)) for n, c in secrets_before]
    assert await _link_ciphertexts(db_session) == links_before


async def _set_link_ciphertext(session: AsyncSession, token_id: str, ct: bytes) -> None:
    await session.execute(
        text(
            "UPDATE workspace_share_token SET token_ciphertext = :ct WHERE id = CAST(:id AS uuid)"
        ),
        {"ct": ct, "id": token_id},
    )
    await session.commit()


@pytest.mark.asyncio
async def test_rotate_aborts_on_share_link_hash_mismatch(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    """Ciphertexts swapped between two links: they decrypt, but do not match their
    `token_hash`; the rotation aborts entirely, as with an unreadable secret."""
    from orcaster_server.cli.task_secrets_keys import RotateError, rotate_all

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    ring = TaskSecretsKeyring([old_key])
    await _seed(db_session, ring, ["A"])
    token_a, id_a = await _mint_link(db_session, ring, "a")
    token_b, id_b = await _mint_link(db_session, ring, "b")
    cts = await _link_ciphertexts(db_session)
    await _set_link_ciphertext(db_session, id_a, cts["b"])
    await _set_link_ciphertext(db_session, id_b, cts["a"])
    secrets_before = (
        await db_session.execute(text("SELECT name, ciphertext FROM task_secrets"))
    ).all()
    links_before = await _link_ciphertexts(db_session)

    engine = create_async_engine(migrated_db_url)
    try:
        with pytest.raises(RotateError) as exc:
            await rotate_all(engine, TaskSecretsKeyring([new_key, old_key]))
    finally:
        await engine.dispose()
    message = str(exc.value)
    undecryptable, unfixable = _rotate_groups(message)
    # Restoring a key does not fix a token that does not match its hash.
    for token_id in (id_a, id_b):
        assert f"share-link:{token_id} (token does not match its hash)" in unfixable
    assert unfixable.endswith(_REVOKE_HINT)
    assert undecryptable == ""
    assert "restore the old key" not in message
    assert token_a not in message and token_b not in message
    secrets_after = (
        await db_session.execute(text("SELECT name, ciphertext FROM task_secrets"))
    ).all()
    assert [(n, bytes(c)) for n, c in secrets_after] == [(n, bytes(c)) for n, c in secrets_before]
    assert await _link_ciphertexts(db_session) == links_before


@pytest.mark.asyncio
async def test_rotate_aborts_on_share_link_not_utf8(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    from orcaster_server.cli.task_secrets_keys import RotateError, rotate_all

    key = Fernet.generate_key()
    ring = TaskSecretsKeyring([key])
    _, token_id = await _mint_link(db_session, ring, "bin")
    await _set_link_ciphertext(db_session, token_id, ring.share_links.encrypt(b"\xff\xfe"))
    links_before = await _link_ciphertexts(db_session)

    engine = create_async_engine(migrated_db_url)
    try:
        with pytest.raises(RotateError) as exc:
            await rotate_all(engine, TaskSecretsKeyring([Fernet.generate_key(), key]))
    finally:
        await engine.dispose()
    message = str(exc.value)
    undecryptable, unfixable = _rotate_groups(message)
    assert f"share-link:{token_id} (token is not UTF-8)" in unfixable
    assert unfixable.endswith(_REVOKE_HINT)
    assert undecryptable == ""
    assert "restore the old key" not in message
    assert await _link_ciphertexts(db_session) == links_before


@pytest.mark.asyncio
async def test_rotate_aborts_on_share_link_not_a_fernet_token(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    """Bytes that are not a Fernet token: no key fixes them, only revoking does."""
    from orcaster_server.cli.task_secrets_keys import RotateError, rotate_all

    key = Fernet.generate_key()
    ring = TaskSecretsKeyring([key])
    _, token_id = await _mint_link(db_session, ring, "garbage")
    await _set_link_ciphertext(db_session, token_id, b"not-a-fernet-token")
    links_before = await _link_ciphertexts(db_session)

    engine = create_async_engine(migrated_db_url)
    try:
        with pytest.raises(RotateError) as exc:
            await rotate_all(engine, TaskSecretsKeyring([Fernet.generate_key(), key]))
    finally:
        await engine.dispose()
    message = str(exc.value)
    undecryptable, unfixable = _rotate_groups(message)
    assert f"share-link:{token_id} (not a Fernet token)" in unfixable
    assert unfixable.endswith(_REVOKE_HINT)
    assert undecryptable == ""
    assert "restore the old key" not in message
    assert await _link_ciphertexts(db_session) == links_before


@pytest.mark.asyncio
async def test_rotate_rejects_task_secret_ciphertext_in_share_row(
    db_session: AsyncSession, migrated_db_url: str
) -> None:
    """Domain separation: a secret ciphertext copied into a share link is not opened
    by the derived keys, so the rotation names it and aborts.

    The direct assertions on the two `MultiFernet` objects and on the message cause
    fail if `share_links` goes back to the raw keys: in that case the copied
    ciphertext decrypts and the rotation rejects it only because of the hash.
    """
    from cryptography.fernet import InvalidToken

    from orcaster_server.cli.task_secrets_keys import RotateError, rotate_all

    key = Fernet.generate_key()
    ring = TaskSecretsKeyring([key])
    await _seed(db_session, ring, ["A"])
    _, token_id = await _mint_link(db_session, ring, "copied")
    secret_ct = (
        await db_session.execute(text("SELECT ciphertext FROM task_secrets WHERE name = 'A'"))
    ).scalar_one()
    share_ct = (await _link_ciphertexts(db_session))["copied"]
    # Before any hash check: neither opens the other's ciphertext.
    with pytest.raises(InvalidToken):
        ring.share_links.decrypt(bytes(secret_ct))
    with pytest.raises(InvalidToken):
        ring.decrypt(share_ct)
    await _set_link_ciphertext(db_session, token_id, bytes(secret_ct))

    engine = create_async_engine(migrated_db_url)
    try:
        with pytest.raises(RotateError) as exc:
            await rotate_all(engine, TaskSecretsKeyring([Fernet.generate_key(), key]))
    finally:
        await engine.dispose()
    message = str(exc.value)
    undecryptable, unfixable = _rotate_groups(message)
    assert (
        f"share-link:{token_id} (encrypted with the raw task secret keys: "
        "old format or a task secret)"
    ) in unfixable
    assert unfixable.endswith(_REVOKE_HINT)
    assert undecryptable == ""


# === `--keys-file` against the keys the server loads ========================


def _write_keys(path: Path, *keys: bytes) -> Path:
    path.write_text("".join(k.decode() + "\n" for k in keys))
    return path


async def _secret_rows(session: AsyncSession) -> list[tuple[str, bytes, str]]:
    rows = (
        await session.execute(text("SELECT name, ciphertext, fingerprint FROM task_secrets"))
    ).all()
    return sorted((n, bytes(c), f) for n, c, f in rows)


def _assert_no_key_material(output: str, *keys: bytes) -> None:
    for key in keys:
        assert key.decode() not in output


@pytest.mark.asyncio
async def test_rotate_keys_file_refuses_primary_missing_from_server_file(
    db_session: AsyncSession,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The copy carries a primary key the server does not have: nothing is rotated."""
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    await _seed(db_session, TaskSecretsKeyring([old_key]), ["A", "B"])
    before = await _secret_rows(db_session)
    server_file = _write_keys(tmp_path / "server-keys", old_key)
    copy = _write_keys(tmp_path / "copy", new_key, old_key)
    monkeypatch.setattr(settings, "database_url", migrated_db_url)
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(server_file))

    with pytest.raises(SystemExit) as exc:
        await task_secrets_keys._rotate(str(copy), None)
    message = str(exc.value)
    assert exc.value.code not in (0, None)
    assert key_id(new_key) in message and key_id(old_key) in message
    assert "nothing was rotated" in message
    assert "restart the server" in message
    assert await _secret_rows(db_session) == before
    captured = capsys.readouterr()
    assert f"keys file primary key id: {key_id(new_key)}" in captured.out
    _assert_no_key_material(message + captured.out + captured.err, old_key, new_key)


def test_rotate_keys_file_refusal_opens_no_db_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    server_file = _write_keys(tmp_path / "server-keys", old_key)
    copy = _write_keys(tmp_path / "copy", new_key, old_key)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(server_file))

    def _no_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("the guard must run before the DB engine is created")

    monkeypatch.setattr(task_secrets_keys, "create_async_engine", _no_engine)
    with pytest.raises(SystemExit, match="nothing was rotated"):
        task_secrets_keys.run(["rotate", "--keys-file", str(copy)])
    # A confirmation does not override verification against a readable file.
    with pytest.raises(SystemExit, match="nothing was rotated"):
        task_secrets_keys.run(
            ["rotate", "--keys-file", str(copy), "--confirm-server-key-id", key_id(new_key)]
        )


@pytest.mark.asyncio
async def test_rotate_keys_file_rotates_when_server_file_has_the_primary(
    db_session: AsyncSession,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    await _seed(db_session, TaskSecretsKeyring([old_key]), ["A"])
    server_file = _write_keys(tmp_path / "server-keys", new_key, old_key)
    copy = _write_keys(tmp_path / "copy", new_key, old_key)
    monkeypatch.setattr(settings, "database_url", migrated_db_url)
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(server_file))

    assert await task_secrets_keys._rotate(str(copy), None) == 0
    rows = await _secret_rows(db_session)
    assert [Fernet(new_key).decrypt(ct).decode() for _, ct, _ in rows] == ["value-A-0123456789"]
    out = capsys.readouterr().out
    ids = f"{key_id(new_key)},{key_id(old_key)}"
    assert f"keys file primary key id: {key_id(new_key)} (2 key(s): {ids})" in out
    assert "rotated 1 task secret(s)" in out
    _assert_no_key_material(out, old_key, new_key)


@pytest.mark.asyncio
async def test_rotate_keys_file_with_unreadable_server_path_needs_confirmation(
    db_session: AsyncSession,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Server path with a variable that only exists inside the service: the script
    cannot verify and requires the id confirmed against the server log."""
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    await _seed(db_session, TaskSecretsKeyring([old_key]), ["A"])
    before = await _secret_rows(db_session)
    copy = _write_keys(tmp_path / "copy", new_key, old_key)
    monkeypatch.delenv("ORCASTER_TEST_UNSET_VAR", raising=False)
    monkeypatch.setattr(settings, "database_url", migrated_db_url)
    monkeypatch.setattr(settings, "task_secrets_keys_path", "$ORCASTER_TEST_UNSET_VAR/keys")
    new_id = key_id(new_key)

    with pytest.raises(SystemExit) as exc:
        await task_secrets_keys._rotate(str(copy), None)
    message = str(exc.value)
    assert new_id in message
    assert f"--confirm-server-key-id {new_id}" in message
    assert "task secrets keys loaded" in message
    assert "nothing was rotated" in message
    assert await _secret_rows(db_session) == before

    # An id that is not the primary key of `--keys-file` is also rejected.
    for wrong in ("0123456789ab", key_id(old_key)):
        with pytest.raises(SystemExit) as exc:
            await task_secrets_keys._rotate(str(copy), wrong)
        assert "does not match" in str(exc.value)
        assert new_id in str(exc.value)
        assert await _secret_rows(db_session) == before
        message += str(exc.value)

    assert await task_secrets_keys._rotate(str(copy), new_id) == 0
    rows = await _secret_rows(db_session)
    assert rows != before
    assert [Fernet(new_key).decrypt(ct).decode() for _, ct, _ in rows] == ["value-A-0123456789"]
    captured = capsys.readouterr()
    _assert_no_key_material(message + captured.out + captured.err, old_key, new_key)


@pytest.mark.asyncio
async def test_rotate_keys_file_with_unset_server_path_needs_confirmation(
    db_session: AsyncSession,
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    key = Fernet.generate_key()
    await _seed(db_session, TaskSecretsKeyring([key]), ["A"])
    before = await _secret_rows(db_session)
    copy = _write_keys(tmp_path / "copy", key)
    monkeypatch.setattr(settings, "database_url", migrated_db_url)
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)

    with pytest.raises(SystemExit) as exc:
        await task_secrets_keys._rotate(str(copy), None)
    assert f"--confirm-server-key-id {key_id(key)}" in str(exc.value)
    assert "TASK_SECRETS_KEYS_PATH is not set" in str(exc.value)
    assert key.decode() not in str(exc.value)
    assert await _secret_rows(db_session) == before

    assert await task_secrets_keys._rotate(str(copy), key_id(key)) == 0
    assert await _secret_rows(db_session) != before


def test_rotate_without_keys_file_ignores_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Without `--keys-file` the script reads the same file as the server: no guard."""
    from orcaster_server.cli import task_secrets_keys
    from orcaster_server.config import settings

    k1, k2 = Fernet.generate_key(), Fernet.generate_key()
    p = _write_keys(tmp_path / "keys", k1, k2)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))

    async def _fake_rotate(engine, ring):
        return task_secrets_keys.RotateResult(task_secrets=1, share_links=0)

    monkeypatch.setattr(task_secrets_keys, "rotate_all", _fake_rotate)
    assert task_secrets_keys.run(["rotate", "--confirm-server-key-id", "0123456789ab"]) == 0
    captured = capsys.readouterr()
    ids = f"{key_id(k1)},{key_id(k2)}"
    assert f"keys file primary key id: {key_id(k1)} (2 key(s): {ids})" in captured.out
    _assert_no_key_material(captured.out + captured.err, k1, k2)
