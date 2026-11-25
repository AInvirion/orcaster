#!/usr/bin/env bats
# Tests for the server plugin installation (setup-scripts/debian13/lib.sh):
# PLUGIN_PACKAGES, PLUGIN_GIT_TOKEN_FILE, install_plugin_packages,
# check_configured_plugins_installed and the git askpass.
# Run:  bats setup-scripts/debian13/tests/
#
# NOTE (bats-core 1.13 gotcha): @test names are pure ASCII.
#
# No test uses root or the network: uv, sudo and the venv python are stubs that
# record their arguments and environment in files under $BATS_TEST_TMPDIR.

setup() {
    # shellcheck source=../lib.sh
    source "${BATS_TEST_DIRNAME}/../lib.sh"
    unset ENV_FILE_OVERRIDE SERVER_ENV_PASSTHROUGH TASK_SECRETS_KEYS_PATH
    unset PLUGIN_PACKAGES PLUGIN_GIT_TOKEN_FILE ORCASTER_PLUGINS
    unset ORCASTER_PLUGIN_GIT_TOKEN UV_STUB_RC UV_EXPORT_RC FAKE_EPS TOKEN_PROBE
    # Tests cannot create root-owned files: the required owner becomes the user
    # running the suite (except in the test that verifies the check).
    PLUGIN_TOKEN_FILE_OWNER_UID="$(id -u)"
    T="$BATS_TEST_TMPDIR"
    export ENV_FILE_OVERRIDE="$T/setup.env"
}

# Full commit SHA (40 lowercase hex digits): the only ref that is accepted.
SHA="0123456789abcdef0123456789abcdef01234567"
REPO_URL="https://github.com/example-org/example-plugin"

write_env() {  # $@ = extra lines
    {
        printf 'DOMAIN="test.example.org"\n'
        printf 'ADMIN_EMAIL="ops@test.example.org"\n'
        printf 'INSTALL_DIR="%s"\n' "$T/install"
        printf '%s\n' "$@"
    } > "$T/setup.env"
}

make_token_file() {  # $1=path  [$2=content]
    printf '%s\n' "${2:-ghp_exampletoken123}" > "$1"
    chmod 600 "$1"
}

# --- validate_plugin_packages ------------------------------------------------

@test "plugins: PLUGIN_PACKAGES absent or empty is valid" {
    write_env
    load_env
    [ -z "$PLUGIN_PACKAGES" ]
    write_env 'PLUGIN_PACKAGES=""'
    load_env
    [ -z "$PLUGIN_PACKAGES" ]
}

@test "plugins: accepts git with SHA, exact version and absolute wheel" {
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA
        other-plugin==1.2.3 extra-plugin[x,y]==2.0rc1 /srv/wheels/third_plugin-0.1.0-py3-none-any.whl\""
    load_env
    _plugin_specs
    [ "${#PLUGIN_SPECS[@]}" -eq 4 ]
    [ "${PLUGIN_SPECS[1]}" = "other-plugin==1.2.3" ]
}

@test "plugins: rejects an item that starts with a dash" {
    write_env 'PLUGIN_PACKAGES="--index-url=https://evil.example example-plugin"'
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"starts with '-'"* ]] || false
}

@test "plugins: rejects name @ url written with spaces" {
    write_env 'PLUGIN_PACKAGES="example-plugin @ git+https://github.com/example-org/example-plugin@0123456789abcdef0123456789abcdef01234567"'
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"without spaces"* ]] || false
    write_env 'PLUGIN_PACKAGES="example-plugin@ git+https://github.com/example-org/example-plugin@0123456789abcdef0123456789abcdef01234567"'
    run load_env
    [ "$status" -ne 0 ]
}

@test "plugins: name @ url with spaces is rejected with a message that states the cause" {
    local spec
    for spec in "example-plugin @ git+$REPO_URL@$SHA" "example-plugin @git+$REPO_URL@$SHA" \
                "example-plugin@ git+$REPO_URL@$SHA"; do
        write_env "PLUGIN_PACKAGES=\"$spec\""
        run load_env
        [ "$status" -ne 0 ]
        [[ "$output" == *"PEP 508"* ]] || false
        [[ "$output" == *"without spaces"* ]] || false
    done
}

@test "plugins: accepts the documented name@git+https form without spaces" {
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+https://github.com/example-org/example-plugin@$SHA\""
    run load_env
    [ "$status" -eq 0 ]
}

@test "plugins: rejects a URL with credentials, names the requirement and does not print the token" {
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+https://deploy-user:SECRETVALUE@github.com/example-org/example-plugin@$SHA\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"credentials"* ]] || false
    [[ "$output" == *"example-plugin@git+https://<omitted>@github.com/example-org/example-plugin@$SHA"* ]] || false
    [[ "$output" != *"SECRETVALUE"* ]] || false
    [[ "$output" != *"deploy-user"* ]] || false
}

