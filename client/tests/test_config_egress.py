"""Lazy default (default_factory) of Settings.egress_allow_file.

Regression: the original field was a plain default ``Path(os.environ.get(...))``
evaluated once when the class body ran (import time), not when
instantiating ``Settings()``. A test or caller that sets the env var at
runtime (via monkeypatch, or a long-lived process that changes it) would not
see it reflected. The correct pattern (same as ``state_dir`` /
``_default_state_dir``) is ``Field(default_factory=...)``.
"""

from pathlib import Path

from orcaster_worker.config import Settings

_ENV = "ORCASTER_EGRESS_ALLOW_FILE"


def test_egress_allow_file_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    s = Settings()
    assert s.egress_allow_file == Path("/var/lib/orcaster-egress/allow.txt")


def test_egress_allow_file_reflects_env_at_instantiation(monkeypatch) -> None:
    """Setting the env var BEFORE instantiating Settings() must be reflected --
    it proves the default is evaluated lazily (default_factory), not once at
    module import."""
    monkeypatch.setenv(_ENV, "/tmp/custom-egress/allow.txt")
    s = Settings()
    assert s.egress_allow_file == Path("/tmp/custom-egress/allow.txt")


def test_egress_allow_file_factory_reads_env_each_call(monkeypatch) -> None:
    """Two instances with different env values must diverge -- if the default
    were evaluated once at import, both instances would share the same fixed
    Path regardless of the current env var."""
    monkeypatch.setenv(_ENV, "/tmp/first/allow.txt")
    first = Settings()
    monkeypatch.setenv(_ENV, "/tmp/second/allow.txt")
    second = Settings()
    assert first.egress_allow_file == Path("/tmp/first/allow.txt")
    assert second.egress_allow_file == Path("/tmp/second/allow.txt")
