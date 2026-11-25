#!/usr/bin/env bats
# `make up client` resolves the worker's env file and pubkey from the SAME path
# as run-fleet.sh and install-release.sh ($CONFIG_DIR, /etc/orcaster), with a
# development fallback to client/ only if there is no host config, and warns on
# stderr. It is tested with `make -n` (paths that reach podman) and with the real
# .worker-config-check target (errors and warnings), without touching podman.
#
# Bats gotcha of this repo: a `[[ ]]` that is not the last statement of a @test
# does not fail the test; assertions go through these helpers.
assert_out() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

refute_out() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected '$2' in the output:"; echo "$1"; return 1; }
}

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    unset CONFIG_DIR ETC_SETUP_ENV_FILE WORKER_ENV WORKER_PUBKEY N
    T="$(mktemp -d)"
    HOST="$T/etc-orcaster"
    DEV="$T/client"
    mkdir -p "$HOST" "$DEV"
    # CONFIG_DIR and ETC_SETUP_ENV_FILE go together (CONFIG_DIR guard).
    HOSTARGS=(CONFIG_DIR="$HOST" ETC_SETUP_ENV_FILE="$HOST/setup.env" CLIENT_DIR="$DEV")
}

teardown() {
    chmod -R u+rwx "$T" 2>/dev/null || true
    rm -rf "$T"
}

mk() { make -C "$REPO_ROOT" "$@" 2>&1; }

# --- (a) host with config: CONFIG_DIR wins, no overrides -------------------

@test "worker-config: host with config, up client without overrides uses env and pubkey from CONFIG_DIR" {
    : > "$HOST/env.1"; : > "$HOST/server-ed25519.pub"
    run mk -n up client N=1 "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $HOST/env.1"
    assert_out "$output" "-v $HOST/server-ed25519.pub:/var/lib/orcaster/ed25519.pub"
    refute_out "$output" "--env-file $DEV"
}

@test "worker-config: without N the host env is 'env' (not '.env')" {
    : > "$HOST/env"; : > "$HOST/server-ed25519.pub"
    run mk -n up client "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $HOST/env "
}

@test "worker-config: ORCH_HOST comes from the same env file as --env-file" {
    printf 'ORCH_BASE_URL=https://orch.example:8443/x\n' > "$HOST/env.1"
    : > "$HOST/server-ed25519.pub"
    run mk -n up client N=1 EGRESS_ISOLATION=1 "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $HOST/env.1"
    assert_out "$output" "orch.example"
}

@test "worker-config: CONFIG_DIR with a trailing slash resolves the same" {
    : > "$HOST/env.1"; : > "$HOST/server-ed25519.pub"
    run mk -n up client N=1 CONFIG_DIR="$HOST/" ETC_SETUP_ENV_FILE="$HOST/setup.env" CLIENT_DIR="$DEV"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $HOST/env.1"
}

@test "worker-config: host with config does not warn about the fallback" {
    : > "$HOST/env.1"; : > "$HOST/server-ed25519.pub"
    run mk .worker-config-check N=1 "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    refute_out "$output" "WARNING"
}

@test "worker-config: host with config but a missing env.N fails naming CONFIG_DIR, without falling back to client/" {
    : > "$HOST/env.1"; : > "$HOST/server-ed25519.pub"
    : > "$DEV/.env.2"
    run mk .worker-config-check N=2 "${HOSTARGS[@]}"
    [ "$status" -ne 0 ]
    assert_out "$output" "$HOST/env.2 is missing"
    refute_out "$output" "fallback"
}

@test "worker-config: host with env but no pubkey fails naming the CONFIG_DIR pubkey" {
    : > "$HOST/env.1"
    : > "$DEV/server-ed25519.pub"
    run mk .worker-config-check N=1 "${HOSTARGS[@]}"
    [ "$status" -ne 0 ]
    assert_out "$output" "$HOST/server-ed25519.pub is missing"
}

@test "worker-config: setup.env alone does not count as host config" {
    : > "$HOST/setup.env"; : > "$DEV/.env"; : > "$DEV/server-ed25519.pub"
    run mk .worker-config-check "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "development fallback"
}

# --- (b) development checkout: no host config -------------------------------

@test "worker-config: without host config it falls back to client/.env and client/server-ed25519.pub" {
    : > "$DEV/.env"; : > "$DEV/server-ed25519.pub"
    run mk -n up client "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $DEV/.env"
    assert_out "$output" "-v $DEV/server-ed25519.pub:"
}

@test "worker-config: the fallback uses client/.env.N with N" {
    : > "$DEV/.env.2"; : > "$DEV/server-ed25519.pub"
    run mk -n up client N=2 "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $DEV/.env.2"
}

@test "worker-config: the fallback prints the paths it used to stderr" {
    : > "$DEV/.env"; : > "$DEV/server-ed25519.pub"
    run bash -c 'make -C "$0" .worker-config-check "${@:1}" 2>&1 >/dev/null' "$REPO_ROOT" "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "WARNING"
    assert_out "$output" "$DEV/.env"
    assert_out "$output" "$DEV/server-ed25519.pub"
}

