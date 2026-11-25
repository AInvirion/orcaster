#!/usr/bin/env bats
# Tests for push-release.sh: it builds the tarball via make-release.sh (real,
# not mocked: it is fast and test_make_release.bats already covers it) and
# talks to the fleet via scp/ssh, which ARE mocked here through PATH (same
# idiom as test_run_fleet.bats for make/systemctl/podman). It never touches a
# real host.
#
# Note on the bats pitfall in this repo: a `[[ ... ]]` that is not the last
# statement of a @test does not make the test fail. That is why content
# assertions use assert_contains/refute_contains (their `[[ ]]` is the last
# statement OF THE FUNCTION, not of the @test) or a single-bracket `[ ]`,
# never a bare `[[ ]]` in the middle of a @test.

load ../../../setup-scripts/debian13/tests/stub_guard

assert_contains() {
    [[ "$1" == *"$2"* ]] || { echo "missing: $2"; echo "in: $1"; return 1; }
}

refute_contains() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected: $2"; echo "in: $1"; return 1; }
}

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../push-release.sh"
    T="$(mktemp -d)"
    CALLS="$T/calls.log"
    export CALLS

    # Where make-release.sh leaves the tarball, via the script's real --out
    # (not an environment seam): this keeps the repo's real dist/ clean on
    # every run of this suite.
    OUT="$T/dist"

    # Test seam: the fleet inventory never comes from the operator's real
    # deploy.local.env (not versioned, and it may not even exist in this
    # checkout).
    DEPLOY_ENV_FILE_OVERRIDE="$T/deploy.local.env"
    export DEPLOY_ENV_FILE_OVERRIDE

    # scp/ssh mocks: they only record the invocation, never touch the network.
    FAKE_BIN="$T/fakebin"
    mkdir -p "$FAKE_BIN"
    cat > "$FAKE_BIN/scp" <<'EOS'
#!/usr/bin/env bash
echo "scp $*" >> "$CALLS"
EOS
    cat > "$FAKE_BIN/ssh" <<'EOS'
#!/usr/bin/env bash
echo "ssh $*" >> "$CALLS"
case "$*" in
    *mktemp*) printf '%s\n' "$FAKE_STAGE" ;;
esac
EOS
    chmod +x "$FAKE_BIN/scp" "$FAKE_BIN/ssh"
    # Path returned by the host's simulated `mktemp -d`.
    FAKE_STAGE="/tmp/push-release.AbCdEfGhIj"
    export FAKE_STAGE
    export PATH="$FAKE_BIN:$PATH"
    # Fails here, before any test, if PATH reaches the real ssh/scp.
    assert_stubbed "$FAKE_BIN" scp ssh
}

teardown() {
    if [ -n "${T:-}" ]; then rm -rf "$T"; fi
}

@test "push-release: without --hosts and without deploy.local.env fails clearly" {
    run bash "$SCRIPT" --out "$OUT" --dry-run
    [ "$status" -ne 0 ]
    assert_contains "$output" "deploy.local.env"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: deploy.local.env without WORKER_HOSTS fails clearly" {
    printf 'WORKER_COUNT_host_x=1\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT" --dry-run
    [ "$status" -ne 0 ]
    assert_contains "$output" "WORKER_HOSTS"
}

