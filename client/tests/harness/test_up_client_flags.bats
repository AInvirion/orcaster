#!/usr/bin/env bats
# Checks that the `up-client` target of the root Makefile renders the worker
# container hardening flags (cap-drop, read-only, tmpfs, limits, TMPDIR on the
# cache volume). It starts no container: it only inspects the command that
# `make -n` would print.
#
# Note: a `[[ ... ]]` that is not the last statement of a @test does NOT fail
# the test in bats (known bash errexit bug). That is why every assert runs
# through this function, whose `return 1` DOES gate.
assert_contains() {
    [[ "$1" == *"$2"* ]] || { echo "missing: $2"; echo "in: $1"; return 1; }
}

refute_contains() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected: $2"; echo "in: $1"; return 1; }
}

# Sets up the seams to run `autostart-sync-client` FOR REAL (without `-n`),
# without touching the real 06-autostart.sh nor the systemd of whoever runs the
# suite. It takes the exit code the fake 06-autostart.sh must return, and leaves
# three things in $TMPENV: a 06-autostart.sh in $TMPENV/fake-worker (pointed to
# with SETUP_WORKER_DIR, which wins over its `:=`), a systemctl stub in
# $TMPENV/bin (prepended to PATH so that `command -v systemctl` succeeds), and
# the --user unit file in $TMPENV/xdg (pointed to with XDG_CONFIG_HOME) that the
# recipe guard requires to exist. Without all three, the recipe falls into the
# no-op and the failure handling is never exercised.
autostart_seam() {
    local hook_exit="${1:-0}"
    mkdir -p "$TMPENV/fake-worker" "$TMPENV/bin" "$TMPENV/xdg/systemd/user"
    # The recipe invokes it with `bash .../06-autostart.sh`, so it runs without chmod.
    printf '#!/usr/bin/env bash\nexit %s\n' "$hook_exit" > "$TMPENV/fake-worker/06-autostart.sh"
    printf '#!/bin/sh\nexit 0\n' > "$TMPENV/bin/systemctl"
    chmod +x "$TMPENV/bin/systemctl"
    : > "$TMPENV/xdg/systemd/user/container-orcaster-worker.service"
}

setup() {
    cd "$BATS_TEST_DIRNAME/../../.."
    # The WORKER_MEMORY fallback only applies when the variable does not come
    # from the environment nor the command line; a value inherited from the
    # shell of whoever runs bats would invalidate the fallback tests.
    unset WORKER_MEMORY
    # Same reason as WORKER_MEMORY: CONFIG_DIR is declared with `?=` so that the
    # environment wins (cloud-init exports the host's), so a CONFIG_DIR exported
    # in the shell of whoever runs bats would invalidate the default test.
    unset CONFIG_DIR
    TMPENV="$(mktemp -d)"
}

teardown() {
    # `if` and not `[ ... ] && rm`: under errexit a test with an empty TMPENV
    # would fail the teardown through the compound's status 1.
    if [ -n "${TMPENV:-}" ]; then rm -rf "$TMPENV"; fi
}

@test "up-client includes hardening flags" {
    run make -n up-client WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" "--cap-drop=ALL"
    assert_contains "$output" "--security-opt=no-new-privileges"
    assert_contains "$output" "--read-only"
    assert_contains "$output" "--pids-limit"
    # HOME as a tmpfs owned by the worker uid (a root-owned tmpfs breaks `uv`);
    # `--mount ... U=true` because the `--tmpfs` shorthand does not support chown.
    assert_contains "$output" "--mount type=tmpfs,dst=/home/orcaster,tmpfs-size=256m,tmpfs-mode=0700,U=true"
    assert_contains "$output" "TMPDIR=/var/cache/orcaster/tmp"
}

# The unsuffixed worker runs supervised by a systemd --user unit; if it is not
# regenerated on recreation, systemd raises it at boot with the OLD command
# (without the egress flags). up-client hooks the re-synchronization
# (06-autostart.sh) at the end, and stops the active unit before recreating to
# avoid the race with Restart.
@test "up-client re-syncs the autostart after recreating (AUTOSTART_SYNC hook)" {
    run make -n up-client WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" "autostart-sync-client"
    assert_contains "$output" "setup-scripts/debian13-worker/06-autostart.sh"
    # Race guard: it stops the worker's active unit before recreating. The stop
    # goes through user-bus.sh, which tells an unreachable bus apart from an
    # inactive unit and aborts before the `podman rm -f`.
    assert_contains "$output" 'user-bus.sh'"'"' stop-unit "container-orcaster-worker.service" "orcaster-worker"'
    # And it probes the manager as a prerequisite, before recreating the sidecar.
    assert_contains "$output" 'user-bus.sh'"'"' check "container-orcaster-worker.service" "orcaster-worker" || exit 1'
}

