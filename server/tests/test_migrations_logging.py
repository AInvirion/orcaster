"""In-process migrations must not disable the server loggers.

`orcaster_server.main._apply_migrations` runs `alembic upgrade head` inside the
uvicorn process at startup. If `env.py` calls `logging.config.fileConfig` there,
the default `disable_existing_loggers=True` disables `orcaster_server.main` (the
`lifespan:` lines) and `uvicorn.error` (`Application startup complete`), and
also replaces the root handlers. From the CLI, on the other hand, `alembic.ini`
must keep configuring logging.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def isolated_loggers() -> Iterator[None]:
    """Save and restore the logging state these tests touch.

    Levels of root, `orcaster_server` and `alembic`, and handlers/propagate/disabled
    of `orcaster_server` and `alembic`. Levels are restored with `setLevel` (not by
    assigning the attribute) because it clears the children's effective-level
    cache. The root handlers are not restored here: pytest adds and removes its
    own in each phase, so the tests replace them with `_root_handlers` inside
    the call phase.
    """
    root = logging.getLogger()
    named = [logging.getLogger(name) for name in ("orcaster_server", "alembic")]
    saved_levels = {lg: lg.level for lg in (root, *named)}
    saved_state = {lg: (lg.handlers[:], lg.propagate, lg.disabled) for lg in named}
    try:
        yield
    finally:
        for lg, (handlers, propagate, disabled) in saved_state.items():
            lg.handlers[:] = handlers
            lg.propagate = propagate
            lg.disabled = disabled
        for lg, level in saved_levels.items():
            lg.setLevel(level)


@contextlib.contextmanager
def _root_handlers(*handlers: logging.Handler) -> Iterator[None]:
    """Replace the root handlers during the block and restore them afterwards.

    Without this, the capture handler that pytest attaches to the root would make
    `_configure_app_logging` always see an operator config. The list is mutated
    in place and restored before the call phase ends, so that pytest removes its
    own handlers afterwards.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers[:] = list(handlers)
    try:
        yield
    finally:
        root.handlers[:] = saved


