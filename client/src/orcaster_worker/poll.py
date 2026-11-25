"""Dataclasses of the `/v1/workers/{id}/poll` contract.

Client-side mirror of `server/orcaster_server/schemas/poll.py`. The names and
types are kept exact so that a change in the server is easy to spot here.

Key decisions:

* `created_at` travels as a raw **string** (not `datetime`). The server
  serializes it with a "Z" suffix (Pydantic v2 default) and the signature was
  computed over that same string. To reproduce the canonical message
  byte-identically when verifying, it cannot be parsed and re-formatted: the
  literal string received from the wire must be preserved.
* `idempotency_key`, `lease_token`, `worker_id`, `target_worker_id` also travel
  as lowercase hyphenated UUID strings — the canonical format of
  `str(uuid.UUID(...))`. Same reason: the signature uses the UUID string, and
  bytes are compared with the server without touching the representation.
* `signature` is standard base64 (with padding), as `signature_to_b64` in the
  server. The client decodes it with `base64.b64decode` before verifying.
* `secrets` travels as a sibling of `payload`, outside the signature. An older
  server does not send it and it reads as `{}`. It is kept out of `repr` so no
  task log prints its values.
* `from_wire` reads each key by name: an unknown key is ignored. That is why a
  worker without `secrets` support ignores it without breaking, and one without
  `worker_scoped_secrets` support does the same with that field.
* `worker_scoped_secrets`: names of `secrets` that came from a row of the
  worker itself on the server. Names only, only for the log. An older server
  does not send it and it reads as `()`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class PollRequest:
    """Body of the POST `/v1/workers/{id}/poll`.

    `status='busy'` when there is a live execution; `lease_token` travels as
    proof of ownership and the server uses it to renew the lease of its own
    execution on every poll (implicit heartbeat).
    """

    status: Literal["idle", "busy"]
    lease_token: str | None = None
    current_task_id: int | None = None
    current_execution_progress: int | None = None
    lease_seconds: int = 120
    name: str | None = None
    ram_mb: int | None = None
    ram_used_mb: int | None = None
    disk_mb: int | None = None
    disk_used_mb: int | None = None
    free_disk_fraction: float | None = None
    cpu_pct: int | None = None
    # Worker version: sent on every poll, not only in `register`, which runs
    # once per identity. Without this, an upgraded worker keeps showing in the
    # dashboard with the version it enrolled with.
    version: str | None = None
    # Declared capabilities, same reason as `version`. None = do not send: the
    # server keeps the stored ones. `{}` is sent and clears them.
    capabilities: dict[str, Any] | None = None
    cache_flush_completed: bool = False

    def to_json(self) -> dict[str, Any]:
        """Serialization for httpx (omits None except where the server accepts it)."""
        body: dict[str, Any] = {"status": self.status}
        if self.lease_token is not None:
            body["lease_token"] = self.lease_token
        if self.current_task_id is not None:
            body["current_task_id"] = self.current_task_id
        if self.current_execution_progress is not None:
            body["current_execution_progress"] = self.current_execution_progress
        body["lease_seconds"] = self.lease_seconds
        if self.name is not None:
            body["name"] = self.name
        if self.ram_mb is not None:
            body["ram_mb"] = self.ram_mb
        if self.ram_used_mb is not None:
            body["ram_used_mb"] = self.ram_used_mb
        if self.disk_mb is not None:
            body["disk_mb"] = self.disk_mb
        if self.disk_used_mb is not None:
            body["disk_used_mb"] = self.disk_used_mb
        if self.free_disk_fraction is not None:
            body["free_disk_fraction"] = self.free_disk_fraction
        if self.cpu_pct is not None:
            body["cpu_pct"] = self.cpu_pct
        # Falsy and not `is not None`: `ORCH_VERSION=` (an empty line in the
        # .env) is a plausible typo, and sending "" would overwrite the stored
        # version with an empty one instead of leaving it as is. Absent means
        # "no data".
        if self.version:
            body["version"] = self.version
        if self.capabilities is not None:
            body["capabilities"] = self.capabilities
        if self.cache_flush_completed:
            body["cache_flush_completed"] = True
        return body


@dataclass(frozen=True)
class PollTaskPayload:
    """Task delivered by the server (zero or one per poll)."""

    task_id: int
    # 'run_batch' for work tasks; a reserved name for control ops; None = 'run_batch' (compat)
    command: str | None
    payload: dict[str, Any]
    created_at: str  # raw wire string (with the server "Z" suffix)
    delivery_mode: Literal["claim", "push"]
    signed: bool
    signature: str | None  # base64 (with padding) or None
    idempotency_key: str | None  # stringified UUID or None
    worker_id: str  # stringified UUID of the recipient
    target_worker_id: str | None
    lease_token: str  # stringified UUID
    lease_expires_at: str  # raw string (with "Z" suffix)
    execution_timeout_seconds: int | None = None  # outer timeout; None = no limit
    # Values of the `payload.task_env` names stored on the server.
    # Never in `repr`: logs that print the task do not see them.
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)
    # Description of the problem if `secrets` arrived malformed (no values).
    # The dispatcher fails the task instead of running it without its secrets.
    secrets_error: str | None = None
    # Names in `secrets` whose value comes from a row of the worker itself.
    worker_scoped_secrets: tuple[str, ...] = ()
    # Description of the problem if `worker_scoped_secrets` arrived malformed. It
    # is diagnostic: it does not fail the task, the dispatcher logs a WARNING.
    worker_scoped_secrets_error: str | None = None


def _parse_secrets(raw: object) -> tuple[dict[str, str], str | None]:
    """Validate the wire `secrets`: absent or null -> `{}`; object of str to str
    -> the dict; any other shape -> `({}, problem)`. The problem names the type
    or the affected names, never a value."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, f"secrets malformed: expected an object, got {type(raw).__name__}"
    bad = sorted(str(k) for k, v in raw.items() if not isinstance(k, str) or not isinstance(v, str))
    if bad:
        return {}, f"secrets malformed: non-string value for {', '.join(bad)}"
    return dict(raw), None


