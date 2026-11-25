"""Constants of the workspaces module.

`DEFAULT_WORKSPACE_ID`: deterministic UUID of the `Default` workspace. It is the
single source at runtime; it must match the literal frozen in
`orcaster_server/migrations/versions/0021_workspaces.py` (that value is the
historical anchor and is not imported from here, to keep the migration
self-contained).
"""

from __future__ import annotations

DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
