#!/usr/bin/env bats
# Integration of run-fleet.sh with make/autostart/as_worker mocked.
# Verifies: N env files stamped with the right token and name, and one 'up' +
# 'autostart' call per worker.
#
# A `[[ ... ]]` that is not the LAST statement of a @test does NOT fail the
# test: it is the twin gotcha of the `! cmd` that this file already documents
# further down. Verified in bats 1.14 with macOS bash 3.2 (`bash -c 'set -eE;
# trap "echo T" ERR; [[ 1 == 2 ]]; echo REACHED'` prints REACHED and exits 0):
# neither errexit nor the ERR trap fire for a `[[ ]]` that returns 1. That is
# why every content assertion goes through these helpers, where the `[[ ]]` is
# the last statement of the FUNCTION and the `return 1` does gate.
assert_out() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

refute_out() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected '$2' in the output:"; echo "$1"; return 1; }
}

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../run-fleet.sh"
    T="$(mktemp -d)"
    export INSTALL_DIR="$T/repo"
    mkdir -p "$INSTALL_DIR/client"
    # Host config dir: the fleet env files no longer live in the checkout. It is
    # exported so lib.sh does not fall back to the real /etc/orcaster.
    export CONFIG_DIR="$T/etc"
    mkdir -p "$CONFIG_DIR"
    CALLS="$T/calls.log"; export CALLS

    # Test setup.env (consumed through ENV_FILE_OVERRIDE by load_env).
    export ENV_FILE_OVERRIDE="$T/setup.env"
    cat > "$ENV_FILE_OVERRIDE" <<EOF
ORCH_BASE_URL=https://orch.example.com
ORCH_ENROLLMENT_TOKEN=tok-fleet
WORKER_COUNT=3
WORKER_NAME_PREFIX=myfleet
EOF

    # make mock in PATH.
    mkdir -p "$T/bin"
    # MAKE_DOWN_FAILS=1: only the prune `down` fails. The `up` keeps working,
    # so we can verify that a half prune does not bring down the deployment.
    cat > "$T/bin/make" <<'M'
#!/usr/bin/env bash
echo "make $*" >> "$CALLS"
if [ "${MAKE_DOWN_FAILS:-0}" = 1 ]; then
    case " $* " in *" down "*) echo "make: down failed" >&2; exit 2 ;; esac
fi
M
    chmod +x "$T/bin/make"

    # systemctl mock: 'is-active <unit>' answers 0 only if the unit is in
    # ACTIVE_UNITS (space-separated list); everything else always 0.
    cat > "$T/bin/systemctl" <<'M'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
# SYSTEMCTL_DISABLE_FAILS=1: only the prune disable fails.
if [ "${SYSTEMCTL_DISABLE_FAILS:-0}" = 1 ]; then
    case " $* " in *" disable "*) echo "Failed to disable unit" >&2; exit 1 ;; esac
fi
# Prune existence probe: `show -p LoadState --value <unit>`, used before each
# disable (the worker's and the egress sidecar's). Without it, on a host where
# that unit was never created the disable would always fail and the warn would
# talk about a container that systemd will relaunch when there is none.
#
# SYSTEMCTL_MISSING_UNITS: units (space-separated) that have no unit file on
# this host, that is LoadState=not-found. Without the variable, the unit
# exists (LoadState=loaded), which is the state of a host with the fleet
# running under isolation.
#
# SYSTEMCTL_BUS_UNREACHABLE=1: the worker's --user bus does not answer
# (as_worker reaches systemd through DBUS_SESSION_BUS_ADDRESS, see lib.sh), so
# the probe cannot know whether the unit exists. It is the case that used to
# skip the sidecar disable silently. It falls ONLY in this branch, so the test
# isolates the probe path from the rest.
if [ "${1:-}" = "--user" ] && [ "${2:-}" = "show" ]; then
    for last in "$@"; do :; done
    if [ "${SYSTEMCTL_BUS_UNREACHABLE:-0}" = 1 ]; then
        echo "Failed to connect to bus: No medium found" >&2
        exit 1
    fi
    case " ${SYSTEMCTL_MISSING_UNITS:-} " in *" $last "*) echo not-found; exit 0 ;; esac
    echo loaded
    exit 0
fi
if [ "${1:-}" = "--user" ] && { [ "${2:-}" = "is-active" ] || [ "${3:-}" = "is-active" ]; }; then
    for last in "$@"; do :; done
    case " ${ACTIVE_UNITS:-} " in *" $last "*) exit 0 ;; esac
    exit 3
fi
M
    chmod +x "$T/bin/systemctl"

    # podman mock: covers every invocation that arrives with an empty
    # RUN_AS_WORKER (this file's seam, see below) and that no more specific mock
    # intercepts first. Without it, a `podman volume rm` added by mistake to the
    # prune loop (further down in this file) would not go through CALLS: it
    # would hit the machine's real podman, which does not fail (or fails
    # silently with `2>/dev/null || true`) and the assertion that guards the
    # worker identity (orcaster-data-N) would pass anyway having seen nothing.
    # The tests that need a specific answer (worker_enrolled, further down)
    # override this file with their own mock inside the @test itself.
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
M
    chmod +x "$T/bin/podman"

    # install/chown/chmod of the config dir and of env.N. No `-o root` or
    # `chown root:` works when the suite runs as a normal user, and before the
    # hardening the seam was the script's own `2>/dev/null || true`: a silence
    # put there by the tests that took the real case along with it (an env.N
    # that the worker cannot read makes the container crash-loop until the
    # step 2/2 timeout, far from the cause). The seam is now this trio of
    # mocks, which records the FULL call and delegates to the real tool without
    # the ownership flags. The *_FAILS simulate the failure that must now
    # abort; the install one fails ONLY the -o/-g form, which is the production
    # condition and the only one that discriminates a fallback.
    cat > "$T/bin/install" <<'M'
#!/usr/bin/env bash
echo "install $*" >> "$CALLS"
if [ "${INSTALL_FAILS:-0}" = 1 ]; then
    case " $* " in *" -o "*) exit 1 ;; esac
fi
args=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o|-g) shift 2 ;;
        *) args+=("$1"); shift ;;
    esac
done
exec /usr/bin/install "${args[@]}"
M
    cat > "$T/bin/chown" <<'M'
#!/usr/bin/env bash
echo "chown $*" >> "$CALLS"
[ "${CHOWN_FAILS:-0}" = 1 ] && exit 1
exit 0
M
    cat > "$T/bin/chmod" <<'M'
#!/usr/bin/env bash
echo "chmod $*" >> "$CALLS"
[ "${CHMOD_FAILS:-0}" = 1 ] && exit 1
exec /bin/chmod "$@"
M
    chmod +x "$T/bin/install" "$T/bin/chown" "$T/bin/chmod"

    # Autostart and as_worker mocks via the script's seams.
    export AUTOSTART_SCRIPT="$T/bin/fake-autostart.sh"
    cat > "$AUTOSTART_SCRIPT" <<'A'
