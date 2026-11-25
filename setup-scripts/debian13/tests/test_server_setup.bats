#!/usr/bin/env bats
# Tests for the SERVER setup harness (setup-scripts/debian13/lib.sh).
# Run:  bats setup-scripts/debian13/tests/
#
# NOTE (bats-core 1.13 gotcha): @test names must be plain ASCII. bats does not
# register a @test whose name contains 'í' (the test silently does not run).

load source_guard_stub
bats_require_minimum_version 1.5.0

setup() {
    # shellcheck source=../lib.sh
    source "${BATS_TEST_DIRNAME}/../lib.sh"
    # Isolate the tests from the runner environment: if any of these vars is
    # exported, the default/timeout tests would read the ambient value.
    unset SERVER_START_TIMEOUT RETRY_MAX RETRY_DELAY ENV_FILE_OVERRIDE
    unset ARTIFACT_MAX_FILE_BYTES ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES
    unset TASK_SECRETS_KEYS_PATH SERVER_ENV_PASSTHROUGH EXTRA_A EXTRA_B
    unset MAX_REQUEST_BODY_BYTES MAX_OUTPUT_BODY_BYTES ARTIFACT_UPLOAD_OVERHEAD_BYTES
    unset ARTIFACT_MAX_TOTAL_BYTES CADDYFILE_OVERRIDE CADDY_STUB_RC WORK
}

# --- retry -------------------------------------------------------------------

# Flaky command: fails until attempt OK_AT (counter kept in a file), then passes.
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
    make_flaky "$BATS_TEST_TMPDIR" 3
    export RETRY_DELAY=0
    run retry "$BATS_TEST_TMPDIR/flaky.sh"
    [ "$status" -eq 0 ]
    [ "$(cat "$BATS_TEST_TMPDIR/count")" -eq 3 ]
}

@test "retry: uses all 5 attempts by default and returns the failure" {
    make_flaky "$BATS_TEST_TMPDIR" 99   # never passes
    export RETRY_DELAY=0
    run retry "$BATS_TEST_TMPDIR/flaky.sh"
    [ "$status" -ne 0 ]
    [ "$(cat "$BATS_TEST_TMPDIR/count")" -eq 5 ]
}

@test "retry: honours RETRY_MAX" {
    make_flaky "$BATS_TEST_TMPDIR" 99
    export RETRY_DELAY=0 RETRY_MAX=2
    run retry "$BATS_TEST_TMPDIR/flaky.sh"
    [ "$status" -ne 0 ]
    [ "$(cat "$BATS_TEST_TMPDIR/count")" -eq 2 ]
}

# --- load_env: SERVER_START_TIMEOUT -----------------------------------------

# Write a minimal valid setup.env in $1 (optional: extra line via $2).
write_env() {  # $1=dir  [$2=extra line]
    cat > "$1/setup.env" <<E
DOMAIN="test.example.org"
ADMIN_EMAIL="ops@test.example.org"
${2:-}
E
}

@test "load_env: SERVER_START_TIMEOUT takes the default 120 if not in setup.env" {
    write_env "$BATS_TEST_TMPDIR"
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ "$SERVER_START_TIMEOUT" -eq 120 ]
}

@test "load_env: empty SERVER_START_TIMEOUT falls back to the default 120" {
    write_env "$BATS_TEST_TMPDIR" 'SERVER_START_TIMEOUT=""'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ "$SERVER_START_TIMEOUT" -eq 120 ]
}

@test "load_env: SERVER_START_TIMEOUT honours the setup.env value" {
    write_env "$BATS_TEST_TMPDIR" 'SERVER_START_TIMEOUT="300"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ "$SERVER_START_TIMEOUT" -eq 300 ]
}

@test "load_env: non-numeric SERVER_START_TIMEOUT aborts" {
    write_env "$BATS_TEST_TMPDIR" 'SERVER_START_TIMEOUT="fast"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
}

# --- load_env: artifact quotas -----------------------------------
# They used to be hardcoded in the 03-app-install.sh heredoc, so a normal
# deployment silently reverted a manual edit of the .env. They now derive from
# setup.env with default = the historical value, so an old setup.env generates
# the same .env as always.

@test "load_env: ARTIFACT_MAX_FILE_BYTES takes the historical default if not in setup.env" {
    write_env "$BATS_TEST_TMPDIR"
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ "$ARTIFACT_MAX_FILE_BYTES" -eq 209715200 ]
}

@test "load_env: ARTIFACT_MAX_FILE_BYTES honours the setup.env value" {
    write_env "$BATS_TEST_TMPDIR" 'ARTIFACT_MAX_FILE_BYTES="524288000"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ "$ARTIFACT_MAX_FILE_BYTES" -eq 524288000 ]
}

@test "load_env: non-numeric ARTIFACT_MAX_FILE_BYTES aborts" {
    write_env "$BATS_TEST_TMPDIR" 'ARTIFACT_MAX_FILE_BYTES="200MB"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
}

# Unlike SERVER_START_TIMEOUT, an empty value does NOT fall back to the default: it
# would silently revert the production quota (the reverted-quota bug). It must
# abort the deployment.
@test "load_env: empty ARTIFACT_MAX_FILE_BYTES aborts, does not fall back to the default" {
    write_env "$BATS_TEST_TMPDIR" 'ARTIFACT_MAX_FILE_BYTES=""'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
}

@test "load_env: ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES takes the historical default if not in setup.env" {
    write_env "$BATS_TEST_TMPDIR"
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ "$ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES" -eq 21474836480 ]
}

@test "load_env: ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES honours the setup.env value" {
    write_env "$BATS_TEST_TMPDIR" 'ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES="107374182400"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ "$ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES" -eq 107374182400 ]
}

@test "load_env: non-numeric ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES aborts" {
    write_env "$BATS_TEST_TMPDIR" 'ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES="100GiB"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
}

@test "load_env: empty ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES aborts, does not fall back to the default" {
    write_env "$BATS_TEST_TMPDIR" 'ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES=""'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
}

# --- 03-app-install.sh: the generated .env interpolates the quotas ---------
# The original bug: the literals lived in the heredoc, so the generated .env
# ignored setup.env. This test runs the REAL heredoc and checks that the value
# comes from setup.env, not from a literal. The heredoc is not a sourceable
# function; it is extracted and evaluated the same way as psql_su further down.

@test "generated server/.env: the quotas come from setup.env, not from a literal" {
    # DIFFERENT custom values for both quotas: a partial reversion (a single old
    # literal back in the heredoc) also fails the test.
    write_env "$BATS_TEST_TMPDIR" '
ARTIFACT_MAX_FILE_BYTES="524288000"
ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES="107374182400"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    # load_env sets SERVER_ENV under INSTALL_DIR (/opt, not writable in tests).
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:5432/db"
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    local heredoc
    heredoc="$(awk 'index($0,"\"$SERVER_ENV\" <<EOF"){f=1} f{print} f&&$0=="EOF"{exit}' "$script")"
    # Run the REAL heredoc under set -u, like 03-app-install.sh in a deployment
    # (set -euo pipefail). The subshell inherits the vars load_env set; if a
    # future edit adds ${NEW_VAR} to the heredoc without declaring it in load_env,
    # this aborts the test instead of silently interpolating an empty value.
    ( set -u; as_app() { "$@"; }; eval "$heredoc" )
    # Both quotas must reflect the setup.env value, not the old literals.
    run grep -F 'ARTIFACT_MAX_FILE_BYTES=524288000' "$SERVER_ENV"
    [ "$status" -eq 0 ]
    run grep -F 'ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES=107374182400' "$SERVER_ENV"
    [ "$status" -eq 0 ]
    # None of the hardcoded literals of the original bug may reappear.
    run grep -F 'ARTIFACT_MAX_FILE_BYTES=209715200' "$SERVER_ENV"
    [ "$status" -ne 0 ]
    run grep -F 'ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES=21474836480' "$SERVER_ENV"
    [ "$status" -ne 0 ]
}

# --- 03-app-install.sh: TASK_SECRETS_KEYS_PATH -----------------------
# Re-provisioning rewrites server/.env from setup.env: the keys path must come
# from setup.env (otherwise the rerun erases it) and arrive literally, without
# the heredoc expanding $CREDENTIALS_DIRECTORY.

render_server_env() {  # $1=extra setup.env content
    write_env "$BATS_TEST_TMPDIR" "$1"
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:5432/db"
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    local heredoc
    heredoc="$(awk 'index($0,"\"$SERVER_ENV\" <<EOF"){f=1} f{print} f&&$0=="EOF"{exit}' "$script")"
    ( set -u; as_app() { "$@"; }; eval "$heredoc" )
}

@test "generated server/.env: absent TASK_SECRETS_KEYS_PATH stays empty" {
    render_server_env ''
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=' "$SERVER_ENV"
    [ "$status" -eq 0 ]
}

@test "generated server/.env: TASK_SECRETS_KEYS_PATH comes literally from setup.env" {
    render_server_env "
TASK_SECRETS_KEYS_PATH='\$CREDENTIALS_DIRECTORY/task-secrets-keys'"
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=$CREDENTIALS_DIRECTORY/task-secrets-keys' "$SERVER_ENV"
    [ "$status" -eq 0 ]
}

# --- 03-app-install.sh: SERVER_ENV_PASSTHROUGH -----------------------
# Generic channel for server settings the installer does not know about: it
# copies to server/.env, as is, the variables that setup.env lists. The
# installer names none of them.

@test "generated server/.env: without SERVER_ENV_PASSTHROUGH adds no extra variables" {
    render_server_env ''
    run grep -F 'SERVER_ENV_PASSTHROUGH' "$SERVER_ENV"
    [ "$status" -ne 0 ]
    run grep -E '^(FINGERPRINT|ARTIFACT_INDICATOR)' "$SERVER_ENV"
    [ "$status" -ne 0 ]
}

