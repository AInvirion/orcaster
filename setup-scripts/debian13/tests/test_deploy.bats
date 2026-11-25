#!/usr/bin/env bats
# Tests for setup-scripts/debian13/deploy.sh with fake ssh and rsync: step order,
# rsync destination (root checkout, not INSTALL_DIR), exclusions and the
# align-unit sent from the operator's checkout.
# Run:  bats setup-scripts/debian13/tests/
#
# NOTE (bats-core 1.13 gotcha): @test names must be plain ASCII.

bats_require_minimum_version 1.5.0

load stub_guard

DEPLOY="${BATS_TEST_DIRNAME}/../deploy.sh"

setup() {
    STUBS="$BATS_TEST_TMPDIR/bin"
    CALLS="$BATS_TEST_TMPDIR/calls"
    PIPED="$BATS_TEST_TMPDIR/piped"
    mkdir -p "$STUBS"
    # Fake ssh: records the remote command; for `bash -s`, saves the stdin.
    # STUB_NO_SETUP_ENV=1 simulates a host without setup.env in the checkout.
    cat > "$STUBS/ssh" <<EOF
#!/usr/bin/env bash
while [ "\$1" = "-p" ]; do shift 2; done
shift   # destination
cmd="\$*"
echo "ssh \$cmd" >> "$CALLS"
case "\$cmd" in
    *"bash -s"*) cat > "$PIPED" ;;
    *"sudo test -f"*) [ "\${STUB_NO_SETUP_ENV:-0}" = 0 ] ;;
    *"NRestarts"*) PATH="$STUBS/remote:\$PATH" sh -c "\$cmd" ;;
    *"08-smoke-test.sh"*) [ "\${STUB_SMOKE_FAIL:-0}" = 0 ] ;;
    *"journalctl"*) echo "journal: uvicorn traceback" ;;
esac
EOF
    # Host commands for the stability check, which really runs with sh: fake
    # systemctl (STUB_INACTIVE: stops being active; STUB_CRASHLOOP: NRestarts
    # grows on every query) and an instant sleep.
    mkdir -p "$STUBS/remote"
    echo 0 > "$BATS_TEST_TMPDIR/nrestarts"
    cat > "$STUBS/remote/systemctl" <<EOF
#!/usr/bin/env bash
case "\$1" in
    is-active) [ "\${STUB_INACTIVE:-0}" = 0 ] ;;
    show)
        n="\$(cat "$BATS_TEST_TMPDIR/nrestarts")"
        echo "\$n"
        [ "\${STUB_CRASHLOOP:-0}" = 1 ] && echo \$((n + 1)) > "$BATS_TEST_TMPDIR/nrestarts"
        true ;;
esac
EOF
    printf '#!/bin/sh\nexit 0\n' > "$STUBS/remote/sleep"
    chmod +x "$STUBS/remote/systemctl" "$STUBS/remote/sleep"
    cat > "$STUBS/rsync" <<EOF
#!/usr/bin/env bash
echo "rsync \$*" >> "$CALLS"
EOF
    chmod +x "$STUBS/ssh" "$STUBS/rsync"
    export PATH="$STUBS:$PATH"
    # Fails here, before any test, if PATH reaches the real ssh/rsync.
    assert_stubbed "$STUBS" ssh rsync

    export DEPLOY_ENV="$BATS_TEST_TMPDIR/deploy.local.env"
    cat > "$DEPLOY_ENV" <<'EOF'
VPS_SSH="root@host.test"
SOURCE_DIR="/root/orcaster"
INSTALL_DIR="/opt/orcaster"
APP_USER="orcaster"
SERVICE="orcaster.service"
DISTRO="debian13"
EOF
    # Minimal tree for --source.
    TREE="$BATS_TEST_TMPDIR/tree"
    mkdir -p "$TREE/setup-scripts/debian13"
    touch "$TREE/setup-scripts/debian13/03-app-install.sh"
}