def _parse_worker_scoped(
    raw: object, secrets: Mapping[str, str]
) -> tuple[tuple[str, ...], str | None]:
    """Validate `worker_scoped_secrets`: absent or null -> `()`; list of str ->
    the names that also came in `secrets`, sorted; any other shape ->
    `((), problem)`. Names only: this field never holds values."""
    if raw is None:
        return (), None
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        return (), (
            f"worker_scoped_secrets malformed: expected a list of names, got {type(raw).__name__}"
        )
    return tuple(sorted({n for n in raw if n in secrets})), None


@dataclass(frozen=True)
class PollCancelSignal:
    """Cancel signal: `task_id` + `lease_token`, matchable against busy."""

    task_id: int
    lease_token: str


@dataclass(frozen=True)
class PollResponse:
    """Poll response: a single `task`, `cancel_signals[]`, cadence hint."""

    task: PollTaskPayload | None = None
    cancel_signals: list[PollCancelSignal] = field(default_factory=list)
    next_poll_after_seconds: int = 30
    clear_cache: bool = False

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> PollResponse:
        """Deserialize from the server JSON."""
        raw_task = data.get("task")
        task: PollTaskPayload | None = None
        if raw_task is not None:
            raw_ets = raw_task.get("execution_timeout_seconds")
            secrets, secrets_error = _parse_secrets(raw_task.get("secrets"))
            worker_scoped, worker_scoped_error = _parse_worker_scoped(
                raw_task.get("worker_scoped_secrets"), secrets
            )
            task = PollTaskPayload(
                task_id=int(raw_task["task_id"]),
                command=str(raw_task["command"]) if raw_task.get("command") is not None else None,
                payload=dict(raw_task.get("payload") or {}),
                created_at=str(raw_task["created_at"]),
                delivery_mode=str(raw_task["delivery_mode"]),  # type: ignore[arg-type]
                signed=bool(raw_task.get("signed", False)),
                signature=raw_task.get("signature"),
                idempotency_key=(
                    str(raw_task["idempotency_key"])
                    if raw_task.get("idempotency_key") is not None
                    else None
                ),
                worker_id=str(raw_task["worker_id"]),
                target_worker_id=(
                    str(raw_task["target_worker_id"])
                    if raw_task.get("target_worker_id") is not None
                    else None
                ),
                lease_token=str(raw_task["lease_token"]),
                lease_expires_at=str(raw_task["lease_expires_at"]),
                execution_timeout_seconds=int(raw_ets) if raw_ets is not None else None,
                secrets=secrets,
                secrets_error=secrets_error,
                worker_scoped_secrets=worker_scoped,
                worker_scoped_secrets_error=worker_scoped_error,
            )
        cancel_signals = [
            PollCancelSignal(task_id=int(s["task_id"]), lease_token=str(s["lease_token"]))
            for s in (data.get("cancel_signals") or [])
        ]
        return cls(
            task=task,
            cancel_signals=cancel_signals,
            next_poll_after_seconds=int(data.get("next_poll_after_seconds", 30)),
            clear_cache=bool(data.get("clear_cache", False)),
        )
