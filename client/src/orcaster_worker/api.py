"""HTTP client for the orchestrator server.

Implements the server's unified contract. The worker speaks **only**
`POST /v1/workers/{id}/poll` for the main loop. The `heartbeat`, `claim` and
`commands/{uuid}/ack` endpoints no longer exist.

Auth: `X-API-Key` header on every authenticated call. The key is obtained in
`register` and persisted through `orcaster_worker.state`.

Errors: raises `httpx.HTTPStatusError` on unexpected 4xx/5xx responses; the
409 semantics (lease lost in complete/output) are translated to `LeaseLostError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import random
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from orcaster_worker.poll import PollRequest, PollResponse

log = logging.getLogger("orcaster.worker.api")

# Status codes that DO trigger a retry (transient). 429 belongs here: rate limit.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# httpx exceptions considered transient (network loss, read timeout, RST in the
# middle of a response, handshake failure). Connect/Read timeout and ConnectError
# are the typical ones when the worker loses the network and comes back.
_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    httpx.PoolTimeout,
)


def _compute_backoff(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: float = 0.2,
) -> float:
    """Exponential backoff with symmetric ±jitter (default ±20%)."""
    raw = base_delay * (2 ** (attempt - 1))
    capped = min(raw, max_delay)
    # Symmetric jitter to avoid a thundering herd among workers that reconnect
    # at the same time after a network blip.
    factor = 1.0 + random.uniform(-jitter, jitter)
    return max(0.0, capped * factor)


def _retry_after_seconds(resp: httpx.Response, *, max_delay: float) -> float | None:
    """Read `Retry-After` (integer seconds). Returns None if it does not apply."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        # The HTTP-date format is not supported (the contract does not require
        # it); fall back to backoff.
        return None
    if secs < 0:
        return None
    return min(secs, max_delay)


class ShutdownError(Exception):
    """Raised when the `stop` event is set during a retry sleep."""


async def _cooperative_sleep(delay: float, stop: asyncio.Event | None) -> None:
    """Sleep that honors `stop`. If `stop` is set before the timeout, raises `ShutdownError`."""
    if stop is None:
        await asyncio.sleep(delay)
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        return
    raise ShutdownError("shutdown requested during retry backoff")


