"""Main worker loop (unified `/poll` endpoint + executor in a separate task).

The worker talks only to POST /v1/workers/{id}/poll for its main loop.
The unified endpoint returns:

    * task (claim or push, optional),
    * cancel_signals[] (executions cancelled server-side),
    * next_poll_after_seconds (server-driven cadence).

Topology: 2 coexisting asyncio.Tasks inside the worker, instead of an
earlier design where poll_loop ``await``ed the dispatch inline (blocking
polls for the whole execution of a task):

    +----------------------+        +-----------------------------+
    |  poll_loop (1, *)    |        |  executor_task (0 or 1)     |
    |  - always alive      | spawn  |  - runs _dispatch_poll_task |
    |  - cadence 1-30s     | -----> |  - honours abandon_event    |
    |  - reap + spawn      |        |  - a single execution       |
    |  - translates cancels| <----- |                             |
    |    to abandon_event  |  reap  +-----------------------------+
    +----------------------+

poll_loop is the only "always alive" task; executor_task lives only while
a task is active. This preserves the "one simultaneous execution per
worker" restriction without N-slot tracking, and lets polls (which act as
implicit heartbeats) keep being sent even if a task takes minutes -
a structural precondition for the ephemeral venv (with heavy dependency
installation).

Every external task carries
``payload={"requirements": [...], "commands": [...]}`` and runs inside
an ephemeral venv created by ``ephemeral_venv`` (see
``orcaster_worker.venv_manager``). In-process reserved commands
(`__reload_config__`) follow their own path in ``_run_reserved``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import mimetypes
import os
import random
import shutil
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from orcaster_worker.api import (
    ArtifactsTooLargeError,
    LeaseLostError,
    OrchestratorAPI,
    ShutdownError,
)
from orcaster_worker.artifacts import ArtifactsSpec, collect_artifacts, plan_batches
from orcaster_worker.cache_flush import flush_caches
from orcaster_worker.config import Settings, load_settings, warn_legacy_env
from orcaster_worker.dispatcher import dispatch_run_batch
from orcaster_worker.egress import EGRESS_KEY_ENV, read_allowlist, write_allowlist
from orcaster_worker.idempotency import get_push_result, store_push_result
from orcaster_worker.internal_handlers import RESERVED_COMMANDS, handle_reserved
from orcaster_worker.output_streamer import SPILL_SUBDIR, OutputStreamer, cleanup_spill_orphans
from orcaster_worker.poll import PollCancelSignal, PollRequest, PollResponse, PollTaskPayload
from orcaster_worker.proc_hardening import make_process_non_dumpable
from orcaster_worker.runner import RunResult, kill_process_group
from orcaster_worker.secret_redaction import (
    SecretRedactor,
    install_log_redaction,
    set_active_redactor,
)
from orcaster_worker.signing import build_task_message, load_public_key, verify_signature
from orcaster_worker.state import Credentials
from orcaster_worker.state import load as load_creds
from orcaster_worker.state import save as save_creds
from orcaster_worker.sysinfo import (
    CpuSampler,
    cgroup_memory_cap_missing,
    detect_disk,
    detect_ram,
    detect_total_disk_mb,
    detect_total_ram_mb,
)
from orcaster_worker.task_cache import (
    ResetStats,
    create_execution_run_dir,
    ensure_task_cache_dir,
    reset_other_job_caches,
    resolve_job_key,
    run_gc,
    sweep_run_dirs,
    sweep_trash_dirs,
    trash_root,
)
from orcaster_worker.task_env import describe_env_sources, render_names
from orcaster_worker.venv_manager import (
    VenvAbandonedError,
    VenvCreationError,
    VenvInstallError,
    ephemeral_venv,
)

log = logging.getLogger("orcaster.worker")


# Streamer drain with a deadline: worker shutdown and synthetic
# diagnostics. We do not block the worker indefinitely for a diagnostic -
# if the server is down, the next /complete fails anyway and the lease
# expires (same loss policy as a crash).
_DRAIN_DEADLINE_SECONDS = 10.0

# Minimum tail (in characters of the already decoded stream, not bytes) that the
# scanner keeps between chunks to match a marker even if it is split at the
# boundary of two chunks. The effective tail is `max(this, 5 * longest marker)`:
# never smaller than the marker. The slice operates on `str`, not on bytes.
_DISK_PREFLIGHT_TAIL_CHARS = 256

# Separator of `ORCH_FLUSH_MARKERS`. Neither comma nor space: a marker is a
# stderr phrase and may contain both.
_FLUSH_MARKER_SEPARATOR = ";"


def parse_flush_markers(raw: str) -> tuple[str, ...]:
    """`ORCH_FLUSH_MARKERS` -> tuple of markers, without empty entries or duplicates.

    Each item is trimmed; `""` is the empty list (no detection). The self-flush
    signal is the stderr text, NOT the exit code: exit 1 is a catch-all that
    would trigger a flush on unrelated failures.
    """
    items = (item.strip() for item in raw.split(_FLUSH_MARKER_SEPARATOR))
    return tuple(dict.fromkeys(item for item in items if item))


def _log_flush_markers(settings: Settings) -> None:
    """INFO at every startup with the self-flush markers in use."""
    markers = parse_flush_markers(settings.flush_markers)
    log.info(
        "startup: flush markers used: %s",
        ", ".join(repr(m) for m in markers) if markers else "(none)",
    )


class _DiskPreflightScanner:
    """Wraps the ``on_output`` callback to detect in flight a disk preflight
    marker (``ORCH_FLUSH_MARKERS``) without buffering the output.

    ``dispatch_run_batch`` does not accumulate stderr locally: each piece is
    streamed through ``on_output`` and discarded (``_pump_stream``), so the only
    place on the client side where the marker is observable is by intercepting
    the stream. It forwards every piece intact to the real sink
    (``streamer.feed``) and matches the markers against ``tail + piece`` so as
    not to miss them when the process splits them at the boundary of two
    chunks. The tail is bounded so it does not grow with the stream. Without
    markers it detects nothing. It scans only ``stderr``: the preflight line
    goes to stderr, and limiting the scan avoids false positives if a task
    prints the text on its stdout.
    """

    def __init__(self, sink: Callable[..., Awaitable[None]], markers: tuple[str, ...] = ()) -> None:
        self._sink = sink
        self._markers = markers
        self._tail_chars = max(_DISK_PREFLIGHT_TAIL_CHARS, 5 * max(map(len, markers), default=0))
        self._tail = ""
        self.marker_seen = False

    async def feed(
        self,
        stdout: str | None = None,
        stderr: str | None = None,
        command: str | None = None,
    ) -> None:
        await self._sink(stdout=stdout, stderr=stderr, command=command)
        if self.marker_seen or stderr is None or not self._markers:
            return
        hay = self._tail + stderr
        if any(marker in hay for marker in self._markers):
            self.marker_seen = True
            self._tail = ""
        else:
            self._tail = hay[-self._tail_chars :]


def _should_self_flush_on_disk_preflight(
    settings: Settings, status_str: str, marker_seen: bool
) -> bool:
    """True if a failed batch died in the disk preflight and self-flush
    is enabled.

    Requires all three conditions: the config flag, the marker seen in the
    stream (the real signal, see ``parse_flush_markers``) and the batch
    having reported failure (``status_str != "ok"``). With status ``ok`` it is
    not triggered even if the marker appears in the output.
    """
    return settings.cache_self_flush_on_disk_preflight and marker_seen and status_str != "ok"


def _task_redactor(task: PollTaskPayload) -> SecretRedactor | None:
    """Redactor for the secrets the server delivered with the task,
    or None if there are no values to mask."""
    redactor = SecretRedactor(task.secrets.values())
    return redactor if redactor.active else None


def _activate_task_redaction(task: PollTaskPayload) -> None:
    """Enables masking of local logs with the secrets of ``task``.
    It stays in force until ``BusyState.reset()``, which runs after the reaper
    logs an executor crash."""
    set_active_redactor(_task_redactor(task))


def _make_streamer(
    api: OrchestratorAPI, settings: Settings, task: PollTaskPayload
) -> OutputStreamer:
    # All output going to the server (command stream and synthetic
    # diagnostics) goes through an OutputStreamer created here: it is the
    # single point where the secrets delivered by the server are masked.
    return OutputStreamer(
        api,
        task.task_id,
        task.lease_token,
        max_bytes=settings.output_flush_max_bytes,
        interval=settings.output_flush_interval_seconds,
        buffer_max_bytes=settings.output_buffer_max_bytes,
        spill_dir=settings.state_dir / SPILL_SUBDIR,
        redactor=_task_redactor(task),
    )


async def _emit_synthetic_output(
    api: OrchestratorAPI,
    settings: Settings,
    task: PollTaskPayload,
    *,
    stdout: str | None = None,
    stderr: str | None = None,
    streamer: OutputStreamer | None = None,
) -> None:
    """Persists synthetic diagnostics (reserved commands, crashes, cancels)
    as output_events BEFORE complete - after complete the execution is
    terminal and /output would return 409.

    With `streamer` (an execution that already streamed) it reuses its seq
    counter - a new streamer would start at seq=1 and the server's ON CONFLICT
    would silently discard the events. Without `streamer`, it creates an
    ephemeral one (seq from 1, execution with no prior output)."""
    if not stdout and not stderr:
        return
    s = streamer if streamer is not None else _make_streamer(api, settings, task)
    await s.feed(stdout=stdout or None, stderr=stderr or None)
    await s.aclose(drain_deadline=_DRAIN_DEADLINE_SECONDS)


def _prepare_cache_env(env: dict[str, str], *, cache_root: Path, job_key: str) -> tuple[Path, Path]:
    """Resolves the task-scoped cache dir, creates it, and populates the
    ecosystem env vars + ``ORCASTER_ARTIFACTS_DIR``. Returns ``(cache_dir, artifacts_dir)``.

    The artifacts dir is a DEDICATED folder (``<cache_dir>/artifacts``), a sibling
    of ``hf/``/``pip/``/``xdg/`` and created EMPTY before the subprocess. Since it
    only contains explicit task outputs, collecting it never drags in caches.
    The call site reuses the returned ``artifacts_dir`` instead of
    recomputing it: this function is the single source of the path."""
    cache_dir = ensure_task_cache_dir(cache_root, job_key)
    env["ORCASTER_CACHE_DIR"] = str(cache_dir)
    env["HF_HOME"] = str(cache_dir / "hf")
    env["PIP_CACHE_DIR"] = str(cache_dir / "pip")
    env["XDG_CACHE_HOME"] = str(cache_dir / "xdg")
    artifacts_dir = cache_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    env["ORCASTER_ARTIFACTS_DIR"] = str(artifacts_dir)
    return cache_dir, artifacts_dir


# --- State shared between asyncio tasks --------------------------------------


@dataclass
class BusyState:
    """Mutable state of the active execution.

    A single simultaneous execution per worker (single slot). poll_loop reads
    these fields when building the POST /poll body; cancel signals are
    matched by (task_id, lease_token).

    ``executor_task`` references the ``asyncio.Task`` that runs the dispatch
    in the background. ``is_busy`` is decided by the task state (it exists and
    is not finished) instead of looking at ``task_id`` / ``abandon_event``,
    to avoid reporting ``idle`` during the reaping window and ``busy`` after
    an executor crash without reap (phantom state).

    The active task may carry an attached ``PollTaskPayload``
    (``task_payload``) so poll_loop can invoke ``_safe_complete_poll`` when
    reaping if executor_task ends with an unhandled exception (without this,
    poll_loop would have no way to report to the server).

    ``active_job_key`` is the job key of the cache the active execution is
    using. ``_run_external`` publishes it before touching the cache so that
    ``cache_gc_loop``, which runs in parallel in the same process, knows which
    dir it must NOT delete. The GC re-reads it before every deletion, so a
    job change in the middle of a pass is also covered. The GC consumes
    ``live_job_key``, not this raw field: see there for why.

    ``streamer`` references the active execution's ``OutputStreamer``
    (published by ``_run_external`` when creating it) so the reaper can
    continue its ``seq`` counter when emitting the synthetic diagnostic of a
    crash - an ephemeral streamer would start at seq=1 and the server's
    ON CONFLICT would silently discard it.
    """

    task_id: int | None = None
    lease_token: str | None = None
    progress: int | None = None
    progress_message: str | None = None
    abandon_event: asyncio.Event = field(default_factory=asyncio.Event)
    executor_task: asyncio.Task[Any] | None = None
    task_payload: PollTaskPayload | None = None
    streamer: OutputStreamer | None = None
    active_job_key: str | None = None
    cache_flush_task: asyncio.Task[Any] | None = None
    cache_flush_pending_ack: bool = False
    # Set by `_run_external` when it sees the disk preflight marker from
    # the process in a failed batch; poll_loop consumes it to launch the flush.
    # NOT cleared in `reset()`: it must survive the reap between the end of the
    # executor and the next poll (same criterion as `cache_flush_pending_ack`).
    self_flush_requested: bool = False

    @property
    def is_busy(self) -> bool:
        return self.executor_task is not None and not self.executor_task.done()

    @property
    def is_flushing(self) -> bool:
        return self.cache_flush_task is not None and not self.cache_flush_task.done()

    @property
    def live_job_key(self) -> str | None:
        """The job key the GC must not delete, or ``None`` if there is no live job.

        ``active_job_key`` is cleared in ``reset()``, whose only call site is
        ``_reap_executor_task``: between the execution's asyncio task
        finishing and the reap in the next poll_loop iteration, the raw field
        still names a dead job. With the disk under pressure that makes the GC
        protect a dir nobody uses any more and, if it was the only multi-GB
        candidate, declare the pressure unrecoverable while looking right at it.
        The window is not short: network backoff takes the poll interval to
        ``poll_max_interval_seconds``.

        Deriving the protection from the task state -the same decision as
        ``is_busy``, and for the same reason- turns it off the instant the job
        stops running without opening the inverse window: while the task is
        alive the key is returned, so the running job's dir stays untouchable.
        ``active_job_key`` is only published from inside the
        ``executor_task``, so "task alive" covers "the dir is in use" except
        for a child that survives the process group kill, a residual the
        previous protection did not cover either beyond the reap. That invariant
        is what sustains the protection of the running job and
        ``test_live_job_key_published_by_the_poll_loop_spawn`` verifies it,
        through the production path: if it breaks, this getter returns ``None``
        while the job is running and the GC deletes the cache of a task in
        progress.

        The order of the reads is load-bearing: the task FIRST and the key
        SECOND. This getter is invoked by the GC thread, and the other way round
        there would be a bad interleaving (read the key of dead job A, the loop
        reaps and claims B, and return A: it would protect a corpse and leave B
        exposed). With the task first, having observed ``done() is False``
        implies that task has not been reaped yet, and a reset after that
        observation leaves the key as ``None`` or as the new job's: both are
        safe. The task read goes to a local so a concurrent reset between the
        check and ``done()`` does not turn into an ``AttributeError`` on
        ``None``. The key is returned directly: any value it has after having
        observed the task alive is safe, by the argument above.
        """
        task = self.executor_task
        if task is None or task.done():
            return None
        return self.active_job_key

    def reset(self) -> None:
        self.task_id = None
        self.lease_token = None
        self.progress = None
        self.progress_message = None
        self.abandon_event = asyncio.Event()
        self.executor_task = None
        self.task_payload = None
        self.streamer = None
        self.active_job_key = None
        # End of the task: logs stop masking its secrets.
        set_active_redactor(None)


# --- Credentials --------------------------------------------------------------


class EnrollmentTokenMissingError(RuntimeError):
    """No persisted credentials and `ORCH_ENROLLMENT_TOKEN` is missing to register."""


class EnrollmentTokenRejectedError(RuntimeError):
    """The server rejected the enrollment token at `/register` (401/422):
    invalid, expired, already used, or malformed body."""


async def ensure_credentials(api: OrchestratorAPI, settings: Settings) -> Credentials:
    creds = load_creds(settings.state_dir)
    if creds is not None:
        log.info("Reusing existing worker_id=%s", creds.worker_id)
        api.set_credentials(creds.worker_id, creds.api_key)
        return creds

    # Actionable fail-fast: registering requires an enrollment token.
    # Without it, `POST /v1/workers/register` would return an opaque 422; we prefer
    # a clear error at boot before touching the network.
    if not settings.enrollment_token:
        raise EnrollmentTokenMissingError(
            "No persisted credentials and the enrollment token needed to "
            "register the worker is missing.\n"
            "  - Ask a workspace owner to issue an enrollment token "
            "from workspace management.\n"
            "  - Set the token in ORCH_ENROLLMENT_TOKEN (env or .env) and restart the "
            "worker.\n"
            "  - The token is only used on first start; after registering, the "
            "credentials stay in state.json and are not requested again.\n"
            "  - The api_key is not configured via env: the server issues it at "
            "register and it is persisted in state.json."
        )

    log.info("No state found; registering against %s", settings.server_url)
    try:
        reg = await api.register(
            enrollment_token=settings.enrollment_token,
            hostname=settings.hostname,
            os_name=settings.worker_os,
            arch=settings.arch,
            ram_mb=detect_total_ram_mb(),
            disk_mb=detect_total_disk_mb(settings.resolved_cache_root()),
            version=settings.version,
            capabilities=settings.capabilities,
            name=settings.worker_name or None,
        )
    except httpx.HTTPStatusError as exc:
        # 401: invalid/revoked/used/expired token. 422: rejected body
        # (empty/malformed token). Instead of propagating the raw traceback of
        # `raise_for_status()`, we give the operator an actionable message.
        code = exc.response.status_code
        if code in (401, 422):
            raise EnrollmentTokenRejectedError(
                f"The server rejected the registration with HTTP {code}: the enrollment token "
                "is invalid, expired or has no uses left.\n"
                "  - Ask a workspace owner to issue a new enrollment "
                "token.\n"
                "  - Update ORCH_ENROLLMENT_TOKEN (env or .env) and restart the worker.\n"
                "  - If the worker was already registered, deleting state.json and "
                "re-registering requires a fresh token."
            ) from exc
        raise
    creds = Credentials(worker_id=reg.worker_id, api_key=reg.api_key)
    save_creds(settings.state_dir, creds)
    api.set_credentials(creds.worker_id, creds.api_key)
    log.info("Registered worker_id=%s", creds.worker_id)
    return creds


# --- Payload of the external run_batch ---------------------------


_PAYLOAD_ERROR = "task payload must include 'requirements' (list) and 'commands' (non-empty list)"


def _extract_task_payload(
    payload: dict[str, Any],
) -> tuple[list[str], list[str], int | float | None] | None:
    """Validates and extracts ``(requirements, commands, timeout_seconds)``.

    Returns ``None`` if the payload does not meet the contract. The caller decides
    how to report the failure (status=failed, exit_code=2).

    ``timeout_seconds`` is optional; it is returned as it comes in
    the payload (or ``None`` if absent). Type/range validation
    is delegated to the dispatcher.
    """
    raw_reqs = payload.get("requirements")
    raw_cmds = payload.get("commands")
    if not isinstance(raw_reqs, list) or not all(isinstance(r, str) for r in raw_reqs):
        return None
    if (
        not isinstance(raw_cmds, list)
        or not raw_cmds
        or not all(isinstance(c, str) for c in raw_cmds)
    ):
        return None
    raw_timeout = payload.get("timeout_seconds")
    return list(raw_reqs), list(raw_cmds), raw_timeout


def _extract_isolation_payload(
    payload: dict[str, Any],
) -> tuple[list[str] | None, list[str] | None]:
    """Extracts ``(task_env, egress_allowlist)`` with tri-state semantics.

    Key absent -> None (legacy). Key present and list of str -> the list
    (even []). Malformed -> None (tolerant: the server already validates at creation;
    a worker must not crash because of a crooked payload)."""

    def _as_str_list(v: object) -> list[str] | None:
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return list(v)
        return None

    task_env = _as_str_list(payload["task_env"]) if "task_env" in payload else None
    egress = _as_str_list(payload["egress_allowlist"]) if "egress_allowlist" in payload else None
    return task_env, egress


def _resolve_isolation(
    payload: dict[str, Any], *, enforce: bool
) -> tuple[list[str] | None, list[str] | None]:
    """Applies the absent/malformed -> strict promotion according to the flag.

    Wraps ``_extract_isolation_payload`` with the rollout lever
    ``Settings.enforce_isolation`` (env ``ORCH_ENFORCE_ISOLATION``):

    - ``enforce=False`` -> pure tri-state (absent or malformed key =
      ``None``, tolerant legacy).
    - ``enforce=True``  -> both an absent key and a present but malformed
      key (e.g. ``"task_env": "x"``) are promoted to ``[]``
      (strict). The goal of the flag is to guarantee that, once the old
      queue is drained, no payload can end up in the unrestricted
      mode (``None``); a malformed key must not be an escape route
      (fail-open) into that mode. A warning naming the key is logged
      so the operator can spot crooked payloads."""
    task_env, egress = _extract_isolation_payload(payload)
    if enforce:
        if task_env is None:
            if "task_env" in payload:
                log.warning(
                    "enforce_isolation: task_env present but malformed (%r); "
                    "treating as strict ([])",
                    payload["task_env"],
                )
            task_env = []
        if egress is None:
            if "egress_allowlist" in payload:
                log.warning(
                    "enforce_isolation: egress_allowlist present but malformed (%r); "
                    "treating as strict ([])",
                    payload["egress_allowlist"],
                )
            egress = []
    return task_env, egress


def _apply_task_egress(
    allow_file: Path, egress_allowlist: list[str] | None, key: bytes | None = None
) -> None:
    """Writes the per-task allowlist for the sidecar proxy to read.

    With ``key`` (``ORCH_EGRESS_KEY``) it signs it with HMAC: the proxy with a
    key ignores entries from a file without a valid signature, so a task
    that rewrites the file does not widen its egress.

    ``None`` (key absent, legacy) -> does not touch the file. List (even []) ->
    writes it; with [] the proxy is left with only the base allowlist.

    Sanitizes each entry before writing: ``write_allowlist`` serializes
    one entry per line without escaping, so a domain with an embedded
    newline (operator-authored payload, not content-validated on the
    server) would inject an extra entry on round-trip through
    ``read_allowlist``. Fail closed: any entry with embedded whitespace is
    dropped (not truncated) and a warning is logged; valid domains are
    still written. Empty entries (``""``) are also dropped
    (they truncate nothing by themselves, but would produce a blank line).

    A write failure (disk full, permissions, or the filesystem mounted
    read-only when the egress volume does not exist yet -
    ``EGRESS_ISOLATION=0``) must never propagate over the real result of the
    task: the generic handler would mark it "runner crashed". With the
    topology off the file is irrelevant (no proxy reads it);
    with the topology on, a stale or missing file fails closed at
    the network layer (the proxy denies any host not in the base
    allowlist). It is caught and logged as a warning, like
    ``_clear_task_egress``.
    """
    if egress_allowlist is None:
        return
    domains: list[str] = []
    for entry in egress_allowlist:
        if not entry or any(ch.isspace() for ch in entry):
            log.warning(
                "egress_allowlist entry dropped (empty or embedded whitespace/newline): %r",
                entry,
            )
            continue
        domains.append(entry)
    try:
        write_allowlist(allow_file, domains, key)
    except OSError as e:
        log.warning("failed to write egress allowlist file %s: %s", allow_file, e)


def _clear_task_egress(
    allow_file: Path, egress_allowlist: list[str] | None, key: bytes | None = None
) -> None:
    """Closes a task's egress window, tolerating write failures.

    The tri-state lives here, not at the call sites: with ``egress_allowlist is
    None`` (legacy) the file is not touched, an exact mirror of the early return of
    ``_apply_task_egress``. This way a new call site cannot forget the guard
    and overwrite a pre-existing allowlist of a legacy flow.

    Called from ``_run_external`` (the window opens before the
    venv setup): the inner ``finally``, which wraps ``dispatch_run_batch``
    and closes the window as soon as the task ends; the early return on
    cancellation before ``run_batch`` (closes before reporting to the server and
    before the venv cleanup); and the outer ``finally``, which covers the
    venv setup failure paths so an orphaned per-task allowlist does not stay
    in force for the next task. In the happy path it runs twice,
    which is harmless: it rewrites the same empty allowlist.

    If the clear (disk full, permissions) raised, the exception would propagate
    over the real dispatch result and the generic "runner crashed" handler
    would mark as failed a task that actually finished well (or lose the real
    status of one that failed). It is caught and logged as a warning instead of
    re-raised: the allowlist stays stale (the next ``_apply_task_egress`` will
    overwrite it anyway), but that is preferable to corrupting the task result.

    ``_apply_task_egress`` already catches ``OSError`` (the real case: disk full,
    permissions, read-only filesystem) and logs its own warning, so that
    case never reaches this ``except``. It is kept as an additional safety
    net (broad ``Exception``) in case some other non-``OSError`` failure
    escaped from the write.
    """
    if egress_allowlist is None:
        return
    try:
        _apply_task_egress(allow_file, [], key)
    except Exception as e:  # noqa: BLE001 - intentional fail-open, see docstring
        log.warning("failed to clear egress allowlist file %s: %s", allow_file, e)


def _warn_if_memory_cap_missing() -> None:
    """Warns at startup if the container runs without a memory cap.

    The cap (`podman run --memory`) is containment: with it, a task that
    runs away dies as `CONSTRAINT_MEMCG` inside its own cgroup; without it
    the global OOM killer fires and can take down the whole host. Losing it was
    silent: a rollout once left the fleet's workers without a cap and it was
    only discovered by reading the `ceiling` reported by the task tool's
    telemetry, which is a third party. The warning carries the recipe, not just
    the diagnosis: whoever reads it is facing an already running worker.

    It never aborts: a worker without a cap is still a useful worker, and a fail-fast
    here would take down the whole fleet of a misconfigured host.
    """
    if cgroup_memory_cap_missing():
        log.warning(
            "startup: container running with NO memory cap (cgroup memory.max=max). "
            "A runaway task will hit the host OOM killer instead of dying in its own "
            "cgroup. Recreate it with `make up client N=<n> WORKER_MEMORY=<size>`, or "
            "declare WORKER_MEMORY in setup.env and re-run run-fleet.sh."
        )


# Worker variables that a task in legacy mode inherits and that the startup
# warning names: the enrollment token and the worker credential.
# Names only; the warning never prints values.
_WORKER_CREDENTIAL_ENV_NAMES: tuple[str, ...] = ("ORCH_ENROLLMENT_TOKEN", "ORCH_API_KEY")


def _ensure_strict_isolation_or_die(settings: Settings) -> None:
    """Startup guard for strict isolation.

    With ``enforce_isolation`` off, a task without ``task_env`` runs in legacy
    mode and inherits the worker's entire environment
    (``task_env.build_scrubbed_base_env``), including ``ORCH_ENROLLMENT_TOKEN``
    and ``ORCH_API_KEY``: anyone who can create tasks can print them to stdout and
    the server stores that output.

    1. INFO at every startup with the names the guard checks
       (``Settings.isolation_required_names``: empty built-in list ∪
       ``ORCH_ISOLATION_REQUIRED_ENV``). A malformed list already stopped the
       worker when loading ``Settings``.
    2. Strict mode on: nothing else.
    3. Off: a WARNING for every worker, naming the credentials that
       a task without ``task_env`` would inherit.
    4. Off and some variable of the list present (non-empty): ERROR naming it
       and ``sys.exit(1)`` before registering. With
       ``--restart unless-stopped`` the worker ends up in a crash loop without claiming
       any task, which is preferable to handing the variable to one.

    Match by exact name, never by pattern or value: a pattern
    like ``*_TOKEN`` would catch tokens the operator keeps in
    ``EXTRA_ENV`` on purpose. Only names are logged, never values.

    ``ORCH_ENFORCE_ISOLATION`` closes only the environment inheritance route (the
    full copy of legacy mode); it is not a sandbox against a task that
    reads files or the worker process, which runs with the same uid.
    """
    names = settings.isolation_required_names
    log.info(
        "startup: isolation-required variables checked: %s",
        ", ".join(names) if names else "(none)",
    )
    if settings.enforce_isolation:
        return
    held = [name for name in _WORKER_CREDENTIAL_ENV_NAMES if os.environ.get(name, "").strip()]
    log.warning(
        "startup: strict isolation is OFF (ORCH_ENFORCE_ISOLATION is not true). A task "
        "without task_env inherits the whole worker environment, including %s, and its "
        "output is stored on the server. Once no queued task lacks task_env, set "
        "ORCH_ENFORCE_ISOLATION=true and recreate the worker.",
        ", ".join(held) if held else "the enrolment token and the worker credential",
    )
    present = [name for name in names if os.environ.get(name, "").strip()]
    if not present:
        return
    log.error(
        "startup: worker holds isolation-required variable(s) %s with strict isolation OFF; "
        "refusing to start. Any task without task_env would inherit the whole worker "
        "environment and could read them. Either move the variable to the workspace's "
        "central task secret and remove it from the worker env (tasks that need it must "
        "declare it in task_env; drain or recreate queued tasks without it before removing "
        "it from the worker env, or they will run without it), or drain the queue of tasks "
        "without task_env, then set ORCH_ENFORCE_ISOLATION=true and recreate the worker (a "
        "restart does not re-read --env-file): on a fleet host, set ORCH_ENFORCE_ISOLATION=true "
        "in setup.env and rerun run-fleet.sh, which re-stamps /etc/orcaster/env.N; on a "
        "standalone worker, add it to client/.env and run `make up client`.",
        ", ".join(present),
    )
    sys.exit(1)


def _take_egress_key_from_environ(settings: Settings) -> None:
    """Removes ``ORCH_EGRESS_KEY`` from ``os.environ`` and logs the mode.

    ``Settings`` already holds the key. Without this ``pop`` a task in legacy mode
    (without ``task_env``) would inherit it with ``dict(os.environ)`` and could sign
    its own allowlist. The process's initial environment block
    (``/proc/<pid>/environ``) does not change, but the process is non-dumpable
    and the task cannot read it. It never logs the value.
    """
    os.environ.pop(EGRESS_KEY_ENV, None)
    if settings.egress_key_bytes is not None:
        log.info("startup: per-task egress allowlist is signed (%s set)", EGRESS_KEY_ENV)
    elif os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"):
        log.warning(
            "startup: %s not set: the per-task egress allowlist is written unsigned. "
            "A proxy with the key ignores it (tasks lose per-task egress); a proxy "
            "without it lets task code add hosts. Recreate with `make up client`.",
            EGRESS_KEY_ENV,
        )


def _sweep_orphan_egress_allowlist(allow_file: Path, key: bytes | None = None) -> None:
    """Clears at boot an orphaned per-task allowlist left by a hard kill.

    The two ``finally`` blocks of ``_run_external`` cover every in-process exit,
    but a SIGKILL (OOM kill, ``podman stop`` exceeding the grace period,
    power loss) during the open window runs neither: the dead task's
    allowlist stays on the egress volume, which survives the
    container recreate, and would govern whatever runs afterwards. This sweep
    runs at startup, before any fail-fast that may abort the
    process: the sidecar proxy keeps serving with that file even if the
    worker dies in a boot loop.

    It only acts if the file exists with entries: if it does not exist, it is not
    created (dev/CI without egress topology); the file is exclusively owned by the
    worker (the operator's base allowlist lives in
    ``ORCASTER_PROXY_BASE_ALLOW``, not here), so overwriting it at boot does not
    delete anyone's configuration.
    """
    try:
        stale = read_allowlist(allow_file)
    except OSError as e:
        log.warning("startup: could not read egress allowlist file %s: %s", allow_file, e)
        return
    if not stale:
        return
    log.warning(
        "startup: clearing orphaned per-task egress allowlist %s (hard-kill leftover): %r",
        allow_file,
        stale,
    )
    _clear_task_egress(allow_file, stale, key)


# --- Signature verification of signed tasks ---------------------------------


def _verify_poll_task_signature(task: PollTaskPayload, settings: Settings) -> bool:
    """Verifies Ed25519 over the 6 canonical fields of the task.

    Every task delivered by the server carries a signature; the
    flag `task.signed` is always True. The caller calls this function without
    inspecting `task.signed` (the "verify or not" decision no longer exists).

    Canonical message (lexicographic order):
        command, created_at, idempotency_key, payload, task_id, worker_id

    The worker_id that is signed = the effective recipient (the worker that
    polls, not target_worker_id nor the creator). This matches the
    worker_id field of PollTaskPayload - for CLAIM tasks the server re-signs
    at claim with the polling worker's UUID; for PUSH the original signature
    at INSERT already used target_worker_id, which is the same worker that polls.

    created_at is passed literally from the wire - the server serialized it with
    the "Z" suffix (Pydantic v2 default) and signed over that same representation.
    """
    if settings.ed25519_public_key_path is None:
        log.error(
            "task_id=%s signed=True but ED25519_PUBLIC_KEY_PATH not configured;"
            " task rejected (server will reclaim on lease timeout).",
            task.task_id,
        )
        return False

    if not task.signature:
        log.error(
            "task_id=%s signed=True but signature field missing; rejecting",
            task.task_id,
        )
        return False

    try:
        sig_bytes = base64.b64decode(task.signature)
        pub = load_public_key(settings.ed25519_public_key_path)
    except Exception as e:  # noqa: BLE001
        log.error(
            "task_id=%s failed to load pubkey or decode signature: %s",
            task.task_id,
            e,
        )
        return False

    msg = build_task_message(
        task_id=task.task_id,
        worker_id=task.worker_id,
        command=task.command if task.command is not None else "run_batch",
        payload=task.payload,
        idempotency_key=task.idempotency_key,
        created_at_iso=task.created_at,
    )
    ok = verify_signature(pub, msg, sig_bytes)
    if not ok:
        log.error("task_id=%s signature invalid; rejecting", task.task_id)
    return ok


# --- Runner with abandon awareness ------------------------------------------


async def _execute_with_abandon(
    command: str,
    args: list[str],
    busy: BusyState,
    grace_seconds: float = 5.0,
    stop: asyncio.Event | None = None,
) -> RunResult | None:
    """Launches a subprocess; if busy.abandon_event or stop is set, kills the
    process group and returns None. If the subprocess ends normally, returns
    the RunResult."""
    argv = [command, *args]
    popen_kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        import subprocess as _sp

        popen_kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["preexec_fn"] = os.setsid

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **popen_kwargs,
    )
    communicate_task = asyncio.create_task(proc.communicate())
    abandon_task = asyncio.create_task(busy.abandon_event.wait())
    stop_task = asyncio.create_task(stop.wait()) if stop is not None else None

    waitset: set[asyncio.Task[Any]] = {communicate_task, abandon_task}
    if stop_task is not None:
        waitset.add(stop_task)

    done, pending = await asyncio.wait(
        waitset,
        return_when=asyncio.FIRST_COMPLETED,
    )

    interrupted = (abandon_task in done) or (stop_task is not None and stop_task in done)
    if interrupted and communicate_task not in done:
        log.info(
            "abandon signal received; killing process group pid=%s grace=%.2fs",
            proc.pid,
            grace_seconds,
        )
        await asyncio.to_thread(kill_process_group, proc, grace_seconds)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(communicate_task, timeout=max(grace_seconds, 1.0) + 1.0)
        for p in pending:
            p.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await p
        return None

    for p in pending:
        p.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await p
    stdout_b, stderr_b = communicate_task.result()
    return RunResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


# --- Safe complete -----------------------------------------------------------


async def _safe_complete_poll(
    api: OrchestratorAPI,
    task: PollTaskPayload,
    *,
    status: str,
    exit_code: int | None,
    cancelled_reason: str | None = None,
) -> None:
    try:
        await api.complete(
            task.task_id,
            lease_token=task.lease_token,
            exit_code=exit_code,
            status=status,
            cancelled_reason=cancelled_reason,
        )
        log.info(
            "Completed task_id=%s status=%s exit_code=%s",
            task.task_id,
            status,
            exit_code,
        )
    except LeaseLostError:
        log.warning("Lease lost for task_id=%s; output discarded.", task.task_id)
    except ShutdownError:
        log.warning(
            "Shutdown during complete for task_id=%s; lease will expire server-side.",
            task.task_id,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "complete() failed for task_id=%s: %s; housekeeping will reclaim the lease.",
            task.task_id,
            e,
        )


# --- Dispatcher of tasks received via /poll ---------------------------------


async def _dispatch_poll_task(
    api: OrchestratorAPI,
    settings: Settings,
    busy: BusyState,
    stop: asyncio.Event,
    task: PollTaskPayload,
) -> None:
    """Top-level routing: signature verification + idempotency + dispatch.

    1. Every task is signed. If the signature does not verify,
       it is rejected silently (log + return). complete() is NOT called,
       not even with failed: an attacker on the server could issue
       invalid signatures to force legitimate tasks to be marked `failed`
       and close the audit. The server reclaims the lease on expiry
       (housekeeping); another worker will claim it with a valid signature if the
       original task was legitimate.
    2. If idempotency_key was already executed -> resend the stored result
       without re-executing.
    3. If ``secrets`` arrived malformed -> complete failed/exit_code=2.
    4. If command is in RESERVED_COMMANDS -> in-process handler.
    5. Otherwise -> _run_external (ephemeral venv + dispatch_run_batch).
    """
    # 1. Signature verification (every task is signed).
    if not _verify_poll_task_signature(task, settings):
        log.warning(
            "task_id=%s worker_id=%s: signature verification failed; "
            "rejecting silently. Server will reclaim lease on expiry.",
            task.task_id,
            task.worker_id,
        )
        return

    # 2. Idempotency.
    if task.idempotency_key is not None:
        prior = get_push_result(settings.state_dir, task.idempotency_key)
        if prior is not None:
            log.info(
                "task_id=%s idempotency_key=%s already executed; re-sending complete",
                task.task_id,
                task.idempotency_key,
            )
            await _safe_complete_poll(
                api,
                task,
                status=prior["status"],
                exit_code=prior.get("exit_code"),
            )
            return

    # 3. Malformed `secrets`: visible failure, same as a malformed external
    # run_batch payload. Running the task without the secrets the server meant to
    # deliver would make it fail later with a misleading error, or worse,
    # silently use the old EXTRA_ENV values.
    if task.secrets_error is not None:
        log.warning("task_id=%s rejected: %s", task.task_id, task.secrets_error)
        await _emit_synthetic_output(api, settings, task, stderr=task.secrets_error)
        await _safe_complete_poll(api, task, status="failed", exit_code=2)
        return

    # 4. Reserved (in-process control op).
    effective_command = task.command if task.command is not None else "run_batch"
    if effective_command in RESERVED_COMMANDS:
        await _run_reserved(api, settings, task)
        return

    # 5. Work task -> subprocess (ephemeral venv + dispatch_run_batch).
    await _run_external(api, settings, busy, stop, task)


async def _run_reserved(
    api: OrchestratorAPI,
    settings: Settings,
    task: PollTaskPayload,
) -> None:
    """Runs a reserved command in-process and reports the result."""
    effective_command = task.command if task.command is not None else "run_batch"
    log.info("dispatching reserved command=%s task_id=%s", effective_command, task.task_id)
    try:
        result = await handle_reserved(effective_command, task.payload, settings=settings)
    except Exception as e:  # noqa: BLE001
        log.exception("reserved handler crashed for command=%s", task.command)
        if task.idempotency_key is not None:
            store_push_result(
                settings.state_dir,
                task.idempotency_key,
                status="failed",
                exit_code=1,
                redactor=_task_redactor(task),
            )
        await _emit_synthetic_output(api, settings, task, stderr=f"handler crashed: {e!r}")
        await _safe_complete_poll(
            api,
            task,
            status="failed",
            exit_code=1,
        )
        return

    if task.idempotency_key is not None:
        store_push_result(
            settings.state_dir,
            task.idempotency_key,
            status=result.status,
            exit_code=result.exit_code,
            result=result.result,
            redactor=_task_redactor(task),
        )
    await _emit_synthetic_output(
        api, settings, task, stdout=result.stdout or None, stderr=result.stderr or None
    )
    await _safe_complete_poll(
        api,
        task,
        status=result.status,
        exit_code=result.exit_code,
    )


async def _run_external(
    api: OrchestratorAPI,
    settings: Settings,
    busy: BusyState,
    stop: asyncio.Event,
    task: PollTaskPayload,
) -> None:
    """Runs an external task in an ephemeral venv.

    Payload contract:
        {"requirements": [<PEP 508 specs>], "commands": [<shell strings>]}

    Steps:
        1. Validate the payload; on failure -> complete failed/exit_code=2.
        2. ``_apply_task_egress``: opens the per-task egress window, which
           covers the venv setup and the commands.
        3. ``async with ephemeral_venv(...)``: creates the venv + installs deps.
        4. ``dispatch_run_batch(commands, env=venv.env, ...)`` runs the
           shell strings inside the venv (PATH and VIRTUAL_ENV injected).
        5. ``_clear_task_egress`` closes the window as soon as the dispatch ends
           (and also on the venv setup failure paths).
        6. Automatic venv cleanup.

    If ``busy.abandon_event`` is set during venv creation/install,
    ``ephemeral_venv`` raises ``VenvAbandonedError`` and we report cancelled.
    If it is set during ``dispatch_run_batch``, the subprocesses are left
    orphaned from the venv but the context manager's cleanup removes them.

    ``busy.task_id``/``lease_token`` are already set by poll_loop before
    spawning the executor_task; ``busy.reset()`` is done by the reaper.
    """
    log.info("Claimed task_id=%s command=%s", task.task_id, task.command)

    parsed = _extract_task_payload(task.payload)
    if parsed is None:
        log.warning(
            "task_id=%s rejected: %s; received keys=%s",
            task.task_id,
            _PAYLOAD_ERROR,
            sorted(task.payload.keys()) if isinstance(task.payload, dict) else "<non-dict>",
        )
        await _emit_synthetic_output(api, settings, task, stderr=_PAYLOAD_ERROR)
        await _safe_complete_poll(
            api,
            task,
            status="failed",
            exit_code=2,
        )
        return
    requirements, commands, timeout_seconds = parsed

    # Single streamer for the whole execution: venv setup + run_batch share
    # the same seq counter and the same backlog channel. It is created before
    # ephemeral_venv so the synthetic diagnostics (cancelled before
    # run_batch, venv errors) reuse the same seq and do not collide with the
    # server's ON CONFLICT.
    streamer = _make_streamer(api, settings, task)
    streamer.start()
    # Publish in BusyState so the reaper continues the seq sequence if
    # the executor crashes with output already streamed.
    busy.streamer = streamer

    # Outer timeout: if the task defines execution_timeout_seconds, schedule a
    # timer that fires abandon_event on expiry. The timer covers venv setup +
    # run_batch (total wall-clock of the execution). If the subprocess ends
    # earlier, the timer is cancelled cleanly.
    outer_timeout_task: asyncio.Task[None] | None = None
    outer_timed_out = False

    async def _outer_timeout_trigger(n: int) -> None:
        nonlocal outer_timed_out
        await asyncio.sleep(n)
        outer_timed_out = True
        log.warning(
            "[execution] outer timeout exceeded after %d seconds for task_id=%s",
            n,
            task.task_id,
        )
        busy.abandon_event.set()

    if task.execution_timeout_seconds is not None:
        outer_timeout_task = asyncio.create_task(
            _outer_timeout_trigger(task.execution_timeout_seconds),
            name=f"outer-timeout-{task.task_id}",
        )

    artifacts_dir: Path | None = None
    cache_dir: Path | None = None
    artifacts_spec = ArtifactsSpec.from_payload(task.payload)

    # Self-contained worker: we resolve the job_key and clear the cache of
    # any PREVIOUS job before creating the venv, so the execution
    # starts with a clean worker and all resources available. The incoming
    # job's dir is kept (only reuse: retry of the same job after a failure).
    job_key = resolve_job_key(
        idempotency_key=task.idempotency_key,
        task_id=task.task_id,
    )
    # Publish the live job BEFORE touching the cache: cache_gc_loop runs in
    # parallel and needs to know which dir it must not delete. The GC re-reads this
    # variable right before each deletion, so publishing it here narrows the
    # window to a single deletion instead of the whole pass. The remaining window
    # is NOT microseconds: if the GC already started the rmtree of this same dir (it
    # chose it when there was no live job), publishing the key does not stop it. That
    # is closed by the GC's detach, not by this order: the GC removes the tree from
    # the namespace before deleting it, so the `ensure_task_cache_dir` call below
    # creates a fresh dir that the in-flight deletion can no longer reach.
    busy.active_job_key = job_key
    cache_root = settings.resolved_cache_root()
    # rmtree can be multi-GB: we offload it to a thread so as not to freeze the
    # event loop (same convention as the other heavy rmtrees in the worker).
    abort_reason: str | None = None
    try:
        reset_stats = await asyncio.to_thread(
            reset_other_job_caches, cache_root, keep_job_key=job_key
        )
    except Exception as e:  # noqa: BLE001
        reset_stats = ResetStats(dirs_deleted=0, bytes_freed_est=0, failed_dirs=())
        abort_reason = f"cache reset raised an exception: {e}"
        log.error("task_id=%s %s", task.task_id, abort_reason)
    if abort_reason is None and reset_stats.failed_dirs:
        offenders = ", ".join(str(p) for p in reset_stats.failed_dirs)
        abort_reason = f"could not clear the cache of a previous job: {offenders}"
    if abort_reason is not None:
        # Reset failure = abort: we do not execute on a dirty worker.
        # Cancel the outer-timeout timer BEFORE returning; otherwise it
        # stays orphaned and would spuriously abort the worker's next task.
        if outer_timeout_task is not None:
            outer_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outer_timeout_task
        log.error(
            "task_id=%s %s; aborting.",
            task.task_id,
            abort_reason,
        )
        await _emit_synthetic_output(
            api,
            settings,
            task,
            stderr=f"worker aborted: {abort_reason}",
            streamer=streamer,
        )
        await _safe_complete_poll(api, task, status="failed", exit_code=None)
        return

    # uv per-job: UV_CACHE_DIR lives inside the job's dir unless there is an explicit
    # worker-wide override (settings.uv_cache_dir).
    try:
        cache_dir = ensure_task_cache_dir(cache_root, job_key)
    except OSError as e:
        cache_dir = None
        log.warning(
            "task_id=%s could not prepare cache dir: %s; ORCASTER_CACHE_DIR will not be set.",
            task.task_id,
            e,
        )
    if settings.uv_cache_dir is not None:
        uv_dir: Path | None = settings.uv_cache_dir
    elif cache_dir is not None:
        uv_dir = cache_dir / "uv"
    else:
        uv_dir = None

    # Env/egress isolation: task_env restricts the scrubbed env
    # to the vars declared in the payload; egress_allowlist is
    # written to settings.egress_allow_file before the venv setup (see
    # _apply_task_egress) so the sidecar proxy applies it per request
    # during the WHOLE execution: `uv venv`/`uv pip install` included.
    # `_resolve_isolation` wraps the extraction with the rollout flag
    # `settings.enforce_isolation` (ORCH_ENFORCE_ISOLATION): it promotes
    # an absent key to strict (`[]`) once the old queue has drained.
    task_env, egress_allowlist = _resolve_isolation(
        task.payload, enforce=settings.enforce_isolation
    )
    # Server secrets: same `task_env` in all modes, with
    # the server's value taking precedence over the worker environment. The
    # origin of each name is recorded, never values nor the text of an entry that is not
    # a valid name or carries a token prefix, unless that
    # name exists in the worker environment or in the server secrets: then
    # it is listed by origin and `render_names` escapes whatever has no name shape.
    # Names with a value from a row of the worker itself go in a
    # separate token (`server_worker_scoped=[...]`), names only.
    if task.worker_scoped_secrets_error is not None:
        log.warning("task_id=%s %s", task.task_id, task.worker_scoped_secrets_error)
    env_sources = describe_env_sources(
        task_env, secrets=task.secrets, worker_scoped=task.worker_scoped_secrets
    )
    log.info("task_id=%s task_env sources: %s", task.task_id, env_sources.render())
    if env_sources.ignored:
        log.warning(
            "task_id=%s server secrets not injected (not declared in task_env or legacy mode): %s",
            task.task_id,
            render_names(env_sources.ignored),
        )
    if env_sources.refused_server:
        log.warning(
            "task_id=%s server secrets refused for reserved env names (worker value applies): %s",
            task.task_id,
            render_names(env_sources.refused_server),
        )

    try:
        # Open the per-task egress window BEFORE ephemeral_venv:
        # a requirement declared by URL (github.com tarball,
        # `git+https://...`) has to see the same allowlist as the task's
        # commands. It widens nothing: it is exactly the allowlist that already
        # governed the dispatch. It goes INSIDE the try so the `finally` below
        # closes the window on every setup failure path.
        _apply_task_egress(settings.egress_allow_file, egress_allowlist, settings.egress_key_bytes)
        async with ephemeral_venv(
            execution_id=str(task.task_id),
            requirements=requirements,
            uv_bin=settings.uv_bin,
            uv_cache_dir=uv_dir,
            task_env=task_env,
            secrets=task.secrets,
            abandon_event=busy.abandon_event,
        ) as venv:
            if busy.abandon_event.is_set() or stop.is_set():
                # Close the egress window BEFORE reporting to the server and
                # before the `return` triggers the venv cleanup: this
                # path exits without going through the dispatch's inner finally, and
                # without this the allowlist would stay in force during the rmtree
                # of the venv and the output/complete retries (a process
                # left behind by an install build hook could keep reaching
                # the task's hosts in that window).
                _clear_task_egress(
                    settings.egress_allow_file, egress_allowlist, settings.egress_key_bytes
                )
                await _emit_synthetic_output(
                    api,
                    settings,
                    task,
                    stderr="cancelled before run_batch",
                    streamer=streamer,
                )
                await _safe_complete_poll(
                    api,
                    task,
                    status="cancelled",
                    exit_code=None,
                )
                return

            # Pre-set ecosystem variables: HF_HOME / PIP_CACHE_DIR
            # / XDG_CACHE_HOME point to subdirs of ORCASTER_CACHE_DIR. The ephemeral
            # cwd (<cache_root>/run/<unique>/) is created below and deleted after the
            # dispatch.
            cmd_env = dict(venv.env)
            if cache_dir is not None:
                try:
                    _, prepared_artifacts_dir = _prepare_cache_env(
                        cmd_env,
                        cache_root=cache_root,
                        job_key=job_key,
                    )
                    if artifacts_spec.enabled:
                        artifacts_dir = prepared_artifacts_dir
                except OSError as e:
                    # Graceful degradation: keep running without the cache.
                    # cache_dir=None avoids delete-after-success on a dir in an
                    # inconsistent state. _prepare_cache_env may have partly mutated
                    # cmd_env before raising (e.g. it set ORCASTER_CACHE_DIR and then
                    # the artifacts mkdir failed): clear all five keys so the
                    # subprocess does not receive partial or inconsistent cache env vars.
                    cache_dir = None
                    for _k in (
                        "ORCASTER_CACHE_DIR",
                        "HF_HOME",
                        "PIP_CACHE_DIR",
                        "XDG_CACHE_HOME",
                        "ORCASTER_ARTIFACTS_DIR",
                    ):
                        cmd_env.pop(_k, None)
                    log.warning(
                        "task_id=%s could not prepare cache env: %s; "
                        "cache env vars will not be set.",
                        task.task_id,
                        e,
                    )

            run_dir: Path | None = None
            try:
                run_dir = create_execution_run_dir(
                    settings.resolved_cache_root(), task_id=task.task_id
                )
            except OSError as e:
                log.warning(
                    "task_id=%s could not create ephemeral run dir: %s; "
                    "subprocess will inherit the worker cwd.",
                    task.task_id,
                    e,
                )

            dispatcher_payload: dict[str, Any] = {"commands": commands}
            if timeout_seconds is not None:
                dispatcher_payload["timeout_seconds"] = timeout_seconds

            # Put the disk preflight scanner between the dispatcher and the
            # streamer. It forwards every chunk intact to `streamer.feed` (the
            # canonical on_output sink) and only records whether it saw the marker;
            # the flush decision is made below, once the batch status is known.
            disk_scanner = _DiskPreflightScanner(
                streamer.feed, parse_flush_markers(settings.flush_markers)
            )
            try:
                status_str, result = await dispatch_run_batch(
                    dispatcher_payload,
                    settings,
                    env=cmd_env,
                    cwd=str(run_dir) if run_dir is not None else None,
                    abandon_event=busy.abandon_event,
                    on_output=disk_scanner.feed,
                )
            finally:
                # Close the egress window as soon as the task ends (legacy:
                # egress_allowlist is None -> _clear_task_egress does not touch the
                # file). Catch and log instead of letting a write failure (disk
                # full/permissions) propagate over the real result of
                # dispatch_run_batch and end up marking the task as "runner crashed".
                _clear_task_egress(
                    settings.egress_allow_file, egress_allowlist, settings.egress_key_bytes
                )
                if run_dir is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(shutil.rmtree, run_dir, True)  # ignore_errors=True
                # Always with a deadline: without it, a server that goes down right
                # as the execution ends would block the drain (and the worker poll)
                # indefinitely. Outside shutdown the deadline is the lease timeout,
                # the real reliability window of streaming: a longer outage expires
                # the lease and the backlog would be discarded anyway with a 409.
                await streamer.aclose(
                    drain_deadline=_DRAIN_DEADLINE_SECONDS
                    if stop.is_set()
                    else float(settings.lease_seconds)
                )
    except VenvAbandonedError:
        if outer_timeout_task is not None:
            outer_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outer_timeout_task
        if outer_timed_out:
            # Outer timeout is a terminal cancel with the fixed reason
            # `outer_timeout`, not a `failed`.
            log.info(
                "task_id=%s outer timeout during venv setup; reporting cancelled.",
                task.task_id,
            )
            await _emit_synthetic_output(
                api,
                settings,
                task,
                stderr=(
                    "[execution] outer timeout exceeded after "
                    f"{task.execution_timeout_seconds} seconds"
                ),
                streamer=streamer,
            )
            await _safe_complete_poll(
                api,
                task,
                status="cancelled",
                exit_code=None,
                cancelled_reason="outer_timeout",
            )
        else:
            log.info("task_id=%s venv abandoned; reporting cancelled.", task.task_id)
            await _emit_synthetic_output(
                api,
                settings,
                task,
                stderr="cancelled by abandon signal during venv setup",
                streamer=streamer,
            )
            await _safe_complete_poll(
                api,
                task,
                status="cancelled",
                exit_code=None,
            )
        return
    except (VenvCreationError, VenvInstallError) as e:
        if outer_timeout_task is not None:
            outer_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outer_timeout_task
        # The uv error may quote the environment: it is masked in the log too.
        redactor = _task_redactor(task)
        log.warning(
            "task_id=%s venv setup failed: %s",
            task.task_id,
            redactor.redact(str(e)) if redactor is not None else e,
        )
        await _emit_synthetic_output(
            api,
            settings,
            task,
            stderr=f"venv setup failed: {e}",
            streamer=streamer,
        )
        await _safe_complete_poll(
            api,
            task,
            status="failed",
            exit_code=1,
        )
        return
    except Exception as e:  # noqa: BLE001
        if outer_timeout_task is not None:
            outer_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outer_timeout_task
        log.exception("runner crashed")
        if not busy.abandon_event.is_set():
            await _emit_synthetic_output(
                api,
                settings,
                task,
                stderr=f"runner error: {e!r}",
                streamer=streamer,
            )
            await _safe_complete_poll(
                api,
                task,
                status="failed",
                exit_code=1,
            )
        return
    finally:
        # Backup close of the egress window. The per-task allowlist is now opened
        # before ephemeral_venv, so the setup failure paths (VenvAbandonedError,
        # VenvCreationError/VenvInstallError, outer timeout during setup, generic
        # crash) must close it too: otherwise it would stay in force and orphaned
        # for the next task. On the happy path the inner finally already closed it
        # as soon as the dispatch ended (the "close the window as soon as the task
        # ends" contract is not relaxed) and repeating the clear changes nothing:
        # it rewrites the same empty allowlist. It goes FIRST in the finally: it is
        # a synchronous call that cannot fail (fail-open, see _clear_task_egress),
        # so it is not at the mercy of the `await` below if the executor is being
        # cancelled.
        _clear_task_egress(settings.egress_allow_file, egress_allowlist, settings.egress_key_bytes)
        # Always cancel the timer if it is still active (the subprocess finished
        # before the timeout or we left through an exception handled above).
        if outer_timeout_task is not None and not outer_timeout_task.done():
            outer_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outer_timeout_task

    if outer_timed_out or (status_str == "abandoned" and busy.abandon_event.is_set()):
        if outer_timed_out:
            # Outer timeout reports `cancelled` with reason `outer_timeout`.
            log.info(
                "task_id=%s outer timeout exceeded; reporting cancelled.",
                task.task_id,
            )
            await _emit_synthetic_output(
                api,
                settings,
                task,
                stderr=(
                    f"[execution] outer timeout exceeded after "
                    f"{task.execution_timeout_seconds} seconds"
                ),
                streamer=streamer,
            )
            await _safe_complete_poll(
                api,
                task,
                status="cancelled",
                exit_code=None,
                cancelled_reason="outer_timeout",
            )
        else:
            log.info("task_id=%s abandoned during run_batch; reporting cancelled.", task.task_id)
            await _emit_synthetic_output(
                api,
                settings,
                task,
                stderr="cancelled by abandon signal",
                streamer=streamer,
            )
            await _safe_complete_poll(
                api,
                task,
                status="cancelled",
                exit_code=None,
            )
        return

    if status_str == "abandoned" or busy.abandon_event.is_set():
        log.info("task_id=%s abandoned during run_batch; reporting cancelled.", task.task_id)
        await _emit_synthetic_output(
            api,
            settings,
            task,
            stderr="cancelled by abandon signal",
            streamer=streamer,
        )
        await _safe_complete_poll(
            api,
            task,
            status="cancelled",
            exit_code=None,
        )
        return

    exit_codes = result.get("exit_codes") or []
    last_exit = exit_codes[-1] if exit_codes else 0
    out_status = "completed" if status_str == "ok" else "failed"

    # If the batch died in the disk preflight, request the cache self-flush.
    # The poll_loop consumes it when idle and deletes EVERYTHING, so the retry
    # passes the preflight without operator intervention.
    if _should_self_flush_on_disk_preflight(settings, status_str, disk_scanner.marker_seen):
        log.info(
            "task_id=%s: disk preflight detected; requesting cache self-flush.",
            task.task_id,
        )
        busy.self_flush_requested = True

    # Artifacts produced by the task: collect and upload BEFORE /complete.
    # After /complete the execution is terminal and the endpoint would return 409.
    artifacts_ok = True
    if artifacts_spec.enabled and artifacts_dir is not None:
        artifacts_ok = await _upload_artifacts(
            api,
            task_id=task.task_id,
            lease_token=task.lease_token,
            artifacts_dir=artifacts_dir,
            spec=artifacts_spec,
            max_request_bytes=settings.artifact_request_max_bytes,
            upload_timeout=settings.artifact_upload_timeout,
        )
        if not artifacts_ok and artifacts_spec.required:
            await _emit_synthetic_output(
                api,
                settings,
                task,
                stderr=(
                    "[artifacts] required artifact upload failed; reporting execution as failed"
                ),
                streamer=streamer,
            )

    out_status = _finalize_status(
        out_status, artifacts_ok=artifacts_ok, required=artifacts_spec.required
    )
    await _safe_complete_poll(
        api,
        task,
        status=out_status,
        exit_code=last_exit,
    )

    # Delete the task-scoped cache after a successful execution. Safe with
    # respect to reuse (retries only happen after a failure; a `completed` task
    # is not retried) and the artifacts were already uploaded above, before
    # /complete. Best-effort: the task is already reported, so an rmtree failure
    # does not affect it; the periodic GC recovers it anyway. Only on real
    # success; on `failed`/`cancelled` it is kept so a retry can reuse it.
    if settings.cache_delete_on_success and out_status == "completed" and cache_dir is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(shutil.rmtree, cache_dir, True)  # ignore_errors=True
        log.info("task_id=%s cache deleted on success: %s", task.task_id, cache_dir)


# --- Poll loop --------------------------------------------------------------


# Grace period to wait for the executor_task to finish cooperatively after
# SIGTERM/SIGINT before cancelling it via asyncio. 10 s is enough for
# `_execute_with_abandon` to do its internal SIGTERM -> SIGKILL escalation
# (default cancel_grace_seconds=5s plus a margin to flush stdout/stderr and emit
# the complete). Higher would make the worker slow to shut down; lower risks
# killing it before the subprocess releases its resources.
GRACE_SECONDS: float = 10.0


def _clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


async def _cooperative_sleep_stop(delay: float, stop: asyncio.Event) -> None:
    """Sleep that can be cancelled by stop. Does not raise; it just returns early."""
    if delay <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)


def _process_cancel_signals(
    signals: list[PollCancelSignal],
    busy: BusyState,
) -> None:
    """If any signal matches the (task_id, lease_token) of the current busy state, set
    abandon_event. Other signals (for executions that are already dead) are ignored.

    We match on ``task_id`` / ``lease_token`` directly (not on ``is_busy``); the
    executor_task may or may not have started yet when a cancel arrives, but if the
    identifiers match we set the abandon_event so the dispatch honors it as soon as
    it checks.
    """
    if busy.task_id is None or busy.lease_token is None:
        return
    for sig in signals:
        if sig.task_id == busy.task_id and sig.lease_token == busy.lease_token:
            log.info(
                "server signals cancel for task_id=%s lease_token=%s",
                sig.task_id,
                sig.lease_token,
            )
            busy.abandon_event.set()
            return


async def _reap_executor_task(
    api: OrchestratorAPI,
    busy: BusyState,
    settings: Settings | None = None,
) -> None:
    """If the executor_task finished, read its result/exception and clear the
    BusyState.

    Cases:
        - Normal completion: the dispatch already called ``complete()`` inside; here
          we only clear the state.
        - Unhandled exception (executor crash): structured log + report
          ``failed/exit_code=2/stderr="executor_crashed: <Type>"`` to the server.
          Without this, the execution stays hanging on the server until housekeeping
          reclaims it by lease timeout (and another worker could run the task twice).
          The diagnostic reuses ``busy.streamer`` if it exists to continue the
          ``seq`` sequence.
        - Cancelled (CancelledError): swallow, do not report; happens during
          cooperative shutdown when we exceeded GRACE_SECONDS.
    """
    task = busy.executor_task
    if task is None or not task.done():
        return

    payload = busy.task_payload
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        exc = None  # treat cancel as a silent terminal state
    except Exception as e:  # noqa: BLE001
        log.warning("error reading executor_task exception: %s", e)
        exc = None

    if exc is not None and not isinstance(exc, asyncio.CancelledError):
        log.error(
            "executor_task crashed for task_id=%s: %s",
            payload.task_id if payload else "<unknown>",
            repr(exc),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if payload is not None:
            if settings is not None:
                await _emit_synthetic_output(
                    api,
                    settings,
                    payload,
                    stderr=f"executor_crashed: {type(exc).__name__}",
                    streamer=busy.streamer,
                )
            await _safe_complete_poll(
                api,
                payload,
                status="failed",
                exit_code=2,
            )

    busy.reset()


async def poll_loop(
    api: OrchestratorAPI,
    settings: Settings,
    busy: BusyState,
    stop: asyncio.Event,
    *,
    gc_wakeup: asyncio.Event | None = None,
) -> None:
    """Unified main loop (`/poll` endpoint + executor in a separate task).

    Each iteration:
        0. Reap the previous executor_task if it is ``done()``.
        1. POST /poll with telemetry + the lease_token of the current busy state.
        2. Process cancel_signals against busy.
        3. If a task came and we are not busy, spawn ``executor_task`` with
           ``asyncio.create_task(_dispatch_poll_task(...))``. We do NOT wait: we
           return to the loop to keep emitting polls (implicit heartbeats against
           lease expiration).
        4. Sleep next_poll_after_seconds clamped to [min, max].

    Network errors: multiplicative backoff up to poll_max_interval_seconds.

    Disk gate
    ---------
    While idle, if ``free/total`` of the cache volume falls below
    ``settings.resolved_claim_min_free_fraction``, the poll reports ``busy`` and the
    worker stops claiming. Before this, nothing on the worker side gated on free
    space (``detect_disk`` only reported telemetry) and on a host with several
    workers on the same cache volume, a long job on a neighbor left the rest
    starting doomed downloads that died in ``uv venv`` with
    ``No space left on device``, burning an attempt per task.

    The gate is on the poll and not on the dispatch on purpose: failing the task
    that was already assigned also burns the attempt, and it would do so on false
    positives (a small task that did fit). Not claiming costs nothing. The residual
    window (the disk drops between the idle poll and the dispatch) is accepted: the
    task that already arrived is dispatched normally.

    Closing the gate asks the GC for an immediate pass (``gc_wakeup``): the gate
    detects exactly the condition that the ``run_gc`` pressure pass exists to fix,
    and waiting for the GC interval (300 s by default) leaves the worker idle for up
    to five minutes before the only mechanism that can free space runs. Without the
    event (tests) the GC keeps its own cadence.

    The gate has a time-based fail-open: if it accumulates more than
    ``settings.cache_claim_gate_max_seconds`` (30 min by default, several GC passes)
    holding back claims, the worker claims again even if the disk is still below
    the floor. The deadline counts only the time in which the gate is actually
    holding back claims and it is paused (not reset) while the worker is busy:
    counting it over the bare disk state meant that a task longer than the deadline
    would latch the fail-open without the gate having blocked anything, because the
    ``busy`` of those minutes was set by the task, and when it finished the worker
    would claim immediately with the volume full. Pausing rather than resetting
    matters: with a reset, a worker with interleaved pushes would never give up.
    The ``Settings`` validator covers a misconfigured floor, but not this other path
    to a permanent gate: ``run_gc`` only deletes dirs under ``cache_root/tasks/``,
    so a worker-wide uv cache, model blobs or any other content on the same volume
    keep it below the floor forever. With the gate on by default, that takes
    workers out of service leaving as the only trace a WARNING that, by the time
    someone investigates, is already stale. Once the deadline expires it is
    preferable that tasks fail with the real cause (which is what happened before
    this feature) than a silently idle fleet.

    Two consequences of using ``busy`` as the signal, both accepted:

    * The server's ``clear_cache`` lane is only delivered to ``idle`` workers
      (``server/orcaster_server/api/poll.py``), so a gated worker cannot be
      flushed from the dashboard. In the common case this is harmless (the worker
      is idle and its GC already emptied ``tasks/``); the case that does remain
      without a way out through this route is a huge worker-wide uv cache, which
      the GC never touches. The time-based fail-open unlocks it by itself: when the
      deadline expires the worker reports ``idle`` again and the flush lane can be
      delivered to it again. Immediate escape hatch:
      ``ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0`` or freeing disk on the host.
      The complete solution is to propagate the signal to the server (instead of
      reporting ``busy``) so that it holds back tasks as ``storage_paused`` does,
      without closing the flush lane.
    * Directed push tasks are delivered even if the worker reports ``busy`` (step
      4 of the pick), so a task pinned to a gated worker arrives anyway. It is
      dispatched: the gate decides whether we ASK for work, not whether we run the
      work that already arrived, the same policy as the residual window above. The
      task runs and fails once with the real cause, which is preferable to ignoring
      it and churning an attempt per lease period.

    Cadence: a worker that reports ``busy`` receives ``next_poll=5`` instead of 30,
    but that cadence exists for a live execution (renewing the lease, reporting
    progress) and a gated worker has none. Since the gate fires on pressure of a
    shared volume, all containers on the host cross the floor together: accepting it
    would mean 6x the polls and DB work during the whole fail-open window, precisely
    while nothing advances. When the reported ``busy`` comes only from the gate, the
    worker stays on its local idle cadence (``poll_max_interval_seconds``). Accepted
    cost: recovery is noticed up to 30 s later instead of up to 5 s, irrelevant
    compared with the fail-open deadline and the GC interval.
    """
    next_interval = 0.0  # first poll immediately
    # Single instance: CpuSampler is stateful (it keeps the previous sample for the
    # delta). If it were instantiated inside the loop, sample() would always return
    # None.
    cpu_sampler = CpuSampler()
    # Previous disk state, to log the gate only on the transition.
    prev_disk_below_floor = False
    # Time-based fail-open of the gate: pausable stopwatch of the time the gate has
    # spent holding back claims, and a "we already gave up" flag. `since` is the
    # instant the current stretch started (None = paused), `accum` the stretches
    # already closed. Monotonic clock and not `datetime` because this measures a
    # duration, not a timestamp that gets persisted: it must not depend on the wall
    # clock (an NTP jump would open or close the gate).
    gate_blocking_since: float | None = None
    gate_blocked_seconds = 0.0
    gate_fail_open = False

    while not stop.is_set():
        if next_interval > 0:
            await _cooperative_sleep_stop(next_interval, stop)
            if stop.is_set():
                break

        # Reap the executor_task from the previous cycle.
        await _reap_executor_task(api, busy, settings)
        # Cache flush: reap if it already finished.
        _reap_cache_flush_task(busy)

        ram = detect_ram()
        disk = detect_disk(settings.resolved_cache_root())
        cpu_pct = cpu_sampler.sample()

        # Disk gate: below the floor, we do not claim. `busy` is what the protocol
        # has to say "do not give me work": steps 5 and 6 of the server's pick
        # (pre-assignment and open-claim) only run with `status='idle'`. It reuses
        # the state that already exists for the flush, which also reports `busy`
        # without a live task. Without a measurement we do not gate: no longer
        # taking tasks because we could not measure would be worse than the problem.
        floor = settings.resolved_claim_min_free_fraction
        disk_below_floor = (
            floor > 0.0 and disk.free_fraction is not None and disk.free_fraction < floor
        )
        # Loss of measurement (statvfs failed, volume unmounted, etc.): it also turns
        # off `disk_below_floor`, just like a real recovery of space. It is
        # distinguished so we do not announce a recovery that did not happen: the
        # fail-open policy is still to claim without a measurement, but the log must
        # say why the gate opened.
        measurement_lost = floor > 0.0 and disk.free_fraction is None

        # Time-based fail-open. The deadline measures time holding back claims, not
        # time with the disk full: while the worker is busy the gate blocks nothing
        # (the `busy` is set by the task or the flush), so the stopwatch is paused.
        # Counting it over the bare `disk_below_floor` latched the fail-open during a
        # task longer than the deadline, and when it finished the worker claimed
        # immediately with the volume full: the failure the gate was meant to avoid.
        # It is paused and not reset: with a reset, a worker with interleaved
        # directed pushes would never accumulate the deadline and would never give up.
        #
        # Losing the measurement turns off `disk_below_floor` and thereby resets the
        # stopwatch. That is acceptable: without a measurement the gate was already
        # open (the worker was claiming), so there was no held-back time to
        # accumulate. What it does NOT revoke is the latch: only a measured recovery
        # does that (below).
        now = time.monotonic()
        gate_blocking = (
            disk_below_floor and not gate_fail_open and not busy.is_busy and not busy.is_flushing
        )
        if gate_blocking:
            if gate_blocking_since is None:
                gate_blocking_since = now
            closed_for = gate_blocked_seconds + (now - gate_blocking_since)
        else:
            if gate_blocking_since is not None:
                gate_blocked_seconds += now - gate_blocking_since
                gate_blocking_since = None
            closed_for = gate_blocked_seconds
        if disk_below_floor:
            if (
                settings.cache_claim_gate_max_seconds > 0.0
                and closed_for >= settings.cache_claim_gate_max_seconds
                and not gate_fail_open
            ):
                # One WARNING and no more: the flag stays latched while the disk
                # remains below the floor, so it neither re-warns nor oscillates per
                # tick.
                log.warning(
                    "disk gate opened after the deadline expired without recovery: "
                    "it spent %.0f s holding back claims (max=%.0f s) and free/total "
                    "is still at %.3f < min=%.2f on %s; the worker claims again and "
                    "tasks will fail for lack of space, which is preferable "
                    "to a silently idle fleet "
                    "(ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=0 disables it)",
                    closed_for,
                    settings.cache_claim_gate_max_seconds,
                    disk.free_fraction,
                    floor,
                    settings.resolved_cache_root(),
                )
                gate_fail_open = True
        else:
            gate_blocking_since = None
            gate_blocked_seconds = 0.0
            # The latch is revoked by a measured recovery, not by a flap of the volume.
            # Losing the measurement goes through this same branch (a remount, a
            # mount flap, the container restarted while the volume attaches), and
            # revoking there made a worker that had already given up close the gate
            # again for another full deadline: one WARNING per cycle instead of the
            # single latched give-up that the gate promises.
            if not measurement_lost:
                gate_fail_open = False

        # The `busy` sets do not go here: the only consumer is the poll `status`,
        # which already ORs those same flags. Adding them would make the gate read
        # as if it yielded to the busy state without changing a single result.
        disk_gated = disk_below_floor and not gate_fail_open
        # Only on the transition, same criterion as `pressure_unrecoverable`: one
        # WARNING per tick while the disk stays full buries the case the log exists
        # to show. No hysteresis: reopening means crossing the same floor, not the
        # GC target, so the worker is not left idle for hours longer than needed.
        #
        # The transition is measured on the disk (`disk_below_floor`) and not on the
        # effective gate: a directed push still reaches a gated worker (step 4 of the
        # pick) and while it runs it the worker reports `busy` because of the task,
        # which would turn off `disk_gated` without the volume having moved a byte.
        # With state hanging off the gate, that announced a false reopening and
        # re-announced the closing when the task finished: two misleading lines per
        # task.
        #
        # With the fail-open already latched no closing is announced: the disk can
        # cross the floor again after a measurement flap, but the gate stays open and
        # saying "the worker does not claim tasks" would be false.
        if disk_below_floor and not prev_disk_below_floor and not gate_fail_open:
            log.warning(
                "disk gate closed: free/total=%.3f < min=%.2f on %s; "
                "the worker does not claim tasks until space is recovered "
                "(ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0 disables it)",
                disk.free_fraction,
                floor,
                settings.resolved_cache_root(),
            )
            # One GC pass now, instead of waiting for its interval (300 s default):
            # the gate detects exactly the condition that the `run_gc` pressure pass
            # exists to fix, and while the gate is closed the server's `clear_cache`
            # lane cannot be delivered to it either.
            if gc_wakeup is not None:
                gc_wakeup.set()
        elif prev_disk_below_floor and not disk_below_floor:
            if measurement_lost:
                log.warning(
                    "disk gate opened due to loss of measurement on %s: this is not "
                    "a recovery of space, it is that cache_root can no longer be "
                    "measured; the worker claims tasks again without "
                    "protection (ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0 "
                    "disables it)",
                    settings.resolved_cache_root(),
                )
            else:
                log.info("disk gate released: the worker claims tasks again")
        prev_disk_below_floor = disk_below_floor

        req = PollRequest(
            # The disk gate now lives on the server: the worker sends the free
            # fraction and reports `idle` while gated, so that the server holds back
            # its tasks and can still deliver the `clear_cache` lane to it.
            # `disk_gated` no longer influences the reported status.
            # A pending self-flush does report `busy` so that the server does not
            # assign a task in this same poll. The worker just reaped the executor,
            # so `is_busy`/`is_flushing` are already False but the flush has not
            # started yet; without this the server would see `idle`, deliver a task
            # that the worker ignores when the flush starts, and that execution
            # would hang until the lease expires.
            status="busy"
            if (busy.is_busy or busy.is_flushing or busy.self_flush_requested)
            else "idle",
            lease_token=busy.lease_token,
            current_task_id=busy.task_id,
            current_execution_progress=busy.progress,
            lease_seconds=settings.lease_seconds,
            name=settings.worker_name or None,
            ram_mb=ram.total_mb,
            ram_used_mb=ram.used_mb,
            disk_mb=disk.total_mb,
            disk_used_mb=disk.used_mb,
            # Fail-open: if the local gate opened because the deadline expired
            # (`gate_fail_open`), we stop reporting the low fraction (we send
            # None = "unknown") so that the server-side gate does not hold us back
            # either. Without this the server would keep gating on the low fraction
            # and the "the worker claims again" of the expired-deadline WARNING would
            # be false: the worker would never receive tasks despite the fail-open.
            # Consistent with the server's current-state/fail-open semantics:
            # absence of a fraction = do not gate.
            free_disk_fraction=None if gate_fail_open else disk.free_fraction,
            cpu_pct=cpu_pct,
            version=settings.version,
            capabilities=settings.capabilities,
            cache_flush_completed=busy.cache_flush_pending_ack,
        )

        try:
            resp: PollResponse = await api.poll(req)
        except ShutdownError:
            log.info("Shutdown during poll; leaving the loop.")
            break
        except Exception as e:  # noqa: BLE001 - best-effort, backoff
            log.warning("poll failed: %s", e)
            next_interval = _clamp(
                max(next_interval * 2.0, settings.poll_min_interval_seconds),
                settings.poll_min_interval_seconds,
                settings.poll_max_interval_seconds,
            )
            continue

        # The poll succeeded: reset the ack only if we sent it in this req.
        # This branch is only reached on the success path (exceptions `continue`
        # before getting here, or `break`, and never reach this line).
        if req.cache_flush_completed:
            busy.cache_flush_pending_ack = False

        _process_cancel_signals(resp.cancel_signals, busy)

        # The flush is triggered by the server (`clear_cache`) or by the worker
        # itself (`self_flush_requested`, set by `_run_external` after a disk
        # preflight). The worker flag is reset when it fires so as not to
        # re-flush on the next poll; the ack follows the same lane as the
        # server-driven flush (`cache_flush_pending_ack`).
        want_flush = resp.clear_cache or busy.self_flush_requested
        if want_flush and not busy.is_flushing and not busy.is_busy:
            busy.self_flush_requested = False
            busy.cache_flush_task = asyncio.create_task(
                _run_cache_flush(settings, busy), name="cache-flush"
            )

        if resp.task is not None:
            if busy.is_busy or busy.is_flushing or req.status == "busy":
                # Only a directed push reaches a `busy` poll. The server does not
                # pick in `busy` a push that uses stored secrets (it stays `queued`
                # until an idle poll), and what it does deliver in `busy` goes out
                # without secrets or audit. It is discarded even if the executor
                # finished while the poll was in flight: the poll was built as
                # `busy`. Log + reject without re-completing (the server will
                # reclaim by lease and an idle poll receives it).
                log.warning(
                    "received task_id=%s while %s; ignoring (server will reclaim)",
                    resp.task.task_id,
                    f"busy with task_id={busy.task_id}"
                    if busy.is_busy
                    else "cache flush in progress"
                    if busy.is_flushing
                    else "the poll reported busy",
                )
            else:
                # Spawn as an asyncio.Task; do NOT await here.
                busy.task_id = resp.task.task_id
                busy.lease_token = resp.task.lease_token
                busy.task_payload = resp.task
                _activate_task_redaction(resp.task)
                # The spawn and the assignment are ONE statement on purpose: the GC
                # exclusion reads `busy.live_job_key`, which requires an assigned,
                # live `executor_task`, and `_dispatch_poll_task` publishes the job
                # key from inside. Splitting this in two with an `await` in between
                # leaves the key published without an assigned task, `live_job_key`
                # as `None` with the job running, and the GC free to delete the cache
                # of the task in progress: the exclusion would be silently lost.
                # Covered by `test_live_job_key_published_by_the_poll_loop_spawn`.
                busy.executor_task = asyncio.create_task(
                    _dispatch_poll_task(api, settings, busy, stop, resp.task),
                    name=f"executor-task-{resp.task.task_id}",
                )

        # Cadence: server-driven hint, clamped to the local range.
        next_interval = _clamp(
            float(resp.next_poll_after_seconds),
            settings.poll_min_interval_seconds,
            settings.poll_max_interval_seconds,
        )
        # If the `busy` we reported came only from the disk gate, we ignore the 5 s
        # cadence: the server gives it to `busy` workers because it assumes a live
        # execution (a lease to renew, progress to report) and here there is none.
        # The gate fires on pressure of a shared volume, so all the workers on the
        # host would accept it at once: 6x the polls for nothing while nothing
        # advances. `busy` is re-read because this tick may have dispatched a
        # directed push task, which does want the 5 s.
        if disk_gated and not busy.is_busy and not busy.is_flushing:
            next_interval = settings.poll_max_interval_seconds

    # On leaving the loop, do a final reap to clean up executor_tasks that
    # finished between the last poll and the break.
    await _reap_executor_task(api, busy, settings)

    # Cache flush: cancel if still alive on exit.
    if busy.cache_flush_task is not None and not busy.cache_flush_task.done():
        busy.cache_flush_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await busy.cache_flush_task


# --- Cache flush helpers -----------------------------------------------------


async def _run_cache_flush(settings: Settings, busy: BusyState) -> None:
    """Run flush_caches in a thread bounded by a timeout and set the ack when it ends.

    Time bound: if the rmtree hangs (NFS, degraded disk) the worker must not stay
    "busy" indefinitely. ``asyncio.wait_for`` releases the await on expiry, but
    does NOT cancel the OS thread of ``to_thread``: the rmtree keeps running in the
    background, best-effort. What we achieve is freeing the worker slot: the
    ``finally`` sets ``cache_flush_pending_ack = True`` just like on the happy
    path, so the worker goes back to reporting idle and taking tasks.
    """
    cache_root = settings.resolved_cache_root()
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(flush_caches, cache_root, settings.uv_cache_dir),
            timeout=settings.cache_flush_timeout_seconds,
        )
        log.info("cache_flush: flush completed in %.1fs", time.monotonic() - started)
    except TimeoutError:
        log.warning(
            "cache_flush: flush exceeded %ss; abandoning the thread (keeps running in background)",
            settings.cache_flush_timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        log.exception("cache_flush: flush_caches failed")
    finally:
        # Also runs on timeout: frees the worker slot.
        # It also runs if poll_loop cancels this task on shutdown:
        # CancelledError is a BaseException and the excepts above do not catch it,
        # so the ack ends up set. It is harmless: poll_loop already exited and the
        # in-memory state is reset when the worker restarts.
        busy.cache_flush_pending_ack = True


def _reap_cache_flush_task(busy: BusyState) -> None:
    """Clear the reference to cache_flush_task once it finished (no /complete report)."""
    if busy.cache_flush_task is not None and busy.cache_flush_task.done():
        # CancelledError is a BaseException; suppress both families (same as
        # _reap_executor_task) in case poll_loop was cancelled externally.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            busy.cache_flush_task.result()
        busy.cache_flush_task = None


# --- Artifact upload ---------------------------------------------------------


# Artifact upload. Reuses the spirit of the output streamer:
# transient errors (network/5xx) -> backoff and retry; 409 -> discard (lease lost);
# 413 -> limit exceeded (depends on `required`).
_ARTIFACT_UPLOAD_MAX_ATTEMPTS = 5
_ARTIFACT_BACKOFF_BASE_SECONDS = 1.0
_ARTIFACT_BACKOFF_MAX_SECONDS = 30.0


async def _upload_artifacts(
    api: OrchestratorAPI,
    *,
    task_id: int,
    lease_token: str,
    artifacts_dir: Path,
    spec: ArtifactsSpec,
    max_request_bytes: int,
    upload_timeout: float | None = None,
) -> bool:
    """Collect and upload the artifacts in the dir. Returns True if the execution
    can be reported normally; False only if `required` and the upload failed
    terminally (the caller will report `failed`).

    Policy:
    - empty dir / no matches -> True (no-op).
    - 409 (lease lost): the attempt is no longer ours; discard and return
      True (`failed` makes no sense: the server already reassigned).
    - transient errors: retry with backoff until exhausted; if exhausted and
      `required` -> False; if not `required` -> True (best-effort, logged).
    - 413: if `required` -> False; otherwise -> True (best-effort)."""
    files = collect_artifacts(artifacts_dir, include=spec.include, exclude=spec.exclude)
    if not files:
        return True
    batches = plan_batches(files, max_request_bytes=max_request_bytes)
    log.info(
        "task_id=%s uploading %d artifact(s) in %d batch(es)",
        task_id,
        len(files),
        len(batches),
    )
    for batch in batches:
        payload = [(f.rel_path, f.abs_path, _guess_content_type(f.rel_path)) for f in batch]
        attempt = 0
        while True:
            try:
                await api.upload_artifacts(
                    task_id, lease_token=lease_token, files=payload, timeout=upload_timeout
                )
                break
            except LeaseLostError:
                log.warning(
                    "task_id=%s lease lost uploading artifacts; discarding backlog",
                    task_id,
                )
                return True
            except ArtifactsTooLargeError:
                log.warning(
                    "task_id=%s artifacts batch rejected (413); required=%s",
                    task_id,
                    spec.required,
                )
                return not spec.required
            except Exception as e:  # noqa: BLE001
                attempt += 1
                if attempt >= _ARTIFACT_UPLOAD_MAX_ATTEMPTS:
                    log.warning(
                        "task_id=%s artifact upload exhausted retries: %s; required=%s",
                        task_id,
                        e,
                        spec.required,
                    )
                    return not spec.required
                delay = min(
                    _ARTIFACT_BACKOFF_MAX_SECONDS,
                    _ARTIFACT_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1),
                )
                delay *= 0.5 + random.random() / 2
                log.warning(
                    "task_id=%s artifact upload failed attempt=%d: %s; retrying in %.1fs",
                    task_id,
                    attempt,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
    return True


def _finalize_status(base_status: str, *, artifacts_ok: bool, required: bool) -> str:
    """Decide the final status after the artifact upload.

    Only downgrades `completed` -> `failed` when the artifacts were `required` and
    their upload failed terminally. Never promotes a previous `failed`."""
    if base_status == "completed" and required and not artifacts_ok:
        return "failed"
    return base_status


def _guess_content_type(rel_path: str) -> str:
    """Content-Type by extension; falls back to octet-stream."""
    guessed, _ = mimetypes.guess_type(rel_path)
    return guessed or "application/octet-stream"


# --- Cache GC loop ----------------------------------------------------------


async def cache_gc_loop(
    settings: Settings,
    stop: asyncio.Event,
    busy: BusyState,
    *,
    gc_wakeup: asyncio.Event | None = None,
) -> None:
    """Periodic loop that runs the GC over the scratch cache.

    Decision: we run in the same process as the worker (not a separate thread)
    because ``shutil.rmtree`` releases the GIL in disk syscalls and the cadence
    (default 300s) is lax compared with the poll_loop (1-30s). If the GC takes
    tens of seconds, the poll_loop keeps running in parallel.

    Running in parallel with the execution is exactly what made the GC dangerous:
    we pass it a getter of ``busy.live_job_key`` so that the live job's dir stays
    out of the candidates. It is a getter and not the value because a pass lasts
    minutes and the live job changes during it: with a frozen value, a job that
    starts mid-pass stayed exposed. It is ``live_job_key`` and not
    ``active_job_key`` because the raw field outlives the job until the next reap,
    and protecting a dead dir under disk pressure leaves the GC with no candidates
    while having a free one right in front of it.

    ``prev_unrecoverable`` carries the pressure state between ticks so that the
    WARNING is emitted on the transition and not once per tick.

    Initial pass: we run once at startup to clean old dirs left over by a worker
    killed before the previous pass.

    ``gc_wakeup`` cuts the interval wait short: the poll_loop disk gate sets it when
    it closes because it detects exactly the condition that the pressure pass exists
    to fix, and waiting 300 s leaves the worker idle all that time without the only
    mechanism that can free space running. The event is consumed BEFORE the pass: if
    the gate sets it again while it runs, it is served with another pass when it
    finishes.
    """
    interval = max(1, int(settings.cache_gc_interval_seconds))
    cache_root = settings.resolved_cache_root()
    log.info("cache_gc: starting (interval=%ds root=%s)", interval, cache_root)
    prev_unrecoverable = False
    while not stop.is_set():
        if gc_wakeup is not None:
            gc_wakeup.clear()
        try:
            stats = await asyncio.to_thread(
                run_gc,
                cache_root,
                max_age_days=settings.cache_max_age_days,
                min_free_fraction=settings.cache_min_free_fraction,
                recover_free_fraction=settings.cache_recover_free_fraction,
                keep_job_key=lambda: busy.live_job_key,
                previously_unrecoverable=prev_unrecoverable,
            )
            prev_unrecoverable = stats.pressure_unrecoverable
        except Exception:  # noqa: BLE001
            log.exception("cache_gc: run_gc failed")
        await _sleep_until_gc_tick(stop, gc_wakeup, interval)
    log.info("cache_gc: stopped")


async def _sleep_until_gc_tick(
    stop: asyncio.Event, gc_wakeup: asyncio.Event | None, interval: int
) -> None:
    """Wait for the GC interval, or until ``stop``/``gc_wakeup`` cut it short."""
    if gc_wakeup is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        return
    waiters = [
        asyncio.create_task(stop.wait(), name="cache-gc-stop"),
        asyncio.create_task(gc_wakeup.wait(), name="cache-gc-wakeup"),
    ]
    try:
        await asyncio.wait(waiters, timeout=interval, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter


# --- Entrypoint --------------------------------------------------------------


def _ensure_pubkey_loadable_or_die(settings: Settings) -> None:
    """Fail-fast at boot: the Ed25519 pubkey must load.

    If it does not load (empty path, missing file, invalid PEM), the worker could
    not verify any signed task and would enter an operationally confusing mode:
    register/heartbeat OK, but every task rejected in `_verify_poll_task_signature`
    and reclaimed by housekeeping. We prefer to abort the process with an
    actionable hint.
    """
    path = settings.ed25519_public_key_path
    hint_suffix = (
        "Universal signing requires a loadable Ed25519 public key.\n"
        "  - Derive the pubkey on the server host:\n"
        "      openssl pkey -in <priv>.pem -pubout -out server-ed25519.pub\n"
        "  - Copy it to the worker host and mount it in the container\n"
        "    (the root Makefile does this if it lives at client/server-ed25519.pub).\n"
        "  - Make sure ORCH_ED25519_PUBLIC_KEY_PATH points to the file\n"
        "    inside the container (default /var/lib/orcaster/ed25519.pub)."
    )
    if path is None:
        log.error(
            "ED25519_PUBLIC_KEY_PATH not configured (settings value is None).\n%s",
            hint_suffix,
        )
        sys.exit(1)
    try:
        load_public_key(path)
    except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
        log.error(
            "Failed to load Ed25519 pubkey from %s: %s\n%s",
            path,
            exc,
            hint_suffix,
        )
        sys.exit(1)
    log.info("Ed25519 pubkey loaded from %s; signature verification enabled", path)


async def run(settings: Settings | None = None, *, env_file: str = ".env") -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Before validating the settings, so it is visible even if startup fails:
    # `Settings` ignores unknown names, and an unsupported legacy one must not be
    # dropped without a trace.
    warn_legacy_env(os.environ, env_file=env_file)
    if settings is None:
        try:
            settings = load_settings()
        except ValidationError as exc:
            # Fail closed with a readable line: e.g. a malformed
            # `ORCH_ISOLATION_REQUIRED_ENV`. The pydantic message names the field and
            # the offending element. Without `input_value`: the raw value of a field
            # could be a secret.
            problems = "; ".join(
                f"{'.'.join(str(part) for part in err['loc']) or 'settings'}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)
            )
            log.error("startup: invalid worker configuration; refusing to start: %s", problems)
            sys.exit(1)
    # Non-dumpable process before anything else. Tasks run with the same uid and,
    # without this, read the worker's environment (including the variables that
    # require isolation) through /proc even if their own env comes clean. Fails
    # closed: if it cannot be done, the worker does not start. It goes after
    # basicConfig only so that the ERROR reaches the log with formatting.
    make_process_non_dumpable()
    _take_egress_key_from_environ(settings)

    # Local logs mask the secrets of the task in progress.
    install_log_redaction()

    # Container hardening (--read-only + small tmpfs for $HOME): TMPDIR is
    # redirected to the cache volume on disk because venvs (torch, etc.) weigh GBs
    # and do not fit in the /home/orcaster tmpfs. tempfile.gettempdir() honors
    # TMPDIR, so venv_manager.py inherits this unchanged; the dir only needs to
    # exist before first use.
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        Path(tmpdir).mkdir(parents=True, exist_ok=True)

    # Containment guard: a container without `--memory` starts anyway, and nothing
    # gives it away until a task runs away. It goes early, right after configuring
    # the logging, so it lands on the first screen of the log.
    _warn_if_memory_cap_missing()

    # Per-task allowlist orphaned by a hard kill during the egress window. It goes
    # before the fail-fasts (isolation and pubkey): even if the worker aborts, the
    # sidecar proxy keeps serving with that file.
    _sweep_orphan_egress_allowlist(settings.egress_allow_file, settings.egress_key_bytes)

    # Isolation guard: warns if strict mode is off and aborts if the worker also
    # has a variable from the list that requires it. It aborts before registering,
    # so no task is ever claimed.
    _log_flush_markers(settings)
    _ensure_strict_isolation_or_die(settings)

    # Fail-fast: if the server's Ed25519 pubkey does not load, abort before
    # registering. Without a pubkey every task is silently rejected
    # (`_verify_poll_task_signature`), leaving the operator with a worker that
    # "connects but runs nothing". Symmetric to the server's fail-fast on
    # `ED25519_PRIVATE_KEY_PATH` (server/orcaster_server/main.py::lifespan).
    _ensure_pubkey_loadable_or_die(settings)

    # Orphans from previous crashes; the old lease is no longer valid
    # (resend impossible -> loss policy).
    cleanup_spill_orphans(settings.state_dir)

    # Ephemeral dirs from previous executions that were not cleaned up (crash).
    with contextlib.suppress(Exception):
        swept = await asyncio.to_thread(sweep_run_dirs, settings.resolved_cache_root())
        if swept:
            log.info("startup: swept %d stale ephemeral run dir(s)", swept)

    # Half-deleted detached trees: the detach deletes synchronously, so the only
    # thing that survives there is what a SIGKILL left mid-rmtree. Nobody else looks
    # at them (the GC only enumerates `tasks/`), so without this sweep those GB are
    # never recovered. The area of the worker-wide uv cache is also swept, which may
    # live on another filesystem (and therefore has its own detach area, a sibling
    # of it and not of `cache_root`).
    with contextlib.suppress(Exception):
        cache_root = settings.resolved_cache_root()
        bases = [cache_root]
        if settings.uv_cache_dir is not None and settings.uv_cache_dir.parent != cache_root:
            bases.append(settings.uv_cache_dir.parent)
        for base in bases:
            swept_trash = await asyncio.to_thread(sweep_trash_dirs, base)
            if swept_trash:
                log.info(
                    "startup: swept %d stale detached cache tree(s) under %s",
                    swept_trash,
                    trash_root(base),
                )

    stop = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Signal received, shutting down...")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    async with OrchestratorAPI(
        str(settings.server_url),
        connect_timeout=settings.http_connect_timeout,
        read_timeout=settings.http_read_timeout,
        max_attempts=settings.network_max_attempts,
        base_delay=settings.network_base_delay_seconds,
        max_delay=settings.network_max_delay_seconds,
        stop=stop,
    ) as api:
        await ensure_credentials(api, settings)

        busy = BusyState()
        # The poll disk gate uses this event to ask the GC for an immediate pass
        # when it closes, instead of waiting for its interval.
        gc_wakeup = asyncio.Event()
        gc_task = asyncio.create_task(
            cache_gc_loop(settings, stop, busy, gc_wakeup=gc_wakeup), name="cache-gc-loop"
        )
        try:
            await poll_loop(api, settings, busy, stop, gc_wakeup=gc_wakeup)
        finally:
            stop.set()
            # Cooperative shutdown of the executor_task if it is still alive.
            await _shutdown_executor(api, busy, settings)
            try:
                await asyncio.wait_for(gc_task, timeout=2.0)
            except TimeoutError:
                gc_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await gc_task
            except Exception as e:  # noqa: BLE001
                log.warning("cache_gc_loop failed during shutdown: %s", e)
            try:
                await asyncio.wait_for(api.goodbye(), timeout=2.0)
            except (TimeoutError, ShutdownError):
                log.info("goodbye skipped (timeout or shutdown in progress).")
            except Exception as e:  # noqa: BLE001
                log.warning("goodbye failed: %s", e)


async def _shutdown_executor(
    api: OrchestratorAPI,
    busy: BusyState,
    settings: Settings | None = None,
) -> None:
    """Cooperative shutdown of the executor_task.

    Steps:
        (a) busy.abandon_event.set() - makes ``_execute_with_abandon``
            kill the subprocess process group.
        (b) await wait_for(executor_task, timeout=GRACE_SECONDS) - waits for
            the dispatch to report ``cancelled`` to the server.
        (c) If it exceeds GRACE_SECONDS, forced cancel() + suppress.
        (d) Final reap to clean up the BusyState.
    """
    if busy.executor_task is None or busy.executor_task.done():
        await _reap_executor_task(api, busy, settings)
        return

    log.info(
        "Executor shutdown: signaling abandon to executor_task=%s",
        busy.executor_task.get_name() if busy.executor_task else "<none>",
    )
    busy.abandon_event.set()
    try:
        await asyncio.wait_for(busy.executor_task, timeout=GRACE_SECONDS)
    except TimeoutError:
        log.warning(
            "executor_task did not finish in %.1fs; cancelling forcibly.",
            GRACE_SECONDS,
        )
        busy.executor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await busy.executor_task
    except Exception as e:  # noqa: BLE001
        log.warning("executor_task raised during shutdown: %s", e)

    await _reap_executor_task(api, busy, settings)


def main() -> None:
    asyncio.run(run())


def _resolve_version() -> str:
    """Version for `--version`. Same source as `Settings.version`."""
    from orcaster_worker import __version__

    return __version__


def cli_entry(argv: list[str] | None = None) -> int:
    """CLI entry point of the orcaster-worker script."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="orcaster-worker",
        description="Orcaster worker agent.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"orcaster-worker {_resolve_version()}",
    )
    parser.add_argument(
        "--config-path",
        type=str,
        default=None,
        help="Path to an .env file (overrides the default './.env').",
    )
    args = parser.parse_args(argv)

    settings: Settings | None = None
    if args.config_path:
        settings = Settings(_env_file=args.config_path)  # type: ignore[call-arg]

    try:
        asyncio.run(run(settings, env_file=args.config_path or ".env"))
    except KeyboardInterrupt:
        return 130
    return 0


# Expose dispatch_run_batch at module level for tests that mock it.
__all__ = [
    "GRACE_SECONDS",
    "BusyState",
    "_dispatch_poll_task",
    "_execute_with_abandon",
    "_reap_executor_task",
    "_run_external",
    "_run_reserved",
    "_shutdown_executor",
    "_verify_poll_task_signature",
    "cache_gc_loop",
    "dispatch_run_batch",
    "ensure_credentials",
    "poll_loop",
    "run",
]


if __name__ == "__main__":
    sys.exit(cli_entry())