@test "push-release: deploy.local.env with invalid syntax aborts naming the file, without building anything" {
    # Unclosed quote: such a file is indistinguishable from a well-formed one
    # that simply does not declare the key if nobody checks that it can be
    # sourced. Without that check, this case would fall into the same silent
    # default as an empty WORKER_HOSTS (lying about the real cause).
    printf 'WORKER_HOSTS="host-a\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT" --dry-run
    [ "$status" -ne 0 ]
    assert_contains "$output" "$DEPLOY_ENV_FILE_OVERRIDE"
    assert_contains "$output" "could not be sourced cleanly"
    # make-release.sh was never invoked: no tarball and no scp/ssh calls.
    # $OUT is not even created; `find` on a nonexistent directory prints its
    # own error to stderr, so it is discarded separately from the `output`
    # that bats combines.
    run bash -c 'find "$1" -name "orcaster-*.tgz" 2>/dev/null' _ "$OUT"
    [ -z "$output" ]
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: deploy.local.env with invalid syntax also aborts with --hosts" {
    # The sourceability check is unconditional (the inventory is read
    # whenever it exists, also with --hosts): a broken file should not go
    # unnoticed just because the host list came from the command line.
    printf 'WORKER_HOSTS="host-a\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT" --hosts "host-cli" --dry-run
    [ "$status" -ne 0 ]
    assert_contains "$output" "could not be sourced cleanly"
}

@test "push-release: --dry-run runs no scp or ssh" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-de-prueba" --dry-run
    [ "$status" -eq 0 ]
    assert_contains "$output" "[dry-run]"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: --dry-run captures the artifact path from make-release and uses it in the message" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-de-prueba" --dry-run
    [ "$status" -eq 0 ]
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    [ -n "$tgz" ]
    [ -f "$tgz" ]
    base="$(basename "$tgz")"
    assert_contains "$output" "$base"
}

@test "push-release: builds the ssh line with sudo bash and the --workers from WORKER_COUNT_<host>" {
    printf 'WORKER_HOSTS="host-a host-b"\nWORKER_COUNT_host_a=3\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    grep -q 'ssh host-a .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/.*--workers 3' "$CALLS"
    # host-b has no WORKER_COUNT_host_b: --workers is omitted entirely and
    # install-release.sh resolves the count from the host's setup.env. Never a
    # silent "--workers 1".
    grep -q 'ssh host-b .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/[^ ]*$' "$CALLS"
    run grep -q 'host-b.*--workers' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: missing WORKER_COUNT_<host> omits --workers, the host resolves it" {
    printf 'WORKER_HOSTS="host-a"\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    assert_contains "$output" "resolved by the host"
    grep -q 'ssh host-a .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/[^ ]*$' "$CALLS"
    run grep -q -- '--workers' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: --dry-run also omits --workers when the key is missing and says so in the message" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a" --dry-run
    [ "$status" -eq 0 ]
    assert_contains "$output" "resolved by the host"
    refute_contains "$output" "--workers"
}

@test "push-release: --hosts still reads WORKER_COUNT_<host> from deploy.local.env" {
    # --hosts replaces the host LIST, not the whole inventory. Sourcing
    # deploy.local.env only in the branch without --hosts left the form
    # `push-release.sh --hosts "$HOST"` with the default of 1 worker: on a
    # 3-worker host, install-release.sh recreated only worker 1 and the other
    # two stayed on the bind mount of the old code.
    printf 'WORKER_HOSTS="host-a host-b"\nWORKER_COUNT_host_a=3\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -eq 0 ]
    grep -q 'ssh host-a .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/.*--workers 3' "$CALLS"
    run grep -q 'host-b' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: --hosts without deploy.local.env still works (the host resolves --workers)" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -eq 0 ]
    grep -q 'ssh host-a .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/[^ ]*$' "$CALLS"
    run grep -q -- '--workers' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: --hosts overrides the deploy.local.env inventory" {
    printf 'WORKER_HOSTS="host-from-the-file"\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT" --hosts "host-de-prueba" --dry-run
    [ "$status" -eq 0 ]
    assert_contains "$output" "host-de-prueba"
    refute_contains "$output" "host-from-the-file"
}

@test "push-release: without --hosts takes the full deploy.local.env inventory" {
    printf 'WORKER_HOSTS="host-a host-b"\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    [ "$(grep -c '^ssh host-a ' "$CALLS")" -eq 3 ]
    [ "$(grep -c '^ssh host-b ' "$CALLS")" -eq 3 ]
    [ "$(grep -c '^scp .*host-a:/tmp/push-release.AbCdEfGhIj/' "$CALLS")" -eq 1 ]
    [ "$(grep -c '^scp .*host-b:/tmp/push-release.AbCdEfGhIj/' "$CALLS")" -eq 1 ]
}