@test "plugins: rejects a query string in any form, names the requirement and does not print the value" {
    local spec
    for spec in "example-plugin@git+$REPO_URL@$SHA?token=SECRETVALUE" \
                "example-plugin@https://example.org/x.whl?token=SECRETVALUE" \
                "example-plugin==1.0?token=SECRETVALUE" \
                "/srv/wheels/x.whl?token=SECRETVALUE"; do
        write_env "PLUGIN_PACKAGES=\"$spec\""
        run load_env
        [ "$status" -ne 0 ]
        [[ "$output" == *"query string"* ]] || false
        [[ "$output" == *"${spec%%\?*}?<omitted>"* ]] || false
        [[ "$output" != *"SECRETVALUE"* ]] || false
    done
}

@test "plugins: rejects git+http and names the requirement" {
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+http://github.com/example-org/example-plugin@$SHA\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"example-plugin@git+http://github.com/example-org/example-plugin@$SHA"* ]] || false
    [[ "$output" == *"git+http://"* ]] || false
    [[ "$output" == *"unencrypted"* ]] || false
}

@test "plugins: rejects other git schemes (ssh, file)" {
    local spec
    for spec in "example-plugin@git+ssh://git@github.com/example-org/example-plugin@$SHA" \
                "example-plugin@git+file:///srv/repo@$SHA"; do
        write_env "PLUGIN_PACKAGES=\"$spec\""
        run load_env
        [ "$status" -ne 0 ]
        [[ "$output" == *"Only git+https://"* ]] || false
    done
}

@test "plugins: rejects a git+https requirement without a package name" {
    write_env "PLUGIN_PACKAGES=\"git+$REPO_URL@$SHA\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"has no package name"* ]] || false
    [[ "$output" == *"git+$REPO_URL@$SHA"* ]] || false
    write_env "PLUGIN_PACKAGES=\"git+$REPO_URL\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"has no package name"* ]] || false
}

@test "plugins: rejects a git+https requirement without a ref" {
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"pins no ref"* ]] || false
    [[ "$output" == *"example-plugin@git+$REPO_URL'"* ]] || false
}

@test "plugins: rejects a git ref that is not a full SHA (branch, tag, short SHA, uppercase)" {
    local ref
    for ref in main v1.2.0 "${SHA:0:12}" "0123456789ABCDEF0123456789abcdef01234567" "${SHA}0" "--upload-pack=x"; do
        write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$ref\""
        run load_env
        [ "$status" -ne 0 ]
        [[ "$output" == *"example-plugin@git+$REPO_URL@$ref"* ]] || false
        [[ "$output" == *"is not a full commit SHA"* ]] || false
    done
}

@test "plugins: rejects an index requirement without an exact version" {
    local spec
    for spec in example-plugin "example-plugin>=1.0" "example-plugin~=1.0" "example-plugin<2" \
                "example-plugin==1.*" "example-plugin===1.0" "example-plugin==" "example-plugin[x]"; do
        write_env "PLUGIN_PACKAGES=\"$spec\""
        run load_env
        [ "$status" -ne 0 ]
        [[ "$output" == *"'$spec'"* ]] || false
        [[ "$output" == *"does not pin an exact version"* ]] || false
    done
}

@test "plugins: rejects direct URLs and paths that are not an absolute wheel" {
    local spec
    for spec in "example-plugin@https://example.org/x.whl" "https://example.org/x.whl" \
                "./x.whl" "/srv/wheels/pkg" "/srv/wheels/x.whl@v1"; do
        write_env "PLUGIN_PACKAGES=\"$spec\""
        run load_env
        [ "$status" -ne 0 ]
        [[ "$output" == *"is not an accepted requirement"* ]] || false
    done
}

@test "plugins: a single invalid requirement among several valid ones aborts" {
    write_env "PLUGIN_PACKAGES=\"other-plugin==1.0 example-plugin@git+$REPO_URL@main third-plugin==3.0\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"example-plugin@git+$REPO_URL@main"* ]] || false
}

# --- validate_plugin_git_token_file -----------------------------------------

@test "plugins: valid token file is accepted" {
    make_token_file "$T/token"
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    run load_env
    [ "$status" -eq 0 ]
    [[ "$output" != *"ghp_exampletoken123"* ]] || false
}

@test "plugins: token file with a relative path aborts" {
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' 'PLUGIN_GIT_TOKEN_FILE="token"'
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"absolute path"* ]] || false
}

@test "plugins: missing token file aborts" {
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' "PLUGIN_GIT_TOKEN_FILE=\"$T/missing\""
    run load_env
    [ "$status" -ne 0 ]
}

@test "plugins: token file inside INSTALL_DIR aborts" {
    mkdir -p "$T/install/secrets"
    make_token_file "$T/install/secrets/token"
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' "PLUGIN_GIT_TOKEN_FILE=\"$T/install/secrets/token\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"is inside"* ]] || false
}

@test "plugins: token file inside REPO_ROOT aborts" {
    mkdir -p "$T/repo/sub"
    make_token_file "$T/repo/sub/token"
    REPO_ROOT="$T/repo"
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' "PLUGIN_GIT_TOKEN_FILE=\"$T/repo/sub/token\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"is inside"* ]] || false
}

@test "plugins: token file with group or other permissions aborts" {
    make_token_file "$T/token"
    chmod 640 "$T/token"
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"chmod 600"* ]] || false
    chmod 604 "$T/token"
    run load_env
    [ "$status" -ne 0 ]
}

