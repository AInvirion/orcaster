#!/usr/bin/env bats
# WORKER_RO_MOUNTS in the root Makefile: resolution from setup.env (same
# channel as WORKER_MEMORY), `-v <dir>:<dir>:ro` flags in the worker's
# `podman run`, and validation BEFORE touching the container.
#
# Two kinds of test:
#   - `make -n`: they look at the command that would be printed. They cover
#     the resolution of the value and the shape of the flags, not the
#     execution order (under -n no recipe is executed).
#   - real execution of `make up-client` against a FAKE podman and systemctl
#     that only record their arguments in $CALLS. This is what proves that an
#     invalid list stops before the unit stop and the `podman rm -f`.
#
# The mountable directories are created INSIDE this tests directory: a bare
# `mktemp -d` lands under /var (macOS) or /tmp (Linux), reserved paths that
# the validator rejects on purpose.
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

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # The setup.env fallback only applies if the variable does not come from
    # the environment: a value inherited from the shell of whoever runs bats
    # would skew the tests.
    unset WORKER_RO_MOUNTS WORKER_MEMORY CONFIG_DIR ETC_SETUP_ENV_FILE
    T="$(mktemp -d)"
    FIX="$(mktemp -d "${BATS_TEST_DIRNAME}/ro-fixture.XXXXXX")"
    mkdir -p "$FIX/example-ro" "$FIX/snap"
    chmod 0755 "$FIX/example-ro" "$FIX/snap"
    # Real files for the recipe's `[ -f ]` (/dev/null is not a regular file).
    : > "$T/worker.env"
    : > "$T/server.pub"
    CALLS="$T/calls.log"; export CALLS
    : > "$CALLS"
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
    if [ -n "${FIX:-}" ]; then
        chmod -R u+rwx "$FIX" 2>/dev/null || true
        rm -rf "$FIX"
    fi
}

# make -n with its own setup.env and without looking at the /etc/orcaster of whoever runs it.
dry_up_client() {
    run make -C "$REPO_ROOT" -n up-client \
        SETUP_ENV_FILE="$T/setup.env" ETC_SETUP_ENV_FILE="$T/no-existe.env" \
        WORKER_ENV="$T/worker.env" WORKER_PUBKEY="$T/server.pub" "$@"
}

