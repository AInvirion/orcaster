"""Allow running the worker as `python -m orcaster_worker`."""

from __future__ import annotations

import sys

from orcaster_worker.main import cli_entry

if __name__ == "__main__":
    sys.exit(cli_entry())
