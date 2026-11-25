from orcaster_worker.main import _extract_isolation_payload


def test_absent_keys_are_none():
    assert _extract_isolation_payload({"commands": ["x"]}) == (None, None)


def test_present_empty_lists():
    assert _extract_isolation_payload({"task_env": [], "egress_allowlist": []}) == ([], [])


def test_present_values():
    got = _extract_isolation_payload(
        {"task_env": ["GITHUB_TOKEN"], "egress_allowlist": ["data.example.org"]}
    )
    assert got == (["GITHUB_TOKEN"], ["data.example.org"])


def test_malformed_is_treated_as_none():
    assert _extract_isolation_payload({"task_env": "x", "egress_allowlist": [1]}) == (None, None)