@test "plugins: empty or multi-line token file aborts" {
    : > "$T/token"; chmod 600 "$T/token"
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    run load_env
    [ "$status" -ne 0 ]
    printf 'a\nb\n' > "$T/token"
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"one non-empty line"* ]] || false
}

@test "plugins: symbolic link token file aborts" {
    make_token_file "$T/real"
    ln -s "$T/real" "$T/token"
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    run load_env
    [ "$status" -ne 0 ]
}

@test "plugins: token file owned by someone other than required aborts" {
    make_token_file "$T/token"
    PLUGIN_TOKEN_FILE_OWNER_UID=0
    [ "$(id -u)" -ne 0 ] || skip "the suite runs as root"
    write_env 'PLUGIN_PACKAGES="example-plugin==1.0"' "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    run load_env
    [ "$status" -ne 0 ]
    [[ "$output" == *"must be owned by root"* ]] || false
}

# --- install_plugin_packages -------------------------------------------------

# Stubs: as_app runs the command as the suite's user. uv and git record argv
# and the relevant environment in uv.calls / git.calls; uv creates the -o
# file, returns UV_EXPORT_RC on `export` and UV_STUB_RC on `pip`; git creates
# the directory for `init` and on `fetch` returns the code in $T/git.rc (0 if
# it does not exist: git runs with an empty environment and sees no suite
# variables). sudo drops its options (up to -u USER), prepends $T/bin to the
# PATH that `env` sets and runs the rest.
# The plugins' git runs as root with an empty environment and a fixed PATH
# (PLUGIN_GIT_PATH): the tests prepend $T/bin to that PATH so the stub runs.
# PLUGIN_TEST_SENTINEL is exported in the suite's environment: if it reaches
# git, the environment was not emptied.
# PLUGIN_TEST_RO (exported and readonly) and plugin_test_fn (exported function)
# are set only by the test that verifies them; git records whether they arrive.
# git also:
#   - on `fetch`, leaves the token (if it has one) in .git/config, as a
#     persisted credential would, to check that the tree handed to uv does not
#     carry it;
#   - on `checkout`, writes a pyproject.toml and a package into the tree;
#   - with GIT_ASKPASS, records the permissions of the askpass and its
#     directory and whether the askpass answers with the token (without
#     writing the token to the log).
# uv, on `pip`, records for each file:// requirement whether the tree has a
# .git, whether any file contains TOKEN_PROBE and whether it has the
# pyproject.toml.
stub_install_env() {
    load_env
    SERVER_DIR="$T/server"
    mkdir -p "$T/bin"
    UV_BIN="$T/bin/uv"
    PLUGIN_GIT_PATH="$T/bin:$PLUGIN_GIT_PATH"
    export PLUGIN_TEST_SENTINEL="leaked-from-caller"
    cat > "$T/bin/uv" <<'S'
#!/usr/bin/env bash
log="$(dirname "$0")/../uv.calls"
printf 'ARGV:' >> "$log"; printf ' %s' "$@" >> "$log"; printf '\n' >> "$log"
printf 'ENV_TOKEN:%s\n' "${ORCASTER_PLUGIN_GIT_TOKEN:-<none>}" >> "$log"
printf 'ENV_ASKPASS:%s\n' "${GIT_ASKPASS:-<none>}" >> "$log"
prev=""
for a in "$@"; do
    [ "$prev" = "-o" ] && echo "pkg==1.0" > "$a"
    prev="$a"
done
if [ "$1" = "pip" ]; then
    for a in "$@"; do
        case "$a" in
            *@file://*)
                d="${a#*@file://}"
                printf 'HANDOFF:%s\n' "$d" >> "$log"
                printf 'HANDOFF_GITDIRS:%s\n' "$(find "$d" -name .git | wc -l | tr -d ' ')" >> "$log"
                if [ -n "${TOKEN_PROBE:-}" ]; then
                    printf 'HANDOFF_TOKEN_HITS:%s\n' "$(grep -rl -- "$TOKEN_PROBE" "$d" | wc -l | tr -d ' ')" >> "$log"
                fi
                if [ -f "$d/pyproject.toml" ]; then r=yes; else r=no; fi
                printf 'HANDOFF_PYPROJECT:%s\n' "$r" >> "$log"
                ;;
        esac
    done
fi
if [ "$1" = "export" ]; then exit "${UV_EXPORT_RC:-0}"; fi
if [ "$1" = "pip" ]; then exit "${UV_STUB_RC:-0}"; fi
exit 0
S
    cat > "$T/bin/git" <<'S'
#!/usr/bin/env bash
log="$(dirname "$0")/../git.calls"
printf 'ARGV:' >> "$log"; printf ' %s' "$@" >> "$log"; printf '\n' >> "$log"
printf 'ENV_TOKEN:%s\n' "${ORCASTER_PLUGIN_GIT_TOKEN:-<none>}" >> "$log"
printf 'ENV_ASKPASS:%s\n' "${GIT_ASKPASS:-<none>}" >> "$log"
printf 'ENV_PROMPT:%s\n' "${GIT_TERMINAL_PROMPT:-<none>}" >> "$log"
printf 'ENV_HOME:%s\n' "${HOME:-<none>}" >> "$log"
printf 'ENV_GCG:%s\n' "${GIT_CONFIG_GLOBAL:-<none>}" >> "$log"
printf 'ENV_NOSYSTEM:%s\n' "${GIT_CONFIG_NOSYSTEM:-<none>}" >> "$log"
printf 'ENV_SENTINEL:%s\n' "${PLUGIN_TEST_SENTINEL:-<none>}" >> "$log"
printf 'ENV_RO:%s\n' "${PLUGIN_TEST_RO:-<none>}" >> "$log"
if declare -F plugin_test_fn > /dev/null; then r=yes; else r=no; fi
printf 'ENV_FUNC:%s\n' "$r" >> "$log"
if [ -n "${GIT_ASKPASS:-}" ]; then
    printf 'ASKPASS_MODE:%s\n' "$(ls -ld "$GIT_ASKPASS" 2>/dev/null | cut -c1-10)" >> "$log"
    printf 'ASKPASS_DIR_MODE:%s\n' "$(ls -ld "$(dirname "$GIT_ASKPASS")" 2>/dev/null | cut -c1-10)" >> "$log"
    pw="$("$GIT_ASKPASS" "Password for 'https://github.com': " 2>/dev/null)"
    if [ -n "$pw" ] && [ "$pw" = "${ORCASTER_PLUGIN_GIT_TOKEN:-}" ]; then r=yes; else r=no; fi
    printf 'ASKPASS_WORKS:%s\n' "$r" >> "$log"
fi
dest=""; prev=""
for a in "$@"; do
    [ "$prev" = "-C" ] && dest="$a"
    prev="$a"
done
for a in "$@"; do
    case "$a" in
        init)
            last="${@: -1}"
            mkdir -p "$last/.git"
            printf 'WORKDIR_MODE:%s\n' "$(ls -ld "$(dirname "$last")" | cut -c1-10)" >> "$log"
            ;;
        fetch)
            printf '[http]\n\textraheader = AUTHORIZATION: basic %s\n' \
                "${ORCASTER_PLUGIN_GIT_TOKEN:-}" > "$dest/.git/config"
            rc=0
            [ -f "$(dirname "$0")/../git.rc" ] && rc="$(cat "$(dirname "$0")/../git.rc")"
            exit "$rc"
            ;;
        checkout)
            printf '[project]\nname = "example-plugin"\n' > "$dest/pyproject.toml"
            mkdir -p "$dest/example_plugin"
            printf 'x = 1\n' > "$dest/example_plugin/__init__.py"
            ;;
    esac
