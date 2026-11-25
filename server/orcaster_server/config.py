from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Schemes an origin can have, and the port a browser does NOT write in the
# `Origin` header because it is the scheme's default.
_ORIGIN_DEFAULT_PORTS = {"http": 80, "https": 443}


def _bad_origin(env_name: str, raw: str, reason: str) -> ValueError:
    """Startup error, with the received value and what needs fixing."""
    return ValueError(
        f"{env_name}={raw!r} is not a valid origin ({reason}). Expected "
        "`scheme://host[:port]`, scheme http or https, with no path, query, "
        "fragment or credentials, e.g. `https://orcaster.example.com`."
    )


def canonical_origin(value: str, *, env_name: str) -> str:
    """Canonical form of an origin, or `ValueError` with the reason.

    A browser sends the serialization of the origin in `Origin`: scheme and host in
    lowercase, without the port when it is the scheme's default, and without path,
    query or fragment. `orcaster_server/auth/origin.py::same_origin` compares that
    header for exact equality (and the `webauthn` library compares the origin of the
    clientDataJSON just as exactly), so a value with uppercase letters, with an
    explicit `:443` or with a path matches no real `Origin`: passkey login breaks
    and every cookie-authenticated POST/PUT/PATCH/DELETE answers 403. The value is
    canonicalized once, when `Settings` is built, and not on every comparison.

    It normalizes what is only a matter of how the value is written (uppercase,
    default port, trailing slash, whitespace) and REJECTS what cannot be fixed
    without guessing the operator's intent: path, query, fragment, userinfo or
    another scheme. Such a value never worked; the failure becomes loud and happens
    at startup, instead of a per-request 403 that does not name the cause.
    """
    raw = value.strip()
    # The trailing slash is the only part of the path that can be removed without
    # guessing: `https://host/` and `https://host` are the same origin.
    trimmed = raw.rstrip("/")
    if not trimmed:
        raise _bad_origin(env_name, raw, "empty")
    try:
        parts = urlsplit(trimmed)
        port = parts.port
    except ValueError as exc:  # non-numeric or out-of-range port
        raise _bad_origin(env_name, raw, str(exc)) from exc
    if parts.scheme not in _ORIGIN_DEFAULT_PORTS:
        raise _bad_origin(env_name, raw, f"scheme must be http or https, got {parts.scheme!r}")
    if parts.path:
        raise _bad_origin(env_name, raw, f"an origin has no path, got {parts.path!r}")
    if parts.query:
        raise _bad_origin(env_name, raw, "an origin has no query string")
    if parts.fragment:
        raise _bad_origin(env_name, raw, "an origin has no fragment")
    if parts.username is not None or parts.password is not None:
        raise _bad_origin(env_name, raw, "an origin carries no credentials")
    host = parts.hostname
    if not host:
        raise _bad_origin(env_name, raw, "no host")
    if port == 0:
        raise _bad_origin(env_name, raw, "port must be between 1 and 65535")
    if port == _ORIGIN_DEFAULT_PORTS[parts.scheme]:
        # A browser omits the default port, so writing it would not match.
        port = None
    # `hostname` returns an IPv6 address without brackets; in an origin they are required.
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    bind_host: str = Field(default="0.0.0.0", alias="BIND_HOST")
    bind_port: int = Field(default=8000, alias="BIND_PORT")
    default_claim_timeout_seconds: int = Field(default=3600, alias="DEFAULT_CLAIM_TIMEOUT_SECONDS")
    ed25519_private_key_path: str | None = Field(default=None, alias="ED25519_PRIVATE_KEY_PATH")
    # Task secrets per workspace. File with one Fernet key per line (the first is the
    # primary; empty lines and `#` lines are ignored). The path expands environment
    # variables, e.g. `$CREDENTIALS_DIRECTORY/task-secrets-keys` with
    # `LoadCredentialEncrypted=`.
    # Unset (or empty): the server starts with a WARNING, the management API
    # answers 503 and the poll delivers no secrets. Set but invalid: startup
    # fails.
    task_secrets_keys_path: str | None = Field(default=None, alias="TASK_SECRETS_KEYS_PATH")

    # Server plugins (`orcaster_server/plugins.py`): ordered, comma-separated list of
    # entry point names of the `orcaster.plugins` group. The listed ones take
    # precedence over the other installed entry points, in the order written. A
    # name that is not installed stops startup.
    plugins: str = Field(default="", alias="ORCASTER_PLUGINS")

    # WebAuthn (auth module). rp_id = domain without scheme or port.
    webauthn_rp_id: str = Field(default="localhost", alias="WEBAUTHN_RP_ID")
    webauthn_rp_name: str = Field(default="Orcaster", alias="WEBAUTHN_RP_NAME")
    # Validated and normalized when Settings is built: see `canonical_origin`. A
    # value with a path, query, fragment, userinfo or another scheme fails startup;
    # uppercase, default port and trailing slash are corrected.
    webauthn_origin: str = Field(default="http://localhost:8000", alias="WEBAUTHN_ORIGIN")
    # Public base URL used to build share links for the status page. Decoupled from
    # the WebAuthn origin: in production they are usually the same domain, but they
    # can differ (e.g. if the status page lives on its own subdomain).
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    # In-process cache of the public status page snapshot. Defence in depth against
    # a misconfigured edge: a valid hit saves the snapshot aggregation (the
    # per-variant count is a seq scan of tasks with no window). The request still
    # resolves the token against the DB: that stays outside the cache so that
    # revocation is immediate.
    # TTL aligned with the max-age=10 emitted by the /status handlers.
    # 0 = disabled (passthrough; the server tests set it to 0).
    status_snapshot_cache_ttl_s: float = Field(default=10.0, alias="STATUS_SNAPSHOT_CACHE_TTL_S")
    session_cookie_secure: bool = Field(default=False, alias="SESSION_COOKIE_SECURE")
    # `/openapi.json`, `/docs` and `/redoc` require a PAT or a session. true opens
    # them without auth; for local development only.
    api_docs_public: bool = Field(default=False, alias="API_DOCS_PUBLIC")

    # Per-client limit for `POST /auth/login/begin`, `register/begin` and the
    # anonymous `complete` endpoints, which use separate buckets with the same
    # values. In-memory token bucket, per process.
    # `AUTH_BEGIN_RATE_PER_MINUTE=0` disables it (only `challenge_max_pending` bounds it).
    auth_begin_rate_per_minute: int = Field(default=30, ge=0, alias="AUTH_BEGIN_RATE_PER_MINUTE")
    auth_begin_rate_burst: int = Field(default=10, ge=1, alias="AUTH_BEGIN_RATE_BURST")
    # Coarse level: bucket per network (IPv4 /24, IPv6 /48). 0 disables it.
    auth_begin_group_rate_per_minute: int = Field(
        default=300, ge=0, alias="AUTH_BEGIN_GROUP_RATE_PER_MINUTE"
    )
    auth_begin_group_burst: int = Field(default=100, ge=1, alias="AUTH_BEGIN_GROUP_BURST")

    # Housekeeping
    worker_heartbeat_timeout_seconds: int = Field(
        default=120, alias="WORKER_HEARTBEAT_TIMEOUT_SECONDS"
    )
    commands_retention_days: int = Field(default=7, alias="COMMANDS_RETENTION_DAYS")
    housekeeping_interval_seconds: int = Field(default=30, alias="HOUSEKEEPING_INTERVAL_SECONDS")
    housekeeping_enabled: bool = Field(default=True, alias="HOUSEKEEPING_ENABLED")

    # Startup grace: after startup, housekeeping neither expires leases nor marks
    # workers offline during this window, to give healthy workers time to poll
    # again after a server restart. Default 150 = lease (120) + one poll round of
    # margin. Per process (each uvicorn has its own).
    startup_grace_seconds: int = Field(default=150, alias="STARTUP_GRACE_SECONDS")

    # Migrations at startup: enabled by default. It can be disabled in environments
    # where the operator prefers to run `orcaster-migrate` manually as an explicit
    # pipeline step.
    migrate_on_startup: bool = Field(default=True, alias="MIGRATE_ON_STARTUP")

    # Dashboard SSE. Real-time push to the dashboard via Server-Sent Events backed by
    # Postgres LISTEN/NOTIFY. Default OFF: all the infrastructure starts inert until
    # this flag is enabled.
    sse_enabled: bool = Field(default=False, alias="SSE_ENABLED")
    # How often a keepalive comment (`: ping`) is emitted on the SSE stream, to avoid
    # the ~100s cutoff by Cloudflare (error 524).
    sse_heartbeat_interval_seconds: int = Field(default=30, alias="SSE_HEARTBEAT_INTERVAL_SECONDS")
    # Output coalescing window: at most one SSE notification per execution every N ms
    # (output is inserted line by line and would flood the stream).
    sse_output_coalesce_ms: int = Field(default=250, alias="SSE_OUTPUT_COALESCE_MS")

    # Body limit for each request. It is enforced by `orcaster_server.body_limit` in
    # the app, so it holds without the proxy; Caddy (06-caddy.sh) sets the same
    # floor. Exceeding it -> 413. The default comfortably covers the largest payload
    # a human writes (create task, command, control-op) and the 64 KiB of a secret.
    # Routes with their own limit (`/output`, `/artifacts`) replace it.
    max_request_body_bytes: int = Field(
        default=4 * 1024 * 1024, ge=1, alias="MAX_REQUEST_BODY_BYTES"
    )
    # `POST /v1/tasks/{id}/output`: the worker bounds the TEXT of a batch to 256 KiB
    # but does not count the JSON envelope (~83 B per event: seq, ts, stream) nor the
    # number of events, so the worst legal batch is 262144 events of 1 byte:
    # ~23.25 MiB measured. 32 MiB leaves margin for deployed workers; on a 413 the
    # worker retries the same batch endlessly.
    max_output_body_bytes: int = Field(
        default=32 * 1024 * 1024, ge=1, alias="MAX_OUTPUT_BODY_BYTES"
    )
    # Multipart overhead (boundaries and headers of up to `artifact_max_count` parts)
    # added to the maximum file bytes in the artifact upload. `ge=1` and not `ge=0`:
    # multipart framing always adds bytes to the body, so with 0 a file of the
    # maximum size would get a 413. It is the same requirement that
    # setup-scripts/debian13 (require_positive_int) enforces for the Caddyfile.
    artifact_upload_overhead_bytes: int = Field(
        default=8 * 1024 * 1024, ge=1, alias="ARTIFACT_UPLOAD_OVERHEAD_BYTES"
    )

    # Artifacts produced by the task. Bytes on the filesystem, metadata in Postgres
    # (task_artifacts table). The default lives OUTSIDE /opt/<repo> to survive the
    # deploy's `rsync --delete` (same criterion as static/robots.txt). The operator
    # can point it at a dedicated volume via ARTIFACTS_ROOT.
    artifacts_root: Path = Field(
        default=Path("/var/lib/orcaster-artifacts"), alias="ARTIFACTS_ROOT"
    )
    # Limits (configurable). Exceeding any of them -> 413 on the upload endpoint.
    artifact_max_file_bytes: int = Field(default=50 * 1024 * 1024, alias="ARTIFACT_MAX_FILE_BYTES")
    artifact_max_total_bytes: int = Field(
        default=200 * 1024 * 1024, alias="ARTIFACT_MAX_TOTAL_BYTES"
    )
    artifact_max_count: int = Field(default=500, alias="ARTIFACT_MAX_COUNT")
    # Disk protection. AGGREGATE logical quota per workspace: the sum of size_bytes
    # of all its artifacts. Different from ARTIFACT_MAX_TOTAL_BYTES (which is
    # per execution). Exceeding it -> 507.
    # This is the DEFAULT: a workspace with a non-NULL `workspaces.artifact_quota_bytes`
    # uses its override (set only by the host via
    # orcaster_server/cli/workspace_quota.py).
    artifact_workspace_max_total_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,  # 10 GiB
        alias="ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES",
    )
    # Floor of PHYSICAL free space on the ARTIFACTS_ROOT filesystem. Global safety
    # net: if the sum of per-workspace quotas exceeds the disk, statvfs cuts in
    # before a real ENOSPC. Dropping below this floor -> 507 / poll paused for ALL
    # workspaces (it is shared). Housekeeping logs a WARNING if what the workspaces
    # can still grow, sum of max(effective quota - usage, 0), exceeds the filesystem's
    # free space - this floor.
    artifact_disk_min_free_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,  # 2 GiB
        alias="ARTIFACT_DISK_MIN_FREE_BYTES",
    )
    # Server-side disk gate. CENTRAL threshold of free-disk fraction below which the
    # server stops delivering tasks to a worker (`storage_paused` pattern), excludes
    # it from the open-claim fairness-yield and skips it in the `all_available`
    # snapshot. The worker reports its `free_disk_fraction` in the poll and the
    # SERVER decides the threshold (not the worker): a single place to adjust for
    # the whole fleet. `0.0` DISABLES the gate entirely (no worker is gated,
    # whatever fraction it reports). A NULL fraction (an old worker that does not
    # send the field, or an unmeasurable volume) never gates: fail-open, which
    # preserves the previous behaviour. Default 0.20, aligned with the worker GC
    # floor (`cache_min_free_fraction`), which is where the GC declares the pressure
    # unrecoverable.
    # ge/le bound it to [0,1] and reject NaN/inf at startup: a malformed value
    # (e.g. `nan`) would make Python disable the gate (`nan > 0.0` is False) while
    # Postgres excludes EVERY reporting worker (for a finite frac, `frac < NaN` is
    # TRUE: NaN sorts at the top). It fails loudly when Settings is built instead of
    # degrading silently. 0.0 = gate disabled.
    worker_claim_min_free_fraction: float = Field(
        default=0.20, ge=0.0, le=1.0, alias="WORKER_CLAIM_MIN_FREE_FRACTION"
    )
    # TTL of the in-process cache of free disk space. The poll measures the disk
    # floor on every request from every worker; statvfs is a blocking syscall and
    # the value does not change significantly between polls (default interval 30s)
    # against a 2 GiB floor. `0` disables the cache (it always measures; this is
    # what the server tests set).
    artifact_disk_free_cache_ttl_s: float = Field(
        default=5.0, alias="ARTIFACT_DISK_FREE_CACHE_TTL_S"
    )
    # How many housekeeping passes between reconciliations of the artifact
    # counter. 10 passes ~ 5 min with the default 30s interval.
    artifact_usage_reconcile_passes: int = Field(
        default=10, alias="ARTIFACT_USAGE_RECONCILE_PASSES"
    )
    # Name suffix of the artifacts counted by the header indicator (for
    # example ".report.json"). Empty (default) => indicator off: the pill is
    # not rendered.
    artifact_indicator_suffix: str = Field(default="", alias="ARTIFACT_INDICATOR_SUFFIX")
    # Path to a sync_status.json written by an external sync process. The
    # contract is versioned by schema_version. The app reads uploaded_total
    # from it for the indicator. Empty => only the artifact count is shown.
    artifact_indicator_sync_status_path: str = Field(
        default="", alias="ARTIFACT_INDICATOR_SYNC_STATUS_PATH"
    )

    @field_validator("webauthn_origin", mode="after")
    @classmethod
    def _canonical_webauthn_origin(cls, value: str) -> str:
        """Fail startup if `WEBAUTHN_ORIGIN` is not a usable origin."""
        return canonical_origin(value, env_name="WEBAUTHN_ORIGIN")

    @model_validator(mode="after")
    def _artifact_upload_body_limit_is_positive(self) -> "Settings":
        """Fail at startup if the artifact upload limit is not positive.

        It is the only body limit derived from three variables; the overhead already
        requires `ge=1` on its field, but the TOTAL and FILE maximum can be <= 0 and
        drag the sum down. With a result <= 0 the middleware would answer 413 to
        every upload: explicit at startup, never a silent fallback.
        """
        limit = self.max_artifact_upload_body_bytes
        if limit <= 0:
            raise ValueError(
                "effective artifact upload body limit must be positive, got "
                f"{limit}: max(ARTIFACT_MAX_TOTAL_BYTES={self.artifact_max_total_bytes}, "
                f"ARTIFACT_MAX_FILE_BYTES={self.artifact_max_file_bytes}) + "
                f"ARTIFACT_UPLOAD_OVERHEAD_BYTES={self.artifact_upload_overhead_bytes}"
            )
        return self

    @property
    def max_artifact_upload_body_bytes(self) -> int:
        """Body limit for `POST /v1/tasks/{id}/artifacts`.

        The most the route accepts in one request: the file bytes do not exceed
        `artifact_max_total_bytes` per execution (and no single file exceeds
        `artifact_max_file_bytes`), plus the multipart overhead.
        """
        return (
            max(self.artifact_max_total_bytes, self.artifact_max_file_bytes)
            + self.artifact_upload_overhead_bytes
        )


settings = Settings()
