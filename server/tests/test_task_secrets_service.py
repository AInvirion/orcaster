"""Task secret store: keys, fingerprint, names and service."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.config import settings
from orcaster_server.task_secrets.keyring import TaskSecretsKeyring
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID


def _write_keys(path: Path, *keys: bytes, extra: str = "") -> Path:
    path.write_text(extra + "".join(k.decode() + "\n" for k in keys))
    return path


# --- Key file -------------------------------------------------------------


def test_load_keyring_ignores_blank_and_comment_lines(tmp_path: Path) -> None:
    from orcaster_server.task_secrets.keyring import load_keyring

    k1, k2 = Fernet.generate_key(), Fernet.generate_key()
    p = tmp_path / "keys"
    p.write_text(f"# primaria\n\n{k1.decode()}\n   \n# vieja\n{k2.decode()}\n")
    ring = load_keyring(str(p))
    assert isinstance(ring, MultiFernet)
    # The first key is the primary: k1 alone decrypts what is encrypted.
    assert Fernet(k1).decrypt(ring.encrypt(b"x")) == b"x"
    # And the second still decrypts old values.
    assert ring.decrypt(Fernet(k2).encrypt(b"old")) == b"old"


def test_load_keyring_expands_credentials_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orcaster_server.task_secrets.keyring import load_keyring

    _write_keys(tmp_path / "task-secrets-keys", Fernet.generate_key())
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert load_keyring("$CREDENTIALS_DIRECTORY/task-secrets-keys") is not None
    assert load_keyring("${CREDENTIALS_DIRECTORY}/task-secrets-keys") is not None


def test_load_keyring_unset_variable_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from orcaster_server.task_secrets.keyring import KeyringError, load_keyring

    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(KeyringError, match="CREDENTIALS_DIRECTORY"):
        load_keyring("$CREDENTIALS_DIRECTORY/task-secrets-keys")


@pytest.mark.parametrize(
    "content",
    ["", "# comments only\n\n", "not-a-fernet-key\n"],
    ids=["empty", "only-comments", "invalid-key"],
)
def test_load_keyring_rejects_empty_or_invalid(tmp_path: Path, content: str) -> None:
    from orcaster_server.task_secrets.keyring import KeyringError, load_keyring

    p = tmp_path / "keys"
    p.write_text(content)
    with pytest.raises(KeyringError):
        load_keyring(str(p))


def test_load_keyring_invalid_line_message_does_not_echo_key(tmp_path: Path) -> None:
    from orcaster_server.task_secrets.keyring import KeyringError, load_keyring

    good = Fernet.generate_key().decode()
    bad = good[:-4] + "!!!!"
    p = tmp_path / "keys"
    p.write_text(f"{good}\n{bad}\n")
    with pytest.raises(KeyringError) as exc:
        load_keyring(str(p))
    assert "line 2" in str(exc.value)
    assert bad not in str(exc.value)
    assert good not in str(exc.value)


def test_load_keyring_missing_file(tmp_path: Path) -> None:
    from orcaster_server.task_secrets.keyring import KeyringError, load_keyring

    with pytest.raises(KeyringError):
        load_keyring(str(tmp_path / "nope"))


def test_get_keyring_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from orcaster_server.task_secrets import keyring

    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    assert keyring.is_configured() is False
    assert keyring.get_keyring() is None
    monkeypatch.setattr(settings, "task_secrets_keys_path", "")
    assert keyring.is_configured() is False
    assert keyring.get_keyring() is None


def test_share_link_cipher_is_domain_separated() -> None:
    """Share links encrypt with derived keys; neither `MultiFernet` opens the
    other's ciphertext, in both directions."""
    from cryptography.fernet import InvalidToken

    old, new = Fernet.generate_key(), Fernet.generate_key()
    ring = TaskSecretsKeyring([new, old])
    share_ct = ring.share_links.encrypt(b"share-token")
    secret_ct = ring.encrypt(b"secret-value")
    assert ring.share_links.decrypt(share_ct) == b"share-token"
    with pytest.raises(InvalidToken):
        ring.decrypt(share_ct)
    with pytest.raises(InvalidToken):
        ring.share_links.decrypt(secret_ct)
    # Deterministic per-key derivation: the old key alone still opens what the old
    # key encrypted (rotation), and the new primary encrypts new values.
    old_ct = TaskSecretsKeyring([old]).share_links.encrypt(b"t")
    assert ring.share_links.decrypt(old_ct) == b"t"
    with pytest.raises(InvalidToken):
        TaskSecretsKeyring([old]).share_links.decrypt(share_ct)


