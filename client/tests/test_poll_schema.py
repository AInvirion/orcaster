from __future__ import annotations

from orcaster_worker.poll import PollRequest, PollResponse


def test_request_omits_ack_when_false() -> None:
    body = PollRequest(status="idle").to_json()
    assert "cache_flush_completed" not in body


def test_request_includes_ack_when_true() -> None:
    body = PollRequest(status="idle", cache_flush_completed=True).to_json()
    assert body["cache_flush_completed"] is True


def test_response_parses_clear_cache_true() -> None:
    resp = PollResponse.from_wire({"clear_cache": True})
    assert resp.clear_cache is True


def test_response_defaults_clear_cache_false() -> None:
    resp = PollResponse.from_wire({})
    assert resp.clear_cache is False


def test_to_json_includes_cpu_pct_when_set() -> None:
    req = PollRequest(status="idle", cpu_pct=55)
    assert req.to_json()["cpu_pct"] == 55


def test_to_json_omits_cpu_pct_when_none() -> None:
    req = PollRequest(status="idle")
    assert "cpu_pct" not in req.to_json()


def test_to_json_includes_free_disk_fraction_when_set() -> None:
    """The disk gate lives in the server: the worker sends the free fraction."""
    req = PollRequest(status="idle", free_disk_fraction=0.03)
    assert req.to_json()["free_disk_fraction"] == 0.03


def test_to_json_omits_free_disk_fraction_when_none() -> None:
    """`None` is the fallback to `/` (cache_root was not measured): the server treats
    the absence as "unknown, do not gate"."""
    req = PollRequest(status="idle")
    assert "free_disk_fraction" not in req.to_json()


def test_to_json_includes_version_when_set() -> None:
    req = PollRequest(status="idle", version="0.16.0")
    assert req.to_json()["version"] == "0.16.0"


def test_to_json_omits_version_when_none() -> None:
    req = PollRequest(status="idle")
    assert "version" not in req.to_json()


def test_to_json_omits_version_when_empty() -> None:
    """A bare `ORCH_VERSION=` in the .env must not overwrite the stored version.

    Absent means "no data" and the server leaves the column as it is;
    sending "" would overwrite it with an empty value.
    """
    req = PollRequest(status="idle", version="")
    assert "version" not in req.to_json()
