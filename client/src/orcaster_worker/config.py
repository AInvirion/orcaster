"""Client configuration.

Every variable is read from the environment (or an adjacent `.env`, via pydantic-settings).
The credentials (`worker_id`, `api_key`) are NOT loaded here: they are persisted in
`state.json` separately (see `orcaster_worker.state`). This avoids passing the API key
through the environment in production.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from orcaster_worker import package_version
from orcaster_worker.egress import parse_egress_key

# Default for the task-scoped scratch cache: the dedicated cache volume
# `/var/cache/orcaster`, NOT the data volume `<state_dir>/cache`
# (= /var/lib/orcaster/cache). Consistent with `uv_cache_dir`, which lives
# under `/var/cache/orcaster/uv`.
_DEFAULT_CACHE_ROOT = Path("/var/cache/orcaster")

# Capability name usable in an `eligibility` requirement. Copy of
# `CAPABILITY_NAME_PATTERN` in `server/orcaster_server/eligibility.py`: the client does not
# import server code. Validating here surfaces the error at worker startup, where the
# operator sees it, instead of in a server warning that ignores the value.
_CAPABILITY_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def parse_capabilities(raw: str) -> dict[str, Any] | None:
    """`ORCH_CAPABILITIES` -> flat map name -> boolean|number, or None.

    Empty/blank is None ("declare nothing": the server keeps what it has).
    `{}` is an explicit empty map, which does clear the stored ones. Any
    other shape raises `ValueError` naming the offending key.
    """
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ORCH_CAPABILITIES is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(
            "ORCH_CAPABILITIES must be a flat JSON object, for example "
            '{"model": true, "model_context": 8192}; got ' + type(value).__name__
        )
    for name, declared in value.items():
        if not _CAPABILITY_NAME_RE.fullmatch(name):
            raise ValueError(
                f"ORCH_CAPABILITIES: the name {name!r} is not usable in a requirement; "
                f"it must match {_CAPABILITY_NAME_RE.pattern}"
            )
        # `bool` is a subclass of `int`: both valid, but it is named first so
        # the rule reads the same as on the server.
        if isinstance(declared, (bool, int)):
            continue
        if isinstance(declared, float) and math.isfinite(declared):
            continue
        raise ValueError(
            f"ORCH_CAPABILITIES: {name!r} must be a boolean or a finite number, got {declared!r}"
        )
    return value


# Built-in list of variables that require strict isolation. Empty on
# purpose: the core names no workload or provider. Each deployment
# declares its own in `ORCH_ISOLATION_REQUIRED_ENV`, which is UNIONED with
# this list (it never replaces it). If a worker extension mechanism
# contributes names, they join the same union.
BUILTIN_ISOLATION_REQUIRED_ENV: tuple[str, ...] = ()

# Grammar of a variable name in the list. It is the same one the pre-swap
# gate in `ops/release` must apply so that shell and worker agree.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_LIST_SPLIT_RE = re.compile(r"[,\s]+")


def parse_isolation_required_env(raw: str) -> tuple[str, ...]:
    """`ORCH_ISOLATION_REQUIRED_ENV` -> tuple of names, without duplicates.

    Separators: commas and/or spaces; empty items are ignored, so `""` is the
    empty list. An item that is not a valid variable name raises
    `ValueError` naming it: a pattern (`*_TOKEN`) or a `NAME=value` pasted
    by mistake is not interpreted, it is rejected (fails closed).
    """
    names: list[str] = []
    for token in _ENV_LIST_SPLIT_RE.split(raw.strip()):
        if not token:
            continue
        if not _ENV_NAME_RE.fullmatch(token):
            # A `NAME=value` pasted by mistake may carry a secret: only the
            # part before the `=` is named, never the value.
            shown = token.split("=", 1)[0] + "=…" if "=" in token else token
            raise ValueError(
                f"ORCH_ISOLATION_REQUIRED_ENV: {shown!r} is not a valid variable name "
                f"(must match {_ENV_NAME_RE.pattern}; separate with commas or spaces)"
            )
        names.append(token)
    return tuple(dict.fromkeys(names))


def _default_state_dir() -> Path:
    # In the container, the Dockerfile sets ORCASTER_HOME=/var/lib/orcaster
    # (mounted as a named volume). Outside the container, use a directory
    # under $HOME for developers running the worker directly.
    env = os.environ.get("ORCASTER_HOME")
    if env:
        return Path(env)
    return Path.home() / ".orcaster-worker"


def _default_egress_allow_file() -> Path:
    # Same literal env var name (WITHOUT the pydantic-settings `ORCH_` prefix)
    # that the sidecar proxy (`egress_proxy.py`) reads; it runs outside
    # `Settings` and reads `os.environ` directly. See the comment on the
    # `egress_allow_file` field.
    return Path(os.environ.get("ORCASTER_EGRESS_ALLOW_FILE", "/var/lib/orcaster-egress/allow.txt"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="ORCH_",
        env_prefix_target="all",
        populate_by_name=True,
        # Several fields are credentials (`enrollment_token`, `egress_key`): without
        # this the `ValidationError` adds `input_value='<raw value>'` to its
        # text, and an uncaught traceback (e.g. `cli_entry` with
        # `--config-path`) leaves it in the container log.
        hide_input_in_errors=True,
    )

    # Server
    server_url: HttpUrl = Field(
        default=HttpUrl("http://localhost:8000"),
        description="Base URL of the server.",
        alias="BASE_URL",
    )

    # Local state
    state_dir: Path = Field(default_factory=_default_state_dir)

    # Descriptive worker name (optional, to tell several instances on the
    # same host apart).
    worker_name: str = Field(default="", alias="WORKER_NAME")

    # Enrollment token (multi-tenancy). The server requires a single-use token,
    # scoped to a workspace, on `POST /v1/workers/register`.
    # It is only used on the first start (self-registration); once the
    # credentials are persisted in `state.json` the worker reuses the identity and
    # does not register again, so the token may be empty on later starts. A workspace
    # *owner* issues it out of band. Empty => the worker fails fast at boot
    # if it has to register (see `main.py::ensure_credentials`).
    enrollment_token: str = Field(default="", alias="ENROLLMENT_TOKEN")

    # Unified `/poll` loop.
    # `next_poll_after_seconds` comes from the server; the client clamps it to this
    # range so it does not get stuck if the server suggests something absurd.
    poll_min_interval_seconds: float = Field(
        default=1.0,
        ge=0.1,
        alias="POLL_MIN_SECONDS",
        description="Minimum interval between polls (lower clamp).",
    )
    poll_max_interval_seconds: float = Field(
        default=30.0,
        ge=1.0,
        alias="POLL_MAX_SECONDS",
        description="Maximum interval between polls (upper clamp + exhausted backoff).",
    )

    # Claim: the lease the client requests from the server.
    lease_seconds: int = Field(default=120, ge=1, le=3600, alias="LEASE_SECONDS")

    # Cancellation: time between SIGTERM and SIGKILL to the subprocess
    # process group on POSIX. On Windows it is best-effort
    # (terminate without escalation).
    cancel_grace_seconds: float = Field(
        default=5.0,
        ge=0.0,
        alias="CANCEL_GRACE_SECONDS",
    )

    # HTTP timeouts (httpx)
    http_connect_timeout: float = 5.0
    http_read_timeout: float = 30.0
    artifact_upload_timeout: float = Field(default=60.0, alias="ARTIFACT_UPLOAD_TIMEOUT")

    # Reconnection / network retries.
    network_max_attempts: int = Field(default=5, ge=1, alias="NETWORK_MAX_ATTEMPTS")
    network_base_delay_seconds: float = Field(default=0.5, ge=0.0)
    network_max_delay_seconds: float = Field(default=30.0, ge=0.0)

    # Worker identity for register
    hostname: str | None = None
    worker_os: str | None = Field(default=None, alias="WORKER_OS")
    arch: str | None = None
    # Single source in `orcaster_worker.package_version`. `ORCH_VERSION`
    # remains an explicit operator override.
    version: str = Field(default_factory=package_version)

    @field_validator("version", mode="before")
    @classmethod
    def _version_no_vacia(cls, v: object) -> object:
        """An empty `ORCH_VERSION=` falls back to the package value.

        A stray line in `.env` is a plausible typo, and the worker does
        know its version: letting the empty override win would make register
        stamp `""` in the dashboard column. The poll already ignores an empty
        value, but `register` runs only once per identity, so
        through that path the column would stay blank forever.
        """
        if isinstance(v, str) and not v.strip():
            return package_version()
        return v.strip() if isinstance(v, str) else v

    # Declared capabilities: flat JSON in `ORCH_CAPABILITIES`, e.g.
    # `{"model": true, "model_context": 8192}`. They travel in register and in every
    # poll, so changing them requires restarting the worker, not re-enrolling it. The
    # raw string is stored and parsed with `parse_capabilities`: a `dict`
    # field would make pydantic-settings decode the JSON before the
    # validator, and an empty `ORCH_CAPABILITIES=` would blow up with an opaque error.
    capabilities_json: str = Field(default="", alias="CAPABILITIES")

    @field_validator("capabilities_json")
    @classmethod
    def _capabilities_validas(cls, v: str) -> str:
        """Fail at startup: the server poll ignores an invalid value."""
        parse_capabilities(v)
        return v

    @property
    def capabilities(self) -> dict[str, Any] | None:
        """None = declare nothing (the server keeps the stored value)."""
        return parse_capabilities(self.capabilities_json)

    # Push commands.
    run_batch_per_command_timeout_seconds: int = Field(
        default=60,
        ge=1,
        alias="RUN_BATCH_PER_COMMAND_TIMEOUT_SECONDS",
        description=(
            "Independent timeout window for each command in run_batch. "
            "Each command gets this full window; a timeout in cmd N "
            "does not reduce the window of cmd N+1. "
            "Override with env RUN_BATCH_PER_COMMAND_TIMEOUT_SECONDS."
        ),
    )

    # Output streaming. The worker streams stdout/stderr to the
    # server via POST /v1/tasks/{id}/output while the task runs, instead of
    # truncating to 16 KiB and sending everything at the end.
    output_flush_max_bytes: int = Field(
        default=8 * 1024,
        ge=1,
        alias="OUTPUT_FLUSH_MAX_BYTES",
        description="Flush the output buffer when a stream exceeds this size.",
    )
    output_flush_interval_seconds: float = Field(
        default=2.0,
        gt=0.0,
        alias="OUTPUT_FLUSH_INTERVAL_SECONDS",
        description="Periodic buffer flush even if the size is not reached.",
    )
    output_buffer_max_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1,
        alias="OUTPUT_BUFFER_MAX_BYTES",
        description=(
            "RAM threshold for the backlog of events awaiting acknowledgement by "
            "the server (reliable streaming). Once exceeded, new events "
            "are spilled to a local .jsonl file and the effective limit "
            "becomes the worker disk."
        ),
    )
    # Artifacts: size cap per request of the multipart upload batch.
    # The worker groups files up to this size; a file that alone
    # exceeds it goes in its own request. The deployment proxy must allow at
    # least this size (`client_max_body_size`).
    artifact_request_max_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1,
        alias="ARTIFACT_REQUEST_MAX_BYTES",
        description="Byte cap per request of the artifact upload batch.",
    )

    # Ed25519: the server public key used to verify the signature of EVERY task
    # (universal signature). The default points to the conventional mount
    # `/var/lib/orcaster/ed25519.pub` that the root `Makefile` mounts from
    # `client/server-ed25519.pub`. The type allows `None` only for tests
    # that exercise the "pubkey missing" case; in production `run()` fails
    # fast at boot if the file does not load (see `main.py::run`).
    ed25519_public_key_path: Path | None = Field(
        default=Path("/var/lib/orcaster/ed25519.pub"),
        alias="ED25519_PUBLIC_KEY_PATH",
    )

    # Ephemeral per-execution venv with uv.
    # `uv_bin` is a path or PATH name of the `uv` binary. Default "uv"
    # (PATH lookup); override in deployments where uv has an absolute path.
    uv_bin: str = Field(default="uv", alias="ORCASTER_UV_BIN")
    # `uv_cache_dir` is exported as `UV_CACHE_DIR` when invoking uv. Self-contained
    # worker: default `None` => the worker uses a PER-JOB uv cache
    # (`<cache_dir>/uv`), cleaned together with the job cache (on success or
    # reset when the job changes). An explicit value restores a shared
    # worker-wide uv cache, an escape hatch for dev/tests (point it at tmp).
    uv_cache_dir: Path | None = Field(
        default=None,
        alias="ORCASTER_UV_CACHE_DIR",
    )

    # Task-scoped scratch cache (ORCASTER_CACHE_DIR exported to the subprocess).
    # Default `/var/cache/orcaster` (dedicated cache volume); the worker GC
    # keeps the directory under control (no artificial quota: when the disk fills up,
    # a reactive LRU cleans).
    cache_root: Path | None = Field(
        default=None,
        alias="ORCASTER_CACHE_ROOT",
        description=(
            "Root of the task-scoped scratch cache. None = `/var/cache/orcaster` "
            "(the dedicated cache volume)."
        ),
    )
    cache_max_age_days: int = Field(
        default=3,
        ge=1,
        alias="ORCASTER_CACHE_MAX_AGE_DAYS",
    )
    cache_min_free_fraction: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        alias="ORCASTER_CACHE_MIN_FREE_FRACTION",
        description="Reactive GC trigger: when free/total drops below this.",
    )
    cache_recover_free_fraction: float = Field(
        default=0.80,
        ge=0.0,
        le=1.0,
        alias="ORCASTER_CACHE_RECOVER_FREE_FRACTION",
        description="Reactive LRU GC target: free space until this ratio is exceeded.",
    )
    cache_claim_min_free_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        alias="ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION",
        description=(
            "Free-disk floor below which the worker stops claiming tasks. "
            "None = use `cache_min_free_fraction`, the same floor as the GC: "
            "the gate closes exactly where the GC declares the pressure "
            "unrecoverable. An explicit value must be <= `cache_min_free_fraction` "
            "(above that floor the GC does not even try to free space). "
            "`0.0` disables the gate."
        ),
    )
    cache_claim_gate_max_seconds: float = Field(
        default=1800.0,
        ge=0.0,
        alias="ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS",
        description=(
            "Maximum time the claim gate may spend holding back claims. "
            "It only counts time spent holding back: it pauses while the worker "
            "runs a task or flushes the cache, because there the `busy` state is not set "
            "by the gate and no claim is blocked, and it resets when the disk "
            "recovers. When it expires, the worker claims again even if the "
            "disk is still below the floor: the GC only deletes under `cache_root/tasks/`, "
            "so a worker-wide uv cache or any other "
            "content on the same volume can keep it full forever and "
            "leave the worker out of service with no trace beyond a WARNING. "
            "Once the limit expires, tasks failing with the real cause "
            "(what happened before the gate) is preferred over a silently idle fleet. "
            "Default 1800 s: generous compared with `cache_gc_interval_seconds` (300 s), "
            "it gives the GC several passes to recover before giving up. `0` "
            "disables the fail-open (the gate may stay closed forever)."
        ),
    )
    cache_gc_interval_seconds: int = Field(
        default=300,
        ge=1,
        alias="ORCASTER_CACHE_GC_INTERVAL_SECONDS",
    )
    cache_flush_timeout_seconds: float = Field(
        default=300.0,
        ge=1,
        alias="ORCASTER_CACHE_FLUSH_TIMEOUT_SECONDS",
        description=(
            "Time bound for the forced cache flush. "
            "If the rmtree hangs (e.g. NFS or a degraded disk) the worker "
            "releases the slot on expiry and takes tasks again; the rmtree "
            "thread is not cancelled, it continues in the background best-effort."
        ),
    )
    cache_delete_on_success: bool = Field(
        default=True,
        alias="ORCASTER_CACHE_DELETE_ON_SUCCESS",
        description=(
            "If True (default), delete the task-scoped cache dir "
            "(`<cache_root>/tasks/<job_key>/`) as soon as the execution finishes "
            "successfully (status `completed`). Safe with respect to reuse: the cache is "
            "kept for retries, which only happen after a failure; a successful "
            "task is not retried. On failure/cancel it is NOT deleted. The periodic "
            "GC (age / disk pressure) remains as a safety net."
        ),
    )
    cache_self_flush_on_disk_preflight: bool = Field(
        default=True,
        alias="ORCASTER_CACHE_SELF_FLUSH_ON_DISK_PREFLIGHT",
        description=(
            "If True (default), the worker triggers its own full cache flush "
            "(`flush_caches`) when a task dies in its disk preflight: "
            "a marker from `ORCH_FLUSH_MARKERS` appears in stderr. "
            "With no markers configured there is nothing to detect and the flag has "
            "no effect. It detects by the marker in the stream, not by the "
            "exit code: a generic exit 1 covers unrelated failures. "
            "The flush deletes ALL of `tasks/*` + the `uv` cache, "
            "not only the previous jobs: in this case the disk was "
            "consumed by the job own partial download, so preserving its "
            "dir would free nothing."
        ),
    )

    # Stderr markers that trigger the self-flush, env
    # `ORCH_FLUSH_MARKERS`: literal phrases separated by `;`. The core ships
    # none (empty = no detection); each deployment declares its own. The
    # parsing lives in `main.parse_flush_markers`.
    flush_markers: str = Field(default="", alias="FLUSH_MARKERS")

    # Per-task egress: a file the worker writes with the domain
    # allowlist of the task in progress and that the sidecar proxy
    # (`egress_proxy.py`) reads per request. Same literal env var name
    # (WITHOUT the pydantic-settings `ORCH_` prefix) in both processes: the
    # proxy runs outside `Settings` and reads `os.environ` directly, so here
    # we also read `ORCASTER_EGRESS_ALLOW_FILE` by hand so both share the
    # same path when the deployment sets it. `default_factory` (not a plain
    # default) so the env var is read when `Settings()` is instantiated, not once
    # at module import (same pattern as `state_dir` above).
    egress_allow_file: Path = Field(default_factory=_default_egress_allow_file)

    # Env/egress isolation: with the flag `True`, a key that is
    # absent (or present but malformed) in the payload (`task_env`/
    # `egress_allowlist`) is promoted to strict (`[]`) without having to
    # re-create each task (see `main.py::_resolve_isolation`).
    #
    # The CODE default is `True`: a worker without the variable runs strict.
    # An explicit `ORCH_ENFORCE_ISOLATION=false` keeps the legacy mode (absent key
    # = `None`, no restriction) while the old queue (tasks without those
    # keys) has not drained; the worker warns at startup with the flag off. The
    # installer (`setup-scripts/debian13-worker`) always stamps an explicit value, and
    # `push-release`/`install-release` do not touch `env.N`: before deploying
    # this version, every live worker must carry its explicit value.
    enforce_isolation: bool = Field(default=True, alias="ENFORCE_ISOLATION")

    # Variables that require strict isolation, env
    # `ORCH_ISOLATION_REQUIRED_ENV`: names separated by commas or spaces.
    # If one of them is present (non-empty) with `enforce_isolation`
    # off, the worker refuses to start
    # (`main.py::_ensure_strict_isolation_or_die`). Effective list = the
    # built-in one (`BUILTIN_ISOLATION_REQUIRED_ENV`, empty: the core names
    # no workload) UNIONED with this setting; names are only added,
    # no source removes a name another one added. Matching is by exact
    # name, never by pattern or by value. A malformed value stops the
    # worker when `Settings` loads (fails closed). The pre-swap gate in
    # `ops/release` reads the same env.N line with the same name grammar.
    isolation_required_env: str = Field(default="", alias="ISOLATION_REQUIRED_ENV")

    @field_validator("isolation_required_env")
    @classmethod
    def _validate_isolation_required_env(cls, value: str) -> str:
        # Validates and normalizes (`,` separator); the parsing lives in a separate
        # function so the startup guard and the tests use the same rule.
        return ",".join(parse_isolation_required_env(value))

    # Key of the signed egress allowlist: 32+ bytes in hex.
    # `make up client` injects it from a Podman secret into the worker and its
    # proxy. The worker removes it from `os.environ` at startup
    # (`main.py::run`), so no task in legacy mode inherits it; the
    # non-dumpable process covers `/proc/<pid>/environ`. Empty = unsigned
    # allowlist. Malformed = the worker does not start.
    egress_key: SecretStr = Field(default=SecretStr(""), alias="EGRESS_KEY", repr=False)

    @field_validator("egress_key")
    @classmethod
    def _validate_egress_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw.strip():
            parse_egress_key(raw)  # ValueError without the value in the message
        return value

    @property
    def egress_key_bytes(self) -> bytes | None:
        raw = self.egress_key.get_secret_value()
        return parse_egress_key(raw) if raw.strip() else None

    @property
    def isolation_required_names(self) -> tuple[str, ...]:
        """Effective list: built-in + configured, without duplicates, in order."""
        merged = BUILTIN_ISOLATION_REQUIRED_ENV + parse_isolation_required_env(
            self.isolation_required_env
        )
        return tuple(dict.fromkeys(merged))

    @model_validator(mode="after")
    def _validate_cache_fractions(self) -> Settings:
        if self.cache_recover_free_fraction <= self.cache_min_free_fraction:
            raise ValueError(
                f"cache_recover_free_fraction ({self.cache_recover_free_fraction}) "
                f"must be > cache_min_free_fraction ({self.cache_min_free_fraction})"
            )
        # Same kind of trap as the one above, applied to the claim gate:
        # the GC pressure pass does not even start above
        # `cache_min_free_fraction` (the condition is `free/total < min`), so
        # a higher claim floor cannot be reopened by the GC; only age-based
        # pruning (`cache_max_age_days`, 3 days by default) would unblock it, if
        # there is anything old to delete. The worker would stop claiming with no
        # visible error: it is rejected at startup rather than leaving the operator
        # with an idle fleet that looks `online` and healthy in the dashboard (the
        # poll body `status` is not rendered; the only signal is the worker log). With
        # `cache_min_free_fraction=0.0` (pressure GC off) any positive floor is
        # rejected, which is the correct conclusion. An explicit `0.0` (gate
        # disabled) is outside the check.
        claim = self.cache_claim_min_free_fraction
        if claim is not None and claim > 0.0 and claim > self.cache_min_free_fraction:
            raise ValueError(
                f"cache_claim_min_free_fraction ({claim}) must be <= "
                f"cache_min_free_fraction ({self.cache_min_free_fraction}): "
                "the GC does not trigger its pressure pass above that floor, "
                "so a higher claim floor leaves the worker unable to claim, with "
                "nothing in the system able to reopen the gate "
                "(use 0.0 to disable the gate)"
            )
        return self

    def resolved_cache_root(self) -> Path:
        """Effective path of the cache root.

        ``cache_root`` if set, or the default ``/var/cache/orcaster``
        (the dedicated cache volume, NOT the data volume
        ``<state_dir>/cache``).
        """
        return self.cache_root if self.cache_root is not None else _DEFAULT_CACHE_ROOT

    @property
    def resolved_claim_min_free_fraction(self) -> float:
        """Effective floor of the claim gate.

        Inherits ``cache_min_free_fraction`` when there is no explicit override: the
        gate closes exactly where the GC declares the pressure unrecoverable, so
        there is a single threshold and not two that could contradict each other. An
        explicit override can only lower that floor (validated by
        ``_validate_cache_fractions``): above ``cache_min_free_fraction`` the GC does
        not run its pressure pass and the gate could not be reopened. An explicit
        ``0.0`` disables the gate (no free fraction is lower than 0).
        """
        if self.cache_claim_min_free_fraction is None:
            return self.cache_min_free_fraction
        return self.cache_claim_min_free_fraction


def load_settings() -> Settings:
    return Settings()


# Unsupported legacy variable names. `Settings` has `extra="ignore"`, so they
# would be dropped without a trace; `warn_legacy_env` names each one and its
# replacement at startup. Their values are never read.
_LEGACY_ENV_PREFIX = "ORCH_TASKORCH_"
_CURRENT_ENV_PREFIX = "ORCH_ORCASTER_"

_log = logging.getLogger(__name__)


def legacy_env_replacements(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    """`(legacy name, replacement)` for each unsupported legacy name in `environ`,
    sorted by name."""
    return [
        (name, _CURRENT_ENV_PREFIX + name.removeprefix(_LEGACY_ENV_PREFIX))
        for name in sorted(environ)
        if name.startswith(_LEGACY_ENV_PREFIX)
    ]


def warn_legacy_env(environ: Mapping[str, str], env_file: Path | str | None = None) -> None:
    """Log one WARNING per unsupported legacy variable name found in `environ` or
    in `env_file` (the dotenv file `Settings` reads), naming its replacement."""
    names: dict[str, str] = dict.fromkeys(environ, "")
    if env_file is not None and Path(env_file).is_file():
        names.update(dict.fromkeys(dotenv_values(env_file), ""))
    for legacy, replacement in legacy_env_replacements(names):
        _log.warning(
            "%s is an unsupported legacy name and is ignored; use %s instead",
            legacy,
            replacement,
        )
