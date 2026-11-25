"""`ORCH_CAPABILITIES`: the worker declares its capabilities from its config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orcaster_worker.config import Settings, parse_capabilities
from orcaster_worker.poll import PollRequest

_ENV = "ORCH_CAPABILITIES"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """`Settings` also reads the cwd `.env`: it is isolated the same way as in
    `test_config_version.py`."""
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


class TestSettings:
    def test_without_variable_declares_nothing(self, clean_env):
        assert Settings().capabilities is None

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_declares_nothing(self, clean_env, raw):
        """A bare `ORCH_CAPABILITIES=` in the .env does not erase the stored ones."""
        clean_env.setenv(_ENV, raw)
        assert Settings().capabilities is None

    def test_flat_map(self, clean_env):
        clean_env.setenv(_ENV, '{"model": true, "model_context": 8192}')
        assert Settings().capabilities == {"model": True, "model_context": 8192}

    def test_empty_map_is_explicit(self, clean_env):
        clean_env.setenv(_ENV, "{}")
        assert Settings().capabilities == {}

    @pytest.mark.parametrize(
        "raw",
        [
            "model",  # not JSON
            '["model"]',
            '"model"',
            '{"model": "llama"}',
            '{"model": null}',
            '{"model": {"nested": true}}',
            '{"model": [1]}',
            '{"model ctx": true}',
            '{"1model": true}',
            '{"model_context": NaN}',
            '{"model_context": Infinity}',
            '{"model_context": 1e400}',  # json.loads gives inf
        ],
    )
    def test_invalid_fails_at_startup(self, clean_env, raw):
        """The server ignores an invalid value in the poll; the error has to
        show up here, where the operator sees it."""
        clean_env.setenv(_ENV, raw)
        with pytest.raises(ValidationError, match="ORCH_CAPABILITIES"):
            Settings()


def test_parse_accepts_finite_float_and_names_with_dot_and_dash():
    assert parse_capabilities('{"gpu.count": 2, "os-family": 1.5, "_x": false}') == {
        "gpu.count": 2,
        "os-family": 1.5,
        "_x": False,
    }


class TestPollBody:
    def test_omits_capabilities_if_none_declared(self):
        assert "capabilities" not in PollRequest(status="idle").to_json()

    def test_includes_declared_capabilities(self):
        body = PollRequest(status="idle", capabilities={"model": True}).to_json()
        assert body["capabilities"] == {"model": True}

    def test_includes_empty_map(self):
        """`{}` is sent: it is the explicit way to erase the stored ones."""
        assert PollRequest(status="idle", capabilities={}).to_json()["capabilities"] == {}
