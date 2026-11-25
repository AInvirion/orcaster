#!/usr/bin/env bats
# Integration of 03-worker-env.sh with id/install/chown mocked (no root).
# Verifies that the BASE .env never receives the multi-use token from setup.env
# (the fleet flow stamps it only in the CONFIG_DIR env.N files; the manual flow
# has the operator place it), and that a rerun preserves previous credentials.

# The fake `id -u` returns 0, so lib.sh's origin guard acts: stub of `ls -lnd`
# that presents the tree as root-owned.
load ../../debian13/tests/source_guard_stub

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../03-worker-env.sh"
    T="$(mktemp -d)"
    export INSTALL_DIR="$T/repo"
    # Host config dir: the base .env no longer lives in the checkout. Exported so
    # lib.sh does not fall back to the real /etc/orcaster.
    export CONFIG_DIR="$T/etc"
    mkdir -p "$CONFIG_DIR"

    # Test setup.env with the multi-use token present (cloud-init flow).
    export ENV_FILE_OVERRIDE="$T/setup.env"
    cat > "$ENV_FILE_OVERRIDE" <<EOF
ORCH_BASE_URL=https://orch.example.com
ORCH_ENROLLMENT_TOKEN=tok-multiuse
EOF

    # Mocks in PATH: id (simulates root), install (-d => mkdir), chown (noop).
    mkdir -p "$T/bin"
    cat > "$T/bin/id" <<'M'
#!/usr/bin/env bash
[ "${1:-}" = "-u" ] && { echo 0; exit 0; }
exit 0
M
    cat > "$T/bin/install" <<'M'
#!/usr/bin/env bash
for last in "$@"; do :; done
mkdir -p "$last"
M
    cat > "$T/bin/chown" <<'M'
#!/usr/bin/env bash
exit 0
M
    chmod +x "$T/bin/id" "$T/bin/install" "$T/bin/chown"
    write_ls_stub "$T/bin"
    export PATH="$T/bin:$PATH"
}

teardown() { rm -rf "$T"; }

@test "03-worker-env: does NOT stamp the multi-use token from setup.env into the base .env" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ -f "$CONFIG_DIR/env" ]
    grep -q '^ORCH_ENROLLMENT_TOKEN=$' "$CONFIG_DIR/env"
    ! grep -q 'tok-multiuse' "$CONFIG_DIR/env"
}

@test "03-worker-env: a rerun preserves the token/api_key placed by the operator" {
    printf 'ORCH_API_KEY=key-prev\nORCH_ENROLLMENT_TOKEN=tok-operator\n' \
        > "$CONFIG_DIR/env"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_API_KEY=key-prev$' "$CONFIG_DIR/env"
    grep -q '^ORCH_ENROLLMENT_TOKEN=tok-operator$' "$CONFIG_DIR/env"
    [[ "$output" == *"credentials preserved"* ]]
}

@test "03-worker-env: without credentials it warns that the token is missing (manual flow)" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ORCH_ENROLLMENT_TOKEN still has to be placed"* ]]
}

@test "03-worker-env: propagates the disk gate from setup.env to the base .env" {
    cat >> "$ENV_FILE_OVERRIDE" <<'EOF'
ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05
ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600
EOF
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05$' "$CONFIG_DIR/env"
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600$' "$CONFIG_DIR/env"
}

@test "03-worker-env: a rerun keeps the disk gate configured in setup.env" {
    cat >> "$ENV_FILE_OVERRIDE" <<'EOF'
ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05
ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600
EOF
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05$' "$CONFIG_DIR/env"
    grep -q '^ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600$' "$CONFIG_DIR/env"
}

# --- Isolation of the tasks' environment ------------------------

# Effective value of a key in an --env-file: the LAST occurrence (podman and
# python-dotenv resolve duplicates that way).
last_value() {
    sed -n "s/^$2=//p" "$1" | tail -n1
}

@test "03-worker-env: a new install stamps ORCH_ENFORCE_ISOLATION=true" {
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(last_value "$CONFIG_DIR/env" ORCH_ENFORCE_ISOLATION)" = "true" ]
    [ "$(grep -c '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env")" -eq 1 ]
    grep -q '^ORCH_ISOLATION_REQUIRED_ENV=$' "$CONFIG_DIR/env"
}

