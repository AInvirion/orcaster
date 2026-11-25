"""Reader for the sync_status.json contract.

An external sync process writes this file after a successful run, counting the
working tree of the sync target repo. The app reads `uploaded_total` for the
artifact indicator (produced / uploaded).

Loading is safe by design: any failure (missing or unreadable file, corrupt
JSON, unknown schema_version) returns None and the UI degrades to showing only
`produced`. This function NEVER raises: the pill must not be able to break the
dashboard. There is no cache: the file is tiny and HTMX already polls.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, ValidationError

SUPPORTED_SCHEMA_VERSION = 1


class SyncStatus(BaseModel):
    """Status of the external sync job. `by_source` (counts per source) is part of
    the schema from v1 even though the UI shows only the total plus freshness
    (future-proofing)."""

    uploaded_total: int
    by_source: dict[str, int] = {}
    commit_sha: str | None = None
    pushed_at: datetime | None = None


def read_sync_status(path: str) -> SyncStatus | None:
    """Load `sync_status.json`. Returns None on any problem."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        return None
    try:
        return SyncStatus.model_validate(data)
    except ValidationError:
        return None
