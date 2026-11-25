#!/usr/bin/env bats
# systemd user bus in `make up client` / `make up proxy`.
#
# The defect: from a `sudo -iu orcaster` shell there is no XDG_RUNTIME_DIR or
# DBUS_SESSION_BUS_ADDRESS, every `systemctl --user` fails to connect to the
# bus, and the recipe read that failure as "inactive unit": it skipped the
# stop, recreated the container and left the manager with a stale MainPID. The
# next `daemon-reload` that did reach the bus stopped the worker.
#
# Three layers, all against a FAKE `systemctl` and `podman` on the PATH:
#   - user-bus.sh on its own (environment derivation, bus probe, stop);
#   - real `make up-client` / `make up-proxy`: the cut happens BEFORE the
#     `podman rm -f`;
#   - 06-autostart.sh with RUN_AS= (the path of the AUTOSTART_SYNC hook): the
#     cut happens BEFORE rewriting the unit file.
#
# The fake systemctl is driven by the environment:
#   FAKE_BUS=up         the manager always responds (default);
#   FAKE_BUS=down       every --user command fails like an unreachable bus;
#   FAKE_BUS=needs-env  responds only if XDG_RUNTIME_DIR arrives (what the
#                       real one does: the manager verbs use
#                       $XDG_RUNTIME_DIR/systemd/private);
#   FAKE_ACTIVE=1|0     answer of `is-active` (default 1);
#   FAKE_STATE=<state>  state printed by `is-active` with exit 3 (wins over
#                       FAKE_ACTIVE);
#   FAKE_STOP_RC=<n>    exit code of `stop` (default 0).
# Each call is recorded in $CALLS with the bus environment it received.
#
# bats gotchas in this repo: a `[[ ... ]]` or a `! cmd` that is not the last
# statement of a @test does NOT make it fail. Assertions go through these
# helpers and grep negations through `run` + `$status`.
assert_out() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

refute_out() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected '$2' in the output:"; echo "$1"; return 1; }
}

refute_calls() {
    run grep -q -- "$1" "$CALLS"
    [ "$status" -ne 0 ] || { echo "unexpected call '$1':"; cat "$CALLS"; return 1; }
}

assert_calls() {
    grep -q -- "$1" "$CALLS" || { echo "missing call '$1':"; cat "$CALLS"; return 1; }
}

# $1 appears in $CALLS before $2 (first occurrence of each).
assert_order() {
    local a b
    a="$(grep -n -m1 -- "$1" "$CALLS" | cut -d: -f1)"
    b="$(grep -n -m1 -- "$2" "$CALLS" | cut -d: -f1)"
    [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ] \
        || { echo "expected '$1' before '$2':"; cat "$CALLS"; return 1; }
}

setup() {
    HERE="$(cd "${BATS_TEST_DIRNAME}/.." && pwd)"
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    HELPER="$HERE/user-bus.sh"
    unset WORKER_RO_MOUNTS WORKER_MEMORY CONFIG_DIR ETC_SETUP_ENV_FILE
    unset XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS FAKE_BUS FAKE_ACTIVE FAKE_STOP_RC FAKE_STATE
    T="$(mktemp -d)"
    CALLS="$T/calls.log"; export CALLS
    : > "$CALLS"
    mkdir -p "$T/bin" "$T/xdg/systemd/user" "$T/run/$(id -u)" "$T/home"
    : > "$T/worker.env"
    : > "$T/server.pub"
    : > "$T/setup.env"

    cat > "$T/bin/systemctl" <<'S'
#!/usr/bin/env bash
echo "systemctl $* [XDG=${XDG_RUNTIME_DIR:-} DBUS=${DBUS_SESSION_BUS_ADDRESS:-}]" >> "$CALLS"
bus="${FAKE_BUS:-up}"
if [ "$bus" = "needs-env" ]; then
    if [ -n "${XDG_RUNTIME_DIR:-}" ]; then bus=up; else bus=down; fi
fi
if [ "$bus" = "down" ]; then
    echo "Failed to connect to bus: No medium found" >&2
    exit 1
fi
case "${2:-}" in
    is-active)
        if [ -n "${FAKE_STATE:-}" ]; then echo "$FAKE_STATE"; exit 3; fi
        if [ "${FAKE_ACTIVE:-1}" = "1" ]; then echo active; exit 0; fi
        echo inactive; exit 3 ;;
    stop) exit "${FAKE_STOP_RC:-0}" ;;
esac
exit 0
S
    cat > "$T/bin/podman" <<'P'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
