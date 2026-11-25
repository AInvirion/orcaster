#!/usr/bin/env bats
# Tests of build-components.sh on a throwaway git repository.
bats_require_minimum_version 1.5.0

setup() {
    export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
    SRC="${BATS_TEST_DIRNAME}/.."
    T="$(mktemp -d)"
    REPO="$T/repo"
    OUT="$T/out"
    mkdir -p "$REPO/ops/release" "$REPO/client/src/orcaster_worker" "$REPO/server"
    cp "$SRC/make-release.sh" "$SRC/build-components.sh" "$REPO/ops/release/"
    write_client 1.2.3
    write_server 1.2.3
    : >"$REPO/client/src/orcaster_worker/__init__.py"
    git -C "$REPO" init -q
    git -C "$REPO" config user.name Test
    git -C "$REPO" config user.email test@example.com
    commit_all
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

write_client() {
    cat >"$REPO/client/pyproject.toml" <<EOF
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "orcaster-worker"
version = "$1"
requires-python = ">=3.13"
classifiers = ["Programming Language :: Python :: 3 :: Only"]

[tool.hatch.build.targets.wheel]
packages = ["src/orcaster_worker"]
EOF
}
write_server() {
    printf '[project]\nname = "orcaster-server"\nversion = "%s"\n' "$1" >"$REPO/server/pyproject.toml"
}
commit_all() {
    git -C "$REPO" add -A
    git -C "$REPO" commit -q -m test
}

@test "build-components: no arguments is a usage error" {
    run bash "$REPO/ops/release/build-components.sh"
    [ "$status" -eq 1 ]
    [[ "$output" == *"usage: build-components.sh"* ]]
}

@test "build-components: a tag without the vX.Y.Z format fails" {
    run bash "$REPO/ops/release/build-components.sh" 1.2.3 "$OUT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"is not a release tag"* ]]
    [ ! -e "$OUT" ]
}

@test "build-components: a server version that differs from the tag fails" {
    command -v uv >/dev/null 2>&1 || skip "uv is not installed"
    write_server 1.2.2
    commit_all
    run bash "$REPO/ops/release/build-components.sh" v1.2.3 "$OUT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"server/pyproject.toml"* ]]
    [[ "$output" != *"client/pyproject.toml"* ]]
    [ ! -e "$OUT" ]
}

@test "build-components: a client version that differs from the tag fails" {
    command -v uv >/dev/null 2>&1 || skip "uv is not installed"
    write_client 1.2.2
    commit_all
    run bash "$REPO/ops/release/build-components.sh" v1.2.3 "$OUT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"client/pyproject.toml"* ]]
    [[ "$output" != *"server/pyproject.toml"* ]]
    [ ! -e "$OUT" ]
}

@test "build-components: both versions wrong are both named" {
    command -v uv >/dev/null 2>&1 || skip "uv is not installed"
    run bash "$REPO/ops/release/build-components.sh" v2.0.0 "$OUT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"server/pyproject.toml"* ]]
    [[ "$output" == *"client/pyproject.toml"* ]]
    [ ! -e "$OUT" ]
}

@test "build-components: a non-empty OUT_DIR fails" {
    command -v uv >/dev/null 2>&1 || skip "uv is not installed"
    mkdir -p "$OUT"
    : >"$OUT/stray"
    run bash "$REPO/ops/release/build-components.sh" v1.2.3 "$OUT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"is not empty"* ]]
    [ "$(ls "$OUT")" = "stray" ]
}

@test "build-components: builds the three files; server and worker bundles are identical" {
    command -v uv >/dev/null 2>&1 || skip "uv is not installed"
    run --separate-stderr bash "$REPO/ops/release/build-components.sh" v1.2.3 "$OUT"
    if [ "$status" -ne 0 ]; then echo "$stderr" >&2; fi
    [ "$status" -eq 0 ]
    [ "$(ls "$OUT" | LC_ALL=C sort)" = "$(printf '%s\n' orcaster-server-1.2.3.tar.gz orcaster-worker-1.2.3.tar.gz orcaster_worker-1.2.3-py3-none-any.whl | LC_ALL=C sort)" ]
    cmp "$OUT/orcaster-server-1.2.3.tar.gz" "$OUT/orcaster-worker-1.2.3.tar.gz"
    tar -tzf "$OUT/orcaster-server-1.2.3.tar.gz" | grep -Fxq "orcaster/RELEASE"
}
