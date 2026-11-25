#!/usr/bin/env bats
# Tests of check-licenses.sh with stub `go` and `go-licenses` on PATH: the
# allowlist, unidentified licences and the notice entry per module version.
bats_require_minimum_version 1.5.0

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../check-licenses.sh"
    T="$(mktemp -d)"
    mkdir -p "$T/bin" "$T/mod"
    printf 'module example.com/app\n' >"$T/mod/go.mod"
    # Stub go: `go list -m` names the main module, `go list -deps` the linked ones.
    cat >"$T/bin/go" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    "list -m") echo "example.com/app" ;;
    list\ -deps*) cat "$STUB_MODULES" ;;
    *) echo "stub go: unexpected: $*" >&2; exit 2 ;;
esac
EOF
    # Stub go-licenses: prints the CSV report it was given.
    cat >"$T/bin/go-licenses" <<'EOF'
#!/usr/bin/env bash
cat "$STUB_REPORT"
EOF
    chmod +x "$T/bin/go" "$T/bin/go-licenses"
    export PATH="$T/bin:$PATH" STUB_MODULES="$T/modules.txt" STUB_REPORT="$T/report.csv"
    printf 'example.com/dep v1.2.3\n' >"$T/modules.txt"
    printf 'example.com/dep,https://example.com/LICENSE,MIT\n' >"$T/report.csv"
    printf 'Notices\n\nexample.com/dep\n  Version:    v1.2.3\n  Licence:    MIT\n' >"$T/THIRD_PARTY"
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

@test "check-licenses: allowed licence and listed version pass" {
    run bash "$SCRIPT" --module-dir "$T/mod" --notice "$T/THIRD_PARTY"
    [ "$status" -eq 0 ]
    [[ "$output" == *"OK  1 modules"* ]]
}

@test "check-licenses: a licence outside the allowlist fails" {
    printf 'example.com/dep,https://example.com/LICENSE,GPL-3.0\n' >"$T/report.csv"
    run bash "$SCRIPT" --module-dir "$T/mod" --notice "$T/THIRD_PARTY"
    [ "$status" -eq 1 ]
    [[ "$output" == *"example.com/dep has licence 'GPL-3.0'"* ]]
}

@test "check-licenses: an unidentified licence fails" {
    printf 'example.com/dep,Unknown,Unknown\n' >"$T/report.csv"
    run bash "$SCRIPT" --module-dir "$T/mod" --notice "$T/THIRD_PARTY"
    [ "$status" -eq 1 ]
    [[ "$output" == *"licence 'Unknown'"* ]]
}

@test "check-licenses: a module version missing from the notice fails" {
    printf 'example.com/dep v1.2.4\n' >"$T/modules.txt"
    run bash "$SCRIPT" --module-dir "$T/mod" --notice "$T/THIRD_PARTY"
    [ "$status" -eq 1 ]
    [[ "$output" == *"example.com/dep v1.2.4 has no entry"* ]]
}

@test "check-licenses: a module path only quoted in prose does not count as an entry" {
    printf 'Notices mention example.com/dep\n  Version:    v1.2.3\n' >"$T/THIRD_PARTY"
    run bash "$SCRIPT" --module-dir "$T/mod" --notice "$T/THIRD_PARTY"
    [ "$status" -eq 1 ]
    [[ "$output" == *"has no entry"* ]]
}

@test "check-licenses: a missing notice file fails" {
    run bash "$SCRIPT" --module-dir "$T/mod" --notice "$T/nope"
    [ "$status" -eq 1 ]
    [[ "$output" == *"third-party notice not found"* ]]
}
