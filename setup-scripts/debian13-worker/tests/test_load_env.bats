#!/usr/bin/env bats
# Tests of load_env: defaults of the fleet parameters and validation.

setup() {
    TEST_TMP="$(mktemp -d)"
    # lib.sh computes ENV_FILE relative to SCRIPT_DIR; we force it to a test setup.env.
    export ENV_FILE_OVERRIDE="$TEST_TMP/setup.env"
    # shellcheck disable=SC1091
    source "${BATS_TEST_DIRNAME}/../lib.sh"
}

teardown() { rm -rf "$TEST_TMP"; }

@test "load_env: WORKER_COUNT defaults to 1" {
    printf 'ORCH_BASE_URL=https://orch.example.com\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$WORKER_COUNT" -eq 1 ]
    [ -z "$WORKER_NAME_PREFIX" ]
    [ -z "$EXTRA_ENV" ]
}

@test "load_env: honors an explicit WORKER_COUNT" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nWORKER_COUNT=4\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$WORKER_COUNT" -eq 4 ]
}

@test "load_env: rejects a non-integer WORKER_COUNT" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nWORKER_COUNT=two\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"WORKER_COUNT"* ]]
}

@test "load_env: propagates ORCH_ENROLLMENT_TOKEN" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ENROLLMENT_TOKEN=tok-fleet\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$ORCH_ENROLLMENT_TOKEN" = "tok-fleet" ]
}

@test "load_env: EGRESS_ISOLATION defaults to 1" {
    printf 'ORCH_BASE_URL=https://orch.example.com\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$EGRESS_ISOLATION" = "1" ]
}

@test "load_env: honors EGRESS_ISOLATION=0 from setup.env" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nEGRESS_ISOLATION=0\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$EGRESS_ISOLATION" = "0" ]
}

@test "load_env: rejects EGRESS_ISOLATION other than 0 or 1" {
    # The Makefile enables the topology with an exact match on '1' (ifeq): a
    # "truthy" value such as 'true' would disable isolation SILENTLY.
    printf 'ORCH_BASE_URL=https://orch.example.com\nEGRESS_ISOLATION=true\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"EGRESS_ISOLATION"* ]]
}

@test "load_env: WORKER_MEMORY defaults to empty (no cap)" {
    printf 'ORCH_BASE_URL=https://orch.example.com\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ -z "$WORKER_MEMORY" ]
}

@test "load_env: honors WORKER_MEMORY from setup.env" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nWORKER_MEMORY=28g\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$WORKER_MEMORY" = "28g" ]
}

@test "load_env: rejects WORKER_MEMORY with an invalid format" {
    # podman --memory expects a number + optional b/k/m/g suffix; a typo must
    # abort the deployment here and not make podman run fail halfway through the
    # fleet.
    printf 'ORCH_BASE_URL=https://orch.example.com\nWORKER_MEMORY=lots\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"WORKER_MEMORY"* ]]
}

@test "load_env: rejects WORKER_MEMORY with a double suffix" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nWORKER_MEMORY=28gb\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"WORKER_MEMORY"* ]]
}

@test "load_env: rejects WORKER_MEMORY without a suffix (plain bytes)" {
    # '28' is a valid format for podman --memory (28 bytes) but is almost
    # certainly a typo for '28g'; below podman's minimum the container is not
    # even created, and the deployment would fail halfway through the fleet.
    # The suffix is required.
    printf 'ORCH_BASE_URL=https://orch.example.com\nWORKER_MEMORY=28\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"WORKER_MEMORY"* ]]
}

@test "load_env: rejects a zero WORKER_MEMORY" {
    # '0g' matches number+suffix but is below podman's minimum: the container
    # is not even created and the deployment would fail halfway through the
    # fleet.
    printf 'ORCH_BASE_URL=https://orch.example.com\nWORKER_MEMORY=0g\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"WORKER_MEMORY"* ]]
}

# --- Read-only host mounts --------------------------------------
# load_env validates only the SYNTAX (it runs as root, which reads any mode); the
# host state is measured by run-fleet.sh and the Makefile as the worker user.
# The rejection classes one by one are in test_check_ro_mounts.bats: here it is
# enough to show that load_env uses the validator and aborts naming the entry.

@test "load_env: WORKER_RO_MOUNTS defaults to empty (no mounts)" {
    printf 'ORCH_BASE_URL=https://orch.example.com\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ -z "$WORKER_RO_MOUNTS" ]
}

@test "load_env: honors WORKER_RO_MOUNTS from setup.env without requiring it to exist on this host" {
    # /opt/example-ro does not have to exist on the machine running the tests:
    # existence is measured later, as the worker user.
    printf "ORCH_BASE_URL=https://orch.example.com\nWORKER_RO_MOUNTS='/opt/example-ro /srv/example-ro/snapshots'\n" > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$WORKER_RO_MOUNTS" = "/opt/example-ro /srv/example-ro/snapshots" ]
}

@test "load_env: rejects a relative WORKER_RO_MOUNTS and names the entry" {
    printf "ORCH_BASE_URL=https://orch.example.com\nWORKER_RO_MOUNTS='/opt/example-ro opt/x'\n" > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"'opt/x' is not an absolute path"* ]]
}

