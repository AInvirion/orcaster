#!/usr/bin/env bats
# Integration of 02-server-pubkey.sh (and of the pubkey check in
# 05-smoke-test.sh) with id/install/chown/sudo/curl mocked, without root.
#
# The focus is the "file present but with invalid content" state: any host
# provisioned by a version prior to the validation leaves it, and so does step 2
# of the migration runbook, which copies client/server-ed25519.pub by hand
# without validating anything. With the worker running, that state goes
# unnoticed: the worker silently rejects EVERY signed task. Both scripts have to
# catch it before getting there.
#
# The keys are real ed25519 (openssl genpkey), not fabricated PEM blocks: the
# check under test is precisely openssl's parsing.
#
# A bats trap that applies to this WHOLE file: a `[[ ... ]]` that is not the
# LAST statement of a @test does NOT fail the test. Verified on bats 1.14 with
# macOS's bash 3.2 (`bash -c 'set -eE; trap "echo T" ERR; [[ 1 == 2 ]]; echo
# REACHED'` prints REACHED and exits 0): neither errexit nor the ERR trap fires
# for a `[[ ]]` that returns 1. It is the twin gotcha of the `! grep` the suite
# already documents, which is why content assertions go through these two
# helpers, where the `[[ ]]` IS the last statement of the function and the
# `return 1` gates the test.
assert_out() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

refute_out() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected '$2' in the output:"; echo "$1"; return 1; }
}

# File mode, GNU first and BSD after. The order matters: GNU coreutils' `stat -f`
# interprets the format as a FILE NAME, fails with rc=1 and still prints the
# filesystem block to stdout, so the form `stat -f ... || stat -c ...`
# concatenates both outputs and compares nothing. BSD's `stat -c`, on the other
# hand, writes nothing to stdout when it rejects the option.
file_mode() {
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

# The fake `id -u` returns 0, so lib.sh's origin guard acts: stub of `ls -lnd`
# that presents the tree as root-owned.
load ../../debian13/tests/source_guard_stub

setup() {
    PUBKEY_SCRIPT="${BATS_TEST_DIRNAME}/../02-server-pubkey.sh"
    SMOKE_SCRIPT="${BATS_TEST_DIRNAME}/../05-smoke-test.sh"
    T="$(mktemp -d)"
    export INSTALL_DIR="$T/repo"
    # Host config dir: the pubkey no longer lives in the checkout.
    export CONFIG_DIR="$T/etc"
    mkdir -p "$CONFIG_DIR"
    PUBKEY_FILE="$CONFIG_DIR/server-ed25519.pub"

    # Real ed25519 key pair. GOOD_PUB is the good one; OTHER_PUB serves to
    # distinguish "repaired with the one from setup.env" from "the one that was
    # already there stayed".
    openssl genpkey -algorithm ed25519 -out "$T/good.pem" 2>/dev/null
    openssl pkey -in "$T/good.pem" -pubout -out "$T/good.pub" 2>/dev/null
    GOOD_PUB="$T/good.pub"
    openssl genpkey -algorithm ed25519 -out "$T/other.pem" 2>/dev/null
    openssl pkey -in "$T/other.pem" -pubout -out "$T/other.pub" 2>/dev/null
    OTHER_PUB="$T/other.pub"

    export ENV_FILE_OVERRIDE="$T/setup.env"
    echo 'ORCH_BASE_URL=https://orch.example.com' > "$ENV_FILE_OVERRIDE"

    # Mocks in PATH: id (simulates root; with a user it returns a fixed uid),
    # install (-d => mkdir), chown (noop: bats does not run with privileges),
    # sudo (noop: 05's as_worker needs no real podman), curl (the smoke test's
    # healthz must not go out to the network).
    mkdir -p "$T/bin"
    cat > "$T/bin/id" <<'M'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ] && [ -z "${2:-}" ]; then echo 0; exit 0; fi
if [ "${1:-}" = "-u" ]; then echo 1001; exit 0; fi
exit 0
M
    cat > "$T/bin/install" <<'M'
#!/usr/bin/env bash
for last in "$@"; do :; done
mkdir -p "$last"
M
    cat > "$T/bin/chown" <<'M'
#!/usr/bin/env bash
exit 0
M
    cat > "$T/bin/sudo" <<'M'
#!/usr/bin/env bash
exit 0
M
    cat > "$T/bin/curl" <<'M'
#!/usr/bin/env bash
exit 0
M
    chmod +x "$T/bin/id" "$T/bin/install" "$T/bin/chown" "$T/bin/sudo" "$T/bin/curl"
    write_ls_stub "$T/bin"
    export PATH="$T/bin:$PATH"
}

teardown() { rm -rf "$T"; }

# Declares SERVER_ED25519_PUBKEY in the test setup.env with the content of the
# PEM file passed in. Single quotes: the block is multiline and setup.env is
# sourced. The command substitution eats the trailing newline, just like an
# operator pasting the block into the editor; this way the file the script
# writes ends up byte for byte equal to the original PEM.
declare_pubkey_in_setup_env() {
    local pem
    pem="$(cat "$1")"
    printf "SERVER_ED25519_PUBKEY='%s'\n" "$pem" >> "$ENV_FILE_OVERRIDE"
}