@test "03-worker-env: a previous env without the line stamps an explicit false" {
    printf 'ORCH_API_KEY=key-prev\nORCH_ENROLLMENT_TOKEN=\n' > "$CONFIG_DIR/env"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_ENFORCE_ISOLATION=false$' "$CONFIG_DIR/env"
    [ "$(grep -c '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env")" -eq 1 ]
}

@test "03-worker-env: a rerun keeps the previous value of the flag" {
    printf 'ORCH_API_KEY=key-prev\nORCH_ENFORCE_ISOLATION=false\nORCH_ENFORCE_ISOLATION=true\n' > "$CONFIG_DIR/env"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(last_value "$CONFIG_DIR/env" ORCH_ENFORCE_ISOLATION)" = "true" ]
    [ "$(grep -c '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env")" -eq 1 ]
}

@test "03-worker-env: the setup.env value wins over the previous env" {
    printf 'ORCH_API_KEY=key-prev\nORCH_ENFORCE_ISOLATION=false\n' > "$CONFIG_DIR/env"
    echo 'ORCH_ENFORCE_ISOLATION=true' >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_ENFORCE_ISOLATION=true$' "$CONFIG_DIR/env"
    [ "$(grep -c '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env")" -eq 1 ]
}

@test "03-worker-env: setup.env false on a new install stamps false" {
    echo 'ORCH_ENFORCE_ISOLATION=false' >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(last_value "$CONFIG_DIR/env" ORCH_ENFORCE_ISOLATION)" = "false" ]
}

@test "03-worker-env: a true from EXTRA_ENV is never overridden by a stamped false" {
    # The previous env carries false (existing host); EXTRA_ENV declares true and
    # setup.env does not declare the flag: no line of its own is stamped.
    printf 'ORCH_API_KEY=key-prev\nORCH_ENFORCE_ISOLATION=false\n' > "$CONFIG_DIR/env"
    cat >> "$ENV_FILE_OVERRIDE" <<'EOF2'
EXTRA_ENV='OTHER=1
ORCH_ENFORCE_ISOLATION=true'
EOF2
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(last_value "$CONFIG_DIR/env" ORCH_ENFORCE_ISOLATION)" = "true" ]
    [ "$(grep -c '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env")" -eq 1 ]
    # Rerun: the true from EXTRA_ENV is still the only and the effective one.
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    [ "$(last_value "$CONFIG_DIR/env" ORCH_ENFORCE_ISOLATION)" = "true" ]
    [ "$(grep -c '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env")" -eq 1 ]
}

@test "03-worker-env: the isolation block goes after EXTRA_ENV" {
    cat >> "$ENV_FILE_OVERRIDE" <<'EOF2'
EXTRA_ENV='EXAMPLE_PROTECTED_VAR=x'
ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR'
EOF2
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    extra_line="$(grep -n '^EXAMPLE_PROTECTED_VAR=' "$CONFIG_DIR/env" | cut -d: -f1)"
    flag_line="$(grep -n '^ORCH_ENFORCE_ISOLATION=' "$CONFIG_DIR/env" | cut -d: -f1)"
    list_line="$(grep -n '^ORCH_ISOLATION_REQUIRED_ENV=' "$CONFIG_DIR/env" | cut -d: -f1)"
    [ "$flag_line" -gt "$extra_line" ]
    [ "$list_line" -gt "$extra_line" ]
}

@test "03-worker-env: stamps the normalized comma-joined list" {
    cat >> "$ENV_FILE_OVERRIDE" <<'EOF2'
ISOLATION_REQUIRED_ENV=' EXAMPLE_PROTECTED_VAR,  OTHER_VAR
EXAMPLE_PROTECTED_VAR,,_THIRD '
EOF2
    run bash "$SCRIPT"
    [ "$status" -eq 0 ]
    grep -q '^ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR,OTHER_VAR,_THIRD$' "$CONFIG_DIR/env"
    [ "$(grep -c '^ORCH_ISOLATION_REQUIRED_ENV=' "$CONFIG_DIR/env")" -eq 1 ]
}

@test "03-worker-env: a malformed list aborts without touching the env" {
    printf 'ORCH_API_KEY=key-prev\n' > "$CONFIG_DIR/env"
    echo "ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR,1BAD'" >> "$ENV_FILE_OVERRIDE"
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [ "$(cat "$CONFIG_DIR/env")" = "ORCH_API_KEY=key-prev" ]
    [[ "$output" == *"1BAD"* ]]
}
