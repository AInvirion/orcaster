#!/usr/bin/env bats
# Tests for make-release.sh: reproducible artifact, no .git inside and with
# the RELEASE manifest embedded.
#
# bats_require_minimum_version: the stdout-contract test uses
# `run --separate-stderr` because `run` merges stdout and stderr into $output
# by default (verified with a minimal test). The script emits a log on stderr
# in addition to the artifact path on stdout, so without separating the
# streams that test would fail even against a correct implementation.
bats_require_minimum_version 1.5.0

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../make-release.sh"
    T="$(mktemp -d)"
    OUT="$T/dist"
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

@test "make-release: produces tarball and sha256 in the output directory" {
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    [ -n "$tgz" ]
    [ -f "$tgz.sha256" ]
}

@test "make-release: the tarball does NOT contain .git" {
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    run tar tzf "$tgz"
    [ "$status" -eq 0 ]
    refute_contains "$output" "/.git/"
}

@test "make-release: embeds RELEASE with the version and commit of the ref" {
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    run tar xzOf "$tgz" orcaster/RELEASE
    [ "$status" -eq 0 ]
    assert_contains "$output" "version="
    assert_contains "$output" "commit=$(git -C "${BATS_TEST_DIRNAME}/../../.." rev-parse HEAD)"
}

@test "make-release: embedded RELEASE ends in a newline (does not lose the last line)" {
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    run bash -c "tar xzOf '$tgz' orcaster/RELEASE | wc -l"
    [ "$status" -eq 0 ]
    [ "$output" -eq 3 ]
}

@test "make-release: stdout is only the artifact path (contract with push-release)" {
    run --separate-stderr bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    [ -f "$output" ]
}

@test "make-release: the declared sha256 matches the tarball" {
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    cd "$OUT"
    # Same detection as make-release.sh and install-release.sh, and for the
    # same reason: `shasum` is a perl tool and a minimal Linux machine does not
    # have it (on Debian it comes with the `perl` package, not `perl-base`).
    # Hard-wiring it here would make this test die with "command not found" on
    # exactly the host where the script under test DOES work (using
    # sha256sum).
    if command -v sha256sum >/dev/null 2>&1; then
        run sha256sum -c "$(basename "$tgz").sha256"
    else
        run shasum -a 256 -c "$(basename "$tgz").sha256"
    fi
    [ "$status" -eq 0 ]
    # The `-c` of some implementations (the native Darwin sha256sum) warns and
    # still returns 0, so the hash is also compared as text, which is the same
    # explicit criterion install-release.sh uses (install-release.sh:185-192).
    run bash -c 'awk "{print \$1}" "$1.sha256"' _ "$(basename "$tgz")"
    [ "$status" -eq 0 ]
    declared="$output"
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$(basename "$tgz")" | awk '{print $1}')"
    else
        actual="$(shasum -a 256 "$(basename "$tgz")" | awk '{print $1}')"
    fi
    [ -n "$declared" ]
    [ "$declared" = "$actual" ]
}

