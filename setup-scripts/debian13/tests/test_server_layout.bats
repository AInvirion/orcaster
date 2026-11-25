#!/usr/bin/env bats
# Tests for setup-scripts/debian13/server-layout.sh (align-unit, restore-unit,
# migrate-static) and its wiring in 03-app-install.sh and lib.sh.
# link-check: ignore-file
# Run:  bats setup-scripts/debian13/tests/
#
# NOTE (bats-core 1.13 gotcha): @test names are pure ASCII.

load source_guard_stub
bats_require_minimum_version 1.5.0

LAYOUT="${BATS_TEST_DIRNAME}/../server-layout.sh"

setup() {
    INST="$BATS_TEST_TMPDIR/opt/orcaster"
    UNIT="$BATS_TEST_TMPDIR/etc/orcaster.service"
    BACKUPS="$BATS_TEST_TMPDIR/backups"
    RELOADS="$BATS_TEST_TMPDIR/reloads"
    mkdir -p "$INST/server" "$(dirname "$UNIT")" "$UNIT.d"
    # Fake systemctl: records each call.
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    cat > "$BATS_TEST_TMPDIR/bin/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$RELOADS"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/systemctl"
    export SYSTEMCTL="$BATS_TEST_TMPDIR/bin/systemctl"
    ME="$(id -un)"
}

# Installed tree with package $1 (orcaster_server | app).
make_tree() {
    mkdir -p "$INST/server/$1/static"
    echo "app = None" > "$INST/server/$1/main.py"
    echo "body{}" > "$INST/server/$1/static/app.css"
}

# Unit with the ExecStart of 05-systemd.sh for module $1.
make_unit() {
    cat > "$UNIT" <<EOF
[Unit]
Description=Orcaster server

[Service]
WorkingDirectory=$INST/server
ExecStart=/usr/local/bin/uv run uvicorn $1.main:app \\
    --host 127.0.0.1 --port 8000 \\
    --workers 2 --no-access-log
ReadWritePaths=$INST /var/lib/orcaster-artifacts

[Install]
WantedBy=multi-user.target
EOF
    printf '[Service]\nReadWritePaths=/var/lib/orcaster-artifacts\n' > "$UNIT.d/artifacts.conf"
}

layout() {
    run bash "$LAYOUT" "$@" --install-dir "$INST" --app-user "$ME" \
        --unit "$UNIT" --backup-dir "$BACKUPS"
}

# --- align-unit ---------------------------------------------------------------

@test "align-unit: app.main -> orcaster_server.main, with backup and daemon-reload" {
    make_tree orcaster_server
    make_unit app
    cp "$UNIT" "$BATS_TEST_TMPDIR/before"
    cp "$UNIT.d/artifacts.conf" "$BATS_TEST_TMPDIR/dropin-before"
    layout align-unit
    [ "$status" -eq 0 ]
    grep -q 'uvicorn orcaster_server.main:app' "$UNIT"
    run grep -c 'app.main:app' "$UNIT"
    [ "$output" = 0 ]
    # Only that line changes.
    run diff "$BATS_TEST_TMPDIR/before" "$UNIT"
    [ "$(grep -c '^[<>]' <<<"$output")" -eq 2 ]
    # Backup identical to the original unit.
    [ "$(find "$BACKUPS" -type f | wc -l)" -eq 1 ]
    cmp "$BATS_TEST_TMPDIR/before" "$(find "$BACKUPS" -type f)"
    # The drop-in is not touched.
    cmp "$BATS_TEST_TMPDIR/dropin-before" "$UNIT.d/artifacts.conf"
    [ "$(cat "$RELOADS")" = "daemon-reload" ]
}

@test "align-unit: idempotent, no backup or reload when it already matches" {
    make_tree orcaster_server
    make_unit orcaster_server
    cp "$UNIT" "$BATS_TEST_TMPDIR/before"
    layout align-unit
    [ "$status" -eq 0 ]
    cmp "$BATS_TEST_TMPDIR/before" "$UNIT"
    [ ! -e "$BACKUPS" ]
    [ ! -e "$RELOADS" ]
}

@test "align-unit: rollback to a tree with app returns the ExecStart to app.main" {
    make_tree app
    make_unit orcaster_server
    layout align-unit
    [ "$status" -eq 0 ]
    grep -q 'uvicorn app.main:app' "$UNIT"
    run grep -c 'orcaster_server' "$UNIT"
    [ "$output" = 0 ]
}

