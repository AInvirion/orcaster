"""Server-side defaults of the PollRequest schema.

The worker always sends `lease_seconds`, but the schema default is the lease
the server would write if a poll omitted the field: it must match the worker
(120 s) as defense in depth, not fall back to the old 60 s.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orcaster_server.schemas.poll import PollRequest


def test_pollrequest_lease_default_is_120():
    """A poll without `lease_seconds` uses 120 s, not the old default of 60."""
    req = PollRequest(status="idle")
    assert req.lease_seconds == 120


def test_pollrequest_lease_respects_explicit_value():
    """If the worker sends `lease_seconds`, the schema keeps it (no override)."""
    req = PollRequest(status="idle", lease_seconds=300)
    assert req.lease_seconds == 300


@pytest.mark.parametrize("invalido", [0, 3601])
def test_pollrequest_lease_out_of_range_rejected(invalido):
    """The ge=1/le=3600 range still applies after the default change."""
    with pytest.raises(ValidationError):
        PollRequest(status="idle", lease_seconds=invalido)


# --- free_disk_fraction (server-side disk gate) ------------------------


def test_pollrequest_free_disk_fraction_default_none():
    """Additive: an older worker that does not send the field gives None (never gates)."""
    req = PollRequest(status="idle")
    assert req.free_disk_fraction is None


@pytest.mark.parametrize("value", [0.0, 0.05, 0.2, 0.999, 1.0])
def test_pollrequest_free_disk_fraction_valid_passes(value):
    """A finite value in [0.0, 1.0] is kept as is."""
    req = PollRequest(status="idle", free_disk_fraction=value)
    assert req.free_disk_fraction == value


@pytest.mark.parametrize(
    "garbage",
    [
        "not-a-number",  # unparseable string
        -0.1,  # below 0
        1.5,  # above 1
        float("nan"),
        float("inf"),
        float("-inf"),
        [0.5],  # impossible type
        {},
        True,  # bool is a subclass of int: float(True)=1.0; it is not a measurement
        False,  # float(False)=0.0 would gate the worker -> None (garbage)
    ],
)
def test_pollrequest_free_disk_fraction_lenient_maps_to_none(garbage):
    """The poll is a hot path: garbage/out of range gives None (fail-open), NOT 422.

    Rejecting with 422 would take the worker out of service over a malformed
    value; the correct meaning of "cannot measure" is None = do not gate (same
    criterion as the lenient normalizer of `version`).
    """
    req = PollRequest(status="idle", free_disk_fraction=garbage)
    assert req.free_disk_fraction is None


def test_pollrequest_free_disk_fraction_numeric_string_is_normalized():
    """A number that arrives as a string is parsed (lenient), not rejected."""
    req = PollRequest(status="idle", free_disk_fraction="0.3")
    assert req.free_disk_fraction == pytest.approx(0.3)
