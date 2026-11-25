#!/usr/bin/env bats
# Tests of check-archives.sh on hand-made dist and components directories.
bats_require_minimum_version 1.5.0

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../check-archives.sh"
    T="$(mktemp -d)"
    DIST="$T/dist"
    COMP="$T/components"
    mkdir -p "$DIST" "$COMP" "$T/stage"
    for f in LICENSE NOTICE THIRD_PARTY README.md orcaster orcaster.exe; do printf '%s\n' "$f" >"$T/stage/$f"; done
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

# make_tgz NAME FILE... / make_zip NAME FILE...: archive FILEs from the stage dir.
make_tgz() { local n="$1"; shift; tar -czf "$DIST/$n" -C "$T/stage" "$@"; }
make_zip() { local n="$1"; shift; (cd "$T/stage" && zip -q "$DIST/$n" "$@"); }
# make_components: the three component files of 1.2.3.
make_components() {
    local n
    for n in orcaster-server-1.2.3.tar.gz orcaster-worker-1.2.3.tar.gz orcaster_worker-1.2.3-py3-none-any.whl; do
        printf 'content of %s\n' "$n" >"$COMP/$n"
    done
}
hash_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi
}
# sums: the checksums file lists every CLI archive and every component file.
sums() {
    local f
    : >"$DIST/orcaster-1.2.3-checksums.txt"
    for f in "$DIST"/orcaster*; do
        case "$f" in *-checksums.txt) continue ;; esac
        (cd "$DIST" && hash_of "$(basename "$f")") >>"$DIST/orcaster-1.2.3-checksums.txt"
    done
    for f in "$COMP"/*; do
        [ -e "$f" ] || continue
        (cd "$COMP" && hash_of "$(basename "$f")") >>"$DIST/orcaster-1.2.3-checksums.txt"
    done
}
# good_release: a complete, valid set of assets.
good_release() {
    make_tgz orcaster-cli-1.2.3-linux-amd64.tar.gz LICENSE NOTICE THIRD_PARTY README.md orcaster
    make_zip orcaster-cli-1.2.3-windows-amd64.zip LICENSE NOTICE THIRD_PARTY README.md orcaster.exe
    make_components
    sums
}
# drop_from_sums PATTERN: remove the lines of the checksums file that match.
drop_from_sums() {
    grep -v -- "$1" "$DIST/orcaster-1.2.3-checksums.txt" >"$T/sums" || true
    mv "$T/sums" "$DIST/orcaster-1.2.3-checksums.txt"
}

@test "check-archives: complete assets pass" {
    good_release
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 0 ]
    [[ "$output" == *"OK  1.2.3: 2 CLI archives"* ]]
}

@test "check-archives: the version may carry its v" {
    good_release
    run bash "$SCRIPT" "$DIST" v1.2.3 "$COMP"
    [ "$status" -eq 0 ]
}

@test "check-archives: an archive without LICENSE fails" {
    make_tgz orcaster-cli-1.2.3-linux-amd64.tar.gz NOTICE THIRD_PARTY README.md orcaster
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not contain LICENSE"* ]]
}

@test "check-archives: an archive without THIRD_PARTY fails" {
    make_zip orcaster-cli-1.2.3-windows-amd64.zip LICENSE NOTICE README.md orcaster.exe
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not contain THIRD_PARTY"* ]]
}

@test "check-archives: an archive without NOTICE fails" {
    make_tgz orcaster-cli-1.2.3-linux-amd64.tar.gz LICENSE THIRD_PARTY README.md orcaster
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not contain NOTICE"* ]]
}

@test "check-archives: an archive without the binary fails" {
    make_tgz orcaster-cli-1.2.3-linux-amd64.tar.gz LICENSE NOTICE THIRD_PARTY README.md
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not contain orcaster."* ]]
}

@test "check-archives: a windows archive needs orcaster.exe" {
    make_zip orcaster-cli-1.2.3-windows-amd64.zip LICENSE NOTICE THIRD_PARTY README.md orcaster
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not contain orcaster.exe"* ]]
}

@test "check-archives: an archive missing from the checksums file fails" {
    good_release
    drop_from_sums 'linux-amd64'
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"orcaster-cli-1.2.3-linux-amd64.tar.gz is not listed in orcaster-1.2.3-checksums.txt"* ]]
}

@test "check-archives: a wrong checksum fails" {
    good_release
    drop_from_sums 'linux-amd64'
    printf '%064d  orcaster-cli-1.2.3-linux-amd64.tar.gz\n' 0 >>"$DIST/orcaster-1.2.3-checksums.txt"
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"checksum of orcaster-cli-1.2.3-linux-amd64.tar.gz does not match"* ]]
}

@test "check-archives: a name outside orcaster-cli-<version>-<os>-<arch> fails" {
    make_tgz orcaster-cli-1.2.3-linux.tar.gz LICENSE NOTICE THIRD_PARTY README.md orcaster
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not follow orcaster-cli-1.2.3-<os>-<arch>"* ]]
}

@test "check-archives: the old name orcaster_1.2.3_linux_amd64.tar.gz is rejected" {
    make_tgz orcaster_1.2.3_linux_amd64.tar.gz LICENSE NOTICE THIRD_PARTY README.md orcaster
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not follow orcaster-cli-1.2.3-<os>-<arch>"* ]]
}

@test "check-archives: a windows archive that is not .zip fails" {
    make_tgz orcaster-cli-1.2.3-windows-amd64.tar.gz LICENSE NOTICE THIRD_PARTY README.md orcaster.exe
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"windows archives are .zip"* ]]
}

@test "check-archives: no CLI archive fails" {
    make_components; sums
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"no orcaster-cli-* archive"* ]]
}

@test "check-archives: a missing component file fails" {
    good_release
    rm "$COMP/orcaster-worker-1.2.3.tar.gz"
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"missing $COMP/orcaster-worker-1.2.3.tar.gz"* ]]
}

@test "check-archives: a component missing from the checksums file fails" {
    good_release
    drop_from_sums 'orcaster_worker-'
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"orcaster_worker-1.2.3-py3-none-any.whl is not listed"* ]]
}

@test "check-archives: a wrong checksum of a component fails" {
    good_release
    printf 'changed\n' >"$COMP/orcaster-server-1.2.3.tar.gz"
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"checksum of orcaster-server-1.2.3.tar.gz does not match"* ]]
}

@test "check-archives: an unexpected file in the components directory fails" {
    good_release
    printf 'x\n' >"$COMP/extra.txt"
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"unexpected file extra.txt"* ]]
}

@test "check-archives: a missing checksums file fails" {
    good_release
    rm "$DIST/orcaster-1.2.3-checksums.txt"
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"missing $DIST/orcaster-1.2.3-checksums.txt"* ]]
}

@test "check-archives: the old checksums.txt name is not accepted" {
    good_release
    mv "$DIST/orcaster-1.2.3-checksums.txt" "$DIST/checksums.txt"
    run bash "$SCRIPT" "$DIST" 1.2.3 "$COMP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"orcaster-1.2.3-checksums.txt"* ]]
}

@test "check-archives: missing arguments are a usage error" {
    run bash "$SCRIPT" "$DIST" 1.2.3
    [ "$status" -ne 0 ]
    [[ "$output" == *"usage: check-archives.sh"* ]]
}
