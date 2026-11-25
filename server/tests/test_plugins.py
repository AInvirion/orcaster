"""Plugin registry: loading, precedence and startup errors."""

from __future__ import annotations

from importlib import metadata

import pytest

from orcaster_server import plugins
from orcaster_server.plugins import (
    LegacyStatusAlias,
    OutcomeRule,
    PluginError,
    PluginRegistry,
    Provider,
    load_registry,
    parse_plugin_list,
)
from orcaster_server.task_variant import known_variant_kinds


def _provider(name: str, kind: str, label: str | None = "1.0") -> Provider:
    def register(registry: PluginRegistry) -> None:
        registry.add_variant_extractor(
            kind, lambda payload: label if payload.get("tool") == kind else None
        )

    return Provider(name=name, register=register)


def _ep(name: str, obj: object) -> metadata.EntryPoint:
    class _EP:
        value = f"fake:{name}"

        def __init__(self) -> None:
            self.name = name

        def load(self) -> object:
            if isinstance(obj, Exception):
                raise obj
            return obj

    return _EP()  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _fresh_registry():
    plugins.reset_registry()
    yield
    plugins.reset_registry()


def test_empty_registry_recognises_nothing() -> None:
    registry = load_registry(entry_points=[])
    assert registry.extractors == ()
    assert registry.kinds == frozenset()


def test_configured_plugins_win_in_written_order_then_entry_points_by_name() -> None:
    eps = [
        _ep("zeta", _provider("zeta", "zeta").register),
        _ep("beta", _provider("beta", "beta")),
        _ep("alpha", _provider("alpha", "alpha")),
    ]
    eps += [_ep("mid", _provider("mid", "mid"))]
    registry = load_registry(configured=["zeta", "mid"], entry_points=eps)
    assert [k for k, _ in registry.extractors] == ["zeta", "mid", "alpha", "beta"]


def test_load_registry_has_no_builtins_parameter() -> None:
    """The core ships no providers of its own: there is no way to inject them."""
    with pytest.raises(TypeError):
        load_registry(builtins=[_provider("builtin", "base")], entry_points=[])  # type: ignore[call-arg]


def test_first_recognising_extractor_wins() -> None:
    def greedy(registry: PluginRegistry) -> None:
        registry.add_variant_extractor("greedy", lambda payload: "g")

    def narrow(registry: PluginRegistry) -> None:
        registry.add_variant_extractor("narrow", lambda payload: "n")

    registry = load_registry(
        configured=["narrow", "greedy"],
        entry_points=[_ep("greedy", greedy), _ep("narrow", narrow)],
    )
    first_kind, extract = registry.extractors[0]
    assert (first_kind, extract({})) == ("narrow", "n")


def test_unknown_configured_name_fails() -> None:
    with pytest.raises(PluginError, match="not installed"):
        load_registry(configured=["ghost"], entry_points=[])


def test_plugin_that_fails_to_import_fails() -> None:
    with pytest.raises(PluginError, match="failed to import"):
        load_registry(entry_points=[_ep("bad", ImportError("boom"))])


def test_plugin_that_raises_while_registering_fails() -> None:
    def explode(registry: PluginRegistry) -> None:
        raise ValueError("nope")

    with pytest.raises(PluginError, match="failed to register"):
        load_registry(entry_points=[_ep("bad", explode)])


def test_object_without_register_fails() -> None:
    with pytest.raises(PluginError, match="neither callable"):
        load_registry(entry_points=[_ep("bad", 42)])


def test_duplicate_kind_fails() -> None:
    with pytest.raises(PluginError, match="already registered"):
        load_registry(
            entry_points=[_ep("a", _provider("a", "same")), _ep("b", _provider("b", "same"))]
        )


@pytest.mark.parametrize("kind", ["all", "other", "current", "Bad Kind", "", "a~b", "x" * 40])
def test_invalid_or_reserved_kind_fails(kind: str) -> None:
    with pytest.raises(PluginError, match="invalid variant kind"):
        load_registry(entry_points=[_ep("a", _provider("a", kind))])


def test_parse_plugin_list() -> None:
    assert parse_plugin_list(None) == []
    assert parse_plugin_list(" a , b,, ") == ["a", "b"]
    with pytest.raises(PluginError, match="more than once"):
        parse_plugin_list("a,b,a")


