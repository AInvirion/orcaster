"""Compatibility shim (deprecated): the package is named `orcaster_server`.

It exists so that an `import app.plugins` written before the rename (for example, an
already installed plugin) keeps resolving. Each `app.<path>` is the **same module
object** as `orcaster_server.<path>` (an alias in `sys.modules`, not a copy), so the
identity of classes such as `PluginRegistry` and `OutcomeRule` is preserved. There is
no second copy of the code.

Importing it emits a `DeprecationWarning`. It will be removed in a future release,
together with the other rename shims (`server/alembic.ini`, `server/scripts/*.py`):
plugins should import `orcaster_server`.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import warnings
from types import ModuleType

import orcaster_server

_OLD = __name__  # "app"
_NEW = "orcaster_server"

warnings.warn(
    f"`{_OLD}` is deprecated and will be removed in a future release: import `{_NEW}` instead",
    DeprecationWarning,
    stacklevel=2,
)


class _AliasLoader(importlib.abc.Loader):
    """Return the real `orcaster_server` module instead of executing a new one."""

    def __init__(self, real_name: str) -> None:
        self._real_name = real_name

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        return importlib.import_module(self._real_name)

    def exec_module(self, module: ModuleType) -> None:
        # The real module has already been executed; the alias has nothing to run.
        return None


class _AliasFinder(importlib.abc.MetaPathFinder):
    """Resolve `app.<path>` as `orcaster_server.<path>` at any depth."""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + "."):
            return None
        real_name = _NEW + fullname[len(_OLD) :]
        if importlib.util.find_spec(real_name) is None:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(real_name))


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

# `import app` only exposes the version; submodules are requested by name.
__version__ = orcaster_server.__version__
