#!/usr/bin/env bash
# Build and smoke-test the orcaster-worker wheel outside the checkout.
#
#   client/scripts/wheel-smoke.sh
#
# Steps:
#   1. `uv build` in client/ (wheel + sdist into a temporary directory).
#   2. Fresh Python 3.13 venv in that directory; install the wheel there. Every
#      check runs from the temporary directory with PYTHONPATH unset, so the
#      checkout is never on sys.path.
#   3. `import orcaster_worker` resolves inside the venv; `orcaster-worker
#      --help` works; `orcaster-worker --version` prints the pyproject version.
#   4. The wheel installs no top-level `app`, `alembic` or `scripts`.
# Requires: uv, python3 (>=3.11, for tomllib). Exit status 0 = every check passed.
set -euo pipefail

CLIENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/wheel-smoke.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { echo "wheel-smoke: FAIL: $*" >&2; exit 1; }

EXPECTED_VERSION="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$CLIENT_DIR/pyproject.toml")"

step "uv build (client/)"
(cd "$CLIENT_DIR" && uv build --out-dir "$WORK/dist")
WHEEL="$(ls "$WORK"/dist/*.whl)"
SDIST="$(ls "$WORK"/dist/*.tar.gz)"
echo "wheel: $(basename "$WHEEL")"
echo "sdist: $(basename "$SDIST")"

step "fresh Python 3.13 venv, install the wheel"
uv venv --quiet --python 3.13 "$WORK/venv"
uv pip install --quiet --python "$WORK/venv/bin/python" "$WHEEL"
BIN="$WORK/venv/bin"

# From here on: run inside $WORK, never from the checkout.
cd "$WORK"
unset PYTHONPATH

step "import orcaster_worker"
"$BIN/python" -I -c '
import sys, orcaster_worker
path = orcaster_worker.__file__
print("orcaster_worker", orcaster_worker.__version__, "from", path)
sys.exit(0 if path.startswith(sys.prefix) else "imported from outside the venv")
'

step "orcaster-worker --help"
"$BIN/orcaster-worker" --help

step "orcaster-worker --version"
VERSION_OUT="$("$BIN/orcaster-worker" --version)"
echo "$VERSION_OUT"
[ "$VERSION_OUT" = "orcaster-worker $EXPECTED_VERSION" ] \
  || fail "--version printed '$VERSION_OUT', expected 'orcaster-worker $EXPECTED_VERSION'"

step "top-level entries of the wheel"
"$BIN/python" -I - "$WHEEL" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
tops = sorted({n.split("/", 1)[0] for n in names})
print("\n".join(tops))
bad = [t for t in tops if t.removesuffix(".py") in {"app", "alembic", "scripts"}]
sys.exit(f"forbidden top-level entries: {bad}" if bad else 0)
PY
SITE="$("$BIN/python" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for name in app alembic scripts; do
  [ ! -e "$SITE/$name" ] && [ ! -e "$SITE/$name.py" ] || fail "site-packages has a top-level '$name'"
done
echo "no top-level app, alembic or scripts in site-packages"

printf '\nwheel-smoke: OK (orcaster-worker %s)\n' "$EXPECTED_VERSION"