def _no_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_registry` with no entry point installed, whatever the environment."""
    real = metadata.entry_points

    def fake(**kwargs):
        if kwargs.get("group") == plugins.ENTRY_POINT_GROUP:
            return []
        return real(**kwargs)

    monkeypatch.setattr(plugins.metadata, "entry_points", fake)


def test_active_registry_without_plugins_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a plugin the core recognizes no variant and classifies no exit."""
    from orcaster_server.config import settings
    from orcaster_server.task_outcomes import classify_terminal, legacy_status_aliases
    from orcaster_server.task_variant import extract_variant

    _no_entry_points(monkeypatch)
    monkeypatch.setattr(settings, "plugins", "")
    registry = plugins.get_registry()
    assert registry.extractors == ()
    assert dict(registry.outcome_defaults) == {}
    assert dict(registry.legacy_status_aliases) == {}
    assert known_variant_kinds() == frozenset()
    payloads = [
        None,
        {},
        {"commands": ["uv pip install example-tool==1.2.3"]},
        {"commands": ["anything"], "requirements": ["pkg==1.0.0"], "x": {"kind": "package"}},
    ]
    for payload in payloads:
        assert extract_variant(payload) == (None, None)
    for code in (1, 3, 4, 5, 6, 7, 255):
        assert classify_terminal("failed", code, {"commands": ["x"]}) == ("failed", None)
    assert legacy_status_aliases() == {}


def test_empty_registry_logs_an_explicit_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="orcaster_server.plugins"):
        load_registry(entry_points=[])
    assert "none loaded" in caplog.text


def test_known_variant_kinds_attribute_follows_the_active_registry() -> None:
    """`KNOWN_VARIANT_KINDS` (module attribute) reads the active registry on each access."""
    from orcaster_server import task_variant

    _install(load_registry(entry_points=[]))
    assert frozenset() == task_variant.KNOWN_VARIANT_KINDS
    _install(load_registry(entry_points=[_ep("tool", _tool_provider)]))
    assert frozenset({"tool"}) == task_variant.KNOWN_VARIANT_KINDS
    with pytest.raises(AttributeError):
        task_variant.NOT_A_NAME  # noqa: B018


def test_active_registry_fails_on_configured_name_that_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orcaster_server.config import settings

    monkeypatch.setattr(settings, "plugins", "ghost")
    with pytest.raises(PluginError, match="not installed"):
        plugins.get_registry()


def test_outcome_defaults_require_an_extractor_of_the_kind() -> None:
    def register(registry: PluginRegistry) -> None:
        registry.add_outcome_defaults("orphan", {3: OutcomeRule(name="x", closes_as="failed")})

    with pytest.raises(PluginError, match="without a variant extractor"):
        load_registry(entry_points=[_ep("a", register)])


# --- outcome profile ----------------------------------------------------------


def _install(registry: plugins.Registry) -> None:
    plugins._active = registry


def _tool_provider(registry: PluginRegistry) -> None:
    registry.add_variant_extractor("tool", lambda p: "1.0" if p.get("tool") else None)
    registry.add_outcome_defaults("tool", {9: OutcomeRule(name="nine", closes_as="completed")})


def test_outcome_profile_of_a_plugin_classifies_its_kind_only() -> None:
    from orcaster_server.task_outcomes import classify_terminal

    _install(load_registry(entry_points=[_ep("tool", _tool_provider)]))
    assert classify_terminal("failed", 9, {"tool": True}) == ("completed", "nine")
    assert classify_terminal("failed", 9, {"other": True}) == ("failed", None)
    assert classify_terminal("failed", 8, {"tool": True}) == ("failed", None)


def test_payload_block_wins_over_the_plugin_profile() -> None:
    from orcaster_server.task_outcomes import classify_terminal

    _install(load_registry(entry_points=[_ep("tool", _tool_provider)]))
    payload = {
        "tool": True,
        "outcomes": {"exit_codes": {"9": {"name": "custom", "closes_as": "failed"}}},
    }
    assert classify_terminal("failed", 9, payload) == ("failed", "custom")


def test_empty_registry_classifies_no_exit_code_by_default() -> None:
    from orcaster_server.task_outcomes import classify_terminal

    _install(load_registry(entry_points=[]))
    for code in range(1, 256):
        assert classify_terminal("failed", code, {"commands": ["x"]}) == ("failed", None)
    # The payload block still holds without any plugin.
    block = {"outcomes": {"exit_codes": {"3": {"name": "mine", "closes_as": "completed"}}}}
    assert classify_terminal("failed", 3, block) == ("completed", "mine")


def test_outcome_defaults_reject_bad_rules() -> None:
    def bad_code(registry: PluginRegistry) -> None:
        registry.add_variant_extractor("tool", lambda p: None)
        registry.add_outcome_defaults("tool", {0: OutcomeRule(name="zero", closes_as="failed")})

    with pytest.raises(PluginError, match="exit codes 1-255"):
        load_registry(entry_points=[_ep("a", bad_code)])


def test_outcome_defaults_of_the_same_kind_cannot_be_registered_twice() -> None:
    def twice(registry: PluginRegistry) -> None:
        registry.add_variant_extractor("tool", lambda p: None)
        registry.add_outcome_defaults("tool", {})
        registry.add_outcome_defaults("tool", {})

    with pytest.raises(PluginError, match="already registered"):
        load_registry(entry_points=[_ep("a", twice)])


def test_core_modules_expose_no_builtin_provider() -> None:
    """Domain modules no longer register behavior of their own."""
    from orcaster_server import task_outcomes, task_variant

    for name in ("register_builtin_variants", "_BUILTIN_EXTRACTORS"):
        assert not hasattr(task_variant, name)
    for name in ("register_builtin_outcomes", "DEFAULT_OUTCOMES_BY_VARIANT_KIND"):
        assert not hasattr(task_outcomes, name)
    assert not hasattr(plugins, "_builtin_providers")


# --- retired status aliases ---------------------------------------------------


def _alias_provider(*aliases: tuple) -> object:
    def register(registry: PluginRegistry) -> None:
        for args in aliases:
            *positional, status = args if len(args) == 3 else (*args, None)
            if status is None:
                registry.add_legacy_status_alias(*positional)
            else:
                registry.add_legacy_status_alias(*positional, status=status)

    return register


def test_legacy_status_alias_is_part_of_the_outcome_profile() -> None:
    from orcaster_server.task_outcomes import legacy_status_aliases

    registry = load_registry(
        entry_points=[
            _ep(
                "a",
                _alias_provider(
                    ("completed_empty", "empty_result"), ("failed_old", "old", "failed")
                ),
            )
        ]
    )
    assert dict(registry.legacy_status_aliases) == {
        "completed_empty": LegacyStatusAlias(status="completed", outcome="empty_result"),
        "failed_old": LegacyStatusAlias(status="failed", outcome="old"),
    }
    _install(registry)
    assert legacy_status_aliases()["completed_empty"].outcome == "empty_result"


def test_legacy_status_alias_duplicated_fails() -> None:
    with pytest.raises(PluginError, match="already registered"):
        load_registry(
            entry_points=[_ep("a", _alias_provider(("old_token", "x"), ("old_token", "y")))]
        )


def test_legacy_status_alias_duplicated_across_plugins_fails() -> None:
    with pytest.raises(PluginError, match="'b'.*already registered"):
        load_registry(
            entry_points=[
                _ep("a", _alias_provider(("old_token", "x"))),
                _ep("b", _alias_provider(("old_token", "x"))),
            ]
        )


@pytest.mark.parametrize(
    "token",
    ["completed", "failed", "queued", "running", "all", "other", "Bad", "1x", "", "a-b", None],
)
def test_legacy_status_alias_with_an_invalid_token_fails(token: object) -> None:
    with pytest.raises(PluginError, match="invalid legacy status alias"):
        load_registry(entry_points=[_ep("a", _alias_provider((token, "fine")))])


@pytest.mark.parametrize("outcome", ["", "Bad", "completed", "other", "x" * 65, 3])
def test_legacy_status_alias_with_an_invalid_outcome_fails(outcome: object) -> None:
    with pytest.raises(PluginError, match="invalid outcome"):
        load_registry(entry_points=[_ep("a", _alias_provider(("old_token", outcome)))])


@pytest.mark.parametrize("status", ["cancelled", "queued", "all", ""])
def test_legacy_status_alias_with_an_invalid_target_status_fails(status: str) -> None:
    with pytest.raises(PluginError, match="must translate to one of"):
        load_registry(entry_points=[_ep("a", _alias_provider(("old_token", "fine", status)))])