@test "align-unit: preserves the unit mode" {
    make_tree orcaster_server
    make_unit app
    chmod 0640 "$UNIT"
    layout align-unit
    [ "$status" -eq 0 ]
    [ "$(stat -c %a "$UNIT" 2>/dev/null || stat -f %Lp "$UNIT")" = 640 ]
}

@test "align-unit: unknown ExecStart fails without touching the unit" {
    make_tree orcaster_server
    make_unit app
    sed -i.bak 's/uvicorn app.main:app/gunicorn other:wsgi/' "$UNIT"
    rm -f "$UNIT.bak"
    cp "$UNIT" "$BATS_TEST_TMPDIR/before"
    layout align-unit
    [ "$status" -ne 0 ]
    [[ "$output" == *"I am not touching it"* ]]
    cmp "$BATS_TEST_TMPDIR/before" "$UNIT"
    [ ! -e "$RELOADS" ]
}

@test "align-unit: without a unit (first install) does nothing" {
    make_tree orcaster_server
    layout align-unit
    [ "$status" -eq 0 ]
    [ ! -e "$UNIT" ]
    [ ! -e "$RELOADS" ]
}

@test "align-unit: without an installed server package fails" {
    make_unit app
    layout align-unit
    [ "$status" -ne 0 ]
    [[ "$output" == *"Cannot find the server package"* ]]
}

@test "align-unit: --dry-run writes nothing" {
    make_tree orcaster_server
    make_unit app
    cp "$UNIT" "$BATS_TEST_TMPDIR/before"
    layout align-unit --dry-run
    [ "$status" -eq 0 ]
    cmp "$BATS_TEST_TMPDIR/before" "$UNIT"
    [ ! -e "$BACKUPS" ]
    [ ! -e "$RELOADS" ]
}

@test "restore-unit: restores the align-unit backup and runs daemon-reload" {
    make_tree orcaster_server
    make_unit app
    cp "$UNIT" "$BATS_TEST_TMPDIR/before"
    layout align-unit
    [ "$status" -eq 0 ]
    rm -f "$RELOADS"
    layout restore-unit "$(find "$BACKUPS" -type f)"
    [ "$status" -eq 0 ]
    cmp "$BATS_TEST_TMPDIR/before" "$UNIT"
    [ "$(cat "$RELOADS")" = "daemon-reload" ]
}

@test "restore-unit: missing backup fails" {
    make_unit app
    layout restore-unit "$BATS_TEST_TMPDIR/nope"
    [ "$status" -ne 0 ]
}

# --- migrate-static -----------------------------------------------------------

# State after the rsync --delete of 03 on a host with the old package: in
# server/app/ only what was excluded (host files) and the __pycache__ remain.
make_leftovers() {
    mkdir -p "$INST/server/app/static/__pycache__" "$INST/server/app/api/__pycache__"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
    echo "wheel" > "$INST/server/app/static/tool-1.0-py3-none-any.whl"
    echo "pyc" > "$INST/server/app/api/__pycache__/x.cpython-313.pyc"
}

@test "migrate-static: moves the host files and deletes the old tree" {
    make_tree orcaster_server
    make_leftovers
    layout migrate-static
    [ "$status" -eq 0 ]
    [ "$(cat "$INST/server/orcaster_server/static/robots.txt")" = "User-agent: *" ]
    [ "$(cat "$INST/server/orcaster_server/static/tool-1.0-py3-none-any.whl")" = "wheel" ]
    [ "$(cat "$INST/server/orcaster_server/static/app.css")" = "body{}" ]
    [ ! -e "$INST/server/app" ]
}

@test "migrate-static: idempotent without an old tree" {
    make_tree orcaster_server
    layout migrate-static
    [ "$status" -eq 0 ]
    [[ "$output" == *"nothing to migrate"* ]]
}

@test "migrate-static: identical copy at the destination is discarded" {
    make_tree orcaster_server
    make_leftovers
    echo "User-agent: *" > "$INST/server/orcaster_server/static/robots.txt"
    layout migrate-static
    [ "$status" -eq 0 ]
    [ ! -e "$INST/server/app" ]
}

@test "migrate-static: conflict overwrites neither file" {
    make_tree orcaster_server
    make_leftovers
    echo "otro" > "$INST/server/orcaster_server/static/robots.txt"
    layout migrate-static
    [ "$status" -ne 0 ]
    [[ "$output" == *"Conflict"* ]]
    [ "$(cat "$INST/server/orcaster_server/static/robots.txt")" = "otro" ]
    [ "$(cat "$INST/server/app/static/robots.txt")" = "User-agent: *" ]
    # What did not clash was moved.
    [ -f "$INST/server/orcaster_server/static/tool-1.0-py3-none-any.whl" ]
}

