"""Compatibility shim: runs `orcaster_server.cli.dev_invite`.

Kept for one release for callers that invoke it by path (`python scripts/dev_invite.py`).
Use the equivalent console command instead (see `[project.scripts]` in pyproject).
"""

import sys

from orcaster_server.cli.dev_invite import main

if __name__ == "__main__":
    sys.exit(main())
