"""Compatibility shim: runs `orcaster_server.cli.task_secrets_keys`.

Kept for one release for callers that invoke it by path (`python scripts/task_secrets_keys.py`).
Use the equivalent console command instead (see `[project.scripts]` in pyproject).
"""

import sys

from orcaster_server.cli.task_secrets_keys import main

if __name__ == "__main__":
    sys.exit(main())
