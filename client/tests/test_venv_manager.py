"""venv_manager tests: creation of an ephemeral venv per execution with uv.

Tests use the real `uv` installed in PATH; if it is not
available, all tests are skipped.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

UV_BIN = shutil.which("uv")
pytestmark = pytest.mark.skipif(
    UV_BIN is None,
    reason="uv binary not found in PATH; install uv to run these tests",
)


@pytest.fixture
def uv_cache_dir(tmp_path: Path) -> Path:
    p = tmp_path / "uv-cache"
    p.mkdir()
    return p


async def test_creates_and_cleans_venv(tmp_path: Path, uv_cache_dir: Path) -> None:
    from orcaster_worker.venv_manager import ephemeral_venv

    captured_path: Path | None = None
    async with ephemeral_venv(
        execution_id="exec-1",
        requirements=[],
        uv_bin=UV_BIN,  # type: ignore[arg-type]
        uv_cache_dir=uv_cache_dir,
    ) as venv:
        assert venv.path.exists() and venv.path.is_dir()
        assert venv.python.exists()
        assert "VIRTUAL_ENV" in venv.env
        assert venv.env["VIRTUAL_ENV"] == str(venv.path)
        assert "PATH" in venv.env
        bin_dir = venv.python.parent
        assert str(bin_dir) in venv.env["PATH"]
        captured_path = venv.path

    assert captured_path is not None
    assert not captured_path.exists()


async def test_installs_requirements(tmp_path: Path, uv_cache_dir: Path) -> None:
    from orcaster_worker.venv_manager import ephemeral_venv

    async with ephemeral_venv(
        execution_id="exec-2",
        requirements=["six"],
        uv_bin=UV_BIN,  # type: ignore[arg-type]
        uv_cache_dir=uv_cache_dir,
    ) as venv:
        proc = await asyncio.create_subprocess_exec(
            str(venv.python),
            "-c",
            "import six; print(six.__version__)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        assert out.decode().strip()


async def test_cleanup_on_exception(tmp_path: Path, uv_cache_dir: Path) -> None:
    from orcaster_worker.venv_manager import ephemeral_venv

    captured: Path | None = None
    with pytest.raises(RuntimeError, match="boom"):
        async with ephemeral_venv(
            execution_id="exec-3",
            requirements=[],
            uv_bin=UV_BIN,  # type: ignore[arg-type]
            uv_cache_dir=uv_cache_dir,
        ) as venv:
            captured = venv.path
            assert captured.exists()
            raise RuntimeError("boom")

    assert captured is not None
    assert not captured.exists()


async def test_empty_requirements_still_creates_venv(tmp_path: Path, uv_cache_dir: Path) -> None:
    from orcaster_worker.venv_manager import ephemeral_venv

    async with ephemeral_venv(
        execution_id="exec-4",
        requirements=[],
        uv_bin=UV_BIN,  # type: ignore[arg-type]
        uv_cache_dir=uv_cache_dir,
    ) as venv:
        assert venv.python.exists()
        proc = await asyncio.create_subprocess_exec(
            str(venv.python),
            "-c",
            "import sys; print(sys.prefix)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        assert proc.returncode == 0
        assert str(venv.path) in out.decode()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="kill semantics differ on Windows; POSIX-only test",
)
async def test_abandon_during_install_kills_uv_process(tmp_path: Path, uv_cache_dir: Path) -> None:
    from orcaster_worker.venv_manager import VenvAbandonedError, ephemeral_venv

    abandon_event = asyncio.Event()

    async def _trigger_abandon() -> None:
        await asyncio.sleep(0.05)
        abandon_event.set()

    asyncio.create_task(_trigger_abandon())

    with pytest.raises((VenvAbandonedError, asyncio.CancelledError)):
        async with ephemeral_venv(
            execution_id="exec-5",
            requirements=["requests"],
            uv_bin=UV_BIN,  # type: ignore[arg-type]
            uv_cache_dir=uv_cache_dir,
            abandon_event=abandon_event,
        ) as venv:
            assert venv.python.exists()
