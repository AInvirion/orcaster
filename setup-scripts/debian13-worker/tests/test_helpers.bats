#!/usr/bin/env bats
# Tests of the pure functions in lib.sh (no root, no podman).

setup() {
    # shellcheck disable=SC1091
    source "${BATS_TEST_DIRNAME}/../lib.sh"
}

@test "worker_name: empty N => base name" {
    run worker_name ""
    [ "$status" -eq 0 ]
    [ "$output" = "orcaster-worker" ]
}

@test "worker_name: numeric N => suffix" {
    run worker_name 2
    [ "$output" = "orcaster-worker-2" ]
}

@test "worker_unit: derives the systemd unit of the container" {
    run worker_unit 3
    [ "$output" = "container-orcaster-worker-3.service" ]
}

@test "worker_unit: empty N => base unit" {
    run worker_unit ""
    [ "$output" = "container-orcaster-worker.service" ]
}

@test "worker_volume: numeric N => orcaster-data-N (mirrors the Makefile)" {
    run worker_volume 2
    [ "$output" = "orcaster-data-2" ]
}

@test "worker_volume: empty N => base volume" {
    run worker_volume ""
    [ "$output" = "orcaster-data" ]
}

@test "stamp_worker_env: writes the required keys" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok-abc" "fleet-1" ""
    grep -q '^ORCH_BASE_URL=https://orch.example.com$' "$env"
    grep -q '^ORCH_ENROLLMENT_TOKEN=tok-abc$' "$env"
    grep -q '^ORCH_WORKER_NAME=fleet-1$' "$env"
    grep -q '^ORCH_ED25519_PUBLIC_KEY_PATH=/var/lib/orcaster/ed25519.pub$' "$env"
    rm -rf "$tmp"
}

@test "stamp_worker_env: preserves the previous token/api_key on a manual rerun (no new token)" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    printf 'ORCH_API_KEY=old-key\nORCH_ENROLLMENT_TOKEN=old-tok\n' > "$env"
    stamp_worker_env "$env" "https://orch.example.com" "" "fleet-1" ""
    grep -q '^ORCH_API_KEY=old-key$' "$env"
    grep -q '^ORCH_ENROLLMENT_TOKEN=old-tok$' "$env"   # with no new token, the previous one wins
    rm -rf "$tmp"
}

@test "stamp_worker_env: rerun with a rotated token: the new non-empty token overrides the previous one" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    printf 'ORCH_API_KEY=apikey-x\nORCH_ENROLLMENT_TOKEN=old\n' > "$env"
    stamp_worker_env "$env" "https://orch.example.com" "new" "fleet-1" ""
    grep -q '^ORCH_ENROLLMENT_TOKEN=new$' "$env"
    grep -q '^ORCH_API_KEY=apikey-x$' "$env"
    rm -rf "$tmp"
}

@test "stamp_worker_env: appends a multiline EXTRA_ENV" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" $'API_TOKEN=tok_xxx\nGITHUB_TOKEN=ghp_yyy'
    grep -q '^API_TOKEN=tok_xxx$' "$env"
    grep -q '^GITHUB_TOKEN=ghp_yyy$' "$env"
    rm -rf "$tmp"
}

@test "stamp_worker_env: without EXTRA_ENV it does not add the section" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" ""
    run grep -q 'EXTRA_ENV' "$env"
    [ "$status" -ne 0 ]
    rm -rf "$tmp"
}

# --- Worker disk gate -------------------------------------------
# Channel separate from EXTRA_ENV: configuration of the worker itself, not
# runtime secrets of the tasks.

@test "stamp_worker_env: stamps the disk gate when values are passed" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" "" "0.05" "600"
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05$' "$env"
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600$' "$env"
    rm -rf "$tmp"
}

@test "stamp_worker_env: without gate values it does not add those lines" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" ""
    run grep -q 'CACHE_CLAIM' "$env"
    [ "$status" -ne 0 ]
    rm -rf "$tmp"
}

@test "stamp_worker_env: accepts the gate floor at 0 without treating it as empty" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" "" "0"
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0$' "$env"
    rm -rf "$tmp"
}

@test "stamp_worker_env: the disk gate block is separate from the EXTRA_ENV one" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" "API_TOKEN=tok_xxx" "0.05" "600"
    gate_line="$(grep -n '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=' "$env" | head -n1 | cut -d: -f1)"
    extra_header_line="$(grep -n 'EXTRA_ENV' "$env" | head -n1 | cut -d: -f1)"
    [ -n "$gate_line" ]
    [ -n "$extra_header_line" ]
    [ "$gate_line" -ne "$extra_header_line" ]
    rm -rf "$tmp"
}