#!/usr/bin/env bash
echo "autostart N=${N:-} CONFIG_DIR=${CONFIG_DIR:-}" >> "$CALLS"
A
    chmod +x "$AUTOSTART_SCRIPT"

    # PATH is exported at the END, after the last `chmod +x` of setup: with the
    # chmod mock in PATH, those setup chmods would enter CALLS and pollute the
    # tests' assertions.
    export PATH="$T/bin:$PATH"

    # as_worker: in tests, runs the command directly (no sudo).
    export RUN_AS_WORKER=""
    # The worker that the script passes to install/chown. It no longer avoids
    # anything real (the mocks take care of that): it is still the machine's
    # user so that a failure message names something that exists.
    export WORKER_USER="$(id -un)"
    # Injected enrollment check (seam): default 'true' so that tests that do
    # not exercise it do not touch the machine's real podman.
    export ENROLL_CHECK=true
}

teardown() {
    rm -rf "$T"
    # Mountable directories for the WORKER_RO_MOUNTS tests.
    if [ -n "${FIX:-}" ]; then
        /bin/chmod -R u+rwx "$FIX" 2>/dev/null || true
        rm -rf "$FIX"
    fi
}

# Mountable directories for WORKER_RO_MOUNTS. They are created INSIDE this
# tests directory: $T falls under /var (macOS) or /tmp (Linux), reserved paths
# that the validator rejects on purpose.
ro_fixture() {
    FIX="$(mktemp -d "${BATS_TEST_DIRNAME}/ro-fixture.XXXXXX")"
    mkdir -p "$FIX/example-ro" "$FIX/snap"
    /bin/chmod 0755 "$FIX/example-ro" "$FIX/snap"
}

@test "run-fleet: stamps WORKER_COUNT env files with token and name" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    for i in 1 2 3; do
        [ -f "$CONFIG_DIR/env.$i" ]
        grep -q '^ORCH_ENROLLMENT_TOKEN=tok-fleet$' "$CONFIG_DIR/env.$i"
        grep -q "^ORCH_WORKER_NAME=myfleet-$i\$" "$CONFIG_DIR/env.$i"
    done
}

@test "run-fleet: rerun keeps the name already stamped in env.N" {
    # worker1 scenario: worker 1 is called `worker1` (stamped by hand or by an
    # earlier version), not PREFIX-1. A rerun does not rename it: the worker
    # sends `name` on every poll and the server overwrites the stored name.
    printf '%s\n' 'ORCH_API_KEY=' 'ORCH_ENROLLMENT_TOKEN=tok-old' \
        'ORCH_WORKER_NAME=worker1' > "$CONFIG_DIR/env.1"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    out="$output"
    grep -q '^ORCH_WORKER_NAME=worker1$' "$CONFIG_DIR/env.1"
    assert_out "$out" "Worker 1/3 (worker1)"
    # Slots without a previous env.N still get PREFIX-i.
    grep -q '^ORCH_WORKER_NAME=myfleet-2$' "$CONFIG_DIR/env.2"
    grep -q '^ORCH_WORKER_NAME=myfleet-3$' "$CONFIG_DIR/env.3"
    # `run` + status: a `!` in the middle of the @test does not gate in bats.
    run grep -q '^ORCH_WORKER_NAME=myfleet-1$' "$CONFIG_DIR/env.1"
    [ "$status" -ne 0 ]
}

@test "run-fleet: env.N with empty ORCH_WORKER_NAME gets PREFIX-i" {
    printf '%s\n' 'ORCH_API_KEY=' 'ORCH_ENROLLMENT_TOKEN=tok-old' \
        'ORCH_WORKER_NAME=' > "$CONFIG_DIR/env.1"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_WORKER_NAME=myfleet-1$' "$CONFIG_DIR/env.1"
}

@test "run-fleet: changing WORKER_NAME_PREFIX does not rename already stamped workers" {
    # Decision: the env.N name also wins over an explicit prefix.
    # Renaming means editing ORCH_WORKER_NAME in env.N (or emptying it) by hand.
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    sed -i.bak 's/^WORKER_NAME_PREFIX=.*/WORKER_NAME_PREFIX=otherfleet/' "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    for i in 1 2 3; do
        grep -q "^ORCH_WORKER_NAME=myfleet-$i\$" "$CONFIG_DIR/env.$i"
    done
    # A NEW slot after the prefix change does get the new prefix.
    sed -i.bak 's/^WORKER_COUNT=.*/WORKER_COUNT=4/' "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_WORKER_NAME=otherfleet-4$' "$CONFIG_DIR/env.4"
    grep -q '^ORCH_WORKER_NAME=myfleet-1$' "$CONFIG_DIR/env.1"
}

@test "run-fleet: ORCH_WORKER_NAME with only spaces counts as empty" {
    # A blank name does not become sticky: the worker would send it as is to
    # the server on every poll.
    printf '%s\n' 'ORCH_API_KEY=' 'ORCH_ENROLLMENT_TOKEN=tok-old' \
        'ORCH_WORKER_NAME=   ' > "$CONFIG_DIR/env.1"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_WORKER_NAME=myfleet-1$' "$CONFIG_DIR/env.1"
}

@test "run-fleet: a trailing CR in ORCH_WORKER_NAME is not restamped" {
    # env.N edited from Windows (CRLF): the name is kept without the \r.
    printf 'ORCH_API_KEY=\nORCH_ENROLLMENT_TOKEN=tok-old\nORCH_WORKER_NAME=worker1\r\n' \
        > "$CONFIG_DIR/env.1"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_WORKER_NAME=worker1$' "$CONFIG_DIR/env.1"
}

@test "run-fleet: passes the host CONFIG_DIR to autostart" {
    # 06-autostart.sh resolves setup.env with its own load_env: without the
    # explicit variable it falls back to the /etc/orcaster default of lib.sh and
    # would read a different directory than the rest of this script, silently.
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "autostart N=1 CONFIG_DIR=$CONFIG_DIR" "$CALLS"
}

@test "run-fleet: one 'up' and one 'autostart' per worker" {
    bash "$SCRIPT"
    [ "$(grep -c 'make .*up client N=1' "$CALLS")" -eq 1 ]
    [ "$(grep -c 'make .*up client N=2' "$CALLS")" -eq 1 ]
    [ "$(grep -c 'make .*up client N=3' "$CALLS")" -eq 1 ]
    [ "$(grep -c 'autostart N=1' "$CALLS")" -eq 1 ]
    [ "$(grep -c 'autostart N=3' "$CALLS")" -eq 1 ]
}

