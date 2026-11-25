#!/usr/bin/env bats
# Tests for check-ro-mounts.sh: the ONLY validator of WORKER_RO_MOUNTS, used by
# load_env, cloud-init-worker.sh, run-fleet.sh, the pre-swap gate of
# install-release.sh and the Makefile.
#
# The test directories are created INSIDE this tests directory and not with a
# bare `mktemp -d`: on macOS that lands under /var/folders and on Linux under
# /tmp, and both are reserved paths that the validator rejects on purpose.
#
# bats gotcha in this repo: a `[[ ... ]]` that is not the last statement of a
# @test does NOT make it fail; content assertions go through these helpers.
assert_out() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

setup() {
    CHECK="${BATS_TEST_DIRNAME}/../check-ro-mounts.sh"
    FIX="$(mktemp -d "${BATS_TEST_DIRNAME}/ro-fixture.XXXXXX")"
    mkdir -p "$FIX/example-ro" "$FIX/snap"
    # Explicit mode: the "other" rule (below) cannot depend on the umask of
    # whoever runs the tests.
    chmod 0755 "$FIX/example-ro" "$FIX/snap"
}

teardown() {
    if [ -n "${FIX:-}" ]; then
        chmod -R u+rwx "$FIX" 2>/dev/null || true
        rm -rf "$FIX"
    fi
}

@test "check-ro-mounts: empty list is valid" {
    run bash "$CHECK" ""
    [ "$status" -eq 0 ]
    run bash "$CHECK" --host ""
    [ "$status" -eq 0 ]
}

@test "check-ro-mounts: one and two valid paths pass the syntax check" {
    run bash "$CHECK" "/opt/example-ro"
    [ "$status" -eq 0 ]
    run bash "$CHECK" "/opt/example-ro /srv/example-ro/snapshots"
    [ "$status" -eq 0 ]
}

@test "check-ro-mounts: rejects a relative path and names it" {
    run bash "$CHECK" "/opt/example-ro opt/example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "'opt/example-ro' is not an absolute path"
}