# up-proxy's `.aardvark-check` reads the DNS version from `podman info`.
if [ "${1:-}" = "info" ]; then echo "aardvark-dns-99.0.0"; fi
if [ "${1:-}" = "container" ] && [ "${2:-}" = "exists" ]; then
    case " ${EXISTING_CONTAINERS:-} " in *" ${3:-} "*) exit 0 ;; *) exit 1 ;; esac
fi
exit 0
P
    chmod +x "$T/bin/systemctl" "$T/bin/podman"

    # Fake SETUP_WORKER_DIR for the real `make` runs: the REAL user-bus.sh (what
    # is under test) and a 06-autostart.sh that only records the call, because
    # the real one requires the host's setup.env. Its own tests are further
    # down.
    mkdir -p "$T/fake-worker"
    ln -s "$HELPER" "$T/fake-worker/user-bus.sh"
    printf '#!/usr/bin/env bash\necho "06-autostart" >> "$CALLS"\nexit 0\n' > "$T/fake-worker/06-autostart.sh"
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

unit_file() { : > "$T/xdg/systemd/user/$1"; }

# user-bus.sh stop-unit against the fake runtime. RUN_USER_BASE is the seam for
# /run/user (same name that cloud-init-worker.sh uses).
stop_unit() {
    PATH="$T/bin:$PATH" XDG_CONFIG_HOME="$T/xdg" RUN_USER_BASE="$T/run" \
        run bash "$HELPER" stop-unit "$@"
}

real_make() {
    PATH="$T/bin:$PATH" XDG_CONFIG_HOME="$T/xdg" RUN_USER_BASE="$T/run" \
        run make -C "$REPO_ROOT" "$@" PODMAN="$T/bin/podman" \
        SETUP_ENV_FILE="$T/setup.env" ETC_SETUP_ENV_FILE="$T/no-existe.env" \
        WORKER_ENV="$T/worker.env" WORKER_PUBKEY="$T/server.pub" \
        SETUP_WORKER_DIR="$T/fake-worker"
}

# --- user-bus.sh stop-unit ---------------------------------------------------

@test "user-bus: unreachable bus with the unit present fails with an error and does not attempt the stop" {
    unit_file container-orcaster-worker.service
    FAKE_BUS=down stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -ne 0 ]
    assert_out "$output" "could not reach the systemd --user manager"
    assert_out "$output" "container-orcaster-worker.service"
    assert_out "$output" "Failed to connect to bus"
    assert_out "$output" "The container orcaster-worker was not touched"
    assert_out "$output" "XDG_RUNTIME_DIR"
    refute_calls 'systemctl --user stop'
}

@test "user-bus: reachable bus and inactive unit exits 0 without stop" {
    unit_file container-orcaster-worker.service
    FAKE_ACTIVE=0 stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    assert_calls 'systemctl --user is-active container-orcaster-worker.service'
    refute_calls 'systemctl --user stop'
}

@test "user-bus: reachable bus and active unit stops it" {
    unit_file container-orcaster-worker.service
    stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    assert_calls 'systemctl --user stop container-orcaster-worker.service'
}

@test "user-bus: a failing stop fails with an error" {
    unit_file container-orcaster-worker.service
    FAKE_STOP_RC=1 stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -ne 0 ]
    assert_out "$output" "could not stop the unit container-orcaster-worker.service"
    assert_out "$output" "The container orcaster-worker was not touched"
}

@test "user-bus: without the variables it derives them from /run/user/<uid> and reaches the manager" {
    unit_file container-orcaster-worker.service
    FAKE_BUS=needs-env stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    local rt="$T/run/$(id -u)"
    assert_calls "systemctl --user stop container-orcaster-worker.service \[XDG=$rt DBUS=unix:path=$rt/bus\]"
}

@test "user-bus: variables that are already set are respected as is" {
    unit_file container-orcaster-worker.service
    XDG_RUNTIME_DIR=/custom/rt DBUS_SESSION_BUS_ADDRESS=unix:path=/custom/bus \
        FAKE_BUS=needs-env stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    assert_calls 'systemctl --user stop container-orcaster-worker.service \[XDG=/custom/rt DBUS=unix:path=/custom/bus\]'
}

@test "user-bus: without a runtime dir it derives nothing and the error names linger" {
    unit_file container-orcaster-worker.service
    rmdir "$T/run/$(id -u)"
    FAKE_BUS=needs-env stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -ne 0 ]
    assert_out "$output" "$T/run/$(id -u) does not exist"
    assert_out "$output" "loginctl enable-linger"
    refute_calls 'systemctl --user stop'
}