@test "migrate-static: old tree with Python code is not touched" {
    make_tree orcaster_server
    make_leftovers
    echo "x = 1" > "$INST/server/app/api/routes.py"
    layout migrate-static
    [ "$status" -ne 0 ]
    [[ "$output" == *"half synchronized"* ]]
    [ -f "$INST/server/app/static/robots.txt" ]
    [ ! -e "$INST/server/orcaster_server/static/robots.txt" ]
}

@test "migrate-static: neither follows nor moves symbolic links" {
    make_tree orcaster_server
    make_leftovers
    echo secret > "$BATS_TEST_TMPDIR/outside"
    ln -s "$BATS_TEST_TMPDIR/outside" "$INST/server/app/static/link.txt"
    layout migrate-static
    [ "$status" -ne 0 ]
    [[ "$output" == *"Symbolic link"* ]]
    [ -L "$INST/server/app/static/link.txt" ]
    [ ! -e "$INST/server/orcaster_server/static/link.txt" ]
    [ "$(cat "$BATS_TEST_TMPDIR/outside")" = secret ]
}

@test "migrate-static: --dry-run moves nothing" {
    make_tree orcaster_server
    make_leftovers
    layout migrate-static --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" == *"would move app/static/robots.txt"* ]]
    [ -f "$INST/server/app/static/robots.txt" ]
    [ ! -e "$INST/server/orcaster_server/static/robots.txt" ]
}

# --- lib.sh: HOST_ONLY_STATIC -------------------------------------------------

@test "host_only_static_excludes: excludes each pattern in both packages" {
    source "${BATS_TEST_DIRNAME}/../lib.sh"
    HOST_ONLY_STATIC="robots.txt *.whl"
    run host_only_static_excludes
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 4 ]
    [ "${lines[0]}" = "--exclude=/server/orcaster_server/static/robots.txt" ]
    [ "${lines[1]}" = "--exclude=/server/app/static/robots.txt" ]
    [ "${lines[2]}" = "--exclude=/server/orcaster_server/static/*.whl" ]
    [ "${lines[3]}" = "--exclude=/server/app/static/*.whl" ]
}