@test "generated server/.env: SERVER_ENV_PASSTHROUGH copies the listed variables, in order" {
    render_server_env '
SERVER_ENV_PASSTHROUGH="EXTRA_B EXTRA_A"
EXTRA_A="/var/lib/x/a.json"
EXTRA_B="value"'
    run grep -Fx 'EXTRA_A=/var/lib/x/a.json' "$SERVER_ENV"
    [ "$status" -eq 0 ]
    run grep -Fx 'EXTRA_B=value' "$SERVER_ENV"
    [ "$status" -eq 0 ]
    local b a
    b="$(grep -nE '^EXTRA_B=' "$SERVER_ENV" | cut -d: -f1)"
    a="$(grep -nE '^EXTRA_A=' "$SERVER_ENV" | cut -d: -f1)"
    [ "$b" -lt "$a" ]
}

@test "generated server/.env: SERVER_ENV_PASSTHROUGH writes a declared empty value and does not duplicate names" {
    render_server_env '
SERVER_ENV_PASSTHROUGH="EXTRA_A EXTRA_A"
EXTRA_A=""'
    run grep -Fx 'EXTRA_A=' "$SERVER_ENV"
    [ "$status" -eq 0 ]
    [ "$(grep -c '^EXTRA_A=' "$SERVER_ENV")" -eq 1 ]
}

@test "generated server/.env: SERVER_ENV_PASSTHROUGH quotes values with spaces, # or \$" {
    render_server_env '
SERVER_ENV_PASSTHROUGH="EXTRA_A EXTRA_B"
EXTRA_A="one two"
EXTRA_B='"'"'a#b$c'"'"''
    run grep -Fx "EXTRA_A='one two'" "$SERVER_ENV"
    [ "$status" -eq 0 ]
    run grep -Fx "EXTRA_B='a#b\$c'" "$SERVER_ENV"
    [ "$status" -eq 0 ]
}

@test "load_env: SERVER_ENV_PASSTHROUGH with an undeclared variable aborts" {
    write_env "$BATS_TEST_TMPDIR" 'SERVER_ENV_PASSTHROUGH="EXTRA_A"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"EXTRA_A"* ]]
}

@test "load_env: SERVER_ENV_PASSTHROUGH with an invalid name aborts" {
    write_env "$BATS_TEST_TMPDIR" 'SERVER_ENV_PASSTHROUGH="bad-name"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad-name"* ]]
}

@test "load_env: SERVER_ENV_PASSTHROUGH does not accept a name the installer already writes" {
    write_env "$BATS_TEST_TMPDIR" '
SERVER_ENV_PASSTHROUGH="BIND_PORT"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"BIND_PORT"* ]]
}

@test "load_env: SERVER_ENV_PASSTHROUGH rejects a value with a single quote" {
    write_env "$BATS_TEST_TMPDIR" '
SERVER_ENV_PASSTHROUGH="EXTRA_A"
EXTRA_A="it'"'"'s"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"EXTRA_A"* ]]
}

@test "03-app-install.sh writes only the core keys into server/.env" {
    # The installer stays generic: any other server variable reaches server/.env
    # through SERVER_ENV_PASSTHROUGH, never as a key written by the script itself.
    # The list is closed on purpose: a new key in the heredoc fails here until it
    # is added to this list.
    local expected="ARTIFACT_MAX_FILE_BYTES ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES BIND_HOST BIND_PORT DATABASE_URL ED25519_PRIVATE_KEY_PATH MIGRATE_ON_STARTUP PUBLIC_BASE_URL SESSION_COOKIE_SECURE SSE_ENABLED TASK_SECRETS_KEYS_PATH WEBAUTHN_ORIGIN WEBAUTHN_RP_ID WEBAUTHN_RP_NAME"
    local actual
    actual="$(awk '/<<EOF$/ {f=1; next} /^EOF$/ {f=0} f && /^[A-Za-z_][A-Za-z0-9_]*=/ {sub(/=.*/, ""); print}' \
        "$BATS_TEST_DIRNAME/../03-app-install.sh" | LC_ALL=C sort | tr '\n' ' ')"
    [ "$actual" = "$expected " ]
}

# --- 03-app-install.sh: rsync exclusions --------------------------------
# The rsync uses --delete: a file in the destination that no exclusion covers is
# DELETED. Token files of external processes (<name>.env) live in the
# destination, next to their tool's code, and the generic '*.env' exclusion
# covers them. These tests run a real rsync with the list the script reads.

# Print, one per line, the rsync exclusions of 03-app-install.sh.
rsync_excludes() {
    sed -n "/^RSYNC_EXCLUDES=(/,/^)/p" "$BATS_TEST_DIRNAME/../03-app-install.sh" \
        | sed -n "s/.*--exclude '\([^']*\)'.*/\1/p"
}

# Print, in order, the rsync rules of 03 as "<flag> <pattern>" (includes the
# --include rules: rsync applies the first rule that matches).
rsync_filter_rules() {
    sed -n "/^RSYNC_EXCLUDES=(/,/^)/p" "$BATS_TEST_DIRNAME/../03-app-install.sh" \
        | sed -nE "s/.*(--include|--exclude) '([^']*)'.*/\1 \2/p"
}

@test "rsync of 03: the exclusion list is read from the script" {
    local list pat
    list="$(rsync_excludes)"
    for pat in '*.env' 'server/.env' 'secrets' 'setup-scripts/debian13/setup.env*'; do
        grep -Fxq -- "$pat" <<<"$list" || { echo "missing exclusion: $pat"; false; }
    done
}

@test "rsync of 03: --delete keeps the token files, server/.env and secrets" {
    command -v rsync
    local src="$BATS_TEST_TMPDIR/src" dst="$BATS_TEST_TMPDIR/dst" args=() pat
    local flag
    while read -r flag pat; do args+=("$flag" "$pat"); done < <(rsync_filter_rules)
    [ "${#args[@]}" -gt 0 ]

    mkdir -p "$src/server" "$src/tools/tool-a" "$src/tools/tool-b" "$src/tools/tool-c"
    echo code > "$src/server/app.py"
    echo tpl > "$src/server/.env.example"
    echo tpl > "$src/tools/tool-a/queue.env.example"

    # Destination: live state the rsync must not touch, plus a stale file it
    # MUST delete (proves --delete is active). The names are generic: the
    # property is "basename ending in .env, at the root or nested", which is the
    # shape of every tool token file.
    local keep=(
        "tools/tool-a/queue.env"
        "tools/tool-a/pool.env"
        "tools/tool-b/sync.env"
        "tools/example-tool/example-tool.env"
        "queue.env"
        "pool.env"
        "sync.env"
        "example-tool.env"
        "server/.env"
        "secrets/server-ed25519.key"
        "setup-scripts/debian13/setup.env"
    )
    local f
    for f in "${keep[@]}"; do
        mkdir -p "$dst/$(dirname "$f")"
        echo live > "$dst/$f"
    done
    echo stale > "$dst/stale.txt"

    run rsync -a --delete "${args[@]}" "$src"/ "$dst"/
    [ "$status" -eq 0 ]
    for f in "${keep[@]}"; do
        [ -f "$dst/$f" ] || { echo "deleted: $f"; false; }
        [ "$(cat "$dst/$f")" = live ] || { echo "overwritten: $f"; false; }
    done
    [ ! -e "$dst/stale.txt" ]
    [ -f "$dst/server/app.py" ]
    [ -f "$dst/server/.env.example" ]
    [ -f "$dst/tools/tool-a/queue.env.example" ]
}

@test "rsync of 03: the README and the manual use the same '*.env' exclusion" {
    # The manual asks to copy "exactly these exclusions": the three lists must
    # cover the token files with the same generic pattern.
    local doc
    for doc in "$BATS_TEST_DIRNAME/../README.md" "$BATS_TEST_DIRNAME/../../../docs/user-manual.md"; do
        grep -Fq -- "--exclude '*.env'" "$doc" || { echo "no '*.env': $doc"; false; }
    done
}

@test "rsync of 03: the '*.env' exclusion leaves out no tracked file" {
    command -v git
    local root
    root="$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)"
    # Path component ending in .env = what '*.env' (no slash) excludes.
    run bash -c "git -C '$root' ls-files | tr '/' '\n' | grep -E '\.env\$'"
    [ "$status" -ne 0 ]
    [ -z "$output" ]
}

# --- 03-app-install.sh: body limits ---------------------------
# 06-caddy.sh renders these same limits in the Caddyfile; if setup.env changed
# them only for Caddy, the app kept its own. 03 propagates them to server/.env
# ONLY if setup.env declares them (never a blind default).

@test "generated server/.env: without body limits in setup.env writes none" {
    render_server_env ''
    run grep -E '^(MAX_REQUEST_BODY_BYTES|MAX_OUTPUT_BODY_BYTES|ARTIFACT_UPLOAD_OVERHEAD_BYTES|ARTIFACT_MAX_TOTAL_BYTES)=' "$SERVER_ENV"
    [ "$status" -ne 0 ]
    run grep -F 'Body limits' "$SERVER_ENV"
    [ "$status" -ne 0 ]
    # The usual quotas are still there.
    run grep -Fx 'ARTIFACT_MAX_FILE_BYTES=209715200' "$SERVER_ENV"
    [ "$status" -eq 0 ]
}

@test "generated server/.env: propagates only the body limits declared in setup.env" {
    render_server_env '
MAX_REQUEST_BODY_BYTES="8388608"
ARTIFACT_MAX_TOTAL_BYTES="314572800"'
    run grep -Fx 'MAX_REQUEST_BODY_BYTES=8388608' "$SERVER_ENV"
    [ "$status" -eq 0 ]
    run grep -Fx 'ARTIFACT_MAX_TOTAL_BYTES=314572800' "$SERVER_ENV"
    [ "$status" -eq 0 ]
    # Undeclared ones are not written (the app uses its default).
    run grep -E '^(MAX_OUTPUT_BODY_BYTES|ARTIFACT_UPLOAD_OVERHEAD_BYTES)=' "$SERVER_ENV"
    [ "$status" -ne 0 ]
    # 06 reads the same value from there as the app.
    run server_env_value ARTIFACT_MAX_TOTAL_BYTES
    [ "$output" = "314572800" ]
}