# The Makefile resolves env.N and the pubkey from CONFIG_DIR on its own
# (WORKER_ENV_HOST). run-fleet passes the config dir, never the
# two file paths. A Makefile without that resolution (rollback to an older
# release) still gets the overrides, or it would look in client/.
@test "run-fleet: does not pass WORKER_ENV/WORKER_PUBKEY to a Makefile that resolves them" {
    printf 'WORKER_ENV_HOST    := $(CONFIG_DIR_NORM)/env\n' > "$INSTALL_DIR/Makefile"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(grep -c "make .*up client N=1 .*CONFIG_DIR=$CONFIG_DIR ETC_SETUP_ENV_FILE=$CONFIG_DIR/setup.env " "$CALLS")" -eq 1 ]
    [ "$(grep -c "make .*up client N=3 .*CONFIG_DIR=$CONFIG_DIR ETC_SETUP_ENV_FILE=$CONFIG_DIR/setup.env " "$CALLS")" -eq 1 ]
    run grep -E 'make .*up client.*(WORKER_ENV|WORKER_PUBKEY)=' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: passes WORKER_ENV/WORKER_PUBKEY only to an older Makefile without WORKER_ENV_HOST" {
    printf 'WORKER_ENV ?= client/.env\n' > "$INSTALL_DIR/Makefile"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "make .*up client N=1 .*WORKER_ENV=$CONFIG_DIR/env.1 WORKER_PUBKEY=$CONFIG_DIR/server-ed25519.pub " "$CALLS"
    grep -q "make .*up client N=3 .*WORKER_ENV=$CONFIG_DIR/env.3 WORKER_PUBKEY=$CONFIG_DIR/server-ed25519.pub " "$CALLS"
}

@test "run-fleet: 2/2 only when all workers have enrolled" {
    cat > "$T/bin/fake-enroll" <<'E'
#!/usr/bin/env bash
echo "enroll-check N=$1" >> "$CALLS"
exit 0
E
    chmod +x "$T/bin/fake-enroll"
    export ENROLL_CHECK="$T/bin/fake-enroll" ENROLL_WAIT=0
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "1/2"
    assert_out "$output" "2/2"
    [ "$(grep -c 'enroll-check N=' "$CALLS")" -eq 3 ]
}

@test "run-fleet: aborts without 2/2 if a worker does not enroll within ENROLL_WAIT" {
    cat > "$T/bin/fake-enroll" <<'E'
#!/usr/bin/env bash
echo "enroll-check N=$1" >> "$CALLS"
[ "$1" != "2" ]   # worker 2 never enrolls (exhausted token, e.g.)
E
    chmod +x "$T/bin/fake-enroll"
    export ENROLL_CHECK="$T/bin/fake-enroll" ENROLL_WAIT=0
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    assert_out "$output" "1/2"
    refute_out "$output" "2/2"
    assert_out "$output" "not enrolled"
    assert_out "$output" " 2"
}

@test "run-fleet: worker_enrolled reads state.json inside the userns (podman unshare)" {
    # The container runs as USER orcaster (internal uid 1000): in rootless mode,
    # state.json ends up 0600 owned by a host SUBUID, unreadable by the host's
    # orcaster. Fixture: state.json present but chmod 000 (same access
    # semantics); only the 'podman unshare' mock grants read access, like the
    # real userns. A direct grep from the host must fail and the deployment
    # must not declare 2/2 through that path.
    [ "$(id -u)" -ne 0 ] || skip "chmod 000 does not block root"
    unset ENROLL_CHECK
    export FLEET_VOLS="$T/volumes" ENROLL_WAIT=0
    for i in 1 2 3; do
        mkdir -p "$FLEET_VOLS/orcaster-data-$i/_data"
        printf '{"worker_id": "w-%s", "api_key": "k"}\n' "$i" \
            > "$FLEET_VOLS/orcaster-data-$i/_data/state.json"
        chmod 000 "$FLEET_VOLS/orcaster-data-$i/_data/state.json"
    done
    cat > "$T/bin/podman" <<'P'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
if [ "${1:-}" = "volume" ] && [ "${2:-}" = "inspect" ]; then
    printf '%s/%s/_data\n' "$FLEET_VOLS" "$3"
    exit 0
fi
if [ "${1:-}" = "unshare" ]; then
    shift
    # Simulates the userns: inside, the process becomes the "owner" of the
    # volume files (subuid); we give read access back to the owner before
    # running the command.
    find "$FLEET_VOLS" -type f -name state.json -exec chmod u+r {} +
    exec "$@"
fi
P
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "2/2"
    grep -q 'podman unshare grep' "$CALLS"
}

@test "run-fleet: aborts early if a worker is left without credentials" {
    # Empty token in setup.env and no previous .env.N: the container can only
    # crash-loop until the step 2/2 timeout. Better to abort before starting
    # anything, with the cause (missing token) in the message.
    cat > "$ENV_FILE_OVERRIDE" <<EOF
ORCH_BASE_URL=https://orch.example.com
ORCH_ENROLLMENT_TOKEN=
WORKER_COUNT=3
WORKER_NAME_PREFIX=myfleet
EOF
    # Real state.json probe: with the default podman mock,
    # `volume inspect` returns no mountpoint, that is, there is no volume.
    unset ENROLL_CHECK
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    out="$output"
    assert_out "$out" "ORCH_ENROLLMENT_TOKEN"
    assert_out "$out" "state.json"
    # `run` + status and not a bare `! grep`: a `!` that is not the LAST
    # statement of the @test does not gate in bats.
    # `run` overwrites $output, which is why the script output is saved first in $out.
    run grep -q 'up client' "$CALLS"
    [ "$status" -ne 0 ]
    refute_out "$out" "1/2"
}

@test "run-fleet: empty token with a previous ORCH_API_KEY does not abort (already enrolled)" {
    # Rerun on an already enrolled fleet (api_key preserved in each .env.N):
    # it does not need a valid token and must not abort.
    cat > "$ENV_FILE_OVERRIDE" <<EOF
ORCH_BASE_URL=https://orch.example.com
ORCH_ENROLLMENT_TOKEN=
WORKER_COUNT=3
WORKER_NAME_PREFIX=myfleet
EOF
    for i in 1 2 3; do
        printf 'ORCH_API_KEY=key-%s\n' "$i" > "$CONFIG_DIR/env.$i"
    done
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "2/2"
    grep -q '^ORCH_API_KEY=key-2$' "$CONFIG_DIR/env.2"
}

# --- Credentials persisted in state.json ------------------------
# An already enrolled worker starts without a token: `ensure_credentials` loads
# worker_id/api_key from state.json and returns before looking at it. These
# tests use the REAL probe (worker_enrolled) with a podman mock that resolves
# the volume to a test directory and runs `podman unshare` directly.

# podman mock with volumes in $FLEET_VOLS. A volume without a directory makes
# `volume inspect` fail, like the real podman with a nonexistent volume.
state_podman_mock() {
    export FLEET_VOLS="$T/volumes"
    mkdir -p "$FLEET_VOLS"
    cat > "$T/bin/podman" <<'P'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
if [ "${1:-}" = "volume" ] && [ "${2:-}" = "inspect" ]; then
    [ -d "$FLEET_VOLS/$3/_data" ] || { echo "Error: no such volume $3" >&2; exit 125; }
    printf '%s/%s/_data\n' "$FLEET_VOLS" "$3"
    exit 0
fi
if [ "${1:-}" = "unshare" ]; then
    shift
    exec "$@"
fi
P
    chmod +x "$T/bin/podman"
}