async def _with_retry[T](
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    stop: asyncio.Event | None = None,
) -> T:
    """Run `coro_factory()` with retry + exponential backoff.

    Only **transient** errors are retried:
        * httpx network exceptions (`ConnectError`, `ReadTimeout`, ...).
        * responses with a status in `_RETRY_STATUS` (429 + 5xx).

    NOT retried: 2xx, non-429 4xx (401/404/409/422/...). The caller decides
    how to interpret those statuses (raise_for_status, mapping to types, etc.).
    To detect a retryable status the response must be inspected WITHOUT
    consuming `raise_for_status`: the factory contract is to return the raw
    `httpx.Response`. The caller post-processes it afterwards.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await coro_factory()
        except _RETRY_EXCEPTIONS as e:
            if attempt >= max_attempts:
                log.warning(
                    "network retry exhausted attempt=%d error_kind=%s",
                    attempt,
                    type(e).__name__,
                )
                raise
            delay = _compute_backoff(attempt, base_delay=base_delay, max_delay=max_delay)
            log.warning(
                "transient network error, retrying attempt=%d delay=%.2f error_kind=%s",
                attempt,
                delay,
                type(e).__name__,
            )
            await _cooperative_sleep(delay, stop)
            continue

        # If the factory returns an httpx.Response, inspect its status.
        if isinstance(result, httpx.Response) and result.status_code in _RETRY_STATUS:
            if attempt >= max_attempts:
                log.warning(
                    "http retry exhausted attempt=%d status=%d",
                    attempt,
                    result.status_code,
                )
                return result  # the caller will raise_for_status
            delay = _retry_after_seconds(result, max_delay=max_delay)
            if delay is None:
                delay = _compute_backoff(attempt, base_delay=base_delay, max_delay=max_delay)
            log.warning(
                "transient http error, retrying attempt=%d delay=%.2f status=%d",
                attempt,
                delay,
                result.status_code,
            )
            # Release the connection before sleeping.
            await result.aclose()
            await _cooperative_sleep(delay, stop)
            continue

        return result


# --- Contract-specific exceptions -------------------------------------


class LeaseLostError(RuntimeError):
    """`complete` returned 409: the lease was revoked/expired."""


class ArtifactsTooLargeError(RuntimeError):
    """The artifacts endpoint returned 413: the batch exceeds a limit."""


# --- Result types ----------------------------------------------------------


@dataclass(frozen=True)
class RegisterResult:
    worker_id: str
    api_key: str


# --- Client ------------------------------------------------------------------


class OrchestratorAPI:
    """Async httpx wrapper. Lifecycle through an async context manager.

    Example:

        async with OrchestratorAPI(base_url, ...) as api:
            res = await api.register(...)
            api.set_credentials(res.worker_id, res.api_key)
            ...
    """

    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        worker_id: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = 5,
        base_delay: float = 0.5,
        max_delay: float = 30.0,
        stop: asyncio.Event | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._worker_id = worker_id
        self._api_key = api_key
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._stop = stop
        timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
        )

    async def _post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST with transparent retry on transient errors."""

        async def _do() -> httpx.Response:
            return await self._client.post(url, json=json, headers=headers)

        return await _with_retry(
            _do,
            max_attempts=self._max_attempts,
            base_delay=self._base_delay,
            max_delay=self._max_delay,
            stop=self._stop,
        )

    def set_stop_event(self, stop: asyncio.Event) -> None:
        """Allow injecting the shutdown event after __init__."""
        self._stop = stop

    async def __aenter__(self) -> OrchestratorAPI:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_credentials(self, worker_id: str, api_key: str) -> None:
        self._worker_id = worker_id
        self._api_key = api_key

    @property
    def worker_id(self) -> str | None:
        return self._worker_id

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            raise RuntimeError("API key not initialized; run register() first.")
        return {"X-API-Key": self._api_key}

    # --- /v1/workers ----------------------------------------------------------

    async def register(
        self,
        *,
        enrollment_token: str,
        hostname: str | None = None,
        os_name: str | None = None,
        arch: str | None = None,
        ram_mb: int | None = None,
        disk_mb: int | None = None,
        version: str | None = None,
        tags: list[str] | None = None,
        capabilities: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> RegisterResult:
        # `enrollment_token` (multi-tenancy): the server requires it as a
        # mandatory field (`min_length=1`). A body without the field => 422; a
        # present but invalid token (revoked/used/expired/nonexistent) => 401.
        # The caller (`ensure_credentials`) fails fast beforehand if it is empty.
        body = {
            "enrollment_token": enrollment_token,
            "hostname": hostname or _detect_hostname(),
            "os": os_name or _detect_os(),
            "arch": arch or platform.machine() or "unknown",
            "ram_mb": ram_mb,
            "disk_mb": disk_mb,
            "version": version,
            "tags": tags or [],
            "capabilities": capabilities or {},
            "name": name,
        }
        r = await self._post("/v1/workers/register", json=body)
        r.raise_for_status()
        data = r.json()
        return RegisterResult(worker_id=data["worker_id"], api_key=data["api_key"])

    async def poll(self, req: PollRequest) -> PollResponse:
        """POST `/v1/workers/{id}/poll` — unified endpoint.

        Replaces the old `heartbeat_worker` + `claim` calls. A single request
        delivers:
            * `task` (claim or push, optional),
            * `cancel_signals[]` (executions cancelled server-side),
            * `next_poll_after_seconds` (server-driven cadence).
        """
        if not self._worker_id:
            raise RuntimeError("worker_id not initialized.")
        r = await self._post(
            f"/v1/workers/{self._worker_id}/poll",
            json=req.to_json(),
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        return PollResponse.from_wire(r.json())

    async def goodbye(self) -> None:
        if not self._worker_id:
            return
        r = await self._post(
            f"/v1/workers/{self._worker_id}/goodbye",
            headers=self._auth_headers(),
        )
        r.raise_for_status()

    # --- /v1/tasks ------------------------------------------------------------

    async def output(
        self,
        task_id: int,
        *,
        lease_token: str,
        events: list[dict[str, Any]],
    ) -> None:
        """Send a batch of output events ``{seq, ts, stream, text}``.

        The server inserts each event into ``output_events`` idempotently
        (``UNIQUE(execution_id, seq)``): resending the same batch after a
        network timeout does not duplicate rows. ``202`` guarantees the batch is
        persisted; ``409`` means the lease is no longer ours → ``LeaseLostError``
        (the streamer's sender discards the backlog)."""
        body = {"lease_token": lease_token, "events": events}
        r = await self._post(
            f"/v1/tasks/{task_id}/output",
            json=body,
            headers=self._auth_headers(),
        )
        if r.status_code == 409:
            raise LeaseLostError(f"lease lost for task {task_id}")
        r.raise_for_status()  # 202 Accepted

    async def upload_artifacts(
        self,
        task_id: int,
        *,
        lease_token: str,
        files: list[tuple[str, Path, str]],
        timeout: float | None = None,
    ) -> None:
        """Upload a batch of artifacts via multipart/form-data.

        ``files`` is ``[(rel_path, abs_path, content_type), ...]``. The ``rel_path``
        travels as the part filename (preserves subdirectories). ``202`` →
        persisted; ``409`` → ``LeaseLostError`` (the caller discards the backlog);
        ``413`` → ``ArtifactsTooLargeError``. Other 4xx/5xx → ``HTTPStatusError``.

        It does not use the transparent retry of ``_post``: the parts stream from
        open files and a retry would have to reopen them; transient-error retry
        lives in the caller (main.py)."""
        opened: list[Any] = []
        try:
            multipart: list[tuple[str, Any]] = []
            for rel_path, abs_path, content_type in files:
                fh = open(abs_path, "rb")  # noqa: SIM115
                opened.append(fh)
                multipart.append(("files", (rel_path, fh, content_type)))
            r = await self._client.post(
                f"/v1/tasks/{task_id}/artifacts",
                data={"lease_token": lease_token},
                files=multipart,
                headers=self._auth_headers(),
                timeout=timeout,
            )
        finally:
            for fh in opened:
                with contextlib.suppress(Exception):
                    fh.close()
        if r.status_code == 409:
            raise LeaseLostError(f"lease lost for task {task_id} (artifacts)")
        if r.status_code == 413:
            raise ArtifactsTooLargeError(f"artifacts batch too large for task {task_id}")
        r.raise_for_status()  # 202 Accepted

    async def complete(
        self,
        task_id: int,
        *,
        lease_token: str,
        exit_code: int | None,
        status: str,
        cancelled_reason: str | None = None,
    ) -> None:
        # Output travels exclusively as events (POST /output); the stdout/stderr
        # fields are no longer part of the server's /complete schema and the
        # client does not send them.
        body: dict[str, Any] = {
            "lease_token": lease_token,
            "exit_code": exit_code,
            "status": status,
        }
        if cancelled_reason is not None:
            body["cancelled_reason"] = cancelled_reason
        r = await self._post(
            f"/v1/tasks/{task_id}/complete",
            json=body,
            headers=self._auth_headers(),
        )
        if r.status_code == 409:
            raise LeaseLostError(f"lease lost for task {task_id}")
        r.raise_for_status()


# Optional path for the operator to expose the host's hostname inside the
# container by mounting the host's `/etc/hostname`. Keeping it distinct from the
# container's own `/etc/hostname` avoids overwriting the file the kernel uses.
_HOST_HOSTNAME_FILE = Path("/etc/host_hostname")


def _detect_hostname() -> str:
    """Resolve the hostname to report to the server.

    Order of preference:
    1. `HOST_HOSTNAME` env — convention for operators running the worker in a
       container (Compose/Podman `-e HOST_HOSTNAME=$(hostname)`).
    2. `/etc/host_hostname` if mounted (read-only bind from the host).
    3. `socket.gethostname()` — the process UTS namespace. Inside a container
       without `--hostname` this is usually a useless short hash
       (e.g. `7f3a2b1c9e0d`), hence the earlier fallbacks.
    """
    env_value = os.environ.get("HOST_HOSTNAME", "").strip()
    if env_value:
        return env_value
    try:
        if _HOST_HOSTNAME_FILE.is_file():
            value = _HOST_HOSTNAME_FILE.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        # Permissions / odd filesystem: fall back to the socket. Not fatal.
        pass
    return socket.gethostname()


def _detect_os() -> str:
    """Return a value consistent with the schema `OS` (linux/darwin/freebsd/windows)."""
    s = platform.system().lower()
    if s == "linux":
        return "linux"
    if s == "darwin":
        return "darwin"
    if s == "windows":
        return "windows"
    if s == "freebsd":
        return "freebsd"
    # Reasonable fallback: the server will reject the registration if it does
    # not match; better to fail early than to lie.
    return s
