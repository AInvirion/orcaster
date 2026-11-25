#!/usr/bin/env bats
# Tests of tag-release.sh and check-tags.sh against a throwaway repository and
# a local bare remote.
bats_require_minimum_version 1.5.0

setup() {
    TAG_SCRIPT="${BATS_TEST_DIRNAME}/../tag-release.sh"
    CHECK_SCRIPT="${BATS_TEST_DIRNAME}/../check-tags.sh"
    T="$(mktemp -d)"
    export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
    export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com
    export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
    git init -q --bare "$T/remote.git"
    git init -q -b main "$T/repo"
    cd "$T/repo"
    git remote add origin "$T/remote.git"
    git commit -q --allow-empty -m one
    C1="$(git rev-parse HEAD)"
    git commit -q --allow-empty -m two
    C2="$(git rev-parse HEAD)"
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

@test "tag-release: creates both tags at the commit and pushes them" {
    run bash "$TAG_SCRIPT" v1.2.3 --ref "$C1" --push
    [ "$status" -eq 0 ]
    [ "$(git rev-parse 'v1.2.3^{commit}')" = "$C1" ]
    [ "$(git rev-parse 'orcaster/v1.2.3^{commit}')" = "$C1" ]
    [ "$(git --git-dir="$T/remote.git" rev-parse 'orcaster/v1.2.3^{commit}')" = "$C1" ]
    [ "$(git --git-dir="$T/remote.git" rev-parse 'v1.2.3^{commit}')" = "$C1" ]
}

@test "tag-release: without --push nothing reaches the remote" {
    run bash "$TAG_SCRIPT" v1.2.3
    [ "$status" -eq 0 ]
    [[ "$output" == *"not pushed; run: git push --atomic origin refs/tags/v1.2.3 refs/tags/orcaster/v1.2.3"* ]]
    [ -z "$(git ls-remote --tags origin)" ]
}

@test "tag-release: keeps an existing release tag at the same commit" {
    git tag v1.2.3 "$C2"
    run bash "$TAG_SCRIPT" v1.2.3
    [ "$status" -eq 0 ]
    [[ "$output" == *"kept    v1.2.3"* ]]
    [[ "$output" == *"created orcaster/v1.2.3"* ]]
}

@test "tag-release: refuses to move a tag" {
    git tag v1.2.3 "$C1"
    run bash "$TAG_SCRIPT" v1.2.3 --ref "$C2"
    [ "$status" -eq 1 ]
    [[ "$output" == *"v1.2.3 already exists at $C1"* ]]
    run git rev-parse --verify --quiet refs/tags/orcaster/v1.2.3
    [ "$status" -ne 0 ]
}

@test "tag-release: refuses a tag that is not vX.Y.Z" {
    run bash "$TAG_SCRIPT" orcaster/v1.2.3
    [ "$status" -eq 1 ]
    [[ "$output" == *"is not a release tag"* ]]
}

@test "check-tags: passes when the module tag is at the release commit" {
    bash "$TAG_SCRIPT" v1.2.3 --ref "$C1" --push
    run bash "$CHECK_SCRIPT" v1.2.3 "$C1"
    [ "$status" -eq 0 ]
    [[ "$output" == *"OK  orcaster/v1.2.3 and v1.2.3 point at $C1"* ]]
}

@test "check-tags: passes with a lightweight module tag" {
    git tag orcaster/v1.2.3 "$C1"
    git push -q origin refs/tags/orcaster/v1.2.3
    run bash "$CHECK_SCRIPT" v1.2.3 "$C1"
    [ "$status" -eq 0 ]
}

@test "check-tags: fails when the module tag is missing" {
    git tag v1.2.3 "$C1"
    git push -q origin refs/tags/v1.2.3
    run bash "$CHECK_SCRIPT" v1.2.3 "$C1"
    [ "$status" -eq 1 ]
    [[ "$output" == *"orcaster/v1.2.3 does not exist on origin"* ]]
}

@test "check-tags: fails when the module tag points elsewhere" {
    git tag -a orcaster/v1.2.3 -m x "$C2"
    git push -q origin refs/tags/orcaster/v1.2.3
    run bash "$CHECK_SCRIPT" v1.2.3 "$C1"
    [ "$status" -eq 1 ]
    [[ "$output" == *"orcaster/v1.2.3 points at $C2, not at $C1"* ]]
}

@test "check-tags: orcaster/v1.2.30 does not satisfy v1.2.3" {
    git tag orcaster/v1.2.30 "$C1"
    git push -q origin refs/tags/orcaster/v1.2.30
    run bash "$CHECK_SCRIPT" v1.2.3 "$C1"
    [ "$status" -eq 1 ]
}