# state.json of worker N in the state.save format (indent=2).
write_state() {
    local n="$1" wid="$2" key="$3"
    mkdir -p "$FLEET_VOLS/orcaster-data-$n/_data"
    printf '{\n  "worker_id": "%s",\n  "api_key": "%s"\n}\n' "$wid" "$key" \
        > "$FLEET_VOLS/orcaster-data-$n/_data/state.json"
}

empty_token_setup_env() {
    cat > "$ENV_FILE_OVERRIDE" <<EOF
ORCH_BASE_URL=https://orch.example.com
ORCH_ENROLLMENT_TOKEN=
WORKER_COUNT=3
WORKER_NAME_PREFIX=myfleet
EOF
}

@test "run-fleet: empty token without ORCH_API_KEY but with state.json does not abort" {
    empty_token_setup_env
    unset ENROLL_CHECK
    export ENROLL_WAIT=0
    state_podman_mock
    for i in 1 2 3; do
        write_state "$i" "wid-secret-$i" "key-secret-$i"
    done
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    out="$output"
    assert_out "$out" "state.json in orcaster-data-1"
    assert_out "$out" "2/2"
    [ "$(grep -c 'make .*up client N=' "$CALLS")" -eq 3 ]
    # env.N still has no token or api_key: the identity lives in the volume.
    grep -q '^ORCH_ENROLLMENT_TOKEN=$' "$CONFIG_DIR/env.1"
    grep -q '^ORCH_API_KEY=$' "$CONFIG_DIR/env.1"
    # The content of state.json is never printed.
    refute_out "$out" "key-secret"
    refute_out "$out" "wid-secret"
}

@test "run-fleet: state.json with an empty api_key aborts as without credentials" {
    # state.load discards a state.json without api_key and the worker would ask
    # for the token again: the probe must require both keys with a value.
    empty_token_setup_env
    unset ENROLL_CHECK
    state_podman_mock
    for i in 1 2 3; do
        write_state "$i" "wid-$i" ""
    done
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    out="$output"
    assert_out "$out" "Worker 1 has no credentials"
    assert_out "$out" "orcaster-data-1"
    run grep -q 'up client' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: aborts at the first worker without state.json and names its volume" {
    empty_token_setup_env
    unset ENROLL_CHECK
    state_podman_mock
    write_state 1 "wid-1" "key-1"
    # Worker 2 has no volume (never enrolled); 3 is never evaluated.
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    out="$output"
    assert_out "$out" "Worker 2 has no credentials"
    assert_out "$out" "orcaster-data-2"
    refute_out "$out" "2/2"
    run grep -q 'up client N=2' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: with a token in setup.env it does not probe state.json before starting" {
    # The usual path: the token is enough and the volume probe is not queried
    # in the loop (only in step 2/2, after all the 'up' calls).
    cat > "$T/bin/fake-enroll" <<'E'
#!/usr/bin/env bash
echo "enroll-check N=$1" >> "$CALLS"
exit 0
E
    chmod +x "$T/bin/fake-enroll"
    export ENROLL_CHECK="$T/bin/fake-enroll" ENROLL_WAIT=0
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    first_check="$(grep -n 'enroll-check N=' "$CALLS" | head -n1 | cut -d: -f1)"
    last_up="$(grep -n 'up client N=' "$CALLS" | tail -n1 | cut -d: -f1)"
    [ "$first_check" -gt "$last_up" ]
}

@test "run-fleet: with a previous ORCH_API_KEY it does not probe state.json before starting" {
    empty_token_setup_env
    for i in 1 2 3; do
        printf 'ORCH_API_KEY=key-%s\n' "$i" > "$CONFIG_DIR/env.$i"
    done
    cat > "$T/bin/fake-enroll" <<'E'
#!/usr/bin/env bash
echo "enroll-check N=$1" >> "$CALLS"
exit 0
E
    chmod +x "$T/bin/fake-enroll"
    export ENROLL_CHECK="$T/bin/fake-enroll" ENROLL_WAIT=0
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    first_check="$(grep -n 'enroll-check N=' "$CALLS" | head -n1 | cut -d: -f1)"
    last_up="$(grep -n 'up client N=' "$CALLS" | tail -n1 | cut -d: -f1)"
    [ "$first_check" -gt "$last_up" ]
}

@test "run-fleet: prunes workers with N greater than WORKER_COUNT and keeps volumes" {
    # Rerun with a reduced fleet (WORKER_COUNT=3): previous env.4/env.5 remain.
    touch "$CONFIG_DIR/env.4" "$CONFIG_DIR/env.5"
    # env (base) and env.example (non-numeric suffix) are decoys: the prune
    # must not touch them, the same check it used to make with .env/.env.example.
    touch "$CONFIG_DIR/env" "$CONFIG_DIR/env.example"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q 'systemctl --user disable --now container-orcaster-worker-4.service' "$CALLS"
    grep -q 'systemctl --user disable --now container-orcaster-worker-5.service' "$CALLS"
    grep -q 'make .*down client N=4' "$CALLS"
    grep -q 'make .*down client N=5' "$CALLS"
    [ ! -e "$CONFIG_DIR/env.4" ]
    [ ! -e "$CONFIG_DIR/env.5" ]
    # Identity intact: no 'volume rm'; base env and env.example remain.
    # `run` + status (not a bare `! grep`): a `!` on a command that is not the
    # last statement of the @test does not gate in bats, and this is the ONLY
    # assertion that guards that the prune does not delete orcaster-data-N.
    run grep -q 'volume rm' "$CALLS"
    [ "$status" -ne 0 ]
    [ -f "$CONFIG_DIR/env" ]
    [ -f "$CONFIG_DIR/env.example" ]
    # Current workers are not pruned.
    [ -f "$CONFIG_DIR/env.1" ]
    ! grep -q 'down client N=3' "$CALLS"
}

# The pruned worker may also have its egress sidecar, with its own unit. If
# that unit stays enabled, systemd relaunches the proxy that the prune's `down`
# just removed: a proxy without a worker is left occupying the egress networks
# and, after a tarball release swap, with its code bind mount over an already
# replaced inode.
@test "run-fleet: the prune also disables the sidecar unit of the surplus worker" {
    touch "$CONFIG_DIR/env.4"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q 'systemctl --user disable --now container-orcaster-worker-4.service' "$CALLS"
    grep -q 'systemctl --user disable --now container-orcaster-proxy-4.service' "$CALLS"
    # The disable goes BEFORE the `down` that removes both containers: the other
    # way around, systemd relaunches what the down stops. The order is half the fix.
    dis_line="$(grep -n 'disable --now container-orcaster-proxy-4.service' "$CALLS" | head -1 | cut -d: -f1)"
    down_line="$(grep -n 'down client N=4' "$CALLS" | head -1 | cut -d: -f1)"
    [ "$dis_line" -lt "$down_line" ]
}

@test "run-fleet: without a sidecar unit the prune neither touches it nor warns falsely" {
    # Host that never had egress: the proxy unit does not exist. Without the
    # existence probe, the disable would fail and the warn would lie.
    touch "$CONFIG_DIR/env.4"
    export SYSTEMCTL_MISSING_UNITS="container-orcaster-proxy-4.service"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q 'systemctl --user disable --now container-orcaster-worker-4.service' "$CALLS"
    # `run` + status: a `!` in the middle of the @test does not gate.
    run grep -q 'disable --now container-orcaster-proxy-4.service' "$CALLS"
    [ "$status" -ne 0 ]
    refute_out "$output" "sidecar of surplus worker"
}

