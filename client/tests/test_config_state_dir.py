"""Default of the local state directory."""

from orcaster_worker.config import Settings, _default_state_dir


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORCASTER_HOME", raising=False)
    monkeypatch.delenv("ORCH_STATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)


def test_default_on_clean_home(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    assert _default_state_dir() == tmp_path / ".orcaster-worker"


def test_explicit_override_wins(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    explicit = tmp_path / "custom"
    monkeypatch.setenv("ORCH_STATE_DIR", str(explicit))
    assert Settings().state_dir == explicit


def test_orcaster_home_wins_over_default(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCASTER_HOME", str(tmp_path / "vol"))
    assert _default_state_dir() == tmp_path / "vol"