@test "worker-config: without host config and without client/.env the error asks to copy .env.example" {
    run mk .worker-config-check "${HOSTARGS[@]}"
    [ "$status" -ne 0 ]
    assert_out "$output" "$DEV/.env is missing"
    assert_out "$output" ".env.example"
}

# --- (c) overrides (run-fleet.sh / install-release.sh) -----------------------

@test "worker-config: overrides win even with host and client/ config" {
    : > "$HOST/env.1"; : > "$HOST/server-ed25519.pub"; : > "$DEV/.env.1"
    : > "$T/o.env"; : > "$T/o.pub"
    run mk -n up client N=1 WORKER_ENV="$T/o.env" WORKER_PUBKEY="$T/o.pub" "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $T/o.env"
    assert_out "$output" "-v $T/o.pub:"
}

@test "worker-config: an override from the environment also wins" {
    : > "$HOST/env.1"; : > "$HOST/server-ed25519.pub"; : > "$T/o.env"
    WORKER_ENV="$T/o.env" run mk -n up client N=1 "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $T/o.env"
}

@test "worker-config: with both overrides there is no fallback warning even without host config" {
    : > "$T/o.env"; : > "$T/o.pub"
    run mk .worker-config-check WORKER_ENV="$T/o.env" WORKER_PUBKEY="$T/o.pub" "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    refute_out "$output" "WARNING"
}

@test "worker-config: an override pointing at a missing file fails naming it" {
    : > "$T/o.pub"
    run mk .worker-config-check WORKER_ENV="$T/nada.env" WORKER_PUBKEY="$T/o.pub" "${HOSTARGS[@]}"
    [ "$status" -ne 0 ]
    assert_out "$output" "$T/nada.env is missing"
}

# --- unreadable file: explicit error, never a silent default ------------------

@test "worker-config: an unreadable host env fails explicitly" {
    [ "$(id -u)" -ne 0 ] || skip "root reads everything"
    : > "$HOST/env.1"; : > "$HOST/server-ed25519.pub"; chmod 000 "$HOST/env.1"
    : > "$DEV/.env.1"
    run mk .worker-config-check N=1 "${HOSTARGS[@]}"
    [ "$status" -ne 0 ]
    assert_out "$output" "$HOST/env.1 exists but is not a file readable"
}

@test "worker-config: an unreadable host pubkey fails explicitly" {
    [ "$(id -u)" -ne 0 ] || skip "root reads everything"
    : > "$HOST/env.1"; : > "$HOST/server-ed25519.pub"; chmod 000 "$HOST/server-ed25519.pub"
    run mk .worker-config-check N=1 "${HOSTARGS[@]}"
    [ "$status" -ne 0 ]
    assert_out "$output" "$HOST/server-ed25519.pub exists but is not a file readable"
}

@test "worker-config: an unreadable client/ env in fallback fails explicitly" {
    [ "$(id -u)" -ne 0 ] || skip "root reads everything"
    : > "$DEV/.env"; : > "$DEV/server-ed25519.pub"; chmod 000 "$DEV/.env"
    run mk .worker-config-check "${HOSTARGS[@]}"
    [ "$status" -ne 0 ]
    assert_out "$output" "$DEV/.env exists but is not a file readable"
}

@test "worker-config: a directory in place of the env does not pass either" {
    mkdir "$HOST/env.1"; : > "$HOST/server-ed25519.pub"
    run mk .worker-config-check N=1 "${HOSTARGS[@]}"
    [ "$status" -ne 0 ]
    assert_out "$output" "is not a file readable"
}

# --- keygen-server keeps its default (writes $(WORKER_PUBKEY)) ---------------

@test "worker-config: keygen-server writes the pubkey to the resolved path (client/ without host config)" {
    run mk -n keygen-server "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "-pubout -out $DEV/server-ed25519.pub"
}

@test "worker-config: keygen-server writes to CONFIG_DIR if there is host config" {
    : > "$HOST/env.1"
    run mk -n keygen-server "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "-pubout -out $HOST/server-ed25519.pub"
}

# --- a single path definition: Makefile == lib.sh == what the scripts pass --------

@test "worker-config: the Makefile without override resolves the same as lib.sh worker_envfile and CLIENT_PUBKEY" {
    : > "$HOST/env.3"; : > "$HOST/server-ed25519.pub"
    (
        source "$REPO_ROOT/setup-scripts/debian13-worker/lib.sh"
        CONFIG_DIR="$HOST"
        want_env="$(worker_envfile 3 "$CONFIG_DIR")"
        run mk -n up client N=3 "${HOSTARGS[@]}"
        assert_out "$output" "--env-file $want_env"
        assert_out "$output" "-v $CONFIG_DIR/server-ed25519.pub:"
    )
}