# The existence probe used to be a `systemctl --user cat` with the output
# hidden, which answers != 0 both when there is no unit file (staying quiet is
# correct) and when the --user bus does not answer (a real failure). In the
# second case the sidecar disable was skipped in total silence, while the
# worker's disable of the same index failed loudly in the same run.
@test "run-fleet: if it cannot probe the unit the prune says so, with the reason" {
    touch "$CONFIG_DIR/env.4"
    export SYSTEMCTL_BUS_UNREACHABLE=1
    run bash "$SCRIPT"
    # Pruning is still cleanup: it warns and continues.
    [ "$status" -eq 0 ]
    assert_out "$output" "Could not tell whether container-orcaster-worker-4.service exists"
    assert_out "$output" "Could not tell whether container-orcaster-proxy-4.service exists"
    # The reason systemd gave is in the notice: without it, the operator does not
    # know whether to look at the worker's bus or at the unit.
    assert_out "$output" "Failed to connect to bus"
    # It does not attempt the disable of what it could not verify.
    run grep -q 'disable --now container-orcaster-worker-4.service' "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q 'disable --now container-orcaster-proxy-4.service' "$CALLS"
    [ "$status" -ne 0 ]
    # But the rest of the prune runs anyway: the `down` is podman and does not
    # need the bus. Without these two assertions, an rc=2 turned into `continue`
    # would pass the test while abandoning the whole prune.
    grep -q 'make .*down client N=4' "$CALLS"
    [ ! -e "$CONFIG_DIR/env.4" ]
}

# The worker's disable used to run without an existence probe. On a host
# migrated to $CONFIG_DIR by hand the env.N may be copied without
# 06-autostart.sh ever having run for that index: there is no unit, the
# disable always fails and the warn talked about a container that systemd will
# relaunch when there is no unit to relaunch it. It is literally the case that
# the sidecar comment uses to justify ITS probe.
@test "run-fleet: without a unit for the surplus worker the prune neither touches it nor warns falsely" {
    touch "$CONFIG_DIR/env.4"
    export SYSTEMCTL_MISSING_UNITS="container-orcaster-worker-4.service"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    # `run` + status: a `!` in the middle of the @test does not gate.
    run grep -q 'disable --now container-orcaster-worker-4.service' "$CALLS"
    [ "$status" -ne 0 ]
    refute_out "$output" "Could not disable container-orcaster-worker-4.service"
    refute_out "$output" "Could not tell whether container-orcaster-worker-4.service"
    # The probe is per unit and not per index: this host's sidecar DOES exist and
    # is disabled anyway, and the container is stopped by the `down` as usual (it
    # may exist without a unit, which is exactly the migration case).
    grep -q 'disable --now container-orcaster-proxy-4.service' "$CALLS"
    grep -q 'make .*down client N=4' "$CALLS"
    [ ! -e "$CONFIG_DIR/env.4" ]
}

@test "run-fleet: a failing sidecar disable warns and does not bring down the prune" {
    touch "$CONFIG_DIR/env.4"
    export SYSTEMCTL_DISABLE_FAILS=1
    run bash "$SCRIPT"
    # Pruning is cleanup: it warns and continues, never aborts a deployment that
    # already started all the requested workers.
    [ "$status" -eq 0 ]
    assert_out "$output" "sidecar of surplus worker 4"
    [ ! -e "$CONFIG_DIR/env.4" ]
}

# --- Permissions of the config dir and of env.N: no silences ---
# The worker runs as $WORKER_USER and rootless podman reads the --env-file as
# that user. An env.N it cannot read gives no clear error: the container
# crash-loops until the step 2/2 timeout, whose message tells you to look at
# the token, which is fine. That is why the three failures abort here, with the cause.

@test "run-fleet: creates the config dir with an explicit owner, only once" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "install -d -m 0750 -o root -g $WORKER_USER $CONFIG_DIR" "$CALLS"
    # Exactly ONCE, with WORKER_COUNT=3: the `install -d` is invariant across
    # the fleet loop and a review moved it outside, but nothing pinned it.
    # Inside, it gave an identical `install -d` per worker (eight with
    # WORKER_COUNT=8, each with its implicit chmod/chown) to do the same work,
    # and putting it back there broke no test.
    # grep -cF: the mktemp $CONFIG_DIR has dots, which are wildcards in a regex.
    [ "$(grep -cF "install -d -m 0750 -o root -g $WORKER_USER $CONFIG_DIR" "$CALLS")" -eq 1 ]
}

@test "run-fleet: leaves each env.N as 0640 root:worker" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "chown root:$WORKER_USER $CONFIG_DIR/env.1" "$CALLS"
    grep -q "chmod 0640 $CONFIG_DIR/env.1" "$CALLS"
}

@test "run-fleet: if it cannot create the dir with an owner it aborts and starts nothing" {
    export INSTALL_FAILS=1
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"Could not create $CONFIG_DIR"* ]] || { echo "$output"; false; }
    # `run` + status: a `!` in the middle of the @test does not gate.
    run grep -q 'up client' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: if the env.N chown fails it aborts with the cause" {
    export CHOWN_FAILS=1
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"0640 root:$WORKER_USER"* ]] || { echo "$output"; false; }
    run grep -q 'up client' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: if the env.N chmod fails it aborts with the cause" {
    # Without the chmod the file keeps the mode that stamp_worker_env left: the
    # chown alone is not enough for the group to be able to read it.
    export CHMOD_FAILS=1
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"0640 root:$WORKER_USER"* ]] || { echo "$output"; false; }
    run grep -q 'up client' "$CALLS"
    [ "$status" -ne 0 ]
}

# --- Prune: warns and continues, but not silently ---------------

@test "run-fleet: if the prune disable fails it warns and keeps pruning" {
    touch "$CONFIG_DIR/env.4"
    export SYSTEMCTL_DISABLE_FAILS=1
    run bash "$SCRIPT"
    # Pruning is cleanup: it cannot bring down a deployment that already started
    # the requested workers. But a live unit relaunches the container that the
    # down stops, so the surplus worker keeps taking tasks: that has to be said.
    [ "$status" -eq 0 ]
    [[ "$output" == *"Could not disable container-orcaster-worker-4.service"* ]] \
        || { echo "$output"; false; }
    grep -q 'make .*down client N=4' "$CALLS"
    [ ! -e "$CONFIG_DIR/env.4" ]
}

@test "run-fleet: if the prune down fails it warns and deletes env.N anyway" {
    touch "$CONFIG_DIR/env.4"
    export MAKE_DOWN_FAILS=1
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Could not stop the container of surplus worker 4"* ]] \
        || { echo "$output"; false; }
    # env.N is deleted anyway, and the message says so: the next rerun will no
    # longer prune it, so the orphan container is left to the operator.
    [ ! -e "$CONFIG_DIR/env.4" ]
}