@test "user-bus: without the unit it is a no-op even if the bus does not respond" {
    FAKE_BUS=down stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    run grep -c . "$CALLS"
    [ "$output" -eq 0 ]
}

@test "user-bus: an is-active that fails without reporting a state is not read as inactive" {
    # Second layer: the probe passed but `is-active` dies without saying the state.
    unit_file container-orcaster-worker.service
    cat > "$T/bin/systemctl" <<'S'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
if [ "${2:-}" = "is-active" ]; then echo "Failed to connect to bus: Connection refused" >&2; exit 1; fi
exit 0
S
    stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -ne 0 ]
    assert_out "$output" "Connection refused"
    refute_calls 'systemctl --user stop'
}

@test "user-bus: an activating unit (auto-restart) is also stopped" {
    # `is-active` exits 3 for activating: reading it as inactive would recreate
    # the container against the Restart of a worker in a crash loop.
    unit_file container-orcaster-worker.service
    FAKE_STATE=activating stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    assert_calls 'systemctl --user stop container-orcaster-worker.service'
}

@test "user-bus: a failed unit is not stopped" {
    unit_file container-orcaster-worker.service
    FAKE_STATE=failed stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    refute_calls 'systemctl --user stop'
}

@test "user-bus: without HOME or XDG_CONFIG_HOME it is a no-op, not a bash error" {
    run env -i PATH="$T/bin:/usr/bin:/bin" CALLS="$CALLS" FAKE_BUS=down \
        bash "$HELPER" stop-unit container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "user-bus: inherited variables that do not work are named as the cause" {
    unit_file container-orcaster-worker.service
    XDG_RUNTIME_DIR=/custom/rt FAKE_BUS=down stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -ne 0 ]
    assert_out "$output" "came from the caller's environment"
}

@test "user-bus: derived variables with the manager down do not suggest repeating the same environment" {
    unit_file container-orcaster-worker.service
    FAKE_BUS=down stop_unit container-orcaster-worker.service orcaster-worker
    [ "$status" -ne 0 ]
    assert_out "$output" "the variables were derived from $T/run/$(id -u)"
    assert_out "$output" "sudo systemctl start user@$(id -u).service"
}

@test "user-bus check: probes without stopping" {
    unit_file container-orcaster-worker.service
    PATH="$T/bin:$PATH" XDG_CONFIG_HOME="$T/xdg" RUN_USER_BASE="$T/run" \
        run bash "$HELPER" check container-orcaster-worker.service orcaster-worker
    [ "$status" -eq 0 ]
    assert_calls 'systemctl --user show-environment'
    refute_calls 'is-active'
    refute_calls 'systemctl --user stop'
}

# --- make up-client / up-proxy -----------------------------------------------

@test "up-client: unreachable bus with the unit present stops BEFORE the podman rm" {
    unit_file container-orcaster-worker.service
    FAKE_BUS=down real_make up-client EGRESS_ISOLATION=0
    [ "$status" -ne 0 ]
    assert_out "$output" "could not reach the systemd --user manager"
    assert_out "$output" "The container orcaster-worker was not touched"
    refute_calls 'podman rm'
    refute_calls 'podman run'
}

@test "up-client: reachable bus and inactive unit recreates without stop" {
    unit_file container-orcaster-worker.service
    FAKE_ACTIVE=0 real_make up-client EGRESS_ISOLATION=0
    [ "$status" -eq 0 ]
    assert_calls 'podman rm -f orcaster-worker'
    assert_calls 'podman run -d'
    refute_calls 'systemctl --user stop'
}

@test "up-client: from a shell without bus variables it derives them and stops the unit" {
    unit_file container-orcaster-worker.service
    FAKE_BUS=needs-env real_make up-client EGRESS_ISOLATION=0
    [ "$status" -eq 0 ]
    local rt="$T/run/$(id -u)"
    assert_calls "systemctl --user stop container-orcaster-worker.service \[XDG=$rt DBUS=unix:path=$rt/bus\]"
    assert_calls 'podman rm -f orcaster-worker'
    # The stop goes BEFORE the rm, and the re-sync after the run.
    assert_order 'systemctl --user stop container-orcaster-worker.service' 'podman rm -f orcaster-worker'
    assert_order 'podman run -d' '06-autostart'
}

