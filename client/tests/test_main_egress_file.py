from pathlib import Path

from orcaster_worker.egress import read_allowlist
from orcaster_worker.main import _apply_task_egress, _clear_task_egress


def test_apply_writes_domains(tmp_path: Path):
    f = tmp_path / "allow.txt"
    _apply_task_egress(f, ["data.example.org", "github.com"])
    assert read_allowlist(f) == ["data.example.org", "github.com"]


def test_apply_none_leaves_file_untouched(tmp_path: Path):
    f = tmp_path / "allow.txt"
    f.write_text("preexisting.example\n")
    _apply_task_egress(f, None)
    assert read_allowlist(f) == ["preexisting.example"]


def test_clear_empties(tmp_path: Path):
    f = tmp_path / "allow.txt"
    _apply_task_egress(f, ["data.example.org"])
    _apply_task_egress(f, [])
    assert read_allowlist(f) == []


def test_apply_drops_entries_with_embedded_newlines(tmp_path: Path, caplog):
    """A domain with an embedded newline must not be able to inject an extra
    entry into the allowlist (round-trip to two lines in write_allowlist)."""
    f = tmp_path / "allow.txt"
    with caplog.at_level("WARNING"):
        _apply_task_egress(f, ["data.example.org", "evil.com\nfoo.com", "github.com"])
    assert read_allowlist(f) == ["data.example.org", "github.com"]
    assert "evil.com" in caplog.text


def test_apply_drops_empty_string_entries(tmp_path: Path, caplog):
    """An empty entry ("") survives the embedded-whitespace check
    (it has no characters to inspect) and would produce a blank line
    in the allowlist if it were not explicitly discarded."""
    f = tmp_path / "allow.txt"
    with caplog.at_level("WARNING"):
        _apply_task_egress(f, ["data.example.org", "", "github.com"])
    assert read_allowlist(f) == ["data.example.org", "github.com"]
    assert "dropped" in caplog.text


def test_apply_task_egress_swallows_write_failure(tmp_path: Path, monkeypatch, caplog):
    """If writing the allowlist fails (e.g. EROFS with EGRESS_ISOLATION=0 in a
    --read-only container, where /var/lib/orcaster-egress does not exist as a
    volume), _apply_task_egress must not propagate: that would overwrite the real
    result of dispatch_run_batch and the generic "runner crashed" handler
    would mark as failed a task whose runner never actually ran."""

    def _boom(path: Path, domains: list[str], key: bytes | None = None) -> None:
        raise OSError("[Errno 30] Read-only file system")

    monkeypatch.setattr("orcaster_worker.main.write_allowlist", _boom)
    f = tmp_path / "allow.txt"
    with caplog.at_level("WARNING"):
        _apply_task_egress(f, ["data.example.org"])  # must not raise
    assert "failed to write egress allowlist file" in caplog.text
    assert str(f) in caplog.text


def test_clear_task_egress_swallows_write_failure(tmp_path: Path, monkeypatch, caplog):
    """If the clear in the finally raises (disk full/permissions), it must not
    propagate: that would overwrite the real result of dispatch_run_batch and the
    generic "runner crashed" handler would mark a successful task as failed.

    The warning is now logged one level lower, in _apply_task_egress
    (which catches OSError directly); the try/except in _clear_task_egress
    remains as an additional safety net for non-OSError exceptions."""

    def _boom(path: Path, domains: list[str], key: bytes | None = None) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("orcaster_worker.main.write_allowlist", _boom)
    f = tmp_path / "allow.txt"
    with caplog.at_level("WARNING"):
        _clear_task_egress(f, ["data.example.org"])  # must not raise
    assert "failed to write egress allowlist file" in caplog.text
    assert str(f) in caplog.text


def test_clear_task_egress_succeeds_normally(tmp_path: Path):
    f = tmp_path / "allow.txt"
    _apply_task_egress(f, ["data.example.org"])
    _clear_task_egress(f, ["data.example.org"])
    assert read_allowlist(f) == []


def test_clear_task_egress_none_leaves_file_untouched(tmp_path: Path):
    """The tri-state lives in the helper: with egress_allowlist None (legacy) the
    clear does not touch the file, mirroring _apply_task_egress."""
    f = tmp_path / "allow.txt"
    f.write_text("preexisting.example\n")
    _clear_task_egress(f, None)
    assert read_allowlist(f) == ["preexisting.example"]