@test "push-release: scp copies the tarball, the sha256 and install-release.sh" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -eq 0 ]
    line="$(grep '^scp ' "$CALLS")"
    assert_contains "$line" ".tgz"
    assert_contains "$line" ".tgz.sha256"
    assert_contains "$line" "install-release.sh"
    assert_contains "$line" "host-a:/tmp/push-release.AbCdEfGhIj/"
}

@test "push-release: a failing host aborts before continuing with the next one (set -e, no rollback)" {
    # This test documents the real behaviour: it is fail-fast through set -e,
    # not an explicit "continue" or "stop" decision written by hand.
    cat > "$FAKE_BIN/ssh" <<'EOS'
#!/usr/bin/env bash
echo "ssh $*" >> "$CALLS"
case "$*" in
    *mktemp*) printf '%s\n' "$FAKE_STAGE" ;;
    *"sudo bash"*) case "$1" in host-a) exit 1 ;; esac ;;
esac
EOS
    chmod +x "$FAKE_BIN/ssh"
    assert_stubbed "$FAKE_BIN" ssh
    printf 'WORKER_HOSTS="host-a host-b"\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    grep -q '^ssh host-a ' "$CALLS"
    run grep -q '^ssh host-b ' "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q '^scp .*host-b:/tmp/push-release.AbCdEfGhIj/' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: --out redirects where make-release leaves the tarball (real interface, not an environment variable)" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a" --dry-run
    [ "$status" -eq 0 ]
    run find "$OUT" -name 'orcaster-*.tgz'
    [ "$status" -eq 0 ]
    [ -n "$output" ]
}

@test "push-release: the inventory does not override REF/OUT/HOSTS/DRY from the command line" {
    # deploy.local.env is an operator file, outside the control of
    # push-release.sh: sourcing it directly in the script's shell would
    # silently override what the operator put on the command line if it
    # declared variables with these names. Each key below collides on purpose
    # with an internal variable of the script.
    bogus_out="$T/nunca-deberia-crearse"
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<EOS
REF="no-existe-como-ref-de-git"
OUT="$bogus_out"
HOSTS="inventory-host-must-never-appear"
DRY=1
WORKER_HOSTS="host-b"
EOS
    run bash "$SCRIPT" --ref HEAD --out "$OUT" --hosts "host-cli"
    [ "$status" -eq 0 ]
    # REF: if the inventory had overridden REF, make-release.sh would receive
    # a nonexistent git ref and fail (non-zero status).
    assert_contains "$output" "Building the package from HEAD"
    # OUT: the tarball lives in the test's real $OUT; the inventory's path is
    # not even touched.
    tgz="$(find "$OUT" -name 'orcaster-*.tgz' | head -n1)"
    [ -n "$tgz" ]
    [ ! -e "$bogus_out" ]
    # HOSTS: uses host-cli (the one from --hosts), never the inventory's HOSTS
    # nor its WORKER_HOSTS.
    grep -q '^ssh host-cli ' "$CALLS"
    run grep -q 'inventory-host-must-never-appear' "$CALLS"
    [ "$status" -ne 0 ]
    run grep -q '^ssh host-b ' "$CALLS"
    [ "$status" -ne 0 ]
    # DRY: if the inventory had overridden DRY=1, there would be no real
    # scp/ssh recorded in $CALLS.
    [ -s "$CALLS" ]
}

