from pathlib import Path

from orcaster_worker.egress import host_allowed, read_allowlist, write_allowlist


def test_exact_match():
    assert host_allowed("data.example.org", ["data.example.org"])
    assert not host_allowed("evil.com", ["data.example.org"])


def test_subdomain_wildcard():
    assert host_allowed("cdn.eu.example.net", [".example.net"])
    assert host_allowed("cdn.eu.example.net", ["*.example.net"])
    assert not host_allowed("example.net.evil.com", [".example.net"])
    # the suffix must fall on a label boundary
    assert not host_allowed("notexample.net", [".example.net"])


def test_case_insensitive_and_port():
    assert host_allowed("Data.Example.org:443", ["data.example.org"])


def test_empty_allowlist_denies():
    assert not host_allowed("data.example.org", [])


def test_roundtrip_file(tmp_path: Path):
    p = tmp_path / "allow.txt"
    write_allowlist(p, ["data.example.org", ".example.net"])
    assert read_allowlist(p) == ["data.example.org", ".example.net"]
    write_allowlist(p, [])
    assert read_allowlist(p) == []
    assert read_allowlist(tmp_path / "missing.txt") == []
