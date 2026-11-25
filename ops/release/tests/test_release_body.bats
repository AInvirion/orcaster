#!/usr/bin/env bats
# Tests of release-body.sh: the notes of a GitHub release.
bats_require_minimum_version 1.5.0

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../release-body.sh"
    T="$(mktemp -d)"
    cat >"$T/CHANGELOG.md" <<'EOF'
# Changelog

## [Unreleased]

## [1.2.3] - 2026-12-01

### Fixed
- Three.

## [1.2.2] - 2026-11-20

### Added
- Two.
EOF
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

@test "release-body: has the Server, Worker and CLI sections" {
    run --separate-stderr bash "$SCRIPT" 1.2.3 "$T/CHANGELOG.md"
    [ "$status" -eq 0 ]
    [[ "$output" == *"## Server"* ]]
    [[ "$output" == *"## Worker"* ]]
    [[ "$output" == *"## CLI"* ]]
}

@test "release-body: no @VERSION@ placeholder is left" {
    run --separate-stderr bash "$SCRIPT" 1.2.3 "$T/CHANGELOG.md"
    [ "$status" -eq 0 ]
    [[ "$output" != *"@VERSION@"* ]]
}

@test "release-body: names the assets of the version" {
    run --separate-stderr bash "$SCRIPT" 1.2.3 "$T/CHANGELOG.md"
    [ "$status" -eq 0 ]
    [[ "$output" == *"orcaster-server-1.2.3.tar.gz"* ]]
    [[ "$output" == *"orcaster-worker-1.2.3.tar.gz"* ]]
    [[ "$output" == *"orcaster-cli-1.2.3-"* ]]
    [[ "$output" == *"orcaster-1.2.3-checksums.txt"* ]]
}

@test "release-body: ends with the CHANGELOG section of the version" {
    run --separate-stderr bash "$SCRIPT" 1.2.3 "$T/CHANGELOG.md"
    [ "$status" -eq 0 ]
    [[ "$output" == *$'### Fixed\n- Three.' ]]
    [[ "$output" != *"- Two."* ]]
}

@test "release-body: accepts the version with and without v" {
    run --separate-stderr bash "$SCRIPT" v1.2.3 "$T/CHANGELOG.md"
    [ "$status" -eq 0 ]
    with_v="$output"
    run --separate-stderr bash "$SCRIPT" 1.2.3 "$T/CHANGELOG.md"
    [ "$status" -eq 0 ]
    [ "$output" = "$with_v" ]
}

@test "release-body: a missing CHANGELOG section fails with nothing on stdout" {
    run --separate-stderr bash "$SCRIPT" 9.9.9 "$T/CHANGELOG.md"
    [ "$status" -ne 0 ]
    [ -z "$output" ]
    [[ "$stderr" == *"9.9.9"* ]]
}

@test "release-body: a bad version is rejected" {
    run --separate-stderr bash "$SCRIPT" 1.2 "$T/CHANGELOG.md"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
    [[ "$stderr" == *"is not a version X.Y.Z"* ]]
}

@test "release-body: no argument is a usage error" {
    run --separate-stderr bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$stderr" == *"usage: release-body.sh"* ]]
}
