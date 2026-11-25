#!/usr/bin/env bats
# The goreleaser before-hook copies root files (LICENSE, NOTICE, ...) into
# orcaster/. Goreleaser refuses to release from a dirty tree and the snapshot
# build does not check it, so every copied file must be ignored by
# orcaster/.gitignore, or the tagged release fails with "git is in a dirty
# state".
bats_require_minimum_version 1.5.0

setup() {
    ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    CONFIG="$ROOT/orcaster/.goreleaser.yaml"
    # Only the repository's own ignore rules count, never a global excludes file.
    export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
}

# copied_files: the names in the `for f in ...; do` list of the copy hook.
copied_files() {
    sed -n 's/.*for f in \([^;]*\); do.*cp .*/\1/p' "$CONFIG"
}

@test "goreleaser hooks: the copy hook list is found" {
    run copied_files
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    [[ "$output" == *LICENSE* ]]
}

@test "goreleaser hooks: every copied file is ignored by orcaster/.gitignore" {
    files="$(copied_files)"
    [ -n "$files" ]
    missing=""
    for f in $files; do
        if ! git -C "$ROOT/orcaster" check-ignore -q --no-index -- "$f"; then
            missing="$missing $f"
        fi
    done
    if [ -n "$missing" ]; then
        echo "not ignored by orcaster/.gitignore:$missing" >&2
        return 1
    fi
}