@test "make-release: the sha256 records a file name, not a path" {
    run --separate-stderr bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$output"
    recorded="$(awk '{print $2}' "$tgz.sha256")"
    case "$recorded" in
        */*) echo "the .sha256 records a path, not a file name: $recorded"; return 1 ;;
    esac
    [ "$recorded" = "$(basename "$tgz")" ]
}

@test "make-release: leaves no .tmp files in the output directory after a successful run" {
    run --separate-stderr bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    run find "$OUT" -maxdepth 1 -name '.*.tmp'
    [ -z "$output" ]
}

@test "make-release: a second run on the same commit leaves the sha256 of the new tarball, not the old one" {
    run --separate-stderr bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    first_tgz="$output"
    first_sha="$(cat "$first_tgz.sha256")"
    # Wait for the next clock second so the manifest's `built=` (one-second
    # resolution) changes between runs and the second tarball differs in bytes
    # from the first, even though the commit and the file name are the SAME
    # (name is deterministic per commit).
    sleep 1
    run --separate-stderr bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    second_tgz="$output"
    [ "$second_tgz" = "$first_tgz" ]
    second_sha="$(cat "$second_tgz.sha256")"
    # If the fix did not publish the .sha256 from the tarball that is on disk
    # after the `mv`, this second run could let the sha256 of the FIRST
    # tarball through (the bug this suite exists to gate).
    [ "$second_sha" != "$first_sha" ]
    cd "$OUT"
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$(basename "$second_tgz")" | awk '{print $1}')"
    else
        actual="$(shasum -a 256 "$(basename "$second_tgz")" | awk '{print $1}')"
    fi
    declared="$(awk '{print $1}' "$(basename "$second_tgz").sha256")"
    [ "$declared" = "$actual" ]
}

@test "make-release: a hash that fails after publishing the tarball does not overwrite the previous good sha256" {
    # First good run: leaves the tarball + .sha256 pair.
    run --separate-stderr bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    good="$output"
    good_sha="$(cat "$good.sha256")"
    # Second run on the SAME commit (same tarball name), with sha256sum/shasum
    # replaced by a stub that always fails. The archiving (git archive + mv of
    # the tarball) runs normally -- the failure happens only at the hashing
    # step, which is the window the .sha256 temporary file protects. Without
    # the fix, the direct redirection (`>`) would have already truncated the
    # good .sha256 BEFORE the stub got to fail.
    FAKE="$T/fakehash"
    mkdir -p "$FAKE"
    {
        echo '#!/usr/bin/env bash'
        echo 'echo "fixture: hash command failure" >&2'
        echo 'exit 1'
    } > "$FAKE/sha256sum"
    cp "$FAKE/sha256sum" "$FAKE/shasum"
    chmod +x "$FAKE/sha256sum" "$FAKE/shasum"
    PATH="$FAKE:$PATH" run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    # The good .sha256 was neither truncated nor replaced by garbage: it is
    # still exactly the one from the previous run.
    [ "$(cat "$good.sha256")" = "$good_sha" ]
    # And no temporary file (neither of the tarball nor of the sha256) is left
    # lying around in $OUT.
    run find "$OUT" -maxdepth 1 -name '.*.tmp'
    [ -z "$output" ]
}

# The floor for --add-virtual-file is git 2.37, not 2.32 (that option does not
# exist in 2.32/2.34/2.35/2.36; it appeared in 2.37). These two tests mock
# `git` through PATH to answer a fixed version to `git --version` and delegate
# any other invocation to the real git, an idiom analogous to the one in
# test_install_release.bats (`id` stub) and
# setup-scripts/debian13-worker/tests/test_run_fleet.bats.
# Synthetic repo with the script inside: REPO_ROOT comes from BASH_SOURCE, so
# the only way to exercise ANOTHER client/pyproject.toml is to copy the script
# into a test repo. It is used for the formats of the `version` key, which is
# what ends up in the artifact name and in the `--tarball` line that runs on
# the host.
fixture_repo() {
    local pyproject_version="$1" repo="$T/repo"
    mkdir -p "$repo/ops/release" "$repo/client"
    cp "$SCRIPT" "$repo/ops/release/make-release.sh"
    printf '[project]\nname = "orcaster-worker"\n%s\n' "$pyproject_version" \
        > "$repo/client/pyproject.toml"
    git -C "$repo" init -q
    git -C "$repo" -c user.email=t@t -c user.name=t add -A
    git -C "$repo" -c user.email=t@t -c user.name=t commit -qm fixture
    printf '%s\n' "$repo"
}

@test "make-release: accepts the version with single quotes (valid TOML) without dragging in the whole line" {
    repo="$(fixture_repo "version = '1.2.3'")"
    run --separate-stderr bash "$repo/ops/release/make-release.sh" --out "$OUT"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    [ -f "$output" ]
    # Without the fix, the `sed` did not match and let the WHOLE LINE through:
    # the name ended up as `orcaster-version = '1.2.3'-<sha>.tgz` (spaces and
    # quotes included) and that string traveled to the host's `--tarball`.
    case "$(basename "$output")" in
        orcaster-1.2.3-*.tgz) ;;
        *) echo "unexpected name: $output"; return 1 ;;
    esac
    run tar xzOf "$output" orcaster/RELEASE
    [ "$status" -eq 0 ]
    [[ "$output" == *"version=1.2.3"* ]]
}

@test "make-release: a pyproject without a literal version aborts with the cause (does not die silently)" {
    repo="$(fixture_repo 'dynamic = ["version"]')"
    run bash "$repo/ops/release/make-release.sh" --out "$OUT"
    [ "$status" -ne 0 ]
    # This guard used to be unreachable: the `grep -m1` with no match exited 1
    # and pipefail killed the script one line earlier, printing nothing.
    [[ "$output" == *"could not read the version"* ]]
    # The abort happens BEFORE the mkdir of the output directory: nothing is
    # created on disk.
    [ ! -d "$OUT" ]
}

@test "make-release: --ref with an ANNOTATED tag records the commit, not the tag object" {
    repo="$(fixture_repo 'version = "1.2.3"')"
    # ANNOTATED tag (-a), which is the kind this repo's releases use. A
    # lightweight tag points straight at the commit and does not exercise the
    # defect.
    git -C "$repo" -c user.email=t@t -c user.name=t tag -a v9.9.9 -m "test release"
    tag_obj="$(git -C "$repo" rev-parse v9.9.9)"
    real_commit="$(git -C "$repo" rev-parse 'v9.9.9^{commit}')"
    # Precondition: if these two are equal, the tag is not annotated and the
    # test proves nothing.
    [ "$tag_obj" != "$real_commit" ]

    run --separate-stderr bash "$repo/ops/release/make-release.sh" --ref v9.9.9 --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$output"

    run tar xzOf "$tgz" orcaster/RELEASE
    [ "$status" -eq 0 ]
    # Without the `^{commit}`, `rev-parse` returns the SHA of the TAG OBJECT
    # and the manifest stores it in a field called `commit=`. The host is left
    # with an identity that names no commit, and the post-deploy verification
    # does not notice because it compares that same value against itself. The
    # tarball bytes are correct: `git archive` peels the tag on its own.
    assert_contains "$output" "commit=$real_commit"
    refute_contains "$output" "commit=$tag_obj"

    # The artifact name comes from the same rev-parse, so it must also derive
    # from the commit and not from the tag object.
    case "$(basename "$tgz")" in
        "orcaster-1.2.3-${real_commit:0:8}.tgz") ;;
        *) echo "unexpected name: $tgz (expected the short commit ${real_commit:0:8})"; return 1 ;;
    esac
}

@test "make-release: a failed archive does not leave a truncated tarball next to the old sha256" {
    # First good run: leaves the tarball + .sha256 pair.
    run --separate-stderr bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    good="$output"
    good_sha="$(cat "$good.sha256")"
    good_size="$(wc -c < "$good")"
    # Second run on the SAME commit (same file name) with a `git` that fails
    # right at `archive`: without the temporary file, `git archive -o`
    # truncated the destination to 0 bytes and the old .sha256 stayed intact.
    # The mock reproduces what the real `git archive` does when it fails: it
    # FIRST opens (and truncates) the `-o` file, and then dies. A mock that
    # just returns 128 without touching the destination gates nothing: the
    # test would pass anyway with the old code.
    REAL_GIT="$(command -v git)"
    FAKE="$T/fakegit"
    mkdir -p "$FAKE"
    {
        echo '#!/usr/bin/env bash'
        echo 'for a in "$@"; do if [ "$a" = "archive" ]; then'
        echo '    prev=""'
        echo '    for b in "$@"; do'
        echo '        case "$b" in -o) prev=o ;; *) if [ "$prev" = o ]; then : > "$b"; prev=""; fi ;; esac'
        echo '    done'
        echo '    echo "fatal: fixture" >&2; exit 128; fi; done'
        echo "exec $REAL_GIT \"\$@\""
    } > "$FAKE/git"
    chmod +x "$FAKE/git"
    PATH="$FAKE:$PATH" run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    [ "$(wc -c < "$good")" -eq "$good_size" ]
    [ "$(cat "$good.sha256")" = "$good_sha" ]
    # And the temporary file is not left lying around in $OUT.
    run find "$OUT" -name '.*.tmp'
    [ -z "$output" ]
}

@test "make-release: rejects git 2.36.x (the floor is 2.37, not 2.32)" {
    REAL_GIT="$(command -v git)"
    FAKE_BIN="$T/fakebin"
    mkdir -p "$FAKE_BIN"
    cat > "$FAKE_BIN/git" <<EOS
#!/usr/bin/env bash
if [ "\$1" = "--version" ]; then echo "git version 2.36.9"; exit 0; fi
exec "$REAL_GIT" "\$@"
EOS
    chmod +x "$FAKE_BIN/git"
    PATH="$FAKE_BIN:$PATH" run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "2.37"
}

@test "make-release: accepts git 2.37.0 (exact floor)" {
    REAL_GIT="$(command -v git)"
    FAKE_BIN="$T/fakebin"
    mkdir -p "$FAKE_BIN"
    cat > "$FAKE_BIN/git" <<EOS
#!/usr/bin/env bash
if [ "\$1" = "--version" ]; then echo "git version 2.37.0"; exit 0; fi
exec "$REAL_GIT" "\$@"
EOS
    chmod +x "$FAKE_BIN/git"
    PATH="$FAKE_BIN:$PATH" run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    [ -n "$tgz" ]
}

@test "make-release: relative --out is resolved against the operator's cwd, not two different ways" {
    WORKDIR="$T/where-the-operator-is"
    mkdir -p "$WORKDIR"
    run --separate-stderr bash -c "cd '$WORKDIR' && '$SCRIPT' --out reldist"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    case "$output" in
        /*) ;;
        *) echo "the printed path is not absolute: $output"; return 1 ;;
    esac
    [ -f "$output" ]
    tgz="$(find "$WORKDIR/reldist" -name 'orcaster-*.tgz' | head -n1)"
    [ -n "$tgz" ]
    [ "$output" = "$tgz" ]
}

assert_contains() {
    [[ "$1" == *"$2"* ]] || { echo "missing: $2"; echo "in: $1"; return 1; }
}

refute_contains() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected: $2"; echo "in: $1"; return 1; }
}

@test "make-release: the tarball has no entries writable by group or others" {
    # git archive applies tar.umask (default 0002) and produced 0775/0664. With
    # tar.umask=0022 the tree comes out 0755/0644, which is what the WARNING of
    # install-release.sh promises on the first delivery, and INSTALL_DIR is not
    # left group-writable while it is owned by root. The global config is
    # overridden so an operator's tar.umask does not change the result.
    run env GIT_CONFIG_GLOBAL=/dev/null bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    run tar tvzf "$tgz"
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    bad="$(printf '%s\n' "$output" | awk 'substr($1,1,1) != "l" && (substr($1,6,1) == "w" || substr($1,9,1) == "w")')"
    [ -z "$bad" ] || { echo "entries writable by group or others:"; echo "$bad" | head; return 1; }
    # And the tree does contain directories and executables: the check is not
    # green by being empty.
    printf '%s\n' "$output" | grep -q '^drwxr-xr-x'
    printf '%s\n' "$output" | grep -q '^-rwxr-xr-x'
}