@test "run-fleet: rerun stops the active unit before recreating the worker" {
    export ACTIVE_UNITS="container-orcaster-worker-2.service"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q 'systemctl --user stop container-orcaster-worker-2.service' "$CALLS"
    stop_line="$(grep -n 'systemctl --user stop container-orcaster-worker-2.service' "$CALLS" | head -1 | cut -d: -f1)"
    up_line="$(grep -n 'make .*up client N=2' "$CALLS" | head -1 | cut -d: -f1)"
    [ "$stop_line" -lt "$up_line" ]
    # Workers without an active unit get no stop. `run` + status and not a bare
    # `!`: a `!` that is not the LAST statement of the @test does not gate in
    # bats; without this, an undue stop on worker 1 went unnoticed.
    run grep -q 'systemctl --user stop container-orcaster-worker-1.service' "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q 'systemctl --user stop container-orcaster-worker-3.service' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: passes EGRESS_ISOLATION=1 to make up client by default" {
    bash "$SCRIPT"
    [ "$(grep -c 'make .*up client N=1 .*EGRESS_ISOLATION=1' "$CALLS")" -eq 1 ]
    [ "$(grep -c 'make .*up client N=2 .*EGRESS_ISOLATION=1' "$CALLS")" -eq 1 ]
}

@test "run-fleet: propagates WORKER_MEMORY to make up client" {
    printf 'WORKER_MEMORY=28g\n' >> "$ENV_FILE_OVERRIDE"
    bash "$SCRIPT"
    [ "$(grep -c 'make .*up client N=1 .*WORKER_MEMORY=28g' "$CALLS")" -eq 1 ]
    [ "$(grep -c 'make .*up client N=3 .*WORKER_MEMORY=28g' "$CALLS")" -eq 1 ]
}

@test "run-fleet: without WORKER_MEMORY it passes the empty value (no cap, Makefile default)" {
    bash "$SCRIPT"
    [ "$(grep -c 'make .*up client N=1 .*WORKER_MEMORY= CONFIG_DIR=' "$CALLS")" -eq 1 ]
}

# --- WORKER_RO_MOUNTS --------------------------------------------
# It is explicit on the make line, like WORKER_MEMORY: run-fleet validated the
# value with load_env, and the containers must get exactly that value rather
# than a re-read by the Makefile.

@test "run-fleet: propagates WORKER_RO_MOUNTS to make up client" {
    ro_fixture
    printf "WORKER_RO_MOUNTS='%s %s'\n" "$FIX/example-ro" "$FIX/snap" >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(grep -c "make .*up client N=1 .*WORKER_RO_MOUNTS=$FIX/example-ro $FIX/snap\$" "$CALLS")" -eq 1 ]
    [ "$(grep -c "make .*up client N=3 .*WORKER_RO_MOUNTS=$FIX/example-ro $FIX/snap\$" "$CALLS")" -eq 1 ]
}

@test "run-fleet: without WORKER_RO_MOUNTS it passes the empty value (no mounts)" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(grep -c 'make .*up client N=1 .*WORKER_RO_MOUNTS=$' "$CALLS")" -eq 1 ]
}

@test "run-fleet: WORKER_RO_MOUNTS with a nonexistent directory aborts before touching any worker" {
    ro_fixture
    printf "WORKER_RO_MOUNTS='%s %s'\n" "$FIX/example-ro" "$FIX/missing" >> "$ENV_FILE_OVERRIDE"
    # With the unit active, the loop would stop it BEFORE make: the probe has to
    # go before the loop or the worker is left stopped.
    export ACTIVE_UNITS="container-orcaster-worker-1.service"
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    assert_out "$output" "'$FIX/missing' does not exist on this host"
    assert_out "$output" "No worker was touched"
    run grep -q 'systemctl --user stop' "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q 'make .*up client' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: WORKER_RO_MOUNTS unreadable by the worker aborts before touching any worker" {
    if [ "$(id -u)" -eq 0 ]; then
        skip "root ignores mode bits: the case cannot be reproduced"
    fi
    ro_fixture
    /bin/chmod 0311 "$FIX/example-ro"
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/example-ro" >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    /bin/chmod 0755 "$FIX/example-ro"
    [ "$status" -ne 0 ]
    assert_out "$output" "cannot read or traverse '$FIX/example-ro'"
    run grep -q 'make .*up client' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: WORKER_RO_MOUNTS with invalid syntax aborts in load_env" {
    printf "WORKER_RO_MOUNTS='/opt/example-ro,/x'\n" >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    assert_out "$output" "'/opt/example-ro,/x' has characters outside"
    run grep -q 'make .*up client' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: under egress it stops the proxy's active unit before recreating" {
    export ACTIVE_UNITS="container-orcaster-proxy-2.service"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q 'systemctl --user stop container-orcaster-proxy-2.service' "$CALLS"
    stop_line="$(grep -n 'systemctl --user stop container-orcaster-proxy-2.service' "$CALLS" | head -1 | cut -d: -f1)"
    up_line="$(grep -n 'make .*up client N=2' "$CALLS" | head -1 | cut -d: -f1)"
    [ "$stop_line" -lt "$up_line" ]
    # Proxies without an active unit get no stop.
    run grep -q 'systemctl --user stop container-orcaster-proxy-1.service' "$CALLS"; [ "$status" -ne 0 ]
}

# Lowering EGRESS_ISOLATION from 1 to 0 and running run-fleet used to leave the
# orcaster-proxy-N sidecars (N <= WORKER_COUNT) alive, without a notice. The
# chosen design (non-destructive, "report + autostart respects the opt-out"):
# run-fleet does NOT stop/disable/delete anything of the in-fleet sidecar, it
# REPORTS it in its own category (stale-by-config, distinct from the orphans by
# index) with the exact manual recipe; removal is up to the operator.
@test "run-fleet: EGRESS_ISOLATION=0 does not touch the in-fleet proxy but reports it as obsolete" {
    export EGRESS_ISOLATION=0 ACTIVE_UNITS="container-orcaster-proxy-2.service"
    # The in-fleet proxy (N=2 <= WORKER_COUNT=3) survives from an isolated run.
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-worker-1\norcaster-worker-2\norcaster-worker-3\norcaster-proxy-2\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    # `run` overwrites $output on each call, and the negations below use it: the
    # script output is saved first (same pattern as the rest of the file).
    out="$output"
    [ "$(grep -c 'make .*up client N=1 .*EGRESS_ISOLATION=0' "$CALLS")" -eq 1 ]
    # But it DOES report the sidecar obsolete by config, with the N and the exact recipe.
    assert_out "$out" "Egress sidecar(s) obsolete by config (EGRESS_ISOLATION=0): 2"
    assert_out "$out" "disable --now container-orcaster-proxy-2.service"
    assert_out "$out" "podman rm -f orcaster-proxy-2"
    assert_out "$out" "podman volume rm orcaster-egress-vol-2"
    # Non-destructive: run-fleet does not stop, disable or delete the in-fleet proxy.
    run grep -q 'systemctl --user stop container-orcaster-proxy' "$CALLS"; [ "$status" -ne 0 ]
    run grep -q 'disable --now container-orcaster-proxy' "$CALLS"; [ "$status" -ne 0 ]
    run grep -q 'volume rm' "$CALLS"; [ "$status" -ne 0 ]
}

@test "run-fleet: EGRESS_ISOLATION=0 separates the obsolete in-fleet proxy from the orphan by index" {
    # Two proxies: N=2 in-fleet (stale-by-config) and N=5 outside the fleet
    # (orphan by index). Each in its own category, not mixed: the orphan by
    # index is NOT relabelled as obsolete-by-config (do not lie about the reason).
    export EGRESS_ISOLATION=0
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-worker-1\norcaster-worker-2\norcaster-worker-3\norcaster-proxy-2\norcaster-proxy-5\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    # N=2 (<= WORKER_COUNT=3): stale-by-config category, with its recipe.
    assert_out "$output" "Egress sidecar(s) obsolete by config (EGRESS_ISOLATION=0): 2"
    assert_out "$output" "disable --now container-orcaster-proxy-2.service"
    # N=5 (> WORKER_COUNT): follows the usual orphan path, unchanged.
    assert_out "$output" "Surplus egress sidecar(s) the prune did NOT see: 5"
    # No crossover: orphan-5 does not enter the stale recipe (which does carry
    # 'podman rm -f'), and in-fleet-2 does not enter the orphan header.
    refute_out "$output" "podman rm -f orcaster-proxy-5"
    refute_out "$output" "the prune did NOT see: 2"
}

# --- Worker disk gate -------------------------------------------

@test "run-fleet: propagates the disk gate to each env.N" {
    printf 'ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05\nORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600\n' >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    for i in 1 2 3; do
        grep -q '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05$' "$CONFIG_DIR/env.$i"
        grep -q '^ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600$' "$CONFIG_DIR/env.$i"
    done
}

@test "run-fleet: without a disk gate in setup.env it does not add those lines" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    run grep -q 'CACHE_CLAIM' "$CONFIG_DIR/env.1"
    [ "$status" -ne 0 ]
}

