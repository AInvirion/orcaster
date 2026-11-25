"""`orcaster-server`: starts uvicorn on `orcaster_server.main:app`.

Configuration through the environment (no flags, so that a systemd unit can
declare it with `Environment=`):

- `HOST`: listen interface (default `127.0.0.1`).
- `PORT`: TCP port (default `8000`).
- `UVICORN_WORKERS`: worker processes (default `1`).
"""

from __future__ import annotations

import os

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_WORKERS = 1


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise SystemExit(f"{name} must be >= 1, got {value}")
    return value


def serve_options() -> dict[str, object]:
    """`uvicorn.run` options derived from the environment."""
    return {
        "host": os.environ.get("HOST", "").strip() or DEFAULT_HOST,
        "port": _int_env("PORT", DEFAULT_PORT),
        "workers": _int_env("UVICORN_WORKERS", DEFAULT_WORKERS),
    }


def main() -> None:
    """Console entry point."""
    import uvicorn

    uvicorn.run("orcaster_server.main:app", **serve_options())


if __name__ == "__main__":
    main()
