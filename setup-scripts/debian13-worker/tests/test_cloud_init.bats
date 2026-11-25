#!/usr/bin/env bats
# Integration of cloud-init-worker.sh with id/sudo/apt-get/git/curl mocked
# (no root, no network). Deployment is by tarball, so the script no longer
# clones anything: this covers detection of the installed code (RELEASE +
# image), the abort when it is missing, and the materialization of setup.env.
#
# Bats note: a `[[ ... ]]` and a `! cmd` that are not the LAST statement of a
# @test do NOT fail the test. Intermediate assertions go through the helpers
# below, whose `return 1` does gate. A bare `[ ... ]` does gate (it is a simple
# command under errexit), so those are left as they are.

# Stub of the `ls -lnd` used by the lib.sh source guard: the script runs as a
# simulated root and the guard requires a root-owned tree.
load ../../debian13/tests/source_guard_stub

assert_contains() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

# Runs the script under test passing CONFIG_DIR only to the immediate process
# (via env, not an export from setup): that is enough for cloud-init-worker.sh
# itself to write into this test's tmpdir instead of /etc/orcaster, and it is
# separate from the path that exercises the export finding (see the dedicated
# test below, which deliberately does NOT go through here).
run_cloud_init() {
    run env CONFIG_DIR="$CONFIG_DIR" bash "$SCRIPT"
}

# $1 = ERE matched against the mock call log.
assert_call() {
    grep -qE "$1" "$CALLS" || { echo "expected '$1' in the calls:"; cat "$CALLS"; return 1; }
}

refute_call() {
    ! grep -qE "$1" "$CALLS" || { echo "did not expect '$1' in the calls:"; cat "$CALLS"; return 1; }
}