@test "generated server/.env: the four declared body limits reach the file" {
    render_server_env '
MAX_REQUEST_BODY_BYTES="8388608"
MAX_OUTPUT_BODY_BYTES="16777216"
ARTIFACT_UPLOAD_OVERHEAD_BYTES="1048576"
ARTIFACT_MAX_TOTAL_BYTES="314572800"'
    for kv in MAX_REQUEST_BODY_BYTES=8388608 MAX_OUTPUT_BODY_BYTES=16777216 \
              ARTIFACT_UPLOAD_OVERHEAD_BYTES=1048576 ARTIFACT_MAX_TOTAL_BYTES=314572800; do
        run grep -Fx "$kv" "$SERVER_ENV"
        [ "$status" -eq 0 ] || { echo "missing $kv"; return 1; }
    done
}

# --- 03-app-install.sh: a rerun does not erase TASK_SECRETS_KEYS_PATH --------
# If server/.env already has the path and setup.env does not declare it, the
# rerun stops before writing anything (fail closed). The tests run the lib.sh
# function and the real heredoc; 03-app-install.sh requires root, so it is not
# run in full.

# Write a previous server/.env ($1 = content) and run the write step:
# rerun check + real heredoc, on that same file.
rerun_server_env() {  # $1=previous server/.env content  $2=extra setup.env lines
    write_env "$BATS_TEST_TMPDIR" "$2"
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    printf '%s\n' "$1" > "$BATS_TEST_TMPDIR/server.env"
    load_env
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:5432/db"
    check_task_secrets_keys_rerun
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    local heredoc
    heredoc="$(awk 'index($0,"\"$SERVER_ENV\" <<EOF"){f=1} f{print} f&&$0=="EOF"{exit}' "$script")"
    ( set -u; as_app() { "$@"; }; eval "$heredoc" )
}

@test "rerun: value in server/.env and none in setup.env aborts without touching server/.env" {
    local before
    before="$BATS_TEST_TMPDIR/server.env.before"
    printf '%s\n' "TASK_SECRETS_KEYS_PATH=/opt/orcaster/secrets/task-secrets-keys" > "$BATS_TEST_TMPDIR/server.env"
    cp "$BATS_TEST_TMPDIR/server.env" "$before"
    run rerun_server_env "TASK_SECRETS_KEYS_PATH=/opt/orcaster/secrets/task-secrets-keys" ""
    [ "$status" -ne 0 ]
    [[ "$output" == *"TASK_SECRETS_KEYS_PATH"* ]]
    [[ "$output" == *"setup.env"* ]]
    [[ "$output" == *"server.env"* ]]
    cmp "$before" "$BATS_TEST_TMPDIR/server.env"
}

@test "rerun: quoted value in server/.env and empty setup.env also aborts" {
    run rerun_server_env 'TASK_SECRETS_KEYS_PATH="/opt/keys"' ""
    [ "$status" -ne 0 ]
    [[ "$output" == *"TASK_SECRETS_KEYS_PATH"* ]]
}

# python-dotenv (what the server uses to read server/.env) accepts `export` and
# spaces around `=`: the check must read those forms the same way, or a
# server/.env edited by hand in either of them would still get emptied.
@test "rerun: export KEY=value in server/.env aborts without touching server/.env" {
    local content before
    content="export TASK_SECRETS_KEYS_PATH=/a/b"
    before="$BATS_TEST_TMPDIR/server.env.before"
    printf '%s\n' "$content" > "$before"
    run rerun_server_env "$content" ""
    [ "$status" -ne 0 ]
    [[ "$output" == *"TASK_SECRETS_KEYS_PATH=/a/b"* ]]
    cmp "$before" "$BATS_TEST_TMPDIR/server.env"
}

@test "rerun: KEY = value with spaces around = aborts without touching server/.env" {
    local content before
    content="TASK_SECRETS_KEYS_PATH = /a/b"
    before="$BATS_TEST_TMPDIR/server.env.before"
    printf '%s\n' "$content" > "$before"
    run rerun_server_env "$content" ""
    [ "$status" -ne 0 ]
    [[ "$output" == *"TASK_SECRETS_KEYS_PATH=/a/b"* ]]
    cmp "$before" "$BATS_TEST_TMPDIR/server.env"
}

@test "rerun: export with spaces and quotes in server/.env aborts without touching server/.env" {
    local content before
    content='  export   TASK_SECRETS_KEYS_PATH =  "/a/b"'
    before="$BATS_TEST_TMPDIR/server.env.before"
    printf '%s\n' "$content" > "$before"
    run rerun_server_env "$content" ""
    [ "$status" -ne 0 ]
    cmp "$before" "$BATS_TEST_TMPDIR/server.env"
}

@test "rerun: the notice says server/.env was not modified and does not promise nothing changed" {
    run rerun_server_env "TASK_SECRETS_KEYS_PATH=/a/b" ""
    [ "$status" -ne 0 ]
    [[ "$output" == *"server/.env was not modified."* ]]
    [[ "$output" != *"Nothing was changed"* ]]
}

# env_file_value directly: the forms python-dotenv reads the same way, and those it does not.
@test "env_file_value: reads KEY=, export KEY=, KEY = and export KEY = with tabs or spaces" {
    local f="$BATS_TEST_TMPDIR/e.env"
    printf '%s\n' "TASK_SECRETS_KEYS_PATH=/a/b" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/a/b" ]
    printf '%s\n' "export TASK_SECRETS_KEYS_PATH=/a/b" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/a/b" ]
    printf '%s\n' "TASK_SECRETS_KEYS_PATH = /a/b" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/a/b" ]
    printf '%s\n' "export TASK_SECRETS_KEYS_PATH = '/a/b'" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/a/b" ]
    printf '%b\n' "\texport\tTASK_SECRETS_KEYS_PATH\t=\t/a/b\r" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/a/b" ]
}

@test "env_file_value: the last line wins, also across different forms" {
    local f="$BATS_TEST_TMPDIR/e.env"
    printf '%s\n' "TASK_SECRETS_KEYS_PATH=/first" "export TASK_SECRETS_KEYS_PATH = /second" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/second" ]
}

@test "env_file_value: does not confuse another key, comments or export without a value" {
    local f="$BATS_TEST_TMPDIR/e.env"
    printf '%s\n' \
        "TASK_SECRETS_KEYS_PATH_OTHER=/x" \
        "TASK_SECRETS_KEYS_PATHX = /x" \
        "exportTASK_SECRETS_KEYS_PATH=/x" \
        "# export TASK_SECRETS_KEYS_PATH=/x" \
        "export TASK_SECRETS_KEYS_PATH" > "$f"
    [ -z "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" ]
    [ -z "$(env_file_value "$BATS_TEST_TMPDIR/does-not-exist.env" TASK_SECRETS_KEYS_PATH)" ]
}

# python-dotenv discards a trailing ` # ...` comment: without quotes it cuts at
# the first space + `#`; with quotes it takes what is between them and ignores
# the rest. A `#` without a preceding space is part of the value.
@test "env_file_value: discards a trailing comment like python-dotenv" {
    local f="$BATS_TEST_TMPDIR/e.env"
    printf '%s\n' "TASK_SECRETS_KEYS_PATH=/opt/keys # local keys" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/opt/keys" ]
    printf '%b\n' "TASK_SECRETS_KEYS_PATH=/opt/keys\t# tab" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/opt/keys" ]
    printf '%s\n' 'TASK_SECRETS_KEYS_PATH="/opt/keys" # local keys' > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/opt/keys" ]
    printf '%s\n' "TASK_SECRETS_KEYS_PATH='/opt/keys'# local keys" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/opt/keys" ]
    printf '%s\n' 'TASK_SECRETS_KEYS_PATH="/opt/a #b" # c' > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/opt/a #b" ]
    printf '%s\n' "TASK_SECRETS_KEYS_PATH=/opt/a#b" > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/opt/a#b" ]
    printf '%s\n' "export TASK_SECRETS_KEYS_PATH = /opt/keys # c"$'\r' > "$f"
    [ "$(env_file_value "$f" TASK_SECRETS_KEYS_PATH)" = "/opt/keys" ]
}

@test "rerun: equal value with a trailing comment in server/.env continues" {
    run rerun_server_env "TASK_SECRETS_KEYS_PATH=/opt/keys # local keys" "
TASK_SECRETS_KEYS_PATH=/opt/keys"
    [ "$status" -eq 0 ]
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=/opt/keys' "$BATS_TEST_TMPDIR/server.env"
    [ "$status" -eq 0 ]
}

@test "rerun: equal quoted value with a trailing comment in server/.env continues" {
    run rerun_server_env 'TASK_SECRETS_KEYS_PATH="/opt/keys" # local keys' "
TASK_SECRETS_KEYS_PATH=/opt/keys"
    [ "$status" -eq 0 ]
}

@test "rerun: a different value with a trailing comment still aborts and shows it without the comment" {
    run rerun_server_env "TASK_SECRETS_KEYS_PATH=/a/keys # old" "
TASK_SECRETS_KEYS_PATH=/b/keys"
    [ "$status" -ne 0 ]
    [[ "$output" == *"sets TASK_SECRETS_KEYS_PATH=/a/keys, but"* ]]
}

# With a value in both files, the rerun continues only if they match. If they
# differ, regenerating server/.env would make the server load a different keys
# file on its next restart: it stops without touching server/.env.
@test "rerun: different values in both files abort, name both values and both files, leave server/.env intact" {
    local before
    before="$BATS_TEST_TMPDIR/server.env.before"
    printf '%s\n' "TASK_SECRETS_KEYS_PATH=/old/path" > "$before"
    run rerun_server_env "TASK_SECRETS_KEYS_PATH=/old/path" "
TASK_SECRETS_KEYS_PATH='\$CREDENTIALS_DIRECTORY/task-secrets-keys'"
    [ "$status" -ne 0 ]
    [[ "$output" == *"server.env sets TASK_SECRETS_KEYS_PATH=/old/path, but "*"setup.env sets a different value: TASK_SECRETS_KEYS_PATH=\$CREDENTIALS_DIRECTORY/task-secrets-keys."* ]]
    [[ "$output" == *"load a different keys file"* ]]
    [[ "$output" == *"server/.env was not modified."* ]]
    [[ "$output" == *"set the server/.env value in "*"setup.env"* ]]
    [[ "$output" == *"set the same new value in both"* ]]
    cmp "$before" "$BATS_TEST_TMPDIR/server.env"
}

