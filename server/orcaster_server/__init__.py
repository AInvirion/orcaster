"""Orchestrator server package.

Exposes a single runtime accessor for the version, mirroring the pattern of the
Python client (`orcaster_worker.package_version`). The version number lives in
`pyproject.toml` as the build source; at runtime that file is NOT read (it may
not be present in the deploy target), and the version is derived from the metadata
of the installed package.
"""

from __future__ import annotations

import importlib.metadata

_DIST_NAME = "orcaster-server"
VERSION_UNKNOWN = "0.0.0+unknown"


def package_version() -> str:
    """Server version, derived from the installed package.

    Single runtime source for the dashboard. It never raises and never returns an
    empty value: a failure here is cosmetic and must not take down a full-page
    handler. `importlib.metadata.version` fails in two non-obvious ways, and both
    fall back to the same obvious marker instead of staying silent:

      - `PackageNotFoundError`: the source tree runs without being installed (the
        deploy target does not guarantee `pip install`).
      - returning `None`: the `.dist-info` exists but has no METADATA (truncated
        installation).
    """
    try:
        found = importlib.metadata.version(_DIST_NAME)
    except Exception:  # noqa: BLE001 - see docstring: never break over a cosmetic value
        found = None
    return found or VERSION_UNKNOWN


__version__ = package_version()
