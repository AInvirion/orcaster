#!/usr/bin/env bats
# `.openssl-check` of the root Makefile: it tests the CAPABILITY to generate
# Ed25519 keys, not just that an `openssl` exists in PATH. The LibreSSL that
# macOS ships in /usr/bin/openssl exists but cannot generate Ed25519, and the
# guard used to let it pass: `keygen-server` then failed with an openssl error
# that did not name the cause.
#
# A fake `openssl` at the front of PATH covers both cases without depending on
# the openssl of whoever runs bats. The real case (OpenSSL 1.1.1+) runs
# `keygen-server` end to end with the system openssl, or with Homebrew's if the
# first one in PATH is LibreSSL; with neither, it is skipped.
#
# Bats gotchas of this repo: a `[[ ... ]]` or a `! cmd` that is not the last
# statement of a @test does NOT make it fail. Assertions go through these
# helpers and grep negations through `run` + `$status`.
assert_out() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

refute_out() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected '$2' in the output:"; echo "$1"; return 1; }
}

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    T="$(mktemp -d)"
    mkdir -p "$T/bin"
    ORIG_PATH="$PATH"
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

# Fake openssl WITHOUT Ed25519: `version` answers like macOS's LibreSSL and
# `genpkey` fails like it. Any other call is noted in $T/calls to prove that
# keygen-server did not get to run it.
stub_openssl_without_ed25519() {
    cat > "$T/bin/openssl" <<M
#!/usr/bin/env bash
echo "openssl \$*" >> "$T/calls"
case "\${1:-}" in
    version) echo "LibreSSL 3.3.6"; exit 0 ;;
    genpkey) echo "genpkey: algorithm Ed25519 not found" >&2; exit 1 ;;
esac
exit 1
M
    chmod +x "$T/bin/openssl"
    export PATH="$T/bin:$ORIG_PATH"
}

# Fake openssl WITH Ed25519: `genpkey` prints a PEM to stdout and exits 0.
stub_openssl_with_ed25519() {
    cat > "$T/bin/openssl" <<'M'
#!/usr/bin/env bash
case "${1:-}" in
    version) echo "OpenSSL 3.0.13 30 Jan 2024"; exit 0 ;;
    genpkey) printf -- '-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n'; exit 0 ;;
esac
exit 0
M
    chmod +x "$T/bin/openssl"
    export PATH="$T/bin:$ORIG_PATH"
}

# A real openssl capable of Ed25519, or empty if there is none.
real_openssl_dir() {
    local cand prefix
    cand="$(command -v openssl 2>/dev/null || true)"
    if [ -n "$cand" ] && "$cand" genpkey -algorithm Ed25519 >/dev/null 2>&1; then
        dirname "$cand"; return 0
    fi
    if command -v brew >/dev/null 2>&1; then
        prefix="$(brew --prefix openssl 2>/dev/null || true)"
        if [ -x "$prefix/bin/openssl" ] \
            && "$prefix/bin/openssl" genpkey -algorithm Ed25519 >/dev/null 2>&1; then
            echo "$prefix/bin"; return 0
        fi
    fi
    return 0
}

run_keygen() {
    run make -C "$REPO_ROOT" keygen-server \
        SERVER_KEY="$T/out/key.pem" WORKER_PUBKEY="$T/out/key.pub" \
        SERVER_ENV="$T/out/env"
}

@test "openssl-check: openssl without Ed25519 stops naming the version and the fix" {
    stub_openssl_without_ed25519
    run make -C "$REPO_ROOT" .openssl-check
    [ "$status" -ne 0 ]
    assert_out "$output" "cannot generate Ed25519 keys"
    assert_out "$output" "LibreSSL 3.3.6"
    assert_out "$output" "brew install openssl"
    assert_out "$output" "first in PATH"
    refute_out "$output" "uses LibreSSL"
    refute_out "$output" "use LibreSSL"
}

@test "openssl-check: keygen-server with openssl without Ed25519 stops before generating" {
    stub_openssl_without_ed25519
    run_keygen
    [ "$status" -ne 0 ]
    assert_out "$output" "LibreSSL 3.3.6"
    assert_out "$output" ".openssl-check"
    [ ! -e "$T/out/key.pem" ]
    [ ! -e "$T/out/key.pub" ]
    [ ! -e "$T/out/env" ]
    # Only the guard's two calls: the capability probe and the version.
    run grep -F -- "-out" "$T/calls"
    [ "$status" -ne 0 ]
    run grep -E "^openssl pkey" "$T/calls"
    [ "$status" -ne 0 ]
}

@test "openssl-check: without openssl in PATH it still asks to install it without recommending LibreSSL" {
    # PATH without openssl: only the binaries the recipe needs.
    local b
    for b in sh bash make; do
        ln -s "$(command -v "$b")" "$T/bin/$b"
    done
    PATH="$T/bin" run "$T/bin/make" -C "$REPO_ROOT" .openssl-check
    [ "$status" -ne 0 ]
    assert_out "$output" "not found in PATH"
    assert_out "$output" "brew install openssl"
    refute_out "$output" "uses LibreSSL"
    refute_out "$output" "use LibreSSL"
}

@test "openssl-check: openssl with Ed25519 passes without writing files" {
    stub_openssl_with_ed25519
    run make -C "$REPO_ROOT" .openssl-check
    [ "$status" -eq 0 ]
    refute_out "$output" "ERROR"
    refute_out "$output" "PRIVATE KEY"
    run ls -A "$T"
    [ "$output" = "bin" ]
}

@test "openssl-check: keygen-server with real OpenSSL generates key and pubkey" {
    local dir
    dir="$(real_openssl_dir)"
    if [ -z "$dir" ]; then
        skip "no openssl with Ed25519 (neither in PATH nor in brew --prefix openssl)"
    fi
    export PATH="$dir:$ORIG_PATH"
    run_keygen
    [ "$status" -eq 0 ]
    [ -s "$T/out/key.pem" ]
    [ -s "$T/out/key.pub" ]
    grep -q '^ED25519_PRIVATE_KEY_PATH=' "$T/out/env"
    "$dir/openssl" pkey -pubin -in "$T/out/key.pub" -noout
}
