"""Server-side disk gate config: `worker_claim_min_free_fraction`."""

import pytest
from pydantic import ValidationError

from orcaster_server.config import Settings


def test_min_free_fraction_default_is_020():
    """Without env, the default threshold is 0.20."""
    s = Settings()
    assert s.worker_claim_min_free_fraction == 0.20


def test_min_free_fraction_override_env(monkeypatch):
    """WORKER_CLAIM_MIN_FREE_FRACTION overrides the default."""
    monkeypatch.setenv("WORKER_CLAIM_MIN_FREE_FRACTION", "0.30")
    s = Settings()
    assert s.worker_claim_min_free_fraction == 0.30


def test_min_free_fraction_zero_disables(monkeypatch):
    """0.0 is valid: it disables the gate entirely."""
    monkeypatch.setenv("WORKER_CLAIM_MIN_FREE_FRACTION", "0.0")
    s = Settings()
    assert s.worker_claim_min_free_fraction == 0.0


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-0.1", "1.5"])
def test_min_free_fraction_malformed_fails_loudly(monkeypatch, bad):
    """A malformed threshold (NaN/inf/outside [0,1]) fails when building Settings.

    Silencing the error would be dangerous: `nan > 0.0` is False (Python disables the
    gate), but in Postgres `frac < NaN` is TRUE for every finite frac (NaN sorts at
    the top), so the SQL would exclude EVERY reporting worker. Failing loudly at
    startup is the correct behaviour.
    """
    monkeypatch.setenv("WORKER_CLAIM_MIN_FREE_FRACTION", bad)
    with pytest.raises(ValidationError):
        Settings()
