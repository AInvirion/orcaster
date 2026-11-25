"""The `app` shim resolves to the same code as `orcaster_server` and warns that it is deprecated."""

import importlib
import sys
import warnings

import pytest


def _fresh_import_app() -> list[warnings.WarningMessage]:
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("app")
    return caught


def test_app_import_warns_deprecation():
    caught = _fresh_import_app()
    assert any(
        issubclass(w.category, DeprecationWarning)
        and "will be removed in a future release" in str(w.message)
        for w in caught
    )


@pytest.mark.parametrize("sub", ["plugins", "task_outcomes", "task_variant", "api.v1"])
def test_app_submodule_is_the_same_module_object(sub):
    _fresh_import_app()
    old = importlib.import_module(f"app.{sub}")
    new = importlib.import_module(f"orcaster_server.{sub}")
    assert old is new


def test_class_identity_is_preserved():
    _fresh_import_app()
    from app.plugins import OutcomeRule, PluginRegistry

    from orcaster_server import plugins

    assert OutcomeRule is plugins.OutcomeRule
    assert PluginRegistry is plugins.PluginRegistry


def test_unknown_submodule_still_fails():
    _fresh_import_app()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.does_not_exist")