@test "rerun: different plain paths in both files abort without touching server/.env" {
    local before
    before="$BATS_TEST_TMPDIR/server.env.before"
    printf '%s\n' "DATABASE_URL=old" "TASK_SECRETS_KEYS_PATH=/a/keys" > "$before"
    run rerun_server_env "DATABASE_URL=old
TASK_SECRETS_KEYS_PATH=/a/keys" "
TASK_SECRETS_KEYS_PATH=/b/keys"
    [ "$status" -ne 0 ]
    [[ "$output" == *"TASK_SECRETS_KEYS_PATH=/a/keys"* ]]
    [[ "$output" == *"TASK_SECRETS_KEYS_PATH=/b/keys"* ]]
    cmp "$before" "$BATS_TEST_TMPDIR/server.env"
}

@test "rerun: equal plain path in both files continues and rewrites server/.env with it" {
    run rerun_server_env "TASK_SECRETS_KEYS_PATH=/opt/keys" "
TASK_SECRETS_KEYS_PATH=/opt/keys"
    [ "$status" -eq 0 ]
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=/opt/keys' "$BATS_TEST_TMPDIR/server.env"
    [ "$status" -eq 0 ]
}

# setup.env is sourced (single quotes leave $CREDENTIALS_DIRECTORY literal) and
# server/.env is read as text: both forms must compare as equal.
@test "rerun: equal CREDENTIALS_DIRECTORY form (single-quoted in setup.env, literal in server/.env) continues" {
    run rerun_server_env 'TASK_SECRETS_KEYS_PATH=$CREDENTIALS_DIRECTORY/task-secrets-keys' "
TASK_SECRETS_KEYS_PATH='\$CREDENTIALS_DIRECTORY/task-secrets-keys'"
    [ "$status" -eq 0 ]
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=$CREDENTIALS_DIRECTORY/task-secrets-keys' "$BATS_TEST_TMPDIR/server.env"
    [ "$status" -eq 0 ]
}

# python-dotenv ignores edge quotes and spaces: a server/.env with
# `export KEY = "path"` and a setup.env with the same bare path are the same value.
@test "rerun: equal values after quote and whitespace normalisation continue" {
    run rerun_server_env '  export   TASK_SECRETS_KEYS_PATH =  "/opt/keys"' "
TASK_SECRETS_KEYS_PATH='/opt/keys'"
    [ "$status" -eq 0 ]
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=/opt/keys' "$BATS_TEST_TMPDIR/server.env"
    [ "$status" -eq 0 ]
}

@test "rerun: only in setup.env continues and writes the setup.env value" {
    run rerun_server_env "DATABASE_URL=old" "
TASK_SECRETS_KEYS_PATH=/opt/keys"
    [ "$status" -eq 0 ]
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=/opt/keys' "$BATS_TEST_TMPDIR/server.env"
    [ "$status" -eq 0 ]
}

@test "rerun: none in either file continues with an empty key" {
    run rerun_server_env "DATABASE_URL=old" ""
    [ "$status" -eq 0 ]
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=' "$BATS_TEST_TMPDIR/server.env"
    [ "$status" -eq 0 ]
}

@test "rerun: commented line or empty value in server/.env counts as no value" {
    run rerun_server_env "#TASK_SECRETS_KEYS_PATH=/opt/keys
TASK_SECRETS_KEYS_PATH=" ""
    [ "$status" -eq 0 ]
    run grep -Fx 'TASK_SECRETS_KEYS_PATH=' "$BATS_TEST_TMPDIR/server.env"
    [ "$status" -eq 0 ]
}

@test "rerun: 03-app-install.sh calls the check before rsync and before writing server/.env" {
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    local check_line rsync_line write_line
    check_line="$(grep -n '^check_task_secrets_keys_rerun$' "$script" | cut -d: -f1)"
    rsync_line="$(grep -n '^rsync -a --delete' "$script" | head -1 | cut -d: -f1)"
    write_line="$(grep -n '"\$SERVER_ENV" <<EOF' "$script" | cut -d: -f1)"
    [ -n "$check_line" ]
    [ -n "$rsync_line" ]
    [ -n "$write_line" ]
    [ "$check_line" -lt "$rsync_line" ]
    [ "$check_line" -lt "$write_line" ]
}

# --- as_app (cwd guard) ----------
# `sudo` inherits the caller's cwd: if root reruns the suite from /root (700),
# the subcommand fails with "cannot chdir" before running and `set -euo
# pipefail` aborts a healthy deployment. as_app must set a cwd that any user can
# traverse.

# `sudo` mock that prints the cwd it was invoked from.
stub_sudo_pwd() {
    local bin="$1"
    mkdir -p "$bin"
    cat >"$bin/sudo" <<'STUB'
#!/usr/bin/env bash
pwd
STUB
    chmod +x "$bin/sudo"
}

@test "as_app: invokes sudo from a traversable cwd, not from the caller's" {
    tmp="$(mktemp -d)"
    stub_sudo_pwd "$tmp/bin"
    mkdir -p "$tmp/priv"
    chmod 700 "$tmp/priv"
    APP_USER="$(id -un)"
    INSTALL_DIR="$tmp/home"
    UV_CACHE="$tmp/cache"
    PATH="$tmp/bin:$PATH"
    cd "$tmp/priv"
    run as_app true
    [ "$status" -eq 0 ]
    [ "$output" = "/" ]
    cd "$BATS_TEST_DIRNAME"
    rm -rf "$tmp"
}

@test "as_app: propagates the command status" {
    tmp="$(mktemp -d)"
    mkdir -p "$tmp/bin"
    cat >"$tmp/bin/sudo" <<'STUB'
#!/usr/bin/env bash
exit 42
STUB
    chmod +x "$tmp/bin/sudo"
    APP_USER="$(id -un)"
    INSTALL_DIR="$tmp/home"
    UV_CACHE="$tmp/cache"
    PATH="$tmp/bin:$PATH"
    run as_app false
    [ "$status" -eq 42 ]
    rm -rf "$tmp"
}

# Extract a function from 01-postgresql.sh (not sourceable: it runs the whole
# deployment). Takes from `name() {` to the first `}` in column 0, or the single
# line if the function is one line.
extract_01_func() {  # $1=name
    awk -v n="$1" '
        index($0, n "() {") == 1 { f = 1; print; if ($0 ~ /}[[:space:]]*$/) exit; next }
        f { print; if ($0 ~ /^}/) exit }
    ' "$BATS_TEST_DIRNAME/../01-postgresql.sh"
}

@test "psql_su: invokes sudo from a traversable cwd" {
    tmp="$(mktemp -d)"
    stub_sudo_pwd "$tmp/bin"
    mkdir -p "$tmp/priv"
    chmod 700 "$tmp/priv"
    PATH="$tmp/bin:$PATH"
    # psql_su lives in 01-postgresql.sh, which is not sourceable (it runs the
    # whole deployment). The definition alone is extracted, which is what is
    # being verified.
    eval "$(extract_01_func psql_su)"
    cd "$tmp/priv"
    run psql_su
    [ "$status" -eq 0 ]
    [ "$output" = "/" ]
    cd "$BATS_TEST_DIRNAME"
    rm -rf "$tmp"
}

# --- sudo: explicit installer requirement -----------------------------
# as_app and psql_su drop from root with `sudo -u`. A minimal Debian 13 with
# direct SSH as root ships no sudo: 00 installs it and every use checks for it
# first, with an installer error instead of the shell's raw 127.

@test "require_sudo: without sudo in PATH aborts with an error that names sudo" {
    mkdir -p "$BATS_TEST_TMPDIR/empty"
    local saved="$PATH"
    # shellcheck disable=SC2123  # PATH deliberately empty: simulates a host without sudo
    PATH="$BATS_TEST_TMPDIR/empty"
    hash -r
    run require_sudo
    PATH="$saved"
    hash -r
    [ "$status" -ne 0 ]
    [[ "$output" == *"ERROR"* ]]
    [[ "$output" == *"sudo"* ]]
    [[ "$output" == *"apt-get install -y sudo"* ]]
}