setup() {
    REAL_LIB="${BATS_TEST_DIRNAME}/../lib.sh"
    export REAL_LIB
    # Physical path (pwd -P): the source guard requires the path the script is
    # invoked through to match the physical one, and on macOS mktemp returns a
    # path under /var, which is a link to /private/var.
    T="$(cd "$(mktemp -d)" && pwd -P)"
    export INSTALL_DIR="$T/repo"
    # Deliberately NOT exported: if this setup exported it, CONFIG_DIR would be
    # in the bats environment BEFORE the script is invoked, and every child the
    # script starts would inherit it through normal environment inheritance,
    # without cloud-init-worker.sh having to export it itself. Such a test
    # would never detect the missing export in the script (false green).
    # run_cloud_init() above passes the value only to the immediate process for
    # the tests that do not exercise this.
    CONFIG_DIR="$T/etc"
    CALLS="$T/calls.log"; export CALLS
    touch "$CALLS"

    # Minimal script CONFIG (seams via env).
    export ORCH_BASE_URL="https://orch.example.com"
    export ORCH_ENROLLMENT_TOKEN="tok-fleet"
    export CLOUD_INIT_LOG="$T/cloud-init.log"

    # Steady-state host: the user exists and the worker image is baked in. It
    # is the only scenario in which cloud-init does anything under tarball
    # deployment; the tests that exercise the abort turn off one of the two
    # conditions (no RELEASE installed, or no image).
    export ORCASTER_EXISTS=1 PODMAN_IMAGE_EXISTS=1

    # Seams for the wait on the lingering runtime dir (see cloud-init-worker.sh).
    # No waiting by default: on macOS /run/user does not exist and we do not
    # want the existing tests to hang or depend on a real systemd-logind.
    export RUNTIME_DIR_WAIT=0
    export RUN_USER_BASE="$T/run"

    # Stubs of the suite the script orchestrates (they record calls).
    STUBS="$T/stubs"; export STUBS
    mkdir -p "$STUBS"
    # They also record the CONFIG_DIR they see in their own environment: a
    # child that did NOT receive it shows it as <unset>.
    # assert_call/refute_call match by substring, so this addition does not
    # break any existing assert on these lines.
    cat > "$STUBS/run-all-worker.sh" <<'S'
#!/usr/bin/env bash
echo "run-all-worker $* CONFIG_DIR=${CONFIG_DIR:-<unset>}" >> "$CALLS"
S
    cat > "$STUBS/run-fleet.sh" <<'S'
#!/usr/bin/env bash
echo "run-fleet CONFIG_DIR=${CONFIG_DIR:-<unset>}" >> "$CALLS"
S
    chmod +x "$STUBS/run-all-worker.sh" "$STUBS/run-fleet.sh"

    # Root-owned tree the script runs from (on the host,
    # /root/orcaster): cloud-init-worker.sh, lib.sh, the
    # WORKER_RO_MOUNTS validator and the run-all-worker.sh/run-fleet.sh stubs,
    # which the script takes from its own directory and not from INSTALL_DIR.
    SUITE="$T/src/setup-scripts/debian13-worker"
    mkdir -p "$SUITE"
    cp "${BATS_TEST_DIRNAME}/../cloud-init-worker.sh" "$REAL_LIB" \
       "${BATS_TEST_DIRNAME}/../check-ro-mounts.sh" \
       "$STUBS/run-all-worker.sh" "$STUBS/run-fleet.sh" "$SUITE/"
    SCRIPT="$SUITE/cloud-init-worker.sh"

    # Mocks on PATH.
    mkdir -p "$T/bin"
    cat > "$T/bin/id" <<'M'
#!/usr/bin/env bash
# id -u => 0 (root). id [-u] orcaster => depends on ORCASTER_EXISTS.
if [ "${1:-}" = "-u" ] && [ -z "${2:-}" ]; then echo 0; exit 0; fi
if [ "${1:-}" = "-u" ]; then
    if [ "${ORCASTER_EXISTS:-0}" = 1 ]; then echo 1001; exit 0; else exit 1; fi
fi
if [ "${ORCASTER_EXISTS:-0}" = 1 ]; then exit 0; else exit 1; fi
M
    cat > "$T/bin/sudo" <<'M'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
# Reading the RELEASE as the worker (read_release_file): runs the cat.
if [ "${1:-}" = "-u" ] && [ "${3:-}" = "cat" ]; then shift 2; exec "$@"; fi
# Rootless image check (golden snapshot detection).
# When REQUIRE_HOME=1, it requires sudo to carry -H (sets HOME to the
# target's home): it simulates a sudoers with env_keep += HOME, where without
# -H orcaster's rootless podman would inherit root's HOME and look at the
# wrong storage (image "absent" => false bootstrap).
if [ "${REQUIRE_HOME:-0}" = 1 ]; then
    case " $* " in *" -H "*) : ;; *) exit 1 ;; esac
fi
# When REQUIRE_RUNTIME_DIR=1, it also requires the XDG_RUNTIME_DIR passed by
# the script to actually exist (simulates rootless podman failing without its
# runtime dir); this allows testing the wait without depending on real timing.
if [ "${REQUIRE_RUNTIME_DIR:-0}" = 1 ]; then
    dir=""
    for arg in "$@"; do
        case "$arg" in
            XDG_RUNTIME_DIR=*) dir="${arg#XDG_RUNTIME_DIR=}" ;;
        esac
    done
    { [ -n "$dir" ] && [ -d "$dir" ]; } || exit 1
fi
if [ "${PODMAN_IMAGE_EXISTS:-0}" = 1 ]; then exit 0; else exit 1; fi
M
    # apt-get and git are tripwires: the script no longer installs packages or
    # clones, so any invocation is recorded in CALLS and the tests expose it.
    # They are mocked anyway so that a regression does not reach the network or
    # touch the system of the machine running the suite.
    cat > "$T/bin/apt-get" <<'M'
#!/usr/bin/env bash
echo "apt-get $*" >> "$CALLS"
exit 0
M
    cat > "$T/bin/git" <<'M'
