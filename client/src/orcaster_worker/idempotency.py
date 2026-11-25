"""Idempotency of push commands.

Local sqlite at `STATE_DIR/commands_seen.db` (mode 0600, same as
`state.json`). Schema:

    CREATE TABLE commands_seen (
        uuid       TEXT PRIMARY KEY,
        acked_at   TEXT NOT NULL  -- ISO-8601 UTC
    )

If the same `uuid` arrives twice (because the previous ACK was lost or the
server resends until it is confirmed), `is_seen()` returns True and the caller
must resend the ACK with `status='already_acked'` without re-executing anything.

We use synchronous sqlite3 (not aiosqlite): writes are few (≤1 per command) and
`asyncio.to_thread` is enough to avoid blocking the loop. Keeping a single
connection per process avoids the open/close overhead, but for simplicity and
test isolation we open one per operation here.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orcaster_worker.secret_redaction import SecretRedactor

_DB_FILENAME = "commands_seen.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands_seen (
    uuid     TEXT PRIMARY KEY,
    acked_at TEXT NOT NULL
)
"""

# Table of the unified endpoint: persists the result of tasks delivered with a
# non-null `idempotency_key`. If the server resends the same `idempotency_key`
# (because the previous complete was lost), the client resends the stored
# result without re-executing.
_SCHEMA_PUSH_RESULTS = """
CREATE TABLE IF NOT EXISTS push_task_results (
    idempotency_key TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    exit_code       INTEGER,
    stdout          TEXT,
    stderr          TEXT,
    result_json     TEXT,
    completed_at    TEXT NOT NULL
)
"""


def db_path(state_dir: Path) -> Path:
    return state_dir / _DB_FILENAME


def _connect(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(state_dir, stat.S_IRWXU)
    path = db_path(state_dir)
    new_file = not path.exists()
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    conn.execute(_SCHEMA_PUSH_RESULTS)
    if new_file:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return conn


def is_seen(state_dir: Path, uuid: str) -> bool:
    """True if the `uuid` was already marked (regardless of the ACK status)."""
    conn = _connect(state_dir)
    try:
        row = conn.execute(
            "SELECT 1 FROM commands_seen WHERE uuid = ?",
            (uuid,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_seen(state_dir: Path, uuid: str) -> None:
    """Insert the `uuid` as seen. Idempotent: a second call is a no-op."""
    conn = _connect(state_dir)
    try:
        now = datetime.now(tz=UTC).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO commands_seen (uuid, acked_at) VALUES (?, ?)",
            (uuid, now),
        )
    finally:
        conn.close()


# --- Push tasks with idempotency_key ----------------


def get_push_result(state_dir: Path, idempotency_key: str) -> dict[str, Any] | None:
    """Return the persisted result if `idempotency_key` was already executed.

    Shape of the dict (consumable by `_safe_complete_poll`):
        {status, exit_code, stdout, stderr, result}
    Where `result` is the optional (JSON-deserialized) dict that came with the
    original execution; None if there was no result dict.

    If the `idempotency_key` does not exist, returns None.
    """
    conn = _connect(state_dir)
    try:
        row = conn.execute(
            """SELECT status, exit_code, stdout, stderr, result_json
                 FROM push_task_results WHERE idempotency_key = ?""",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        result_obj = json.loads(row[4]) if row[4] is not None else None
        return {
            "status": row[0],
            "exit_code": row[1],
            "stdout": row[2],
            "stderr": row[3],
            "result": result_obj,
        }
    finally:
        conn.close()


def store_push_result(
    state_dir: Path,
    idempotency_key: str,
    *,
    status: str,
    exit_code: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    result: dict[str, Any] | None = None,
    redactor: SecretRedactor | None = None,
) -> None:
    """Persist the result of a task with `idempotency_key`.

    With `redactor`, stdout, stderr and the strings of `result` are stored with
    the server's secrets masked: the stored result is resent and must not carry
    values the original execution did not send.

    Idempotent: a second call with the same key is a no-op (`INSERT OR IGNORE`).
    This guarantees that if complete() failed but the result was already stored,
    we do not overwrite it with a new state (the first execution is the one that
    counts for that key).
    """
    if redactor is not None:
        stdout = redactor.redact(stdout) if stdout is not None else None
        stderr = redactor.redact(stderr) if stderr is not None else None
        result = redactor.redact_obj(result) if result is not None else None
    conn = _connect(state_dir)
    try:
        now = datetime.now(tz=UTC).isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO push_task_results
                 (idempotency_key, status, exit_code, stdout, stderr,
                  result_json, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                idempotency_key,
                status,
                exit_code,
                stdout,
                stderr,
                json.dumps(result) if result is not None else None,
                now,
            ),
        )
    finally:
        conn.close()