@test "run-fleet: a full rerun keeps the configured disk gate" {
    # This is the regression of the issue: stamp_worker_env used not to know
    # these variables, so a rerun of the suite lost them silently even though
    # setup.env still declared them.
    printf 'ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05\nORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600\n' >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    for i in 1 2 3; do
        grep -q '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05$' "$CONFIG_DIR/env.$i"
        grep -q '^ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600$' "$CONFIG_DIR/env.$i"
        grep -q '^ORCH_ENROLLMENT_TOKEN=tok-fleet$' "$CONFIG_DIR/env.$i"
    done
}

@test "run-fleet: the fleet env files are written to the config dir, not to the checkout" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ -f "$CONFIG_DIR/env.1" ]
    run test -e "$INSTALL_DIR/client/.env.1"
    [ "$status" -ne 0 ]
}

# Orphan probe: the prune only sees the env.N files of $CONFIG_DIR (its own
# bookkeeping). A worker container with a higher index whose env.N never
# existed there (migration of a multi-worker host to the per-host layout, or a
# prune `down` that failed and deleted env.N anyway) stays invisible and keeps
# taking tasks. The notice is the only thing that gives it away.
@test "run-fleet: warns about the surplus worker container that the prune cannot see" {
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-worker-1\norcaster-worker-2\norcaster-worker-3\norcaster-worker-4\norcaster-worker-5\norcaster-proxy-1\nunrelated-container\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Surplus worker(s) the prune did NOT see: 4 5"* ]] \
        || { echo "output:"; echo "$output"; return 1; }
    # It warns, but does NOT prune: env.N is the proof of index ownership.
    run grep -q 'down client N=4' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: a consistent fleet does not warn about surplus" {
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-worker-1\norcaster-worker-2\norcaster-worker-3\norcaster-proxy-1\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" != *"the prune did NOT see"* ]] \
        || { echo "unexpected notice:"; echo "$output"; return 1; }
}

# The probe also has to see the sidecar: before the fix it filtered only by
# `orcaster-worker-*`, so an orphan orcaster-proxy-N stayed invisible and the
# probe exited 0 without saying anything.
@test "run-fleet: warns about the surplus egress sidecar that the prune cannot see" {
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-worker-1\norcaster-worker-2\norcaster-worker-3\norcaster-proxy-4\nunrelated-container\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "Surplus egress sidecar(s) the prune did NOT see: 4"
    # The remedy names the PROXY unit, which is the container that was seen.
    assert_out "$output" "container-orcaster-proxy-<n>.service"
    # It warns, but does NOT prune: env.N is the proof of index ownership.
    run grep -q 'down client N=4' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: a surplus worker and a surplus sidecar are reported separately" {
    # Two notices and not one: the remedy names the unit to disable, and only the
    # unit of the container that the probe really saw can be named.
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-worker-1\norcaster-worker-4\norcaster-proxy-5\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "Surplus worker(s) the prune did NOT see: 4"
    assert_out "$output" "Surplus egress sidecar(s) the prune did NOT see: 5"
}

@test "run-fleet: if 'podman ps -a' fails it says so instead of taking the probe as good" {
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) echo "cannot connect to podman" >&2; exit 125 ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"Could not list the host containers"* ]] \
        || { echo "output:"; echo "$output"; return 1; }
}

# --- Containers WITHOUT suffix ---------------------
# The README tells you to converge to the fleet layout the host that was
# finished by hand through the Bootstrap (env without suffix, orcaster-worker
# container without suffix) by running THIS script: it stamps env.1 and creates
# orcaster-worker-1, and the unsuffixed one stays alive with its unit enabled.
# Two workers asking the same server for tasks, and after the next tarball swap
# the unsuffixed one runs over already replaced inodes. The prune's `env.*`
# glob cannot see it (that host's env has no suffix) and neither can the
# probe's numeric branches: the probe exited 0 without a word, which is the
# silent orphan that this probe came to report.