done
exit 0
S
    chmod +x "$T/bin/uv" "$T/bin/git"
    PATH="$T/bin:$PATH"
    as_app() { "$@"; }
    sudo() {
        printf '%s\n' "$*" >> "$T/sudo.argv"
        while [ "$#" -gt 0 ] && [ "$1" != "-u" ]; do shift; done
        shift 2
        local -a rest=()
        local a
        for a in "$@"; do
            case "$a" in
                PATH=*) rest+=("PATH=$T/bin:${a#PATH=}") ;;
                *) rest+=("$a") ;;
            esac
        done
        "${rest[@]}"
    }
    export UV_STUB_RC UV_EXPORT_RC TOKEN_PROBE
}

# Root's working directory: the parent of the `git init` destination.
root_workdir() {
    grep -o '^ARGV: -c credential.helper= init --quiet .*' "$T/git.calls" | head -1 \
        | awk '{print $NF}' | xargs dirname
}

@test "plugins: install without PLUGIN_PACKAGES calls neither uv nor git" {
    write_env
    stub_install_env
    run install_plugin_packages
    [ "$status" -eq 0 ]
    [[ "$output" == *"no plugins declared"* ]] || false
    [ ! -e "$T/uv.calls" ]
    [ ! -e "$T/git.calls" ]
}