@test "02-server-pubkey: an already present, valid pubkey is left untouched" {
    cp "$GOOD_PUB" "$PUBKEY_FILE"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "content unchanged"
    cmp -s "$GOOD_PUB" "$PUBKEY_FILE"
}

@test "02-server-pubkey: a present but truncated pubkey is repaired from setup.env" {
    head -2 "$GOOD_PUB" > "$PUBKEY_FILE"
    declare_pubkey_in_setup_env "$GOOD_PUB"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    cmp -s "$GOOD_PUB" "$PUBKEY_FILE"
}

@test "02-server-pubkey: a present pubkey with arbitrary text is repaired from setup.env" {
    echo 'this is not a key' > "$PUBKEY_FILE"
    declare_pubkey_in_setup_env "$GOOD_PUB"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    # ASCII-only literal on purpose: bash 3.2 (macOS) matching with multibyte
    # patterns is not reliable, and an assertion that asserts nothing is worse
    # than none.
    assert_out "$output" "is replaced"
    cmp -s "$GOOD_PUB" "$PUBKEY_FILE"
}

@test "02-server-pubkey: a present, invalid pubkey with no replacement in setup.env aborts" {
    echo 'this is not a key' > "$PUBKEY_FILE"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -ne 0 ]
    assert_out "$output" "$PUBKEY_FILE"
    assert_out "$output" "SERVER_ED25519_PUBKEY"
}

@test "02-server-pubkey: a present, invalid pubkey with an invalid replacement aborts" {
    echo 'this is not a key' > "$PUBKEY_FILE"
    printf "SERVER_ED25519_PUBKEY='neither is this'\n" >> "$ENV_FILE_OVERRIDE"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -ne 0 ]
    assert_out "$output" "SERVER_ED25519_PUBKEY"
}

@test "02-server-pubkey: with no file and a valid pubkey in setup.env it writes it" {
    declare_pubkey_in_setup_env "$GOOD_PUB"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    cmp -s "$GOOD_PUB" "$PUBKEY_FILE"
}

@test "02-server-pubkey: with no file and no pubkey in setup.env it aborts offering both ways out" {
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -ne 0 ]
    assert_out "$output" "SERVER_ED25519_PUBKEY"
    assert_out "$output" "$PUBKEY_FILE"
}

@test "02-server-pubkey: a rerun after writing does not touch the file again" {
    declare_pubkey_in_setup_env "$GOOD_PUB"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "content unchanged"
    cmp -s "$GOOD_PUB" "$PUBKEY_FILE"
}

@test "02-server-pubkey: a valid pubkey from another server is replaced with the declared one" {
    # The "already present and valid" branch validated FORM and not IDENTITY, so
    # an old well-formed pubkey won forever: rotating the server key this way was
    # impossible, and a golden snapshot baked against staging left the whole
    # fleet enrolling fine and silently rejecting every signed task.
    cp "$OTHER_PUB" "$PUBKEY_FILE"
    declare_pubkey_in_setup_env "$GOOD_PUB"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "is NOT the one declared"
    cmp -s "$GOOD_PUB" "$PUBKEY_FILE"
}

@test "02-server-pubkey: with an unreadable SERVER_ED25519_PUBKEY the valid file is kept and a warning is given" {
    # Do not degrade a host that works today: the FILE's key works, so aborting
    # would leave it without a worker. But do not stay silent either.
    cp "$GOOD_PUB" "$PUBKEY_FILE"
    printf "SERVER_ED25519_PUBKEY='is not a key'\n" >> "$ENV_FILE_OVERRIDE"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "cannot confirm"
    cmp -s "$GOOD_PUB" "$PUBKEY_FILE"
}

@test "02-server-pubkey: the unchanged branch reconciles the mode to 0644" {
    # Mirror of "00-preflight: the setup.env reconciliation leaves the mode at
    # 0640". A 0600 root:root file (what a manual cp under umask 077 leaves)
    # cannot be read by the worker, which silently rejects every signed task
    # while this script reported "content unchanged" in green.
    cp "$GOOD_PUB" "$PUBKEY_FILE"
    chmod 0600 "$PUBKEY_FILE"
    run bash "$PUBKEY_SCRIPT"
    [ "$status" -eq 0 ]
    perm="$(file_mode "$PUBKEY_FILE")"
    [ "$perm" = "644" ]
    cmp -s "$GOOD_PUB" "$PUBKEY_FILE"
}

@test "05-smoke-test: a present, valid pubkey passes the artifact check" {
    cp "$GOOD_PUB" "$PUBKEY_FILE"
    touch "$CONFIG_DIR/env"
    run bash "$SMOKE_SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "Worker provisioned"
}

@test "05-smoke-test: a present but invalid pubkey fails the smoke test" {
    echo 'this is not a key' > "$PUBKEY_FILE"
    touch "$CONFIG_DIR/env"
    run bash "$SMOKE_SCRIPT"
    [ "$status" -ne 0 ]
    assert_out "$output" "$PUBKEY_FILE"
    run grep -F "Worker provisioned" <<< "$output"
    [ "$status" -ne 0 ]
}

@test "05-smoke-test: a missing pubkey still fails with the missing-file message" {
    touch "$CONFIG_DIR/env"
    run bash "$SMOKE_SCRIPT"
    [ "$status" -ne 0 ]
    assert_out "$output" "Missing $PUBKEY_FILE"
}
