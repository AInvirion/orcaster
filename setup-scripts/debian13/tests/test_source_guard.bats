#!/usr/bin/env bats
# Tests for the server installer's source guard (lib.sh:
# require_root_owned_source, _source_guard_check_env_file and
# require_source_outside_install_dir).
# Run:  bats setup-scripts/debian13/tests/
#
# NOTE (bats-core 1.13 gotcha): @test names must be plain ASCII.
#
# No test uses root: `id -u` and `ls -lnd` are stubbed (see
# source_guard_stub.bash). The source tree is a copy of the scripts in the
# tmpdir; the stub presents it as root-owned except what the test marks.

load source_guard_stub
load stub_guard

setup() {
    umask 022
    T="$(cd "$BATS_TEST_TMPDIR" && pwd -P)"
    SRC="$T/src"
    SUITE="$SRC/setup-scripts/debian13"
    mkdir -p "$SUITE"
    cp "$BATS_TEST_DIRNAME"/../*.sh "$SUITE/"
    cp "$BATS_TEST_DIRNAME/../../../Makefile" "$SRC/Makefile"
    STUB="$T/stubs"
    write_ls_stub "$STUB"
    write_root_id_stub "$STUB"
    # Commands with side effects: they record the call and do nothing.
    for c in apt-get useradd install mkdir chown chmod rsync sudo systemctl \
             psql curl uv openssl caddy ufw; do
        printf '#!/usr/bin/env bash\necho "%s $*" >> "%s/effects"\n' "$c" "$T" \
            > "$STUB/$c"
        chmod +x "$STUB/$c"
    done
    # Tests prepend $STUB to PATH with `env`; rsync must resolve to the stub.
    PATH="$STUB:$PATH" assert_stubbed "$STUB" rsync
    export SOURCE_GUARD_FIXTURE="$SRC"
    export SOURCE_GUARD_FOREIGN="$T/foreign"
    : > "$SOURCE_GUARD_FOREIGN"
    cat > "$T/setup.env" <<E
DOMAIN="test.example.org"
ADMIN_EMAIL="ops@test.example.org"
INSTALL_DIR="$T/install"
E
    unset ENV_FILE_OVERRIDE
}

# Sources the copy's lib.sh as simulated root and then runs "$@".
run_as_fake_root() {
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; shift; "$@"' _ \
        "$SUITE/lib.sh" "$@"
}

@test "guard: a root tree passes and defines physical SCRIPT_DIR and REPO_ROOT" {
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; echo "$SCRIPT_DIR|$REPO_ROOT"' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [ "$output" = "$SUITE|$SRC" ]
}

@test "guard: lib.sh owned by APP_USER aborts naming path, owner and mode" {
    echo "$SUITE/lib.sh" > "$SOURCE_GUARD_FOREIGN"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/lib.sh has owner uid 1001 and mode -rw-r--r--"* ]]
    [[ "$output" == *"/root/orcaster"* ]]
}

@test "guard: numbered script with group write aborts" {
    chmod g+w "$SUITE/03-app-install.sh"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/03-app-install.sh has owner uid 0 and mode -rw-rw-r--"* ]] \
        || [[ "$output" == *"$SUITE/03-app-install.sh has owner uid 0 and mode -rwxrwxr-x"* ]]
}

@test "guard: run-all.sh with other write aborts" {
    chmod o+w "$SUITE/run-all.sh"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/run-all.sh has owner uid 0"* ]]
}

@test "guard: Makefile owned by APP_USER aborts" {
    echo "$SRC/Makefile" > "$SOURCE_GUARD_FOREIGN"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SRC/Makefile has owner uid 1001"* ]]
}

@test "guard: ancestor directory with group write aborts" {
    chmod g+w "$SRC/setup-scripts"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SRC/setup-scripts has owner uid 0 and mode drwxrwxr-x"* ]]
}

@test "guard: REPO_ROOT owned by APP_USER aborts" {
    echo "$SRC" > "$SOURCE_GUARD_FOREIGN"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SRC has owner uid 1001"* ]]
}

@test "guard: a script that is a symbolic link aborts" {
    mv "$SUITE/04-migrate.sh" "$T/04-migrate.sh"
    ln -s "$T/04-migrate.sh" "$SUITE/04-migrate.sh"
    run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/04-migrate.sh is not a regular file"* ]]
}

@test "guard: invoking through a path with symbolic links aborts" {
    ln -s "$SRC" "$T/via-link"
    run env PATH="$STUB:$PATH" bash -c 'source "$1"' _ "$T/via-link/setup-scripts/debian13/lib.sh"
    [ "$status" -eq 1 ]
    [[ "$output" == *"goes through symbolic links"* ]]
}

@test "guard: setup.env owned by APP_USER aborts before sourcing it" {
    cp "$T/setup.env" "$SUITE/setup.env"
    echo 'touch "$SENTINEL"' >> "$SUITE/setup.env"
    echo "$SUITE/setup.env" > "$SOURCE_GUARD_FOREIGN"
    SENTINEL="$T/sourced" run_as_fake_root true
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SUITE/setup.env has owner uid 1001"* ]]
    [ ! -e "$T/sourced" ]
}

@test "guard: ENV_FILE_OVERRIDE with group write aborts in load_env" {
    mkdir -p "$SRC/conf"
    cp "$T/setup.env" "$SRC/conf/setup.env"
    chmod 664 "$SRC/conf/setup.env"
    ENV_FILE_OVERRIDE="$SRC/conf/setup.env" run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"$SRC/conf/setup.env has owner uid 0 and mode -rw-rw-r--"* ]]
}

@test "guard: REPO_ROOT equal to INSTALL_DIR aborts" {
    cp "$T/setup.env" "$SUITE/setup.env"
    printf 'INSTALL_DIR="%s"\n' "$SRC" >> "$SUITE/setup.env"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"is inside INSTALL_DIR ($SRC)"* ]]
}

@test "guard: REPO_ROOT inside INSTALL_DIR aborts (/opt layout)" {
    cp "$T/setup.env" "$SUITE/setup.env"
    printf 'INSTALL_DIR="%s"\n' "$T" >> "$SUITE/setup.env"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"is inside INSTALL_DIR"* ]]
}

@test "guard: INSTALL_DIR inside the checkout aborts, even if it does not exist yet" {
    cp "$T/setup.env" "$SUITE/setup.env"
    printf 'INSTALL_DIR="%s"\n' "$SRC/deploy/new" >> "$SUITE/setup.env"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"INSTALL_DIR ($SRC/deploy/new) is inside the checkout"* ]]
}

@test "guard: INSTALL_DIR that reaches the checkout through a link aborts" {
    ln -s "$SRC" "$T/alias"
    cp "$T/setup.env" "$SUITE/setup.env"
    printf 'INSTALL_DIR="%s"\n' "$T/alias" >> "$SUITE/setup.env"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"is inside INSTALL_DIR ($T/alias)"* ]]
}

@test "guard: a root tree separate from INSTALL_DIR loads setup.env" {
    cp "$T/setup.env" "$SUITE/setup.env"
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; load_env; echo "install=$INSTALL_DIR"' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"install=$T/install"* ]]
}

@test "guard: without root it does nothing (08 and the tests run without root)" {
    echo "$SUITE/lib.sh" > "$SOURCE_GUARD_FOREIGN"
    rm "$STUB/id"
    run env PATH="$STUB:$PATH" bash -c 'source "$1"; echo sourced' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [ "$output" = "sourced" ]
}

@test "guard: aborts before any side effect in 00, 03 and run-all" {
    cp "$T/setup.env" "$SUITE/setup.env"
    echo "$SUITE/lib.sh" > "$SOURCE_GUARD_FOREIGN"
    local s
    for s in 00-preflight.sh 03-app-install.sh run-all.sh; do
        run env PATH="$STUB:$PATH" bash "$SUITE/$s"
        [ "$status" -eq 1 ]
        [[ "$output" == *"Source guard"* ]]
    done
    [ ! -e "$T/effects" ]
}

@test "guard: with an overlapping INSTALL_DIR it aborts before any side effect" {
    cp "$T/setup.env" "$SUITE/setup.env"
    printf 'INSTALL_DIR="%s"\n' "$SRC" >> "$SUITE/setup.env"
    local s
    # 00 is left out: require_debian13 reads /etc/os-release before load_env.
    for s in 03-app-install.sh 05-systemd.sh run-all.sh; do
        run env PATH="$STUB:$PATH" bash "$SUITE/$s"
        [ "$status" -eq 1 ]
        [[ "$output" == *"Source guard"* ]]
    done
    [ ! -e "$T/effects" ]
}

@test "guard: INSTALL_DIR with dot-dot components aborts" {
    cp "$T/setup.env" "$SUITE/setup.env"
    printf 'INSTALL_DIR="%s"\n' "$T/nothing/../src" >> "$SUITE/setup.env"
    run_as_fake_root load_env
    [ "$status" -eq 1 ]
    [[ "$output" == *"cannot have '.' or '..' components"* ]]
}

@test "guard: load_env sources the verified physical path of setup.env" {
    mkdir -p "$SRC/conf"
    cp "$T/setup.env" "$SRC/conf/setup.env"
    ln -s "$SRC/conf" "$T/conf-link"
    ENV_FILE_OVERRIDE="$T/conf-link/setup.env" run env PATH="$STUB:$PATH" \
        bash -c 'source "$1"; set -x; load_env' _ "$SUITE/lib.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *". $SRC/conf/setup.env"* ]]
}