@test "stamp_worker_env: the disk gate survives a rerun that passes the same value again" {
    # Regression: a rerun that DOES pass the same value again (because
    # setup.env still has it) must keep it in the .env.
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" "" "0.05" "600"
    stamp_worker_env "$env" "https://orch.example.com" "" "fleet-1" "" "0.05" "600"
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05$' "$env"
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600$' "$env"
    rm -rf "$tmp"
}

# --- Precedence of the gate over an EXTRA_ENV with the same key: podman's
# --env-file resolves a duplicated key with the LAST occurrence in the file, so
# the block written later is the one that wins. The new (validated) channel must
# win over a leftover of the old workaround declared in EXTRA_ENV.

@test "stamp_worker_env: the EXTRA_ENV block is stamped before the gate one" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" "API_TOKEN=tok_xxx" "0.05" "600"
    extra_line="$(grep -n '^# --- EXTRA_ENV' "$env" | head -n1 | cut -d: -f1)"
    gate_line="$(grep -n '^# --- Worker disk gate' "$env" | head -n1 | cut -d: -f1)"
    [ -n "$extra_line" ]
    [ -n "$gate_line" ]
    [ "$gate_line" -gt "$extra_line" ]
    rm -rf "$tmp"
}

@test "stamp_worker_env: the gate value wins over an EXTRA_ENV that declares the same key (--env-file last-wins)" {
    tmp="$(mktemp -d)"
    env="$tmp/.env.1"
    stamp_worker_env "$env" "https://orch.example.com" "tok" "fleet-1" \
        "ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.10" "0.05" "600"
    # The key appears twice in the file; podman's --env-file (and python-dotenv)
    # resolves with the LAST occurrence, not with grep -q.
    last_value="$(grep '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=' "$env" | tail -n1 | cut -d= -f2)"
    [ "$last_value" = "0.05" ]
    rm -rf "$tmp"
}

# Minimal installed tree for the RELEASE tests. The format is the one embedded
# by make-release.sh: three lines version/commit/built, with a trailing
# newline.
make_release_dir() {  # $1=dir  $2=commit
    mkdir -p "$1"
    printf 'version=0.19.0\ncommit=%s\nbuilt=2026-08-18T00:00:00Z\n' "$2" > "$1/RELEASE"
}

@test "read_release_commit: returns the commit of the installed RELEASE" {
    tmp="$(mktemp -d)"; make_release_dir "$tmp" abc1234def
    run read_release_commit "$tmp"
    [ "$status" -eq 0 ]
    [ "$output" = "abc1234def" ]
    rm -rf "$tmp"
}

@test "read_release_commit: fails without RELEASE" {
    tmp="$(mktemp -d)"
    run read_release_commit "$tmp"
    [ "$status" -ne 0 ]
    rm -rf "$tmp"
}

@test "read_release_commit: fails when RELEASE has no commit line" {
    # A truncated (or zero-byte) manifest is not installed code: without this
    # guard the caller silently ends up with an empty string.
    tmp="$(mktemp -d)"
    printf 'version=0.19.0\n' > "$tmp/RELEASE"
    run read_release_commit "$tmp"
    [ "$status" -ne 0 ]
    rm -rf "$tmp"
}

@test "read_release_commit: fails when RELEASE is empty" {
    tmp="$(mktemp -d)"
    : > "$tmp/RELEASE"
    run read_release_commit "$tmp"
    [ "$status" -ne 0 ]
    rm -rf "$tmp"
}

@test "read_release_commit: returns a single line with two commit= entries" {
    # Defense against a malformed manifest: the first one wins, and the output
    # is a single line (the callers compare it with =).
    tmp="$(mktemp -d)"
    printf 'commit=first\ncommit=second\n' > "$tmp/RELEASE"
    run read_release_commit "$tmp"
    [ "$status" -eq 0 ]
    [ "$output" = "first" ]
    rm -rf "$tmp"
}