@test "require_sudo: with sudo in PATH does nothing" {
    stub_sudo_pwd "$BATS_TEST_TMPDIR/bin"
    PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    run require_sudo
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "as_app: without sudo aborts with the installer error, not with 127" {
    mkdir -p "$BATS_TEST_TMPDIR/empty"
    export APP_USER=orcaster INSTALL_DIR=/nonexistent UV_CACHE=/nonexistent
    local saved="$PATH"
    # shellcheck disable=SC2123  # PATH deliberately empty: simulates a host without sudo
    PATH="$BATS_TEST_TMPDIR/empty"
    hash -r
    run as_app true
    PATH="$saved"
    hash -r
    [ "$status" -eq 1 ]
    [[ "$output" == *"ERROR"*"sudo"* ]]
}

@test "00-preflight: installs sudo and verifies it after apt" {
    local f="$BATS_TEST_DIRNAME/../00-preflight.sh"
    run grep -E '^apt_install .*\bsudo\b' "$f"
    [ "$status" -eq 0 ]
    # require_sudo runs after the base packages are installed.
    local n_apt n_req
    n_apt="$(grep -nE '^apt_install ' "$f" | head -1 | cut -d: -f1)"
    n_req="$(grep -nE '^require_sudo$' "$f" | head -1 | cut -d: -f1)"
    [ -n "$n_req" ]
    [ "$n_req" -gt "$n_apt" ]
}

@test "psql_su: a sudo failure returns its exit and leaves an installer error" {
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    printf '#!/usr/bin/env bash\necho "sudo: boom" >&2\nexit 127\n' > "$BATS_TEST_TMPDIR/bin/sudo"
    chmod +x "$BATS_TEST_TMPDIR/bin/sudo"
    PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    eval "$(extract_01_func psql_su)"
    run -127 psql_su -c "SELECT 1"
    [[ "$output" == *"ERROR"*"postgres"* ]]
}

@test "pg_exists: a psql_su failure aborts with its own message, does not return false" {
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    printf '#!/usr/bin/env bash\nexit 127\n' > "$BATS_TEST_TMPDIR/bin/sudo"
    chmod +x "$BATS_TEST_TMPDIR/bin/sudo"
    PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    eval "$(extract_01_func psql_su)"
    eval "$(extract_01_func pg_exists)"
    # Inside an `if`, as 01 uses it: the failure must stop, not take the else branch.
    run bash -c "$(declare -f err die psql_su pg_exists require_sudo)
        if pg_exists \"SELECT 1\" \"role x\"; then echo BRANCH-YES; else echo BRANCH-NO; fi
        echo CONTINUED"
    [ "$status" -eq 1 ]
    [[ "$output" == *"ERROR"*"role x"* ]]
    [[ "$output" != *"BRANCH-"* ]]
    [[ "$output" != *"CONTINUED"* ]]
}

@test "pg_exists: tells exists (1) from does not exist (empty)" {
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    printf '#!/usr/bin/env bash\necho "${PG_STUB_OUT:-}"\n' > "$BATS_TEST_TMPDIR/bin/sudo"
    chmod +x "$BATS_TEST_TMPDIR/bin/sudo"
    PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    eval "$(extract_01_func psql_su)"
    eval "$(extract_01_func pg_exists)"
    PG_STUB_OUT=1 run pg_exists "SELECT 1" "x"
    [ "$status" -eq 0 ]
    PG_STUB_OUT='' run pg_exists "SELECT 1" "x"
    [ "$status" -eq 1 ]
}

@test "01-postgresql: no psql_su feeds a pipe or is an if condition" {
    # `if psql_su … | grep -q` swallows the exit of psql_su. The probes go
    # through pg_exists; the other psql_su calls stand alone under set -e.
    # Comments are stripped (they quote the old pattern); `||` is not a pipe.
    local code
    code="$(sed 's/#.*//' "$BATS_TEST_DIRNAME/../01-postgresql.sh")"
    run grep -nE 'psql_su.*[^|]\|([^|]|$)' <<<"$code"
    [ "$status" -ne 0 ]
    run grep -nE '(if|while|until|!)[[:space:]]+psql_su' <<<"$code"
    [ "$status" -ne 0 ]
    # And the check bites: the old pattern is detected.
    run grep -nE 'psql_su.*[^|]\|([^|]|$)' <<<'if psql_su -tAc "SELECT 1" | grep -q 1; then'
    [ "$status" -eq 0 ]
}

@test "01-postgresql: if psql as postgres fails, it stops before creating the role" {
    local tmp="$BATS_TEST_TMPDIR"
    write_ls_stub "$tmp/bin"
    write_root_id_stub "$tmp/bin"
    for c in apt-get systemctl; do
        printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/$c"
        chmod +x "$tmp/bin/$c"
    done
    printf '#!/usr/bin/env bash\necho "sudo: broken stub" >&2\nexit 127\n' > "$tmp/bin/sudo"
    chmod +x "$tmp/bin/sudo"
    write_env "$tmp" "DB_PASSWORD=\"x\"
INSTALL_DIR=\"$tmp/inst\""
    ENV_FILE_OVERRIDE="$tmp/setup.env" PATH="$tmp/bin:$PATH" \
        run bash "$BATS_TEST_DIRNAME/../01-postgresql.sh"
    [ "$status" -eq 1 ]
    [[ "$output" == *"ERROR"* ]]
    [[ "$output" == *"psql"* ]]
    [[ "$output" != *"Creating role"* ]]
}

# --- load_env: body limits -------------------------------------
# Optional and WITHOUT a default in load_env (03 propagates only the declared
# ones), but if declared they must be positive integers of digits only: Caddy
# would read "4MB" as 4,000,000 B, not as 4 MiB.

@test "load_env: optional body limits stay undefined if not in setup.env" {
    write_env "$BATS_TEST_TMPDIR"
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ -z "${MAX_REQUEST_BODY_BYTES+x}" ]
    [ -z "${MAX_OUTPUT_BODY_BYTES+x}" ]
    [ -z "${ARTIFACT_UPLOAD_OVERHEAD_BYTES+x}" ]
    [ -z "${ARTIFACT_MAX_TOTAL_BYTES+x}" ]
}

@test "load_env: declared body limits honour the setup.env value" {
    write_env "$BATS_TEST_TMPDIR" 'MAX_REQUEST_BODY_BYTES="8388608"
ARTIFACT_MAX_TOTAL_BYTES="314572800"'
    export ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env"
    load_env
    [ "$MAX_REQUEST_BODY_BYTES" -eq 8388608 ]
    [ "$ARTIFACT_MAX_TOTAL_BYTES" -eq 314572800 ]
}

@test "load_env: invalid body limits and ARTIFACT_MAX_FILE_BYTES abort" {
    local key bad
    for key in MAX_REQUEST_BODY_BYTES MAX_OUTPUT_BODY_BYTES ARTIFACT_UPLOAD_OVERHEAD_BYTES \
               ARTIFACT_MAX_TOTAL_BYTES ARTIFACT_MAX_FILE_BYTES; do
        for bad in "4MB" "" "0" "-5" "007" "1.5" "4 MiB"; do
            write_env "$BATS_TEST_TMPDIR" "${key}=\"${bad}\""
            ENV_FILE_OVERRIDE="$BATS_TEST_TMPDIR/setup.env" run bash -c \
                "source '$BATS_TEST_DIRNAME/../lib.sh'; load_env"
            [ "$status" -ne 0 ] || { echo "did not abort: ${key}='${bad}'"; return 1; }
            [[ "$output" == *"$key"* ]] || { echo "the error does not name $key: $output"; return 1; }
        done
    done
}

# --- server_env_value / resolve_limit (precedence server/.env > setup.env > default)

# Set SERVER_ENV under the tmpdir and write its content ($1).
use_server_env() {
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    printf '%s\n' "$1" > "$SERVER_ENV"
}

@test "server_env_value: matches the exact line, strips quotes and spaces, last one wins" {
    use_server_env '# MAX_REQUEST_BODY_BYTES=1
MAX_REQUEST_BODY_BYTES_X=2
XMAX_REQUEST_BODY_BYTES=3
MAX_REQUEST_BODY_BYTES=  "7"
MAX_REQUEST_BODY_BYTES= 9 '
    run server_env_value MAX_REQUEST_BODY_BYTES
    [ "$status" -eq 0 ]
    [ "$output" = "9" ]
    use_server_env "MAX_REQUEST_BODY_BYTES='8388608'"
    run server_env_value MAX_REQUEST_BODY_BYTES
    [ "$output" = "8388608" ]
}

@test "server_env_value: absent key or missing file returns 1" {
    use_server_env 'OTHER=1'
    run server_env_value MAX_REQUEST_BODY_BYTES
    [ "$status" -eq 1 ]
    SERVER_ENV="$BATS_TEST_TMPDIR/does-not-exist.env"
    run server_env_value MAX_REQUEST_BODY_BYTES
    [ "$status" -eq 1 ]
}

# Write $1 (with printf %b escapes) as server/.env and require that
# server_env_value MAX_REQUEST_BODY_BYTES returns 0 and the value $2. The forms
# are those python-dotenv (the app's parser) accepts for an assignment.
assert_env_value() {  # $1=line  $2=expected value
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    printf '%b\n' "$1" > "$SERVER_ENV"
    run server_env_value MAX_REQUEST_BODY_BYTES
    [ "$status" -eq 0 ] && [ "$output" = "$2" ] || {
        echo "'$1' -> status=$status output='$output' (expected '$2')"; return 1; }
}

@test "server_env_value: accepts export, indentation, spaces around the equals sign and tab" {
    assert_env_value 'export MAX_REQUEST_BODY_BYTES=8388608' 8388608
    assert_env_value 'export   MAX_REQUEST_BODY_BYTES = 8388608  ' 8388608
    assert_env_value '  MAX_REQUEST_BODY_BYTES=8388608' 8388608
    assert_env_value '\tMAX_REQUEST_BODY_BYTES=8388608' 8388608
    assert_env_value 'export\tMAX_REQUEST_BODY_BYTES=8388608' 8388608
    assert_env_value 'MAX_REQUEST_BODY_BYTES =8388608' 8388608
    assert_env_value 'MAX_REQUEST_BODY_BYTES= 8388608' 8388608
}

@test "server_env_value: accepts CRLF and a trailing comment, with or without quotes" {
    assert_env_value 'MAX_REQUEST_BODY_BYTES=8388608\r' 8388608
    assert_env_value 'MAX_REQUEST_BODY_BYTES="8388608"\r' 8388608
    assert_env_value 'MAX_REQUEST_BODY_BYTES=8388608 # new limit' 8388608
    assert_env_value 'MAX_REQUEST_BODY_BYTES=8388608\t# new limit' 8388608
    assert_env_value 'MAX_REQUEST_BODY_BYTES="8388608" # new limit' 8388608
    assert_env_value "MAX_REQUEST_BODY_BYTES='8388608' # new limit" 8388608
    assert_env_value 'MAX_REQUEST_BODY_BYTES="8388608"# new limit' 8388608
}

@test "server_env_value: export does not match other keys and the last form wins" {
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    printf '%b\n' 'exportMAX_REQUEST_BODY_BYTES=1' 'export MAX_REQUEST_BODY_BYTES_X=2' \
        'MAX_REQUEST_BODY_BYTES=3' 'export MAX_REQUEST_BODY_BYTES = 4 # end' > "$SERVER_ENV"
    run server_env_value MAX_REQUEST_BODY_BYTES
    [ "$status" -eq 0 ]
    [ "$output" = "4" ]
}

@test "server_env_value: a line that mentions the key without being an assignment warns and is ignored" {
    local form
    for form in 'MAX_REQUEST_BODY_BYTES: 8388608' 'MAX_REQUEST_BODY_BYTES 8388608' \
                'MAX_REQUEST_BODY_BYTES' 'export MAX_REQUEST_BODY_BYTES'; do
        SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
        printf '%s\n' "$form" > "$SERVER_ENV"
        run server_env_value MAX_REQUEST_BODY_BYTES
        [ "$status" -eq 1 ] || { echo "did not return 1 with '$form'"; return 1; }
        [[ "$output" == *"mentions MAX_REQUEST_BODY_BYTES"* ]] || { echo "no warning with '$form': $output"; return 1; }
    done
}

