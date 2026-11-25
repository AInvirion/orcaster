#!/usr/bin/env bats
# Tests for install-release.sh. The two cases that justify the shape of the
# script: a sha256 that does not match and a tar interrupted halfway must NOT
# touch the code the host already had.

assert_contains() {
    [[ "$1" == *"$2"* ]] || { echo "missing: $2"; echo "in: $1"; return 1; }
}

refute_contains() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected: $2"; echo "in: $1"; return 1; }
}

# `ls -lnd` stub shared with the source guard of lib.sh: the script requires
# the root copy (SOURCE_DIR), its ancestors and the setup.env it sources to be
# owned by root, and the real lib.sh it sources from there runs the same
# guard. The bats tests cannot create root-owned files.
load ../../../setup-scripts/debian13/tests/source_guard_stub

# Put a setup-scripts/debian13-worker/lib.sh in the test tarball's tree and
# rebuild it (with its .sha256). The lib.sh content comes in on stdin. Without
# this, the test tarball never ships lib.sh and the tests measure the inline
# fallback of worker_unit/proxy_unit, not the branch that runs in production.
add_lib_to_tarball() {
    mkdir -p "$SRC/setup-scripts/debian13-worker"
    cat > "$SRC/setup-scripts/debian13-worker/lib.sh"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
}

# Replace the tarball's Makefile with one from an older release
# (no WORKER_ENV_HOST: its defaults point into client/) and rebuild it. It is
# what a --ref rollback brings to this installer.
use_old_makefile_in_tarball() {
    printf 'WORKER_ENV ?= $(abspath $(CLIENT_DIR))/.env\n' > "$SRC/Makefile"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
}

# Put the REAL check-ro-mounts.sh in the test tarball's tree and rebuild it.
# The pre-swap gate runs the new release's validator, so without this the
# tests would only measure the branch of a release that does not ship it.
add_ro_check_to_tarball() {
    mkdir -p "$SRC/setup-scripts/debian13-worker"
    cp "${BATS_TEST_DIRNAME}/../../../setup-scripts/debian13-worker/check-ro-mounts.sh" \
        "$SRC/setup-scripts/debian13-worker/check-ro-mounts.sh"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
}

# Put a fake 06-autostart.sh in the tarball's tree and rebuild it. It records
# in CALLS where it ran from and with what environment; with AUTOSTART_FAILS=1
# it exits 1 and, otherwise, brings the units of worker N back up (clears
# STOPPED_LOG and records them in ACTIVATED_LOG), which is what the real one
# does when it regenerates the adoption unit.
add_autostart_to_tarball() {
    mkdir -p "$SRC/setup-scripts/debian13-worker"
    cat > "$SRC/setup-scripts/debian13-worker/06-autostart.sh" <<'EOS'
#!/usr/bin/env bash
echo "06-autostart path=$0 CONFIG_DIR=${CONFIG_DIR:-} N=${N:-} EGRESS_ISOLATION=${EGRESS_ISOLATION:-} cwd=$PWD WORKER_USER=${WORKER_USER:-} WORKER_HOME=${WORKER_HOME:-}" >> "$CALLS"
[ -z "${AUTOSTART_FAILS:-}" ] || { echo "06-autostart: simulated failure" >&2; exit 1; }
: > "${STOPPED_LOG:-/dev/null}"
printf 'container-orcaster-worker-%s.service\ncontainer-orcaster-proxy-%s.service\n' "$N" "$N" >> "${ACTIVATED_LOG:-/dev/null}"
EOS
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
}

# Mountable directories for WORKER_RO_MOUNTS. They are created INSIDE this
# tests directory: $T falls under /var (macOS) or /tmp (Linux), reserved paths
# that the validator rejects on purpose.
ro_fixture() {
    FIX="$(mktemp -d "${BATS_TEST_DIRNAME}/ro-fixture.XXXXXX")"
    mkdir -p "$FIX/ro-dir"
    chmod 0755 "$FIX/ro-dir"
}

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../install-release.sh"
    # Physical (pwd -P): the script prints SOURCE_DIR physically resolved, and
    # on macOS mktemp returns a path under /var, which is a link to /private/var.
    T="$(cd "$(mktemp -d)" && pwd -P)"
    INSTALL_DIR="$T/opt/orcaster"
    mkdir -p "$INSTALL_DIR"
    echo "old-code" > "$INSTALL_DIR/marker.txt"

    # Valid tarball of a minimal tree with a RELEASE file.
    SRC="$T/src/orcaster"
    mkdir -p "$SRC"
    echo "new-code" > "$SRC/marker.txt"
    # The release Makefile resolves the worker env and pubkey from CONFIG_DIR
    # (WORKER_ENV_HOST): the installer detects it by this assignment and
    # then passes no WORKER_ENV/WORKER_PUBKEY. make itself is mocked.
    printf 'WORKER_ENV_HOST    := $(CONFIG_DIR_NORM)/env\n' > "$SRC/Makefile"
    printf 'version=9.9.9\ncommit=%s\nbuilt=2026-08-18T00:00:00Z\n' \
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" > "$SRC/RELEASE"
    TGZ="$T/rel.tgz"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )

    # The script requires root (push-release.sh invokes it with `sudo bash`).
    # Before faking root for the tests that exercise the install logic, we
    # record whether bats ALREADY runs as real root, for the test that checks
    # the unprivileged guard (that one cannot fake anything).
    IS_REAL_ROOT=0
    [ "$(id -u)" -eq 0 ] && IS_REAL_ROOT=1
    REAL_PATH="$PATH"

    # `id` stub that answers uid 0 only to `id -u` (no argument, which is how
    # the root guard invokes it). Any other invocation (including
    # `id -u "$WORKER_USER"`, which the pre-swap validation tests with RESTART
    # exercise) falls through to the real `id`.
    FAKE_ROOT_BIN="$T/fakebin"
    mkdir -p "$FAKE_ROOT_BIN"
    cat > "$FAKE_ROOT_BIN/id" <<'EOS'
#!/usr/bin/env bash
if [ "$#" -eq 1 ] && [ "$1" = "-u" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
EOS
    chmod +x "$FAKE_ROOT_BIN/id"
    write_ls_stub "$FAKE_ROOT_BIN"
    export PATH="$FAKE_ROOT_BIN:$PATH"

    # Root's copy of the release: on the host it is /root/orcaster,
    # which does not exist on macOS. The environment variable is the override
    # channel that --help documents (the --source-dir tests override it with
    # the option).
    export SOURCE_DIR="$T/root/orcaster"
    mkdir -p "$T/root"

    # Host config dir for the pre-swap validation tests (RESTART=1). It ships
    # an empty setup.env, which is the state of a steady-state host that
    # declares no optional setting: it exists (RESTART=1 requires it, it is
    # the only file the tarball can never provide), it sources without
    # problems and it declares nothing. Tests that need other content
    # overwrite it; the bootstrap one deletes it.
    export CONFIG_DIR="$T/etc/orcaster"
    mkdir -p "$CONFIG_DIR"
    echo "# test setup.env: steady-state host, no optional settings declared" \
        > "$CONFIG_DIR/setup.env"

    # Mocks of sudo/getent/chown for the RESTART=1 path (run_as_worker), an
    # idiom analogous to
    # setup-scripts/debian13-worker/tests/test_run_fleet.bats: they log to
    # CALLS instead of touching the real systemd/podman/filesystem.
    #
    # `sudo` models the only part of the systemd state these tests need:
    #   - is-active: 0 if the unit is in ACTIVE_UNITS and this mock has not
    #     stopped it yet; 3 in any other case (inactive unit, the default).
    #   - stop: records it in STOPPED_LOG, so from then on is-active answers
    #     3. This is what makes the script's pre-stop noticeable.
    #   - show-environment: the manager probe; fails with BUS_DOWN=1.
    #   - the `make up client` line: exit 0, and with MAKE_REACTIVATES=1 it
    #     clears STOPPED_LOG (the units come back on their own). With
    #     MAKE_FAILS=1 it fails. Without AUTOSTART_RESYNC=0 or AUTOSTART_SYNC=0
    #     on the line and with the worker's unit file in $FAKE_HOME, it
    #     simulates the Makefile's autostart-sync-client hook as it runs on a
    #     host installed from a tarball: its 06-autostart.sh comes from
    #     INSTALL_DIR and the source guard of lib.sh aborts it (reproduced with
    #     the real Makefile and lib.sh in the test "the Makefile hook from
    #     INSTALL_DIR ..."). With AUTOSTART_SYNC_FATAL=0 the Makefile warns and
    #     exits 0, and the unit is left stopped.
    # A test that exports none of those variables sees the same behaviour as
    # before they existed: no active unit, no stop, make returns 0.
    CALLS="$T/calls.log"; export CALLS
    STOPPED_LOG="$T/stopped_units.log"; export STOPPED_LOG
    : > "$STOPPED_LOG"
    ACTIVATED_LOG="$T/activated_units.log"; export ACTIVATED_LOG
    : > "$ACTIVATED_LOG"
    cat > "$FAKE_ROOT_BIN/sudo" <<'EOS'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
last=""
for last in "$@"; do :; done
case " $* " in
    *" bash -s "*)
        # WORKER_RO_MOUNTS validator run as the worker with the script on
        # stdin: it is REALLY executed (without the `-u <user>`), so that the
        # test measures the real rule and not an exit 0 from the mock.
        shift 2
        exec "$@"
        ;;
    *" test -r "*)
        # Readability probes as $WORKER_USER: the setup.env, each env.N and
        # the server's pubkey. There are several and the setup.env is ALWAYS
        # the first, so two knobs are needed:
        #   - SUDO_TEST_R_FAILS=1: all of them fail. This is the state root
        #     does NOT see (file or directory mode/owner) and it serves the
        #     setup.env case, which cuts in before the others.
        #   - SUDO_TEST_R_FAILS_FOR=<substring>: only the file whose path
        #     contains it fails. Without this the env.N probe and the pubkey
        #     probe cannot be reached (the setup.env one aborts first) and the
        #     test would measure the wrong message.
        if [ -n "${SUDO_TEST_R_FAILS:-}" ]; then exit 1; fi
        case "$last" in
            *"${SUDO_TEST_R_FAILS_FOR:-__ninguna_ruta_contiene_esto__}"*) exit 1 ;;
        esac
        exit 0
        ;;
    *" show-environment"*)
        # Pre-swap probe of the systemd --user manager. BUS_DOWN=1: the
        # manager does not answer, as on a host without linger.
        if [ -n "${BUS_DOWN:-}" ]; then
            echo "Failed to connect to bus: No medium found" >&2
            exit 1
        fi
        exit 0
        ;;
    *" is-active "*)
        # Prints the state like systemctl (except with --quiet): the pre-stop
        # reads it to also stop what is in activating/deactivating.
        st=inactive
        if grep -qx "$last" "${STOPPED_LOG:-/dev/null}" 2>/dev/null; then st=inactive
        elif grep -qx "$last" "${ACTIVATED_LOG:-/dev/null}" 2>/dev/null; then st=active
        else
            case " ${ACTIVE_UNITS:-} " in *" $last "*) st=active ;; esac
            case " ${ACTIVATING_UNITS:-} " in *" $last "*) st=activating ;; esac
        fi
        case " $* " in *" --quiet "*) ;; *) echo "$st" ;; esac
        [ "$st" = active ] && exit 0
        exit 3
        ;;
    *" stop "*)
        # STOP_FAILS=1: the stop job fails (timeout, unit in failed). It does
        # not record the unit in STOPPED_LOG on purpose: it was left half
        # stopped.
        if [ -n "${STOP_FAILS:-}" ]; then
            echo "Job for $last failed because a timeout was exceeded." >&2
            exit 1
        fi
        echo "$last" >> "${STOPPED_LOG:-/dev/null}"
        exit 0
        ;;
    *" make "*)
        if [ -n "${MAKE_FAILS:-}" ]; then exit 1; fi
        # The hook runs except with AUTOSTART_SYNC=0, or with AUTOSTART_RESYNC=0
        # in a Makefile that knows it. MAKEFILE_OLD=1 simulates the tarball of
        # an older release, whose Makefile ignores AUTOSTART_RESYNC.
        hook=1
        case " $* " in *" AUTOSTART_SYNC=0 "*) hook=0 ;; esac
        if [ -z "${MAKEFILE_OLD:-}" ]; then
            case " $* " in *" AUTOSTART_RESYNC=0 "*) hook=0 ;; esac
        fi
        if [ "$hook" = 1 ]; then
            n=""
            for a in "$@"; do case "$a" in N=*) n="${a#N=}" ;; esac; done
            if [ -f "${FAKE_HOME:-/nonexistent}/.config/systemd/user/container-orcaster-worker-$n.service" ]; then
                echo "ERROR Source guard: the suite runs from the INSTALL_DIR (simulated by the make mock)" >&2
                echo "WARNING: Could not re-sync the autostart (container-orcaster-worker-$n.service)." >&2
                case " $* " in *" AUTOSTART_SYNC_FATAL=0 "*) exit 0 ;; esac
                exit 1
            fi
        fi
        if [ -n "${MAKE_REACTIVATES:-}" ]; then : > "${STOPPED_LOG:-/dev/null}"; fi
        exit 0
        ;;
esac
exit 0
EOS
    chmod +x "$FAKE_ROOT_BIN/sudo"
    # `chown` mocked for the same reason as in test_run_fleet.bats: bats does
    # not run as root, and on top of that `chown user:user` with the real user
    # running the tests fails on macOS (its primary group is 'staff', not a
    # group of the same name). A real chown failure is tested with
    # CHOWN_FAILS=1.
    cat > "$FAKE_ROOT_BIN/chown" <<'EOS'
#!/usr/bin/env bash
echo "chown $*" >> "$CALLS"
# CHOWN_FAILS=1 fails the chown to the worker user (the one of INSTALL_DIR);
# CHOWN_ROOT_FAILS=1, the chown -R root:root of root's copy (SOURCE_DIR).
case " $* " in
    *" root:root "*) [ -n "${CHOWN_ROOT_FAILS:-}" ] || exit 0 ;;
    *) [ -n "${CHOWN_FAILS:-}" ] || exit 0 ;;
esac
echo "chown: fake failure (simulated by the test)" >&2
exit 1
EOS
    chmod +x "$FAKE_ROOT_BIN/chown"
    # Fake sysctl (fs.protected_hardlinks): SYSCTL_HARDLINKS=<value> returns
    # it; "unreadable" simulates sysctl failing. Default 1 (Debian).
    cat > "$FAKE_ROOT_BIN/sysctl" <<'EOS'