@test "push-release: a WORKER_COUNT_ that is not an integer aborts before touching the fleet" {
    # The inventory value ends up interpolated in the string that runs on the
    # host: with `1; echo x` the remote shell runs a compound command and
    # `ssh` returns the status of the LAST command, so a FAILED install
    # reached the "OK $host". It is validated on the sender side.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
WORKER_COUNT_host_a="1; echo INJECTION"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "WORKER_COUNT_host_a"
    assert_contains "$output" "integer"
    # Nothing in the fleet was touched: no scp and no ssh.
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: WORKER_COUNT_ at zero aborts (no deploy that recreates nothing)" {
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
WORKER_COUNT_host_a="0"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "WORKER_COUNT_host_a"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: no command touches a predictable /tmp name for the installer or the tarball" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -eq 0 ]
    run grep -F '/tmp/install-release.sh' "$CALLS"
    [ "$status" -ne 0 ]
    run grep -E "(/tmp/orcaster-|host-a:/tmp/ )" "$CALLS"
    [ "$status" -ne 0 ]
    # The staging directory is created with mktemp -d under umask 077 and the random template.
    grep -q "^ssh host-a umask 077; mktemp -d .*push-release.XXXXXXXXXX" "$CALLS"
}

@test "push-release: verifies the sha256 on the host before sudo bash, with a remote cleanup trap" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -eq 0 ]
    line="$(grep '^ssh host-a d=' "$CALLS")"
    assert_contains "$line" "trap 'rm -rf -- \"\$d\"' EXIT"
    assert_contains "$line" "cd '$FAKE_STAGE' && sha256sum -c "
    # Order: sha256sum -c comes before sudo bash.
    pre="${line%%sudo bash*}"
    assert_contains "$pre" "sha256sum -c"
    # Owner and mode of the staging directory are checked on the host before the scp.
    grep -q "^ssh host-a \[ -d '$FAKE_STAGE' \].*-O '$FAKE_STAGE'.*= 700" "$CALLS"
    [ "$(grep -n 'stat -c' "$CALLS" | cut -d: -f1)" -lt "$(grep -n '^scp ' "$CALLS" | cut -d: -f1)" ]
}

@test "push-release: success leaves no local fallback rm -rf (the remote trap cleans up)" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -eq 0 ]
    run grep -q '^ssh host-a rm -rf' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: if the install fails, deletes the host staging directory and aborts" {
    cat > "$FAKE_BIN/ssh" <<'EOS'
#!/usr/bin/env bash
echo "ssh $*" >> "$CALLS"
case "$*" in
    *mktemp*) printf '%s\n' "$FAKE_STAGE" ;;
    *"sudo bash"*) exit 1 ;;
esac
EOS
    chmod +x "$FAKE_BIN/ssh"
    assert_stubbed "$FAKE_BIN" ssh
    printf 'WORKER_HOSTS="host-a host-b"\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    grep -q "^ssh host-a rm -rf -- '$FAKE_STAGE'" "$CALLS"
    run grep -q '^ssh host-b ' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: if the scp fails, deletes the host staging directory and does not install" {
    cat > "$FAKE_BIN/scp" <<'EOS'
#!/usr/bin/env bash
echo "scp $*" >> "$CALLS"
exit 1
EOS
    chmod +x "$FAKE_BIN/scp"
    assert_stubbed "$FAKE_BIN" scp
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -ne 0 ]
    grep -q "^ssh host-a rm -rf -- '$FAKE_STAGE'" "$CALLS"
    run grep -q 'sudo bash' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: if the fallback deletion fails it warns with the manual command, never silently" {
    cat > "$FAKE_BIN/ssh" <<'EOS'
#!/usr/bin/env bash
echo "ssh $*" >> "$CALLS"
case "$*" in
    *mktemp*) printf '%s\n' "$FAKE_STAGE" ;;
    *"sudo bash"*) exit 1 ;;
    *"rm -rf -- '/"*) exit 255 ;;
esac
EOS
    chmod +x "$FAKE_BIN/ssh"
    assert_stubbed "$FAKE_BIN" ssh
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -ne 0 ]
    assert_contains "$output" "WARNING: could not delete the staging directory $FAKE_STAGE on host-a"
}