@test "server_env_value: a value that does not reduce to digits warns and resolve_limit aborts" {
    local form
    for form in 'MAX_REQUEST_BODY_BYTES=# 8388608' 'MAX_REQUEST_BODY_BYTES=8388608#x' \
                'MAX_REQUEST_BODY_BYTES = 8 MiB'; do
        SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
        printf '%s\n' "$form" > "$SERVER_ENV"
        run resolve_limit out MAX_REQUEST_BODY_BYTES 4194304
        [ "$status" -ne 0 ] || { echo "did not abort with '$form'"; return 1; }
        [[ "$output" == *"does not reduce to an integer"* ]] || { echo "no warning with '$form': $output"; return 1; }
        [[ "$output" == *"must be a positive integer"* ]] || { echo "no die with '$form': $output"; return 1; }
    done
}

@test "resolve_limit: server/.env beats setup.env and setup.env beats the default" {
    use_server_env 'MAX_OUTPUT_BODY_BYTES=16777216'
    MAX_OUTPUT_BODY_BYTES=1000          # setup.env: loses to server/.env
    MAX_REQUEST_BODY_BYTES=8388608      # setup.env: no line in server/.env
    resolve_limit out_a MAX_OUTPUT_BODY_BYTES 33554432
    resolve_limit out_b MAX_REQUEST_BODY_BYTES 4194304
    resolve_limit out_c ARTIFACT_MAX_TOTAL_BYTES 209715200
    [ "$out_a" = "16777216" ]
    [ "$out_b" = "8388608" ]
    [ "$out_c" = "209715200" ]
}

@test "resolve_limit: invalid value in server/.env aborts and names the file" {
    use_server_env 'MAX_OUTPUT_BODY_BYTES=32MB'
    run resolve_limit out MAX_OUTPUT_BODY_BYTES 33554432
    [ "$status" -ne 0 ]
    [[ "$output" == *"MAX_OUTPUT_BODY_BYTES"* ]]
    [[ "$output" == *"$SERVER_ENV"* ]]
}

# --- 06-caddy.sh: real Caddyfile rendering -----------------------
# Runs 06-caddy.sh for real with id/caddy/systemctl/dig stubs at the front of
# PATH and the Caddyfile under the tmpdir (CADDYFILE_OVERRIDE). The previous
# test only grepped the script text and missed that the backticks of a comment
# inside the unquoted heredoc executed request_body and handle as commands
# (SC2006).

