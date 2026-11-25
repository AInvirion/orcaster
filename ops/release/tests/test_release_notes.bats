#!/usr/bin/env bats
# Tests of release-notes.sh: the CHANGELOG.md section of one version.
bats_require_minimum_version 1.5.0

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../release-notes.sh"
    T="$(mktemp -d)"
    cat >"$T/CHANGELOG.md" <<'EOF'
# Changelog

## [Unreleased]

## [1.2.10] - 2026-12-01

### Fixed
- Ten.

## [1.2.1] - 2026-11-20

### Added
- One.

## [1.0.0]

[1.2.1]: https://example.com/compare/v1.0.0...v1.2.1
EOF
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

@test "release-notes: prints the section of the version, trimmed" {
    run --separate-stderr bash "$SCRIPT" 1.2.1 "$T/CHANGELOG.md"
    [ "$status" -eq 0 ]
    [ "$output" = "$(printf '### Added\n- One.')" ]
}

@test "release-notes: accepts the tag with its v" {
    run --separate-stderr bash "$SCRIPT" v1.2.10 "$T/CHANGELOG.md"
    [ "$status" -eq 0 ]
    [ "$output" = "$(printf '### Fixed\n- Ten.')" ]
}

@test "release-notes: 1.2.1 does not match the heading of 1.2.10" {
    printf '# Changelog\n\n## [1.2.10]\n\n- Ten.\n' >"$T/CHANGELOG.md"
    run bash "$SCRIPT" 1.2.1 "$T/CHANGELOG.md"
    [ "$status" -eq 1 ]
    [[ "$output" == *'no "## [1.2.1]" heading'* ]]
}

@test "release-notes: a missing heading fails" {
    run bash "$SCRIPT" 9.9.9 "$T/CHANGELOG.md"
    [ "$status" -eq 1 ]
    [[ "$output" == *'no "## [9.9.9]" heading'* ]]
}

@test "release-notes: an empty section fails" {
    run bash "$SCRIPT" 1.0.0 "$T/CHANGELOG.md"
    [ "$status" -eq 1 ]
    [[ "$output" == *'section of'*'is empty'* ]]
}

@test "release-notes: a missing file fails" {
    run bash "$SCRIPT" 1.2.1 "$T/nope.md"
    [ "$status" -eq 1 ]
    [[ "$output" == *"not found"* ]]
}