@test "host_only_static_excludes: empty excludes nothing" {
    source "${BATS_TEST_DIRNAME}/../lib.sh"
    HOST_ONLY_STATIC=""
    run host_only_static_excludes
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "validate_host_only_static: rejects paths, .. and options" {
    source "${BATS_TEST_DIRNAME}/../lib.sh"
    local bad
    for bad in "static/robots.txt" "../x" "--delete"; do
        HOST_ONLY_STATIC="robots.txt $bad"
        run validate_host_only_static
        [ "$status" -ne 0 ] || { echo "accepted: $bad"; false; }
    done
    HOST_ONLY_STATIC="robots.txt *.whl tool.sh.bak-*"
    run validate_host_only_static
    [ "$status" -eq 0 ]
}

# End to end with a real rsync: the rules of 03 plus HOST_ONLY_STATIC keep the
# host files in app/static even though the new tree no longer brings app/, and
# migrate-static leaves them in orcaster_server/static.
@test "rsync of 03 + migrate-static: host files survive the rename" {
    command -v rsync
    source "${BATS_TEST_DIRNAME}/../lib.sh"
    HOST_ONLY_STATIC="robots.txt *.whl"
    local args=() flag pat
    while read -r flag pat; do args+=("$flag" "$pat"); done < <(
        sed -n "/^RSYNC_EXCLUDES=(/,/^)/p" "$BATS_TEST_DIRNAME/../03-app-install.sh" \
            | sed -nE "s/.*(--include|--exclude|--filter) '([^']*)'.*/\1 \2/p")
    local x
    while IFS= read -r x; do args+=("$x"); done < <(host_only_static_excludes)

    local src="$BATS_TEST_TMPDIR/src"
    mkdir -p "$src/server/orcaster_server/static"
    echo "app = None" > "$src/server/orcaster_server/main.py"
    echo "body{}" > "$src/server/orcaster_server/static/app.css"
    # Destination: previous release with the app package and host files.
    mkdir -p "$INST/server/app/static" "$INST/server/app/api/__pycache__"
    echo "app = None" > "$INST/server/app/main.py"
    echo "old" > "$INST/server/app/static/app.css"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
    echo "wheel" > "$INST/server/app/static/tool-1.0-py3-none-any.whl"
    echo "pyc" > "$INST/server/app/api/__pycache__/x.pyc"

    run rsync -a --delete "${args[@]}" "$src"/ "$INST"/
    [ "$status" -eq 0 ]
    [ -f "$INST/server/app/static/robots.txt" ]
    [ ! -e "$INST/server/app/main.py" ]
    [ ! -e "$INST/server/app/static/app.css" ]

    layout migrate-static
    [ "$status" -eq 0 ]
    [ "$(cat "$INST/server/orcaster_server/static/robots.txt")" = "User-agent: *" ]
    [ -f "$INST/server/orcaster_server/static/tool-1.0-py3-none-any.whl" ]
    [ ! -e "$INST/server/app" ]

    # A second deployment does not delete them from the new package.
    run rsync -a --delete "${args[@]}" "$src"/ "$INST"/
    [ "$status" -eq 0 ]
    [ -f "$INST/server/orcaster_server/static/robots.txt" ]
    [ -f "$INST/server/orcaster_server/static/tool-1.0-py3-none-any.whl" ]
}

# --- migrate-static with the versioned shim server/app/__init__.py -----
# The repo versions server/app/__init__.py (alias app.* -> orcaster_server.*
# for old plugins). After the rsync --delete of 03 that file legitimately
# remains in $INSTALL_DIR/server/app/.

SHIM_REPO="${BATS_TEST_DIRNAME}/../../../server/app/__init__.py"

# Host with the old release: complete app package (several .py + static).
make_old_full_app() {
    mkdir -p "$INST/server/app/api" "$INST/server/app/static"
    echo "app = None" > "$INST/server/app/main.py"
    echo "x = 1" > "$INST/server/app/api/routes.py"
    echo "# app viejo completo" > "$INST/server/app/__init__.py"
    echo "old" > "$INST/server/app/static/app.css"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
}

# rsync arguments of 03 (RSYNC_EXCLUDES) + HOST_ONLY_STATIC="robots.txt".
# Leaves the result in the RSYNC_ARGS array.
load_rsync_args() {
    source "${BATS_TEST_DIRNAME}/../lib.sh"
    HOST_ONLY_STATIC="robots.txt"
    RSYNC_ARGS=()
    local flag pat x
    while read -r flag pat; do RSYNC_ARGS+=("$flag" "$pat"); done < <(
        sed -n "/^RSYNC_EXCLUDES=(/,/^)/p" "$BATS_TEST_DIRNAME/../03-app-install.sh" \
            | sed -nE "s/.*(--include|--exclude|--filter) '([^']*)'.*/\1 \2/p")
    while IFS= read -r x; do RSYNC_ARGS+=("$x"); done < <(host_only_static_excludes)
}

# Source tree: orcaster_server + the versioned shim.
make_src_with_shim() {
    SRC="$BATS_TEST_TMPDIR/src"
    mkdir -p "$SRC/server/orcaster_server/static" "$SRC/server/app"
    echo "app = None" > "$SRC/server/orcaster_server/main.py"
    echo "body{}" > "$SRC/server/orcaster_server/static/app.css"
    cp "$SHIM_REPO" "$SRC/server/app/__init__.py"
}

@test "migrate-static: identical shim is respected and the host static migrates" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app/static"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -eq 0 ]
    cmp "$SHIM_REPO" "$INST/server/app/__init__.py"
    [ "$(cat "$INST/server/orcaster_server/static/robots.txt")" = "User-agent: *" ]
    [ ! -e "$INST/server/app/static" ]
    [ ! -e "$INST/server/orcaster_server/__init__.py" ]
}

@test "migrate-static: shim with nothing else from the host is a no-op that succeeds" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -eq 0 ]
    cmp "$SHIM_REPO" "$INST/server/app/__init__.py"
}

@test "migrate-static: idempotent with the shim present" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app/static"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -eq 0 ]
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -eq 0 ]
    cmp "$SHIM_REPO" "$INST/server/app/__init__.py"
    [ "$(cat "$INST/server/orcaster_server/static/robots.txt")" = "User-agent: *" ]
}

@test "migrate-static: --dry-run with the shim neither counts nor touches it" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app/static"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
    layout migrate-static --shim-source "$SHIM_REPO" --dry-run
    [ "$status" -eq 0 ]
    [[ "$output" != *"app/__init__.py"* ]]
    [ -f "$INST/server/app/static/robots.txt" ]
    cmp "$SHIM_REPO" "$INST/server/app/__init__.py"
}

