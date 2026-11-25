"""Common fixtures.

`postgres_url`: `postgresql+asyncpg://...` URL ready for integration tests.
Strategy:

1. If `TEST_DATABASE_URL` is set, use it as is (CI or an environment with a DB
   already provided).
2. Otherwise, try to start an ephemeral Postgres container with `podman` (or
   `docker` as a fallback). The container is destroyed when the session ends.
3. If no container runtime is available and `TEST_DATABASE_URL` is empty, the
   tests that depend on `postgres_url` are explicitly skipped.

`app_client`: httpx AsyncClient mounted on the FastAPI app with the DB already
migrated and reset between tests.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from orcaster_server import plugins
from orcaster_server.plugins import OutcomeRule, PluginRegistry
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID

# --- example plugin for the tests ----------------------------------------------
# The core ships no extractors or outcome profile. Tests that need a recognized
# variant or a default exit-code classification request the `example_plugin`
# fixture, which installs this provider as the only plugin of the active registry.
# The rest run with an empty registry (autouse fixture `_empty_plugin_registry`),
# which is the core's behavior without plugins.

EXAMPLE_KIND = "example-tool"
EXAMPLE_PLUGIN_NAME = "example"
_RE_EXAMPLE_PIP = re.compile(r"example[_-]tool==(\d+\.\d+\.\d+)")

# Test outcome profile: one exit that closes `completed` and four that close
# `failed` with a name. The codes mimic a real tool contract (3 to 7).
EXAMPLE_OUTCOMES = {
    3: OutcomeRule(name="empty_result", closes_as="completed"),
    4: OutcomeRule(name="invalid_input", closes_as="failed"),
    5: OutcomeRule(name="access_denied", closes_as="failed"),
    6: OutcomeRule(name="unsupported_input", closes_as="failed"),
    7: OutcomeRule(name="not_reachable", closes_as="failed"),
}

# Status alias in the test profile: `status=completed_empty` is equivalent to
# `status=completed` + `outcome=empty_result`.
EXAMPLE_LEGACY_STATUS = "completed_empty"
EXAMPLE_LEGACY_OUTCOME = "empty_result"


def example_extract(payload: dict) -> str | None:
    """Test extractor: the version of the pip spec `example-tool==X.Y.Z` in `commands`."""
    commands = payload.get("commands")
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        return None
    m = _RE_EXAMPLE_PIP.search("\n".join(commands))
    return m.group(1) if m else None


def example_provider(registry: PluginRegistry) -> None:
    """Test provider: `example-tool` extractor, its outcome profile and an alias."""
    registry.add_variant_extractor(EXAMPLE_KIND, example_extract)
    registry.add_outcome_defaults(EXAMPLE_KIND, EXAMPLE_OUTCOMES)
    registry.add_legacy_status_alias(EXAMPLE_LEGACY_STATUS, EXAMPLE_LEGACY_OUTCOME)


def example_payload(*extra_commands: str, version: str = "1.2.3") -> dict:
    """Payload that the test extractor recognizes as `example-tool` with label `version`.

    With the `example_plugin` fixture active, an exit 3 to 7 of this task without a
    `payload.outcomes` block is classified with `EXAMPLE_OUTCOMES`. Without the
    fixture, the task is `other` and those exits close as a plain `failed`.
    """
    return {"commands": [f"uv pip install example-tool=={version}", *extra_commands]}


class FakeEntryPoint:
    """In-memory entry point for `load_registry(entry_points=[...])`."""

    def __init__(self, name: str, obj: object) -> None:
        self.name = name
        self.value = f"tests:{name}"
        self._obj = obj

    def load(self) -> object:
        if isinstance(self._obj, BaseException):
            raise self._obj
        return self._obj


def install_registry(*providers: tuple[str, object]) -> plugins.Registry:
    """Install the given `(name, provider)` providers as the active registry."""
    registry = plugins.load_registry(
        entry_points=[FakeEntryPoint(name, obj) for name, obj in providers]
    )
    plugins._active = registry
    return registry


@pytest.fixture(autouse=True)
def _writable_artifacts_root(tmp_path_factory, monkeypatch) -> None:
    """Point ARTIFACTS_ROOT at a writable temporary directory.

    The lifespan refuses to start without a writable artifacts root, and the
    default (/var/lib/orcaster-artifacts) does not exist on a development
    machine. Tests that need another root set it themselves.
    """
    from orcaster_server.config import settings as _settings

    monkeypatch.setattr(_settings, "artifacts_root", tmp_path_factory.mktemp("artifacts"))


@pytest.fixture(autouse=True)
def _empty_plugin_registry() -> Iterator[None]:
    """Each test starts with an empty registry: the core without plugins.

    Hermetic: it does not depend on which entry points are installed in the
    environment.
    """
    install_registry()
    yield
    plugins.reset_registry()


@pytest.fixture
def example_plugin() -> plugins.Registry:
    """Install the `example` test plugin as the only plugin of the active registry."""
    return install_registry((EXAMPLE_PLUGIN_NAME, example_provider))


_PG_IMAGE = "docker.io/library/postgres:17-alpine"
_PG_PASSWORD = "tasktest"
_PG_DB = "tasktest"
_PG_USER = "tasktest"


def _find_runtime() -> str | None:
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_pg(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                # TCP accept != Postgres ready; the caller adds a margin for auth.
                time.sleep(0.5)
                return True
        except OSError:
            time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    env_url = os.environ.get("TEST_DATABASE_URL")
    if env_url:
        yield env_url
        return

    runtime = _find_runtime()
    if runtime is None:
        pytest.skip(
            "Neither podman nor docker is available; "
            "set TEST_DATABASE_URL to run integration tests."
        )

    port = _free_port()
    name = f"orcaster-pg-{uuid.uuid4().hex[:8]}"

    try:
        subprocess.run(
            [
                runtime,
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-e",
                f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                "-e",
                f"POSTGRES_USER={_PG_USER}",
                "-e",
                f"POSTGRES_DB={_PG_DB}",
                "-p",
                f"{port}:5432",
                _PG_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"Could not start Postgres with {runtime}: {exc.stderr.decode()}")

    try:
        if not _wait_for_pg("127.0.0.1", port):
            pytest.fail(f"Postgres was not ready within the timeout on port {port}")
        # Small extra margin so postgres accepts authenticated connections.
        time.sleep(2.0)
        yield f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@127.0.0.1:{port}/{_PG_DB}"
    finally:
        subprocess.run([runtime, "rm", "-f", name], capture_output=True)


SERVER_DIR = Path(__file__).resolve().parent.parent
# `workspaces` is NOT truncated directly, but `TRUNCATE ... CASCADE` on
# `users` empties it anyway (TRUNCATE CASCADE ignores ON DELETE SET NULL and
# drags along every table with an FK to the truncated one). The `Default`
# workspace seeded by migration 0021 is reference data: the workers/
# tasks.workspace_id columns have a DEFAULT pointing to its id, so that row is
# re-seeded after every TRUNCATE (see `_truncate_and_seed`).
TABLES = (
    "workers",
    "tasks",
    "task_executions",
    "admin_actions",
    "webauthn_challenges",
    "sessions",
    "passkey_credentials",
    "invitations",
    "users",
    "workspace_members",
    "worker_enrollment_tokens",
    "personal_access_tokens",
    "user_active_workspace",
    # Migration 0056: task secrets per workspace.
    "task_secrets",
)

# Deterministic UUID of the `Default` workspace. Single source: imported from
# `orcaster_server.workspaces.constants`; it must match the literal frozen in
# orcaster_server/migrations/versions/0021_workspaces.py::DEFAULT_WORKSPACE_ID.

# TRUNCATE includes `workspaces` so that `RESTART IDENTITY CASCADE` resolves the
# FK order without error; then the deterministic Default row is re-seeded.
_TRUNCATE_SQL = "TRUNCATE " + ", ".join((*TABLES, "workspaces")) + " RESTART IDENTITY CASCADE"
_SEED_DEFAULT_WORKSPACE_SQL = (
    "INSERT INTO workspaces (id, name, slug) "
    "VALUES (:id, 'Default', 'default') "
    "ON CONFLICT (id) DO NOTHING"
)


async def _truncate_and_seed(conn) -> None:
    """Empty all test tables and re-seed the `Default` workspace."""
    await conn.execute(text(_TRUNCATE_SQL))
    await conn.execute(
        text(_SEED_DEFAULT_WORKSPACE_SQL),
        {"id": DEFAULT_WORKSPACE_ID},
    )


def _alembic(args: list[str], url: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest_asyncio.fixture(scope="session")
async def migrated_db_url(postgres_url: str) -> AsyncIterator[str]:
    """DB with `alembic upgrade head` applied.

    Idempotent: if already at head, alembic is a no-op. The teardown runs a
    downgrade to return the DB to its base state in case other tests assume a
    clean slate.
    """
    up = _alembic(["upgrade", "head"], postgres_url)
    if up.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{up.stdout}\n{up.stderr}")
    try:
        yield postgres_url
    finally:
        _alembic(["downgrade", "base"], postgres_url)


@pytest_asyncio.fixture
async def clean_db_for_downgrade(postgres_url: str) -> None:
    """Empty the test tables before a test that runs `alembic downgrade`.

    `migrated_db_url` is session-scoped and does not truncate, so a migration
    test inherits the rows left by the previous one. The 0048 and 0056
    downgrades restore narrower CHECKs on `admin_actions` (without `target_kind`
    'share_token' or 'task_secret') and fail with `check_violation` if audited
    rows with those values remain. This is intentional in the migrations (they
    do not silently discard audit rows), so the isolation lives here. All test
    tables are emptied, not only `admin_actions`, so that no other CHECK
    restored by a downgrade depends on test order.

    It does not change the DB revision: at head it truncates and re-seeds
    `Default` like `app_client`; at base (tests of `test_migrations.py`) there is
    nothing to empty; with a half-applied schema (a previous test failed midway
    through the chain) it truncates the tables that exist, without seeding.
    """
    candidates = [*TABLES, "workspaces"]
    engine = create_async_engine(postgres_url)
    try:
        async with engine.begin() as conn:
            existing = set(
                (
                    await conn.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname='public' AND tablename = ANY(:t)"
                        ),
                        {"t": candidates},
                    )
                ).scalars()
            )
            if existing == set(candidates):
                await _truncate_and_seed(conn)
            elif existing:
                present = ", ".join(t for t in candidates if t in existing)
                await conn.execute(text(f"TRUNCATE {present} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def _session_ed25519_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session Ed25519 key (universal task signing).

    With universal signing, every task created via `/admin/tasks` is signed at
    INSERT and via `/v1/workers/{id}/poll` it is re-signed at claim. If no key
    is loaded, both endpoints answer 503, so the tests that use `app_client`
    need a key available. One is generated at session start and loaded through
    the `app_client` fixture (which monkeypatches
    `settings.ed25519_private_key_path`).
    """
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _Priv,
    )

    priv = _Priv.generate()
    pem = priv.private_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PrivateFormat.PKCS8,
        encryption_algorithm=_ser.NoEncryption(),
    )
    p = tmp_path_factory.mktemp("ed25519_conftest") / "server.pem"
    p.write_bytes(pem)
    return p