@test "push-release: an unexpected staging path from the host aborts without scp or rm" {
    for bad in "/tmp/otro-nombre" "relativa/push-release.AbCdEfGhIj" "/tmp/../etc/push-release.AbCdEfGhIj" "/tmp/push-release.AbC; rm -rf x"; do
        : > "$CALLS"
        export FAKE_STAGE="$bad"
        run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
        [ "$status" -ne 0 ]
        assert_contains "$output" "unexpected staging path"
        run grep -E '^scp |rm -rf' "$CALLS"
        [ "$status" -ne 0 ]
    done
}

@test "push-release: a staging directory that is not 0700 of the ssh user aborts without scp" {
    cat > "$FAKE_BIN/ssh" <<'EOS'
#!/usr/bin/env bash
echo "ssh $*" >> "$CALLS"
case "$*" in
    *mktemp*) printf '%s\n' "$FAKE_STAGE" ;;
    *"stat -c"*) exit 1 ;;
esac
EOS
    chmod +x "$FAKE_BIN/ssh"
    assert_stubbed "$FAKE_BIN" ssh
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -ne 0 ]
    assert_contains "$output" "is not a 0700 directory of the ssh user"
    run grep -q '^scp ' "$CALLS"
    [ "$status" -ne 0 ]
    grep -q "^ssh host-a rm -rf -- '$FAKE_STAGE'" "$CALLS"
}

@test "push-release: --dry-run shows the real staging pattern and the exact sudo bash" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a" --dry-run
    [ "$status" -eq 0 ]
    assert_contains "$output" "mktemp -d \${TMPDIR:-/tmp}/push-release.XXXXXXXXXX"
    assert_contains "$output" "sha256sum -c "
    assert_contains "$output" "rm -rf -- \${TMPDIR:-/tmp}/push-release.XXXXXXXXXX"
    refute_contains "$output" "/tmp/install-release.sh"
}

@test "push-release: --dry-run shows the exact string that would run as root" {
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
WORKER_COUNT_host_a=2
EOS
    run bash "$SCRIPT" --out "$OUT" --dry-run
    [ "$status" -eq 0 ]
    assert_contains "$output" "sudo bash \${TMPDIR:-/tmp}/push-release.XXXXXXXXXX/install-release.sh --tarball \${TMPDIR:-/tmp}/push-release.XXXXXXXXXX/"
    assert_contains "$output" "--workers 2"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: a declared CONFIG_DIR_<host> adds --config-dir to the remote line" {
    # The host's config directory (setup.env, the env.N files, the pubkey)
    # does not arrive through the environment: the invocation is `sudo bash`
    # (env_reset does not propagate it) and it travels over ssh (which does not
    # accept CONFIG_DIR: it is not in AcceptEnv). The only channel is the
    # --config-dir option, and the inventory is what declares it per host.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a=/srv/orcaster
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    grep -q 'ssh host-a .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/.*--config-dir /srv/orcaster' "$CALLS"
    # The host's log line says so, as it does for the workers.
    assert_contains "$output" "config-dir: /srv/orcaster (from deploy.local.env)"
}

@test "push-release: CONFIG_DIR_<host> coexists with WORKER_COUNT_<host>" {
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
WORKER_COUNT_host_a=3
CONFIG_DIR_host_a=/srv/orcaster
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    grep -q 'ssh host-a .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/[^ ]* --workers 3 --config-dir /srv/orcaster$' "$CALLS"
}