@test "migrate-static: app/__init__.py different from the shim still aborts" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app/static"
    echo "# app viejo completo" > "$INST/server/app/__init__.py"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -ne 0 ]
    [[ "$output" == *"half synchronized"* ]]
    [ "$(cat "$INST/server/app/__init__.py")" = "# app viejo completo" ]
    [ -f "$INST/server/app/static/robots.txt" ]
    [ ! -e "$INST/server/orcaster_server/static/robots.txt" ]
}

@test "migrate-static: shim modified by one byte aborts" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app"
    { cat "$SHIM_REPO"; echo "# extra"; } > "$INST/server/app/__init__.py"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -ne 0 ]
    [[ "$output" == *"half synchronized"* ]]
}

@test "migrate-static: without --shim-source an __init__.py aborts even if it is the shim" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    layout migrate-static
    [ "$status" -ne 0 ]
    [[ "$output" == *"half synchronized"* ]]
}

@test "migrate-static: nonexistent --shim-source tolerates no __init__.py" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    layout migrate-static --shim-source "$BATS_TEST_TMPDIR/does-not-exist.py"
    [ "$status" -ne 0 ]
    [[ "$output" == *"half synchronized"* ]]
}

@test "migrate-static: another .py next to the shim aborts" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app/static"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    echo "x = 1" > "$INST/server/app/extra.py"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -ne 0 ]
    [[ "$output" == *"half synchronized"* ]]
    [ -f "$INST/server/app/extra.py" ]
    [ ! -e "$INST/server/orcaster_server/static/robots.txt" ]
}

@test "migrate-static: a nested __init__.py equal to the shim is not tolerated" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app/api"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    cp "$SHIM_REPO" "$INST/server/app/api/__init__.py"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -ne 0 ]
    [[ "$output" == *"half synchronized"* ]]
}

@test "migrate-static: shim as a symbolic link is not tolerated" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app"
    ln -s "$SHIM_REPO" "$INST/server/app/__init__.py"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -ne 0 ]
}

# Deployment incident: a host with the complete old app/ and then the rsync of
# 03 of the new tree (old code out, shim in, host static excluded from
# --delete). migrate-static used to abort because of the shim.
@test "rsync of 03 + migrate-static on a host with a complete old app/ and shim" {
    command -v rsync
    load_rsync_args
    make_src_with_shim
    make_old_full_app

    run rsync -a --delete "${RSYNC_ARGS[@]}" "$SRC"/ "$INST"/
    [ "$status" -eq 0 ]
    # Incident state: old code out, shim and host static in.
    [ ! -e "$INST/server/app/main.py" ]
    [ ! -e "$INST/server/app/api" ]
    cmp "$SRC/server/app/__init__.py" "$INST/server/app/__init__.py"
    [ -f "$INST/server/app/static/robots.txt" ]

    layout migrate-static --shim-source "$SRC/server/app/__init__.py"
    [ "$status" -eq 0 ]
    cmp "$SRC/server/app/__init__.py" "$INST/server/app/__init__.py"
    [ "$(cat "$INST/server/orcaster_server/static/robots.txt")" = "User-agent: *" ]
    [ ! -e "$INST/server/app/static" ]
    [ "$(cat "$INST/server/orcaster_server/static/app.css")" = "body{}" ]

    # Second deployment on the already migrated host: rsync + migrate-static are idempotent.
    run rsync -a --delete "${RSYNC_ARGS[@]}" "$SRC"/ "$INST"/
    [ "$status" -eq 0 ]
    layout migrate-static --shim-source "$SRC/server/app/__init__.py"
    [ "$status" -eq 0 ]
    cmp "$SRC/server/app/__init__.py" "$INST/server/app/__init__.py"
    [ -f "$INST/server/orcaster_server/static/robots.txt" ]
}

@test "03-app-install.sh passes the source tree shim to migrate-static" {
    local f="$BATS_TEST_DIRNAME/../03-app-install.sh"
    run grep -F -e '--shim-source "$REPO_ROOT/server/app/__init__.py"' "$f"
    [ "$status" -eq 0 ]
}

# --- rsync of 03 on the real host: perishable __pycache__ and HOME state --
# The tests with a real rsync use GNU rsync: it is the one in CI (Linux) and in
# production. openrsync (macOS /usr/bin/rsync) does not implement the perishable
# modifier (-p). Locally without GNU rsync they are skipped with an explicit
# reason; with CI=true the absence of GNU rsync is a failure, never a skip.
need_gnu_rsync() {
    local c
    for c in "$(command -v rsync || true)" /opt/homebrew/bin/rsync /usr/local/bin/rsync; do
        { [ -n "$c" ] && [ -x "$c" ]; } || continue
        if "$c" --version 2>/dev/null | head -1 | grep -q '^rsync  version'; then
            RSYNC_BIN="$c"
            return 0
        fi
    done
    if [ -n "${CI:-}" ]; then
        echo "CI without GNU rsync: these tests cannot be skipped" >&2
        return 1
    fi
    skip "no GNU rsync (only openrsync): the perishable -p filter is validated by the Linux CI"
}