@test "read_release_commit: under pipefail it still returns the commit (does not reproduce the real SIGPIPE)" {
    # Both production callers run with `set -o pipefail`, so the function's
    # contract has to hold there. To be clear: this test does NOT reproduce the
    # SIGPIPE that motivated removing the pipe to `head` (with a three-line
    # manifest sed finishes before head closes, so the piped version passes
    # too); the reason to remove it is that the race exists and is out of our
    # control, not that this test detects it.
    tmp="$(mktemp -d)"; make_release_dir "$tmp" abc123
    run bash -c 'set -o pipefail; source "$1"; read_release_commit "$2"' _ \
        "${BATS_TEST_DIRNAME}/../lib.sh" "$tmp"
    [ "$status" -eq 0 ]
    [ "$output" = "abc123" ]
    rm -rf "$tmp"
}

# should_bootstrap receives the CONTENT of the RELEASE (the caller reads it once
# with read_release_file); without a file, the text is empty.
release_text_of() {  # $1=dir
    cat "$1/RELEASE" 2>/dev/null || true
}

@test "release_commit_of: returns the commit of an already read RELEASE" {
    run release_commit_of "$(printf 'version=0.19.0\ncommit=abc1234\nbuilt=x\n')"
    [ "$status" -eq 0 ]
    [ "$output" = "abc1234" ]
}

@test "release_commit_of: fails on empty text or text without a commit" {
    run release_commit_of ""
    [ "$status" -ne 0 ]
    run release_commit_of "version=0.19.0"
    [ "$status" -ne 0 ]
}

@test "should_bootstrap: code with RELEASE + image present => skip" {
    tmp="$(mktemp -d)"; make_release_dir "$tmp" abc123
    run should_bootstrap "$(release_text_of "$tmp")" true     # 'true' simulates 'image exists'
    [ "$output" = "skip" ]
    rm -rf "$tmp"
}

@test "should_bootstrap: code present, image absent => bootstrap" {
    tmp="$(mktemp -d)"; make_release_dir "$tmp" abc123
    run should_bootstrap "$(release_text_of "$tmp")" false    # 'false' simulates 'image does NOT exist'
    [ "$output" = "bootstrap" ]
    rm -rf "$tmp"
}

@test "should_bootstrap: without RELEASE => bootstrap even if the image exists" {
    tmp="$(mktemp -d)"                    # code absent: there is no RELEASE
    run should_bootstrap "$(release_text_of "$tmp")" true
    [ "$output" = "bootstrap" ]
    rm -rf "$tmp"
}

@test "should_bootstrap: RELEASE without a commit => bootstrap" {
    # The file exists but declares no identity: not enough to consider the code
    # installed.
    tmp="$(mktemp -d)"
    printf 'version=0.19.0\n' > "$tmp/RELEASE"
    run should_bootstrap "$(release_text_of "$tmp")" true
    [ "$output" = "bootstrap" ]
    rm -rf "$tmp"
}

@test "should_bootstrap: a stray .git no longer counts as installed code" {
    # Regression of the old criterion: the marker used to be the checkout's
    # .git. A half-migrated host, with the old repo still present but no pushed
    # release, must ask for bootstrap.
    tmp="$(mktemp -d)"; mkdir -p "$tmp/.git"
    run should_bootstrap "$(release_text_of "$tmp")" true
    [ "$output" = "bootstrap" ]
    rm -rf "$tmp"
}

@test "env_quote: source round-trip of values with metacharacters" {
    tmp="$(mktemp -d)"
    v='pa$s "double" `tick` $(cmd) and a line
break'
    echo "V=$(env_quote "$v")" > "$tmp/env"
    V=""
    # shellcheck disable=SC1090
    source "$tmp/env"
    [ "$V" = "$v" ]
    rm -rf "$tmp"
}

@test "env_quote: preserves single quotes" {
    tmp="$(mktemp -d)"
    v="it's a 'quoted' value"
    echo "V=$(env_quote "$v")" > "$tmp/env"
    V=""
    # shellcheck disable=SC1090
    source "$tmp/env"
    [ "$V" = "$v" ]
    rm -rf "$tmp"
}

@test "env_quote: empty value => empty single quotes" {
    [ "$(env_quote "")" = "''" ]
}

@test "lib.sh: defines WORKER_IMAGE aligned with the Makefile default" {
    [ "$WORKER_IMAGE" = "localhost/orcaster-worker:local" ]
}

# Flaky command for the retry tests: it fails until attempt OK_AT (counter
# persisted in a file) and passes from then on.
make_flaky() {  # $1=dir  $2=ok_at
    echo 0 > "$1/count"
    cat > "$1/flaky.sh" <<F
#!/usr/bin/env bash
n="\$(cat "$1/count")"
n=\$((n + 1))
echo "\$n" > "$1/count"
[ "\$n" -ge $2 ]
F
    chmod +x "$1/flaky.sh"
}