@test "deploy: order rsync -> chown -> 03 -> align-unit -> restart -> smoke" {
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -eq 0 ]
    local order
    order="$(sed -E 's/^(ssh|rsync) .*(sudo test -f|rsync|chown -R root:root|03-app-install.sh|bash -s -- align-unit|systemctl restart|NRestarts|08-smoke-test.sh).*/\2/' "$CALLS" | tr '\n' ' ')"
    [ "$order" = "sudo test -f rsync chown -R root:root 03-app-install.sh bash -s -- align-unit systemctl restart NRestarts 08-smoke-test.sh " ] \
        || { echo "order: $order"; false; }
}

@test "deploy: rsync goes to the root checkout with sudo and --delete, never to INSTALL_DIR" {
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -eq 0 ]
    run grep '^rsync ' "$CALLS"
    [[ "$output" == *"--delete"* ]]
    [[ "$output" == *"--rsync-path=sudo rsync"* ]]
    [[ "$output" == *" root@host.test:/root/orcaster/" ]]
    [[ "$output" != *"/opt/orcaster"* ]]
    # No -a: it does not keep the local owner; the later chown leaves it as root.
    [[ "$output" == *"-rlptD"* ]]
}

@test "deploy: the exclusions protect setup.env and its backups, not the template" {
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -eq 0 ]
    local line
    line="$(grep '^rsync ' "$CALLS")"
    [[ "$line" == *"--include=/setup-scripts/debian13/setup.env.example --exclude=/setup-scripts/debian13/setup.env*"* ]]
    [[ "$line" == *"--exclude=*.env"* ]]
}

@test "deploy: the exclusions protect setup.env with a real rsync" {
    command -v /usr/bin/rsync || command -v /opt/homebrew/bin/rsync || skip "no real rsync"
    local real_rsync
    real_rsync="$(PATH="${PATH#"$STUBS:"}" command -v rsync)"
    # shellcheck source=../deploy.sh
    source "$DEPLOY"
    local args=() x src="$BATS_TEST_TMPDIR/src" dst="$BATS_TEST_TMPDIR/dst"
    while IFS= read -r x; do args+=("$x"); done < <(deploy_rsync_excludes debian13)
    mkdir -p "$src/setup-scripts/debian13" "$dst/setup-scripts/debian13"
    echo tpl > "$src/setup-scripts/debian13/setup.env.example"
    echo live > "$dst/setup-scripts/debian13/setup.env"
    echo bak > "$dst/setup-scripts/debian13/setup.env.bak-20261116"
    echo stale > "$dst/stale.txt"
    run "$real_rsync" -rlptD --delete "${args[@]}" "$src"/ "$dst"/
    [ "$status" -eq 0 ]
    [ "$(cat "$dst/setup-scripts/debian13/setup.env")" = live ]
    [ "$(cat "$dst/setup-scripts/debian13/setup.env.bak-20261116")" = bak ]
    [ -f "$dst/setup-scripts/debian13/setup.env.example" ]
    [ ! -e "$dst/stale.txt" ]
}

@test "deploy: align-unit is sent from the operator's checkout" {
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -eq 0 ]
    # The deployed tree has no server-layout.sh (rollback to an old release):
    # the one from deploy.sh's checkout is sent anyway.
    [ ! -e "$TREE/setup-scripts/debian13/server-layout.sh" ]
    cmp "$PIPED" "${BATS_TEST_DIRNAME}/../server-layout.sh"
    run grep 'bash -s -- align-unit' "$CALLS"
    [[ "$output" == *"--install-dir '/opt/orcaster'"* ]]
    [[ "$output" == *"--app-user 'orcaster'"* ]]
    [[ "$output" == *"--unit '/etc/systemd/system/orcaster.service'"* ]]
}

@test "deploy: without setup.env in the root checkout it aborts before the rsync" {
    export STUB_NO_SETUP_ENV=1
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"One-time migration"* ]]
    run grep -c '^rsync ' "$CALLS"
    [ "$output" = 0 ]
}

@test "deploy: --dry-run only runs the rsync as a dry run" {
    run bash "$DEPLOY" --source "$TREE" --dry-run
    [ "$status" -eq 0 ]
    grep -q '^rsync .*--dry-run' "$CALLS"
    run grep -cE '03-app-install|align-unit|restart|chown' "$CALLS"
    [ "$output" = 0 ]
}