#!/usr/bin/env bash
echo "git $*" >> "$CALLS"
exit 0
M
    # The curl mock fails the first CURL_FAILS invocations (default 0), with a
    # counter persisted next to CALLS: it allows testing the healthz preflight
    # retries.
    cat > "$T/bin/curl" <<'M'
#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
n="$(cat "$CALLS.curl" 2>/dev/null || echo 0)"; n=$((n + 1)); echo "$n" > "$CALLS.curl"
[ "$n" -gt "${CURL_FAILS:-0}" ] || exit 1
M
    # install of the config dir: the script creates it 0750 root:orcaster, and
    # no `-o root` works when running the suite as a normal user.
    # Before hardening, the seam was the script's own fallback (`|| install -d
    # -m 0750` without -o/-g), which in production manufactured a root:root dir
    # that the worker cannot traverse: a fallback that only existed for the
    # tests. The seam is now this mock, which records the FULL call (the tests
    # assert on -o/-g) and delegates to the real install without the ownership
    # flags.
    # INSTALL_FAILS=1 fails ONLY the form with -o/-g and lets the form without
    # ownership through: that is the production condition (nobody can `-o root`
    # without being root) and the only one that discriminates. A mock that
    # failed both would pass with the fallback put back, because the fallback
    # would die too.
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
    # chown/chmod of setup.env: the worker runs as orcaster and reads it
    # through the group, so a failure aborts instead of continuing silently.
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
    chmod +x "$T/bin/id" "$T/bin/sudo" "$T/bin/apt-get" "$T/bin/git" "$T/bin/curl" \
             "$T/bin/chown" "$T/bin/chmod" "$T/bin/install"
    write_ls_stub "$T/bin"
    export PATH="$T/bin:$PATH"
}

teardown() { rm -rf "$T"; }

# Code installed by push-release.sh in INSTALL_DIR (the worker's), WITHOUT .git.
# The script no longer runs anything from there: it only looks at the RELEASE,
# which is written separately on purpose to simulate a host with code but
# without a pushed release.
make_installed() {
    mkdir -p "$INSTALL_DIR/setup-scripts/debian13-worker"
    echo "installed-code" > "$INSTALL_DIR/marker.txt"
}

# Release manifest, in the same format that make-release.sh embeds.
make_release_file() {
    printf 'version=1.0.0\ncommit=abc123\nbuilt=2026-08-18T00:00:00Z\n' \
        > "$INSTALL_DIR/RELEASE"
}

# Steady-state host: installed code + RELEASE, which is what detection looks at.
make_installed_release() {
    make_installed
    make_release_file
}

@test "cloud-init: with RELEASE and image present it provisions and starts the fleet" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    # --from 02 skips the apt/user preflight: the host is already provisioned
    # (it is the only way to have RELEASE and image).
    assert_call 'run-all-worker --from 02'
    assert_call 'run-fleet'
}

@test "cloud-init: declares the installed release before provisioning" {
    # Replaces the identity signal that the checkout ref used to give: the
    # first-boot log has to say WHICH code started the fleet.
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    assert_contains "$output" "version=1.0.0"
    assert_contains "$output" "commit=abc123"
}

@test "cloud-init: reads the RELEASE once and as orcaster, not as root" {
    # INSTALL_DIR belongs to the worker: root does not follow what that user
    # leaves there (a RELEASE that was a link to a root file would end up in
    # the log).
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    assert_call "^sudo -u orcaster cat -- $INSTALL_DIR/RELEASE\$"
    [ "$(grep -c ' cat -- ' "$CALLS")" -eq 1 ]
    assert_contains "$output" "commit=abc123"
}

@test "cloud-init: a RELEASE that orcaster cannot read counts as absent" {
    make_installed_release
    chmod 000 "$INSTALL_DIR/RELEASE"
    run_cloud_init
    /bin/chmod 644 "$INSTALL_DIR/RELEASE"
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "push-release"
}