@test "check-ro-mounts: rejects colon, comma and wildcards" {
    run bash "$CHECK" "/opt/r:i"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/opt/r:i' has characters outside"
    run bash "$CHECK" "/opt/r,i"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/opt/r,i' has characters outside"
    # No glob expansion: the '*' reaches the validation as is.
    run bash "$CHECK" "/opt/*"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/opt/*' has characters outside"
}

@test "check-ro-mounts: a space splits the path and the relative piece is rejected" {
    run bash "$CHECK" "/opt/mi dir"
    [ "$status" -eq 1 ]
    assert_out "$output" "'dir' is not an absolute path"
}

@test "check-ro-mounts: rejects the root" {
    run bash "$CHECK" "/"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/' is the host root"
    run bash "$CHECK" "//"
    [ "$status" -eq 1 ]
    assert_out "$output" "is the host root"
}

@test "check-ro-mounts: rejects dot and dot-dot components" {
    run bash "$CHECK" "/opt/../etc"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/opt/../etc' has a '.' or '..' component"
    run bash "$CHECK" "/opt/./example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/opt/./example-ro' has a '.' or '..' component"
    run bash "$CHECK" "/opt/example-ro/.."
    [ "$status" -eq 1 ]
    # A name that contains '..' without being a component is legal.
    run bash "$CHECK" "/opt/a..b"
    [ "$status" -eq 0 ]
}

@test "check-ro-mounts: rejects double slash and trailing slash" {
    run bash "$CHECK" "//etc"
    [ "$status" -eq 1 ]
    assert_out "$output" "'//etc' is not in canonical form"
    run bash "$CHECK" "/opt/example-ro/"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/opt/example-ro/' is not in canonical form"
}

@test "check-ro-mounts: rejects exact reserved paths" {
    local r
    for r in /app /var/lib/orcaster /var/cache/orcaster /var/lib/orcaster-egress \
             /home/orcaster /proc /sys /dev /etc /usr /bin /sbin /lib /lib64 \
             /var /tmp /root /run; do
        run bash "$CHECK" "$r"
        [ "$status" -eq 1 ] || { echo "accepted $r"; return 1; }
        assert_out "$output" "'$r' is, is under, or contains"
    done
}

@test "check-ro-mounts: rejects paths below a reserved one" {
    run bash "$CHECK" "/app/src"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/app/src' is, is under, or contains /app"
    run bash "$CHECK" "/etc/orcaster"
    [ "$status" -eq 1 ]
    assert_out "$output" "contains /etc"
}

@test "check-ro-mounts: rejects parent paths of a reserved one" {
    run bash "$CHECK" "/home"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/home' is, is under, or contains /home/orcaster"
}

@test "check-ro-mounts: a textual prefix that is not a component does not collide" {
    # /etcetera is not under /etc, nor /apps under /app.
    run bash "$CHECK" "/etcetera /apps"
    [ "$status" -eq 0 ]
}

@test "check-ro-mounts: rejects duplicate entries" {
    run bash "$CHECK" "/opt/example-ro /opt/example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "'/opt/example-ro' is duplicated"
}

@test "check-ro-mounts: the syntax is fully checked before the host" {
    # The malformed entry is reported for what it is, even though the first
    # one does not exist on the host.
    run bash "$CHECK" --host "$FIX/does-not-exist relative"
    [ "$status" -eq 1 ]
    assert_out "$output" "'relative' is not an absolute path"
}

@test "check-ro-mounts --host: existing readable directories pass" {
    run bash "$CHECK" --host "$FIX/example-ro $FIX/snap"
    [ "$status" -eq 0 ]
}

@test "check-ro-mounts --host: rejects a nonexistent directory and names it" {
    run bash "$CHECK" --host "$FIX/example-ro $FIX/does-not-exist"
    [ "$status" -eq 1 ]
    assert_out "$output" "'$FIX/does-not-exist' does not exist on this host"
}

@test "check-ro-mounts --host: rejects a file that is not a directory" {
    : > "$FIX/a-file"
    run bash "$CHECK" --host "$FIX/a-file"
    [ "$status" -eq 1 ]
    assert_out "$output" "'$FIX/a-file' does not exist on this host, is not a directory"
}

@test "check-ro-mounts --host: rejects a directory the user cannot read" {
    if [ "$(id -u)" -eq 0 ]; then
        skip "root ignores mode bits: the case cannot be reproduced"
    fi
    chmod 0311 "$FIX/example-ro"
    run bash "$CHECK" --host "$FIX/example-ro"
    chmod 0755 "$FIX/example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "cannot read or traverse '$FIX/example-ro'"
}

@test "check-ro-mounts --host: rejects a directory the user cannot traverse" {
    if [ "$(id -u)" -eq 0 ]; then
        skip "root ignores mode bits: the case cannot be reproduced"
    fi
    chmod 0644 "$FIX/example-ro"
    run bash "$CHECK" --host "$FIX/example-ro"
    chmod 0755 "$FIX/example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "cannot read or traverse '$FIX/example-ro'"
}

@test "check-ro-mounts --host: rejects a symlink that resolves to a reserved path" {
    # /usr and not /etc: on macOS /etc is itself a symlink to /private/etc.
    ln -s /usr "$FIX/link-to-usr"
    run bash "$CHECK" --host "$FIX/link-to-usr"
    [ "$status" -eq 1 ]
    assert_out "$output" "'$FIX/link-to-usr' resolves to"
}

# --- Readability from the container uid ("other" bits) ------------------------
# The container process is uid 1000 of the image, a host subuid in rootless
# podman: neither the owner nor in the group. The user running the tests (the
# owner) can read these cases, and they still have to fail. They do not depend
# on sudo or root.

@test "check-ro-mounts --host: 0755 with 0644 files and a 0755 subdirectory passes" {
    mkdir "$FIX/example-ro/sub"
    chmod 0755 "$FIX/example-ro/sub"
    : > "$FIX/example-ro/pkg.whl"
    : > "$FIX/example-ro/sub/other.whl"
    chmod 0644 "$FIX/example-ro/pkg.whl" "$FIX/example-ro/sub/other.whl"
    run bash "$CHECK" --host "$FIX/example-ro"
    [ "$status" -eq 0 ]
}

@test "check-ro-mounts --host: rejects a 0750 directory (owner and group only)" {
    chmod 0750 "$FIX/example-ro"
    run bash "$CHECK" --host "$FIX/example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "'$FIX/example-ro' is not readable by \"other\""
    assert_out "$output" "host subuid"
}

@test "check-ro-mounts --host: rejects a 0700 directory even though the owner can read it" {
    chmod 0700 "$FIX/example-ro"
    run bash "$CHECK" --host "$FIX/example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "'$FIX/example-ro' is not readable by \"other\""
}

@test "check-ro-mounts --host: rejects a 0640 file inside and names it" {
    : > "$FIX/example-ro/pkg.whl"
    chmod 0640 "$FIX/example-ro/pkg.whl"
    run bash "$CHECK" --host "$FIX/example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "'$FIX/example-ro/pkg.whl' is not readable by \"other\""
    assert_out "$output" "chmod -R o+rX '$FIX/example-ro'"
}

@test "check-ro-mounts --host: rejects a nested 0750 subdirectory and names it" {
    mkdir -p "$FIX/example-ro/a/b"
    : > "$FIX/example-ro/a/b/pkg.whl"
    chmod 0644 "$FIX/example-ro/a/b/pkg.whl"
    chmod 0755 "$FIX/example-ro/a"
    chmod 0750 "$FIX/example-ro/a/b"
    run bash "$CHECK" --host "$FIX/example-ro"
    [ "$status" -eq 1 ]
    assert_out "$output" "'$FIX/example-ro/a/b' is not readable by \"other\""
}

@test "check-ro-mounts --host: a symlink entry is followed and its target is judged" {
    chmod 0750 "$FIX/snap"
    ln -s "$FIX/snap" "$FIX/link-to-snap"
    run bash "$CHECK" --host "$FIX/link-to-snap"
    [ "$status" -eq 1 ]
    assert_out "$output" "is not readable by \"other\""
}

@test "check-ro-mounts --host: an inner symlink is neither followed nor judged" {
    # Inside the container the symlink resolves against the container
    # filesystem, not the host's: the host's 0700 target says nothing.
    chmod 0700 "$FIX/snap"
    ln -s "$FIX/snap" "$FIX/example-ro/link-outside"
    run bash "$CHECK" --host "$FIX/example-ro"
    [ "$status" -eq 0 ]
}

@test "check-ro-mounts: no arguments is a usage error" {
    run bash "$CHECK"
    [ "$status" -eq 2 ]
}