@test "worker-config: an explicit WORKER_ENV/WORKER_PUBKEY override resolves the same as the default" {
    : > "$HOST/env.3"; : > "$HOST/server-ed25519.pub"
    run mk -n up client N=3 "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    without_override="$(printf '%s\n' "$output" | grep -E -- '--env-file|ed25519.pub:/var')"
    run mk -n up client N=3 "${HOSTARGS[@]}" WORKER_ENV="$HOST/env.3" WORKER_PUBKEY="$HOST/server-ed25519.pub"
    [ "$status" -eq 0 ]
    with_override="$(printf '%s\n' "$output" | grep -E -- '--env-file|ed25519.pub:/var')"
    [ "$without_override" = "$with_override" ]
}

# --- the scripts do not pass WORKER_ENV/WORKER_PUBKEY ------------------------
# run-fleet.sh and install-release.sh call `make up client N=i` with
# CONFIG_DIR + ETC_SETUP_ENV_FILE and whatever worker_config_make_args prints.
# Against this repo's Makefile that is nothing: the Makefile is the single
# definition of the two paths. Only an older Makefile without that block (rollback)
# gets the overrides back.

@test "worker-config: worker_config_make_args prints no override for this repo's Makefile" {
    run bash -c 'source "$0"; worker_config_make_args "$1" 3 "$2"' \
        "$REPO_ROOT/setup-scripts/debian13-worker/lib.sh" "$REPO_ROOT" "$HOST"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "worker-config: worker_config_make_args prints both overrides for an older Makefile without WORKER_ENV_HOST" {
    mkdir -p "$T/old"
    printf 'WORKER_ENV ?= $(abspath $(CLIENT_DIR))/.env\n' > "$T/old/Makefile"
    run bash -c 'source "$0"; worker_config_make_args "$1" 3 "$2"' \
        "$REPO_ROOT/setup-scripts/debian13-worker/lib.sh" "$T/old" "$HOST"
    [ "$status" -eq 0 ]
    [ "$output" = "WORKER_ENV=$HOST/env.3
WORKER_PUBKEY=$HOST/server-ed25519.pub" ]
}

@test "worker-config: the make line the scripts now send resolves env.N and the pubkey from CONFIG_DIR" {
    : > "$HOST/env.3"; : > "$HOST/server-ed25519.pub"
    # Same variables as run-fleet.sh / install-release.sh, minus podman-only
    # knobs; no WORKER_ENV, no WORKER_PUBKEY.
    run mk -n up client N=3 AUTOSTART_SYNC=0 "${HOSTARGS[@]}"
    [ "$status" -eq 0 ]
    assert_out "$output" "--env-file $HOST/env.3 "
    assert_out "$output" "-v $HOST/server-ed25519.pub:/var/lib/orcaster/ed25519.pub"
    # `make -n` prints the recipe text, so the source is checked instead of
    # the warning: host for both files, never the checkout.
    assert_out "$output" 'if [ "host" = dev ] || [ "host" = dev ]'
    refute_out "$output" "--env-file $DEV"
}

@test "worker-config: install-release.sh stubs match lib.sh (worker_envfile, worker_config_make_args)" {
    fn() { sed -n "/^$1() {\$/,/^}\$/p" "$2"; }
    for f in worker_envfile worker_config_make_args; do
        lib="$(fn "$f" "$REPO_ROOT/setup-scripts/debian13-worker/lib.sh")"
        inst="$(fn "$f" "$REPO_ROOT/ops/release/install-release.sh")"
        [ -n "$lib" ]
        [ "$lib" = "$inst" ] || { echo "$f differs between lib.sh and install-release.sh"; return 1; }
    done
}

# --- printed instructions: they no longer ask for WORKER_ENV/WORKER_PUBKEY ----

@test "worker-config: 05-smoke-test.sh and run-all-worker.sh do not print WORKER_ENV=/WORKER_PUBKEY=" {
    run grep -n 'WORKER_ENV=\|WORKER_PUBKEY=' \
        "$REPO_ROOT/setup-scripts/debian13-worker/05-smoke-test.sh" \
        "$REPO_ROOT/setup-scripts/debian13-worker/run-all-worker.sh"
    [ "$status" -eq 1 ]
}

@test "worker-config: lib.sh MAKE_CFG is empty with the default CONFIG_DIR, otherwise CONFIG_DIR+ETC_SETUP_ENV_FILE" {
    run bash -c 'unset CONFIG_DIR; source "$0"; printf "[%s]" "$MAKE_CFG"' "$REPO_ROOT/setup-scripts/debian13-worker/lib.sh"
    [ "$status" -eq 0 ]
    [ "$output" = "[]" ]
    run bash -c 'CONFIG_DIR=/srv/tk source "$0"; printf "%s" "$MAKE_CFG"' "$REPO_ROOT/setup-scripts/debian13-worker/lib.sh"
    [ "$status" -eq 0 ]
    [ "$output" = " CONFIG_DIR=/srv/tk ETC_SETUP_ENV_FILE=/srv/tk/setup.env" ]
}
