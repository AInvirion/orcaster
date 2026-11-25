#!/usr/bin/env bash
# Build and smoke-test the orcaster-server wheel outside the checkout.
#
#   server/scripts-dev/wheel-smoke.sh
#
# Steps:
#   1. `uv build` in server/ (wheel + sdist into a temporary directory).
#   2. The wheel installs no top-level `app`, `alembic` or `scripts`.
#   3. Fresh Python 3.13 venv in that directory; install the wheel there. Every
#      check runs from an empty temporary directory with PYTHONPATH unset, so
#      the checkout is never on sys.path or the working directory.
#   4. Throwaway Postgres 18 in rootless Podman (host port 5433); `orcaster-migrate`
#      upgrades it to head.
#   5. `orcaster-server` boots; `/auth/login` and a packaged static file answer 200.
# Requires: uv, podman (rootless), curl, python3 (>=3.11). Exit status 0 = every check passed.
set -euo pipefail

SERVER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/wheel-smoke.XXXXXX")"
PG_NAME="orcaster-wheel-smoke-$$"
PG_PORT=5433
APP_PORT="${WHEEL_SMOKE_PORT:-8765}"
SERVER_PID=""

cleanup() {
  [ -z "$SERVER_PID" ] || kill "$SERVER_PID" 2>/dev/null || true
  podman stop -t 2 "$PG_NAME" >/dev/null 2>&1 || true
  podman volume prune -f >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { echo "wheel-smoke: FAIL: $*" >&2; exit 1; }

EXPECTED_VERSION="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$SERVER_DIR/pyproject.toml")"

step "uv build (server/)"
(cd "$SERVER_DIR" && uv build --out-dir "$WORK/dist")
WHEEL="$(ls "$WORK"/dist/*.whl)"
SDIST="$(ls "$WORK"/dist/*.tar.gz)"
echo "wheel: $(basename "$WHEEL")"
echo "sdist: $(basename "$SDIST")"

step "top-level entries of the wheel"
python3 -I - "$WHEEL" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
tops = sorted({n.split("/", 1)[0] for n in names})
print("\n".join(tops))
bad = [t for t in tops if t.removesuffix(".py") in {"app", "alembic", "scripts"}]
sys.exit(f"forbidden top-level entries: {bad}" if bad else 0)
PY

step "fresh Python 3.13 venv, install the wheel"
uv venv --quiet --python 3.13 "$WORK/venv"
uv pip install --quiet --python "$WORK/venv/bin/python" "$WHEEL"
BIN="$WORK/venv/bin"
# Not checked in site-packages: the `alembic` dependency legitimately installs its
# own top-level `alembic`. The wheel contents check above is what guards ours.

# From here on: run inside an empty directory, never from the checkout.
mkdir "$WORK/run"
cd "$WORK/run"
unset PYTHONPATH

step "import orcaster_server"
"$BIN/python" -I -c '
import sys, orcaster_server
path = orcaster_server.__file__
print("orcaster_server", orcaster_server.__version__, "from", path)
sys.exit(0 if path.startswith(sys.prefix) else "imported from outside the venv")
'

step "throwaway Postgres 18 (rootless Podman, port $PG_PORT)"
podman run --rm -d --name "$PG_NAME" -e POSTGRES_PASSWORD=smoke \
  -p "$PG_PORT:5432" docker.io/library/postgres:18 >/dev/null
ready=0
for _ in $(seq 1 60); do
  if podman exec "$PG_NAME" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[ "$ready" = 1 ] || fail "postgres did not become ready"
# pg_isready answers during initdb's temporary server; wait for the final one.
sleep 2
export DATABASE_URL="postgresql+asyncpg://postgres:smoke@127.0.0.1:$PG_PORT/postgres"

step "orcaster-migrate (to head)"
"$BIN/orcaster-migrate"

step "orcaster-server: /auth/login and a static file"
# The server refuses to boot without a signing key.
"$BIN/python" -I -c '
import sys
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
open(sys.argv[1], "wb").write(Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
' "$WORK/ed25519.pem"
export ED25519_PRIVATE_KEY_PATH="$WORK/ed25519.pem"
export HOST=127.0.0.1 PORT="$APP_PORT" MIGRATE_ON_STARTUP=false
export ARTIFACTS_ROOT="$WORK/artifacts" HOUSEKEEPING_ENABLED=false
"$BIN/orcaster-server" >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
check_url() {
  local path="$1" code="" _
  for _ in $(seq 1 60); do
    kill -0 "$SERVER_PID" 2>/dev/null || { cat "$WORK/server.log" >&2; fail "orcaster-server exited"; }
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$APP_PORT$path" || true)"
    [ "$code" = 200 ] && { echo "GET $path -> 200"; return 0; }
    sleep 1
  done
  cat "$WORK/server.log" >&2
  fail "GET $path -> $code, expected 200"
}
check_url /auth/login
check_url /static/app.css
kill "$SERVER_PID"; wait "$SERVER_PID" 2>/dev/null || true; SERVER_PID=""

printf '\nwheel-smoke: OK (orcaster-server %s)\n' "$EXPECTED_VERSION"