@test "run-fleet: warns about the unsuffixed worker that the prune cannot see" {
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-worker\norcaster-worker-1\norcaster-worker-2\norcaster-worker-3\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "Unsuffixed worker (orcaster-worker) the prune did NOT see"
    # The remedy names the unit of the UNSUFFIXED container, which is different
    # from the suffixed ones: container-orcaster-worker.service.
    assert_out "$output" "disable --now container-orcaster-worker.service"
    # And the `down` goes WITHOUT N=: the Makefile defines an empty `N ?=` and
    # derives WORKER_SUFFIX from it, so omitting N is what points the recipe at
    # the unsuffixed container; with an N= it would stop another one. It is
    # measured on the output dumped to a file because the end-of-line anchor
    # cannot be expressed with assert_out.
    printf '%s\n' "$output" > "$T/out"
    grep -q 'down client$' "$T/out"
    # `run` + status and not a bare `!`: a `!` that is not the LAST statement of
    # the @test does not gate in bats.
    run grep -q 'down client N=' "$T/out"
    [ "$status" -ne 0 ]
    # It warns, but does NOT prune: env.N is the proof of index ownership, and
    # the unsuffixed container has none.
    run grep -q 'down client' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "run-fleet: the unsuffixed proxy is reported alone, without inventing an unsuffixed worker" {
    # Two flags and two independent notices, the same rule that governs the two
    # numeric lists: the remedy names the unit of the container that the probe
    # really saw. A single notice would send you to disable
    # container-orcaster-worker.service on a host where that container does not
    # exist, and that is claiming something the probe did not verify.
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-proxy\norcaster-worker-1\norcaster-worker-2\norcaster-worker-3\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "Unsuffixed egress sidecar (orcaster-proxy) the prune did NOT see"
    assert_out "$output" "disable --now container-orcaster-proxy.service"
    refute_out "$output" "Unsuffixed worker"
    refute_out "$output" "disable --now container-orcaster-worker.service"
}

@test "run-fleet: the probe reports the raw suffix, 08 is not 8" {
    # Deliberate asymmetry with the prune, which does normalise to base 10: here
    # the index names a container that exists on the host with that literal
    # name. A orcaster-worker-08 reported as 8 would send the operator to
    # disable and stop container-orcaster-worker-8.service, which does not exist.
    cat > "$T/bin/podman" <<'M'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case " $* " in
    *" ps -a "*) printf 'orcaster-worker-1\norcaster-worker-2\norcaster-worker-3\norcaster-worker-08\n' ;;
esac
M
    chmod +x "$T/bin/podman"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    assert_out "$output" "Surplus worker(s) the prune did NOT see: 08"
    refute_out "$output" "Surplus worker(s) the prune did NOT see: 8"
}

# The prune, on the contrary, DOES normalise: `${env_file##*/env.}` keeps
# leading zeros, so an env.08 left n=08 and with that raw value the disable and
# the `down` named container-orcaster-worker-08.service and orcaster-worker-08,
# which this script NEVER creates (it iterates `seq 1 "$WORKER_COUNT"`, which
# gives 8). The unit probe answered rc=1, the down stopped nothing, env.08 was
# deleted anyway and the operator read "pruning surplus worker 08" while the
# real orcaster-worker-8 kept taking tasks, already invisible to the next
# rerun's glob.
@test "run-fleet: pruning an env.08 dismantles index 8, not 08" {
    cat > "$ENV_FILE_OVERRIDE" <<EOF
ORCH_BASE_URL=https://orch.example.com
ORCH_ENROLLMENT_TOKEN=tok-fleet
WORKER_COUNT=1
WORKER_NAME_PREFIX=myfleet
EOF
    touch "$CONFIG_DIR/env.08"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q 'systemctl --user disable --now container-orcaster-worker-8.service' "$CALLS"
    grep -q 'systemctl --user disable --now container-orcaster-proxy-8.service' "$CALLS"
    grep -q 'make .*down client N=8' "$CALLS"
    # The names with the raw zero are exactly the ones this script never creates.
    # `run` + status: a `!` in the middle of the @test does not gate in bats.
    run grep -q 'container-orcaster-worker-08.service' "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q 'down client N=08' "$CALLS"
    [ "$status" -ne 0 ]
    # The `rm` goes on the ORIGINAL glob path: the file on disk is named
    # env.08 even though the index is 8, and a rebuilt env.$n would delete nothing.
    [ ! -e "$CONFIG_DIR/env.08" ]
    # The current worker stays up.
    [ -f "$CONFIG_DIR/env.1" ]
    run grep -q 'down client N=1' "$CALLS"
    [ "$status" -ne 0 ]
}

# --- Task environment isolation ---------------------------------

@test "run-fleet: a new fleet stamps ORCH_ENFORCE_ISOLATION=true in each env.N" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    for i in 1 2 3; do
        grep -q '^ORCH_ENFORCE_ISOLATION=true$' "$CONFIG_DIR/env.$i"
        [ "$(grep -c '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env.$i")" -eq 1 ]
        grep -q '^ORCH_ISOLATION_REQUIRED_ENV=$' "$CONFIG_DIR/env.$i"
    done
}

@test "run-fleet: an existing env.N without the line gets false; a new slot gets true" {
    printf '%s\n' 'ORCH_API_KEY=key-prev' 'ORCH_ENROLLMENT_TOKEN=tok-old' \
        'ORCH_WORKER_NAME=myfleet-1' > "$CONFIG_DIR/env.1"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_ENFORCE_ISOLATION=false$' "$CONFIG_DIR/env.1"
    grep -q '^ORCH_ENFORCE_ISOLATION=true$' "$CONFIG_DIR/env.2"
    grep -q '^ORCH_ENFORCE_ISOLATION=true$' "$CONFIG_DIR/env.3"
    # A rerun changes nothing: each env.N keeps what it already had.
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_ENFORCE_ISOLATION=false$' "$CONFIG_DIR/env.1"
    grep -q '^ORCH_ENFORCE_ISOLATION=true$' "$CONFIG_DIR/env.2"
}

@test "run-fleet: ORCH_ENFORCE_ISOLATION=true in setup.env switches an existing host to strict" {
    printf '%s\n' 'ORCH_API_KEY=key-prev' 'ORCH_ENFORCE_ISOLATION=false' > "$CONFIG_DIR/env.1"
    echo 'ORCH_ENFORCE_ISOLATION=true' >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    for i in 1 2 3; do
        grep -q '^ORCH_ENFORCE_ISOLATION=true$' "$CONFIG_DIR/env.$i"
        [ "$(grep -c '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env.$i")" -eq 1 ]
    done
}

@test "run-fleet: stamps a normalised ISOLATION_REQUIRED_ENV in each env.N" {
    echo "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR OTHER_VAR,EXAMPLE_PROTECTED_VAR'" >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    for i in 1 2 3; do
        grep -q '^ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR,OTHER_VAR$' "$CONFIG_DIR/env.$i"
    done
}

@test "run-fleet: a flag conflict between setup.env and EXTRA_ENV aborts without starting anything" {
    cat >> "$ENV_FILE_OVERRIDE" <<'EOF2'
ORCH_ENFORCE_ISOLATION=false
EXTRA_ENV='ORCH_ENFORCE_ISOLATION=true'
EOF2
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    out="$output"
    [ ! -e "$CONFIG_DIR/env.1" ]
    run grep -q 'up client' "$CALLS"
    [ "$status" -ne 0 ]
    assert_out "$out" "different values"
}

@test "run-fleet: a malformed list aborts without starting anything" {
    echo "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR,BAD.NAME'" >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    out="$output"
    [ ! -e "$CONFIG_DIR/env.1" ]
    run grep -q 'up client' "$CALLS"
    [ "$status" -ne 0 ]
    assert_out "$out" "BAD.NAME"
}

@test "run-fleet: EXTRA_ENV with ORCH_ISOLATION_REQUIRED_ENV aborts without starting anything" {
    echo "EXTRA_ENV='ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR'" >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    out="$output"
    [ ! -e "$CONFIG_DIR/env.1" ]
    run grep -q 'up client' "$CALLS"
    [ "$status" -ne 0 ]
    assert_out "$out" "ISOLATION_REQUIRED_ENV in setup.env"
}
