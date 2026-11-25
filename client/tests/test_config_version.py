"""The worker must report its real version, not a frozen literal."""

import importlib.metadata
from importlib.metadata import version as pkg_version

from orcaster_worker import package_version
from orcaster_worker.config import Settings

_ENV_VERSION = "ORCH_VERSION"


class TestVersionDefault:
    def test_default_is_the_installed_package_version(self, monkeypatch, tmp_path):
        """Without ORCH_VERSION in the environment, Settings reports the wheel's version.

        The `chdir` is not cosmetic: `Settings` also reads the cwd `.env`
        (`model_config.env_file`), so clearing only the process variable
        lets a developer checkout's `.env` with `ORCH_VERSION=`
        fail this test for a reason unrelated to the code under test.
        """
        monkeypatch.delenv(_ENV_VERSION, raising=False)
        monkeypatch.chdir(tmp_path)
        s = Settings()
        assert s.version == pkg_version("orcaster-worker")

    def test_orch_version_remains_an_explicit_override(self, monkeypatch, tmp_path):
        """The ORCH_VERSION contract is kept in order to force a different value."""
        monkeypatch.setenv(_ENV_VERSION, "9.9.9-test")
        monkeypatch.chdir(tmp_path)
        s = Settings()
        assert s.version == "9.9.9-test"


class TestPackageVersion:
    def test_dunder_version_comes_from_installed_package(self):
        """`__version__` cannot be a literal that drifts out of sync."""
        from orcaster_worker import __version__

        assert __version__ == pkg_version("orcaster-worker")

    def test_uninstalled_package_falls_back_to_marker(self, monkeypatch):
        """Source tree without installing: an obvious marker, not a plausible number."""

        def _raise(_name):
            raise importlib.metadata.PackageNotFoundError("orcaster-worker")

        monkeypatch.setattr(importlib.metadata, "version", _raise)
        assert package_version() == "0.0.0+unknown"

    def test_incomplete_metadata_returns_none_without_breaking(self, monkeypatch):
        """`importlib.metadata.version` returns None for a dist-info without METADATA.

        It does not raise `PackageNotFoundError`, so a guard that only catches that
        exception lets `None` through to `Settings.version`, typed `str`: the worker
        would die with a ValidationError pointing at ORCH_VERSION on startup.
        """
        monkeypatch.setattr(importlib.metadata, "version", lambda _name: None)
        assert package_version() == "0.0.0+unknown"

    def test_unexpected_metadata_error_does_not_break_import(self, monkeypatch):
        """`package_version` runs on package import: it cannot propagate."""

        def _boom(_name):
            raise OSError("unreadable dist-info")

        monkeypatch.setattr(importlib.metadata, "version", _boom)
        assert package_version() == "0.0.0+unknown"


class TestEmptyOverride:
    """A bare `ORCH_VERSION=` in the .env cannot erase the version.

    The poll already ignores the empty value, but `register` runs only once per
    identity: through that path the dashboard column stayed blank
    forever. The worker does know its version, so the typo falls back to the default.
    """

    def test_empty_orch_version_falls_back_to_package(self, monkeypatch, tmp_path):
        monkeypatch.setenv(_ENV_VERSION, "")
        monkeypatch.chdir(tmp_path)
        assert Settings().version == pkg_version("orcaster-worker")

    def test_blank_orch_version_falls_back_to_package(self, monkeypatch, tmp_path):
        monkeypatch.setenv(_ENV_VERSION, "   ")
        monkeypatch.chdir(tmp_path)
        assert Settings().version == pkg_version("orcaster-worker")

    def test_orch_version_is_stripped(self, monkeypatch, tmp_path):
        monkeypatch.setenv(_ENV_VERSION, "  9.9.9-test  ")
        monkeypatch.chdir(tmp_path)
        assert Settings().version == "9.9.9-test"


class _FakeDist:
    """Distribution whose `locate_file` points to a test tree."""

    def __init__(self, root):
        self._root = root

    def locate_file(self, name):
        return self._root / name


def _write_pkg(root, content):
    pkg = root / "orcaster_worker"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(content, encoding="utf-8")
    (pkg / "main.py").write_text("x = 1\n", encoding="utf-8")
    return pkg


