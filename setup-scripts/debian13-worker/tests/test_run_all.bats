#!/usr/bin/env bats
# Integration of run-all-worker.sh over a copy of the suite in a temp dir, with
# the numbered scripts replaced by stubs that record their name. It never
# touches apt, podman or the system: it only verifies the chain it orchestrates
# and the header that declares which code is being provisioned.
#
# bats note: a `[[ ... ]]` or a `! cmd` that is not the LAST statement of a @test
# does NOT fail the test; intermediate assertions go through the helper.

assert_contains() {
    [[ "$1" == *"$2"* ]] || { echo "missing '$2' in the output:"; echo "$1"; return 1; }
}

# The fake `id -u` returns 0, so lib.sh's origin guard acts: stub of `ls -lnd`
# that presents the tree as root-owned.
load ../../debian13/tests/source_guard_stub

setup() {
    # Physical path with the repo's shape (src/setup-scripts/debian13-worker): the
    # origin guard requires the invoked path to be the physical one (on macOS
    # mktemp falls under /var, a link to /private/var) and the tree
    # (REPO_ROOT = two levels above the suite) not to contain INSTALL_DIR.
    T="$(cd "$(mktemp -d)" && pwd -P)"
    SUITE="$T/src/setup-scripts/debian13-worker"
    mkdir -p "$SUITE" "$T/bin" "$T/etc" "$T/install"
    cp "$BATS_TEST_DIRNAME/../lib.sh" "$BATS_TEST_DIRNAME/../run-all-worker.sh" "$SUITE/"

    CALLS="$T/calls.log"; export CALLS
    touch "$CALLS"
    # 01-fetch-code.sh no longer exists in the suite, but the stub stays on
    # purpose: if run-all-worker invoked it again, the log would show it.
    for s in 00-preflight.sh 01-fetch-code.sh 02-server-pubkey.sh \
             03-worker-env.sh 04-build.sh 05-smoke-test.sh; do
        printf '#!/usr/bin/env bash\necho "%s" >> "$CALLS"\n' "$s" > "$SUITE/$s"
        chmod +x "$SUITE/$s"
    done

    export CONFIG_DIR="$T/etc"
    INSTALL_DIR_FIXTURE="$T/install"
    {
        echo 'ORCH_BASE_URL="https://orch.example.com"'
        echo "INSTALL_DIR=\"$T/install\""
    } > "$T/etc/setup.env"

    # need_root: without this stub the script re-executes itself with sudo and the
    # test hangs asking for a password.
    cat > "$T/bin/id" <<'M'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ] && [ -z "${2:-}" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
M
    chmod +x "$T/bin/id"
    write_ls_stub "$T/bin"
    export PATH="$T/bin:$PATH"
}

teardown() { rm -rf "$T"; }

make_release_file() {
    printf 'version=0.19.0\ncommit=abc123def456\nbuilt=2026-08-18T00:00:00Z\n' \
        > "$INSTALL_DIR_FIXTURE/RELEASE"
}

@test "run-all-worker: runs the chain 00 02 03 04 05 and never 01" {
    make_release_file
    run bash "$SUITE/run-all-worker.sh"
    [ "$status" -eq 0 ]
    [ "$(tr '\n' ' ' < "$CALLS")" = "00-preflight.sh 02-server-pubkey.sh 03-worker-env.sh 04-build.sh 05-smoke-test.sh " ]
}

@test "run-all-worker: --from 03 resumes without re-running the earlier ones" {
    make_release_file
    run bash "$SUITE/run-all-worker.sh" --from 03
    [ "$status" -eq 0 ]
    [ "$(tr '\n' ' ' < "$CALLS")" = "03-worker-env.sh 04-build.sh 05-smoke-test.sh " ]
}

@test "run-all-worker: the header declares the installed release" {
    # Replaces the `repo ref:` line that left with git: without this the
    # provisioning log does not say which code just started.
    make_release_file
    run bash "$SUITE/run-all-worker.sh"
    [ "$status" -eq 0 ]
    assert_contains "$output" "version=0.19.0"
    assert_contains "$output" "commit=abc123def456"
}

@test "run-all-worker: without RELEASE it warns that it cannot declare the code" {
    run bash "$SUITE/run-all-worker.sh"
    [ "$status" -eq 0 ]
    assert_contains "$output" "RELEASE"
    [ "$(tr '\n' ' ' < "$CALLS")" = "00-preflight.sh 02-server-pubkey.sh 03-worker-env.sh 04-build.sh 05-smoke-test.sh " ]
}

@test "run-all-worker: --from without a value fails with the cause, not silently" {
    # The earlier `${2:-0}` was dead code: the while's closing `shift` failed
    # with $#=0 and `set -e` killed the script with exit 1 and no output.
    run bash "$SUITE/run-all-worker.sh" --from
    [ "$status" -ne 0 ]
    assert_contains "$output" "--from needs a value"
    # And no numbered step ran.
    [ ! -s "$CALLS" ]
}

@test "run-all-worker: with the worker user created it reads RELEASE as that user" {
    # INSTALL_DIR belongs to the worker: root does not read it directly.
    make_release_file
    cat > "$T/bin/id" <<'M'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ] && [ -z "${2:-}" ]; then echo 0; exit 0; fi
if [ "${1:-}" = "-u" ] && [ "${2:-}" = "orcaster" ]; then echo 1001; exit 0; fi
exec /usr/bin/id "$@"
M
    cat > "$T/bin/sudo" <<'M'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
shift 2; exec "$@"
M
    chmod +x "$T/bin/id" "$T/bin/sudo"
    run bash "$SUITE/run-all-worker.sh"
    [ "$status" -eq 0 ]
    grep -qx "sudo -u orcaster cat -- $T/install/RELEASE" "$CALLS"
    assert_contains "$output" "commit=abc123def456"
}

@test "run-all-worker: without the worker user it does not read a RELEASE that is not root-owned" {
    # Bootstrap: the user does not exist yet and there is nobody to read it as; a
    # foreign RELEASE is not read as root.
    make_release_file
    echo "$T/install/RELEASE" > "$T/foreign"
    run env SOURCE_GUARD_FIXTURE="$T" SOURCE_GUARD_FOREIGN="$T/foreign" \
        bash "$SUITE/run-all-worker.sh"
    [ "$status" -eq 0 ]
    assert_contains "$output" "No readable $T/install/RELEASE"
    [[ "$output" != *"commit=abc123def456"* ]]
}

@test "run-all-worker: without the worker user it does not follow a RELEASE that is a link" {
    echo "commit=secret" > "$T/secret"
    ln -s "$T/secret" "$T/install/RELEASE"
    run bash "$SUITE/run-all-worker.sh"
    [ "$status" -eq 0 ]
    assert_contains "$output" "readable"
    [[ "$output" != *"secret"* ]]
}
