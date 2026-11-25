"""In-process handlers for tasks with a reserved command (`__name__`).

Available reserved commands:
    * `__reload_config__` — re-reads `.env` and logs. Informational no-op.

Each handler returns an `InternalResult` that `_dispatch_poll_task` turns into
a `complete()` call on the API. The complete status is mapped as follows:

    InternalResult.exit_code == 0  → status='completed'
    InternalResult.exit_code != 0  → status='failed'

This keeps the contract uniform with regular tasks (run_batch).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from orcaster_worker.config import Settings

log = logging.getLogger("orcaster.worker.internal_handlers")


RESERVED_COMMANDS: frozenset[str] = frozenset(
    {
        "__reload_config__",
    }
)


@dataclass(frozen=True)
class InternalResult:
    """Uniform result of a reserved handler.

    The `result` dict (when applicable) travels to the server as synthetic output
    (`_emit_synthetic_output` → POST /output); `/complete` only carries
    status/exit_code. It is also persisted in `result_json` for idempotency:
    the client needs it to resend the idempotent complete.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    result: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        return "completed" if self.exit_code == 0 else "failed"


async def handle_reserved(
    command: str,
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> InternalResult:
    """Dispatch a reserved command. If the command is not recognized, returns
    `unsupported` (exit_code=2).
    """
    if command == "__reload_config__":
        return await _handle_reload_config(payload, settings)
    log.warning("unknown reserved command=%r; treating as unsupported", command)
    return InternalResult(
        exit_code=2,
        stderr=f"unknown reserved command: {command!r}",
    )


async def _handle_reload_config(payload: dict[str, Any], settings: Settings) -> InternalResult:
    """Log the intent to reload config. A real no-op for now.

    Reloading `Settings` requires re-reading `.env`, which is trivial with
    pydantic-settings; but since there is (yet) no caller that mutates the
    worker's runtime state based on the config, the handler reports without
    mutating.
    """
    log.info(
        "__reload_config__ received (payload keys=%s); no-op",
        sorted(payload.keys()) if isinstance(payload, dict) else "<non-dict>",
    )
    return InternalResult(
        exit_code=0,
        stdout="config reload acknowledged (no-op in this build)",
        result={"reloaded": False, "reason": "no runtime mutation"},
    )