# --- Startup (lifespan) ---------------------------------------------------


def _prepare_lifespan(monkeypatch: pytest.MonkeyPatch, ed25519_key: Path) -> None:
    from orcaster_server import db as app_db
    from orcaster_server import signing

    monkeypatch.setattr(settings, "ed25519_private_key_path", str(ed25519_key))
    signing.reset_key_cache()
    monkeypatch.setattr(settings, "migrate_on_startup", False)
    monkeypatch.setattr(settings, "housekeeping_enabled", False)
    monkeypatch.setattr(app_db, "init_engine", lambda: None)

    async def _noop_dispose() -> None:
        return None

    monkeypatch.setattr(app_db, "dispose_engine", _noop_dispose)


@pytest.mark.asyncio
async def test_lifespan_warns_once_when_keys_unset(
    _session_ed25519_key: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from fastapi import FastAPI

    from orcaster_server.main import lifespan
    from orcaster_server.task_secrets import keyring

    _prepare_lifespan(monkeypatch, _session_ed25519_key)
    monkeypatch.setattr(settings, "task_secrets_keys_path", None)
    keyring.reset_keyring_cache()
    with caplog.at_level(logging.WARNING):
        async with lifespan(FastAPI()):
            pass
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "TASK_SECRETS_KEYS_PATH" in r.getMessage()
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_lifespan_fails_when_keys_invalid(
    _session_ed25519_key: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi import FastAPI

    from orcaster_server.main import lifespan
    from orcaster_server.task_secrets import keyring

    _prepare_lifespan(monkeypatch, _session_ed25519_key)
    bad = tmp_path / "keys"
    bad.write_text("garbage\n")
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(bad))
    keyring.reset_keyring_cache()
    with pytest.raises(keyring.KeyringError):
        async with lifespan(FastAPI()):
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_lifespan_succeeds_with_valid_keys(
    _session_ed25519_key: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi import FastAPI

    from orcaster_server.main import lifespan
    from orcaster_server.task_secrets import keyring

    _prepare_lifespan(monkeypatch, _session_ed25519_key)
    p = _write_keys(tmp_path / "keys", Fernet.generate_key())
    monkeypatch.setattr(settings, "task_secrets_keys_path", str(p))
    keyring.reset_keyring_cache()
    async with lifespan(FastAPI()):
        assert keyring.get_keyring() is not None


# --- Fingerprint ----------------------------------------------------------


def test_fingerprint_is_keyed_hmac_not_plain_sha256() -> None:
    """HMAC fingerprint with a key derived from the primary: a DB dump does not
    allow guessing the value offline."""
    from orcaster_server.task_secrets.keyring import TaskSecretsKeyring

    value = "hf_abcdefghijklmnopWXYZ"
    ring = TaskSecretsKeyring([Fernet.generate_key()])
    fp = ring.fingerprint(value)
    assert fp.startswith("hmac-sha256:")
    digest = fp.removeprefix("hmac-sha256:")
    assert len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)
    assert digest != hashlib.sha256(value.encode()).hexdigest()[:12]
    assert value[-4:] not in fp
    # Deterministic for the same key, different across values.
    assert ring.fingerprint(value) == fp
    assert ring.fingerprint(value + "x") != fp


def test_fingerprint_differs_across_primary_keys() -> None:
    from orcaster_server.task_secrets.keyring import TaskSecretsKeyring

    k1, k2 = Fernet.generate_key(), Fernet.generate_key()
    value = "hf_abcdefghijklmnopWXYZ"
    assert TaskSecretsKeyring([k1]).fingerprint(value) != TaskSecretsKeyring([k2]).fingerprint(
        value
    )
    # Only the primary counts: old keys do not change the fingerprint.
    assert TaskSecretsKeyring([k1, k2]).fingerprint(value) == TaskSecretsKeyring([k1]).fingerprint(
        value
    )


def test_load_keyring_fingerprints_with_primary(tmp_path: Path) -> None:
    from orcaster_server.task_secrets.keyring import TaskSecretsKeyring, load_keyring

    k1, k2 = Fernet.generate_key(), Fernet.generate_key()
    ring = load_keyring(str(_write_keys(tmp_path / "keys", k1, k2)))
    assert ring.fingerprint("some-value-123456") == TaskSecretsKeyring([k1]).fingerprint(
        "some-value-123456"
    )


# --- Names and values -----------------------------------------------------


@pytest.mark.parametrize("name", ["API_TOKEN", "_X", "a1", "A" * 128])
def test_secret_name_accepts(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    assert validate_secret_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "1ABC", "HF-TOKEN", "HF TOKEN", "A" * 129, "ORCH_API_KEY", "ÑAME"],
)
def test_secret_name_rejects(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    with pytest.raises(ValueError):
        validate_secret_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "SSL_CERT_X",
        "NODE_EXTRA_CA_BUNDLE",
        # Shell startup, glibc loader and runtimes.
        "BASH_ENV",
        "ENV",
        "IFS",
        "CDPATH",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "PROMPT_COMMAND",
        "GCONV_PATH",
        "LOCPATH",
        "GLIBC_TUNABLES",
        "glibc_tunables",
        "NODE_OPTIONS",
        "NODE_PATH",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        # Proxy: set by the worker.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        # Git prefixes, interpreters and installers.
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_EXEC_PATH",
        "PERL5LIB",
        "PERL5OPT",
        "PERL_MM_OPT",
        "RUBYOPT",
        "RUBYLIB",
        "GEM_HOME",
        "BUNDLE_GEMFILE",
        "NPM_CONFIG_REGISTRY",
        "JAVA_HOME",
        "DOTNET_STARTUP_HOOKS",
        "CORECLR_PROFILER",
    ],
)
def test_secret_name_rejects_reserved(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    with pytest.raises(ValueError, match="reserved name"):
        validate_secret_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "MY_PATH",
        "HOMEDIR",
        "HF_TOKEN",
        "CA_BUNDLE_X",
        "XLD_A",
        "GITHUB_TOKEN",
        "GITHUB_TOKENS",
        "OPENROUTER_API_KEY",
        # `hf_token` starts with `hf_`: the prefix guard stops it.
        "github_token",
        "CC_TOKEN",
        "EDITOR_API_KEY",
        "NODEJS_TOKEN",
        "MY_ORCH_TOKEN",
    ],
)
def test_secret_name_reserved_is_not_overbroad(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    assert validate_secret_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["path", "Path", "Ld_Preload", "git_ssh_command", "pythonpath", "http_proxy", "x_ca_bundle"],
)
def test_secret_name_reserved_is_case_insensitive(name: str) -> None:
    """The denylist compares in uppercase: curl and others read `http_proxy` in
    lowercase, and a case variant must not bypass the rule."""
    from orcaster_server.env_names import validate_secret_name

    with pytest.raises(ValueError, match="reserved name"):
        validate_secret_name(name)


