"""Local persistence of the worker credentials.

After `register`, the server returns `worker_id` and `api_key`. They are stored
in `STATE_DIR/state.json` with mode 0600 so the next process start reuses the
identity without registering again.

Format:
    {"worker_id": "<uuid>", "api_key": "<opaque>"}

Concurrency: one worker process per STATE_DIR. No lockfile is used.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_STATE_FILENAME = "state.json"


@dataclass(frozen=True)
class Credentials:
    worker_id: str
    api_key: str


def state_path(state_dir: Path) -> Path:
    return state_dir / _STATE_FILENAME


def load(state_dir: Path) -> Credentials | None:
    p = state_path(state_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    wid = data.get("worker_id")
    key = data.get("api_key")
    if not wid or not key:
        return None
    return Credentials(worker_id=str(wid), api_key=str(key))


def save(state_dir: Path, creds: Credentials) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    # Best-effort: on POSIX the directory is restricted too.
    with contextlib.suppress(OSError):
        os.chmod(state_dir, stat.S_IRWXU)
    p = state_path(state_dir)
    payload = json.dumps(
        {"worker_id": creds.worker_id, "api_key": creds.api_key},
        indent=2,
    )
    # Atomic write: write tmp -> chmod 0600 -> rename.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    # Windows: chmod is not fully effective; the rename still happens.
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)