@pytest_asyncio.fixture
async def app_client(
    migrated_db_url: str,
    _session_ed25519_key: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """httpx client on the FastAPI app with a migrated and reset DB.

    TRUNCATE between tests guarantees isolation without recreating the schema
    each time.

    Universal signing: sets `ED25519_PRIVATE_KEY_PATH` to the session key so that
    `/admin/tasks` and `/v1/workers/{id}/poll` can sign (without a key they would
    return 503). Specific tests can use `monkeypatch.setattr` to force another
    configuration (e.g. `test_admin_tasks_no_key_returns_503`).
    """
    # If a previous test left the DB downgraded (e.g. test_migrations), re-apply
    # head before truncating. `alembic upgrade head` is idempotent.
    truncate_engine = create_async_engine(migrated_db_url)
    try:
        async with truncate_engine.connect() as conn:
            tables_exist = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_tables "
                        "WHERE schemaname='public' AND tablename = ANY(:t)"
                    ),
                    {"t": list(TABLES)},
                )
            ).scalar_one()
        if tables_exist != len(TABLES):
            up = _alembic(["upgrade", "head"], migrated_db_url)
            if up.returncode != 0:
                pytest.fail(f"alembic upgrade head (rescue) failed:\n{up.stdout}\n{up.stderr}")
        async with truncate_engine.begin() as conn:
            await _truncate_and_seed(conn)
    finally:
        await truncate_engine.dispose()

    # Import after setting the env so orcaster_server/config picks up the URL if it reloads.
    from orcaster_server import db as app_db
    from orcaster_server import signing as app_signing
    from orcaster_server import storage as app_storage
    from orcaster_server.auth.service import passkey as passkey_service
    from orcaster_server.config import settings
    from orcaster_server.main import _auth, app
    from orcaster_server.pats import service as pats_service
    from orcaster_server.status.cache import snapshot_cache

    settings.database_url = migrated_db_url
    # Universal signing: a loadable Ed25519 key so admin_create_task and poll can
    # sign. monkeypatch is used so it is reverted between tests (some specific
    # tests override it with their own keys).
    monkeypatch.setattr(settings, "ed25519_private_key_path", str(_session_ed25519_key))
    # Public snapshot cache inert by default: tests seed data and query
    # immediately; a stale snapshot would break assertions. The cache tests
    # enable the TTL with their own monkeypatch. clear() avoids entries leaking
    # between tests that do enable it.
    monkeypatch.setattr(settings, "status_snapshot_cache_ttl_s", 0.0)
    snapshot_cache.clear()
    # The `begin` limit is process state; without this, tests that perform
    # several `login/begin` calls would contaminate each other. The same goes
    # for the bounded WARNING on challenge-cap evictions: its window and its
    # pending count must not carry over from one test to the next. Anonymous
    # `complete` has its own limiter: both are reset.
    _auth.begin_limiter.reset()
    _auth.complete_limiter.reset()
    passkey_service._evictions_warning.reset()
    # Same for the bounded WARNING about a disabled user's PAT.
    pats_service._disabled_warning.reset()
    pats_service._legacy_prefix_warning.reset()
    # Same rationale for the free-disk-space cache. Several tests monkeypatch
    # `storage.disk_free_bytes` and expect the value to take effect in the same
    # test's poll; with a live TTL they would see the previous value.
    monkeypatch.setattr(settings, "artifact_disk_free_cache_ttl_s", 0.0)
    app_storage.disk_free_cache.clear()
    app_signing.reset_key_cache()
    app_db.init_engine()
    try:
        transport = ASGITransport(app=app)
        # Default `HX-Request: true` so /admin/workers and /admin/tasks return the
        # partial (not the 303 redirect to the dashboard). The UI tests are
        # written against the partial's contract; human navigations are covered
        # by a test dedicated to the redirect.
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"HX-Request": "true"},
            event_hooks={"request": [browser_origin_hook(settings.webauthn_origin)]},
        ) as client:
            yield client
    finally:
        await app_db.dispose_engine()