async def test_app_loggers_still_emit_after_in_process_migrations(
    migrated_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orcaster_server import main as main_module

    monkeypatch.setattr(main_module.settings, "database_url", migrated_db_url)
    # `_apply_migrations` writes DATABASE_URL into the environment; monkeypatch
    # restores it when done so it does not leak into other tests.
    monkeypatch.setenv("DATABASE_URL", migrated_db_url)

    # Loggers created BEFORE the upgrade, as in the real lifespan.
    app_log = logging.getLogger("orcaster_server.main")
    uvicorn_log = logging.getLogger("uvicorn.error")
    root_handlers = list(logging.getLogger().handlers)

    # Same path as the lifespan: in a separate thread.
    await asyncio.to_thread(main_module._apply_migrations)

    assert not app_log.disabled, "alembic env.py disabled the orcaster_server.main logger"
    assert not uvicorn_log.disabled, "alembic env.py disabled the uvicorn.error logger"
    assert logging.getLogger().handlers == root_handlers, (
        "alembic env.py replaced the root logger handlers"
    )

    with caplog.at_level(logging.INFO, logger="orcaster_server.main"):
        app_log.info("lifespan: probe after migrations")
    assert "lifespan: probe after migrations" in caplog.messages


def test_alembic_cli_still_configures_logging_from_ini(migrated_db_url: str) -> None:
    """From the CLI `fileConfig` stays active: alembic logs its INFO to stderr."""
    env = os.environ.copy()
    env["DATABASE_URL"] = migrated_db_url
    proc = subprocess.run(
        ["uv", "run", "alembic", "current"],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    # `generic` format from alembic.ini: `%(levelname)-5.5s [%(name)s] %(message)s`.
    assert "INFO  [alembic.runtime.migration]" in proc.stderr, proc.stderr


@pytest.mark.usefixtures("isolated_loggers")
def test_configure_app_logging_emits_info_to_stderr_when_unconfigured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With uvicorn's default config (root without handler, at WARNING) the INFO
    `lifespan:` lines and alembic's reach stderr, which systemd sends to the
    journal."""
    from orcaster_server import main as main_module

    root = logging.getLogger()
    app_log = logging.getLogger("orcaster_server")
    alembic_log = logging.getLogger("alembic")
    # State of a uvicorn process without --log-config: nobody configured root,
    # `orcaster_server` or `alembic`. alembic attaches a `NullHandler` to its
    # logger: it does not count as output.
    app_log.handlers[:] = []
    alembic_log.handlers[:] = [logging.NullHandler()]
    root.setLevel(logging.WARNING)
    app_log.setLevel(logging.NOTSET)
    alembic_log.setLevel(logging.NOTSET)
    with _root_handlers():
        main_module._configure_app_logging()
        main_module._configure_app_logging()  # idempotent: does not duplicate the handler

        assert len(app_log.handlers) == 1
        assert len(alembic_log.handlers) == 2
        assert app_log.level == logging.INFO
        assert alembic_log.level == logging.INFO
        logging.getLogger("orcaster_server.main").info("lifespan: task secrets keys loaded; probe")
        logging.getLogger("alembic.runtime.migration").info("Running upgrade probe")
    err = capsys.readouterr().err
    assert err.count("INFO [orcaster_server.main] lifespan: task secrets keys loaded; probe") == 1
    assert err.count("INFO [alembic.runtime.migration] Running upgrade probe") == 1


@pytest.mark.usefixtures("isolated_loggers")
def test_configure_app_logging_respects_operator_level_on_app() -> None:
    """If the operator already configured logging (handler on root, level on
    `orcaster_server`), no handler is added and the level is not overridden."""
    from orcaster_server import main as main_module

    app_log = logging.getLogger("orcaster_server")
    operator_handler = logging.StreamHandler(io.StringIO())
    app_log.handlers[:] = []
    app_log.setLevel(logging.WARNING)
    with _root_handlers(operator_handler):
        main_module._configure_app_logging()

        assert app_log.handlers == []
        assert app_log.level == logging.WARNING
        assert logging.getLogger().handlers == [operator_handler]


@pytest.mark.usefixtures("isolated_loggers")
def test_configure_app_logging_keeps_root_warning_from_operator_config() -> None:
    """`--log-config` that only configures root (handler, WARNING): `orcaster_server`
    and `alembic` inherit WARNING; the helper neither sets INFO nor adds a handler."""
    from orcaster_server import main as main_module

    root = logging.getLogger()
    app_log = logging.getLogger("orcaster_server")
    alembic_log = logging.getLogger("alembic")
    alembic_null = logging.NullHandler()
    app_log.handlers[:] = []
    alembic_log.handlers[:] = [alembic_null]
    app_log.setLevel(logging.NOTSET)
    alembic_log.setLevel(logging.NOTSET)
    root.setLevel(logging.WARNING)
    stream = io.StringIO()
    operator_handler = logging.StreamHandler(stream)
    operator_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    with _root_handlers(operator_handler):
        main_module._configure_app_logging()

        assert app_log.level == logging.NOTSET
        assert alembic_log.level == logging.NOTSET
        assert app_log.handlers == []
        assert alembic_log.handlers == [alembic_null]
        logging.getLogger("orcaster_server.main").info("info probe must not leak")
        logging.getLogger("alembic.runtime.migration").info("alembic info must not leak")
        logging.getLogger("orcaster_server.main").warning("warning probe")
    out = stream.getvalue()
    assert "info probe must not leak" not in out
    assert "alembic info must not leak" not in out
    assert "WARNING orcaster_server.main warning probe" in out


@pytest.mark.usefixtures("isolated_loggers")
def test_configure_app_logging_keeps_root_debug_from_operator_config() -> None:
    """Root at DEBUG with a handler: the DEBUG of `orcaster_server.*` still reaches the
    operator's handler (the helper does not cut it down to INFO)."""
    from orcaster_server import main as main_module

    root = logging.getLogger()
    app_log = logging.getLogger("orcaster_server")
    app_log.handlers[:] = []
    app_log.setLevel(logging.NOTSET)
    root.setLevel(logging.DEBUG)
    stream = io.StringIO()
    operator_handler = logging.StreamHandler(stream)
    operator_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    with _root_handlers(operator_handler):
        main_module._configure_app_logging()

        assert app_log.level == logging.NOTSET
        logging.getLogger("orcaster_server.main").debug("debug probe")
    assert "DEBUG orcaster_server.main debug probe" in stream.getvalue()


class _ConfigureCalled(Exception):
    pass


async def test_lifespan_configures_app_logging_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan calls `_configure_app_logging` before any other step.

    The recorder raises its own exception: if the lifespan stopped calling it,
    startup would proceed normally and fail for another reason or not fail.
    """
    from fastapi import FastAPI

    from orcaster_server import main as main_module

    def _recorder() -> None:
        raise _ConfigureCalled

    monkeypatch.setattr(main_module, "_configure_app_logging", _recorder)
    # No migrations or housekeeping: if the call were missing, the lifespan
    # must not touch real infrastructure before failing.
    monkeypatch.setattr(main_module.settings, "migrate_on_startup", False)
    monkeypatch.setattr(main_module.settings, "housekeeping_enabled", False)
    with pytest.raises(_ConfigureCalled):
        async with main_module.lifespan(FastAPI()):
            pass  # pragma: no cover - __aenter__ must have failed.
