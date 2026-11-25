"""Compatibility shim: runs `orcaster_server.cli.workspace_quota`.

Kept for one release for callers that invoke it by path (`python scripts/workspace_quota.py`).
Use the equivalent console command instead (see `[project.scripts]` in pyproject).
"""

import sys

from orcaster_server.cli.workspace_quota import main

if __name__ == "__main__":
    sys.exit(main())