# Builds a fake podman and systemctl that record into $CALLS, and the worker's
# --user unit in $T/xdg, so the recipe's stop guard is really exercised
# (without the unit, the recipe skips the stop and the test would prove
# nothing).
fake_runtime() {
    mkdir -p "$T/bin" "$T/xdg/systemd/user"
    printf '#!/usr/bin/env bash\necho "podman $*" >> "$CALLS"\nexit 0\n' > "$T/bin/podman"
    printf '#!/usr/bin/env bash\necho "systemctl $*" >> "$CALLS"\nexit 0\n' > "$T/bin/systemctl"
    chmod +x "$T/bin/podman" "$T/bin/systemctl"
    : > "$T/xdg/systemd/user/container-orcaster-worker.service"
    # With the unit present the recipe ends in autostart-sync-client, which
    # runs 06-autostart.sh; the real one requires the host's setup.env and its
    # failure is fatal. A SETUP_WORKER_DIR is built with the REAL scripts
    # (validator, user-bus.sh) linked and only 06-autostart.sh replaced.
    mkdir -p "$T/worker-dir"
    local f
    for f in "$REPO_ROOT"/setup-scripts/debian13-worker/*.sh; do
        ln -s "$f" "$T/worker-dir/$(basename "$f")"
    done
    rm -f "$T/worker-dir/06-autostart.sh"
    printf '#!/usr/bin/env bash\necho "06-autostart" >> "$CALLS"\nexit 0\n' > "$T/worker-dir/06-autostart.sh"
}

# REAL execution of up-client against the fake runtime. EGRESS_ISOLATION=0 so
# as not to pull in up-proxy/aardvark; the rest of the recipe runs in full.
real_up_client() {
    local WORKER_DIR_ARG=""
    if [ -d "$T/worker-dir" ]; then WORKER_DIR_ARG="SETUP_WORKER_DIR=$T/worker-dir"; fi
    PATH="$T/bin:$PATH" XDG_CONFIG_HOME="$T/xdg" \
        run make -C "$REPO_ROOT" up-client PODMAN="$T/bin/podman" EGRESS_ISOLATION=0 \
        SETUP_ENV_FILE="$T/setup.env" ETC_SETUP_ENV_FILE="$T/no-existe.env" \
        WORKER_ENV="$T/worker.env" WORKER_PUBKEY="$T/server.pub" \
        ${WORKER_DIR_ARG:+"$WORKER_DIR_ARG"} "$@"
}

@test "makefile ro-mounts: not declared adds no mounts and does no validation" {
    printf "WORKER_MEMORY='28g'\n" > "$T/setup.env"
    dry_up_client
    [ "$status" -eq 0 ]
    # The pubkey line stays EXACTLY as before: nothing appended.
    assert_out "$output" "ed25519.pub:ro,z \\"
    refute_out "$output" "check-ro-mounts.sh"
    refute_out "$output" "WORKER_RO_MOUNTS"
}

@test "makefile ro-mounts: empty in setup.env gives the same command as not declared" {
    printf "WORKER_MEMORY='28g'\n" > "$T/setup.env"
    dry_up_client
    [ "$status" -eq 0 ]
    not_declared="$output"
    printf "WORKER_MEMORY='28g'\nWORKER_RO_MOUNTS=''\n" > "$T/setup.env"
    dry_up_client
    [ "$status" -eq 0 ]
    [ "$output" = "$not_declared" ]
}

@test "makefile ro-mounts: one entry gives -v dir:dir:ro without z" {
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/example-ro" > "$T/setup.env"
    dry_up_client
    [ "$status" -eq 0 ]
    assert_out "$output" "ed25519.pub:ro,z -v $FIX/example-ro:$FIX/example-ro:ro \\"
    refute_out "$output" "$FIX/example-ro:$FIX/example-ro:ro,z"
    refute_out "$output" "$FIX/example-ro:$FIX/example-ro:ro,Z"
    # The validation runs as a prerequisite, with --host.
    assert_out "$output" "check-ro-mounts.sh' --host '$FIX/example-ro'"
}

@test "makefile ro-mounts: two entries give two mounts in order" {
    printf "WORKER_RO_MOUNTS='%s %s'\n" "$FIX/example-ro" "$FIX/snap" > "$T/setup.env"
    dry_up_client
    [ "$status" -eq 0 ]
    assert_out "$output" "-v $FIX/example-ro:$FIX/example-ro:ro -v $FIX/snap:$FIX/snap:ro \\"
}

@test "makefile ro-mounts: a multiline list gives the same words to the validator and to podman" {
    # GNU Make 3.81's foreach does not split on a newline: without the strip
    # the two paths reached podman as a single broken -v.
    dry_up_client WORKER_RO_MOUNTS="$FIX/example-ro
$FIX/snap"
    [ "$status" -eq 0 ]
    assert_out "$output" "-v $FIX/example-ro:$FIX/example-ro:ro -v $FIX/snap:$FIX/snap:ro \\"
    assert_out "$output" "check-ro-mounts.sh' --host '$FIX/example-ro $FIX/snap'"
}

@test "makefile ro-mounts: the command line wins over setup.env" {
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/example-ro" > "$T/setup.env"
    dry_up_client WORKER_RO_MOUNTS="$FIX/snap"
    [ "$status" -eq 0 ]
    assert_out "$output" "-v $FIX/snap:$FIX/snap:ro"
    refute_out "$output" "-v $FIX/example-ro:$FIX/example-ro:ro"
}

@test "makefile ro-mounts: an explicit empty value on the command line disables the mounts" {
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/example-ro" > "$T/setup.env"
    dry_up_client WORKER_RO_MOUNTS=
    [ "$status" -eq 0 ]
    refute_out "$output" "$FIX/example-ro:$FIX/example-ro:ro"
    refute_out "$output" "check-ro-mounts.sh"
    # Empty on purpose is not "could not read": no warning.
    refute_out "$output" "could not read WORKER_RO_MOUNTS"
}

@test "makefile ro-mounts: the environment wins over setup.env" {
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/example-ro" > "$T/setup.env"
    WORKER_RO_MOUNTS="$FIX/snap" dry_up_client
    [ "$status" -eq 0 ]
    assert_out "$output" "-v $FIX/snap:$FIX/snap:ro"
    refute_out "$output" "-v $FIX/example-ro:$FIX/example-ro:ro"
}

@test "makefile ro-mounts: without a readable setup.env it warns that it could not read the list" {
    # /etc/orcaster is 0750 root:orcaster: whoever runs make outside the group
    # cannot see the file. There is no way to know whether the host declares
    # mounts, so the recipe says so instead of silently creating the container
    # without them.
    dry_up_client SETUP_ENV_FILE="$T/no-existe.env"
    [ "$status" -eq 0 ]
    assert_out "$output" "WARNING: could not read WORKER_RO_MOUNTS from the host setup.env"
    assert_out "$output" "no readable setup.env"
}

@test "makefile ro-mounts: /etc/orcaster without traversal warns with the group reason" {
    if [ "$(id -u)" -eq 0 ]; then
        skip "root ignores mode bits: [ -x ] is true and the case cannot be reproduced"
    fi
    mkdir -p "$T/etc"
    printf "WORKER_RO_MOUNTS='/opt/example-ro'\n" > "$T/etc/setup.env"
    chmod 000 "$T/etc"
    run make -C "$REPO_ROOT" -n up-client ETC_SETUP_ENV_FILE="$T/etc/setup.env" \
        REPO_SETUP_ENV_FILE="$T/no-existe.env" \
        WORKER_ENV="$T/worker.env" WORKER_PUBKEY="$T/server.pub"
    chmod 755 "$T/etc"
    [ "$status" -eq 0 ]
    refute_out "$output" "/opt/example-ro:/opt/example-ro:ro"
    assert_out "$output" "WARNING: could not read WORKER_RO_MOUNTS"
    assert_out "$output" "$T/etc exists but this user cannot traverse it"
}

@test "makefile ro-mounts: a setup.env that bash cannot source warns loudly" {
    printf "WORKER_RO_MOUNTS='%s'\nreturn 1\n" "$FIX/example-ro" > "$T/setup.env"
    dry_up_client
    [ "$status" -eq 0 ]
    # Parse-time $(warning) and recipe WARNING, like the memory cap.
    assert_out "$output" "WITHOUT the WORKER_RO_MOUNTS mounts"
    assert_out "$output" "WARNING: could not read WORKER_RO_MOUNTS"
    assert_out "$output" "could not source it"
    refute_out "$output" "$FIX/example-ro:$FIX/example-ro:ro"
}

@test "makefile ro-mounts: a readable setup.env that does not declare the list does not warn" {
    printf "WORKER_MEMORY='28g'\n" > "$T/setup.env"
    dry_up_client
    [ "$status" -eq 0 ]
    refute_out "$output" "could not read WORKER_RO_MOUNTS"
}

# --- Real execution against the fake runtime ----------------------------------

@test "makefile ro-mounts: a valid list creates the container with the mounts" {
    fake_runtime
    printf "WORKER_RO_MOUNTS='%s %s'\n" "$FIX/example-ro" "$FIX/snap" > "$T/setup.env"
    real_up_client
    [ "$status" -eq 0 ]
    run grep -c '^podman rm -f orcaster-worker$' "$CALLS"
    [ "$output" -eq 1 ]
    run grep -E "^podman run -d .* -v $FIX/example-ro:$FIX/example-ro:ro -v $FIX/snap:$FIX/snap:ro " "$CALLS"
    [ "$status" -eq 0 ]
}

@test "makefile ro-mounts: a nonexistent directory stops before the stop and the podman rm" {
    fake_runtime
    printf "WORKER_RO_MOUNTS='%s %s'\n" "$FIX/example-ro" "$FIX/no-existe" > "$T/setup.env"
    real_up_client
    [ "$status" -ne 0 ]
    assert_out "$output" "'$FIX/no-existe' does not exist on this host"
    assert_out "$output" "The container orcaster-worker was not touched"
    # No build, no unit stop, no rm, no run: the runtime was not touched.
    run grep -c . "$CALLS"
    [ "$output" -eq 0 ]
}

@test "makefile ro-mounts: under make -j the validation still stops before the build" {
    # Without -j the prerequisites run in the order they are listed; with -j
    # they do not, and `podman build` started in parallel with the validation.
    # The .NOTPARALLEL of up-client is what keeps the order.
    fake_runtime
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/no-existe" > "$T/setup.env"
    real_up_client -j4
    [ "$status" -ne 0 ]
    assert_out "$output" "'$FIX/no-existe' does not exist on this host"
    run grep -c . "$CALLS"
    [ "$output" -eq 0 ]
}

@test "makefile ro-mounts: an invalid entry on the command line stops before the podman rm" {
    fake_runtime
    : > "$T/setup.env"
    real_up_client WORKER_RO_MOUNTS="/var/lib/orcaster"
    [ "$status" -ne 0 ]
    assert_out "$output" "'/var/lib/orcaster' is, is under, or contains"
    run grep -q 'podman rm' "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q 'systemctl --user stop' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "makefile ro-mounts: a directory unreadable by the user stops before the podman rm" {
    if [ "$(id -u)" -eq 0 ]; then
        skip "root ignores mode bits: the case cannot be reproduced"
    fi
    fake_runtime
    chmod 0311 "$FIX/example-ro"
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/example-ro" > "$T/setup.env"
    real_up_client
    chmod 0755 "$FIX/example-ro"
    [ "$status" -ne 0 ]
    assert_out "$output" "cannot read or traverse '$FIX/example-ro'"
    run grep -q 'podman rm' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "makefile ro-mounts: a quote in the list does not break the recipe, the validator rejects it" {
    fake_runtime
    : > "$T/setup.env"
    real_up_client WORKER_RO_MOUNTS="/opt/r'i"
    [ "$status" -ne 0 ]
    assert_out "$output" "'/opt/r'i' has characters outside"
    run grep -q 'podman rm' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "makefile ro-mounts: without a list the real execution does not invoke the validator" {
    fake_runtime
    printf "WORKER_MEMORY='28g'\n" > "$T/setup.env"
    real_up_client
    [ "$status" -eq 0 ]
    refute_out "$output" "WORKER_RO_MOUNTS"
    run grep -E '^podman run -d .*ed25519.pub:ro,z --no-healthcheck' "$CALLS"
    [ "$status" -eq 0 ]
}

# The healthcheck used to be launched with `podman exec` and the full
# container environment, readable by a task in /proc. The worker's run
# disables it (--no-healthcheck also overrides the HEALTHCHECK of an old
# image) and does not set any health command.
@test "makefile: the worker run passes --no-healthcheck and no health command" {
    fake_runtime
    printf "WORKER_MEMORY='28g'\n" > "$T/setup.env"
    real_up_client
    [ "$status" -eq 0 ]
    run grep -c '^podman run -d ' "$CALLS"
    [ "$output" = "1" ]
    run grep -E '^podman run -d .* --no-healthcheck( |$)' "$CALLS"
    [ "$status" -eq 0 ]
    run grep -E '^podman run -d .*--health-' "$CALLS"
    [ "$status" -ne 0 ]
}