@test "up-client AUTOSTART_SYNC=0 does not hook the autostart re-sync" {
    # Seam for run-fleet.sh (which invokes 06-autostart.sh explicitly): the hook
    # is disabled; the target is still invoked but its body exits early.
    run make -n up-client AUTOSTART_SYNC=0 WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" "autostart-sync-client N=\"\" AUTOSTART_SYNC=\"0\""
}

# AUTOSTART_RESYNC=0 (the install-release.sh path) turns off ONLY the final
# re-synchronization: the manager probe and the user-bus.sh stops, which also
# stop units in activating/deactivating, must remain.
@test "up-client AUTOSTART_RESYNC=0 keeps the probe and the stops and turns off only the resync" {
    run make -n up-client AUTOSTART_RESYNC=0 EGRESS_ISOLATION=1 WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" 'user-bus.sh'"'"' check "container-orcaster-worker.service" "orcaster-worker" || exit 1'
    assert_contains "$output" 'user-bus.sh'"'"' check "container-orcaster-proxy.service" "orcaster-proxy" || exit 1'
    assert_contains "$output" 'user-bus.sh'"'"' stop-unit "container-orcaster-worker.service" "orcaster-worker"'
    assert_contains "$output" 'user-bus.sh'"'"' stop-unit "container-orcaster-proxy.service" "orcaster-proxy"'
    assert_contains "$output" 'autostart-sync-client N="" AUTOSTART_SYNC="1" AUTOSTART_RESYNC="0"'
}

@test "autostart-sync-client AUTOSTART_RESYNC=0 does not run 06-autostart.sh" {
    autostart_seam 1
    PATH="$TMPENV/bin:$PATH" XDG_CONFIG_HOME="$TMPENV/xdg" \
        run make autostart-sync-client SETUP_WORKER_DIR="$TMPENV/fake-worker" AUTOSTART_RESYNC=0
    [ "$status" -eq 0 ]
    refute_contains "$output" "Autostart already enabled"
    refute_contains "$output" "Could not re-sync the autostart"
}

# --- Memory cap: fallback to setup.env ----------------------------------------
# A manual `make up client` used to create the container WITHOUT --memory even
# when setup.env declared WORKER_MEMORY: the value only arrived if run-fleet.sh
# passed it explicitly. The cap is containment, not cosmetic (a runaway task
# dies as CONSTRAINT_MEMCG in its cgroup instead of triggering the host OOM
# killer), so up-client resolves it from setup.env on its own when it does not
# come from the environment nor the CLI.

@test "up-client resolves WORKER_MEMORY from setup.env" {
    printf "ORCH_BASE_URL='https://orch.example.com'\nWORKER_MEMORY='28g'\n" > "$TMPENV/setup.env"
    run make -n up-client SETUP_ENV_FILE="$TMPENV/setup.env" \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" "--memory 28g"
}

@test "up-client: WORKER_MEMORY from the command line wins over setup.env" {
    printf "WORKER_MEMORY='28g'\n" > "$TMPENV/setup.env"
    run make -n up-client SETUP_ENV_FILE="$TMPENV/setup.env" WORKER_MEMORY=30g \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" "--memory 30g"
    refute_contains "$output" "--memory 28g"
}

@test "up-client: an explicit empty WORKER_MEMORY disables the cap" {
    # Operator opt-out, and the run-fleet.sh path when setup.env declares no
    # cap: the empty command-line value wins over the file.
    printf "WORKER_MEMORY='28g'\n" > "$TMPENV/setup.env"
    run make -n up-client SETUP_ENV_FILE="$TMPENV/setup.env" WORKER_MEMORY= \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    refute_contains "$output" "--memory"
}