@test "up-client N=2: the cut names the suffixed unit and container" {
    unit_file container-orcaster-worker-2.service
    FAKE_BUS=down real_make up-client EGRESS_ISOLATION=0 N=2
    [ "$status" -ne 0 ]
    assert_out "$output" "container-orcaster-worker-2.service"
    assert_out "$output" "The container orcaster-worker-2 was not touched"
    refute_calls 'podman rm'
}

@test "up-client EGRESS_ISOLATION=1: unreachable bus with only the worker unit does not even recreate the sidecar" {
    # up-proxy is a prerequisite and runs before the up-client recipe: without
    # the prior probe, the sidecar (without its own unit) was recreated and the
    # cut came later.
    unit_file container-orcaster-worker.service
    FAKE_BUS=down real_make up-client EGRESS_ISOLATION=1
    [ "$status" -ne 0 ]
    assert_out "$output" "The container orcaster-worker was not touched"
    refute_calls 'podman rm'
    refute_calls 'podman run'
}

@test "up-client EGRESS_ISOLATION=1: unreachable bus with only the sidecar unit stops before the build" {
    unit_file container-orcaster-proxy.service
    FAKE_BUS=down real_make up-client EGRESS_ISOLATION=1
    [ "$status" -ne 0 ]
    assert_out "$output" "The container orcaster-proxy was not touched"
    refute_calls 'podman build'
    refute_calls 'podman rm'
    refute_calls 'podman run'
}

@test "up-client EGRESS_ISOLATION=1: if the worker stop fails it warns that the sidecar was already recreated" {
    unit_file container-orcaster-worker.service
    unit_file container-orcaster-proxy.service
    # The proxy stop works; the worker's fails.
    cat > "$T/bin/systemctl" <<'S'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
case "${2:-}" in
    is-active) echo active; exit 0 ;;
    stop) case "${3:-}" in *worker*) echo "Job failed" >&2; exit 1 ;; esac ;;
esac
exit 0
S
    real_make up-client EGRESS_ISOLATION=1
    [ "$status" -ne 0 ]
    assert_out "$output" "could not stop the unit container-orcaster-worker.service"
    assert_out "$output" "The orcaster-proxy sidecar WAS recreated"
    refute_calls 'podman rm -f orcaster-worker'
}

@test "up-proxy: unreachable bus with the sidecar unit present stops BEFORE the podman rm" {
    unit_file container-orcaster-proxy.service
    FAKE_BUS=down real_make up-proxy
    [ "$status" -ne 0 ]
    assert_out "$output" "could not reach the systemd --user manager"
    assert_out "$output" "The container orcaster-proxy was not touched"
    refute_calls 'podman rm'
    refute_calls 'podman run'
}

@test "up-proxy: reachable bus and active unit stops and recreates the sidecar" {
    unit_file container-orcaster-proxy.service
    real_make up-proxy
    [ "$status" -eq 0 ]
    assert_calls 'systemctl --user stop container-orcaster-proxy.service'
    assert_calls 'podman rm -f orcaster-proxy'
}

# `make -f /abs/path/Makefile` from a cwd outside the repo is a supported way
# to invoke it (see RO_MOUNTS_CHECK in the Makefile). SETUP_WORKER_DIR is
# relative to the repo, so user-bus.sh has to be anchored to the Makefile and
# not to the cwd: without that the probe died with "No such file or directory"
# on EVERY host, even without an autostart unit. SETUP_WORKER_DIR is NOT
# overridden here on purpose.
foreign_cwd_make() {
    mkdir -p "$T/foreign"
    PATH="$T/bin:$PATH" XDG_CONFIG_HOME="$T/xdg" RUN_USER_BASE="$T/run" \
        run make -C "$T/foreign" -f "$REPO_ROOT/Makefile" "$@" PODMAN="$T/bin/podman" \
        SETUP_ENV_FILE="$T/setup.env" ETC_SETUP_ENV_FILE="$T/no-existe.env" \
        WORKER_ENV="$T/worker.env" WORKER_PUBKEY="$T/server.pub"
}

@test "make -f absolute Makefile from a foreign cwd: the bus probe finds user-bus.sh" {
    unit_file container-orcaster-worker.service
    foreign_cwd_make .autostart-bus-check EGRESS_ISOLATION=0
    [ "$status" -eq 0 ]
    refute_out "$output" "No such file"
    assert_calls 'systemctl --user show-environment'
}

@test "make -f absolute Makefile from a foreign cwd: without a unit the probe is a no-op, not an error" {
    foreign_cwd_make .autostart-bus-check EGRESS_ISOLATION=0
    [ "$status" -eq 0 ]
    refute_out "$output" "No such file"
}

