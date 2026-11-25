"""Pure validation of the name stored as `users.display_name`."""

from __future__ import annotations

import pytest

from orcaster_server.auth.router import _clean_display_name, _is_placeholder_name


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "workspace:11111111-2222-3333-4444-555555555555",
        " Workspace:11111111-2222-3333-4444-555555555555",
        "x" * 201,
    ],
)
def test_rejects_blank_placeholder_and_oversized(raw: str) -> None:
    assert _clean_display_name(raw) is None


def test_trims_real_name() -> None:
    assert _clean_display_name("  Ana Pérez ") == "Ana Pérez"


def test_placeholder_detection_requires_leading_workspace_uuid() -> None:
    assert _is_placeholder_name("workspace:11111111-2222-3333-4444-555555555555")
    assert not _is_placeholder_name("Workspace: Ana")
    assert not _is_placeholder_name("My workspace: notes")