@test "up-client without a readable setup.env applies no cap and says so" {
    # Development checkout: setup.env is not versioned and does not exist. This
    # case used to be SILENT on purpose ("there is nothing to warn about"); a
    # later review reversed the rule, because the same silence covers the case
    # that does matter: on a tarball-installed host, the tree's file never
    # exists (git archive does not package it) and the /etc one may not be
    # readable. It does not abort: the target must keep working without a cap.
    run make -n up-client SETUP_ENV_FILE="$TMPENV/no-existe.env" \
        ETC_SETUP_ENV_FILE="$TMPENV/tampoco.env" \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    refute_contains "$output" "--memory"
    assert_contains "$output" "WARNING: the worker is created WITHOUT a memory cap"
    assert_contains "$output" "no readable setup.env"
    # Both search paths, so the operator knows where to put the file without
    # reading the Makefile.
    assert_contains "$output" "$TMPENV/tampoco.env"
    assert_contains "$output" "setup-scripts/debian13-worker/setup.env"
}

# --- No path without a cap stays silent --------------------------------------
# The warning is printed in the up-client recipe and not as a parse-time
# $(warning): the cap block is evaluated for EVERY target, and a `make help` in
# a development checkout has nothing to warn about. `make -n` prints the recipes
# without running them, so the assertion sees the printf anyway.

@test "up-client with a resolved cap does NOT warn" {
    # Mutation guard for the rest of the block: if the warning stopped being
    # conditioned on an empty cap, it would always be printed and the tests
    # below would pass with the logic inverted.
    printf "WORKER_MEMORY='28g'\n" > "$TMPENV/setup.env"
    run make -n up-client SETUP_ENV_FILE="$TMPENV/setup.env" \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" "--memory 28g"
    refute_contains "$output" "WARNING: the worker is created WITHOUT a memory cap"
}

@test "up-client warns when setup.env is readable but declares no cap" {
    # Different from "file not found": here the host is configured and what is
    # missing is the line. The reason names the specific file.
    printf "ORCH_BASE_URL='https://orch.example.com'\n" > "$TMPENV/setup.env"
    run make -n up-client SETUP_ENV_FILE="$TMPENV/setup.env" \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    refute_contains "$output" "--memory"
    assert_contains "$output" "$TMPENV/setup.env does not declare WORKER_MEMORY"
}

@test "up-client warns when /etc/orcaster exists and cannot be traversed" {
    # The hole that $(wildcard) cannot see: /etc/orcaster is 0750
    # root:orcaster, so for a user outside the group the file exists but stat
    # gives EACCES, the wildcard returns empty and the search falls back to the
    # tree, where the tarball never ships setup.env. Without this warning the
    # operator sees a container without a cap and no reason.
    if [ "$(id -u)" -eq 0 ]; then
        skip "root ignores the mode bits: [ -x ] is true and the case does not reproduce"
    fi
    mkdir -p "$TMPENV/etc"
    printf "WORKER_MEMORY='24g'\n" > "$TMPENV/etc/setup.env"
    chmod 000 "$TMPENV/etc"
    run make -n up-client ETC_SETUP_ENV_FILE="$TMPENV/etc/setup.env" \
        REPO_SETUP_ENV_FILE="$TMPENV/no-existe.env" \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    # The teardown deletes TMPENV: the permission must be restored before
    # asserting (a failing assert ends the test and skips the chmod below).
    chmod 755 "$TMPENV/etc"
    [ "$status" -eq 0 ]
    refute_contains "$output" "--memory"
    assert_contains "$output" "$TMPENV/etc exists but this user cannot traverse it"
    assert_contains "$output" "orcaster group"
}

@test "up-client also warns when the cap was emptied on purpose" {
    # An explicit `WORKER_MEMORY=` (the operator opt-out and the one run-fleet.sh
    # propagates when the host declares no cap) skips the Makefile's whole
    # block, so the reason comes from the SETUP_ENV_DIAG default. Creating the
    # container without a cap has the same consequence: it is stated too.
    printf "WORKER_MEMORY='28g'\n" > "$TMPENV/setup.env"
    run make -n up-client SETUP_ENV_FILE="$TMPENV/setup.env" WORKER_MEMORY= \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    refute_contains "$output" "--memory"
    assert_contains "$output" "WARNING: the worker is created WITHOUT a memory cap"
    assert_contains "$output" "cap emptied on purpose"
}

# --- Memory cap: the three remaining silences ---------------------------------