class TestPatchedMarker:
    """The version cannot claim something false about a patched worker.

    In production the worker runs with the host checkout bind-mounted over
    `PYTHONPATH`, while the metadata comes from the dist-info baked into the image.
    `make restart client` applies edits without rebuilding, so the two copies
    can diverge.
    """

    def _stub(self, monkeypatch, loaded, installed_root):
        import orcaster_worker

        monkeypatch.setattr(orcaster_worker, "__file__", str(loaded / "__init__.py"))
        monkeypatch.setattr(importlib.metadata, "version", lambda _n: "0.16.0")
        monkeypatch.setattr(
            importlib.metadata, "distribution", lambda _n: _FakeDist(installed_root)
        )

    def test_identical_copies_carry_no_marker(self, monkeypatch, tmp_path):
        loaded = _write_pkg(tmp_path / "src", "v = 1\n")
        _write_pkg(tmp_path / "site", "v = 1\n")
        self._stub(monkeypatch, loaded, tmp_path / "site")
        assert package_version() == "0.16.0"

    def test_edited_source_carries_marker(self, monkeypatch, tmp_path):
        loaded = _write_pkg(tmp_path / "src", "v = 2  # hot-patch\n")
        _write_pkg(tmp_path / "site", "v = 1\n")
        self._stub(monkeypatch, loaded, tmp_path / "site")
        assert package_version() == "0.16.0+patched"

    def test_extra_file_carries_marker(self, monkeypatch, tmp_path):
        loaded = _write_pkg(tmp_path / "src", "v = 1\n")
        (loaded / "extra.py").write_text("y = 2\n", encoding="utf-8")
        _write_pkg(tmp_path / "site", "v = 1\n")
        self._stub(monkeypatch, loaded, tmp_path / "site")
        assert package_version() == "0.16.0+patched"

    def test_version_with_existing_local_segment_is_extended(self, monkeypatch, tmp_path):
        loaded = _write_pkg(tmp_path / "src", "v = 2\n")
        _write_pkg(tmp_path / "site", "v = 1\n")
        self._stub(monkeypatch, loaded, tmp_path / "site")
        monkeypatch.setattr(importlib.metadata, "version", lambda _n: "0.16.0+abc")
        assert package_version() == "0.16.0+abc.patched"

    def test_editable_install_carries_no_marker(self, monkeypatch, tmp_path):
        """`locate_file` points to a nonexistent site-packages: nothing to compare."""
        loaded = _write_pkg(tmp_path / "src", "v = 1\n")
        self._stub(monkeypatch, loaded, tmp_path / "does-not-exist")
        assert package_version() == "0.16.0"

    def test_unreadable_metadata_carries_no_marker(self, monkeypatch, tmp_path):
        """When in doubt, no marker: a false 'patched' is worse than not warning."""
        loaded = _write_pkg(tmp_path / "src", "v = 1\n")
        import orcaster_worker

        monkeypatch.setattr(orcaster_worker, "__file__", str(loaded / "__init__.py"))
        monkeypatch.setattr(importlib.metadata, "version", lambda _n: "0.16.0")

        def _boom(_n):
            raise OSError("unreadable dist-info")

        monkeypatch.setattr(importlib.metadata, "distribution", _boom)
        assert package_version() == "0.16.0"


class TestDistName:
    def test_only_orcaster_worker_is_consulted(self, monkeypatch):
        """Without `orcaster-worker` metadata the version is unknown; no other name is tried."""
        seen = []

        def _fake(name):
            seen.append(name)
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", _fake)
        monkeypatch.setattr("orcaster_worker._source_matches_installed", lambda: True)
        assert package_version() == "0.0.0+unknown"
        assert seen == ["orcaster-worker"]

    def test_new_name_has_priority(self, monkeypatch):
        seen = []

        def _fake(name):
            seen.append(name)
            return "1.2.3"

        monkeypatch.setattr(importlib.metadata, "version", _fake)
        monkeypatch.setattr("orcaster_worker._source_matches_installed", lambda: True)
        assert package_version() == "1.2.3"
        assert seen == ["orcaster-worker"]
