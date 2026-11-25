#!/usr/bin/env bats
# Integration of 00-preflight.sh with id/sudo/apt-get/useradd/usermod/loginctl/
# sleep/install mocked (no root, no network, no real systemd). Covers two
# ownership reconciliations, both justified because this is the only script that
# always runs AFTER the orcaster user is created:
#   - $CONFIG_DIR/setup.env: cloud-init-worker.sh may write it before the user
#     exists.
#   - the code tree in $INSTALL_DIR: the chown -R that 01-fetch-code.sh used to
#     do on every pass died with that script (tarball deployment); in the
#     Bootstrap flow, install-release.sh --no-restart installs the code BEFORE
#     this script creates orcaster.
#
# Scope note: these mocks simulate that chown/chmod IS INVOKED with the right
# arguments (we assert on the command, through CALLS), not that the real
# filesystem ends up with that owner: bats tests run as a normal user and a real
# chown to root:orcaster is not possible without privileges. Verifying that the
# host filesystem really ends up root:orcaster is the job of the real run on the
# VPS (smoke test / runbook).

# The fake `id -u` returns 0, so lib.sh's origin guard acts: stub of `ls -lnd`
# that presents the tree as root-owned.
load ../../debian13/tests/source_guard_stub

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../00-preflight.sh"
    T="$(mktemp -d)"
    export INSTALL_DIR="$T/repo"
    export CONFIG_DIR="$T/etc"
    mkdir -p "$CONFIG_DIR"
    CALLS="$T/calls.log"; export CALLS
    touch "$CALLS"

    # OS_RELEASE_FILE (test seam, see lib.sh): bats does not run on real Debian.
    export OS_RELEASE_FILE="$T/os-release"
    cat > "$OS_RELEASE_FILE" <<'EOF'
ID=debian
VERSION_ID="13"
EOF

    # Real setup.env in CONFIG_DIR, root:root from the test's point of view
    # (an earlier cloud-init "wrote" it before orcaster existed).
    cat > "$CONFIG_DIR/setup.env" <<'EOF'
ORCH_BASE_URL='https://orch.example.com'
EOF

    # Mocks in PATH: id (root + already existing orcaster with a fixed uid), sudo/
    # apt-get/useradd/usermod/loginctl (no-op, record the call), sleep
    # (instant: without it the runtime dir loop takes a real 10s), install
    # (creates the directory, as in test_worker_env.bats), chown (records the
    # call; the real chmod DOES run, because it needs no privileges on a file
    # that is already ours).
    mkdir -p "$T/bin"
    cat > "$T/bin/id" <<'M'
#!/usr/bin/env bash
# id -u => 0 (root, for need_root). id -u orcaster => simulated fixed uid.
# id orcaster (without -u) => exists (avoids creating the user, out of scope
# for this test).
if [ "${1:-}" = "-u" ] && [ -z "${2:-}" ]; then echo 0; exit 0; fi
if [ "${1:-}" = "-u" ]; then echo 1001; exit 0; fi
exit 0
M
    cat > "$T/bin/sudo" <<'M'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
exit 0
M
    cat > "$T/bin/apt-get" <<'M'
#!/usr/bin/env bash
echo "apt-get $*" >> "$CALLS"
exit 0
M
    cat > "$T/bin/useradd" <<'M'
#!/usr/bin/env bash
echo "useradd $*" >> "$CALLS"
exit 0
M
    cat > "$T/bin/usermod" <<'M'
#!/usr/bin/env bash
echo "usermod $*" >> "$CALLS"
exit 0
M
    cat > "$T/bin/loginctl" <<'M'
#!/usr/bin/env bash
echo "loginctl $*" >> "$CALLS"
exit 0
M
    cat > "$T/bin/sleep" <<'M'
#!/usr/bin/env bash
exit 0
M
    cat > "$T/bin/install" <<'M'
#!/usr/bin/env bash
echo "install $*" >> "$CALLS"
for last in "$@"; do :; done
mkdir -p "$last"
M
    cat > "$T/bin/chown" <<'M'
#!/usr/bin/env bash
echo "chown $*" >> "$CALLS"
[ "${CHOWN_FAILS:-0}" = 1 ] && exit 1
exit 0
M
    # Fake sysctl (fs.protected_hardlinks): SYSCTL_HARDLINKS=<value> returns it;
    # "unreadable" simulates sysctl failing. Default 1 (Debian).
    cat > "$T/bin/sysctl" <<'M'