#!/usr/bin/env bash
echo "sysctl $*" >> "${CALLS:-/dev/null}"
[ "${SYSCTL_HARDLINKS:-1}" = unreadable ] && { echo "sysctl: cannot stat" >&2; exit 255; }
echo "${SYSCTL_HARDLINKS:-1}"
EOS
    chmod +x "$FAKE_ROOT_BIN/sysctl"
    # The home in the getent stub has to EXIST on disk: the pre-swap gate
    # requires `[ -d "$home" ]`, and the /home/<user> this stub used to report
    # does not exist on macOS, where this suite runs.
    FAKE_HOME="$T/home/$(id -un)"
    mkdir -p "$FAKE_HOME"
    export FAKE_HOME
    cat > "$FAKE_ROOT_BIN/getent" <<'EOS'
#!/usr/bin/env bash
if [ "$1" = "passwd" ] && [ -n "${2:-}" ]; then
    printf '%s:x:1000:1000::%s:/bin/bash\n' "$2" "${FAKE_HOME:-/home/$2}"
    exit 0
fi
exit 2
EOS
    chmod +x "$FAKE_ROOT_BIN/getent"
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
    if [ -n "${FIX:-}" ]; then rm -rf "$FIX"; fi
}

@test "install-release: installs the new code and leaves RELEASE at the root" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
    [ -f "$INSTALL_DIR/RELEASE" ]
}