@test "push-release: without CONFIG_DIR_<host> the remote line carries no --config-dir" {
    # Default case: the host falls back to the /etc/orcaster default of
    # install-release.sh and the remote string is the same as before this key
    # existed.
    printf 'WORKER_HOSTS="host-a"\n' > "$DEPLOY_ENV_FILE_OVERRIDE"
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    run grep -q -- '--config-dir' "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: CONFIG_DIR_ with a relative path aborts before touching the fleet" {
    # A relative path would be resolved on the host side against the cwd of
    # `sudo`, not against anything the operator chose.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a=srv/orcaster
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "CONFIG_DIR_host_a"
    assert_contains "$output" "absolute path"
    assert_contains "$output" "$DEPLOY_ENV_FILE_OVERRIDE"
    # Nothing in the fleet was touched: neither scp nor ssh.
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: CONFIG_DIR_ with a semicolon aborts before touching the fleet" {
    # The value ends up interpolated into the string that runs on the host:
    # with `/x; echo y` the remote shell runs a compound and `ssh` returns the
    # status of the LAST command, so a FAILED install would reach the
    # "OK $host". It is validated on the sender, because on the host side the
    # string has already been split.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a="/srv/orcaster; echo INJECTION"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "CONFIG_DIR_host_a"
    assert_contains "$output" "$DEPLOY_ENV_FILE_OVERRIDE"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: CONFIG_DIR_ with a space aborts before touching the fleet" {
    # A space needs no metacharacters to break things: it splits the value into
    # two words and the second piece becomes a stray argument of
    # install-release.sh, which aborts with "unknown argument".
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a="/srv/task orch"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "CONFIG_DIR_host_a"
    assert_contains "$output" "only allows letters"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: CONFIG_DIR_ at the root aborts before touching the fleet" {
    # install-release.sh rejects it at the host gate. Without the equivalent
    # guard on the sender, with two or more hosts the first would end up fully
    # deployed before the second aborts remotely.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a="/"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "CONFIG_DIR_host_a"
    assert_contains "$output" "cannot be the root"
    assert_contains "$output" "$DEPLOY_ENV_FILE_OVERRIDE"
    # Nothing in the fleet was touched: neither scp nor ssh.
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: CONFIG_DIR_ with two slashes also aborts as root" {
    # '//' is deliberately not normalized on the sender; the verdict is
    # measured on the trimmed value, which collapses to '/'.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a="//"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "CONFIG_DIR_host_a"
    assert_contains "$output" "cannot be the root"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: CONFIG_DIR_ with .. aborts before touching the fleet" {
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a="/srv/../etc/orcaster"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "CONFIG_DIR_host_a"
    assert_contains "$output" "does not allow '..' as a path component"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: CONFIG_DIR_ with two dots in the name is still legal" {
    # 'a..b' is not a '..' component: the guard measures components, not
    # substrings.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a=/srv/a..b/orcaster
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    grep -q 'ssh host-a .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/.*--config-dir /srv/a..b/orcaster' "$CALLS"
}

@test "push-release: --dry-run shows --config-dir in the preview" {
    # The dry-run exists to audit what will run as root on the fleet: a preview
    # that omits --config-dir is useless for that.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a=/srv/orcaster
EOS
    run bash "$SCRIPT" --out "$OUT" --dry-run
    [ "$status" -eq 0 ]
    assert_contains "$output" "sudo bash \${TMPDIR:-/tmp}/push-release.XXXXXXXXXX/install-release.sh --tarball \${TMPDIR:-/tmp}/push-release.XXXXXXXXXX/"
    assert_contains "$output" "--config-dir /srv/orcaster"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: a host with hyphens resolves CONFIG_DIR_ with underscores" {
    # Shell variable names do not allow hyphens: same conversion as
    # WORKER_COUNT_<host>.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="worker-1"
CONFIG_DIR_worker_1=/srv/orcaster
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    grep -q 'ssh worker-1 .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/.*--config-dir /srv/orcaster' "$CALLS"
}

@test "push-release: an FQDN host resolves CONFIG_DIR_ translating the dots too" {
    # Shell variable names do not allow dots, and a host can be an FQDN.
    # Translating only the hyphens, the built key was
    # `CONFIG_DIR_worker.example.com`: not a valid identifier, so the indirect
    # expansion in read_inventory_var returns empty with status 0 and the
    # operator's override was silently lost. The deploy fell back to the
    # /etc/orcaster default and only aborted at the host's pre-swap gate, which
    # is exactly the failure CONFIG_DIR_<host> exists to close.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="worker.example.com"
CONFIG_DIR_worker_example_com=/srv/orcaster
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    grep -q 'ssh worker.example.com .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/.*--config-dir /srv/orcaster' "$CALLS"
    assert_contains "$output" "config-dir: /srv/orcaster (from deploy.local.env)"
}

@test "push-release: an FQDN host resolves WORKER_COUNT_ translating the dots too" {
    # The sibling key had the same defect and is fixed the same way: on an FQDN
    # host with 3 workers, the inventory's WORKER_COUNT was read as empty,
    # --workers was omitted and the host resolved the count, with nobody saying
    # that the declared override was being ignored.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="worker.example.com"
WORKER_COUNT_worker_example_com=3
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -eq 0 ]
    grep -q 'ssh worker.example.com .*sudo bash /tmp/push-release.AbCdEfGhIj/install-release.sh --tarball /tmp/push-release.AbCdEfGhIj/.*--workers 3' "$CALLS"
    assert_contains "$output" "workers: 3, from deploy.local.env"
}