@test "deploy: --fast is rejected with the cause" {
    run bash "$DEPLOY" --source "$TREE" --fast
    [ "$status" -ne 0 ]
    [[ "$output" == *"03-app-install.sh"* ]]
    [ ! -e "$CALLS" ]
}

@test "deploy: SOURCE_DIR inside INSTALL_DIR aborts" {
    sed -i.bak 's#^SOURCE_DIR=.*#SOURCE_DIR="/opt/orcaster/src"#' "$DEPLOY_ENV"
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"inside INSTALL_DIR"* ]]
    [ ! -e "$CALLS" ]
}

@test "deploy: inactive service after the restart fails with the journal and no smoke" {
    export STUB_INACTIVE=1
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"stopped being active"* ]]
    [[ "$output" == *"journal: uvicorn traceback"* ]]
    [[ "$output" != *"Deploy finished"* ]]
    run grep -c '08-smoke-test.sh' "$CALLS"
    [ "$output" = 0 ]
}

@test "deploy: crash-loop (NRestarts grows) fails even if the unit stays active" {
    export STUB_CRASHLOOP=1
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"crash-loop"* ]]
    [[ "$output" == *"journal: uvicorn traceback"* ]]
    [[ "$output" != *"Deploy finished"* ]]
}

@test "deploy: a stable service passes the check and reports it" {
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"active and without restarts for 20 s"* ]]
    [[ "$output" == *"Deploy finished"* ]]
}

@test "deploy: a failed smoke test is fatal and shows the journal" {
    export STUB_SMOKE_FAIL=1
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"The smoke test failed"* ]]
    [[ "$output" == *"journal: uvicorn traceback"* ]]
    [[ "$output" != *"Deploy finished"* ]]
}

@test "deploy: --no-smoke warns that only the stability check remains" {
    run bash "$DEPLOY" --source "$TREE" --no-smoke
    [ "$status" -eq 0 ]
    [[ "$output" == *"nobody queried /healthz"* ]]
    run grep -c '08-smoke-test.sh' "$CALLS"
    [ "$output" = 0 ]
}

@test "deploy: invalid DEPLOY_STABLE_SECONDS aborts before touching the host" {
    export DEPLOY_STABLE_SECONDS=abc
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"DEPLOY_STABLE_SECONDS"* ]]
    [ ! -e "$CALLS" ]
}

@test "deploy: --ref uses git archive of the ref and rejects a nonexistent one" {
    export GIT_CONFIG_GLOBAL=/dev/null
    run bash "$DEPLOY" --ref HEAD --no-restart
    [ "$status" -eq 0 ]
    [[ "$output" == *"git archive HEAD"* ]]
    run bash "$DEPLOY" --ref does-not-exist-ref
    [ "$status" -ne 0 ]
    [[ "$output" == *"does not exist in"* ]]
}

@test "deploy: the git archive tree omits docs and keeps the code" {
    export GIT_CONFIG_GLOBAL=/dev/null
    # Fake rsync that copies the source tree (second to last argument).
    cat > "$STUBS/rsync" <<EOF
#!/usr/bin/env bash
src="\${@: -2:1}"
cp -R "\$src" "$BATS_TEST_TMPDIR/snapshot"
EOF
    chmod +x "$STUBS/rsync"
    run bash "$DEPLOY" --ref HEAD --no-restart
    [ "$status" -eq 0 ]
    S="$BATS_TEST_TMPDIR/snapshot"
    [ -d "$S/server" ]
    [ -d "$S/setup-scripts" ]
    [ -d "$S/ops" ]
    omitted=(docs llms.txt)
    for p in "${omitted[@]}"; do
        [ ! -e "$S/$p" ]
    done
}

@test "deploy: without config it creates the template and stops" {
    rm -f "$DEPLOY_ENV"
    run bash "$DEPLOY" --source "$TREE"
    [ "$status" -ne 0 ]
    [ -f "$DEPLOY_ENV" ]
    grep -q '^SOURCE_DIR="/root/orcaster"' "$DEPLOY_ENV"
    [ ! -e "$CALLS" ]
}