@test "install-release: a sha256 that does not match aborts without touching the installed code" {
    printf 'deadbeef  %s\n' "$(basename "$TGZ")" > "$TGZ.sha256"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "sha256"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

# sha256 re-check on the host: push-release.sh already verifies it with
# `sha256sum -c` before the `sudo bash`, but the installer does not rely on
# that: the <tarball>.sha256 that travels next to the tarball is checked again
# here and fails CLOSED. Without the file there is no silent path: it aborts
# naming it.
@test "install-release: without <tarball>.sha256 it aborts naming the file, without touching the installed code" {
    rm -f "$TGZ.sha256"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "the sha256 file is missing at $TGZ.sha256"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a tarball altered after the scp (neighbouring sha256 file does not match) aborts with expected and computed" {
    printf 'tampered' >> "$TGZ"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "does not match (expected "
    assert_contains "$output" "computed "
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a truncated tarball aborts without touching the installed code" {
    head -c 200 "$TGZ" > "$TGZ.trunc"
    mv "$TGZ.trunc" "$TGZ"
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    # The handler's message is also part of the contract: it is the only line
    # that tells the operator there is nothing to revert. Without this
    # assertion, leaving the handler as `|| { rm -rf "$staging"; exit 1; }`
    # (without the echo) passed, and the operator was left with tar's generic
    # stderr.
    assert_contains "$output" "the installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    # The invariant this test justifies is that the staging does NOT survive
    # an interrupted tar. Without this assert, deleting the tar xzf handler
    # (which is exactly the code this test claims to protect) did not make it
    # fail: status/marker are already guaranteed by set -e alone.
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.new"
    [ "$status" -ne 0 ]
}

@test "install-release: a tarball without RELEASE aborts without touching the installed code" {
    rm "$SRC/RELEASE"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "RELEASE"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a tarball without the orcaster/ prefix aborts without touching the installed code" {
    mv "$T/src/orcaster" "$T/src/other-name"
    tar czf "$TGZ" -C "$T/src" other-name
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "does not have the orcaster/ prefix"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    [ ! -d "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "install-release: if the second mv fails it self-repairs and restores the previous code" {
    # Fake mv: only the mv that moves the extracted tree (.orcaster.new/
    # orcaster) to its final destination fails; any other mv (the
    # first swap of the old code to .orcaster.old) goes to the real mv. It
    # simulates the most fragile case of the invariant: the first mv has
    # already run, INSTALL_DIR does not exist, and the second mv fails
    # (permissions, quota, read-only remount).
    FAKE_MV_BIN="$T/fakebin_mv"
    mkdir -p "$FAKE_MV_BIN"
    cat > "$FAKE_MV_BIN/mv" <<'EOS'
#!/usr/bin/env bash
for a in "$@"; do
    case "$a" in
        *.orcaster.new/orcaster) echo "mv: fake failure (simulated by the test)" >&2; exit 1 ;;
    esac
done
exec /bin/mv "$@"
EOS
    chmod +x "$FAKE_MV_BIN/mv"
    PATH="$FAKE_MV_BIN:$PATH" run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "$INSTALL_DIR"
    # And the message has to be the one of THIS branch. All three of the
    # handler's contain $INSTALL_DIR, so with only the assert above the worst
    # possible mutation stayed green: telling the operator "$INSTALL_DIR DOES
    # NOT EXIST, restore it by hand" just when the previous code is already
    # back in place.
    assert_contains "$output" "the previous code was restored"
    refute_contains "$output" "DOES NOT EXIST"
    [ -d "$INSTALL_DIR" ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: if the first mv fails it says the previous rollback no longer exists" {
    # The FIRST mv (backup of the installed code to .orcaster.old) was the only
    # bare statement under `set -e` in step 3, and it runs right AFTER
    # `rm -rf "$previous"`: a failure here left the operator with the raw
    # stderr of mv and no idea that the rollback of the previous delivery had
    # just been lost. Same fake idiom as the test of the second mv, but keyed
    # on the DESTINATION of the first one, so the two do not step on each
    # other.
    previous="$(dirname "$INSTALL_DIR")/.orcaster.old"
    mkdir -p "$previous"
    echo "rollback-previous-delivery" > "$previous/marker.txt"
    FAKE_MV_BIN="$T/fakebin_mv1"
    mkdir -p "$FAKE_MV_BIN"
    cat > "$FAKE_MV_BIN/mv" <<'EOS'
#!/usr/bin/env bash
for a in "$@"; do
    case "$a" in
        *.orcaster.old) echo "mv: fake failure (simulated by the test)" >&2; exit 1 ;;
    esac
done
exec /bin/mv "$@"
EOS
    chmod +x "$FAKE_MV_BIN/mv"
    PATH="$FAKE_MV_BIN:$PATH" run bash "$SCRIPT" --tarball "$TGZ" \
        --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    # Both halves of the message: the good one and the bad one. Without the
    # second, the operator believes the rollback is still available.
    assert_contains "$output" "NOTHING was replaced"
    assert_contains "$output" "that rollback path no longer exists"
    # And both are true: the installed code is still in place...
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    # ...and the .orcaster.old of the previous delivery really is gone (it is
    # deleted by the `rm -rf "$previous"` that precedes the mv). This assert is
    # what keeps the message from lying the other way round, promising a
    # rollback that is no longer there.
    [ ! -d "$previous" ]
    [ ! -d "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "install-release: the old checkout's .git does not survive the swap" {
    # Simulates a host that has not migrated: it still has a git checkout in
    # /opt/orcaster/.git, and moving off it is the whole point of the
    # tarball deploy. The invariant of THIS script is that the swap moves the
    # whole old directory (with its .git) to .orcaster.old, rather than
    # deleting it: the new tarball (via git archive in make-release.sh, which
    # already has its own test) never contains .git.
    mkdir -p "$INSTALL_DIR/.git"
    echo "ref: refs/heads/main" > "$INSTALL_DIR/.git/HEAD"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    run test -e "$INSTALL_DIR/.git"
    [ "$status" -ne 0 ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old/.git"
    [ "$status" -eq 0 ]
}

@test "install-release: without root it aborts with a clear cause and does not touch the installed code" {
    [ "$IS_REAL_ROOT" -eq 0 ] || skip "bats is running as real root; the guard cannot be tested without real privileges"
    PATH="$REAL_PATH" run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "root"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: --help works without root" {
    PATH="$REAL_PATH" run bash "$SCRIPT" --help
    [ "$status" -eq 0 ]
    assert_contains "$output" "install-release"
}

# --- Pre-swap validation of the host state (RESTART=1) ----------------------
# Host state failures (missing or unsourceable setup.env, missing env.N,
# missing pubkey, missing user) have to abort BEFORE the swap: neither
# .orcaster.old may exist (that would prove the swap ran) nor may the marker
# change.

@test "install-release: aborts before the swap if the host's setup.env is missing" {
    # Real scenario: a host with env.N and pubkey (it looks migrated) but
    # without setup.env. The gate used to let it through and the damage showed
    # up AFTER the swap: container recreated without --memory (the Makefile
    # falls back to the tree's setup.env, which the tarball does not ship) and
    # 06-autostart.sh dead in load_env, with the Makefile swallowing the
    # failure, that is, the systemd unit stopped and the host unable to
    # survive the next reboot.
    rm "$CONFIG_DIR/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "$CONFIG_DIR/setup.env"
    assert_contains "$output" "setup-scripts/debian13-worker/README.md"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

@test "install-release: a setup.env that cannot be sourced aborts before the swap" {
    # The `|| return 0` that read_setup_env_var had turned this into an empty
    # string: WORKER_MEMORY was not validated (the failure only showed up in
    # podman run, with the previous container already deleted) and a
    # deliberate EGRESS_ISOLATION=0 went back to the Makefile default of 1
    # without saying anything. With the sentinel it is fatal and pre-swap.
    printf 'WORKER_MEMORY=28\nif [\n' > "$CONFIG_DIR/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "$CONFIG_DIR/setup.env"
    assert_contains "$output" "could not source it"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.new"
    [ "$status" -ne 0 ]
}

@test "install-release: aborts before the swap if any worker's env.N is missing" {
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    # WORKERS=1 (default) requires env.1; we do not create it.
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "$CONFIG_DIR/env.1"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

@test "install-release: aborts before the swap if the server's pubkey is missing" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    worker_user="$(id -un)"
    # server-ed25519.pub is not created.
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "server-ed25519.pub"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

@test "install-release: aborts before the swap if the worker user does not exist" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    run env WORKER_USER="orcaster-no-existe-xyz" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "orcaster-no-existe-xyz"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

@test "install-release: --no-restart does not require setup.env, env.N or the user (new host bootstrap)" {
    # CONFIG_DIR stays empty (no setup.env, no env.1 or pubkey) and the user
    # does not exist: exactly the state of a freshly created host, first
    # delivery of the Bootstrap (setup-scripts/debian13-worker/README.md). With
    # --no-restart the swap has to complete anyway. The setup.env that
    # setup() creates is deleted on purpose: leaving it would make this test
    # stop testing what it claims to test as soon as RESTART=1 started to
    # require it.
    rm "$CONFIG_DIR/setup.env"
    run env WORKER_USER="orcaster-no-existe-xyz" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: invalid EGRESS_ISOLATION in setup.env aborts before the swap" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "EGRESS_ISOLATION=true" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "EGRESS_ISOLATION"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

# --- Coverage of the recreation block ----------------------------------------
# The eight tests above pass --no-restart; these two are the first to
# exercise the full RESTART=1 path (swap + `make up client`).

@test "install-release: RESTART=1 passes the config dir and EGRESS_ISOLATION from setup.env to make up client, not WORKER_ENV/WORKER_PUBKEY" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
    grep -q "make -C $INSTALL_DIR up client N=1 .*ETC_SETUP_ENV_FILE=$CONFIG_DIR/setup.env CONFIG_DIR=$CONFIG_DIR " "$CALLS"
    grep -q "EGRESS_ISOLATION=0" "$CALLS"
    # The Makefile resolves env.1 and the pubkey from CONFIG_DIR.
    run grep -E 'make .*up client.*(WORKER_ENV|WORKER_PUBKEY)=' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: an older Makefile without WORKER_ENV_HOST (rollback) still gets WORKER_ENV/WORKER_PUBKEY per worker" {
    use_old_makefile_in_tarball
    cfg2="$T/srv/orcaster"
    mkdir -p "$cfg2"
    echo "WORKER_COUNT=2" > "$cfg2/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$cfg2/env.1"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$cfg2/env.2"
    echo "server-pubkey" > "$cfg2/server-ed25519.pub"
    run env WORKER_USER="$(id -un)" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --config-dir "$cfg2"
    [ "$status" -eq 0 ]
    grep -q "make -C $INSTALL_DIR up client N=1 WORKER_ENV=$cfg2/env.1 WORKER_PUBKEY=$cfg2/server-ed25519.pub ETC_SETUP_ENV_FILE=$cfg2/setup.env CONFIG_DIR=$cfg2 " "$CALLS"
    grep -q "make -C $INSTALL_DIR up client N=2 WORKER_ENV=$cfg2/env.2 WORKER_PUBKEY=$cfg2/server-ed25519.pub ETC_SETUP_ENV_FILE=$cfg2/setup.env CONFIG_DIR=$cfg2 " "$CALLS"
}

@test "install-release: RESTART=1 without EGRESS_ISOLATION in setup.env does not add the flag to make up client" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    # The setup.env from setup() exists and sources fine, but does not declare
    # EGRESS_ISOLATION: it is the third legitimate state (empty string),
    # different from the missing file and the unsourceable one, which abort.
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    up_line="$(grep 'make -C '"$INSTALL_DIR"' up client N=1' "$CALLS")"
    assert_contains "$up_line" "CONFIG_DIR=$CONFIG_DIR"
    [[ "$up_line" != *"EGRESS_ISOLATION"* ]]
}

# --- Probe for the leftover env.N: a worker count that is too low -----------
# Recreating fewer workers than the host has leaves the remaining ones
# running on dead inodes, with exit 0 and no message. The probe for
# env.$((WORKERS+1)) is the only thing that catches it. It is a warning, not
# an abort: a host can legitimately be shrinking its fleet, and aborting the
# whole deploy for that would be worse than warning about it.

@test "install-release: warns (does not abort) if the host has more env.N than the workers to recreate" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$CONFIG_DIR/env.2"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --workers 1
    [ "$status" -eq 0 ]
    assert_contains "$output" "$CONFIG_DIR/env.2"
    assert_contains "$output" "--workers"
    assert_contains "$output" "WORKER_COUNT in $CONFIG_DIR/setup.env"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
    run grep -q "N=2" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: --workers 0 aborts before the swap instead of recreating nothing" {
    # `seq 1 0` emits nothing in GNU coreutils: without validation, the TWO
    # loops of this script (pre-swap validation and recreation) iterated zero
    # times and the deploy ended with exit 0, the swap done and no container
    # recreated. A 0 only arrives from the inventory: push-release.sh uses
    # `${!var:-1}`, which does not substitute on WORKER_COUNT_<host>=0 because
    # '0' is not an empty string.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --workers 0
    [ "$status" -ne 0 ]
    # The EXACT validation message, not a loose "--workers": the `seq` of
    # BSD/Darwin (where these bats run) counts BACKWARDS with `seq 1 0`, so
    # without this anchor the test passed anyway through the "env.0 missing"
    # message and did not gate the validation on the platform where it runs.
    assert_contains "$output" "--workers must be >= 1 (got: '0')"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

@test "install-release: non-numeric --workers aborts with the cause" {
    run env CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --workers dos
    [ "$status" -ne 0 ]
    assert_contains "$output" "--workers must be an integer >= 1 (got: 'dos')"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: invalid WORKER_MEMORY in setup.env aborts before the swap" {
    # Same reason as EGRESS_ISOLATION: the value ends up in the --memory of
    # the podman run of step 4 (via the Makefile fallback). A '28' without a
    # suffix is below podman's minimum, so the container would not be created
    # AFTER up-client already did `podman rm -f` on the previous one.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_MEMORY=28" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "WORKER_MEMORY"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

@test "install-release: valid WORKER_MEMORY in setup.env does not block the deploy" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_MEMORY=28g" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

# --- Protected variables without isolation in the pre-swap gate -------------
# The new release's worker exits with code 1 at startup if it has a variable
# from the ORCH_ISOLATION_REQUIRED_ENV list with a value and
# ORCH_ENFORCE_ISOLATION off. Without the gate, step 4 recreated it and it
# ended up in a crash loop with the new code already installed. The fixtures
# use a neutral name (EXAMPLE_PROTECTED_VAR) as the list. The variable's value
# must never appear in the output.

GATE_SENTINEL="sentinel-must-not-print"

# A one-worker host, ready to pass the rest of the pre-swap gate. The content
# of env.1 arrives on stdin.
worker_host() {
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    cat > "$CONFIG_DIR/env.1"
}

run_restart_deploy() {
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
}

# Overwrites $output (the final `run`s): each test's own assertions go BEFORE
# calling it.
assert_gate_abort_without_swap() {
    [ "$status" -ne 0 ]
    assert_contains "$output" "EXAMPLE_PROTECTED_VAR"
    assert_contains "$output" "ORCH_ENFORCE_ISOLATION=true"
    assert_contains "$output" "a variable from the list of variables that require isolation (EXAMPLE_PROTECTED_VAR)"
    assert_contains "$output" "central secret"
    assert_contains "$output" "The installed code was not touched"
    refute_contains "$output" "$GATE_SENTINEL"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
    run grep -q "up client" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: EXAMPLE_PROTECTED_VAR in env.N without ORCH_ENFORCE_ISOLATION aborts before the swap" {
    printf 'ORCH_ENROLLMENT_TOKEN=tok\nORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\n' \
        "$GATE_SENTINEL" | worker_host
    run_restart_deploy
    assert_contains "$output" "$CONFIG_DIR/env.1"
    assert_gate_abort_without_swap
}

@test "install-release: EXAMPLE_PROTECTED_VAR with ORCH_ENFORCE_ISOLATION false aborts before the swap" {
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=false\n' \
        "$GATE_SENTINEL" | worker_host
    run_restart_deploy
    assert_gate_abort_without_swap
}

@test "install-release: with EXAMPLE_PROTECTED_VAR, the true values of ORCH_ENFORCE_ISOLATION pass the gate" {
    # The true values of pydantic, case-insensitive, the lowercase name
    # (pydantic-settings is case-insensitive on the name) and a trailing \r
    # from a CRLF file, which podman strips. The last occurrence wins, as in
    # podman's --env-file.
    for flag in 'ORCH_ENFORCE_ISOLATION=true' 'ORCH_ENFORCE_ISOLATION=1' \
        'ORCH_ENFORCE_ISOLATION=YES' 'ORCH_ENFORCE_ISOLATION=on' \
        'ORCH_ENFORCE_ISOLATION=t' 'orch_enforce_isolation=Y' \
        $'ORCH_ENFORCE_ISOLATION=true\r' \
        $'ORCH_ENFORCE_ISOLATION=false\nORCH_ENFORCE_ISOLATION=True'; do
        rm -rf "$INSTALL_DIR" "$(dirname "$INSTALL_DIR")/.orcaster.old"
        mkdir -p "$INSTALL_DIR"
        echo "old-code" > "$INSTALL_DIR/marker.txt"
        printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\n%s\n' "$GATE_SENTINEL" "$flag" | worker_host
        run_restart_deploy
        [ "$status" -eq 0 ] || { echo "flag: $flag"; echo "$output"; return 1; }
        refute_contains "$output" "$GATE_SENTINEL"
        run cat "$INSTALL_DIR/marker.txt"
        [ "$output" = "new-code" ]
    done
}

@test "install-release: with EXAMPLE_PROTECTED_VAR, an ORCH_ENFORCE_ISOLATION with quotes or spaces aborts before the swap" {
    # podman passes the value as is (without stripping quotes or spaces) and
    # pydantic does not strip them either: Settings() fails at startup and the
    # worker ends up in a crash loop. The gate has to treat them as not true.
    # The same goes for a flag overridden by a final empty occurrence.
    for flag in 'ORCH_ENFORCE_ISOLATION= YES ' 'ORCH_ENFORCE_ISOLATION="on"' \
        "ORCH_ENFORCE_ISOLATION='t'" 'ORCH_ENFORCE_ISOLATION= 1' \
        $'ORCH_ENFORCE_ISOLATION=true\r\r' \
        $'ORCH_ENFORCE_ISOLATION=true\nORCH_ENFORCE_ISOLATION='; do
        printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\n%s\n' "$GATE_SENTINEL" "$flag" | worker_host
        run_restart_deploy
        [ "$status" -ne 0 ] || { echo "flag: $flag"; echo "$output"; return 1; }
        assert_contains "$output" "$CONFIG_DIR/env.1"
        assert_gate_abort_without_swap
    done
}

@test "install-release: with an empty or absent list, the variable does not trigger the gate" {
    # Empty built-in list: without ORCH_ISOLATION_REQUIRED_ENV (or empty) the
    # gate protects no name, even if there are values and the flag is off.
    printf 'EXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=false\n' "$GATE_SENTINEL" | worker_host
    run_restart_deploy
    [ "$status" -eq 0 ]
    refute_contains "$output" "$GATE_SENTINEL"
    rm -rf "$INSTALL_DIR" "$(dirname "$INSTALL_DIR")/.orcaster.old"
    mkdir -p "$INSTALL_DIR"
    echo "old-code" > "$INSTALL_DIR/marker.txt"
    printf 'ORCH_ISOLATION_REQUIRED_ENV=\nEXAMPLE_PROTECTED_VAR=%s\n' "$GATE_SENTINEL" | worker_host
    run_restart_deploy
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: a variable outside the list does not trigger the gate" {
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_OTHER_VAR=%s\nexample_protected_var=%s\n' \
        "$GATE_SENTINEL" "$GATE_SENTINEL" | worker_host
    run_restart_deploy
    [ "$status" -eq 0 ]
}

@test "install-release: the list accepts commas and spaces, the last line wins and the message names each variable" {
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_OTHER_VAR\nORCH_ISOLATION_REQUIRED_ENV= EXAMPLE_PROTECTED_VAR,EXAMPLE_VAR_B  EXAMPLE_VAR_C \nEXAMPLE_PROTECTED_VAR=%s\nEXAMPLE_VAR_C=%s\n' \
        "$GATE_SENTINEL" "$GATE_SENTINEL" | worker_host
    run_restart_deploy
    assert_contains "$output" "(EXAMPLE_PROTECTED_VAR,EXAMPLE_VAR_C)"
    refute_contains "$output" "$GATE_SENTINEL"
    [ "$status" -ne 0 ]
}

@test "install-release: a malformed list in env.N aborts before the swap without printing values" {
    # Fails closed, like the worker: a pattern, a NAME=value or quotes are not
    # interpreted, they are rejected.
    for list in '*_TOKEN' "EXAMPLE_PROTECTED_VAR=$GATE_SENTINEL" '"EXAMPLE_PROTECTED_VAR"' '1BAD' 'EXAMPLE_PROTECTED_VAR;x'; do
        printf 'ORCH_ISOLATION_REQUIRED_ENV=%s\nORCH_ENFORCE_ISOLATION=true\n' "$list" | worker_host
        run_restart_deploy
        [ "$status" -ne 0 ] || { echo "list: $list"; echo "$output"; return 1; }
        assert_contains "$output" "$CONFIG_DIR/env.1"
        assert_contains "$output" "malformed"
        assert_contains "$output" "The installed code was not touched"
        refute_contains "$output" "$GATE_SENTINEL"
        run cat "$INSTALL_DIR/marker.txt"
        [ "$output" = "old-code" ]
        run grep -q "up client" "$CALLS"
        [ "$status" -ne 0 ]
    done
}

@test "install-release: a malformed ISOLATION_REQUIRED_ENV in setup.env aborts before the swap" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" | worker_host
    printf "ISOLATION_REQUIRED_ENV='*_TOKEN'\n" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    [ "$status" -ne 0 ]
    assert_contains "$output" "EXTRA_ENV of $CONFIG_DIR/setup.env"
    assert_contains "$output" "malformed"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: if the gate awk fails, the deploy aborts without swap and without printing the value" {
    # Fail-closed: an awk that exits with an error (e.g. macOS's on an invalid
    # byte under a UTF-8 locale, which also repeats the line on stderr) must
    # not count as "no key". The stub fails only on the gate's invocation and
    # repeats its input on stderr, like that awk.
    cat > "$FAKE_ROOT_BIN/awk" <<EOS
#!/usr/bin/env bash
case "\$*" in
    *ORCH_ENFORCE_ISOLATION*) cat >&2; exit 2 ;;
esac
exec $(command -v awk) "\$@"
EOS
    chmod +x "$FAKE_ROOT_BIN/awk"
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true\n' "$GATE_SENTINEL" | worker_host
    run_restart_deploy
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not evaluate EXTRA_ENV of $CONFIG_DIR/setup.env"
    assert_contains "$output" "exited with code 2"
    assert_contains "$output" "The installed code was not touched"
    refute_contains "$output" "$GATE_SENTINEL"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run grep -q "up client" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: if the gate cannot read an env.N, it aborts naming it without swap" {
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true\n' "$GATE_SENTINEL" | worker_host
    # Root reads everything; as a regular user the chmod is enough to make the
    # redirect fail.
    [ "$IS_REAL_ROOT" -eq 0 ] || skip "as real root the file is still readable"
    chmod 000 "$CONFIG_DIR/env.1"
    run_restart_deploy
    chmod 600 "$CONFIG_DIR/env.1"
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not evaluate $CONFIG_DIR/env.1 (worker 1/1)"
    refute_contains "$output" "$GATE_SENTINEL"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: an empty or commented EXAMPLE_PROTECTED_VAR does not trigger the gate" {
    # Empty, only spaces, commented, or overridden by a last empty occurrence:
    # the worker treats it as absent (os.environ.get(...).strip()).
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=\nORCH_ENROLLMENT_TOKEN=tok\n' > "$T/env.vacio"
    worker_host < "$T/env.vacio"
    printf 'EXAMPLE_PROTECTED_VAR=  \n# EXAMPLE_PROTECTED_VAR=%s\n' "$GATE_SENTINEL" >> "$CONFIG_DIR/env.1"
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nEXAMPLE_PROTECTED_VAR=\n' "$GATE_SENTINEL" >> "$CONFIG_DIR/env.1"
    run_restart_deploy
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: EXAMPLE_PROTECTED_VAR only in the multiline EXTRA_ENV of setup.env aborts before the swap" {
    # The env.N does not have it yet, but the next run-fleet.sh re-stamps
    # EXTRA_ENV into each env.N and the worker would end up in a crash loop.
    echo "ORCH_ENROLLMENT_TOKEN=tok" | worker_host
    printf "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR'\nEXTRA_ENV='EXAMPLE_VAR_A=x\nEXAMPLE_PROTECTED_VAR=%s\nEXAMPLE_VAR_B=x'\n" \
        "$GATE_SENTINEL" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    assert_contains "$output" "EXTRA_ENV of $CONFIG_DIR/setup.env"
    assert_gate_abort_without_swap
}

@test "install-release: an ORCH_ENFORCE_ISOLATION only in env.N does not cover the EXTRA_ENV key" {
    # The flag added by hand in env.N does not survive the re-stamping by
    # run-fleet.sh, which writes only EXTRA_ENV. The EXTRA_ENV list comes from
    # ISOLATION_REQUIRED_ENV in setup.env: load_env refuses to declare it
    # inside.
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true\n' "$GATE_SENTINEL" | worker_host
    printf "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR'\nEXTRA_ENV='EXAMPLE_PROTECTED_VAR=%s'\n" "$GATE_SENTINEL" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    assert_contains "$output" "EXTRA_ENV of $CONFIG_DIR/setup.env"
    assert_gate_abort_without_swap
}

@test "install-release: EXAMPLE_PROTECTED_VAR with ORCH_ENFORCE_ISOLATION=true in EXTRA_ENV passes the gate" {
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true\n' "$GATE_SENTINEL" | worker_host
    printf "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR'\nEXTRA_ENV='EXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true'\n" \
        "$GATE_SENTINEL" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    [ "$status" -eq 0 ]
    refute_contains "$output" "$GATE_SENTINEL"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: setup.env's own ORCH_ENFORCE_ISOLATION true covers the EXTRA_ENV variable" {
    # Own channel: stamp_worker_env writes it AFTER EXTRA_ENV in each env.N
    # and with --env-file the last occurrence wins. The gate has to evaluate
    # what run-fleet.sh will stamp, not EXTRA_ENV alone; otherwise it aborts
    # the documented configuration even though the worker starts fine.
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true\n' "$GATE_SENTINEL" | worker_host
    printf "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR'\nEXTRA_ENV='EXAMPLE_PROTECTED_VAR=%s'\nORCH_ENFORCE_ISOLATION='true'\n" \
        "$GATE_SENTINEL" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    [ "$status" -eq 0 ]
    refute_contains "$output" "$GATE_SENTINEL"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: setup.env's own ORCH_ENFORCE_ISOLATION false does not cover the EXTRA_ENV variable" {
    # Today's (old) env.N lets the worker start, but the next run-fleet.sh
    # stamps false at the end and leaves it in a crash loop: the EXTRA_ENV leg
    # has to abort naming itself.
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true\n' "$GATE_SENTINEL" | worker_host
    printf "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR'\nEXTRA_ENV='EXAMPLE_PROTECTED_VAR=%s'\nORCH_ENFORCE_ISOLATION='false'\n" \
        "$GATE_SENTINEL" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    assert_contains "$output" "EXTRA_ENV of $CONFIG_DIR/setup.env"
    assert_gate_abort_without_swap
}

@test "install-release: without its own ORCH_ENFORCE_ISOLATION, the EXTRA_ENV flag still covers the variable" {
    # Compatibility with old hosts: a setup.env without the variable (covered
    # above) or with it declared empty does not change what is evaluated.
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true\n' "$GATE_SENTINEL" | worker_host
    printf "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR'\nEXTRA_ENV='EXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true'\nORCH_ENFORCE_ISOLATION=''\n" \
        "$GATE_SENTINEL" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    [ "$status" -eq 0 ]
    refute_contains "$output" "$GATE_SENTINEL"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

# Pins the configuration of a live strict host: setup.env with its own
# ORCH_ENFORCE_ISOLATION and the list in double quotes, without EXTRA_ENV,
# and the env.1 that run-fleet.sh stamps from that. The deploy has to pass
# without printing the value.
@test "install-release: strict host with a double-quoted list and no EXTRA_ENV passes the gate" {
    printf 'ORCH_ENROLLMENT_TOKEN=tok\nEXAMPLE_PROTECTED_VAR=%s\nORCH_ENFORCE_ISOLATION=true\nORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\n' \
        "$GATE_SENTINEL" | worker_host
    printf "ORCH_ENFORCE_ISOLATION='true'\nISOLATION_REQUIRED_ENV=\"EXAMPLE_PROTECTED_VAR\"\n" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    [ "$status" -eq 0 ]
    refute_contains "$output" "$GATE_SENTINEL"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

# Pins the configuration of a live legacy host: setup.env without
# ORCH_ENFORCE_ISOLATION or ISOLATION_REQUIRED_ENV, and an env.1 with the flag
# off and no list. There is nothing to protect and the deploy has to pass.
@test "install-release: legacy host with no list and no own flag in setup.env passes the gate" {
    printf 'ORCH_ENROLLMENT_TOKEN=tok\nORCH_ENFORCE_ISOLATION=false\n' | worker_host
    echo "WORKER_COUNT=1" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: EXAMPLE_PROTECTED_VAR in the second of two workers aborts naming its env.N" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" | worker_host
    printf 'ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR\nEXAMPLE_PROTECTED_VAR=%s\n' "$GATE_SENTINEL" > "$CONFIG_DIR/env.2"
    echo "WORKER_COUNT=2" > "$CONFIG_DIR/setup.env"
    run_restart_deploy
    assert_contains "$output" "$CONFIG_DIR/env.2 (worker 2/2)"
    assert_gate_abort_without_swap
}

# --- WORKER_RO_MOUNTS in the pre-swap gate ---------------------------------
# An invalid entry, a missing directory or one the worker cannot read must
# abort BEFORE the swap: in step 4 they would cut the `make up client` short
# with the worker unit already stopped by the pre-stop.

@test "install-release: WORKER_RO_MOUNTS with a nonexistent directory aborts before the swap" {
    add_ro_check_to_tarball
    ro_fixture
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf "WORKER_RO_MOUNTS='%s %s'\n" "$FIX/ro-dir" "$FIX/no-existe" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "'$FIX/no-existe' does not exist on this host"
    assert_contains "$output" "The installed code was not touched"
    # It ran as the worker user, not as root.
    grep -q "sudo -u $worker_user bash -s -- --host" "$CALLS"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.new"
    [ "$status" -ne 0 ]
    run grep -q " make " "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: WORKER_RO_MOUNTS with a reserved path aborts before the swap" {
    add_ro_check_to_tarball
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_RO_MOUNTS='/etc'" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "'/etc' is, is under, or contains /etc"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a valid WORKER_RO_MOUNTS passes the gate and is passed explicitly to make" {
    add_ro_check_to_tarball
    ro_fixture
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/ro-dir" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
    grep -q "make -C $INSTALL_DIR up client N=1 .*WORKER_RO_MOUNTS=$FIX/ro-dir" "$CALLS"
}

@test "install-release: without WORKER_RO_MOUNTS make receives the empty value and the validator does not run" {
    add_ro_check_to_tarball
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -qE "make -C $INSTALL_DIR up client N=1 .*WORKER_RO_MOUNTS=( |$)" "$CALLS"
    run grep -q "bash -s" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: a release without the validator warns that WORKER_RO_MOUNTS is not applied" {
    # Path of a rollback to a release older than WORKER_RO_MOUNTS: its
    # Makefile does not know the variable. It does not abort, but it does not
    # stay silent either.
    ro_fixture
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf "WORKER_RO_MOUNTS='%s'\n" "$FIX/ro-dir" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    assert_contains "$output" "WARNING"
    assert_contains "$output" "WITHOUT those mounts"
}

@test "install-release: no warning if --workers matches the real number of workers on the host" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --workers 1
    [ "$status" -eq 0 ]
    [[ "$output" != *"WARNING"* ]]
}

# --- Number of workers resolved from the host (WORKER_COUNT in setup.env) ---
# The host ALREADY declares its WORKER_COUNT in $CONFIG_DIR/setup.env: it is
# what run-fleet.sh iterates to stamp the env.N files and create the
# containers. Keeping the number also in the operator's deploy.local.env left
# two copies drifting apart with nothing to reconcile them. The inventory
# still counts, but as an explicit override.

@test "install-release: without --workers it recreates the ones the host's WORKER_COUNT declares" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$CONFIG_DIR/env.2"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_COUNT=2" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
    grep -q "make -C $INSTALL_DIR up client N=2" "$CALLS"
    # The Makefile hook is turned off for each worker: it would run
    # 06-autostart.sh from INSTALL_DIR and the origin guard aborts it.
    # The re-sync is done by the script itself from SOURCE_DIR.
    grep -q "make -C $INSTALL_DIR up client N=1 .*AUTOSTART_RESYNC=0" "$CALLS"
    grep -q "make -C $INSTALL_DIR up client N=2 .*AUTOSTART_RESYNC=0" "$CALLS"
    # The INDEX has to travel in EVERYTHING derived per worker, not only in
    # N=. Without these greps, mutating the pre-stop to `worker_unit 1` left
    # the suite green, and the real effect is that the pre-stop always stops
    # unit 1. The env.N per index is the Makefile's (WORKER_ENV_HOST from N);
    # the rollback test above covers the explicit WORKER_ENV per index.
    grep -q "ETC_SETUP_ENV_FILE=$CONFIG_DIR/setup.env" "$CALLS"
    grep -q "container-orcaster-worker-2.service" "$CALLS"
    # No warning: the host and the deploy say the same thing, which is the
    # whole point of reading it from the host.
    [[ "$output" != *"WARNING"* ]]
}

@test "install-release: without --workers and without WORKER_COUNT it recreates just one, like load_env" {
    # load_env defaults WORKER_COUNT to 1 (lib.sh:101,
    # WORKER_COUNT="${WORKER_COUNT:-1}"). This script mirrors that default:
    # established hosts were provisioned that way and do not declare the
    # variable, so requiring it here would break them with nothing wrong on
    # their side.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
    run grep -q "N=2" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: an explicit --workers wins over the host's WORKER_COUNT, but warns" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$CONFIG_DIR/env.2"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_COUNT=2" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --workers 1
    [ "$status" -eq 0 ]
    # The warning names BOTH values and where each one comes from. It goes
    # before any later `run`, which would overwrite $output.
    assert_contains "$output" "1 worker(s) were requested via --workers"
    assert_contains "$output" "this host declares 2"
    assert_contains "$output" "$CONFIG_DIR/setup.env"
    # And the explicit one wins.
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
    run grep -q "N=2" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: a non-numeric WORKER_COUNT in setup.env aborts before the swap" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_COUNT=dos" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "WORKER_COUNT in $CONFIG_DIR/setup.env must be an integer >= 1 (got: 'dos')"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

@test "install-release: a zero WORKER_COUNT in setup.env aborts before the swap" {
    # `seq 1 0` emits nothing in GNU coreutils: without validation, the swap
    # happened and no container was recreated, with exit 0 and no message. It
    # is the same validation as the argument's, which is why it lives in a
    # function.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_COUNT=0" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "WORKER_COUNT in $CONFIG_DIR/setup.env must be >= 1 (got: '0')"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

# --- The recreation loop does not leave systemd units stopped silently -----
# With the swap already done, a loop failure leaves container i down, its unit
# stopped by the pre-stop and workers i+1..N on dead inodes. The script has to
# name exactly that, as the handler of the second `mv` does.

@test "install-release: if make up client fails it names the stopped units and the workers not recreated" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$CONFIG_DIR/env.2"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf 'WORKER_COUNT=2\nEGRESS_ISOLATION=0\n' > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" MAKE_FAILS=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "'make up client' failed for worker 1/2"
    assert_contains "$output" "Unit stopped by this deploy and not back up: container-orcaster-worker-1.service"
    # This handler was the last of the three that still recommended a
    # `systemctl --user start`, exactly what its two siblings declare cannot
    # work: `up-client` does `podman rm -f` before the `podman run`, so the
    # container that the adoption unit's PIDFile names no longer exists. The
    # remedy is to regenerate it.
    refute_contains "$output" "systemctl --user start container-orcaster-worker-1.service"
    assert_contains "$output" "sudo env CONFIG_DIR=$CONFIG_DIR N=1 WORKER_USER=$(id -un) WORKER_HOME=$FAKE_HOME bash $SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh"
    assert_contains "$output" "Workers 2..2 were NOT recreated"
    # The swap already happened: the message has to say where each thing ended
    # up.
    assert_contains "$output" "$INSTALL_DIR"
    assert_contains "$output" "$(dirname "$INSTALL_DIR")/.orcaster.old"
}

# The opposite face of the handler above: if this deploy did not stop any unit,
# there is none to regenerate, and the message must not invent a remedy or
# name a worker that has no unit.
@test "install-release: if make up client fails with no stopped units it invents no remedy" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf 'WORKER_COUNT=1\nEGRESS_ISOLATION=0\n' > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="" MAKE_FAILS=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "'make up client' failed for worker 1/1"
    refute_contains "$output" "Unit stopped by this deploy"
    refute_contains "$output" "are not brought up with"
    refute_contains "$output" "06-autostart.sh"
}

@test "install-release: a unit that does not come back after recreating is not left silent" {
    # The container ends up healthy and the unit stopped: `make up client`
    # delegates to autostart-sync-client, which swallows the failure of
    # 06-autostart.sh with an `|| printf` (Makefile:734). The host stops
    # surviving a reboot and until this check nobody said so. Without
    # MAKE_REACTIVATES, the mock does not bring back up the unit that the
    # pre-stop stopped.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "did NOT become active after recreating it"
    # The remedy is to REGENERATE the unit, not to start it: it is an adoption
    # unit and its PIDFile names the container that the swap + `make up client`
    # destroyed, so a `systemctl --user start` fails looking for a PIDFile that
    # does not exist. The message has to print the command that does work,
    # with the CONFIG_DIR of THIS host (not the default /etc/orcaster).
    assert_contains "$output" "sudo env CONFIG_DIR=$CONFIG_DIR N=1 WORKER_USER=$(id -un) WORKER_HOME=$FAKE_HOME bash $SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh"
    refute_contains "$output" "systemctl --user start container-orcaster-worker-1.service"
    # The container WAS recreated and the new code is in place: the exit != 0
    # is because of the unit, not the deploy.
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: a unit that comes back up after recreating raises no warning" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" MAKE_REACTIVATES=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    # The pre-stop ran: it is the reason for the later check to exist.
    grep -q "systemctl --user stop container-orcaster-worker-1.service" "$CALLS"
    [[ "$output" != *"did NOT become active after recreating it"* ]]
}

@test "install-release: does not require a unit that was not active before to come back" {
    # Host without autostart enabled: autostart-sync-client is a no-op by
    # design (Makefile:731 requires the unit file to exist), so asking for
    # the unit to end up active would be asking for something nobody promised.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    run grep -q "systemctl --user stop" "$CALLS"
    [ "$status" -ne 0 ]
}

# --- Autostart re-sync from SOURCE_DIR -------------------------------------
# If the `make up client` of this script ran the autostart-sync-client hook,
# it would execute 06-autostart.sh from INSTALL_DIR; the source guard would
# abort it and every host would be left with its units stopped and rc=1.
# Instead the hook is turned off and the script regenerates the unit as root
# from root's copy.

# Premise of the make mock, measured with the REAL Makefile and lib.sh: the
# hook running from a tree that IS the INSTALL_DIR collides with the guard.
@test "install-release: the Makefile hook run from INSTALL_DIR collides with the source guard" {
    # As real root, require_root_owned_source cuts in earlier (the fixture
    # lives under a world-writable /tmp) and the test would pass for another
    # reason.
    if [ "$IS_REAL_ROOT" -eq 1 ]; then skip "measures the overlap guard, which as real root sits behind the permissions one"; fi
    repo="$BATS_TEST_DIRNAME/../../.."
    mkdir -p "$INSTALL_DIR/setup-scripts" "$T/bin" "$FAKE_HOME/.config/systemd/user"
    cp "$repo/Makefile" "$INSTALL_DIR/"
    cp -R "$repo/setup-scripts/debian13-worker" "$INSTALL_DIR/setup-scripts/"
    printf 'INSTALL_DIR=%s\nORCH_BASE_URL=https://orch.test\n' "$INSTALL_DIR" > "$CONFIG_DIR/setup.env"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    printf '#!/bin/sh\nexit 0\n' > "$T/bin/systemctl"
    chmod +x "$T/bin/systemctl"
    # Without the `id` stub (uid 0): the hook runs as the worker user.
    # Without XDG_CONFIG_HOME, as in the `sudo -u` of run_as_worker: the hook
    # looks for the unit there, and the CI runner defines it.
    run env -u XDG_CONFIG_HOME HOME="$FAKE_HOME" PATH="$T/bin:$REAL_PATH" \
        make -C "$INSTALL_DIR" autostart-sync-client N=1 CONFIG_DIR="$CONFIG_DIR"
    [ "$status" -ne 0 ]
    # The specific overlap diagnostic, not just any source guard.
    assert_contains "$output" "Source guard: the suite runs from $INSTALL_DIR, which is inside INSTALL_DIR ($INSTALL_DIR)"
    assert_contains "$output" "Could not re-sync the autostart (container-orcaster-worker-1.service)"
    # With AUTOSTART_RESYNC=0 (what install-release.sh passes) it is a no-op.
    run env -u XDG_CONFIG_HOME HOME="$FAKE_HOME" PATH="$T/bin:$REAL_PATH" \
        make -C "$INSTALL_DIR" autostart-sync-client N=1 CONFIG_DIR="$CONFIG_DIR" AUTOSTART_RESYNC=0
    [ "$status" -eq 0 ]
    refute_contains "$output" "Source guard"
}

@test "install-release: with autostart enabled it re-syncs from SOURCE_DIR and not from INSTALL_DIR" {
    add_autostart_to_tarball
    restart_host
    printf 'WORKER_COUNT=2\nEGRESS_ISOLATION=1\nWORKER_USER=%s\n' "$(id -un)" > "$CONFIG_DIR/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$CONFIG_DIR/env.2"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    for n in 1 2; do
        : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-$n.service"
        : > "$FAKE_HOME/.config/systemd/user/container-orcaster-proxy-$n.service"
    done
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service container-orcaster-proxy-1.service container-orcaster-worker-2.service container-orcaster-proxy-2.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    refute_contains "$output" "Source guard"
    refute_contains "$output" "did NOT become active after recreating it"
    assert_contains "$output" "re-syncing the systemd --user unit from $SOURCE_DIR"
    # Only the Makefile's re-sync is turned off on both workers; the manager
    # probe and the user-bus.sh stops stay (AUTOSTART_SYNC=0 would turn them
    # off and let units in activating/deactivating through).
    grep -q "make -C $INSTALL_DIR up client N=1 .*AUTOSTART_RESYNC=0" "$CALLS"
    grep -q "make -C $INSTALL_DIR up client N=2 .*AUTOSTART_RESYNC=0" "$CALLS"
    run grep -q "AUTOSTART_SYNC=0" "$CALLS"
    [ "$status" -ne 0 ]
    # And the unit is regenerated from root's copy, once per worker, with the
    # host's CONFIG_DIR and EGRESS_ISOLATION, and as root (no sudo -u).
    grep -qx "06-autostart path=$SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh CONFIG_DIR=$CONFIG_DIR N=1 EGRESS_ISOLATION=1 cwd=/ WORKER_USER=$(id -un) WORKER_HOME=$FAKE_HOME" "$CALLS"
    grep -qx "06-autostart path=$SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh CONFIG_DIR=$CONFIG_DIR N=2 EGRESS_ISOLATION=1 cwd=/ WORKER_USER=$(id -un) WORKER_HOME=$FAKE_HOME" "$CALLS"
    run grep -c "^06-autostart " "$CALLS"
    [ "$output" = "2" ]
    run grep -q "06-autostart path=$INSTALL_DIR" "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q "^sudo .*06-autostart" "$CALLS"
    [ "$status" -ne 0 ]
}

# 06-autostart.sh resolves the user by re-reading setup.env (load_env), so an
# environment override different from the one in setup.env would make the
# re-sync regenerate the units of ANOTHER user. With autostart units present
# it stops before the swap.
@test "install-release: WORKER_USER from the environment differing from setup.env with autostart aborts before the swap" {
    add_autostart_to_tarball
    restart_host
    printf 'EGRESS_ISOLATION=0\nWORKER_USER=otro-usuario\n' > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "this deploy recreates the containers as $(id -un) (home $FAKE_HOME), but 06-autostart.sh, which re-syncs the autostart, would resolve WORKER_USER=otro-usuario and WORKER_HOME=$FAKE_HOME from $CONFIG_DIR/setup.env"
    assert_contains "$output" "container-orcaster-worker-1.service"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run grep -q "up client" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: WORKER_USER from the environment with a setup.env that does not declare it reaches 06-autostart.sh" {
    add_autostart_to_tarball
    restart_host
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -q "^06-autostart .* N=1 .* WORKER_USER=$(id -un) WORKER_HOME=$FAKE_HOME$" "$CALLS"
}

@test "install-release: WORKER_USER from the environment differing from setup.env without autostart still wins" {
    restart_host
    printf 'EGRESS_ISOLATION=0\nWORKER_USER=otro-usuario\n' > "$CONFIG_DIR/setup.env"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    assert_contains "$output" "The explicit one wins"
    refute_contains "$output" "06-autostart.sh, which re-syncs the autostart, would resolve"
}

@test "install-release: empty WORKER_USER in setup.env with an environment override and autostart aborts (load_env falls back to orcaster)" {
    if [ "$(id -un)" = orcaster ]; then skip "the user running the bats is the lib.sh default"; fi
    restart_host
    printf 'EGRESS_ISOLATION=0\nWORKER_USER=\n' > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "would resolve WORKER_USER=orcaster and WORKER_HOME=$FAKE_HOME"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

# 06-autostart.sh used WORKER_HOME=/home/orcaster by default (lib.sh) and not
# the passwd home that make uses: it is passed the verified one, and a
# different declaration in setup.env stops before the swap.
@test "install-release: 06-autostart.sh receives the worker's passwd home, not the lib.sh default" {
    add_autostart_to_tarball
    restart_host
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    [[ "$FAKE_HOME" != /home/orcaster ]]
    grep -q "^06-autostart .* N=1 .* WORKER_HOME=$FAKE_HOME$" "$CALLS"
}

@test "install-release: WORKER_HOME in setup.env differing from the passwd home with autostart aborts before the swap" {
    restart_host
    printf 'EGRESS_ISOLATION=0\nWORKER_HOME=/srv/otro-home\n' > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "would resolve WORKER_USER=$(id -un) and WORKER_HOME=/srv/otro-home"
    assert_contains "$output" "The installed code was not touched"
    run grep -q "up client" "$CALLS"
    [ "$status" -ne 0 ]
}

# push-release.sh sends this installer with the tarball of the requested
# --ref: a rollback to an older release brings a Makefile that does not know
# AUTOSTART_RESYNC.
@test "install-release: with the Makefile of an earlier release the hook warns, the loop continues and root's re-sync fixes it" {
    add_autostart_to_tarball
    restart_host
    printf 'WORKER_COUNT=2\nEGRESS_ISOLATION=0\n' > "$CONFIG_DIR/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$CONFIG_DIR/env.2"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    for n in 1 2; do : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-$n.service"; done
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" MAKEFILE_OLD=1 \
        ACTIVE_UNITS="container-orcaster-worker-1.service container-orcaster-worker-2.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    # The old Makefile's hook collided with the guard, but only as a WARNING.
    assert_contains "$output" "WARNING: Could not re-sync the autostart (container-orcaster-worker-1.service)"
    grep -q "make -C $INSTALL_DIR up client N=1 .*AUTOSTART_SYNC_FATAL=0" "$CALLS"
    grep -q "make -C $INSTALL_DIR up client N=2 .*AUTOSTART_SYNC_FATAL=0" "$CALLS"
    run grep -c "^06-autostart " "$CALLS"
    [ "$output" = "2" ]
}

# Premise of the test above, with the REAL Makefile: without
# AUTOSTART_RESYNC=0 (what a Makefile that does not know the variable sees)
# and with AUTOSTART_SYNC_FATAL=0, the hook collides with the guard and
# exits 0.
@test "install-release: the Makefile hook without AUTOSTART_RESYNC and with AUTOSTART_SYNC_FATAL=0 warns and exits 0" {
    if [ "$IS_REAL_ROOT" -eq 1 ]; then skip "as real root the permissions guard cuts in earlier"; fi
    repo="$BATS_TEST_DIRNAME/../../.."
    mkdir -p "$INSTALL_DIR/setup-scripts" "$T/bin" "$FAKE_HOME/.config/systemd/user"
    cp "$repo/Makefile" "$INSTALL_DIR/"
    cp -R "$repo/setup-scripts/debian13-worker" "$INSTALL_DIR/setup-scripts/"
    printf 'INSTALL_DIR=%s\nORCH_BASE_URL=https://orch.test\n' "$INSTALL_DIR" > "$CONFIG_DIR/setup.env"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    printf '#!/bin/sh\nexit 0\n' > "$T/bin/systemctl"
    chmod +x "$T/bin/systemctl"
    run env -u XDG_CONFIG_HOME HOME="$FAKE_HOME" PATH="$T/bin:$REAL_PATH" \
        make -C "$INSTALL_DIR" autostart-sync-client N=1 CONFIG_DIR="$CONFIG_DIR" AUTOSTART_SYNC_FATAL=0
    [ "$status" -eq 0 ]
    assert_contains "$output" "which is inside INSTALL_DIR"
    assert_contains "$output" "WARNING: Could not re-sync the autostart"
}

# A unit in activating (auto-restart) is not seen by `is-active --quiet`; the
# pre-stop stops it anyway and requires it to come back.
@test "install-release: a unit in activating is stopped before recreating and required to come back" {
    add_autostart_to_tarball
    restart_host
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVATING_UNITS="container-orcaster-worker-1.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    assert_contains "$output" "Unit container-orcaster-worker-1.service activating: stop before recreating"
    grep -q "systemctl --user stop container-orcaster-worker-1.service" "$CALLS"
}

@test "install-release: a unit in activating with a failed re-sync exits non-zero" {
    add_autostart_to_tarball
    restart_host
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" AUTOSTART_FAILS=1 \
        ACTIVATING_UNITS="container-orcaster-worker-1.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "some systemd --user units remain stopped: container-orcaster-worker-1.service"
}

# Redeploy over a unit that had already been left stopped (by an earlier
# failed deploy): the pre-stop does not stop it, but a failed re-sync must
# still exit non-zero, not rc=0 with the unit down.
@test "install-release: redeploy with the unit already stopped and a failed re-sync exits non-zero" {
    add_autostart_to_tarball
    restart_host
    printf 'EGRESS_ISOLATION=1\n' > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-proxy-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" AUTOSTART_FAILS=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "the autostart re-sync of worker 1 failed"
    assert_contains "$output" "container-orcaster-worker-1.service"
    assert_contains "$output" "container-orcaster-proxy-1.service"
    assert_contains "$output" "sudo env CONFIG_DIR=$CONFIG_DIR N=1 WORKER_USER=$(id -un) WORKER_HOME=$FAKE_HOME bash $SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh"
}

# The remedy command is copied and pasted: with a passwd home that contains
# spaces, each value has to remain ONE word when parsed.
@test "install-release: the printed remedy quotes a home with spaces" {
    FAKE_HOME="$T/home/worker data"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    export FAKE_HOME
    add_autostart_to_tarball
    restart_host
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" AUTOSTART_FAILS=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    # The real call received the whole home.
    grep -q "^06-autostart .* WORKER_HOME=$FAKE_HOME$" "$CALLS"
    cmd="$(printf '%s\n' "$output" | grep -m1 '^       sudo env CONFIG_DIR=' | sed 's/^       //')"
    [ -n "$cmd" ]
    eval "set -- $cmd"
    [ "$1" = sudo ]
    [ "$2" = env ]
    [ "$3" = "CONFIG_DIR=$CONFIG_DIR" ]
    [ "$4" = "N=1" ]
    [ "$5" = "WORKER_USER=$(id -un)" ]
    [ "$6" = "WORKER_HOME=$FAKE_HOME" ]
    [ "$7" = bash ]
    [ "$8" = "$SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh" ]
    [ "$#" -eq 8 ]
}

@test "install-release: redeploy with the unit already stopped and a successful re-sync exits 0" {
    add_autostart_to_tarball
    restart_host
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -q "^06-autostart .* N=1 " "$CALLS"
}

@test "install-release: if the re-sync from SOURCE_DIR fails, it warns, continues and exits non-zero" {
    add_autostart_to_tarball
    restart_host
    printf 'WORKER_COUNT=2\nEGRESS_ISOLATION=0\nWORKER_USER=%s\n' "$(id -un)" > "$CONFIG_DIR/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$CONFIG_DIR/env.2"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" AUTOSTART_FAILS=1 \
        ACTIVE_UNITS="container-orcaster-worker-1.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "the autostart re-sync of worker 1 failed"
    assert_contains "$output" "some systemd --user units remain stopped: container-orcaster-worker-1.service"
    assert_contains "$output" "sudo env CONFIG_DIR=$CONFIG_DIR N=1 WORKER_USER=$(id -un) WORKER_HOME=$FAKE_HOME bash $SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh"
    # It does not cut the loop: worker 2 is recreated anyway.
    grep -q "make -C $INSTALL_DIR up client N=2" "$CALLS"
}

@test "install-release: without the unit file it does not run 06-autostart.sh" {
    add_autostart_to_tarball
    restart_host
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    run grep -q "06-autostart" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: a release without 06-autostart.sh warns and leaves the unit recorded as stopped" {
    restart_host
    printf 'EGRESS_ISOLATION=0\nWORKER_USER=%s\n' "$(id -un)" > "$CONFIG_DIR/setup.env"
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    run env WORKER_USER="$(id -un)" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "$SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh does not exist"
    assert_contains "$output" "some systemd --user units remain stopped: container-orcaster-worker-1.service"
}

# --- Ownership of the installed tree (the chown -R that died with 01-fetch-code)

# The chown -R depends on fs.protected_hardlinks=1: it is required before
# chowning.
@test "install-release: fs.protected_hardlinks=0 is fatal and does not chown the worker's tree" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" SYSCTL_HARDLINKS=0 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "fs.protected_hardlinks=0"
    assert_contains "$output" "sysctl -w fs.protected_hardlinks=1"
    run grep -q "^chown -R $worker_user:$worker_user" "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q "make -C $INSTALL_DIR up client" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: unreadable fs.protected_hardlinks is fatal (fails closed)" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" SYSCTL_HARDLINKS=unreadable \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not read the sysctl fs.protected_hardlinks"
    run grep -q "^chown -R $worker_user:$worker_user" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: with fs.protected_hardlinks=1 it chowns without a sysctl message" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" SYSCTL_HARDLINKS=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    run grep -q protected_hardlinks <<<"$output"
    [ "$status" -ne 0 ]
    grep -q "^chown -R $worker_user:$worker_user $INSTALL_DIR" "$CALLS"
}

@test "install-release: a chown that fails with the user present is fatal" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" CHOWN_FAILS=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "ERROR: the chown -R $worker_user:$worker_user on $INSTALL_DIR failed"
    assert_contains "$output" "$INSTALL_DIR"
    # A tree the worker cannot read brings up no container: the recreation is
    # not continued.
    run grep -q "make -C $INSTALL_DIR up client" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: without the worker user the chown is skipped with the command to reconcile" {
    # Bootstrap (README, Bootstrap section): the user does not exist yet, so
    # there is nobody to chown to. This is an explicit path, distinct from the
    # chown failure, and it prints the exact command instead of promising that
    # someone else will do it.
    rm "$CONFIG_DIR/setup.env"
    run env WORKER_USER="orcaster-no-existe-xyz" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    assert_contains "$output" "does not exist yet on this host"
    assert_contains "$output" "chown -R 'orcaster-no-existe-xyz:orcaster-no-existe-xyz' '$INSTALL_DIR'"
    # The only chown is the root copy's (root:root), not the worker's.
    run grep -q "^chown .*orcaster-no-existe-xyz" "$CALLS"
    [ "$status" -ne 0 ]
}

# --- Modes of the extracted tree ------------------------------------
# The WARNING of the first delivery promises a tree without group or other
# write. Two layers guarantee it: make-release.sh builds 0755/0644 and this
# script runs chmod -R go-w on what it extracted, for tarballs built some
# other way.

# Lists whatever has group or other write under $1 (no links, which always
# show lrwxrwxrwx).
group_or_world_writable() {  # $1=directory
    find "$1" ! -type l \( -perm -g+w -o -perm -o+w \) -print
}

@test "install-release: a 0775/0666 tarball ends up without group or other write" {
    mkdir -p "$SRC/sub"
    printf 'x\n' > "$SRC/sub/writable.txt"
    chmod 0666 "$SRC/sub/writable.txt"
    chmod 0775 "$SRC" "$SRC/sub"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
    # umask 0002: without the script's chmod, tar (as non-root) would leave 0775/0666.
    run bash -c 'umask 0002 && exec "$@"' _ bash "$SCRIPT" \
        --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    [ "$(cat "$INSTALL_DIR/sub/writable.txt")" = "x" ]
    run group_or_world_writable "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "install-release: the first-delivery WARNING names the modes it leaves" {
    rm "$CONFIG_DIR/setup.env"
    chmod 0775 "$SRC"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
    run env WORKER_USER="orcaster-no-existe-xyz" CONFIG_DIR="$CONFIG_DIR" \
        bash -c 'umask 0002 && exec "$@"' _ bash "$SCRIPT" \
        --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    assert_contains "$output" "no group or other write"
    # What the WARNING says is what was left on disk.
    [ -z "$(group_or_world_writable "$INSTALL_DIR")" ]
}

@test "source-dir: inside INSTALL_DIR of a real tarball aborts on overlap, not on the parent's mode" {
    # On a real host, with the make-release.sh tarball (0775), the parent mode
    # rule rejected --source-dir "$INSTALL_DIR/src" before reaching the overlap
    # check. Here INSTALL_DIR comes from a real tarball and the ls stub reports
    # its real modes (SOURCE_GUARD_FIXTURE), with umask 0002 so that the result
    # does not depend on the runner's umask.
    local dist="$T/dist"
    run env GIT_CONFIG_GLOBAL=/dev/null bash "${BATS_TEST_DIRNAME}/../make-release.sh" --out "$dist"
    [ "$status" -eq 0 ]
    local real_tgz
    real_tgz="$(find "$dist" -name 'orcaster-*.tgz' | head -n1)"
    [ -n "$real_tgz" ]
    rm -rf "$INSTALL_DIR"
    run bash -c 'umask 0002 && exec "$@"' _ bash "$SCRIPT" \
        --tarball "$real_tgz" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    local before
    before="$(cat "$INSTALL_DIR/RELEASE")"
    run env SOURCE_GUARD_FIXTURE="$INSTALL_DIR" bash -c 'umask 0002 && exec "$@"' _ \
        bash "$SCRIPT" --tarball "$real_tgz" --install-dir "$INSTALL_DIR" --no-restart \
        --source-dir "$INSTALL_DIR/src"
    [ "$status" -eq 1 ]
    assert_contains "$output" "overlaps with $INSTALL_DIR"
    refute_contains "$output" "has owner uid"
    assert_contains "$output" "The installed code was not touched"
    [ "$(cat "$INSTALL_DIR/RELEASE")" = "$before" ]
    [ ! -e "$INSTALL_DIR/src" ]
}

# --- The branch that sources the lib.sh of the freshly installed release ----
# It is the one that runs in production (a whole lib.sh, in a shell under `set
# -euo pipefail`, AFTER the destructive swap) and no test exercised it: the
# test tarball never shipped a lib.sh, so what was measured was the inline
# fallback of worker_unit/proxy_unit.

@test "install-release: the repo's real lib.sh is sourced without breaking the deploy" {
    # The origin guard of the real lib.sh requires run-all-worker.sh next to it.
    mkdir -p "$SRC/setup-scripts/debian13-worker"
    cp "$BATS_TEST_DIRNAME/../../../setup-scripts/debian13-worker/run-all-worker.sh" \
        "$SRC/setup-scripts/debian13-worker/"
    add_lib_to_tarball < "$BATS_TEST_DIRNAME/../../../setup-scripts/debian13-worker/lib.sh"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "EGRESS_ISOLATION=0" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" MAKE_REACTIVATES=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    # The unit names come from lib.sh and match the convention.
    grep -q "systemctl --user stop container-orcaster-worker-1.service" "$CALLS"
    # And the host's CONFIG_DIR survives the source (lib.sh assigns it), and
    # the real lib.sh's worker_config_make_args adds no override.
    grep -q "make -C $INSTALL_DIR up client N=1 .*CONFIG_DIR=$CONFIG_DIR " "$CALLS"
    run grep -E 'make .*up client.*(WORKER_ENV|WORKER_PUBKEY)=' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: with lib.sh present the unit names come from it and not from the fallback" {
    add_lib_to_tarball <<'EOS'
# shellcheck shell=bash
worker_unit() { printf 'unidad-desde-lib-%s.service\n' "${1:-}"; }
proxy_unit()  { printf 'proxy-desde-lib-%s.service\n' "${1:-}"; }
EOS
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="unidad-desde-lib-1.service proxy-desde-lib-1.service" \
        MAKE_REACTIVATES=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -q "systemctl --user stop unidad-desde-lib-1.service" "$CALLS"
    grep -q "systemctl --user stop proxy-desde-lib-1.service" "$CALLS"
    run grep -q "container-orcaster-worker-1.service" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: a lib.sh that assigns CONFIG_DIR does not repoint where the env.N files come from" {
    # lib.sh is sourced in this script's shell: an unguarded top-level
    # assignment would overwrite CONFIG_DIR AFTER the swap and the deploy would
    # start reading the env.N files and the pubkey from the wrong directory.
    # Today's lib.sh uses ${CONFIG_DIR:-...}, but that is a property of the
    # installed release, not a guarantee of this script.
    add_lib_to_tarball <<'EOS'
# shellcheck shell=bash
CONFIG_DIR=/ruta/equivocada
WORKER_USER=usuario-equivocado
worker_unit() { printf 'container-orcaster-worker-%s.service\n' "${1:-}"; }
proxy_unit()  { printf 'container-orcaster-proxy-%s.service\n' "${1:-}"; }
EOS
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -q "ETC_SETUP_ENV_FILE=$CONFIG_DIR/setup.env CONFIG_DIR=$CONFIG_DIR " "$CALLS"
    run grep -q "/ruta/equivocada" "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q "usuario-equivocado" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: a lib.sh that fails when sourced aborts stating the deploy state" {
    # A top-level command that fails: with `set -euo pipefail` in the script's
    # shell that killed the deploy with bash's bare message, right after the
    # swap, without saying that the new code was already in place and that no
    # container had been recreated.
    add_lib_to_tarball <<'EOS'
# shellcheck shell=bash
command-that-exists-on-no-host
worker_unit() { printf 'container-orcaster-worker-%s.service\n' "${1:-}"; }
proxy_unit()  { printf 'container-orcaster-proxy-%s.service\n' "${1:-}"; }
EOS
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not be sourced"
    assert_contains "$output" "NO container was recreated"
    assert_contains "$output" "$(dirname "$INSTALL_DIR")/.orcaster.old"
    # The INSTALL_DIR rollback does not undo root's copy: the message says so
    # to keep the operator from running the new suite believing otherwise.
    assert_contains "$output" "That command does not touch root's copy: $SOURCE_DIR already has the new release"
    run grep -q "make -C $INSTALL_DIR up client" "$CALLS"
    [ "$status" -ne 0 ]
}

# --- Host knobs the script used to read from the default, not setup.env -----

@test "install-release: without --install-dir the destination comes from the host's INSTALL_DIR" {
    # cloud-init-worker.sh STAMPS INSTALL_DIR into the setup.env it generates,
    # so every host provisioned by cloud-init declares this knob. With the
    # hardcoded default, the swap went to /opt/orcaster and the tree
    # the host declares (the one run-fleet.sh and 06-autostart.sh read) froze
    # on the previous release, with exit 0.
    #
    # Careful when writing tests like this one: without --install-dir, the
    # destination comes from setup.env, so the file MUST declare a path inside
    # $T. If it does not, the script points at the machine's real
    # /opt/orcaster (`id -u` is faked to 0) and runs `rm -rf` right
    # there.
    declarado="$T/srv/orcaster"
    mkdir -p "$declarado"
    echo "old-code-declarado" > "$declarado/marker.txt"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf 'INSTALL_DIR=%s\n' "$declarado" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ"
    [ "$status" -eq 0 ]
    run cat "$declarado/marker.txt"
    [ "$output" = "new-code" ]
    grep -q "make -C $declarado up client N=1" "$CALLS"
    # And the hardcoded default was not touched.
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: an explicit --install-dir wins over the host's, but warns" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf 'INSTALL_DIR=%s\n' "$T/srv/orcaster" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    assert_contains "$output" "WARNING"
    assert_contains "$output" "$T/srv/orcaster"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
    [ ! -d "$T/srv/orcaster" ]
}

# The comparison behind the WARNING above normalizes trailing slashes on both
# sides. Without that, '/opt/x/' against an INSTALL_DIR=/opt/x in setup.env
# warned that "the host and the inventory do not say the same thing" about the
# SAME directory: a message asserting a difference that does not exist, exactly
# the class of defect this PR has been closing. It is also the realistic case:
# the trailing slash is typed by the operator on the command line, not by
# cloud-init.
@test "install-release: an extra trailing slash does not invent a disagreement with the host" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf 'INSTALL_DIR=%s\n' "$INSTALL_DIR" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR/"
    [ "$status" -eq 0 ]
    refute_contains "$output" "WARNING"
    # And the destination is still the same tree, with no '//' on the make line.
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
    run grep -q "$INSTALL_DIR//" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: without WORKER_USER in the environment the chown comes from the host's setup.env" {
    worker_user="$(id -un)"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    printf 'WORKER_USER=%s\n' "$worker_user" > "$CONFIG_DIR/setup.env"
    # Without WORKER_USER in the environment: if the script fell back to the
    # 'orcaster' default, the chown and the `sudo -u` would go to the wrong user
    # (or it would abort saying the user does not exist, with the Bootstrap
    # guidance, on a host in steady state).
    run env CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -q "chown -R $worker_user:$worker_user $INSTALL_DIR" "$CALLS"
    grep -q "sudo -u $worker_user " "$CALLS"
    run grep -q "chown -R orcaster:orcaster" "$CALLS"
    [ "$status" -ne 0 ]
}

# --- The setup.env that root reads and the worker does not -------------------

@test "install-release: a setup.env that root reads but the worker cannot aborts before the swap" {
    # The gate's `[ -r ]` measures root, and root reads any mode. The real
    # consumers read it as the worker user: the Makefile's WORKER_MEMORY
    # fallback (container without a cap) and the load_env of 06-autostart.sh
    # (stopped systemd unit, failure swallowed by the Makefile).
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" SUDO_TEST_R_FAILS=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "cannot read it"
    assert_contains "$output" "The installed code was not touched"
    # The swap did not happen and the staging was left clean.
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    [ ! -d "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "install-release: an env.N that root reads but the worker cannot aborts before the swap" {
    # The gate's `[ -f ]` measures root. The real consumer of env.N is podman,
    # which opens it as the worker user through the --env-file of `make up
    # client`: with the file at 0600 root:root the gate passed, the swap was
    # done, and make died with the worker container already destroyed and the
    # following workers on dead inodes.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.2"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    # The previous delivery's rollback has to survive the abort: the message
    # claims that nothing was touched.
    previous="$(dirname "$INSTALL_DIR")/.orcaster.old"
    mkdir -p "$previous"
    echo "rollback-previous-delivery" > "$previous/marker.txt"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        SUDO_TEST_R_FAILS_FOR="env.2" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --workers 2
    [ "$status" -ne 0 ]
    # env.1 CAN be read: the message has to name the one that fails.
    assert_contains "$output" "$CONFIG_DIR/env.2"
    assert_contains "$output" "cannot read it"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run cat "$previous/marker.txt"
    [ "$output" = "rollback-previous-delivery" ]
    [ ! -d "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "install-release: a pubkey that root reads but the worker cannot aborts before the swap" {
    # The quietest of the three cases: the pubkey does not travel through
    # --env-file but through a bind mount, and mounting a file unreadable by the
    # process inside does NOT fail in podman run. The container came up healthy
    # and the worker rejected every signed task with no error anywhere.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    previous="$(dirname "$INSTALL_DIR")/.orcaster.old"
    mkdir -p "$previous"
    echo "rollback-previous-delivery" > "$previous/marker.txt"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        SUDO_TEST_R_FAILS_FOR="server-ed25519.pub" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "$CONFIG_DIR/server-ed25519.pub"
    assert_contains "$output" "cannot read it"
    # The concrete consequence is part of the message's contract: without it the
    # operator does not know which symptom to look for.
    assert_contains "$output" "every signed task"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run cat "$previous/marker.txt"
    [ "$output" = "rollback-previous-delivery" ]
    [ ! -d "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "install-release: a command that fails IN THE MIDDLE of setup.env aborts before the swap" {
    # The previous probe ran without errexit and with the source as the left
    # side of a `&&`: a top-level command that fails in the MIDDLE of the file
    # got past it (the `.` returns the status of the last command, an
    # assignment), and load_env died for the same reason after the swap. The
    # script's message claimed to cover exactly this case.
    #
    # Honest scope of this test depending on the bash that runs it: it gates
    # the absence of `set -euo pipefail`. It does NOT tell the `&&` form from
    # the `;` form under macOS's bash 3.2 (there errexit is not suspended on the
    # left side of `&&` either); that difference was measured by hand on Debian
    # 13 with bash 5.2, which is the production host's, and there the `&&` form
    # is inert.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    {
        echo "WORKER_MEMORY='28g'"
        echo "grep -q '^EXAMPLE_VAR_A=' $T/no-existe-este-archivo"
        echo "EGRESS_ISOLATION=0"
    } > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not source it"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a zero WORKER_MEMORY is rejected before the swap" {
    # The case that tells the real regex (^0*[1-9]...) from the naive one: '0g'
    # matches the format but is below podman's minimum, so the container is not
    # created AFTER up-client has already done `podman rm -f` on the previous
    # one.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_MEMORY=0g" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "WORKER_MEMORY"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a failing stop in the post-swap window says where everything was left" {
    # It was the only bare statement under `set -e` in the post-swap block: the
    # operator saw only systemd's stderr, with no way to know that the new code
    # was already installed or that no worker had been recreated.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "ORCH_ENROLLMENT_TOKEN=tok2" > "$CONFIG_DIR/env.2"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    echo "WORKER_COUNT=2" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        ACTIVE_UNITS="container-orcaster-worker-1.service" STOP_FAILS=1 \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "container-orcaster-worker-1.service"
    assert_contains "$output" "$INSTALL_DIR"
    assert_contains "$output" "$(dirname "$INSTALL_DIR")/.orcaster.old"
    assert_contains "$output" "were not recreated either"
    # And it did not keep recreating anything.
    run grep -q "make -C $INSTALL_DIR up client" "$CALLS"
    [ "$status" -ne 0 ]
}
# --- Installer edge cases (--config-dir, home, .orcaster.old) -----------------

# The host config directory had no option of its own alongside --install-dir:
# it only arrived through the CONFIG_DIR environment variable, which in
# production is not a channel (the invocation is `sudo bash`, and sudo with
# env_reset does not propagate the environment; ssh does not accept it either).
# A host whose config does not live in /etc/orcaster aborted at the pre-swap
# gate asking for /etc/orcaster/setup.env, on a host that never needed to
# migrate anything.
@test "install-release: --config-dir wins over the environment and resolves the env.N and the pubkey there" {
    cfg2="$T/srv/orcaster"
    mkdir -p "$cfg2"
    echo "# host setup.env, in a path that is not /etc/orcaster" > "$cfg2/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$cfg2/env.1"
    echo "server-pubkey" > "$cfg2/server-ed25519.pub"
    worker_user="$(id -un)"
    # The environment points to a directory that does NOT exist: if the option
    # did not win, the gate would abort over the missing setup.env instead of
    # installing.
    run env WORKER_USER="$worker_user" CONFIG_DIR="$T/etc/inexistente" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --config-dir "$cfg2"
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
    # The paths that the recreation passes to make come from --config-dir;
    # the Makefile derives env.1 and the pubkey from CONFIG_DIR.
    grep -q "ETC_SETUP_ENV_FILE=$cfg2/setup.env" "$CALLS"
    grep -q "CONFIG_DIR=$cfg2 " "$CALLS"
}

@test "install-release: --config-dir with a trailing slash leaves no double-slash paths" {
    cfg2="$T/srv/orcaster"
    mkdir -p "$cfg2"
    echo "# host setup.env" > "$cfg2/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$cfg2/env.1"
    echo "server-pubkey" > "$cfg2/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --config-dir "$cfg2///"
    [ "$status" -eq 0 ]
    grep -q "CONFIG_DIR=$cfg2 " "$CALLS"
    # The extra slash would work just the same, but a message with '//' reads
    # like a script error: that is why it is normalized.
    run grep -q "$cfg2//" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: a relative --config-dir aborts without touching the installed code" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" \
        --config-dir "etc/orcaster" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "absolute path"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: --config-dir with a semicolon aborts without touching the installed code" {
    # The value ends up interpolated into the `make` line of step 4: a
    # metacharacter stops being part of the argument and becomes a statement.
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" \
        --config-dir "/srv/orcaster;echo x" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "only accepts letters"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: --config-dir with a space aborts without touching the installed code" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" \
        --config-dir "/srv/task orch" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "only accepts letters"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

# Without sha256sum, the script fell back to `shasum -a 256` blindly. On a
# minimal Debian host without `perl` (shasum ships with that package, not with
# `perl-base`) that died with a "command not found" and the exit 127 of
# `set -e`: the operator learned that something failed, not that the integrity
# check never ran.
@test "install-release: without sha256sum or shasum it aborts saying it cannot verify" {
    bash_bin="$(command -v bash)"
    # PATH limited to the fakebin: it has `id` (the only thing the script needs
    # before resolving the hash tool) and neither of the two tools.
    run env PATH="$FAKE_ROOT_BIN" "$bash_bin" "$SCRIPT" \
        --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "neither sha256sum nor shasum"
    assert_contains "$output" "the installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

# $home feeds the HOME of every rootless podman and systemd --user of the
# deploy (run_as_worker). Empty, podman resolves its storage against
# /.local/share/containers: it fails, or worse, creates a new empty storage
# where the worker image is not. It used to be resolved AFTER the swap, so the
# operator saw only podman's message.
@test "install-release: an empty home of the worker user aborts before the swap" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    cat > "$FAKE_ROOT_BIN/getent" <<'EOS'
#!/usr/bin/env bash
if [ "$1" = "passwd" ] && [ -n "${2:-}" ]; then
    printf '%s:x:1000:1000:::/bin/bash\n' "$2"
    exit 0
fi
exit 2
EOS
    chmod +x "$FAKE_ROOT_BIN/getent"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "is not an absolute path"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a relative home of the worker user aborts before the swap" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    cat > "$FAKE_ROOT_BIN/getent" <<'EOS'
#!/usr/bin/env bash
if [ "$1" = "passwd" ] && [ -n "${2:-}" ]; then
    printf '%s:x:1000:1000::home/%s:/bin/bash\n' "$2" "$2"
    exit 0
fi
exit 2
EOS
    chmod +x "$FAKE_ROOT_BIN/getent"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "is not an absolute path"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a failing getent does not kill the deploy silently" {
    # With `pipefail`, `home="$(getent ... | cut ...)"` as a bare assignment
    # aborted the script with no message at all. That is why it lives in its
    # own `if`.
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    cat > "$FAKE_ROOT_BIN/getent" <<'EOS'
#!/usr/bin/env bash
echo "getent: failure simulated by the test" >&2
exit 2
EOS
    chmod +x "$FAKE_ROOT_BIN/getent"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "getent passwd"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

# The symlink already had its guard; a regular file at the install path had
# none, and the handler of the second `mv` ended up claiming something false.
@test "install-release: an INSTALL_DIR that is a regular file aborts with the cause" {
    rm -rf "$INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    echo "no soy un directorio" > "$INSTALL_DIR"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "is not a directory"
    # What it must NOT say: that this was a new installation. There was
    # something at the path and it is still there.
    refute_contains "$output" "new installation"
    run cat "$INSTALL_DIR"
    [ "$output" = "no soy un directorio" ]
}

# --workers with --no-restart is contradictory: the number is only consumed in
# the recreation, which --no-restart does not do. It used to be discarded
# silently (exit 0 without mentioning it), which is exactly what the rest of
# this script avoids.
@test "install-release: --workers with --no-restart warns that the number is ignored" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" \
        --workers 3 --no-restart
    # It does not abort: installing the code without touching containers is
    # exactly what --no-restart asks for.
    [ "$status" -eq 0 ]
    assert_contains "$output" "the number is ignored"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: plain --no-restart does not warn about any ignored number" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    refute_contains "$output" "the number is ignored"
}

# The two swap guards (symlink and non-directory) ran AFTER the
# `rm -rf "$previous"`, so an abort had already taken away the .orcaster.old of
# the previous delivery while the message claimed "The installed code was not
# touched": true of $INSTALL_DIR and false of the manual rollback that this
# very script promises and that its handlers name.
@test "install-release: the symlink abort does not take away the .orcaster.old" {
    previous="$(dirname "$INSTALL_DIR")/.orcaster.old"
    mkdir -p "$previous"
    echo "rollback-previous-delivery" > "$previous/marker.txt"
    rm -rf "$INSTALL_DIR"
    ln -s "$T/otro-arbol" "$INSTALL_DIR"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "is a symlink"
    # The rollback that the message claims is intact has to still be there.
    run cat "$previous/marker.txt"
    [ "$output" = "rollback-previous-delivery" ]
}

@test "install-release: the regular file abort does not take away the .orcaster.old" {
    previous="$(dirname "$INSTALL_DIR")/.orcaster.old"
    mkdir -p "$previous"
    echo "rollback-previous-delivery" > "$previous/marker.txt"
    rm -rf "$INSTALL_DIR"
    echo "no soy un directorio" > "$INSTALL_DIR"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "is not a directory"
    run cat "$previous/marker.txt"
    [ "$output" = "rollback-previous-delivery" ]
}

# A deploy that DOES reach the swap must keep recycling the .orcaster.old: the
# guard above moved the `rm -rf "$previous"`, it did not remove it.
@test "install-release: the swap still replaces the previous .orcaster.old" {
    previous="$(dirname "$INSTALL_DIR")/.orcaster.old"
    mkdir -p "$previous"
    echo "rollback-oldest" > "$previous/marker.txt"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
    # The .orcaster.old is now the code that this deploy replaced, not the one
    # from two deliveries ago.
    run cat "$previous/marker.txt"
    [ "$output" = "old-code" ]
}

# --- Validation of $INSTALL_DIR -----------------------------------------------
# $CONFIG_DIR had its three guards (character set, absolute path, trailing
# slash) from before; $INSTALL_DIR, which is the path the swap REPLACES, had
# none. The value does not arrive only through --install-dir: it also comes
# from the INSTALL_DIR of the host's setup.env, which this script does not
# control.

@test "install-release: --install-dir with a trailing slash over a symlink still aborts" {
    # The trailing slash was not cosmetic: it resolves the link, so `[ -L ]`
    # was FALSE and `[ -e ] && [ ! -d ]` passed too. BOTH swap guards were
    # inert, the `rm -rf "$previous"` took away the rollback of the previous
    # delivery, and the `mv` died afterwards with coreutils' bare message (its
    # rename() rejects a trailing slash).
    previous="$(dirname "$INSTALL_DIR")/.orcaster.old"
    mkdir -p "$previous"
    echo "rollback-previous-delivery" > "$previous/marker.txt"
    rm -rf "$INSTALL_DIR"
    ln -s "$T/otro-arbol" "$INSTALL_DIR"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR/" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "is a symlink"
    run cat "$previous/marker.txt"
    [ "$output" = "rollback-previous-delivery" ]
    # And the link is still in place, not swapped.
    [ -L "$INSTALL_DIR" ]
}

@test "install-release: --install-dir with a trailing slash leaves no double-slash paths" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR///" --no-restart
    [ "$status" -eq 0 ]
    assert_contains "$output" ">> Installed in $INSTALL_DIR"
    # It would work just the same with '//', but a message like that reads like
    # a script error: that is why it is normalized, same criterion as
    # --config-dir.
    refute_contains "$output" "$INSTALL_DIR//"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "new-code" ]
}

@test "install-release: a relative --install-dir aborts without touching the installed code" {
    # A relative path would be resolved against the cwd of this script (the
    # `sudo` one), so $staging and $previous, and with them the `rm -rf`, would
    # land next to a directory that is not the one the operator named.
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "opt/orcaster" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "absolute path"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: --install-dir with a semicolon aborts without touching the installed code" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "/opt/orcaster;echo x" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "only allows letters"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

# --- The RELEASE has to DECLARE a commit, not just exist ---------------------
# read_release_commit (setup-scripts/debian13-worker/lib.sh) treats a RELEASE
# without a commit= line as "no installed code" and returns rc=1. With only the
# `[ -f ]`, an empty or truncated manifest was installed with exit 0 and then
# should_bootstrap reported "bootstrap" on a healthy host, redoing the whole
# provisioning, while run-all-worker.sh could not state which code is running.

@test "install-release: a RELEASE without a commit line aborts without touching the installed code" {
    printf 'version=9.9.9\nbuilt=2026-08-18T00:00:00Z\n' > "$SRC/RELEASE"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "declares no commit"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    [ ! -d "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "install-release: a zero-byte RELEASE aborts without touching the installed code" {
    : > "$SRC/RELEASE"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "declares no commit"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

# --- --help prints the whole header and nothing else -------------------------
# The range of the `sed -n` is hand-written: adding lines to the header without
# recomputing it cuts the documentation in half or makes --help spit out the
# body of the script. And --install-dir is the remedy that three different
# abort messages name, but it used to be missing from the help.

@test "install-release: --help documents --install-dir and does not spit out code" {
    PATH="$REAL_PATH" run bash "$SCRIPT" --help
    [ "$status" -eq 0 ]
    assert_contains "$output" "--install-dir"
    assert_contains "$output" "/opt/orcaster"
    refute_contains "$output" "set -euo pipefail"
}

# This is the case that used to pass both format guards (absolute path, no
# metacharacters) and only failed after the swap, when podman could no longer
# find the HOME it was given.
@test "install-release: a missing home of the worker user aborts before the swap" {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
    cat > "$FAKE_ROOT_BIN/getent" <<'EOS'
#!/usr/bin/env bash
if [ "$1" = "passwd" ] && [ -n "${2:-}" ]; then
    printf '%s:x:1000:1000::/home/no-existe-%s:/bin/bash\n' "$2" "$2"
    exit 0
fi
exit 2
EOS
    chmod +x "$FAKE_ROOT_BIN/getent"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "does not exist on this host, or is not a directory"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: --config-dir at the root aborts without touching the installed code" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" \
        --config-dir "/" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "cannot be the root"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

# "//" normalizes to "/" via the trailing-slash rtrim: a guard placed before
# that rtrim would be inert here.
@test "install-release: --config-dir with two slashes also aborts as the root" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" \
        --config-dir "//" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "cannot be the root"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: --config-dir with .. aborts without touching the installed code" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" \
        --config-dir "$T/srv/../etc/orcaster" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "does not accept '..' as a path component"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: a config directory with two dots in its name is still legal" {
    cfg2="$T/etc/a..b"
    mkdir -p "$cfg2"
    echo "# test setup.env" > "$cfg2/setup.env"
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$cfg2/env.1"
    echo "server-pubkey" > "$cfg2/server-ed25519.pub"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --config-dir "$cfg2"
    [ "$status" -eq 0 ]
    grep -q "CONFIG_DIR=$cfg2 " "$CALLS"
}

# Root and '..' guards on INSTALL_DIR: parity with the CONFIG_DIR guards.
# The guard runs after the rtrim and before the dirname, so it aborts before
# staging/previous are computed and before the extraction.
@test "install-release: --install-dir at the root aborts without touching the installed code" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "/" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "cannot be the root"
    # The guard runs before the dirname: nothing is extracted into //.orcaster.new.
    [ ! -e /.orcaster.new ]
}

# "//" normalizes to "/" through the rtrim of trailing slashes: a guard placed
# before that rtrim would be inert here.
@test "install-release: --install-dir with two slashes also aborts as the root" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "//" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "cannot be the root"
    [ ! -e /.orcaster.new ]
}

@test "install-release: --install-dir with .. aborts without touching the installed code" {
    # The '..' resolves to the same real directory, so the old marker proves
    # that the guard aborted before the swap.
    run bash "$SCRIPT" --tarball "$TGZ" \
        --install-dir "$T/opt/../opt/orcaster" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "does not allow '..' as a path component"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
}

@test "install-release: an install-dir with two dots in its name is still legal" {
    install2="$T/opt/a..b"
    mkdir -p "$install2"
    echo "old-code" > "$install2/marker.txt"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$install2" --no-restart
    [ "$status" -eq 0 ]
    run cat "$install2/marker.txt"
    [ "$output" = "new-code" ]
}

# --- Pre-swap probe of the systemd --user manager ----------------------------
# With an autostart unit file present and the manager unreachable, step 4
# would stop AFTER the swap, with all the workers on replaced inodes.

@test "install-release: unreachable systemd --user manager with an autostart unit aborts before the swap" {
    printf 'ORCH_ENROLLMENT_TOKEN=tok\n' | worker_host
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    # Empty /run/user base: no uid dir, like a host without linger. Without
    # this the branch depended on whether the host running the bats has a real
    # /run/user/<uid> (a GitHub runner does, a container does not).
    mkdir -p "$T/run/user"
    ORCASTER_RUN_USER_BASE="$T/run/user" BUS_DOWN=1 run_restart_deploy
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not reach the systemd --user manager"
    assert_contains "$output" "container-orcaster-worker-1.service"
    assert_contains "$output" "Failed to connect to bus"
    assert_contains "$output" "loginctl enable-linger"
    refute_contains "$output" "exists but the manager does not respond"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
    run grep -q "up client" "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q " stop " "$CALLS"
    [ "$status" -ne 0 ]
}

@test "install-release: downed systemd --user manager with /run/user/<uid> present points to user@<uid>.service" {
    # The other branch of the diagnosis: linger is on (the uid dir exists) but
    # user@<uid>.service is down. The remedy is not enable-linger.
    printf 'ORCH_ENROLLMENT_TOKEN=tok\n' | worker_host
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-worker-1.service"
    uid="$(/usr/bin/id -u)"
    mkdir -p "$T/run/user/$uid"
    ORCASTER_RUN_USER_BASE="$T/run/user" BUS_DOWN=1 run_restart_deploy
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not reach the systemd --user manager"
    assert_contains "$output" "exists but the manager does not respond"
    assert_contains "$output" "systemctl status user@$uid.service"
    refute_contains "$output" "loginctl enable-linger"
    assert_contains "$output" "The installed code was not touched"
    run cat "$INSTALL_DIR/marker.txt"
    [ "$output" = "old-code" ]
    run test -e "$(dirname "$INSTALL_DIR")/.orcaster.old"
    [ "$status" -ne 0 ]
}

@test "install-release: with an autostart unit and a reachable manager it probes and deploys" {
    printf 'ORCH_ENROLLMENT_TOKEN=tok\n' | worker_host
    mkdir -p "$FAKE_HOME/.config/systemd/user"
    : > "$FAKE_HOME/.config/systemd/user/container-orcaster-proxy-1.service"
    run_restart_deploy
    [ "$status" -eq 0 ]
    grep -q "systemctl --user show-environment" "$CALLS"
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
}

@test "install-release: without autostart units it does not require the systemd --user manager" {
    printf 'ORCH_ENROLLMENT_TOKEN=tok\n' | worker_host
    BUS_DOWN=1 run_restart_deploy
    [ "$status" -eq 0 ]
    run grep -q "show-environment" "$CALLS"
    [ "$status" -ne 0 ]
    grep -q "make -C $INSTALL_DIR up client N=1" "$CALLS"
}

# --- Root's copy of the release (SOURCE_DIR) ---------------------------------
# INSTALL_DIR stays owned by the worker user: root executes nothing from
# there. The script leaves a root copy of the same release in SOURCE_DIR and
# sources the lib.sh of that copy. `ls -lnd` goes through the stub of
# source_guard_stub.bash.

# Prepares a steady-state host for the RESTART=1 path (one worker).
restart_host() {
    echo "ORCH_ENROLLMENT_TOKEN=tok" > "$CONFIG_DIR/env.1"
    echo "server-pubkey" > "$CONFIG_DIR/server-ed25519.pub"
}

@test "source-dir: leaves a root copy of the release, with no group or other write permission" {
    mkdir -p "$SOURCE_DIR"
    echo "release-anterior" > "$SOURCE_DIR/marker.txt"
    printf 'x\n' > "$SRC/writable.txt"
    chmod 0666 "$SRC/writable.txt"
    tar czf "$TGZ" -C "$T/src" orcaster
    ( cd "$T" && shasum -a 256 "$(basename "$TGZ")" > "$TGZ.sha256" )
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -eq 0 ]
    [ "$(cat "$SOURCE_DIR/marker.txt")" = "new-code" ]
    [ -f "$SOURCE_DIR/RELEASE" ]
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "new-code" ]
    # go-w applied to the copy (the chown root:root is a mock: the bats do not
    # run as root, the call is verified).
    [ "$(/bin/ls -ln "$SOURCE_DIR/writable.txt" | cut -c1-10)" = "-rw-r--r--" ]
    grep -q "^chown -R root:root $SOURCE_DIR.new$" "$CALLS"
    [ ! -e "$SOURCE_DIR.new" ]
    [ ! -e "$SOURCE_DIR.old" ]
    assert_contains "$output" "Root's copy of the release in $SOURCE_DIR"
}

@test "source-dir: --source-dir wins over the environment variable" {
    mkdir -p "$T/otra-raiz"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart \
        --source-dir "$T/otra-raiz/orcaster"
    [ "$status" -eq 0 ]
    [ "$(cat "$T/otra-raiz/orcaster/marker.txt")" = "new-code" ]
    [ ! -e "$SOURCE_DIR" ]
}

@test "source-dir: with no option or environment it comes from the host's setup.env" {
    restart_host
    mkdir -p "$T/host-raiz"
    echo "SOURCE_DIR=$T/host-raiz/orcaster" > "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env -u SOURCE_DIR WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    [ "$(cat "$T/host-raiz/orcaster/marker.txt")" = "new-code" ]
}

@test "source-dir: under RESTART=1 the lib.sh comes from SOURCE_DIR and never from INSTALL_DIR" {
    restart_host
    add_lib_to_tarball <<'EOS'
echo "lib-sourced-from=${BASH_SOURCE[0]}" >> "$CALLS"
worker_unit() { printf 'container-orcaster-worker-%s.service\n' "$1"; }
proxy_unit() { printf 'container-orcaster-proxy-%s.service\n' "$1"; }
worker_envfile() { printf '%s/env.%s\n' "$2" "$1"; }
EOS
    # The chown to the worker user runs after the swap and before the source:
    # the mock simulates there a worker process that rewrites the lib.sh of
    # INSTALL_DIR. That content must never run as root.
    cat > "$FAKE_ROOT_BIN/chown" <<'EOS'
#!/usr/bin/env bash
echo "chown $*" >> "$CALLS"
for a in "$@"; do last="$a"; done
lib="$last/setup-scripts/debian13-worker/lib.sh"
case " $* " in *" root:root "*) ;; *)
    [ -f "$lib" ] && echo 'echo TAMPERED >> "$CALLS"' >> "$lib" ;;
esac
exit 0
EOS
    chmod +x "$FAKE_ROOT_BIN/chown"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -eq 0 ]
    grep -q "^lib-sourced-from=$SOURCE_DIR/setup-scripts/debian13-worker/lib.sh$" "$CALLS"
    run grep -q "lib-sourced-from=$INSTALL_DIR" "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q TAMPERED "$CALLS"
    [ "$status" -ne 0 ]
    # And the tampering did happen: the test is not green because of an inert mock.
    grep -q "TAMPERED" "$INSTALL_DIR/setup-scripts/debian13-worker/lib.sh"
}

@test "source-dir: inside INSTALL_DIR aborts before touching the installed code" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart \
        --source-dir "$INSTALL_DIR/suite"
    [ "$status" -ne 0 ]
    assert_contains "$output" "overlaps with $INSTALL_DIR"
    assert_contains "$output" "The installed code was not touched"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
    [ ! -e "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "source-dir: INSTALL_DIR inside SOURCE_DIR aborts before touching the installed code" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart \
        --source-dir "$T/opt"
    [ "$status" -ne 0 ]
    assert_contains "$output" "overlaps with"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
    [ ! -e "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "source-dir: the staging as SOURCE_DIR aborts" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart \
        --source-dir "$(dirname "$INSTALL_DIR")/.orcaster.new"
    [ "$status" -ne 0 ]
    assert_contains "$output" "overlaps with"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
}

@test "source-dir: a SOURCE_DIR that reaches INSTALL_DIR through a parent link aborts" {
    ln -s "$INSTALL_DIR" "$T/alias"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart \
        --source-dir "$T/alias/suite"
    [ "$status" -ne 0 ]
    assert_contains "$output" "overlaps with"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
}

@test "source-dir: a SOURCE_DIR that is a symbolic link aborts" {
    mkdir -p "$T/real-suite"
    ln -s "$T/real-suite" "$SOURCE_DIR"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "is a symbolic link"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
    [ -L "$SOURCE_DIR" ]
}

@test "source-dir: a relative SOURCE_DIR aborts" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart \
        --source-dir "root/orcaster"
    [ "$status" -ne 0 ]
    assert_contains "$output" "absolute path"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
}

@test "source-dir: a group-writable parent aborts naming the path and mode" {
    chmod g+w "$T/root"
    run env SOURCE_GUARD_FIXTURE="$T/root" bash "$SCRIPT" --tarball "$TGZ" \
        --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "$T/root has owner uid 0 and mode drwxrwxr-x"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
    [ ! -e "$SOURCE_DIR" ]
}

@test "source-dir: an ancestor owned by another user aborts" {
    echo "$T" > "$T/foreign"
    run env SOURCE_GUARD_FIXTURE="$T" SOURCE_GUARD_FOREIGN="$T/foreign" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "$T has owner uid 1001"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
}

@test "source-dir: a git checkout in SOURCE_DIR is not replaced" {
    mkdir -p "$SOURCE_DIR/.git"
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "git checkout"
    [ -d "$SOURCE_DIR/.git" ]
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
}

@test "source-dir: if the chown root:root of the copy fails it aborts without touching anything" {
    mkdir -p "$SOURCE_DIR"
    echo "release-anterior" > "$SOURCE_DIR/marker.txt"
    run env CHOWN_ROOT_FAILS=1 bash "$SCRIPT" --tarball "$TGZ" \
        --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not build root's copy"
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
    [ "$(cat "$SOURCE_DIR/marker.txt")" = "release-anterior" ]
    [ ! -e "$SOURCE_DIR.new" ]
    [ ! -e "$(dirname "$INSTALL_DIR")/.orcaster.new" ]
}

@test "source-dir: a group-writable setup.env aborts before it is sourced" {
    restart_host
    printf 'touch "%s"\n' "$T/sourced" > "$CONFIG_DIR/setup.env"
    chmod 664 "$CONFIG_DIR/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        SOURCE_GUARD_FIXTURE="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "not sourcing $CONFIG_DIR/setup.env as root"
    assert_contains "$output" "mode -rw-rw-r--"
    [ ! -e "$T/sourced" ]
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
}

@test "source-dir: --help documents --source-dir" {
    PATH="$REAL_PATH" run bash "$SCRIPT" --help
    [ "$status" -eq 0 ]
    assert_contains "$output" "--source-dir"
    assert_contains "$output" "/root/orcaster"
    refute_contains "$output" "set -euo pipefail"
}

@test "source-dir: setup.env is checked on every read, not just once" {
    # A setup.env that, when sourced, makes itself group-writable: the second
    # read has to refuse to source it.
    restart_host
    cat > "$CONFIG_DIR/setup.env" <<E
[ -e "$T/sourced-once" ] && touch "$T/sourced-twice"
touch "$T/sourced-once"
/bin/chmod g+w "\${BASH_SOURCE[0]}"
E
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        SOURCE_GUARD_FIXTURE="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ "$status" -ne 0 ]
    assert_contains "$output" "not sourcing $CONFIG_DIR/setup.env as root"
    [ -e "$T/sourced-once" ]
    [ ! -e "$T/sourced-twice" ]
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
}

@test "source-dir: setup.env is sourced through its physical path" {
    restart_host
    mv "$CONFIG_DIR" "$T/etc/orcaster-real"
    ln -s "$T/etc/orcaster-real" "$CONFIG_DIR"
    printf 'echo "${BASH_SOURCE[0]}" >> "%s"\n' "$T/sourced-from" \
        > "$T/etc/orcaster-real/setup.env"
    worker_user="$(id -un)"
    run env WORKER_USER="$worker_user" CONFIG_DIR="$CONFIG_DIR" \
        bash "$SCRIPT" --tarball "$TGZ" --install-dir "$INSTALL_DIR"
    [ -s "$T/sourced-from" ]
    run grep -vx "$T/etc/orcaster-real/setup.env" "$T/sourced-from"
    [ "$status" -ne 0 ]
}

@test "source-dir: a group-writable parent of INSTALL_DIR aborts before extracting" {
    # The release that is later copied to SOURCE_DIR is extracted there:
    # another user with write access could change it before the cp -a.
    chmod g+w "$T/opt"
    run env SOURCE_GUARD_FIXTURE="$T/opt" bash "$SCRIPT" --tarball "$TGZ" \
        --install-dir "$INSTALL_DIR" --no-restart
    [ "$status" -ne 0 ]
    assert_contains "$output" "the parent directory of INSTALL_DIR ($T/opt) must be root-only"
    assert_contains "$output" "$T/opt has owner uid 0 and mode drwxrwxr-x"
    [ ! -e "$T/opt/.orcaster.new" ]
    [ ! -e "$SOURCE_DIR" ]
    [ "$(cat "$INSTALL_DIR/marker.txt")" = "old-code" ]
}

@test "source-dir: a parent of INSTALL_DIR that does not exist yet is measured through its ancestor" {
    run bash "$SCRIPT" --tarball "$TGZ" --install-dir "$T/nuevo/sub/orcaster" --no-restart
    [ "$status" -eq 0 ]
    [ "$(cat "$T/nuevo/sub/orcaster/marker.txt")" = "new-code" ]
}