#!/usr/bin/env bash
echo "sysctl $*" >> "$CALLS"
[ "${SYSCTL_HARDLINKS:-1}" = unreadable ] && { echo "sysctl: cannot stat" >&2; exit 255; }
echo "${SYSCTL_HARDLINKS:-1}"
M
    chmod +x "$T/bin/sysctl"
    # $INSTALL_DIR is left NOT created by hand (unlike CONFIG_DIR above): the
    # realistic path is a freshly provisioned host, where the script's
    # `install -d` of line 84 (mocked above: does mkdir -p of the last argument)
    # creates it, empty, only then. The chown -R over $INSTALL_DIR runs
    # afterwards, against that empty directory.
    chmod +x "$T/bin/id" "$T/bin/sudo" "$T/bin/apt-get" "$T/bin/useradd" \
        "$T/bin/usermod" "$T/bin/loginctl" "$T/bin/sleep" "$T/bin/install" \
        "$T/bin/chown"
    write_ls_stub "$T/bin"
    export PATH="$T/bin:$PATH"
}

teardown() { rm -rf "$T"; }

@test "00-preflight: reconciles the group of a pre-existing setup.env to root:orcaster" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "chown root:orcaster $CONFIG_DIR/setup.env" "$CALLS"
}

@test "00-preflight: if setup.env does not live in CONFIG_DIR, it does not chown it" {
    # Only realistic path where the "if it exists" guard is false: load_env read
    # from elsewhere via ENV_FILE_OVERRIDE (test seam, see lib.sh) and
    # CONFIG_DIR/setup.env was never written. In production, without the
    # override, load_env would already have died before getting here if the file
    # were missing.
    #
    # "No chown ran" cannot be asserted: the chown -R of $INSTALL_DIR always
    # runs, independent of setup.env. The assertion is narrowed to the specific
    # chown of setup.env, which is what this test covers.
    export ENV_FILE_OVERRIDE="$T/setup.env.alt"
    cp "$CONFIG_DIR/setup.env" "$ENV_FILE_OVERRIDE"
    rm -f "$CONFIG_DIR/setup.env"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    run grep -q "chown root:orcaster $CONFIG_DIR/setup.env" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "00-preflight: the setup.env reconciliation leaves the mode at 0640" {
    chmod 0600 "$CONFIG_DIR/setup.env"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    # GNU first and BSD after, and the order matters: GNU coreutils' `stat -f`
    # interprets the format as a FILE NAME, fails with rc=1 and STILL prints the
    # filesystem block to stdout, so the inverse form concatenated both outputs
    # and the test failed on Debian, which is the platform this suite's README
    # documents. BSD's `stat -c`, on the other hand, writes nothing to stdout when
    # it rejects the option.
    perm="$(stat -c '%a' "$CONFIG_DIR/setup.env" 2>/dev/null || stat -f '%Lp' "$CONFIG_DIR/setup.env")"
    [ "$perm" = "640" ]
}

# --- Ownership of the installed tree (the chown -R that was lost when -------
# 01-fetch-code.sh was removed with the tarball deployment) -----------

@test "00-preflight: converges ownership of the installed tree with chown -R" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q "chown -R orcaster:orcaster $INSTALL_DIR" "$CALLS"
}

@test "00-preflight: a chown -R that fails over INSTALL_DIR is fatal, not silent" {
    run env CHOWN_FAILS=1 bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"chown -R orcaster:orcaster $INSTALL_DIR"*"failed"* ]]
}

@test "00-preflight: an empty INSTALL_DIR (freshly provisioned host) does not break chown -R" {
    # install -d (mocked) creates an empty INSTALL_DIR at line 84; the chown -R
    # further down runs over that directory with nothing inside.
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ -d "$INSTALL_DIR" ]
    [ -z "$(ls -A "$INSTALL_DIR")" ]
}

# --- fs.protected_hardlinks=1 as a precondition of chown -R ------------------

@test "00-preflight: fs.protected_hardlinks=0 stops before touching anything" {
    SYSCTL_HARDLINKS=0 run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    echo "$output" | grep -qF -- "fs.protected_hardlinks=0"
    echo "$output" | grep -qF -- "sysctl -w fs.protected_hardlinks=1"
    run grep -qE "^(apt-get|chown)" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "00-preflight: unreadable fs.protected_hardlinks stops (fails closed)" {
    SYSCTL_HARDLINKS=unreadable run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    echo "$output" | grep -qF -- "Could not read the sysctl fs.protected_hardlinks"
    run grep -qE "^(apt-get|chown)" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "00-preflight: with fs.protected_hardlinks=1 it passes with no sysctl message" {
    SYSCTL_HARDLINKS=1 run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    run grep -q protected_hardlinks <<<"$output"
    [ "$status" -ne 0 ]
}