# Tree of the real host (old release, after two rollbacks) built from the
# tests/fixtures listing: complete app/ and alembic/ packages, and the orphan
# server/orcaster_server/ left by the failed attempt with only __pycache__. The
# listing does not distinguish files from directories and has no .pyc files: a
# name without a dot, a __pycache__ or a prefix of another path is a directory;
# each __pycache__ gets a .pyc.
make_real_host_v058() {
    local kind rel
    while read -r kind rel; do
        if [ "$kind" = d ]; then
            mkdir -p "$INST/$rel"
            if [ "${rel##*/}" = __pycache__ ]; then
                echo pyc > "$INST/$rel/x.cpython-313.pyc"
            fi
        else
            mkdir -p "$INST/$(dirname "$rel")"
            echo "x = 1" > "$INST/$rel"
        fi
    done < <(awk '
        { p[NR] = $0; n = split($0, a, "/")
          for (i = 1; i < n; i++) { q = a[1]; for (j = 2; j <= i; j++) q = q "/" a[j]; pre[q] = 1 } }
        END { for (i = 1; i <= NR; i++) {
                n = split(p[i], a, "/"); b = a[n]
                d = (b == "__pycache__" || b !~ /\./ || (p[i] in pre))
                print (d ? "d " : "f ") p[i] } }' \
        "${BATS_TEST_DIRNAME}/fixtures/host-v0.58.0-server-tree.txt")
    # State of the service HOME that is not in git.
    mkdir -p "$INST/.cache/uv/sdists-v9/.git" "$INST/.uv-cache/wheels" \
        "$INST/secrets" "$INST/server/.venv" "$INST/.local/share" "$INST/.config/uv"
    echo gitdir > "$INST/.cache/uv/sdists-v9/.git/HEAD"
    echo wheel > "$INST/.uv-cache/wheels/a.whl"
    echo "ed25519-private" > "$INST/secrets/ed25519.pem"
    echo "ed25519-public" > "$INST/secrets/ed25519.pub"
    echo "task-secrets-keys" > "$INST/secrets/task-secrets-keys"
    echo "age-backup" > "$INST/secrets/task-secrets-keys.20261027T125258Z.age"
    echo "home=venv" > "$INST/server/.venv/pyvenv.cfg"
    echo "state" > "$INST/.local/share/state"
    echo "cfg" > "$INST/.config/uv/uv.toml"
    echo "KEY=1" > "$INST/server/.env"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
}

# Source tree: without server/alembic or server/app except the shim.
make_src_v059() {
    make_src_with_shim
    mkdir -p "$SRC/server/orcaster_server/api" "$SRC/server/orcaster_server/migrations/versions" \
        "$SRC/docs/sub/.cache"
    echo "x = 1" > "$SRC/server/orcaster_server/__init__.py"
    echo "x = 1" > "$SRC/server/orcaster_server/api/__init__.py"
    echo "x = 1" > "$SRC/server/orcaster_server/migrations/env.py"
    echo "x = 1" > "$SRC/server/orcaster_server/migrations/versions/0001_init.py"
    echo "[alembic]" > "$SRC/server/alembic.ini"
    echo "tracked" > "$SRC/docs/sub/.cache/tracked.txt"
}

# rsync warns "cannot delete non-empty directory" (exit 0) for a directory that
# no longer exists in the source but holds a deliberately excluded file. In the
# fixture only server/app/static is legitimate, which keeps the robots.txt of
# HOST_ONLY_STATIC; any other (e.g. a directory retained by __pycache__) is the defect.
assert_no_stale_dir_warnings() {
    local unexpected
    unexpected="$(printf '%s\n' "$output" | grep 'cannot delete' \
        | grep -vx 'cannot delete non-empty directory: server/app/static' || true)"
    [ -z "$unexpected" ] || { echo "unexpected warnings: $unexpected" >&2; return 1; }
}

@test "rsync of 03 on the real host: no cannot delete, no leftovers, state intact" {
    need_gnu_rsync
    load_rsync_args
    make_src_v059
    make_real_host_v058
    # Preconditions: the listing reproduces the incident host.
    [ -f "$INST/server/alembic/versions/0061_te_worker_assigned_idx.py" ]
    [ -f "$INST/server/app/main.py" ]
    [ -f "$INST/server/orcaster_server/api/__pycache__/x.cpython-313.pyc" ]
    [ ! -e "$INST/server/orcaster_server/main.py" ]

    run "$RSYNC_BIN" -a --delete "${RSYNC_ARGS[@]}" "$SRC"/ "$INST"/
    [ "$status" -eq 0 ]
    assert_no_stale_dir_warnings
    # Removed packages: gone with their __pycache__, except what the host excluded.
    [ ! -e "$INST/server/alembic" ]
    [ ! -e "$INST/server/app/main.py" ]
    [ ! -e "$INST/server/app/api" ]
    [ ! -e "$INST/server/app/workspaces" ]
    [ ! -e "$INST/server/app/auth" ]
    cmp "$SRC/server/app/__init__.py" "$INST/server/app/__init__.py"
    [ -f "$INST/server/app/static/robots.txt" ]
    [ -f "$INST/server/orcaster_server/main.py" ]
    # Host state: service HOME, secrets, .env, venv.
    [ "$(cat "$INST/.cache/uv/sdists-v9/.git/HEAD")" = gitdir ]
    [ "$(cat "$INST/.local/share/state")" = state ]
    [ "$(cat "$INST/.config/uv/uv.toml")" = cfg ]
    [ "$(cat "$INST/.uv-cache/wheels/a.whl")" = wheel ]
    [ "$(cat "$INST/server/.venv/pyvenv.cfg")" = "home=venv" ]
    [ "$(cat "$INST/server/.env")" = "KEY=1" ]
    [ "$(cat "$INST/secrets/ed25519.pem")" = "ed25519-private" ]
    [ "$(cat "$INST/secrets/ed25519.pub")" = "ed25519-public" ]
    [ "$(cat "$INST/secrets/task-secrets-keys")" = "task-secrets-keys" ]
    [ "$(cat "$INST/secrets/task-secrets-keys.20261027T125258Z.age")" = "age-backup" ]

    # migrate-static, in a single run: shim intact and host static migrated.
    layout migrate-static --shim-source "$SRC/server/app/__init__.py"
    [ "$status" -eq 0 ]
    cmp "$SRC/server/app/__init__.py" "$INST/server/app/__init__.py"
    [ "$(cat "$INST/server/orcaster_server/static/robots.txt")" = "User-agent: *" ]
    run find "$INST/server/app" -mindepth 1 -not -name __init__.py
    [ -z "$output" ]
    [ ! -e "$INST/server/alembic" ]

    # A second deployment on the already migrated host changes nothing.
    run "$RSYNC_BIN" -a --delete "${RSYNC_ARGS[@]}" "$SRC"/ "$INST"/
    [ "$status" -eq 0 ]
    assert_no_stale_dir_warnings
    layout migrate-static --shim-source "$SRC/server/app/__init__.py"
    [ "$status" -eq 0 ]
    [ -f "$INST/server/orcaster_server/static/robots.txt" ]
    [ "$(cat "$INST/secrets/ed25519.pem")" = "ed25519-private" ]
}

@test "rsync of 03: removed directory with only __pycache__ is deleted and the live one stays" {
    need_gnu_rsync
    load_rsync_args
    make_src_v059
    mkdir -p "$INST/server/alembic/versions/__pycache__" \
        "$INST/server/orcaster_server/api/__pycache__"
    echo "x = 1" > "$INST/server/alembic/env.py"
    echo pyc > "$INST/server/alembic/versions/__pycache__/0001_init.cpython-313.pyc"
    echo pyc > "$INST/server/orcaster_server/api/__pycache__/y.cpython-313.pyc"
    run "$RSYNC_BIN" -a --delete "${RSYNC_ARGS[@]}" "$SRC"/ "$INST"/
    [ "$status" -eq 0 ]
    [[ "$output" != *"cannot delete non-empty directory"* ]]
    [ ! -e "$INST/server/alembic" ]
    # __pycache__ of a directory that still exists is not touched.
    [ -f "$INST/server/orcaster_server/api/__pycache__/y.cpython-313.pyc" ]
    [ -f "$INST/server/orcaster_server/api/__init__.py" ]
}

@test "rsync of 03: HOME /.cache /.local /.config survive and the anchored ones do not hide versioned paths" {
    need_gnu_rsync
    load_rsync_args
    make_src_v059
    mkdir -p "$INST/.cache/uv" "$INST/.local/share" "$INST/.config/uv" "$INST/docs/sub/.cache" \
        "$INST/secrets"
    echo c > "$INST/.cache/uv/f"
    echo l > "$INST/.local/share/f"
    echo g > "$INST/.config/uv/f"
    echo "old tracked" > "$INST/docs/sub/.cache/tracked.txt"
    echo gone > "$INST/removed-from-git.txt"
    echo key > "$INST/secrets/ed25519.pem"
    run "$RSYNC_BIN" -a --delete "${RSYNC_ARGS[@]}" "$SRC"/ "$INST"/
    [ "$status" -eq 0 ]
    [[ "$output" != *"cannot delete"* ]]
    [ "$(cat "$INST/.cache/uv/f")" = c ]
    [ "$(cat "$INST/.local/share/f")" = l ]
    [ "$(cat "$INST/.config/uv/f")" = g ]
    [ "$(cat "$INST/secrets/ed25519.pem")" = key ]
    # Anchored: docs/sub/.cache is tracked and gets updated; what was removed from git goes away.
    [ "$(cat "$INST/docs/sub/.cache/tracked.txt")" = tracked ]
    [ ! -e "$INST/removed-from-git.txt" ]
}

@test "migrate-static: subdirectories with only __pycache__ next to the shim are cleaned" {
    make_tree orcaster_server
    mkdir -p "$INST/server/app/api/__pycache__" "$INST/server/app/auth/service/__pycache__" \
        "$INST/server/app/__pycache__" "$INST/server/app/static"
    cp "$SHIM_REPO" "$INST/server/app/__init__.py"
    echo pyc > "$INST/server/app/api/__pycache__/x.cpython-313.pyc"
    echo pyc > "$INST/server/app/auth/service/__pycache__/y.cpython-313.pyc"
    echo pyc > "$INST/server/app/__pycache__/__init__.cpython-313.pyc"
    echo "User-agent: *" > "$INST/server/app/static/robots.txt"
    layout migrate-static --shim-source "$SHIM_REPO"
    [ "$status" -eq 0 ]
    cmp "$SHIM_REPO" "$INST/server/app/__init__.py"
    [ "$(cat "$INST/server/orcaster_server/static/robots.txt")" = "User-agent: *" ]
    run find "$INST/server/app" -mindepth 1 -not -name __init__.py
    [ -z "$output" ]
}

@test "03-app-install.sh uses perishable __pycache__ and anchors the HOME state" {
    local f="$BATS_TEST_DIRNAME/../03-app-install.sh"
    run grep -F -e "--filter '-p __pycache__'" "$f"
    [ "$status" -eq 0 ]
    run grep -F -e "--exclude '__pycache__'" "$f"
    [ "$status" -ne 0 ]
    for p in /.cache /.local /.config; do
        run grep -F -e "--exclude '$p'" "$f"
        [ "$status" -eq 0 ]
    done
}

@test "03-app-install.sh calls migrate-static after the rsync and align-unit at the end" {
    local f="$BATS_TEST_DIRNAME/../03-app-install.sh" rsync_line migrate_line align_line
    rsync_line="$(grep -n 'as_app rsync -a --delete' "$f" | cut -d: -f1)"
    migrate_line="$(grep -n 'server-layout.sh" migrate-static' "$f" | cut -d: -f1)"
    align_line="$(grep -n 'server-layout.sh" align-unit' "$f" | cut -d: -f1)"
    [ -n "$rsync_line" ] && [ -n "$migrate_line" ] && [ -n "$align_line" ]
    [ "$migrate_line" -gt "$rsync_line" ]
    [ "$align_line" -gt "$migrate_line" ]
    grep -q 'RSYNC_EXCLUDES+=("${_HOST_ONLY_EXCLUDES\[@\]}")' "$f"
}

@test "setup.env.example documents HOST_ONLY_STATIC" {
    grep -q '^#HOST_ONLY_STATIC=' "$BATS_TEST_DIRNAME/../setup.env.example"
}

@test "align-unit: the restore-unit notice names the script by file" {
    make_tree orcaster_server
    make_unit app
    layout align-unit
    [ "$status" -eq 0 ]
    [[ "$output" == *"sudo bash $LAYOUT restore-unit"* ]]
}

@test "align-unit: via bash -s the notice does not say bash bash" {
    make_tree orcaster_server
    make_unit app
    run bash -s -- align-unit --install-dir "$INST" --app-user "$ME" \
        --unit "$UNIT" --backup-dir "$BACKUPS" < "$LAYOUT"
    [ "$status" -eq 0 ]
    [[ "$output" != *"bash bash"* ]]
    [[ "$output" == *"<root checkout>/setup-scripts/debian13/server-layout.sh restore-unit"* ]]
}