@test "cloud-init: creates its log as 0600" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    [ -f "$CLOUD_INIT_LOG" ]
    [ "$(/bin/ls -ln "$CLOUD_INIT_LOG" | cut -c1-10)" = "-rw-------" ]
}

@test "cloud-init: a previous 0644 log becomes 0600" {
    make_installed_release
    : > "$CLOUD_INIT_LOG"; /bin/chmod 644 "$CLOUD_INIT_LOG"
    run_cloud_init
    [ "$status" -eq 0 ]
    [ "$(/bin/ls -ln "$CLOUD_INIT_LOG" | cut -c1-10)" = "-rw-------" ]
}

@test "cloud-init: the abort also offers the manual provisioning path" {
    # The gate also triggers with the code ALREADY pushed and the image not
    # built: asking that operator to push again does not help.
    make_installed_release
    export PODMAN_IMAGE_EXISTS=0
    run_cloud_init
    [ "$status" -ne 0 ]
    assert_contains "$output" "push-release"
    assert_contains "$output" "run-all-worker.sh"
}

@test "cloud-init: the provisioning path does not touch git or apt" {
    # Goal: the host keeps no history or clone credential. The git/apt-get
    # mocks record any invocation.
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    refute_call '^git '
    refute_call '^apt-get '
}

@test "cloud-init: without an installed RELEASE it aborts asking for a pushed release" {
    # Code present but no manifest: half-migrated host or old snapshot.
    make_installed
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "push-release"
}

@test "cloud-init: without the baked worker image it aborts as well" {
    # The other half of the detection: the snapshot has the code but the image
    # was not baked in, so the host cannot start anything.
    make_installed_release
    export PODMAN_IMAGE_EXISTS=0
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "push-release"
}

@test "cloud-init: materializes setup.env in the config dir and not in the checkout" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    [ -f "$CONFIG_DIR/setup.env" ]
    run test -f "$INSTALL_DIR/setup-scripts/debian13-worker/setup.env"
    [ "$status" -ne 0 ]
    run test -f "$SUITE/setup.env"
    [ "$status" -ne 0 ]
}

@test "setup.env: no longer declares clone keys" {
    # The host does not clone: the keys for the code origin, the ref and the
    # read token no longer exist and must not come back through the back door
    # of the materialized setup.env.
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    run grep -qE '^(REPO|GIT)_[A-Z_]+=' "$CONFIG_DIR/setup.env"
    [ "$status" -ne 0 ]
}

@test "cloud-init: leaves setup.env as 0640 root:orcaster" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    assert_call "chown root:orcaster $CONFIG_DIR/setup.env"
    assert_call "chmod 0640 $CONFIG_DIR/setup.env"
}

# --- Config dir permissions: no silent fallbacks -----
# The worker runs as orcaster and rootless podman reads --env-file as that
# user: if the dir is not traversable or the file is not readable by the
# group, none of them starts. The three failures below used to be silent and
# now abort.

@test "cloud-init: creates the config dir as 0750 root:orcaster" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    # With explicit -o/-g: the fallback that dropped them left the dir
    # root:root 0750, which is exactly the state the worker cannot traverse.
    assert_call "install -d -m 0750 -o root -g orcaster $CONFIG_DIR"
}

@test "cloud-init: if it cannot create the dir with an owner it aborts, it does not create it without one" {
    # The fallback that used to be here (`|| install -d -m 0750` without
    # -o/-g) dropped the ownership and left the dir root:root: it exists, but
    # orcaster cannot traverse it and no worker starts. We prefer to abort with
    # the cause.
    make_installed_release
    export INSTALL_FAILS=1
    run_cloud_init
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not create $CONFIG_DIR as 0750 root:orcaster"
    # No second attempt without -o/-g.
    refute_call 'install -d -m 0750 [^-]'
    # And it does not continue: no provisioning on a dir the worker cannot read.
    refute_call "run-fleet"
}

