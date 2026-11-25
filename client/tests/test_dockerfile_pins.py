"""The `uv` image in the Dockerfiles is pinned by version and digest.

`ghcr.io/astral-sh/uv:latest` is a mutable reference: two builds of the
worker image on different days bring a different `uv`, and a change in that
image reaches the worker without review. The correct reference is
`ghcr.io/astral-sh/uv:<X.Y.Z>@sha256:<digest>`, in a single place in the file
(a named stage that both worker stages copy from). Bumping the version is
a reviewed change, just like `ruff` and the actions (`actions pinned by SHA`).

It is the image equivalent of the pin check in
`.github/workflows/ci.yml`. It runs with the rest of the client suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {".git", ".venv", ".worktrees", "node_modules"}
_UV_IMAGE = "ghcr.io/astral-sh/uv"
_PINNED_UV = re.compile(rf"^{re.escape(_UV_IMAGE)}:\d+\.\d+\.\d+@sha256:[0-9a-f]{{64}}$")


def _dockerfiles() -> list[Path]:
    found = []
    for path in _REPO_ROOT.rglob("*"):
        if _SKIP_DIRS.intersection(path.relative_to(_REPO_ROOT).parts):
            continue
        if path.is_file() and path.name in {"Dockerfile", "Containerfile"}:
            found.append(path)
    return sorted(found)


def _uv_references(path: Path) -> list[tuple[int, str]]:
    """(line, image reference) of each use of the `uv` image."""
    refs = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        code = line.split("#", 1)[0]
        for token in code.replace("--from=", " ").split():
            if token.startswith(_UV_IMAGE):
                refs.append((number, token))
    return refs


def test_the_repository_has_a_dockerfile_that_uses_uv() -> None:
    """Guard against an empty pass: if the Dockerfile moves, the check says so."""
    assert any(_uv_references(p) for p in _dockerfiles())


@pytest.mark.parametrize("dockerfile", _dockerfiles(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_uv_image_is_pinned_by_version_and_digest(dockerfile: Path) -> None:
    """Every reference to the uv image is `:<X.Y.Z>@sha256:<64 hex>`; never `latest`."""
    bad = [
        f"{dockerfile.relative_to(_REPO_ROOT)}:{number}: {ref}"
        for number, ref in _uv_references(dockerfile)
        if not _PINNED_UV.match(ref)
    ]
    assert not bad, "uv image without version and digest:\n" + "\n".join(bad)


def test_uv_image_is_set_in_one_place() -> None:
    """A single uv image reference per Dockerfile: a bump touches one line."""
    for dockerfile in _dockerfiles():
        refs = _uv_references(dockerfile)
        assert len(refs) <= 1, (
            f"{dockerfile.relative_to(_REPO_ROOT)} names the uv image {len(refs)} times "
            f"(lines {[n for n, _ in refs]}); use a named stage"
        )