NO_ORIGIN_HEADER = "X-Test-No-Origin"


def browser_origin_hook(origin: str):
    """Request hook that mimics a browser.

    `require_session` and `AuthOriginCheckMiddleware` require `Origin` (or
    `Sec-Fetch-Site`) on POSTs authenticated by session cookie. A browser always
    sends one of the two, so the test client does too: it adds `Origin` unless
    the request already carries `Origin` or `Sec-Fetch-Site`. With
    `X-Test-No-Origin` it is omitted on purpose (to test the rejection with no
    header at all) and the control header is removed.
    """

    async def hook(request) -> None:
        if request.headers.pop(NO_ORIGIN_HEADER, None) is not None:
            return
        if "origin" not in request.headers and "sec-fetch-site" not in request.headers:
            request.headers["Origin"] = origin

    return hook


async def _mint_session_for_workspace(
    workspace_id: str = DEFAULT_WORKSPACE_ID, *, role: str = "owner"
) -> str:
    """Create a User + `role` membership of the given workspace and mint a session.

    Reuses the app's live engine (`app_client` already called `init_engine`).
    Returns the plaintext session token, ready to set in the `"session"` cookie.
    The `Default` workspace already exists (re-seeded by the fixture).
    """
    from orcaster_server import db as app_db
    from orcaster_server.auth.config import AuthConfig
    from orcaster_server.auth.service.session import create_session
    from orcaster_server.workspaces.service import add_member

    cfg = AuthConfig(rp_id="test", rp_name="Test", origin="http://test", cookie_name="session")
    sessionmaker = app_db.get_sessionmaker()
    if sessionmaker is None:  # pragma: no cover - defensive
        raise RuntimeError("app engine not initialized; is the app_client fixture missing?")
    async with sessionmaker() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name) VALUES ('Admin') RETURNING id")
        )
        user_id = str(row.scalar_one())
        await add_member(s, workspace_id=workspace_id, user_id=user_id, role=role)
        token = await create_session(s, cfg, user_id=user_id, user_agent=None, ip=None)
        await s.commit()
        return token


