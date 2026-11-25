"""The Postgres major matches in CI, `Makefile` and the local fixture.

Production runs Postgres 17. If the local test DB has a different major than
CI and the development target, a migration or a query may pass in one place
and fail in another.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRODUCTION_MAJOR = "17"


def _major(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    assert match, f"{pattern!r} not found"
    return match.group(1)


def test_local_test_fixture_uses_the_production_major() -> None:
    conftest = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    assert _major(r'_PG_IMAGE = "[^"]*postgres:(\d+)', conftest) == _PRODUCTION_MAJOR


def test_makefile_and_ci_use_the_same_major_as_the_fixture() -> None:
    makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert _major(r"PG_IMAGE\s*\?=\s*\S*postgres:(\d+)", makefile) == _PRODUCTION_MAJOR
    assert _major(r"image:\s*postgres:(\d+)", ci) == _PRODUCTION_MAJOR