@test "load_env: rejects WORKER_RO_MOUNTS on a reserved path" {
    printf "ORCH_BASE_URL=https://orch.example.com\nWORKER_RO_MOUNTS='/var/lib/orcaster'\n" > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"'/var/lib/orcaster' is, is under, or contains"* ]]
}

@test "load_env: rejects WORKER_RO_MOUNTS with a colon" {
    printf "ORCH_BASE_URL=https://orch.example.com\nWORKER_RO_MOUNTS='/opt/example-ro:/x'\n" > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"WORKER_RO_MOUNTS"* ]]
}

# --- Worker disk gate -------------------------------------------

@test "load_env: disk gate floor defaults to empty" {
    printf 'ORCH_BASE_URL=https://orch.example.com\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ -z "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" ]
}

@test "load_env: gate fail-open deadline defaults to empty" {
    printf 'ORCH_BASE_URL=https://orch.example.com\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ -z "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" ]
}

@test "load_env: honors the disk gate floor from setup.env" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" = "0.05" ]
}

@test "load_env: accepts the disk gate floor at 0 (disables the gate)" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" = "0" ]
}

@test "load_env: accepts the disk gate floor at 1" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=1\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" = "1" ]
}

@test "load_env: rejects a disk gate floor greater than 1" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=1.5\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION"* ]]
}

@test "load_env: rejects a disk gate floor with an invalid format" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=twenty\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION"* ]]
}

@test "load_env: honors the gate fail-open deadline from setup.env" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" = "600" ]
}

@test "load_env: accepts the gate fail-open deadline at 0 (disables the fail-open)" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=0\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" = "0" ]
}

@test "load_env: rejects a gate fail-open deadline with an invalid format" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=half-hour\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS"* ]]
}

# --- Warning for a key duplicated between the gate and EXTRA_ENV: NOT
# fail-fast (a .env with the old workaround in EXTRA_ENV is a state that works
# today), only a warning on stderr so that the operator cleans up the leftover.

@test "load_env: warns if the gate floor is also declared in EXTRA_ENV" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05\nEXTRA_ENV="ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.10"\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -eq 0 ]
    [[ "$output" == *"ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION"* ]]
    [[ "$output" == *"EXTRA_ENV"* ]]
    [[ "$output" == *"0.05"* ]]   # names the value that ends up winning (the gate one)
}

@test "load_env: warns if the gate deadline is also declared in EXTRA_ENV" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=600\nEXTRA_ENV="ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=1200"\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -eq 0 ]
    [[ "$output" == *"ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS"* ]]
    [[ "$output" == *"600"* ]]
}

@test "load_env: warns with a multiline EXTRA_ENV that includes the gate key among others" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05\nEXTRA_ENV="API_TOKEN=tok_xxx\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.10\nGITHUB_TOKEN=ghp_yyy"\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -eq 0 ]
    [[ "$output" == *"ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION"* ]]
}

@test "load_env: warns even if the key is indented in a multiline EXTRA_ENV" {
    # An EXTRA_ENV written with indentation in the user-data is still honored by
    # podman --env-file (it does a TrimSpace of each line), so the duplicate is
    # real: the warning's grep cannot anchor on a bare '^'.
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05\nEXTRA_ENV="API_TOKEN=tok_xxx\n    ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.10"\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -eq 0 ]
    [[ "$output" == *"ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION"* ]]
    [[ "$output" == *"EXTRA_ENV"* ]]
}

@test "load_env: does not warn when EXTRA_ENV declares no gate key" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.05\nEXTRA_ENV="API_TOKEN=tok_xxx"\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -eq 0 ]
    [[ "$output" != *"CACHE_CLAIM"* ]]
}

@test "load_env: does not warn if the gate is empty even though EXTRA_ENV declares that key" {
    # With no value in the gate channel, stamp_worker_env does not stamp that
    # line: there is no real duplicate in the resulting .env, so there is nothing
    # to warn about.
    printf 'ORCH_BASE_URL=https://orch.example.com\nEXTRA_ENV="ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.10"\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -eq 0 ]
    [[ "$output" != *"CACHE_CLAIM"* ]]
}

@test "load_env: CLIENT_ENV and CLIENT_PUBKEY live in CONFIG_DIR, not in the checkout" {
    tmp="$(mktemp -d)"
    printf 'ORCH_BASE_URL="https://orch.example.com"\n' > "$tmp/setup.env"
    CONFIG_DIR="$tmp" ENV_FILE_OVERRIDE="$tmp/setup.env"
    export CONFIG_DIR ENV_FILE_OVERRIDE
    load_env
    [ "$CLIENT_ENV" = "$tmp/env" ]
    [ "$CLIENT_PUBKEY" = "$tmp/server-ed25519.pub" ]
    rm -rf "$tmp"
}