@test "up-client parses setup.env with bash and not with make's sh" {
    # `$(shell ...)` runs under /bin/sh (dash on Debian, the fleet's real host)
    # while what consumes the same file at deployment (`load_env`) is bash: with
    # two different parsers, a setup.env that the deployment accepts left the
    # container without a cap and without saying so.
    printf 'EXTRA=(a b)\nWORKER_MEMORY=%s\n' "'28g'" > "$TMPENV/setup.env"
    # It only discriminates where /bin/sh is NOT bash. On macOS /bin/sh is bash
    # in POSIX mode and accepts the same file, so the test would pass without
    # proving anything: an explicit skip rather than an empty green.
    if /bin/sh -c '. "$0" >/dev/null 2>&1' "$TMPENV/setup.env"; then
        skip "/bin/sh accepts bash syntax (it is not dash): the case does not reproduce on this host"
    fi
    run make -n up-client SETUP_ENV_FILE="$TMPENV/setup.env" \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" "--memory 28g"
}

@test "up-client warns when setup.env exists but cannot be sourced" {
    # Empty-because-it-declares-no-cap and empty-because-it-could-not-be-read
    # are different states: the second cannot stay silent. `return 1` propagates
    # a non-zero status from the `.` in any bash (a syntax error depends on the
    # version).
    printf 'WORKER_MEMORY=%s\nreturn 1\n' "'28g'" > "$TMPENV/setup.env"
    run make -n up-client SETUP_ENV_FILE="$TMPENV/setup.env" \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    # "WITHOUT a memory cap" is NOT enough as an assertion: that same text is
    # produced by the Makefile's parse-time $(warning), which already existed,
    # so deleting the recipe warning left the test green. What gates is the
    # state's own REASON, the only one of the four that comes from
    # SETUP_ENV_DIAG without going through the diagnostic $(shell).
    assert_contains "$output" "WITHOUT a memory cap. Reason:"
    assert_contains "$output" "could not source it"
    # It warns and continues, it does not abort: this block is evaluated for
    # EVERY target, and a broken setup.env cannot break `make down client` nor
    # `make logs client`.
    refute_contains "$output" "--memory"
}

@test "SETUP_ENV_FILE prefers /etc/orcaster/setup.env when it exists" {
    mkdir -p "$TMPENV/etc"
    printf 'WORKER_MEMORY=%s\n' "'24g'" > "$TMPENV/etc/setup.env"
    run make -n up-client ETC_SETUP_ENV_FILE="$TMPENV/etc/setup.env" \
        WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null
    [ "$status" -eq 0 ]
    assert_contains "$output" "--memory 24g"
}

@test "the SETUP_ENV_FILE default is anchored to the Makefile, not to the cwd" {
    # The default path is resolved against the directory of the Makefile ITSELF:
    # relative, it was resolved against make's cwd, and `make -f
    # /opt/.../Makefile` from another directory did not find the file and left
    # the cap empty without saying so. The Makefile is copied to a temporary
    # tree so as not to write the (unversioned) setup.env inside the real
    # checkout.
    #
    # The assertion is on the variable's VALUE (`make -p`) and not on the
    # up-client recipe: that `-f` is not propagated to the autostart-sync-client
    # sub-make, so the full invocation fails for another reason and is useless
    # as an assert.
    mkdir -p "$TMPENV/repo/setup-scripts/debian13-worker"
    cp Makefile "$TMPENV/repo/Makefile"
    printf 'WORKER_MEMORY=%s\n' "'28g'" \
        > "$TMPENV/repo/setup-scripts/debian13-worker/setup.env"
    cd "$TMPENV"
    run make -f "$TMPENV/repo/Makefile" -p -n help ETC_SETUP_ENV_FILE=/nonexistent/setup.env
    [ "$status" -eq 0 ]
    assert_contains "$output" "WORKER_MEMORY_FLAG := --memory 28g"
}

# --- CONFIG_DIR reaches 06-autostart.sh --------------------------------------
# The hook invoked the script without telling it where the host config lives, so
# `load_env` fell back to the lib.sh default (/etc/orcaster). On a host with the
# config at another path the script died asking for /etc/orcaster/setup.env, the
# recipe's `|| echo` swallowed the failure and the systemd --user unit was left
# unregenerated, with its PIDFile naming the container that `make up client` had
# just destroyed.
#
# The target `autostart-sync-client` is invoked directly and not `up-client`: it
# has no prerequisites, so `make -n` does not drag in the build/egress noise.

@test "autostart-sync-client passes CONFIG_DIR to 06-autostart.sh" {
    run make -n autostart-sync-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "CONFIG_DIR=\"/etc/orcaster\" bash setup-scripts/debian13-worker/06-autostart.sh"
}

