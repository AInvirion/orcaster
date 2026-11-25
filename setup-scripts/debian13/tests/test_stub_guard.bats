#!/usr/bin/env bats
# Tests for stub_guard.bash (assert_stubbed): the guard that keeps a suite with
# fake ssh/scp/rsync from reaching the real system binary.
# Run:  bats setup-scripts/debian13/tests/
#
# NOTE (bats-core 1.13 gotcha): @test names must be plain ASCII.

bats_require_minimum_version 1.5.0

load stub_guard

setup() {
    D="$BATS_TEST_TMPDIR/stubs"
    mkdir -p "$D"
}

@test "stub_guard: an executable stub prepended to PATH passes" {
    printf '#!/bin/sh\nexit 0\n' > "$D/ssh"
    chmod +x "$D/ssh"
    PATH="$D:$PATH" assert_stubbed "$D" ssh
}

@test "stub_guard: a stub without +x fails and names the real ssh" {
    command -v ssh || skip "no real ssh in PATH"
    printf '#!/bin/sh\nexit 0\n' > "$D/ssh"
    chmod -x "$D/ssh"
    real="$(command -v ssh)"
    PATH="$D:$PATH" run -1 assert_stubbed "$D" ssh
    [[ "$output" == *"does not exist or is not executable"* ]] || return 1
    [[ "$output" == *"'ssh' resolves to '$real'"* ]] || return 1
}

@test "stub_guard: a missing stub fails" {
    run -1 assert_stubbed "$D" scp
    [[ "$output" == *"$D/scp"* ]] || return 1
}

@test "stub_guard: a stubs directory outside PATH fails" {
    printf '#!/bin/sh\nexit 0\n' > "$D/rsync"
    chmod +x "$D/rsync"
    run -1 assert_stubbed "$D" rsync
    [[ "$output" == *"not to $D/rsync"* ]] || return 1
}

@test "stub_guard: accepts the directory through a symbolic link" {
    printf '#!/bin/sh\nexit 0\n' > "$D/ssh"
    chmod +x "$D/ssh"
    ln -s "$D" "$BATS_TEST_TMPDIR/link"
    PATH="$BATS_TEST_TMPDIR/link:$PATH" assert_stubbed "$D" ssh
}

@test "stub_guard: checks every command and fails if just one is wrong" {
    printf '#!/bin/sh\nexit 0\n' > "$D/ssh"
    chmod +x "$D/ssh"
    PATH="$D:$PATH" run -1 assert_stubbed "$D" ssh scp
    [[ "$output" == *"$D/scp"* ]] || return 1
}