@test "push-release: a host that cannot be translated to an inventory key aborts naming the key" {
    # `user@host` translated is still not a valid shell variable name, and an
    # invalid key is ALWAYS read as empty with status 0: letting it through
    # means losing the override silently again. This is required whether or not
    # there is an inventory (here there is no file, the list comes from
    # --hosts): the behaviour cannot depend on the presence of the file.
    run bash "$SCRIPT" --out "$OUT" --hosts "root@1.2.3.4"
    [ "$status" -ne 0 ]
    assert_contains "$output" "root@1.2.3.4"
    assert_contains "$output" "CONFIG_DIR_root@1_2_3_4"
    assert_contains "$output" "~/.ssh/config"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: a CONFIG_DIR_ declared empty aborts instead of silently falling back to the default" {
    # Declared empty and absent were read as the same empty string: a
    # `CONFIG_DIR_host_a=` fell back to the /etc/orcaster default without a
    # word in the log. Absent is a legitimate choice (the default); empty is a
    # half-written inventory and has to be named.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a"
CONFIG_DIR_host_a=
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "CONFIG_DIR_host_a"
    assert_contains "$output" "empty"
    assert_contains "$output" "$DEPLOY_ENV_FILE_OVERRIDE"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: with two hosts, a malformed CONFIG_DIR_ on the second aborts without touching the first" {
    # The validation used to live inside the per-host loop: host-a was already
    # deployed (release installed and workers recreated) when the script
    # aborted on host-b's value. The empty $CALLS is the proof that no host of
    # the fleet was touched, not only the one with the error.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a host-b"
CONFIG_DIR_host_b="/srv/orcaster; echo INJECTION"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "CONFIG_DIR_host_b"
    # Neither scp nor ssh: the mock was never invoked, not even for host-a.
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
    # The pre-flight runs before the package is built, so there is no tarball
    # either.
    run bash -c 'find "$1" -name "orcaster-*.tgz" 2>/dev/null' _ "$OUT"
    [ -z "$output" ]
}

@test "push-release: with two hosts, a malformed WORKER_COUNT_ on the second aborts without touching the first" {
    # Same lateness in the sibling key: it is hoisted the same way.
    cat > "$DEPLOY_ENV_FILE_OVERRIDE" <<'EOS'
WORKER_HOSTS="host-a host-b"
WORKER_COUNT_host_b="1; echo INJECTION"
EOS
    run bash "$SCRIPT" --out "$OUT"
    [ "$status" -ne 0 ]
    assert_contains "$output" "WORKER_COUNT_host_b"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
    run bash -c 'find "$1" -name "orcaster-*.tgz" 2>/dev/null' _ "$OUT"
    [ -z "$output" ]
}