@test "autostart-sync-client honours a command-line CONFIG_DIR" {
    # It is the install-release.sh path, which passes CONFIG_DIR this way in its
    # `make up client`; the Makefile's `?=` must let it win.
    run make -n autostart-sync-client CONFIG_DIR=/srv/orcaster
    [ "$status" -eq 0 ]
    assert_contains "$output" "CONFIG_DIR=\"/srv/orcaster\" bash setup-scripts/debian13-worker/06-autostart.sh"
    refute_contains "$output" "CONFIG_DIR=\"/etc/orcaster\""
}

@test "autostart-sync-client honours an environment CONFIG_DIR" {
    # cloud-init-worker.sh path, which templates CONFIG_DIR and exports it for
    # its children: run-fleet.sh and the `make` runs below it inherit it.
    CONFIG_DIR=/srv/orcaster run make -n autostart-sync-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "CONFIG_DIR=\"/srv/orcaster\" bash setup-scripts/debian13-worker/06-autostart.sh"
    refute_contains "$output" "CONFIG_DIR=\"/etc/orcaster\""
}

@test "the CONFIG_DIR default is derived from ETC_SETUP_ENV_FILE" {
    # A single place decides where the host config lives: the directory of the
    # setup.env that the Makefile looks for. If CONFIG_DIR were hardcoded, a
    # host with the config moved would have the Makefile reading one setup.env
    # and 06-autostart.sh looking for another, with nothing saying so.
    run make -n autostart-sync-client ETC_SETUP_ENV_FILE=/srv/orcaster/setup.env
    [ "$status" -eq 0 ]
    assert_contains "$output" "CONFIG_DIR=\"/srv/orcaster\" bash setup-scripts/debian13-worker/06-autostart.sh"
}

@test "the autostart fallback suggests sudo env and not the positional N form" {
    # `bash 06-autostart.sh N=2` passes N as a POSITIONAL argument and the
    # script reads it from the ENVIRONMENT, so N was silently ignored and the
    # operator re-synchronized the wrong worker. And `sudo VAR=value cmd` is
    # rejected by sudo unless sudoers allows it, so the command did not even start.
    run make -n autostart-sync-client N=2
    [ "$status" -eq 0 ]
    assert_contains "$output" "sudo env CONFIG_DIR=<host-config-dir> N=2 bash /root/orcaster/setup-scripts/debian13-worker/06-autostart.sh"
    refute_contains "$output" "06-autostart.sh N="
    refute_contains "$output" "sudo bash setup-scripts/debian13-worker/06-autostart.sh"
}

@test "the autostart fallback with an empty N does not emit a bare N" {
    # Unsuffixed worker (the single-worker host case): the suggested command
    # must not carry ` N=` without a value, which 06-autostart.sh would
    # interpret as an empty container name.
    run make -n autostart-sync-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "sudo env CONFIG_DIR=<host-config-dir> bash /root/orcaster/setup-scripts/debian13-worker/06-autostart.sh"
    refute_contains "$output" "host-config-dir> N="
}

# --- The remedy must not repeat the value that failed ------------------------
# The message used to suggest `sudo env CONFIG_DIR=$(CONFIG_DIR) ...
# 06-autostart.sh`, i.e. the SAME value the script had just died with. And the
# dominant reason for that death is precisely that the value points at the wrong
# directory: nothing persists the host's CONFIG_DIR in an interactive shell, so
# make resolves its default /etc/orcaster. The operator copied a well-formed
# command and failed identically again, with no hint that CONFIG_DIR was the
# suspect.

@test "the autostart fallback names the CONFIG_DIR it tried and does not repeat it in the command" {
    run make -n autostart-sync-client
    [ "$status" -eq 0 ]
    # What it DOES know: which value was tried.
    assert_contains "$output" "Tried with CONFIG_DIR=/etc/orcaster."
    # And that this value is the suspect, not a colour detail.
    assert_contains "$output" "If this host's config does not live in /etc/orcaster, that is the most likely cause"
    # What it CANNOT know: where this host's config lives. It goes as a gap to
    # fill in, never as the value that just failed.
    assert_contains "$output" "sudo env CONFIG_DIR=<host-config-dir>"
    refute_contains "$output" "sudo env CONFIG_DIR=/etc/orcaster"
    # The warning is fatal: a re-sync failure leaves the unit out of sync with
    # the freshly created container, and `make` cannot report success over that
    # state.
    assert_contains "$output" "exit 1; }"
    refute_contains "$output" "|| printf"
}

