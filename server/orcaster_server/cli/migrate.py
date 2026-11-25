"""`orcaster-migrate`: applies the packaged alembic migrations.

Usage: `orcaster-migrate [TARGET]` (default `head`). Reads `DATABASE_URL` from
the environment (or from `.env` in the working directory, via `migrations/env.py`).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config

_PACKAGE_DIR = Path(__file__).resolve().parent.parent


def alembic_config() -> Config:
    """Alembic config pointing to the package's `alembic.ini` and migrations."""
    cfg = Config(str(_PACKAGE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PACKAGE_DIR / "migrations"))
    return cfg


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply Orcaster server database migrations")
    parser.add_argument(
        "target",
        nargs="?",
        default="head",
        help="alembic revision to upgrade to (default: head)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Console entry point."""
    args = parse_args(argv)
    command.upgrade(alembic_config(), args.target)


if __name__ == "__main__":
    main()
