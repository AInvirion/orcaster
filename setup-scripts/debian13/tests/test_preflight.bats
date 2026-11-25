#!/usr/bin/env bats
# 00-preflight.sh of the SERVER: requires fs.protected_hardlinks=1 (precondition
# for root's chown -R over INSTALL_DIR, which APP_USER controls). sysctl,
# apt-get and the rest are mocked: no root, no network.
#
# NOTE (bats-core 1.13 gotcha): @test names in plain ASCII.

load source_guard_stub
bats_require_minimum_version 1.5.0

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../00-preflight.sh"
    T="$(mktemp -d)"
    export INSTALL_DIR="$T/repo"
    CALLS="$T/calls.log"; export CALLS
    touch "$CALLS"
    export OS_RELEASE_FILE="$T/os-release"
    printf 'ID=debian\nVERSION_ID="13"\n' > "$OS_RELEASE_FILE"
    export ENV_FILE_OVERRIDE="$T/setup.env"
    : > "$ENV_FILE_OVERRIDE"

    mkdir -p "$T/bin"
    cat > "$T/bin/id" <<'M'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ] && [ -z "${2:-}" ]; then echo 0; exit 0; fi
exit 0
M
    for c in apt-get sudo useradd install; do
        printf '#!/usr/bin/env bash\necho "%s $*" >> "$CALLS"\nexit 0\n' "$c" > "$T/bin/$c"
    done
    cat > "$T/bin/sysctl" <<'M'
#!/usr/bin/env bash
echo "sysctl $*" >> "$CALLS"
[ "${SYSCTL_HARDLINKS:-1}" = unreadable ] && { echo "sysctl: cannot stat" >&2; exit 255; }
echo "${SYSCTL_HARDLINKS:-1}"
M
    chmod +x "$T/bin"/*
    write_ls_stub "$T/bin"
    export PATH="$T/bin:$PATH"
}

teardown() { rm -rf "$T"; }

@test "server 00-preflight: fs.protected_hardlinks=0 stops before apt" {
    SYSCTL_HARDLINKS=0 run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    echo "$output" | grep -qF -- "fs.protected_hardlinks=0"
    echo "$output" | grep -qF -- "sysctl -w fs.protected_hardlinks=1"
    run grep -q "^apt-get" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "server 00-preflight: fs.protected_hardlinks unreadable stops (fails closed)" {
    SYSCTL_HARDLINKS=unreadable run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    echo "$output" | grep -qF -- "Could not read the fs.protected_hardlinks sysctl"
    run grep -q "^apt-get" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "server 00-preflight: with fs.protected_hardlinks=1 the check neither aborts nor prints anything" {
    # Only the check is asserted (it queries the sysctl and does not mention it):
    # the rest of the script depends on the host (lib.sh reads a fixed
    # /etc/os-release) and may fail later for other reasons in this mocked
    # environment.
    SYSCTL_HARDLINKS=1 run bash "$SCRIPT"
    run grep -q protected_hardlinks <<<"$output"
    [ "$status" -ne 0 ]
    run grep -q "^sysctl -n fs.protected_hardlinks" "$CALLS"
    [ "$status" -eq 0 ]
}