@test "load_env: without setup.env the error explains the inline config migration" {
    tmp="$(mktemp -d)"
    CONFIG_DIR="$tmp" ENV_FILE_OVERRIDE="$tmp/missing.env"
    export CONFIG_DIR ENV_FILE_OVERRIDE
    run load_env
    rm -rf "$tmp"
    [ "$status" -ne 0 ]
    [[ "$output" == *"Copy the setup.env from the old checkout to $tmp"* ]]
    [[ "$output" != *"runbook"* ]]
}

# --- Task environment isolation ---------------------------------

@test "load_env: ORCH_ENFORCE_ISOLATION and ISOLATION_REQUIRED_ENV default to empty" {
    printf 'ORCH_BASE_URL=https://orch.example.com\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ -z "$ORCH_ENFORCE_ISOLATION" ]
    [ -z "$ISOLATION_REQUIRED_ENV" ]
}

@test "load_env: accepts ORCH_ENFORCE_ISOLATION true and false" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ENFORCE_ISOLATION=true\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$ORCH_ENFORCE_ISOLATION" = "true" ]
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ENFORCE_ISOLATION=false\n' > "$ENV_FILE_OVERRIDE"
    load_env
    [ "$ORCH_ENFORCE_ISOLATION" = "false" ]
}

@test "load_env: rejects ORCH_ENFORCE_ISOLATION other than true or false" {
    printf 'ORCH_BASE_URL=https://orch.example.com\nORCH_ENFORCE_ISOLATION=1\n' > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"ORCH_ENFORCE_ISOLATION must be true or false"* ]]
}

@test "load_env: normalizes ISOLATION_REQUIRED_ENV to comma-joined names" {
    cat > "$ENV_FILE_OVERRIDE" <<'EOF2'
ORCH_BASE_URL=https://orch.example.com
ISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR, OTHER_VAR	_THIRD
EXAMPLE_PROTECTED_VAR,,'
EOF2
    load_env
    [ "$ISOLATION_REQUIRED_ENV" = "EXAMPLE_PROTECTED_VAR,OTHER_VAR,_THIRD" ]
}

@test "load_env: rejects a malformed ISOLATION_REQUIRED_ENV and names the token" {
    printf "ORCH_BASE_URL=https://orch.example.com\nISOLATION_REQUIRED_ENV='EXAMPLE_PROTECTED_VAR,BAD-NAME'\n" > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"BAD-NAME"* ]]
}

@test "load_env: rejects a wildcard in ISOLATION_REQUIRED_ENV (never patterns)" {
    printf "ORCH_BASE_URL=https://orch.example.com\nISOLATION_REQUIRED_ENV='*_TOKEN'\n" > "$ENV_FILE_OVERRIDE"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"ISOLATION_REQUIRED_ENV"* ]]
}

@test "load_env: aborts if EXTRA_ENV declares ORCH_ISOLATION_REQUIRED_ENV" {
    cat > "$ENV_FILE_OVERRIDE" <<'EOF2'
ORCH_BASE_URL=https://orch.example.com
EXTRA_ENV='OTHER=1
ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR'
EOF2
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"ISOLATION_REQUIRED_ENV in setup.env"* ]]
}

@test "load_env: aborts if EXTRA_ENV declares an indented ORCH_ISOLATION_REQUIRED_ENV" {
    cat > "$ENV_FILE_OVERRIDE" <<'EOF2'
ORCH_BASE_URL=https://orch.example.com
EXTRA_ENV='OTHER=1
    ORCH_ISOLATION_REQUIRED_ENV='
EOF2
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"ORCH_ISOLATION_REQUIRED_ENV"* ]]
}

@test "load_env: aborts if setup.env and EXTRA_ENV declare the flag with different values" {
    cat > "$ENV_FILE_OVERRIDE" <<'EOF2'
ORCH_BASE_URL=https://orch.example.com
ORCH_ENFORCE_ISOLATION=false
EXTRA_ENV='  ORCH_ENFORCE_ISOLATION=true'
EOF2
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"different values"* ]]
}

@test "load_env: the conflict compares the last occurrence in EXTRA_ENV" {
    cat > "$ENV_FILE_OVERRIDE" <<'EOF2'
ORCH_BASE_URL=https://orch.example.com
ORCH_ENFORCE_ISOLATION=true
EXTRA_ENV='ORCH_ENFORCE_ISOLATION=true
ORCH_ENFORCE_ISOLATION=false'
EOF2
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"different values"* ]]
}

@test "load_env: the same flag value in setup.env and EXTRA_ENV does not abort" {
    cat > "$ENV_FILE_OVERRIDE" <<'EOF2'
ORCH_BASE_URL=https://orch.example.com
ORCH_ENFORCE_ISOLATION=true
EXTRA_ENV='ORCH_ENFORCE_ISOLATION=true  '
EOF2
    load_env
    [ "$ORCH_ENFORCE_ISOLATION" = "true" ]
}

@test "load_env: the flag only in EXTRA_ENV does not abort" {
    cat > "$ENV_FILE_OVERRIDE" <<'EOF2'
ORCH_BASE_URL=https://orch.example.com
EXTRA_ENV='ORCH_ENFORCE_ISOLATION=true'
EOF2
    load_env
    [ -z "$ORCH_ENFORCE_ISOLATION" ]
}