@test "make -f absolute Makefile from a foreign cwd: unreachable bus stops with the user-bus.sh message" {
    unit_file container-orcaster-worker.service
    FAKE_BUS=down foreign_cwd_make .autostart-bus-check EGRESS_ISOLATION=0
    [ "$status" -ne 0 ]
    refute_out "$output" "No such file"
    assert_out "$output" "could not reach the systemd --user manager"
}

@test "up-client AUTOSTART_SYNC=0: does not query the manager (run-fleet path)" {
    unit_file container-orcaster-worker.service
    FAKE_BUS=down real_make up-client EGRESS_ISOLATION=0 AUTOSTART_SYNC=0
    [ "$status" -eq 0 ]
    refute_calls 'systemctl'
    assert_calls 'podman run -d'
}

# --- 06-autostart.sh with RUN_AS= (AUTOSTART_SYNC hook) -----------------------

# Minimal lib.sh stub: the real one requires setup.env and root helpers.
autostart_lib() {
    cat > "$T/lib.sh" <<'LIB'
step() { :; }
log()  { :; }
ok()   { :; }
warn() { :; }
die()  { echo "DIE: $*" >&2; exit 1; }
need_root() { :; }
load_env() { WORKER_USER=orcaster; WORKER_HOME=/home/orcaster; }
worker_name() { local n="${1:-}"; if [ -n "$n" ]; then printf 'orcaster-worker-%s\n' "$n"; else printf 'orcaster-worker\n'; fi; }
worker_unit() { printf 'container-%s.service\n' "$(worker_name "${1:-}")"; }
proxy_name() { local n="${1:-}"; if [ -n "$n" ]; then printf 'orcaster-proxy-%s\n' "$n"; else printf 'orcaster-proxy\n'; fi; }
proxy_unit() { printf 'container-%s.service\n' "$(proxy_name "${1:-}")"; }
LIB
}

run_autostart() {
    run env -i HOME="$T/home" PATH="$T/bin:/usr/bin:/bin" CALLS="$CALLS" \
        LIB_OVERRIDE="$T/lib.sh" RUN_USER_BASE="$T/run" \
        EXISTING_CONTAINERS="orcaster-worker" RUN_AS= "$@" bash "$HERE/06-autostart.sh"
}

@test "06-autostart RUN_AS=: unreachable bus stops BEFORE rewriting the unit" {
    autostart_lib
    run_autostart FAKE_BUS=down
    [ "$status" -ne 0 ]
    assert_out "$output" "could not reach the systemd --user manager of the worker"
    [ ! -e "$T/home/.config/systemd/user/container-orcaster-worker.service" ]
    refute_calls 'podman generate systemd'
    refute_calls 'daemon-reload'
}

@test "06-autostart RUN_AS=: without bus variables it derives them and completes the daemon-reload" {
    autostart_lib
    run_autostart FAKE_BUS=needs-env
    [ "$status" -eq 0 ]
    local rt="$T/run/$(id -u)"
    assert_calls "systemctl --user daemon-reload \[XDG=$rt DBUS=unix:path=$rt/bus\]"
    assert_calls "systemctl --user enable --now container-orcaster-worker.service \[XDG=$rt"
    [ -f "$T/home/.config/systemd/user/container-orcaster-worker.service" ]
}

@test "06-autostart as root (as_worker): unreachable manager stops BEFORE rewriting the unit" {
    autostart_lib
    cat >> "$T/lib.sh" <<'LIB'
as_worker() {
    echo "as_worker $*" >> "$CALLS"
    if [ "${1:-}" = "podman" ] && [ "${2:-}" = "container" ] && [ "${3:-}" = "exists" ]; then return 0; fi
    if [ "${1:-}" = "systemctl" ] && [ "${3:-}" = "show-environment" ]; then
        echo "Failed to connect to bus: No such file or directory" >&2; return 1
    fi
    return 0
}
LIB
    run env -i HOME="$T/home" PATH="$T/bin:/usr/bin:/bin" CALLS="$CALLS" \
        LIB_OVERRIDE="$T/lib.sh" bash "$HERE/06-autostart.sh"
    [ "$status" -ne 0 ]
    assert_out "$output" "could not reach the systemd --user manager of the worker"
    assert_out "$output" "loginctl enable-linger orcaster"
    refute_calls 'podman generate systemd'
    refute_calls 'daemon-reload'
}