make_caddy_stubs() {  # $1=stubs dir
    local bin="$1"
    mkdir -p "$bin"
    # need_root asks `id -u`: simulate root so 06 does not re-run with sudo.
    cat > "$bin/id" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi
STUB
    # caddy: records the call, saves the validated config and exits with
    # CADDY_STUB_RC (0 by default).
    cat > "$bin/caddy" <<'STUB'
#!/usr/bin/env bash
echo "caddy $*" >> "$STUB_DIR/caddy.calls"
case "${1:-}" in
    version) echo "v2.stub" ;;
    validate)
        while [ $# -gt 0 ]; do
            [ "$1" = "--config" ] && cp "$2" "$STUB_DIR/validated.caddyfile"
            shift
        done
        exit "${CADDY_STUB_RC:-0}" ;;
esac
STUB
    cat > "$bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_DIR/systemctl.calls"
STUB
    cat > "$bin/dig" <<'STUB'
#!/usr/bin/env bash
echo "203.0.113.10"
STUB
    # With simulated root, as_app drops with `sudo -u USER env ... cmd`: the stub
    # removes `-u USER` and runs the command as is (the tests lack that user).
    cat > "$bin/sudo" <<'STUB'
#!/usr/bin/env bash
[ "${1:-}" = "-u" ] && shift 2
exec "$@"
STUB
    chmod +x "$bin"/*
    # The source guard acts with simulated root: the test checkout is not
    # root-owned, so `ls` is stubbed as well.
    write_ls_stub "$bin"
}

# Run 06-caddy.sh in the working directory $WORK (default: the test tmpdir).
# $1 = extra setup.env lines; $2 (optional) = server/.env content (if omitted,
# the file does not exist). Leaves the status in $rc; stdout and stderr in
# $STUB_DIR/out and $STUB_DIR/err. A previous Caddyfile, if the test created
# one, is $CADDYFILE_OVERRIDE.
run_06() {
    WORK="${WORK:-$BATS_TEST_TMPDIR}"
    export STUB_DIR="$WORK/stubs"
    make_caddy_stubs "$STUB_DIR/bin"
    mkdir -p "$WORK/home/server" "$WORK/etc"
    write_env "$WORK" "INSTALL_DIR=\"$WORK/home\"
${1:-}"
    if [ "$#" -ge 2 ]; then
        printf '%s\n' "$2" > "$WORK/home/server/.env"
    fi
    export ENV_FILE_OVERRIDE="$WORK/setup.env"
    export CADDYFILE_OVERRIDE="$WORK/etc/Caddyfile"
    rc=0
    PATH="$STUB_DIR/bin:$PATH" bash "$BATS_TEST_DIRNAME/../06-caddy.sh" \
        >"$STUB_DIR/out" 2>"$STUB_DIR/err" || rc=$?
}

# Number of entries in a directory (1 = only the Caddyfile, no temporaries).
count_entries() {
    find "$1" -mindepth 1 | wc -l | tr -d ' '
}

# The three max_size values of the installed Caddyfile, in order: artifacts output default.
caddy_max_sizes() {
    grep -E '^[[:space:]]+max_size ' "$CADDYFILE_OVERRIDE" | awk '{print $2}' | paste -sd' ' -
}

@test "06-caddy: without vars renders the default limits and no shell errors" {
    run_06 ''
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    # (a) no spurious command: request_body and handle used to be executed.
    run grep -c "command not found" "$STUB_DIR/err"
    [ "$output" = "0" ]
    [ ! -s "$STUB_DIR/err" ]
    # (b) 200 MiB + 8 MiB = 218103808 ; 32 MiB ; 4 MiB
    [ "$(caddy_max_sizes)" = "218103808 33554432 4194304" ]
    # The Caddyfile comment comes out intact and the regexp keeps its literal $.
    grep -Fq '# One request_body per handle, never a global one' "$CADDYFILE_OVERRIDE"
    grep -Fq 'path_regexp ^/v1/tasks/[0-9]+/artifacts$' "$CADDYFILE_OVERRIDE"
    grep -Fq 'path_regexp ^/v1/tasks/[0-9]+/output$' "$CADDYFILE_OVERRIDE"
    grep -Fq 'test.example.org {' "$CADDYFILE_OVERRIDE"
}

@test "06-caddy: validates the temporary file, not the Caddyfile, installs what was validated and with the override does not touch systemctl" {
    run_06 ''
    [ "$rc" -eq 0 ]
    grep -Fq -- '--adapter caddyfile' "$STUB_DIR/caddy.calls"
    # What is validated is the temporary file (same directory), never the final destination.
    run grep -Fq -- "--config $CADDYFILE_OVERRIDE --adapter" "$STUB_DIR/caddy.calls"
    [ "$status" -ne 0 ]
    grep -Fq -- "--config $CADDYFILE_OVERRIDE." "$STUB_DIR/caddy.calls"
    cmp "$STUB_DIR/validated.caddyfile" "$CADDYFILE_OVERRIDE"
    # No leftover temporaries and readable by the caddy user (0644).
    [ "$(count_entries "$WORK/etc")" -eq 1 ]
    run bash -c "ls -l '$CADDYFILE_OVERRIDE' | cut -c1-10"
    [ "$output" = "-rw-r--r--" ]
    # CADDYFILE_OVERRIDE (test mode) skips enable/reload/restart and says so in the log.
    [ ! -e "$STUB_DIR/systemctl.calls" ]
    grep -Fq 'CADDYFILE_OVERRIDE set' "$STUB_DIR/out"
    grep -Fq 'skipping systemctl' "$STUB_DIR/out"
    run grep -Fq 'Caddy serving' "$STUB_DIR/out"
    [ "$status" -ne 0 ]
}

@test "06-caddy: the Caddyfile sets Strict-Transport-Security and validates" {
    run_06 ''
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    # A single directive, in the site block, with the exact value.
    [ "$(grep -c 'Strict-Transport-Security' "$CADDYFILE_OVERRIDE")" = "1" ]
    grep -Fxq '    header Strict-Transport-Security "max-age=31536000"' "$CADDYFILE_OVERRIDE"
    # The Caddyfile with the header is the one that was validated.
    grep -Fq 'Strict-Transport-Security' "$STUB_DIR/validated.caddyfile"
    grep -Fq -- '--adapter caddyfile' "$STUB_DIR/caddy.calls"
}

@test "06-caddy: setup.env values change the limits (FILE larger than TOTAL wins)" {
    run_06 'MAX_REQUEST_BODY_BYTES="8388608"
MAX_OUTPUT_BODY_BYTES="1048576"
ARTIFACT_UPLOAD_OVERHEAD_BYTES="1048576"
ARTIFACT_MAX_FILE_BYTES="524288000"'
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    # max(200 MiB, 500 MiB) + 1 MiB = 525336576 ; output 1 MiB ; default 8 MiB
    [ "$(caddy_max_sizes)" = "525336576 1048576 8388608" ]
}

@test "06-caddy: server/.env beats setup.env and quotes are accepted" {
    run_06 'ARTIFACT_MAX_TOTAL_BYTES="104857600"
MAX_REQUEST_BODY_BYTES="8388608"' \
'ARTIFACT_MAX_TOTAL_BYTES=314572800
MAX_OUTPUT_BODY_BYTES="16777216"'
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    # TOTAL: 300 MiB from server/.env (not the 100 MiB of setup.env) + 8 MiB.
    # OUTPUT: 16 MiB from server/.env, quoted. Default: 8 MiB from setup.env.
    [ "$(caddy_max_sizes)" = "322961408 16777216 8388608" ]
}

@test "06-caddy: non-numeric limit aborts without touching the current Caddyfile" {
    local bad n=0
    for bad in "4MB" "" "0" "-1" "010"; do
        n=$((n + 1))
        WORK="$BATS_TEST_TMPDIR/case$n"
        mkdir -p "$WORK/etc"
        echo "CURRENT CADDYFILE" > "$WORK/etc/Caddyfile"
        run_06 "MAX_REQUEST_BODY_BYTES=\"$bad\""
        [ "$rc" -ne 0 ] || { echo "did not abort with '$bad'"; return 1; }
        grep -Fq "MAX_REQUEST_BODY_BYTES" "$STUB_DIR/err"
        [ "$(cat "$CADDYFILE_OVERRIDE")" = "CURRENT CADDYFILE" ]
        [ "$(count_entries "$WORK/etc")" -eq 1 ]
        [ ! -e "$STUB_DIR/systemctl.calls" ]
    done
}

@test "06-caddy: non-numeric value in server/.env aborts without touching the current Caddyfile" {
    mkdir -p "$BATS_TEST_TMPDIR/etc"
    echo "CURRENT CADDYFILE" > "$BATS_TEST_TMPDIR/etc/Caddyfile"
    run_06 '' 'MAX_OUTPUT_BODY_BYTES=32MB'
    [ "$rc" -ne 0 ]
    grep -Fq "MAX_OUTPUT_BODY_BYTES" "$STUB_DIR/err"
    [ "$(cat "$CADDYFILE_OVERRIDE")" = "CURRENT CADDYFILE" ]
    [ ! -e "$STUB_DIR/systemctl.calls" ]
}

@test "06-caddy: if caddy validate fails, the current Caddyfile stays intact" {
    mkdir -p "$BATS_TEST_TMPDIR/etc"
    echo "CURRENT CADDYFILE" > "$BATS_TEST_TMPDIR/etc/Caddyfile"
    export CADDY_STUB_RC=1
    run_06 ''
    [ "$rc" -ne 0 ]
    grep -Fq "is left unchanged" "$STUB_DIR/err"
    [ "$(cat "$CADDYFILE_OVERRIDE")" = "CURRENT CADDYFILE" ]
    # No leftover temporary file and no caddy reload with an invalid config.
    [ "$(count_entries "$WORK/etc")" -eq 1 ]
    [ ! -e "$STUB_DIR/systemctl.calls" ]
}

@test "06-caddy: server/.env with export, spaces and a trailing comment sets the app limits" {
    # The old grep ^KEY= did not see these lines: Caddy cut off at the default
    # while the app accepted the real value (or 06 aborted on the comment).
    run_06 '' $'export MAX_REQUEST_BODY_BYTES=8388608\n  MAX_OUTPUT_BODY_BYTES = 16777216   # comment\nARTIFACT_MAX_TOTAL_BYTES="104857600" # comment\nARTIFACT_UPLOAD_OVERHEAD_BYTES=1048576\r'
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    # artifacts: max(100 MiB, 200 MiB from FILE) + 1 MiB ; output 16 MiB ; default 8 MiB
    [ "$(caddy_max_sizes)" = "210763776 16777216 8388608" ]
    [ ! -s "$STUB_DIR/err" ]
}

@test "06-caddy: a server/.env line that does not reduce to an integer warns and aborts" {
    mkdir -p "$BATS_TEST_TMPDIR/etc"
    echo "CURRENT CADDYFILE" > "$BATS_TEST_TMPDIR/etc/Caddyfile"
    run_06 '' 'MAX_REQUEST_BODY_BYTES=# 8388608'
    [ "$rc" -ne 0 ]
    grep -Fq "does not reduce to an integer" "$STUB_DIR/err"
    grep -Fq "MAX_REQUEST_BODY_BYTES" "$STUB_DIR/err"
    [ "$(cat "$CADDYFILE_OVERRIDE")" = "CURRENT CADDYFILE" ]
}

# --- 06-caddy.sh: key declared in setup.env and absent from server/.env ------

@test "06-caddy: key in setup.env but not in server/.env warns with its name and does not abort" {
    run_06 'MAX_REQUEST_BODY_BYTES="2097152"' 'OTHER=1'
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    grep -Fq "MAX_REQUEST_BODY_BYTES is declared in setup.env but not in" "$STUB_DIR/err"
    grep -Fq "the app will keep its default" "$STUB_DIR/err"
    grep -Fq "Re-run 03-app-install.sh" "$STUB_DIR/err"
    # It warns only about the missing one, and the Caddyfile uses the setup.env value.
    [ "$(grep -c "is declared in setup.env" "$STUB_DIR/err")" -eq 1 ]
    [ "$(caddy_max_sizes)" = "218103808 33554432 2097152" ]
}

@test "06-caddy: without server/.env and with keys in setup.env warns once per key" {
    run_06 'MAX_REQUEST_BODY_BYTES="2097152"
ARTIFACT_UPLOAD_OVERHEAD_BYTES="1048576"'
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    grep -Fq "MAX_REQUEST_BODY_BYTES is declared" "$STUB_DIR/err"
    grep -Fq "ARTIFACT_UPLOAD_OVERHEAD_BYTES is declared" "$STUB_DIR/err"
    [ "$(grep -c "is declared in setup.env" "$STUB_DIR/err")" -eq 2 ]
}

@test "06-caddy: key present in both setup.env and server/.env does not warn" {
    run_06 'MAX_REQUEST_BODY_BYTES="2097152"' 'MAX_REQUEST_BODY_BYTES=2097152'
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    [ ! -s "$STUB_DIR/err" ]
}

# --- install_caddyfile / caddy_activate_or_rollback (lib.sh) -----------------
# systemctl is simulated with a shell function (lib.sh is sourced in the test).

@test "06-caddy: with a current Caddyfile leaves a .prev copy and the new one installed" {
    mkdir -p "$BATS_TEST_TMPDIR/etc"
    echo "CURRENT CADDYFILE" > "$BATS_TEST_TMPDIR/etc/Caddyfile"
    run_06 ''
    [ "$rc" -eq 0 ] || { cat "$STUB_DIR/err"; return 1; }
    [ "$(cat "$CADDYFILE_OVERRIDE.prev")" = "CURRENT CADDYFILE" ]
    grep -Fq 'test.example.org {' "$CADDYFILE_OVERRIDE"
    # Only Caddyfile and Caddyfile.prev: no leftover temporary file.
    [ "$(count_entries "$WORK/etc")" -eq 2 ]
}

@test "install_caddyfile: without a current Caddyfile creates no .prev" {
    echo NEW > "$BATS_TEST_TMPDIR/new"
    install_caddyfile "$BATS_TEST_TMPDIR/new" "$BATS_TEST_TMPDIR/Caddyfile"
    [ "$(cat "$BATS_TEST_TMPDIR/Caddyfile")" = "NEW" ]
    [ ! -e "$BATS_TEST_TMPDIR/Caddyfile.prev" ]
}

@test "caddy_activate_or_rollback: if reload and restart fail restores the previous one and exits with an error" {
    local dest="$BATS_TEST_TMPDIR/Caddyfile"
    echo CURRENT > "$dest"
    echo NEW > "$BATS_TEST_TMPDIR/new"
    install_caddyfile "$BATS_TEST_TMPDIR/new" "$dest"
    [ "$(cat "$dest")" = "NEW" ]
    systemctl() { echo "systemctl $*" >> "$BATS_TEST_TMPDIR/sc.calls"; [ "$1" = "enable" ]; }
    run caddy_activate_or_rollback "$dest"
    [ "$status" -ne 0 ]
    [[ "$output" == *"restored the previous one"* ]]
    [ "$(cat "$dest")" = "CURRENT" ]
    grep -Fq "systemctl reload caddy" "$BATS_TEST_TMPDIR/sc.calls"
    grep -Fq "systemctl restart caddy" "$BATS_TEST_TMPDIR/sc.calls"
}

@test "caddy_activate_or_rollback: without .prev and with a failure says so and keeps the new one" {
    local dest="$BATS_TEST_TMPDIR/Caddyfile"
    echo NEW > "$BATS_TEST_TMPDIR/new"
    install_caddyfile "$BATS_TEST_TMPDIR/new" "$dest"
    systemctl() { [ "$1" = "enable" ]; }
    run caddy_activate_or_rollback "$dest"
    [ "$status" -ne 0 ]
    [[ "$output" == *"there was no previous one"* ]]
    [ "$(cat "$dest")" = "NEW" ]
}

@test "caddy_activate_or_rollback: if reload fails but restart passes keeps the new one" {
    local dest="$BATS_TEST_TMPDIR/Caddyfile"
    echo CURRENT > "$dest"
    echo NEW > "$BATS_TEST_TMPDIR/new"
    install_caddyfile "$BATS_TEST_TMPDIR/new" "$dest"
    systemctl() { [ "$1" != "reload" ]; }
    run caddy_activate_or_rollback "$dest"
    [ "$status" -eq 0 ]
    [ "$(cat "$dest")" = "NEW" ]
}

# --- sync_server_deps: uv.lock is mandatory ----------------------
# 03-app-install.sh used to fall back to `uv sync` without --frozen if the lock
# was missing: it re-resolved against the index and exited 0. A missing lock now
# stops the deployment before invoking uv. as_app is replaced by a stub that
# records the invocation, so the test sees what would have run without a real
# sudo or uv.

stub_as_app_log() {
    AS_APP_LOG="$BATS_TEST_TMPDIR/as_app.log"
    : > "$AS_APP_LOG"
    as_app() { printf '%s\n' "$*" >> "$AS_APP_LOG"; }
}

@test "sync_server_deps: without uv.lock aborts with a message and does not invoke uv" {
    SERVER_DIR="$BATS_TEST_TMPDIR/server"
    mkdir -p "$SERVER_DIR"
    UV_BIN=/usr/local/bin/uv
    stub_as_app_log
    run sync_server_deps
    [ "$status" -ne 0 ]
    [[ "$output" == *"$SERVER_DIR/uv.lock"* ]]
    [ ! -s "$AS_APP_LOG" ]
}

@test "sync_server_deps: with uv.lock runs uv sync --frozen on the server" {
    SERVER_DIR="$BATS_TEST_TMPDIR/server"
    mkdir -p "$SERVER_DIR"
    : > "$SERVER_DIR/uv.lock"
    UV_BIN=/usr/local/bin/uv
    stub_as_app_log
    run sync_server_deps
    [ "$status" -eq 0 ]
    [ "$(cat "$AS_APP_LOG")" = "/usr/local/bin/uv sync --project $SERVER_DIR --frozen" ]
}

@test "sync_server_deps: propagates the uv sync failure" {
    SERVER_DIR="$BATS_TEST_TMPDIR/server"
    mkdir -p "$SERVER_DIR"
    : > "$SERVER_DIR/uv.lock"
    UV_BIN=/usr/local/bin/uv
    as_app() { return 7; }
    run sync_server_deps
    [ "$status" -eq 7 ]
}

@test "03-app-install.sh: resolves dependencies only via sync_server_deps" {
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    grep -qE '^[[:space:]]*sync_server_deps[[:space:]]*$' "$script"
    # No other uv sync invocation in the script: neither with nor without --frozen.
    ! grep -nE '"\$UV_BIN"[[:space:]]+sync' "$script"
}

# --- 03-app-install.sh: root does not write inside INSTALL_DIR ---------------
# INSTALL_DIR belongs to APP_USER: a symbolic link in place of secrets/ or of a
# file would make a root chown, chmod or write reach another file. After the
# chown -R, whatever 03 writes into INSTALL_DIR goes through as_app.

@test "03-app-install.sh: always syncs with rsync, no REPO_ROOT = INSTALL_DIR shortcut" {
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    run grep -F '"$REPO_ROOT" = "$INSTALL_DIR"' "$script"
    [ "$status" -ne 0 ]
    run grep -c '^rsync -a --delete "${RSYNC_EXCLUDES\[@\]}" "$REPO_ROOT"/ "$SRC_STAGE"/$' "$script"
    [ "$output" = 1 ]
}

@test "03-app-install.sh: the rsync into INSTALL_DIR runs as APP_USER" {
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    # No root rsync writes into INSTALL_DIR; the only one that does goes through as_app.
    run grep -nE '^rsync .*INSTALL_DIR' "$script"
    [ "$status" -ne 0 ]
    run grep -c '^as_app rsync -a --delete "${RSYNC_EXCLUDES\[@\]}" "$SRC_STAGE"/ "$INSTALL_DIR"/$' "$script"
    [ "$output" = 1 ]
}

@test "03-app-install.sh: after chown -R there is no root chown, chmod, install or write" {
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    run bash -c "sed -n '/^chown -R /,\$p' '$script' | tail -n +2 \
        | grep -vE '^[[:space:]]*#' \
        | grep -nE '^[[:space:]]*(chown|chmod|install |cat >|cp |mv |ln )'"
    [ "$status" -ne 0 ]
    run grep -nE '^as_app sh -c .*"\$SERVER_ENV" <<EOF' "$script"
    [ "$status" -eq 0 ]
}

@test "02-python-uv.sh: the uv cache is created by APP_USER, not root" {
    local script="$BATS_TEST_DIRNAME/../02-python-uv.sh"
    run grep -nE '^install -d .*UV_CACHE' "$script"
    [ "$status" -ne 0 ]
    run grep -c '^as_app install -d -m 755 "$UV_CACHE"$' "$script"
    [ "$output" = 1 ]
}

@test "03-app-install.sh: the staging area is readable by the APP_USER group across the whole tree" {
    local script="$BATS_TEST_DIRNAME/../03-app-install.sh"
    run grep -c '^chgrp -R "$APP_USER" "$SRC_STAGE"$' "$script"
    [ "$output" = 1 ]
}

@test "rsync of 03: setup.env and its backups do not reach INSTALL_DIR nor get deleted there; the template is copied" {
    command -v rsync
    local src="$BATS_TEST_TMPDIR/src" dst="$BATS_TEST_TMPDIR/dst" args=() flag pat f
    while read -r flag pat; do args+=("$flag" "$pat"); done < <(rsync_filter_rules)
    mkdir -p "$src/setup-scripts/debian13" "$dst/setup-scripts/debian13"
    for f in setup.env setup.env.bak-20261115 setup.env.prev; do
        echo "DB_PASSWORD=secret" > "$src/setup-scripts/debian13/$f"
    done
    echo template > "$src/setup-scripts/debian13/setup.env.example"
    echo old > "$dst/setup-scripts/debian13/setup.env.bak-legacy"
    run rsync -a --delete "${args[@]}" "$src"/ "$dst"/
    [ "$status" -eq 0 ]
    [ -f "$dst/setup-scripts/debian13/setup.env.example" ]
    [ ! -e "$dst/setup-scripts/debian13/setup.env" ]
    [ ! -e "$dst/setup-scripts/debian13/setup.env.bak-20261115" ]
    [ ! -e "$dst/setup-scripts/debian13/setup.env.prev" ]
    # A backup that was already in INSTALL_DIR is not deleted by --delete.
    [ "$(cat "$dst/setup-scripts/debian13/setup.env.bak-legacy")" = old ]
}

# --- server/.env: root does not read it through a link or a FIFO --
# APP_USER owns server/.env and can replace it. A link to a root file or a FIFO
# must not be read as root: they are rejected by type before opening anything
# and the message names the path without printing content.

# `timeout 5` (coreutils); macOS lacks it: gtimeout or, failing that, perl alarm.
with_timeout() {
    if command -v timeout >/dev/null 2>&1; then timeout 5 "$@"
    elif command -v gtimeout >/dev/null 2>&1; then gtimeout 5 "$@"
    else perl -e 'alarm 5; exec @ARGV or exit 127' -- "$@"; fi
}

# Prepare a root "secret" and a server/.env that is a link to it.
plant_env_symlink() {
    SECRET="$BATS_TEST_TMPDIR/root-only"
    printf 'MAX_REQUEST_BODY_BYTES=TOP-SECRET-VALUE\nTASK_SECRETS_KEYS_PATH=/TOP-SECRET-PATH\n' > "$SECRET"
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    ln -s "$SECRET" "$SERVER_ENV"
}

plant_env_fifo() {
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    mkfifo "$SERVER_ENV"
}

assert_refused_without_leak() {
    [ "$status" -ne 0 ]
    [ "$status" -ne 124 ] && [ "$status" -ne 142 ]   # timeout (124) or SIGALRM (142): it hung
    [[ "$output" == *"$SERVER_ENV"* ]]
    [[ "$output" != *TOP-SECRET* ]]
}

@test "guard_server_env: symbolic link to a root file aborts without leaking content" {
    plant_env_symlink
    run with_timeout bash -c 'source "$1"; SERVER_ENV="$2"; APP_USER=orcaster; SERVER_DIR=/x; guard_server_env' _ "$BATS_TEST_DIRNAME/../lib.sh" "$SERVER_ENV"
    assert_refused_without_leak
}

@test "guard_server_env: dangling link also aborts" {
    SERVER_ENV="$BATS_TEST_TMPDIR/server.env"
    ln -s "$BATS_TEST_TMPDIR/does-not-exist" "$SERVER_ENV"
    run with_timeout bash -c 'source "$1"; SERVER_ENV="$2"; APP_USER=orcaster; SERVER_DIR=/x; guard_server_env' _ "$BATS_TEST_DIRNAME/../lib.sh" "$SERVER_ENV"
    [ "$status" -ne 0 ]
    [[ "$output" == *"$SERVER_ENV"* ]]
}

@test "server_env_value: symbolic link to a root file aborts without leaking content" {
    plant_env_symlink
    run with_timeout bash -c 'source "$1"; SERVER_ENV="$2"; APP_USER=orcaster; SERVER_DIR=/x; resolve_limit OUT MAX_REQUEST_BODY_BYTES 1' _ "$BATS_TEST_DIRNAME/../lib.sh" "$SERVER_ENV"
    assert_refused_without_leak
}

@test "server_env_value: FIFO does not hang and aborts" {
    plant_env_fifo
    run with_timeout bash -c 'source "$1"; SERVER_ENV="$2"; APP_USER=orcaster; SERVER_DIR=/x; resolve_limit OUT MAX_REQUEST_BODY_BYTES 1' _ "$BATS_TEST_DIRNAME/../lib.sh" "$SERVER_ENV"
    assert_refused_without_leak
}

@test "check_task_secrets_keys_rerun: symbolic link aborts without leaking content" {
    plant_env_symlink
    run with_timeout bash -c 'source "$1"; SERVER_ENV="$2"; APP_USER=orcaster; SERVER_DIR=/x; TASK_SECRETS_KEYS_PATH=; check_task_secrets_keys_rerun' _ "$BATS_TEST_DIRNAME/../lib.sh" "$SERVER_ENV"
    assert_refused_without_leak
}

@test "check_task_secrets_keys_rerun: FIFO does not hang and aborts" {
    plant_env_fifo
    run with_timeout bash -c 'source "$1"; SERVER_ENV="$2"; APP_USER=orcaster; SERVER_DIR=/x; TASK_SECRETS_KEYS_PATH=; check_task_secrets_keys_rerun' _ "$BATS_TEST_DIRNAME/../lib.sh" "$SERVER_ENV"
    assert_refused_without_leak
}

@test "regular server/.env: same values as before, also reading via as_app as root" {
    use_server_env 'MAX_REQUEST_BODY_BYTES="8388608"
TASK_SECRETS_KEYS_PATH=/opt/keys # c'
    run server_env_value MAX_REQUEST_BODY_BYTES
    [ "$status" -eq 0 ] && [ "$output" = "8388608" ]
    # As root (simulated id) the read goes through as_app, never directly.
    write_root_id_stub "$BATS_TEST_TMPDIR/bin"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    as_app() { echo "as_app $*" >> "$BATS_TEST_TMPDIR/as_app.log"; "$@"; }
    run server_env_value MAX_REQUEST_BODY_BYTES
    [ "$status" -eq 0 ] && [ "$output" = "8388608" ]
    grep -Fq "as_app cat -- $SERVER_ENV" "$BATS_TEST_TMPDIR/as_app.log"
}