@test "cloud-init: if the chown of setup.env fails it aborts with the cause" {
    # It used to be best-effort, defended on the grounds that 00-preflight.sh
    # reconciles the mode on any full rerun. On THIS path it does not: the
    # startup calls run-all-worker.sh --from 02, which skips exactly
    # 00-preflight.
    make_installed_release
    export CHOWN_FAILS=1
    run_cloud_init
    [ "$status" -ne 0 ]
    assert_contains "$output" "0640 root:orcaster"
    refute_call "run-fleet"
}

@test "cloud-init: if the chmod of setup.env fails it aborts with the cause" {
    # With umask 077 the file is born 0600: without the chmod, the orcaster
    # group cannot read it even if the chown worked.
    make_installed_release
    export CHMOD_FAILS=1
    run_cloud_init
    [ "$status" -ne 0 ]
    assert_contains "$output" "0640 root:orcaster"
    refute_call "run-fleet"
}

@test "detection: waits for the lingering runtime dir before deciding" {
    make_installed_release
    export REQUIRE_RUNTIME_DIR=1
    export RUNTIME_DIR_WAIT=3
    export RUN_USER_BASE="$T/run"
    # The dir does NOT exist yet (race between cloud-init and the lingering
    # session); a background process creates it 1s later, as systemd-logind
    # would. With REQUIRE_RUNTIME_DIR=1 the sudo mock requires the dir to
    # really exist, so if the script does NOT wait, detection fails,
    # need_bootstrap stays at 1 and the script aborts: the only way to reach
    # status 0 with --from 02 is to have waited for the background process to
    # create the dir.
    ( sleep 1; mkdir -p "$T/run/1001" ) &
    run_cloud_init
    [ "$status" -eq 0 ]
    assert_call 'run-all-worker --from 02'
    assert_call 'run-fleet'
}

@test "detection: the image check sets the worker HOME (sudo -H)" {
    # Without -H, whether HOME points to orcaster's home depends on the
    # sudoers env_reset; with env_keep += HOME rootless podman would look at
    # root's storage, the image would show as "absent" and a healthy host would
    # abort asking for a release it already has.
    make_installed_release
    export REQUIRE_HOME=1
    run_cloud_init
    [ "$status" -eq 0 ]
    grep -q 'run-all-worker --from 02' "$CALLS"
}

@test "preflight healthz: retries on transient failures" {
    make_installed_release
    export RETRY_DELAY=0 CURL_FAILS=2
    run_cloud_init
    [ "$status" -eq 0 ]
    [ "$(grep -c '^curl' "$CALLS")" -eq 3 ]
}

@test "preflight healthz down: aborts after exhausting the 5 retries" {
    make_installed_release
    export RETRY_DELAY=0 CURL_FAILS=99
    run_cloud_init
    [ "$status" -ne 0 ]
    [ "$(grep -c '^curl' "$CALLS")" -eq 5 ]
    assert_contains "$output" "healthz"
}

@test "ORCH_BASE_URL placeholder => aborts early with a clear message" {
    make_installed_release
    unset ORCH_BASE_URL
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "ORCH_BASE_URL"
}

@test "EGRESS_ISOLATION invalid => aborts before provisioning" {
    make_installed_release
    export EGRESS_ISOLATION=true
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "EGRESS_ISOLATION"
}

@test "WORKER_MEMORY invalid => aborts before provisioning" {
    make_installed_release
    export WORKER_MEMORY=lots
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "WORKER_MEMORY"
}

@test "WORKER_MEMORY without suffix (plain bytes) => aborts early" {
    # Almost certainly a typo for '28g': 28 bytes is below podman's minimum and
    # the container is not even created. Same contract as load_env.
    make_installed_release
    export WORKER_MEMORY=28
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "WORKER_MEMORY"
}

