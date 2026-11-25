#!/usr/bin/env bats
# Tests of the source guard of the worker suite (lib.sh:
# require_root_owned_source, _source_guard_check_env_file and
# require_source_outside_install_dir).
# Run:  bats setup-scripts/debian13-worker/tests/
#
# NOTE (bats-core 1.13 gotcha): @test names are plain ASCII.
# With macOS's bash 3.2 a `[[ ]]` that is not the last statement does not make
# the test fail: that is why each one carries `|| return 1`.
#
# No test uses root: `id -u` and `ls -lnd` are stubbed (see
# setup-scripts/debian13/tests/source_guard_stub.bash). The source tree is a
# copy of the scripts in the tmpdir; the stub presents it as root-owned except
# for what the test marks.

load ../../debian13/tests/source_guard_stub

setup() {
    umask 022
    T="$(cd "$BATS_TEST_TMPDIR" && pwd -P)"
    SRC="$T/src"
    SUITE="$SRC/setup-scripts/debian13-worker"
    mkdir -p "$SUITE"
    cp "$BATS_TEST_DIRNAME"/../*.sh "$SUITE/"
    cp "$BATS_TEST_DIRNAME/../../../Makefile" "$SRC/Makefile"
    STUB="$T/stubs"
    write_ls_stub "$STUB"
    write_root_id_stub "$STUB"
    # Commands with effects: they record the call and do nothing.
    for c in apt-get useradd usermod install mkdir chown chmod loginctl podman \
             sudo tee systemctl curl make openssl; do
        printf '#!/usr/bin/env bash\necho "%s $*" >> "%s/effects"\n' "$c" "$T" \
            > "$STUB/$c"
        chmod +x "$STUB/$c"
    done
    export SOURCE_GUARD_FIXTURE="$SRC"
    export SOURCE_GUARD_FOREIGN="$T/foreign"
    : > "$SOURCE_GUARD_FOREIGN"
    export CONFIG_DIR="$T/etc"
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG_DIR/setup.env" <<E
ORCH_BASE_URL="https://orch.example.org"
INSTALL_DIR="$T/install"
E
    printf 'ID=debian\nVERSION_ID="13"\n' > "$T/os-release"
    export OS_RELEASE_FILE="$T/os-release"
    # Minimal CONFIG of cloud-init-worker.sh.
    export ORCH_ENROLLMENT_TOKEN="tok"
    export CLOUD_INIT_LOG="$T/cloud-init.log"
    unset ENV_FILE_OVERRIDE LIB_OVERRIDE INSTALL_DIR
}

# Sources the copy's lib.sh as simulated root and then runs "$@".
run_as_fake_root() {
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; shift; "$@"' _ \
        "$SUITE/lib.sh" "$@"
}

# Appends INSTALL_DIR=$1 to setup.env (the last one wins when sourcing).
set_install_dir() {
    printf 'INSTALL_DIR="%s"\n' "$1" >> "$CONFIG_DIR/setup.env"
}

@test "guard: a root tree passes and defines physical SCRIPT_DIR, REPO_ROOT and RO_MOUNTS_CHECK" {
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; echo "$SCRIPT_DIR|$REPO_ROOT|$RO_MOUNTS_CHECK"' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [ "$output" = "$SUITE|$SRC|$SUITE/check-ro-mounts.sh" ]
}

@test "guard: a worker-user lib.sh aborts naming path, owner and mode" {
    echo "$SUITE/lib.sh" > "$SOURCE_GUARD_FOREIGN"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/lib.sh has owner uid 1001 and mode -rw-r--r--"* ]] || return 1
    [[ "$output" == *"/root/orcaster"* ]] || return 1
}

@test "guard: check-ro-mounts.sh with group write permission aborts" {
    chmod g+w "$SUITE/check-ro-mounts.sh"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/check-ro-mounts.sh has owner uid 0 and mode -rw-rw-r--"* ]] \
        || [[ "$output" == *"$SUITE/check-ro-mounts.sh has owner uid 0 and mode -rwxrwxr-x"* ]] || return 1
}

@test "guard: run-fleet.sh with other write permission aborts" {
    chmod o+w "$SUITE/run-fleet.sh"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/run-fleet.sh has owner uid 0"* ]] || return 1
}

@test "guard: a worker-user user-bus.sh aborts" {
    echo "$SUITE/user-bus.sh" > "$SOURCE_GUARD_FOREIGN"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/user-bus.sh has owner uid 1001"* ]] || return 1
}

@test "guard: a worker-user Makefile aborts" {
    echo "$SRC/Makefile" > "$SOURCE_GUARD_FOREIGN"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SRC/Makefile has owner uid 1001"* ]] || return 1
}

@test "guard: an ancestor directory with group write permission aborts" {
    chmod g+w "$SRC/setup-scripts"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SRC/setup-scripts has owner uid 0 and mode drwxrwxr-x"* ]] || return 1
}

@test "guard: a missing run-all-worker.sh aborts" {
    rm "$SUITE/run-all-worker.sh"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"cannot read the owner or mode of $SUITE/run-all-worker.sh"* ]] || return 1
}

@test "guard: a script that is a symbolic link aborts" {
    mv "$SUITE/04-build.sh" "$T/04-build.sh"
    ln -s "$T/04-build.sh" "$SUITE/04-build.sh"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/04-build.sh is not a regular file"* ]] || return 1
}

@test "guard: invoking through a path with symbolic links aborts" {
    ln -s "$SRC" "$T/via-link"
    run env PATH="$STUB:$PATH" bash -c 'source "$1"' _ "$T/via-link/setup-scripts/debian13-worker/lib.sh"
    [ "$status" -eq 1 ]
    [[ "$output" == *"goes through symbolic links"* ]] || return 1
}

@test "guard: a setup.env owned by another user aborts before sourcing it" {
    echo 'touch "$SENTINEL"' >> "$CONFIG_DIR/setup.env"
    echo "$CONFIG_DIR/setup.env" > "$SOURCE_GUARD_FOREIGN"
    SOURCE_GUARD_FIXTURE="$T" SENTINEL="$T/sourced" run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"$CONFIG_DIR/setup.env has owner uid 1001"* ]] || return 1
    [ ! -e "$T/sourced" ]
}

@test "guard: a setup.env with group write permission aborts before sourcing it" {
    echo 'touch "$SENTINEL"' >> "$CONFIG_DIR/setup.env"
    chmod 664 "$CONFIG_DIR/setup.env"
    SOURCE_GUARD_FIXTURE="$T" SENTINEL="$T/sourced" run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"$CONFIG_DIR/setup.env has owner uid 0 and mode -rw-rw-r--"* ]] || return 1
    [ ! -e "$T/sourced" ]
}

@test "guard: a CONFIG_DIR with group write permission aborts before sourcing setup.env" {
    chmod g+w "$CONFIG_DIR"
    SOURCE_GUARD_FIXTURE="$T" run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"$CONFIG_DIR has owner uid 0 and mode drwxrwxr-x"* ]] || return 1
}

@test "guard: setup.env 0640 in CONFIG_DIR 0750 (host layout) passes" {
    chmod 0750 "$CONFIG_DIR"
    chmod 0640 "$CONFIG_DIR/setup.env"
    run env PATH="$STUB:$PATH" SOURCE_GUARD_FIXTURE="$T" \
        bash -c 'source "$1"; load_env; echo "install=$INSTALL_DIR"' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"install=$T/install"* ]] || return 1
}

@test "guard: REPO_ROOT equal to INSTALL_DIR aborts" {
    set_install_dir "$SRC"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"is inside INSTALL_DIR ($SRC)"* ]] || return 1
}

@test "guard: REPO_ROOT inside INSTALL_DIR aborts (suite run from /opt)" {
    set_install_dir "$T"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"is inside INSTALL_DIR"* ]] || return 1
}

@test "guard: INSTALL_DIR inside the tree aborts, even if it does not exist yet" {
    set_install_dir "$SRC/deploy/new"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"INSTALL_DIR ($SRC/deploy/new) is inside the tree"* ]] || return 1
}

@test "guard: an INSTALL_DIR that reaches the tree through a link aborts" {
    ln -s "$SRC" "$T/alias"
    set_install_dir "$T/alias"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"is inside INSTALL_DIR ($T/alias)"* ]] || return 1
}

@test "guard: an overlapping INSTALL_DIR aborts before validating WORKER_RO_MOUNTS" {
    set_install_dir "$SRC"
    echo 'WORKER_RO_MOUNTS="/srv/x"' >> "$CONFIG_DIR/setup.env"
    # A check-ro-mounts.sh that leaves a trace: it must not run.
    echo 'touch "$SENTINEL"' >> "$SUITE/check-ro-mounts.sh"
    SENTINEL="$T/ro-check-ran" run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"Source guard"* ]] || return 1
    [ ! -e "$T/ro-check-ran" ]
}

@test "guard: without root it does not act (06-autostart.sh as the worker, fleet-disk-check.sh)" {
    echo "$SUITE/lib.sh" > "$SOURCE_GUARD_FOREIGN"
    rm "$STUB/id"
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; echo sourced' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [ "$output" = "sourced" ]
}

@test "guard: aborts before any effect in 00, 02, 03, run-all-worker, run-fleet and cloud-init" {
    echo "$SUITE/lib.sh" > "$SOURCE_GUARD_FOREIGN"
    local s
    for s in 00-preflight.sh 02-server-pubkey.sh 03-worker-env.sh \
             run-all-worker.sh run-fleet.sh 06-autostart.sh cloud-init-worker.sh; do
        run env PATH="$STUB:$PATH" bash "$SUITE/$s"
        [ "$status" -eq 1 ]
        [[ "$output" == *"Source guard"* ]] || return 1
    done
    [ ! -e "$T/effects" ]
    [ ! -e "$CLOUD_INIT_LOG" ]
}

@test "guard: with an overlapping INSTALL_DIR it aborts before any effect" {
    set_install_dir "$SRC"
    local s
    for s in 00-preflight.sh 03-worker-env.sh run-all-worker.sh run-fleet.sh; do
        run env PATH="$STUB:$PATH" bash "$SUITE/$s"
        [ "$status" -eq 1 ]
        [[ "$output" == *"Source guard"* ]] || return 1
    done
    [ ! -e "$T/effects" ]
}

@test "guard: cloud-init with an overlapping INSTALL_DIR aborts before any effect" {
    # cloud-init does not call load_env: INSTALL_DIR comes from its CONFIG block.
    run env PATH="$STUB:$PATH" INSTALL_DIR="$T" bash "$SUITE/cloud-init-worker.sh"
    [ "$status" -eq 1 ]
    [[ "$output" == *"is inside INSTALL_DIR ($T)"* ]] || return 1
    [ ! -e "$T/effects" ]
    [ ! -e "$CLOUD_INIT_LOG" ]
}

@test "guard: the old user-data (cloud-init from the worker INSTALL_DIR) aborts" {
    # Previous layout: the suite inside /opt/orcaster, owned by the worker.
    local inst="$T/opt/orcaster"
    mkdir -p "$inst/setup-scripts"
    cp -R "$SUITE" "$inst/setup-scripts/"
    echo "$inst" > "$SOURCE_GUARD_FOREIGN"
    run env PATH="$STUB:$PATH" SOURCE_GUARD_FIXTURE="$T/opt" INSTALL_DIR="$inst" \
        bash "$inst/setup-scripts/debian13-worker/cloud-init-worker.sh"
    [ "$status" -eq 1 ]
    [[ "$output" == *"$inst has owner uid 1001"* ]] || return 1
    [ ! -e "$T/effects" ]
}

@test "guard: INSTALL_DIR with a '..' component aborts" {
    set_install_dir "$T/nothing/../src"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not allow '.' or '..' as a path component"* ]] || return 1
}

@test "guard: INSTALL_DIR with a '.' component aborts" {
    set_install_dir "$T/./install"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not allow '.' or '..' as a path component"* ]] || return 1
}

@test "guard: a directory named a..b in INSTALL_DIR is legal" {
    set_install_dir "$T/a..b/install"
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; load_env; echo "install=$INSTALL_DIR"' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"install=$T/a..b/install"* ]] || return 1
}

@test "guard: load_env sources the verified physical path of setup.env" {
    # CONFIG_DIR through a link: the real target is verified and sourced.
    mv "$CONFIG_DIR" "$T/etc-real"
    ln -s "$T/etc-real" "$CONFIG_DIR"
    echo 'echo "sourced-from=${BASH_SOURCE[0]}"' >> "$T/etc-real/setup.env"
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; load_env' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"sourced-from=$T/etc-real/setup.env"* ]] || return 1
}

@test "guard: a failing _source_guard_check_env_file stops load_env without sourcing" {
    echo 'touch "$SENTINEL"' >> "$CONFIG_DIR/setup.env"
    echo "$CONFIG_DIR" > "$SOURCE_GUARD_FOREIGN"
    # Without set -e: the stop cannot depend on the caller's errexit.
    SOURCE_GUARD_FIXTURE="$T" SENTINEL="$T/sourced" run env PATH="$STUB:$PATH" \
        bash -c 'source "$1"; load_env; echo continued' _ "$SUITE/lib.sh"
    [ "$status" -eq 1 ]
    [[ "$output" != *"continued"* ]] || return 1
    [ ! -e "$T/sourced" ]
}

@test "read_release_file: root without the worker user reads a root-owned RELEASE" {
    mkdir -p "$T/install"
    printf 'version=1\ncommit=abc\n' > "$T/install/RELEASE"
    run_as_fake_root read_release_file "$T/install" nonexistent-user-xyz
    [ "$status" -eq 0 ]
    [[ "$output" == *"commit=abc"* ]] || return 1
}

@test "read_release_file: root without the worker user does not read a foreign RELEASE" {
    mkdir -p "$T/install"
    printf 'commit=abc\n' > "$T/install/RELEASE"
    echo "$T/install/RELEASE" > "$SOURCE_GUARD_FOREIGN"
    SOURCE_GUARD_FIXTURE="$T" run_as_fake_root read_release_file "$T/install" nonexistent-user-xyz
    [ "$status" -ne 0 ]
    [ -z "$output" ]
}

@test "read_release_file: root without the worker user does not follow a link" {
    mkdir -p "$T/install"
    echo "commit=secret" > "$T/secret"
    ln -s "$T/secret" "$T/install/RELEASE"
    run_as_fake_root read_release_file "$T/install" nonexistent-user-xyz
    [ "$status" -ne 0 ]
    [ -z "$output" ]
}

@test "read_release_file: root with the worker user reads it as that user" {
    mkdir -p "$T/install"
    printf 'commit=abc\n' > "$T/install/RELEASE"
    run_as_fake_root read_release_file "$T/install" "$(id -un)"
    # The sudo stub executes nothing: if root had read it, it would show up.
    [ -z "$output" ]
    grep -qx "sudo -u $(id -un) cat -- $T/install/RELEASE" "$T/effects"
}

@test "guard: LIB_OVERRIDE is ignored as root in run-fleet.sh and 06-autostart.sh" {
    # The tree's lib.sh (foreign, so its guard aborts) is the one that runs;
    # the LIB_OVERRIDE one would leave a trace.
    echo "$SUITE/lib.sh" > "$SOURCE_GUARD_FOREIGN"
    printf 'touch "%s"\n' "$T/override-sourced" > "$T/evil-lib.sh"
    local s
    for s in run-fleet.sh 06-autostart.sh; do
        run env PATH="$STUB:$PATH" LIB_OVERRIDE="$T/evil-lib.sh" bash "$SUITE/$s"
        [ "$status" -eq 1 ]
        [[ "$output" == *"Source guard"* ]] || return 1
    done
    [ ! -e "$T/override-sourced" ]
}
