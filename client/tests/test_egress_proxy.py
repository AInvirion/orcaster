from pathlib import Path

from orcaster_worker.egress_proxy import ProxyPolicy


def test_policy_base_only(tmp_path: Path):
    pol = ProxyPolicy(base=["pypi.org", "orchestrator.example"], allow_file=tmp_path / "a.txt")
    assert pol.allows("pypi.org", 443)
    assert not pol.allows("data.example.org", 443)


def test_policy_reads_per_task_file_live(tmp_path: Path):
    f = tmp_path / "a.txt"
    pol = ProxyPolicy(base=["pypi.org"], allow_file=f)
    assert not pol.allows("data.example.org", 443)
    f.write_text("data.example.org\n.example.net\n")
    assert pol.allows("data.example.org", 443)  # re-read on each call
    assert pol.allows("cdn.eu.example.net", 443)


def test_policy_blocks_nonstandard_ports(tmp_path: Path):
    pol = ProxyPolicy(base=["pypi.org"], allow_file=tmp_path / "a.txt")
    assert pol.allows("pypi.org", 443)
    assert pol.allows("pypi.org", 80)
    assert not pol.allows("pypi.org", 22)
    assert not pol.allows("pypi.org", 8080)