@test "WORKER_MEMORY zero => aborts early" {
    # '0g' satisfies number+suffix but is below podman's minimum.
    # Same contract as load_env.
    make_installed_release
    export WORKER_MEMORY=0g
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "WORKER_MEMORY"
}

@test "setup.env: round-trip of EXTRA_ENV and PEM with metacharacters" {
    make_installed_release
    EXTRA_ENV='API_TOKEN=tok$x "q" `t`
GITHUB_TOKEN=ghp_y'
    SERVER_ED25519_PUBKEY='-----BEGIN PUBLIC KEY-----
AbC=
-----END PUBLIC KEY-----'
    export EXTRA_ENV SERVER_ED25519_PUBKEY
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    [ -f "$setup_env" ]
    got_extra="$(env -i bash -c "source '$setup_env'; printf %s \"\$EXTRA_ENV\"")"
    got_pem="$(env -i bash -c "source '$setup_env'; printf %s \"\$SERVER_ED25519_PUBKEY\"")"
    [ "$got_extra" = "$EXTRA_ENV" ]
    [ "$got_pem" = "$SERVER_ED25519_PUBKEY" ]
}

@test "setup.env: materializes EGRESS_ISOLATION with default 1" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$EGRESS_ISOLATION\"")"
    [ "$got" = "1" ]
}

@test "setup.env: honors EGRESS_ISOLATION=0 from the CONFIG" {
    make_installed_release
    export EGRESS_ISOLATION=0
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$EGRESS_ISOLATION\"")"
    [ "$got" = "0" ]
}

@test "setup.env: materializes WORKER_MEMORY with empty default (no cap)" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    grep -q '^WORKER_MEMORY=' "$setup_env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$WORKER_MEMORY\"")"
    [ -z "$got" ]
}

@test "setup.env: honors WORKER_MEMORY from the CONFIG" {
    make_installed_release
    export WORKER_MEMORY=28g
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$WORKER_MEMORY\"")"
    [ "$got" = "28g" ]
}

# --- WORKER_RO_MOUNTS --------------------------------------------
# cloud-init validates the SYNTAX with the same check-ro-mounts.sh as load_env,
# before writing setup.env and starting anything. The host state is measured by
# run-fleet.sh (a stub here) as the worker user.

@test "setup.env: materializes WORKER_RO_MOUNTS with empty default (no mounts)" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    grep -q '^WORKER_RO_MOUNTS=' "$setup_env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$WORKER_RO_MOUNTS\"")"
    [ -z "$got" ]
}

@test "setup.env: honors WORKER_RO_MOUNTS from the CONFIG" {
    make_installed_release
    export WORKER_RO_MOUNTS="/opt/example-ro /srv/example-ro/snapshots"
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$WORKER_RO_MOUNTS\"")"
    [ "$got" = "/opt/example-ro /srv/example-ro/snapshots" ]
}

@test "WORKER_RO_MOUNTS invalid => aborts before writing setup.env and provisioning" {
    make_installed_release
    export WORKER_RO_MOUNTS="/opt/example-ro /home"
    run_cloud_init
    [ "$status" -ne 0 ]
    assert_contains "$output" "'/home' is, is under, or contains /home/orcaster"
    refute_call 'run-all-worker'
    refute_call 'run-fleet'
    [ ! -e "$CONFIG_DIR/setup.env" ]
}

# --- Worker disk gate -----------------------------------

@test "setup.env: materializes the disk gate with empty default" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION\"")"
    [ -z "$got" ]
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS\"")"
    [ -z "$got" ]
}

@test "setup.env: honors the disk gate floor from the CONFIG" {
    make_installed_release
    export ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION\"")"
    [ "$got" = "0.05" ]
}

@test "setup.env: honors the gate fail-open deadline from the CONFIG" {
    make_installed_release
    export ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS\"")"
    [ "$got" = "600" ]
}

