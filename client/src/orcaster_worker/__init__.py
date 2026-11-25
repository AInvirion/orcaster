"""Worker agent.

Modules:
  - config: pydantic-settings with the worker env vars.
  - state:  local persistence of WORKER_ID + API_KEY.
  - api:    httpx.AsyncClient wrapper for `/v1/workers/*` and `/v1/tasks/*`.
  - runner: subprocess execution in its own process group.
  - main:   register → claim → execute → complete loop.
"""

import importlib.metadata
from pathlib import Path

_DIST_NAME = "orcaster-worker"
_UNKNOWN = "0.0.0+unknown"
_PATCHED = "patched"


def _distribution() -> importlib.metadata.Distribution:
    return importlib.metadata.distribution(_DIST_NAME)


def _installed_version() -> str | None:
    return importlib.metadata.version(_DIST_NAME)


def _source_matches_installed() -> bool:
    """Is the loaded package byte for byte the one the metadata describes?

    In production the worker runs with `PYTHONPATH=/app/src` and the host
    checkout bind-mounted there, while the metadata comes from the `dist-info`
    baked in when the image was built. `make up client` rebuilds, so on that
    path they match; but `make restart client` (the README recipe for applying
    edits), a `podman restart` or a start from the autostart systemd unit
    rebuild nothing, and then the reported version describes the image and not
    the code that is actually running.

    When in doubt it returns True (no marker): a false "patched" is worse than
    not warning, and this runs at package import.
    """
    try:
        loaded = Path(__file__).resolve().parent
        installed = Path(_distribution().locate_file("orcaster_worker")).resolve()
        # Same directory (dev venv) or editable install, where `locate_file`
        # points to a site-packages that does not exist: there are no two copies
        # that could diverge and no file has to be read.
        if loaded == installed or not installed.is_dir():
            return True
        ours = {p.relative_to(loaded): p for p in loaded.rglob("*.py")}
        theirs = {p.relative_to(installed): p for p in installed.rglob("*.py")}
        if ours.keys() != theirs.keys():
            return False
        return all(ours[k].read_bytes() == theirs[k].read_bytes() for k in ours)
    except Exception:  # noqa: BLE001 - see docstring: never break the import
        return True


def package_version() -> str:
    """Worker version, derived from the installed package.

    Single source for `__version__`, the default of `Settings.version` and the
    CLI `--version`: literals drift out of sync silently, and the one that
    lived in `Settings` made the whole fleet report `0.1.0` to the dashboard.

    Never raises and never returns empty. It runs at package import and feeds a
    `str`-typed field (`Settings.version`), so a failure here would take the
    whole worker down over a cosmetic value. `importlib.metadata.version` has
    two non-obvious failure modes: `PackageNotFoundError` (source tree not
    installed) and returning `None` when the `.dist-info` exists but has no
    METADATA (truncated install). Both fall to the same obvious marker.

    When the loaded code does not match what the metadata describes, the
    version carries the local segment `+patched`. It is the only way for the
    dashboard not to claim something false about a hot-patched worker. The cost
    is reading the package `.py` files once per process, a few dozen small files.
    """
    try:
        found = _installed_version()
    except Exception:  # noqa: BLE001 - see docstring: never break the import
        found = None
    if not found:
        return _UNKNOWN
    if _source_matches_installed():
        return found
    # PEP 440 local segments are separated with `.`, so a version that already
    # has one is extended instead of carrying a second `+`.
    sep = "." if "+" in found else "+"
    return f"{found}{sep}{_PATCHED}"


__version__ = package_version()