@test "plugins: install fetches git specs and passes them as file after --constraint in order" {
    write_env 'PLUGIN_PACKAGES="other-plugin==2.0 example-plugin@git+https://github.com/example-org/example-plugin@0123456789abcdef0123456789abcdef01234567 third-plugin==3.0"'
    stub_install_env
    run install_plugin_packages
    [ "$status" -eq 0 ]
    grep -q "^ARGV: export --project $T/server --frozen --no-hashes --no-emit-project" "$T/uv.calls"
    run grep '^ARGV: pip install' "$T/uv.calls"
    [[ "$output" =~ --python\ $T/server/.venv/bin/python\ --constraint\ ([^\ ]+)/constraints.txt\ other-plugin==2.0\ example-plugin@file://([^\ ]+)/src-1\ third-plugin==3.0$ ]] || false
    [ "${BASH_REMATCH[1]}" = "${BASH_REMATCH[2]}" ]
    # git: init, fetch of the ref from the URL (without saving it as a remote), checkout.
    grep -q '^ARGV: -c credential.helper= init --quiet .*/src-1$' "$T/git.calls"
    grep -q '^ARGV: -c credential.helper= -C .*/src-1 fetch --quiet --depth 1 --no-tags https://github.com/example-org/example-plugin 0123456789abcdef0123456789abcdef01234567$' "$T/git.calls"
    grep -q '^ARGV: -c credential.helper= -C .*/src-1 checkout --quiet --detach FETCH_HEAD$' "$T/git.calls"
    run grep -c 'remote' "$T/git.calls"
    [ "$output" = "0" ]
    # Without a token file: git without token or askpass, but without prompt; no sudo.
    run grep -c '^ENV_TOKEN:<none>' "$T/git.calls"
    [ "$output" = "3" ]
    run grep -c '^ENV_PROMPT:0' "$T/git.calls"
    [ "$output" = "3" ]
    [ ! -e "$T/sudo.argv" ]
}

@test "plugins: install removes the temporary directory" {
    write_env 'PLUGIN_PACKAGES="example-plugin@git+https://github.com/example-org/example-plugin@0123456789abcdef0123456789abcdef01234567 other-plugin==2.0"'
    stub_install_env
    run install_plugin_packages
    [ "$status" -eq 0 ]
    dir="$(grep -o -- '--constraint [^ ]*' "$T/uv.calls" | cut -d' ' -f2 | xargs dirname)"
    [ -n "$dir" ]
    [ ! -e "$dir" ]
}

@test "plugins: install with a git failure aborts before uv and removes the temporary directory" {
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\""
    stub_install_env
    echo 128 > "$T/git.rc"
    run install_plugin_packages
    [ "$status" -ne 0 ]
    [[ "$output" == *"git failed"* ]] || false
    [[ "$output" == *"ORCASTER_PLUGINS"* ]] || false
    [ ! -e "$T/uv.calls" ]
    dir="$(grep -o '^ARGV: -c credential.helper= init --quiet .*' "$T/git.calls" | awk '{print $NF}' | xargs dirname)"
    [ -n "$dir" ]
    [ ! -e "$dir" ]
}

@test "plugins: install with a uv export failure aborts before uv pip and removes the temporary directory" {
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA other-plugin==2.0\""
    stub_install_env
    UV_EXPORT_RC=2
    run install_plugin_packages
    [ "$status" -ne 0 ]
    [[ "$output" == *"uv export --frozen"* ]] || false
    [[ "$output" == *"ORCASTER_PLUGINS"* ]] || false
    grep -q '^ARGV: export ' "$T/uv.calls"
    run grep -c '^ARGV: pip ' "$T/uv.calls"
    [ "$output" = "0" ]
    dir="$(grep -o '^ARGV: -c credential.helper= init --quiet .*' "$T/git.calls" | awk '{print $NF}' | xargs dirname)"
    [ -n "$dir" ]
    [ ! -e "$dir" ]
}

@test "plugins: install revalidates PLUGIN_PACKAGES and calls neither git nor uv with a branch ref" {
    write_env
    stub_install_env
    PLUGIN_PACKAGES="other-plugin==2.0 example-plugin@git+$REPO_URL@main"
    run install_plugin_packages
    [ "$status" -ne 0 ]
    [[ "$output" == *"is not a full commit SHA"* ]] || false
    [ ! -e "$T/git.calls" ]
    [ ! -e "$T/uv.calls" ]
}

@test "plugins: install with a uv pip failure aborts with a clear message" {
    write_env 'PLUGIN_PACKAGES="other-plugin==2.0"'
    stub_install_env
    UV_STUB_RC=1
    run install_plugin_packages
    [ "$status" -ne 0 ]
    [[ "$output" == *"uv pip install"* ]] || false
    [[ "$output" == *"ORCASTER_PLUGINS"* ]] || false
}

@test "plugins: the token reaches only git's environment, never uv or argv" {
    make_token_file "$T/token" "ghp_SECRETTOKEN42"
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\"" \
        "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    stub_install_env
    run install_plugin_packages
    [ "$status" -eq 0 ]
    [[ "$output" != *"ghp_SECRETTOKEN42"* ]] || false
    # git: the three steps with token, askpass and no prompt; sudo keeps only that variable.
    run grep -c '^ENV_TOKEN:ghp_SECRETTOKEN42$' "$T/git.calls"
    [ "$output" = "3" ]
    run grep -c '^ASKPASS_WORKS:yes$' "$T/git.calls"
    [ "$output" = "3" ]
    run grep -c '^ENV_PROMPT:0$' "$T/git.calls"
    [ "$output" = "3" ]
    # git does not go through sudo (it runs as root, not as APP_USER).
    [ ! -e "$T/sudo.argv" ]
    # No argv (git, uv) contains the token.
    run grep -q '^ARGV:.*ghp_SECRETTOKEN42' "$T/git.calls"
    [ "$status" -ne 0 ]
    # uv (export and pip install, hence the build backends): neither token nor askpass.
    run grep -q ghp_SECRETTOKEN42 "$T/uv.calls"
    [ "$status" -ne 0 ]
    run grep -c '^ENV_ASKPASS:<none>$' "$T/uv.calls"
    [ "$output" = "2" ]
}

@test "plugins: the git that receives the token does not run as APP_USER and does not inherit the environment" {
    make_token_file "$T/token" "ghp_SECRETTOKEN42"
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\"" \
        "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    stub_install_env
    run install_plugin_packages
    [ "$status" -eq 0 ]
    # No sudo -u APP_USER around git.
    [ ! -e "$T/sudo.argv" ]
    # Empty environment: a variable exported by the caller does not reach git.
    run grep -c '^ENV_SENTINEL:<none>$' "$T/git.calls"
    [ "$output" = "3" ]
}

@test "plugins: the git that receives the token inherits neither exported functions nor readonly variables" {
    make_token_file "$T/token" "ghp_SECRETTOKEN42"
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\"" \
        "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    stub_install_env
    plugin_test_fn() { :; }
    export -f plugin_test_fn
    export PLUGIN_TEST_RO="leaked-readonly"
    readonly PLUGIN_TEST_RO
    run install_plugin_packages
    [ "$status" -eq 0 ]
    run grep -c '^ENV_RO:<none>$' "$T/git.calls"
    [ "$output" = "3" ]
    run grep -c '^ENV_FUNC:no$' "$T/git.calls"
    [ "$output" = "3" ]
    # The token still arrives: the sanitizing does not remove what git needs.
    run grep -c '^ASKPASS_WORKS:yes$' "$T/git.calls"
    [ "$output" = "3" ]
}

@test "plugins: root's temporary directory ignores a foreign TMPDIR" {
    # TMPDIR points to a directory without the sticky bit and owned by someone
    # other than root (in the suite, the user running it): it is not used; the
    # directory goes to /tmp.
    mkdir -m 755 "$T/hostile-tmp"
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\""
    stub_install_env
    TMPDIR="$T/hostile-tmp" run install_plugin_packages
    [ "$status" -eq 0 ]
    dir="$(root_workdir)"
    [ -n "$dir" ]
    [[ "$dir" != "$T/hostile-tmp/"* ]] || false
    [ "$(cd "$(dirname "$dir")" && pwd -P)" = "$(cd /tmp && pwd -P)" ]
    run find "$T/hostile-tmp" -mindepth 1
    [ -z "$output" ]
}

@test "plugins: GIT_ASKPASS lives outside INSTALL_DIR, in a private directory" {
    make_token_file "$T/token" "ghp_SECRETTOKEN42"
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\"" \
        "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    stub_install_env
    run install_plugin_packages
    [ "$status" -eq 0 ]
    run grep '^ENV_ASKPASS:' "$T/git.calls"
    [ "${#lines[@]}" -eq 3 ]
    local l p
    for l in "${lines[@]}"; do
        p="${l#ENV_ASKPASS:}"
        [ "$p" != "<none>" ]
        [[ "$p" == /* ]] || false
        [[ "$p" != "$T/install/"* ]] || false
        [[ "$p" != "$REPO_ROOT/"* ]] || false
    done
    run grep -c '^ASKPASS_MODE:-rwx------$' "$T/git.calls"
    [ "$output" = "3" ]
    run grep -c '^ASKPASS_DIR_MODE:drwx------$' "$T/git.calls"
    [ "$output" = "3" ]
}

@test "plugins: git runs with isolated config (NOSYSTEM, HOME and GLOBAL outside INSTALL_DIR)" {
    local with_token
    for with_token in no yes; do
        rm -f "$T/git.calls" "$T/uv.calls"
        if [ "$with_token" = yes ]; then
            make_token_file "$T/token" "ghp_SECRETTOKEN42"
            write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\"" \
                "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
        else
            write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\""
        fi
        stub_install_env
        run install_plugin_packages
        [ "$status" -eq 0 ]
        run grep -c '^ENV_NOSYSTEM:1$' "$T/git.calls"
        [ "$output" = "3" ]
        run grep -c '^ENV_PROMPT:0$' "$T/git.calls"
        [ "$output" = "3" ]
        run grep -c '^ENV_SENTINEL:<none>$' "$T/git.calls"
        [ "$output" = "3" ]
        local l v
        run grep -E '^ENV_(HOME|GCG):' "$T/git.calls"
        [ "${#lines[@]}" -eq 6 ]
        for l in "${lines[@]}"; do
            v="${l#*:}"
            [ "$v" != "<none>" ]
            [[ "$v" != "$T/install"* ]] || false
            [[ "$v" != "$REPO_ROOT"* ]] || false
        done
        # git's working directory is private and outside INSTALL_DIR.
        run grep -c '^WORKDIR_MODE:drwx------$' "$T/git.calls"
        [ "$output" = "1" ]
        dir="$(root_workdir)"
        [ -n "$dir" ]
        [[ "$dir" != "$T/install"* ]] || false
    done
}

@test "plugins: the tree handed to uv carries neither .git nor the token" {
    make_token_file "$T/token" "ghp_SECRETTOKEN42"
    write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\"" \
        "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
    stub_install_env
    TOKEN_PROBE="ghp_SECRETTOKEN42"
    run install_plugin_packages
    [ "$status" -eq 0 ]
    grep -q '^HANDOFF_PYPROJECT:yes$' "$T/uv.calls"
    grep -q '^HANDOFF_GITDIRS:0$' "$T/uv.calls"
    grep -q '^HANDOFF_TOKEN_HITS:0$' "$T/uv.calls"
    # uv does not receive root's checkout, but a copy.
    handoff="$(grep '^HANDOFF:' "$T/uv.calls" | cut -d: -f2-)"
    [ "$(dirname "$handoff")" != "$(root_workdir)" ]
}

@test "plugins: a failure copying the checkout for APP_USER aborts before uv and removes the temporary directories" {
    local side
    for side in reader writer; do
        rm -f "$T/git.calls" "$T/uv.calls"
        write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\""
        stub_install_env
        if [ "$side" = writer ]; then
            as_app() { if [ "$1" = tar ]; then cat > /dev/null; return 2; fi; "$@"; }
        else
            tar() { command tar "$@" 2>/dev/null; [ "$1" != -C ] || [ "$3" != -cf ] || return 2; }
        fi
        run install_plugin_packages
        [ "$status" -ne 0 ]
        [[ "$output" == *"could not copy"* ]] || false
        [ ! -e "$T/uv.calls" ]
        dir="$(root_workdir)"
        [ -n "$dir" ]
        [ ! -e "$dir" ]
        unset -f tar
    done
}

@test "plugins: temporary directories are removed on success and on failure" {
    local mode
    for mode in ok git uv; do
        rm -f "$T/git.calls" "$T/uv.calls"
        make_token_file "$T/token" "ghp_SECRETTOKEN42"
        write_env "PLUGIN_PACKAGES=\"example-plugin@git+$REPO_URL@$SHA\"" \
            "PLUGIN_GIT_TOKEN_FILE=\"$T/token\""
        stub_install_env
        rm -f "$T/git.rc"; UV_STUB_RC=0
        [ "$mode" = git ] && echo 128 > "$T/git.rc"
        [ "$mode" = uv ] && UV_STUB_RC=1
        run install_plugin_packages
        if [ "$mode" = ok ]; then [ "$status" -eq 0 ]; else [ "$status" -ne 0 ]; fi
        dir="$(root_workdir)"
        [ -n "$dir" ]
        [ ! -e "$dir" ]
        # The askpass existed while git ran (it is not a path that was never
        # created) and no longer exists.
        askpass="$(grep -m1 '^ENV_ASKPASS:' "$T/git.calls" | cut -d: -f2-)"
        [ "$(grep -m1 '^ASKPASS_MODE:' "$T/git.calls")" = "ASKPASS_MODE:-rwx------" ]
        [ ! -e "$askpass" ]
        if [ "$mode" != git ]; then
            handoff="$(grep '^HANDOFF:' "$T/uv.calls" | cut -d: -f2-)"
            [ -n "$handoff" ]
            [ ! -e "$(dirname "$handoff")" ]
        fi
    done
}

# --- askpass -----------------------------------------------------------------

# The askpass does not live in the repo: lib.sh writes it in root's private
# directory of each installation. The tests write it in $T with the same function.
askpass_setup() {
    ASKPASS="$T/askpass/plugin-git-askpass.sh"
    mkdir -p "$T/askpass"
    write_plugin_git_askpass "$ASKPASS"
}

@test "askpass: username for github.com is x-access-token" {
    askpass_setup
    run env ORCASTER_PLUGIN_GIT_TOKEN=tok123 "$ASKPASS" "Username for 'https://github.com': "
    [ "$status" -eq 0 ]
    [ "$output" = "x-access-token" ]
}

@test "askpass: password for github.com is the token" {
    askpass_setup
    run env ORCASTER_PLUGIN_GIT_TOKEN=tok123 "$ASKPASS" "Password for 'https://x-access-token@github.com': "
    [ "$status" -eq 0 ]
    [ "$output" = "tok123" ]
    run env ORCASTER_PLUGIN_GIT_TOKEN=tok123 "$ASKPASS" "Password for 'https://github.com': "
    [ "$output" = "tok123" ]
}

@test "askpass: another host receives nothing" {
    askpass_setup
    for prompt in "Password for 'https://gitlab.example.com': " \
                  "Password for 'https://github.com.evil.example': " \
                  "Username for 'https://example.org': "; do
        run env ORCASTER_PLUGIN_GIT_TOKEN=tok123 "$ASKPASS" "$prompt"
        [ "$status" -eq 0 ]
        [ -z "$output" ]
    done
}

@test "askpass: is executable only by its owner and contains no secrets" {
    askpass_setup
    [ -x "$ASKPASS" ]
    [ "$(ls -ld "$ASKPASS" | cut -c1-10)" = "-rwx------" ]
    run grep -Eq 'gh[pousr]_[A-Za-z0-9]|github_pat_' "$ASKPASS"
    [ "$status" -ne 0 ]
}

# --- check_configured_plugins_installed --------------------------------------

# venv python: stub that treats the names in FAKE_EPS as installed.
stub_venv_python() {
    load_env
    SERVER_DIR="$T/server"
    mkdir -p "$SERVER_DIR/.venv/bin"
    cat > "$SERVER_DIR/.venv/bin/python" <<'S'
#!/usr/bin/env bash
shift 2   # -c <code>
printf '%s\n' "$@" > "$(dirname "$0")/python.argv"
missing=""
for n in "$@"; do
    case " ${FAKE_EPS:-} " in *" $n "*) ;; *) missing="$missing $n" ;; esac
done
[ -z "$missing" ] && exit 0
echo "${missing# }"
exit 3
S
    chmod +x "$SERVER_DIR/.venv/bin/python"
    as_app() { "$@"; }
    export FAKE_EPS
}

@test "plugins check: without ORCASTER_PLUGINS does nothing" {
    write_env
    stub_venv_python
    run check_configured_plugins_installed
    [ "$status" -eq 0 ]
    [ ! -e "$T/server/.venv/bin/python.argv" ]
}

@test "plugins check: ORCASTER_PLUGINS outside SERVER_ENV_PASSTHROUGH aborts" {
    write_env 'ORCASTER_PLUGINS="example-plugin"'
    stub_venv_python
    run check_configured_plugins_installed
    [ "$status" -ne 0 ]
    [[ "$output" == *"SERVER_ENV_PASSTHROUGH"* ]] || false
}

@test "plugins check: SERVER_ENV_PASSTHROUGH separated by tabs or newlines is recognized" {
    write_env 'ORCASTER_PLUGINS="example-plugin"'
    stub_venv_python
    FAKE_EPS="example-plugin"
    SERVER_ENV_PASSTHROUGH=$'FOO\tORCASTER_PLUGINS'
    run check_configured_plugins_installed
    [ "$status" -eq 0 ]
    SERVER_ENV_PASSTHROUGH=$'FOO\n  ORCASTER_PLUGINS\tBAR'
    run check_configured_plugins_installed
    [ "$status" -eq 0 ]
}

@test "plugins check: a name that merely contains ORCASTER_PLUGINS does not count as listed" {
    write_env 'ORCASTER_PLUGINS="example-plugin"'
    stub_venv_python
    FAKE_EPS="example-plugin"
    SERVER_ENV_PASSTHROUGH="MY_ORCASTER_PLUGINS ORCASTER_PLUGINS_EXTRA"
    run check_configured_plugins_installed
    [ "$status" -ne 0 ]
    [[ "$output" == *"SERVER_ENV_PASSTHROUGH"* ]] || false
}

@test "plugins check: missing plugin aborts and names it" {
    write_env 'ORCASTER_PLUGINS="example-plugin, other-plugin"' 'SERVER_ENV_PASSTHROUGH="ORCASTER_PLUGINS"'
    stub_venv_python
    FAKE_EPS="example-plugin"
    run check_configured_plugins_installed
    [ "$status" -ne 0 ]
    [[ "$output" == *"other-plugin"* ]] || false
}

@test "plugins check: present plugins pass and the names go through argv" {
    write_env 'ORCASTER_PLUGINS=" example-plugin ,other-plugin,"' 'SERVER_ENV_PASSTHROUGH="ORCASTER_PLUGINS"'
    stub_venv_python
    FAKE_EPS="example-plugin other-plugin"
    run check_configured_plugins_installed
    [ "$status" -eq 0 ]
    [ "$(cat "$T/server/.venv/bin/python.argv")" = "$(printf 'example-plugin\nother-plugin')" ]
}

@test "plugins check: the real python code detects the missing entry point" {
    command -v python3 >/dev/null || skip "no python3"
    write_env 'ORCASTER_PLUGINS="example-plugin-not-installed"' 'SERVER_ENV_PASSTHROUGH="ORCASTER_PLUGINS"'
    load_env
    SERVER_DIR="$T/server"
    mkdir -p "$SERVER_DIR/.venv/bin"
    ln -s "$(command -v python3)" "$SERVER_DIR/.venv/bin/python"
    as_app() { "$@"; }
    run check_configured_plugins_installed
    [ "$status" -ne 0 ]
    [[ "$output" == *"example-plugin-not-installed"* ]] || false
    [[ "$output" == *"not installed"* ]] || false
}

# --- Static wiring -----------------------------------------------------------

@test "plugins: 03 installs the plugins after sync_server_deps" {
    f="${BATS_TEST_DIRNAME}/../03-app-install.sh"
    sync_line="$(grep -n '^sync_server_deps$' "$f" | cut -d: -f1)"
    install_line="$(grep -n '^install_plugin_packages$' "$f" | cut -d: -f1)"
    check_line="$(grep -n '^check_configured_plugins_installed$' "$f" | cut -d: -f1)"
    [ -n "$sync_line" ]
    [ -n "$install_line" ]
    [ -n "$check_line" ]
    [ "$install_line" -gt "$sync_line" ]
    [ "$check_line" -gt "$install_line" ]
}

@test "plugins: the unit in 05 starts with uv run without --exact" {
    f="${BATS_TEST_DIRNAME}/../05-systemd.sh"
    grep -q '^ExecStart=${UV_BIN} run uvicorn' "$f"
    run grep -n -- '--exact' "$f"
    [ "$status" -ne 0 ]
}