@test "invalid disk gate (floor outside 0..1) in cloud-init aborts before provisioning" {
    make_installed_release
    export ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=1.5
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION"
}

@test "invalid disk gate (non-numeric deadline) in cloud-init aborts before provisioning" {
    make_installed_release
    export ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=half-hour
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    assert_contains "$output" "ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS"
}

# --- Templated CONFIG_DIR reaches the child processes --

@test "CONFIG_DIR templated in the CONFIG reaches run-all-worker.sh and run-fleet.sh exported" {
    # Deliberately does NOT use run_cloud_init: that helper passes CONFIG_DIR
    # through the immediate process environment (env CONFIG_DIR=... bash
    # "$SCRIPT"), and that environment inheritance alone would be enough for
    # the children to see it too, without cloud-init-worker.sh having to export
    # it. Such a test would never detect the missing export (false green): it
    # is exactly what the `export CONFIG_DIR="$T/etc"` of the old setup() used
    # to mask.
    #
    # In production the operator templates the CONFIG block as TEXT of the
    # script itself (never as an environment variable): cloud-init runs the
    # user-data as is, with nothing set in its environment. That is reproduced
    # here: the DEFAULT of the CONFIG_DIR line is changed in a copy of the
    # script and it is run without CONFIG_DIR in the bats environment or in
    # the immediate child.
    make_installed_release
    local custom_dir="$T/srv-orcaster"
    local templated="$SUITE/cloud-init-templated.sh"
    sed "s#CONFIG_DIR:=/etc/orcaster#CONFIG_DIR:=$custom_dir#" "$SCRIPT" > "$templated"
    chmod +x "$templated"

    run env -u CONFIG_DIR bash "$templated"
    [ "$status" -eq 0 ]
    # The children see the same CONFIG_DIR that was templated, not <unset> or
    # the /etc/orcaster default.
    assert_call "run-all-worker --from 02 CONFIG_DIR=$custom_dir"
    assert_call "run-fleet CONFIG_DIR=$custom_dir"
    refute_call 'CONFIG_DIR=<unset>'
}

# --- Isolation of the task environment -----------------------

@test "setup.env: without flag or list in the CONFIG it does not write those lines" {
    make_installed_release
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    run grep -qE '^(ORCH_ENFORCE_ISOLATION|ISOLATION_REQUIRED_ENV)=' "$setup_env"
    [ "$status" -ne 0 ]
}

@test "setup.env: materializes ORCH_ENFORCE_ISOLATION from the CONFIG" {
    make_installed_release
    export ORCH_ENFORCE_ISOLATION=false
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$ORCH_ENFORCE_ISOLATION\"")"
    [ "$got" = "false" ]
}

@test "setup.env: materializes normalized ISOLATION_REQUIRED_ENV" {
    make_installed_release
    export ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR, OTHER_VAR'
    run_cloud_init
    [ "$status" -eq 0 ]
    setup_env="$CONFIG_DIR/setup.env"
    got="$(env -i bash -c "source '$setup_env'; printf %s \"\$ISOLATION_REQUIRED_ENV\"")"
    [ "$got" = "EXAMPLE_PROTECTED_VAR,OTHER_VAR" ]
}

@test "ORCH_ENFORCE_ISOLATION invalid => aborts before provisioning" {
    make_installed_release
    export ORCH_ENFORCE_ISOLATION=yes
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    [ ! -e "$CONFIG_DIR/setup.env" ]
    assert_contains "$output" "ORCH_ENFORCE_ISOLATION"
}

@test "ISOLATION_REQUIRED_ENV malformed => aborts before writing setup.env" {
    make_installed_release
    export ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR,9BAD'
    run_cloud_init
    [ "$status" -ne 0 ]
    refute_call 'run-all-worker'
    [ ! -e "$CONFIG_DIR/setup.env" ]
    assert_contains "$output" "9BAD"
}