@pytest_asyncio.fixture
async def authed_admin_client(app_client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """`app_client` with a valid session of an `owner` of the `Default` workspace.

    Inserts a User, makes it `owner` of the Default workspace, mints a session
    and sets the `"session"` cookie. All workers/tasks in the tests live in
    Default, so the authenticated admin sees and mutates them without
    restriction; it only adds the auth layer, without changing the semantics.

    Exposes `.workspace_id` (str) with the UUID of the seeded workspace so SSE
    tests can publish on the correct channel.
    """
    token = await _mint_session_for_workspace(DEFAULT_WORKSPACE_ID)
    # No `domain=`: with base_url http://test (single-label host) the httpx cookie
    # jar does not match `domain="test"` and does not send the cookie. Without a
    # domain, httpx ties it to the request host and sends it on every /admin/*.
    app_client.cookies.set("session", token)
    # Expose the workspace_id directly on the client for SSE tests.
    app_client.workspace_id = DEFAULT_WORKSPACE_ID  # type: ignore[attr-defined]
    yield app_client


async def mint_pat_via_app(
    *,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    role: str = "owner",
    add_to_workspace: bool = True,
    display_name: str = "Pat User",
    email: str | None = "pat-user@example.com",
    label: str | None = "cli-laptop",
    expires_at=None,
) -> tuple[str, str]:
    """Create a User (optionally a workspace member) and mint a PAT for it.

    Returns `(plaintext_token, user_id)`. Meant for public `/api/v1` API tests:
    it reuses the app's live engine (`app_client` already called `init_engine`).
    With `add_to_workspace=False` the owner is not a member, so `resolve_pat`
    returns NOT_MEMBER (403).
    """
    from orcaster_server import db as app_db
    from orcaster_server.pats.service import mint_pat
    from orcaster_server.workspaces.service import add_member

    sm = app_db.get_sessionmaker()
    if sm is None:  # pragma: no cover - defensive
        raise RuntimeError("app engine not initialized; is the app_client fixture missing?")
    async with sm() as s:
        row = await s.execute(
            text("INSERT INTO users (display_name, email) VALUES (:n, :e) RETURNING id"),
            {"n": display_name, "e": email},
        )
        user_id = str(row.scalar_one())
        if add_to_workspace:
            await add_member(s, workspace_id=workspace_id, user_id=user_id, role=role)
        token = await mint_pat(
            s,
            user_id=user_id,
            workspace_id=workspace_id,
            label=label,
            expires_at=expires_at,
        )
        await s.commit()
        return token, user_id


async def mint_token_via_app(workspace_id: str = DEFAULT_WORKSPACE_ID) -> str:
    """Mint an enrollment token using the app's live engine.

    Meant for the tests' `_register` helpers: `app_client` already called
    `orcaster_server.db.init_engine()`, so its sessionmaker is reused. Each call
    inserts a fresh row in `worker_enrollment_tokens` (single-use) and returns
    the plaintext token, ready for the body of `POST /v1/workers/register`.
    """
    from orcaster_server import db as app_db
    from orcaster_server.workspaces.enrollment import mint_enrollment_token as _mint_service

    sessionmaker = app_db.get_sessionmaker()
    if sessionmaker is None:  # pragma: no cover - defensive
        raise RuntimeError("app engine not initialized; is the app_client fixture missing?")
    async with sessionmaker() as s:
        token = await _mint_service(s, workspace_id=workspace_id)
        await s.commit()
        return token


@pytest_asyncio.fixture
async def db_session(migrated_db_url: str) -> AsyncIterator:
    """AsyncSession against the migrated DB, with a prior TRUNCATE for isolation.

    For service tests that do not need HTTP. Commit is explicit, left to the
    test; the rollback when the context closes covers failures.

    Like `app_client`: if a previous test left the DB downgraded (e.g.
    `test_migrations`), it re-applies `head` before truncating; without this
    rescue the TRUNCATE would fail with "relation ... does not exist" depending
    on test order.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(migrated_db_url)
    try:
        async with engine.connect() as conn:
            tables_exist = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_tables "
                        "WHERE schemaname='public' AND tablename = ANY(:t)"
                    ),
                    {"t": list(TABLES)},
                )
            ).scalar_one()
        if tables_exist != len(TABLES):
            up = _alembic(["upgrade", "head"], migrated_db_url)
            if up.returncode != 0:
                pytest.fail(f"alembic upgrade head (rescue) failed:\n{up.stdout}\n{up.stderr}")
        async with engine.begin() as conn:
            await _truncate_and_seed(conn)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_task_secret_refusals() -> Iterator[None]:
    """In-memory state of secret refusals, clean per test.

    The dashboard registry (`blocked._REFUSALS`), the poll ERROR window
    (`poll._refusal_last_logged`) and the refusal memory (`poll._refusal_memory`)
    live in the process: without this, a refusal from one poll test shows up on
    the secrets page of another test that shares the default workspace.
    """
    from orcaster_server.api import poll
    from orcaster_server.task_secrets import blocked

    blocked._REFUSALS.clear()
    poll._refusal_last_logged.clear()
    poll._refusal_memory.clear()
    poll._refusal_mark_written.clear()
    yield
    blocked._REFUSALS.clear()
    poll._refusal_last_logged.clear()
    poll._refusal_memory.clear()
    poll._refusal_mark_written.clear()