@pytest.mark.parametrize(
    "name",
    [
        # Compilers, build and packaging.
        "CC",
        "CXX",
        "CPP",
        "LDSHARED",
        "CFLAGS",
        "LDFLAGS",
        "MAKEFLAGS",
        "GOFLAGS",
        "RUSTC_WRAPPER",
        "CARGO_HOME",
        "CARGO_BUILD_RUSTC_WRAPPER",
        # Interpreters and runtimes.
        "PERLLIB",
        "CLASSPATH",
        "PHPRC",
        "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        # Helper programs and client configuration.
        "LESSOPEN",
        "PAGER",
        "EDITOR",
        "VISUAL",
        "SHELL",
        "CURL_HOME",
        "WGETRC",
        "HGRCPATH",
        "OPENSSL_CONF",
        "OPENSSL_MODULES",
        "XDG_CONFIG_HOME",
        # Case variants of the new entries.
        "cc",
        "Ldflags",
        "cargo_home",
        "Openssl_Conf",
        "node_options",
        "xdg_config_home",
        "pager",
    ],
)
def test_secret_name_rejects_closed_list_additions(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    with pytest.raises(ValueError, match="reserved name"):
        validate_secret_name(name)


# The two names with a concrete case. `HF_ENDPOINT` is read by a client library and
# sends the task's `HF_TOKEN` to the host it names; `SSLKEYLOGFILE` makes Python's
# `ssl` write the TLS session keys to the path it names.
@pytest.mark.parametrize("name", ["HF_ENDPOINT", "SSLKEYLOGFILE", "Hf_Endpoint", "SslKeyLogFile"])
def test_secret_name_rejects_issue_680_additions(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    with pytest.raises(ValueError, match="reserved name"):
        validate_secret_name(name)


# Names reviewed and deliberately left out. If one enters the list, this test forces
# that decision to be reviewed as well.
@pytest.mark.parametrize(
    "name",
    [
        "HOSTALIASES",
        "LOCALDOMAIN",
        "RES_OPTIONS",
        "LUA_INIT",
        "PHP_INI_SCAN_DIR",
        "ZDOTDIR",
        "TCLLIBPATH",
        "R_PROFILE_USER",
        "NETRC",
        "MALLOC_ARENA_MAX",
        "LC_MESSAGES",
        "HF_TOKEN",
        "HF_HOME",
    ],
)
def test_secret_name_issue_680_left_out_names_stay_allowed(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    assert validate_secret_name(name) == name


@pytest.mark.parametrize("name", ["ORCH_TOKEN", "orch_token", "Orch_X", "oRCH_api_key"])
def test_secret_name_rejects_orch_prefix_in_any_case(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    with pytest.raises(ValueError, match="reserved ORCH_ prefix") as exc_info:
        validate_secret_name(name)
    # Without repeating the name.
    assert name not in str(exc_info.value)


@pytest.mark.parametrize("name", ["orch_lower", "Orch_X"])
def test_task_env_keeps_case_sensitive_orch_rule(name: str) -> None:
    """`task_env` keeps its case-sensitive `ORCH_` rule: changing it would reject
    payloads that production accepts today."""
    from orcaster_server.schemas.tasks import AdminCreateTaskRequest

    req = AdminCreateTaskRequest(payload={"task_env": [name]})
    assert req.payload["task_env"] == [name]


def test_task_env_does_not_apply_secret_denylist() -> None:
    from orcaster_server.schemas.tasks import AdminCreateTaskRequest

    req = AdminCreateTaskRequest(payload={"task_env": ["PATH", "LD_PRELOAD", "PYTHONPATH"]})
    assert req.payload["task_env"] == ["PATH", "LD_PRELOAD", "PYTHONPATH"]


def test_task_env_rejects_non_identifier_names() -> None:
    """`task_env` requires the same name rule as secrets: a name that is not an
    identifier is rejected with 422, without repeating it."""
    from orcaster_server.schemas.tasks import AdminCreateTaskRequest

    with pytest.raises(ValueError, match=r"task_env\[0\] is not a valid environment variable"):
        AdminCreateTaskRequest(payload={"task_env": ["weird-name", "API_TOKEN"]})
    req = AdminCreateTaskRequest(payload={"task_env": ["WEIRD_NAME", "API_TOKEN"]})
    assert req.payload["task_env"] == ["WEIRD_NAME", "API_TOKEN"]
    with pytest.raises(ValueError, match="may not declare ORCH_ vars"):
        AdminCreateTaskRequest(payload={"task_env": ["ORCH_X"]})


@pytest.mark.parametrize("value", ["x", "a" * 65536, "ñ" * 32768])
def test_secret_value_accepts(value: str) -> None:
    from orcaster_server.task_secrets.service import validate_secret_value

    assert validate_secret_value(value) == value


@pytest.mark.parametrize("value", ["", "a\x00b", "a" * 65537, "ñ" * 32769])
def test_secret_value_rejects(value: str) -> None:
    from orcaster_server.task_secrets.service import validate_secret_value

    with pytest.raises(ValueError):
        validate_secret_value(value)


_PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"


@pytest.mark.parametrize(
    "value",
    [
        _PEM,
        _PEM.replace("\n", "\r\n"),
        '{\n  "type": "service_account",\n  "private_key": "-----BEGIN KEY-----\\nAB\\n"\n}',
        '{"type": "service_account"}',
        "[1, 2]",
        "-----BEGIN not a pem but has a newline\n",
    ],
)
def test_secret_value_accepts_multiline_and_one_line_json(value: str) -> None:
    from orcaster_server.task_secrets.service import validate_secret_value

    assert validate_secret_value(value) == value


@pytest.mark.parametrize(
    "value",
    [
        # An <input type=password> strips line breaks.
        _PEM.replace("\n", ""),
        # The single-line prompt keeps only the header.
        "-----BEGIN PRIVATE KEY-----",
        "  -----BEGIN RSA PRIVATE KEY-----MIIEvQIB-----END RSA PRIVATE KEY-----",
        # First line of a multiline JSON.
        "{",
        "[",
        " { ",
    ],
)
def test_secret_value_rejects_multiline_value_cut_to_one_line(value: str) -> None:
    from orcaster_server.task_secrets.service import validate_secret_value

    with pytest.raises(ValueError, match="line breaks") as exc:
        validate_secret_value(value)
    # The message does not quote the value.
    assert "MIIEvQIB" not in str(exc.value)
    assert "PRIVATE KEY" not in str(exc.value)


# --- Service --------------------------------------------------------------


async def _user(session: AsyncSession, email: str | None = "owner@example.com") -> str:
    row = await session.execute(
        text("INSERT INTO users (display_name, email) VALUES ('Owner', :e) RETURNING id"),
        {"e": email},
    )
    return str(row.scalar_one())


@pytest.mark.asyncio
async def test_set_list_delete_roundtrip(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session)
    meta = await service.set_secret(
        db_session,
        ring,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="API_TOKEN",
        value="hf_secretvalue1234",
        actor_user_id=uid,
    )
    await db_session.commit()
    assert meta.name == "API_TOKEN"
    assert meta.fingerprint == ring.fingerprint("hf_secretvalue1234")
    assert meta.updated_by == "owner@example.com"

    stored = (
        await db_session.execute(text("SELECT ciphertext FROM task_secrets WHERE name='API_TOKEN'"))
    ).scalar_one()
    assert b"hf_secretvalue1234" not in bytes(stored)

    listed = await service.list_secrets(db_session, DEFAULT_WORKSPACE_ID)
    assert [m.name for m in listed] == ["API_TOKEN"]
    assert not hasattr(listed[0], "value")

    # Replacement: same id, created_at untouched, updated_at advances, new fingerprint.
    meta2 = await service.set_secret(
        db_session,
        ring,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="API_TOKEN",
        value="hf_othervalue9876",
        actor_user_id=uid,
    )
    await db_session.commit()
    assert meta2.created_at == meta.created_at
    assert meta2.updated_at >= meta.updated_at
    assert meta2.fingerprint != meta.fingerprint

    resolved = await service.resolve_for_task(
        db_session, ring, workspace_id=DEFAULT_WORKSPACE_ID, names=["API_TOKEN", "OTHER"]
    )
    assert resolved.values == {"API_TOKEN": "hf_othervalue9876"}
    assert resolved.worker_scoped == []

    assert await service.delete_secret(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, name="API_TOKEN", actor_user_id=uid
    )
    await db_session.commit()
    assert not await service.delete_secret(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, name="API_TOKEN", actor_user_id=uid
    )
    assert await service.list_secrets(db_session, DEFAULT_WORKSPACE_ID) == []

    audits = (
        (
            await db_session.execute(
                text(
                    "SELECT action, target_kind, actor, payload FROM admin_actions "
                    "WHERE target_kind = 'task_secret' ORDER BY id"
                )
            )
        )
        .mappings()
        .all()
    )
    assert [a["action"] for a in audits] == [
        "set_task_secret",
        "set_task_secret",
        "delete_task_secret",
    ]
    for a in audits:
        assert a["actor"] == uid
        # The payload names the scope; without a worker, the workspace one.
        assert a["payload"] == {"name": "API_TOKEN", "worker_id": None, "scope": "workspace"}


@pytest.mark.asyncio
async def test_updated_by_falls_back_and_nulls(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session, email=None)
    await service.set_secret(
        db_session,
        ring,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="A",
        value="v",
        actor_user_id=uid,
    )
    await db_session.commit()
    [meta] = await service.list_secrets(db_session, DEFAULT_WORKSPACE_ID)
    assert meta.updated_by == "Owner"
    # Deleting the user leaves the author NULL (FK ON DELETE SET NULL). Audit rows
    # store the actor as text, not as an FK.
    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
    await db_session.commit()
    [meta] = await service.list_secrets(db_session, DEFAULT_WORKSPACE_ID)
    assert meta.updated_by is None


@pytest.mark.asyncio
async def test_resolve_for_task_names_undecryptable(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    old = TaskSecretsKeyring([Fernet.generate_key()])
    new = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session)
    for name in ("GOOD", "BAD_ONE", "BAD_TWO"):
        await service.set_secret(
            db_session,
            new if name == "GOOD" else old,
            workspace_id=DEFAULT_WORKSPACE_ID,
            name=name,
            value=f"value-of-{name}",
            actor_user_id=uid,
        )
    await db_session.commit()
    with pytest.raises(service.UndecryptableTaskSecrets) as exc:
        await service.resolve_for_task(
            db_session,
            new,
            workspace_id=DEFAULT_WORKSPACE_ID,
            names=["GOOD", "BAD_TWO", "BAD_ONE"],
        )
    assert exc.value.names == ["BAD_ONE", "BAD_TWO"]
    assert "value-of" not in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_for_task_empty_names_skips_query(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    resolved = await service.resolve_for_task(
        db_session, ring, workspace_id=DEFAULT_WORKSPACE_ID, names=[]
    )
    assert resolved == service.ResolvedSecrets(values={}, worker_scoped=[])


@pytest.mark.asyncio
async def test_secrets_are_scoped_by_workspace(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session)
    other = (
        await db_session.execute(
            text("INSERT INTO workspaces (name, slug) VALUES ('Other', 'other') RETURNING id")
        )
    ).scalar_one()
    await service.set_secret(
        db_session,
        ring,
        workspace_id=str(other),
        name="API_TOKEN",
        value="other-ws",
        actor_user_id=uid,
    )
    await db_session.commit()
    assert await service.list_secrets(db_session, DEFAULT_WORKSPACE_ID) == []
    resolved = await service.resolve_for_task(
        db_session, ring, workspace_id=DEFAULT_WORKSPACE_ID, names=["API_TOKEN"]
    )
    assert resolved.values == {}
    assert not await service.delete_secret(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, name="API_TOKEN", actor_user_id=uid
    )


def test_declared_names_from_payload() -> None:
    from orcaster_server.task_secrets.service import declared_task_env

    assert declared_task_env({}) == []
    assert declared_task_env(None) == []
    assert declared_task_env({"task_env": None}) == []
    assert declared_task_env({"task_env": "API_TOKEN"}) == []
    assert declared_task_env({"task_env": ["B", "A", "B", 3]}) == ["A", "B"]


# --- Token prefix in the name -----------------------------------------------

TOKEN_NAMES = [
    "hf_x",
    "ghp_x",
    "gho_x",
    "ghs_x",
    "ghu_x",
    "ghr_x",
    "github_pat_x",
    "hf_token",
    "glpat-x",
    "xoxb-x",
    "xoxp-x",
    "sk-x",
]


@pytest.mark.parametrize("name", TOKEN_NAMES)
def test_secret_name_rejects_token_prefix_without_echo(name: str) -> None:
    """Same guard as `task_env`; the message does not repeat the name (it is the token)."""
    from orcaster_server.env_names import validate_secret_name

    with pytest.raises(ValueError, match="known token prefix") as exc:
        validate_secret_name(f"{name}SECRETMARK")
    assert "SECRETMARK" not in str(exc.value)


@pytest.mark.parametrize("name", ["HF_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "SK_X", "github_token"])
def test_secret_name_token_prefix_is_case_sensitive(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name

    assert validate_secret_name(name) == name


@pytest.mark.parametrize("name", ["hf_x", "ghp_x", "github_pat_x"])
def test_secret_name_pattern_still_accepts_token_prefix_for_delete(name: str) -> None:
    """Deletion only validates the shape: a row predating the prefix validation is deleted."""
    from orcaster_server.env_names import validate_secret_name_pattern

    assert validate_secret_name_pattern(name) == name


@pytest.mark.parametrize("name", ["SECRETMARK-x", "SECRETMARK=v", "1SECRETMARK"])
def test_secret_name_shape_error_does_not_echo(name: str) -> None:
    from orcaster_server.env_names import validate_secret_name, validate_secret_name_pattern

    for fn in (validate_secret_name, validate_secret_name_pattern):
        with pytest.raises(ValueError, match="invalid secret name") as exc:
            fn(name)
        assert "SECRETMARK" not in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tok\r\n", "tok"),
        ("tok\n", "tok"),
        ("tok", "tok"),
        ("a\r\nb\r\n", "a\nb"),
        ("a\r\nb\r\n\r\n", "a\nb\n"),
    ],
)
def test_normalize_secret_form_value(raw: str, expected: str) -> None:
    from orcaster_server.api.admin_ui import normalize_secret_form_value

    assert normalize_secret_form_value(raw) == expected
