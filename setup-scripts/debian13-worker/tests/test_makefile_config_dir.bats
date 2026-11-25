#!/usr/bin/env bats
# Guard: the Makefile's CONFIG_DIR <-> ETC_SETUP_ENV_FILE derivation is
# one-directional, and a manual `make up client CONFIG_DIR=...` (without
# ETC_SETUP_ENV_FILE=) silently desynchronized the autostart's memory cap. The
# guard fails LOUDLY BEFORE creating anything, but ONLY in the bad state, without
# bothering any sanctioned path. It is tested with `make -n` because the guard
# is a parse-phase `$(error)`: it fires in a dry run, without touching podman or
# creating any container.
#
# Bats gotcha of this repo (see test_run_fleet.bats): a `[[ ... ]]` that is not
# the last statement of a @test does NOT fail the test. That is why content
# assertions go through these helpers, where the `[[ ]]` is the last statement
# of the FUNCTION and the `return 1` does gate.
assert_out() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

refute_out() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected '$2' in the output:"; echo "$1"; return 1; }
}

setup() {
    # test_makefile_config_dir.bats lives in setup-scripts/debian13-worker/tests/,
    # so the repo root (where the Makefile lives) is three levels up.
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # Do not inherit a CONFIG_DIR from the runner's environment: it would change
    # the origin and falsify the command-line cases.
    unset CONFIG_DIR
    unset ETC_SETUP_ENV_FILE
}

@test "makefile: up client with a command-line CONFIG_DIR and ETC at its default fails" {
    run make -C "$REPO_ROOT" -n up client CONFIG_DIR=/srv/orcaster
    [ "$status" -ne 0 ]
    assert_out "$output" "ETC_SETUP_ENV_FILE is still at its default"
    assert_out "$output" "ETC_SETUP_ENV_FILE=/srv/orcaster/setup.env"
}

@test "makefile: passing both variables together does not fire (install-release path)" {
    run make -C "$REPO_ROOT" -n up client CONFIG_DIR=/srv/orcaster ETC_SETUP_ENV_FILE=/srv/orcaster/setup.env
    [ "$status" -eq 0 ]
    refute_out "$output" "ETC_SETUP_ENV_FILE is still at its default"
}

@test "makefile: a command-line CONFIG_DIR equal to the ETC dir does not fire" {
    run make -C "$REPO_ROOT" -n up client CONFIG_DIR=/etc/orcaster
    [ "$status" -eq 0 ]
    refute_out "$output" "ETC_SETUP_ENV_FILE is still at its default"
}

@test "makefile: CONFIG_DIR from the environment does not fire (cloud-init/run-fleet path)" {
    export CONFIG_DIR=/srv/orcaster
    run make -C "$REPO_ROOT" -n up client
    unset CONFIG_DIR
    [ "$status" -eq 0 ]
    refute_out "$output" "ETC_SETUP_ENV_FILE is still at its default"
}

@test "makefile: a target that is not up is not blocked by the mismatch" {
    run make -C "$REPO_ROOT" -n help CONFIG_DIR=/srv/orcaster
    [ "$status" -eq 0 ]
    refute_out "$output" "ETC_SETUP_ENV_FILE is still at its default"
}

@test "makefile: the direct form up-client also fires the guard" {
    run make -C "$REPO_ROOT" -n up-client CONFIG_DIR=/srv/orcaster
    [ "$status" -ne 0 ]
    assert_out "$output" "ETC_SETUP_ENV_FILE is still at its default"
    assert_out "$output" "ETC_SETUP_ENV_FILE=/srv/orcaster/setup.env"
}

@test "makefile: up server with CONFIG_DIR does not fire (the guard is client-only)" {
    run make -C "$REPO_ROOT" -n up server CONFIG_DIR=/srv/orcaster
    [ "$status" -eq 0 ]
    refute_out "$output" "ETC_SETUP_ENV_FILE is still at its default"
}

@test "makefile: a trailing slash in CONFIG_DIR does not trigger a false positive" {
    run make -C "$REPO_ROOT" -n up client CONFIG_DIR=/etc/orcaster/
    [ "$status" -eq 0 ]
    refute_out "$output" "ETC_SETUP_ENV_FILE is still at its default"
}