@test "the autostart fallback reports the resolved CONFIG_DIR, not a fixed /etc/orcaster" {
    # Mutation guard: the value the message says it tried must be the one that
    # was really passed to the script, otherwise it asserts something it does
    # not know again (this time the other way round: blaming a directory nobody
    # used).
    run make -n autostart-sync-client CONFIG_DIR=/srv/orcaster
    [ "$status" -eq 0 ]
    assert_contains "$output" "Tried with CONFIG_DIR=/srv/orcaster."
    refute_contains "$output" "Tried with CONFIG_DIR=/etc/orcaster."
    refute_contains "$output" "sudo env CONFIG_DIR=/srv/orcaster"
    # And the explanation cannot assert where the value came from either: on
    # this path (install-release.sh passes it on the command line, cloud-init
    # through the environment) it is NOT the Makefile default, so the text stays
    # with what holds for any origin.
    assert_contains "$output" "If this host's config does not live in /srv/orcaster, that is the most likely cause"
    refute_contains "$output" "that value is the Makefile default"
}

# --- The autostart hook failure, tested FOR REAL ------------------------------
# A re-sync failure used to be swallowed with a `|| printf` and the target
# exited 0. That contract left the systemd --user manager with the old unit
# loaded (MainPID of the already deleted container) while `make` reported
# success, and the next `daemon-reload` stopped the worker. Now the warning is
# still emitted and the target exits non-zero. The tests above read the recipe
# TEXT under `make -n`; these two run the whole recipe against the fake seams
# and test the BEHAVIOUR, in both directions.

@test "autostart-sync-client: a 06-autostart.sh failure flips the target and warns" {
    autostart_seam 1
    PATH="$TMPENV/bin:$PATH" XDG_CONFIG_HOME="$TMPENV/xdg" \
        run make autostart-sync-client SETUP_WORKER_DIR="$TMPENV/fake-worker"
    # 1. The re-sync failure is no longer swallowed.
    [ "$status" -ne 0 ]
    # 2. The warning is really emitted, and says what state the host was left in.
    assert_contains "$output" "Could not re-sync the autostart"
    # The prefix says the same as the exit code: fatal is ERROR.
    assert_contains "$output" "ERROR: Could not re-sync the autostart"
    refute_contains "$output" "WARNING: Could not re-sync the autostart"
    assert_contains "$output" "The container orcaster-worker WAS recreated"
    assert_contains "$output" "daemon-reload"
    # 3. And the guarded branch was entered, not the no-op for lack of systemctl
    #    or of the unit.
    assert_contains "$output" ">> Autostart already enabled"
}

@test "autostart-sync-client AUTOSTART_SYNC_FATAL=0: the failure warns and exits 0 (install-release path)" {
    # install-release.sh recreates several workers in a loop and verifies each
    # unit on its own: it cannot be cut halfway by the re-sync.
    autostart_seam 1
    PATH="$TMPENV/bin:$PATH" XDG_CONFIG_HOME="$TMPENV/xdg" \
        run make autostart-sync-client SETUP_WORKER_DIR="$TMPENV/fake-worker" AUTOSTART_SYNC_FATAL=0
    [ "$status" -eq 0 ]
    assert_contains "$output" "Could not re-sync the autostart"
    # It exits 0, so the prefix is WARNING and not ERROR: an ERROR with exit 0
    # reads as a failed deployment in the release log. The rest of the text does
    # not change, so the existing greps keep finding it.
    assert_contains "$output" "WARNING: Could not re-sync the autostart"
    refute_contains "$output" "ERROR: Could not re-sync the autostart"
    assert_contains "$output" "The container orcaster-worker WAS recreated"
}

@test "autostart-sync-client: with a successful 06-autostart.sh it emits no warning" {
    # The opposite direction: neither the warning nor a non-zero exit may come
    # out when the re-sync worked; the operator would chase a failure that did
    # not happen.
    autostart_seam 0
    PATH="$TMPENV/bin:$PATH" XDG_CONFIG_HOME="$TMPENV/xdg" \
        run make autostart-sync-client SETUP_WORKER_DIR="$TMPENV/fake-worker"
    [ "$status" -eq 0 ]
    assert_contains "$output" ">> Autostart already enabled"
    refute_contains "$output" "Could not re-sync the autostart"
}