# --- --keep-staging --------------------------------------------------------
# The fake ssh in these cases RUNS locally what would run on the host
# (install session with its remote trap and the fallback rm -rf) on a real
# staging directory inside $T, with `sudo` and `sha256sum` replaced by
# functions in that bash. This measures whether the directory really survives.
keep_staging_ssh() {
    export FAKE_STAGE="$T/push-release.AbCdEfGhIj"
    cat > "$FAKE_BIN/ssh" <<'EOS'
#!/usr/bin/env bash
echo "ssh $*" >> "$CALLS"
case "$2" in
    *mktemp*) mkdir -p "$FAKE_STAGE"; printf '%s\n' "$FAKE_STAGE" ;;
    *"sudo bash"*)
        bash -c 'sha256sum() { :; }; sudo() { [ "${FAKE_INSTALL_FAIL:-0}" != 1 ]; }; '"$2" ;;
    "rm -rf -- '"*) bash -c "$2" ;;
esac
EOS
    chmod +x "$FAKE_BIN/ssh"
    assert_stubbed "$FAKE_BIN" ssh
}

@test "push-release: --keep-staging with a successful deploy deletes the staging and does not warn" {
    keep_staging_ssh
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a" --keep-staging
    [ "$status" -eq 0 ]
    [ ! -e "$FAKE_STAGE" ]
    refute_contains "$output" "--keep-staging:"
    run grep -q "^ssh host-a rm -rf" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: --keep-staging with a failed deploy keeps the staging and prints host, path and the delete command" {
    keep_staging_ssh
    FAKE_INSTALL_FAIL=1 run bash "$SCRIPT" --out "$OUT" --hosts "host-a" --keep-staging
    [ "$status" -ne 0 ]
    [ -d "$FAKE_STAGE" ]
    assert_contains "$output" "host-a:$FAKE_STAGE"
    assert_contains "$output" "ssh host-a \"rm -rf -- '$FAKE_STAGE'\""
    # The local fallback deletion does not run either.
    run grep -q "^ssh host-a rm -rf" "$CALLS"
    [ "$status" -ne 0 ]
}

@test "push-release: --keep-staging with a failed scp keeps the staging" {
    keep_staging_ssh
    printf '#!/usr/bin/env bash\necho "scp $*" >> "$CALLS"\nexit 1\n' > "$FAKE_BIN/scp"
    assert_stubbed "$FAKE_BIN" scp
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a" --keep-staging
    [ "$status" -ne 0 ]
    [ -d "$FAKE_STAGE" ]
    assert_contains "$output" "ssh host-a \"rm -rf -- '$FAKE_STAGE'\""
}

@test "push-release: without --keep-staging a failed deploy deletes the staging and does not print the keep warning" {
    keep_staging_ssh
    FAKE_INSTALL_FAIL=1 run bash "$SCRIPT" --out "$OUT" --hosts "host-a"
    [ "$status" -ne 0 ]
    [ ! -e "$FAKE_STAGE" ]
    refute_contains "$output" "--keep-staging:"
}

@test "push-release: --help documents --keep-staging" {
    run bash "$SCRIPT" --help
    [ "$status" -eq 0 ]
    assert_contains "$output" "--keep-staging"
}

@test "push-release: --dry-run with --keep-staging reflects it in the preview" {
    run bash "$SCRIPT" --out "$OUT" --hosts "host-a" --dry-run --keep-staging
    [ "$status" -eq 0 ]
    assert_contains "$output" "--keep-staging"
    run test -e "$CALLS"
    [ "$status" -ne 0 ]
}

@test "docs: no doc stages install-release.sh in a fixed-name /tmp" {
    root="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # Walk only what exists: docs/ may be incomplete in a checkout.
    files=()
    while IFS= read -r f; do files+=("$f"); done < <(
        {
            [ -d "$root/docs" ] && find "$root/docs" -name '*.md' -not -path '*/superpowers/*'
            find "$root/setup-scripts" -name README.md
            find "$root/ops" -name README.md
        } | sort)
    [ "${#files[@]}" -gt 0 ]
    run grep -n '/tmp/install-release\.sh' "${files[@]}"
    [ "$status" -eq 1 ]
}
