"""Path-load contract of `task_variant.py`, without the `orcaster_server` package.

Operations tools (`ops/`) load `server/orcaster_server/task_variant.py` with
`spec_from_file_location` and without `orcaster_server` on the path. If the module
imports `orcaster_server.*` again at module level, those suites only fail later in
the ops CI; this test catches it in the server suite. Without `orcaster_server` there
is no plugin registry: the vocabulary is empty and no payload is recognised (the core
ships no extractors). `task_outcomes.py` depends on `orcaster_server.models` and no ops
tool loads it by path; if one starts to, this contract must be extended to that module.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SERVER_APP = Path(__file__).resolve().parents[1] / "orcaster_server"

_LOADER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("_standalone", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
assert "orcaster_server" not in sys.modules, "the module imported the orcaster_server package"
"""
_PRINT_KINDS = "print(sorted(m.KNOWN_VARIANT_KINDS))"
_PRINT_VARIANT = "import json; print(json.dumps(m.extract_variant(json.loads(sys.argv[2]))))"


def _run(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    # `-I`: no current directory and no PYTHONPATH, as in the ops CI job.
    return subprocess.run(
        [sys.executable, "-I", "-c", code, *args],
        capture_output=True,
        text=True,
        cwd="/",
        check=False,
    )


def test_task_variant_loads_by_path_without_app_package() -> None:
    proc = _run(_LOADER + _PRINT_KINDS, str(SERVER_APP / "task_variant.py"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_standalone_helpers_work_without_app_package() -> None:
    code = _LOADER + (
        "assert m.clamp_variant('k', 'x' * 100) == ('k', 'x' * 62)\n"
        "assert m.variant_sort_key('1.10.0') > m.variant_sort_key('1.9.0')\n"
        "print(m.VARIANT_SEP)"
    )
    proc = _run(code, str(SERVER_APP / "task_variant.py"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "~"


def test_standalone_extract_matches_server_registry_without_plugins() -> None:
    import json

    from orcaster_server import task_variant

    payload = '{"commands": ["uv pip install example-tool==1.2.3"]}'
    proc = _run(_LOADER + _PRINT_VARIANT, str(SERVER_APP / "task_variant.py"), payload)
    assert proc.returncode == 0, proc.stderr
    import_free = tuple(json.loads(proc.stdout))
    assert import_free == task_variant.extract_variant(json.loads(payload))