@test "retry: retries until the command passes" {
    tmp="$(mktemp -d)"
    make_flaky "$tmp" 3
    export RETRY_DELAY=0
    run retry "$tmp/flaky.sh"
    [ "$status" -eq 0 ]
    [ "$(cat "$tmp/count")" -eq 3 ]
    rm -rf "$tmp"
}

@test "retry: exhausts the 5 default attempts and returns the failure" {
    tmp="$(mktemp -d)"
    make_flaky "$tmp" 99   # never passes
    export RETRY_DELAY=0
    run retry "$tmp/flaky.sh"
    [ "$status" -ne 0 ]
    [ "$(cat "$tmp/count")" -eq 5 ]
    rm -rf "$tmp"
}

@test "retry: honors RETRY_MAX" {
    tmp="$(mktemp -d)"
    make_flaky "$tmp" 99
    export RETRY_DELAY=0 RETRY_MAX=2
    run retry "$tmp/flaky.sh"
    [ "$status" -ne 0 ]
    [ "$(cat "$tmp/count")" -eq 2 ]
    rm -rf "$tmp"
}

# --- as_worker -------------------------------------------------
# `sudo` inherits the caller's cwd: if root invokes the suite from /root (700),
# the subcommand fails with "cannot chdir" before running. as_worker must set a
# cwd that any user can traverse.

# `sudo` mock that prints the cwd it was invoked from.
stub_sudo_pwd() {
    local bin="$1"
    mkdir -p "$bin"
    cat >"$bin/sudo" <<'EOF'
#!/usr/bin/env bash
pwd
EOF
    chmod +x "$bin/sudo"
}

@test "as_worker: invokes sudo from a traversable cwd, not the caller's" {
    tmp="$(mktemp -d)"
    stub_sudo_pwd "$tmp/bin"
    mkdir -p "$tmp/priv"
    chmod 700 "$tmp/priv"
    WORKER_USER="$(id -un)"
    WORKER_HOME="$tmp/home"
    PATH="$tmp/bin:$PATH"
    cd "$tmp/priv"
    run as_worker true
    [ "$status" -eq 0 ]
    [ "$output" = "/" ]
    cd "$BATS_TEST_DIRNAME"
    rm -rf "$tmp"
}

@test "as_worker: propagates the command status" {
    tmp="$(mktemp -d)"
    mkdir -p "$tmp/bin"
    cat >"$tmp/bin/sudo" <<'EOF'
#!/usr/bin/env bash
exit 42
EOF
    chmod +x "$tmp/bin/sudo"
    WORKER_USER="$(id -un)"
    WORKER_HOME="$tmp/home"
    PATH="$tmp/bin:$PATH"
    run as_worker false
    [ "$status" -eq 42 ]
    rm -rf "$tmp"
}

# The two `sudo -u orcaster` calls that decide the golden snapshot fast path
# cannot use as_worker (load_env has not run yet at that point), so their cwd
# guard lives in cloud-init-worker.sh itself. Structural check: the real
# failure only reproduces with root + a mode 700 cwd, outside the scope of this
# suite, and without this the regression would come back silently.
@test "cloud-init-worker: sets the cwd before the fast-path sudo calls" {
    script="$BATS_TEST_DIRNAME/../cloud-init-worker.sh"
    run grep -n '^cd /$' "$script"
    [ "$status" -eq 0 ]
    cd_line="${output%%:*}"
    run grep -n 'sudo -u orcaster' "$script"
    [ "$status" -eq 0 ]
    first_sudo="$(printf '%s\n' "$output" | head -n1)"
    first_sudo="${first_sudo%%:*}"
    [ "$cd_line" -lt "$first_sudo" ]
}

# The recipes the operator copies by hand are the path the README recommends,
# and the same text is printed at the end of provisioning.
@test "operator recipes: cd / before the sudo that starts the worker" {
    for f in ../README.md ../05-smoke-test.sh ../run-all-worker.sh; do
        run grep -c 'cd /' "$BATS_TEST_DIRNAME/$f"
        [ "$status" -eq 0 ]
    done
}

# --- Configuration paths outside the checkout ------------------

@test "worker_envfile: empty N => base env in the config dir" {
    run worker_envfile "" /etc/orcaster
    [ "$output" = "/etc/orcaster/env" ]
}

@test "worker_envfile: numeric N => env.N in the config dir" {
    run worker_envfile 2 /etc/orcaster
    [ "$output" = "/etc/orcaster/env.2" ]
}
