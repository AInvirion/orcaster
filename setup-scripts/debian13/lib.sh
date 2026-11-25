# shellcheck shell=bash
# =============================================================================
# Helpers shared by the Debian 13 setup scripts.
# Sourced, not executed:  source "$(dirname "$0")/lib.sh"
# =============================================================================

# Paths. SCRIPT_DIR = .../setup-scripts/debian13 ; REPO_ROOT = repo root.
# Both are physical (pwd -P): whatever root sources or runs after the source
# guard resolves without going through symbolic links.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
ENV_FILE="$SCRIPT_DIR/setup.env"
# Path the script was invoked through (may contain links); the source guard
# requires it to match SCRIPT_DIR.
_SCRIPT_DIR_INVOKED="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -L)"

# --- Colored output (falls back to plain text without a TTY) -----------------
if [ -t 1 ]; then
    _C_BLUE="\033[1;34m"; _C_YELLOW="\033[1;33m"; _C_RED="\033[1;31m"
    _C_GREEN="\033[1;32m"; _C_DIM="\033[2m"; _C_RST="\033[0m"
else
    _C_BLUE=""; _C_YELLOW=""; _C_RED=""; _C_GREEN=""; _C_DIM=""; _C_RST=""
fi

log()  { printf "${_C_BLUE}>>${_C_RST} %s\n" "$*"; }
ok()   { printf "${_C_GREEN}OK${_C_RST} %s\n" "$*"; }
warn() { printf "${_C_YELLOW}!!${_C_RST} %s\n" "$*" >&2; }
err()  { printf "${_C_RED}ERROR${_C_RST} %s\n" "$*" >&2; }
die()  { err "$*"; exit 1; }
step() { printf "\n${_C_DIM}── %s ──${_C_RST}\n" "$*"; }

# --- Source tree guard -------------------------------------------------------
# The installer runs as root. If the service user (APP_USER) could write any
# file that root sources or runs, a compromised server process would change it
# and the next installer run would execute it as root. For that reason the
# installer only runs from a root-owned checkout, separate from INSTALL_DIR
# (default /root/orcaster), and 03 copies it with rsync to
# INSTALL_DIR, which does belong to APP_USER. The guard does not repair a tree
# that was already modified: it prevents running from a tree APP_USER can
# modify.
#
# require_root_owned_source runs when this file is sourced, before any other
# effect, and aborts if:
#   - the script was invoked through a path with symbolic links;
#   - SCRIPT_DIR or any directory between SCRIPT_DIR and / (REPO_ROOT included)
#     is not owned by root, or has group or other write, or has an ACL;
#   - lib.sh, run-all.sh, any other *.sh in SCRIPT_DIR, the Makefile in
#     REPO_ROOT or setup.env (if they exist) are not regular files owned by
#     root without group or other write and without an ACL.
# load_env repeats the check on the setup.env it is about to source (also with
# ENV_FILE_OVERRIDE) and then require_source_outside_install_dir requires
# REPO_ROOT and INSTALL_DIR to be disjoint. No effect happens before load_env.
#
# It only acts when the process is root (id -u = 0): without root there is no
# boundary to cross, and need_root re-runs the script with sudo, where the
# guard runs again before any effect.
#
# Owner and mode come from `ls -lnd` (same format on GNU and BSD; a '+' after
# the permissions marks an ACL). `ls` and `id` are resolved through PATH, like
# any other installer command: root's PATH is trusted. The tests prepend stubs
# of `ls` and `id` to PATH, because they cannot create root-owned files; no
# environment variable relaxes the guard.

# Owner (uid) and permissions of a path, without following links. Prints
# "<perms> <uid>" or fails if the path does not exist.
_source_guard_stat() {  # $1=path
    local perms uid
    read -r perms _ uid _ < <(ls -lnd -- "$1" 2>/dev/null) || return 1
    [ -n "$perms" ] && [ -n "$uid" ] || return 1
    printf '%s %s' "$perms" "$uid"
}

# Aborts if $1 is not owned by root, has group or other write, or has an ACL.
# $2 = required type: 'd' (directory) or '-' (regular file).
_source_guard_check() {  # $1=path  $2=type
    local p="$1" want="$2" st perms uid
    st="$(_source_guard_stat "$p")" || \
        die "Source guard: cannot read the owner or mode of $p."
    perms="${st% *}"; uid="${st##* }"
    if [ "${perms:0:1}" != "$want" ]; then
        die "Source guard: $p is not a $([ "$want" = d ] && echo directory || echo 'regular file') (mode $perms; a symbolic link is not accepted). $_SOURCE_GUARD_LAYOUT"
    fi
    if [ "$uid" != 0 ] || [ "${perms:5:1}" = w ] || [ "${perms:8:1}" = w ] \
        || [ "${perms:10:1}" = + ]; then
        die "Source guard: the installer runs as root and does not run code another user can write. $p has owner uid $uid and mode $perms (required: owner root, no group or other write, no ACL). $_SOURCE_GUARD_LAYOUT"
    fi
}

# Chain of directories from $1 (absolute physical path) up to /, $1 included.
_source_guard_check_ancestors() {  # $1=directory
    local d="$1"
    while :; do
        _source_guard_check "$d" d
        [ "$d" = / ] && break
        d="$(dirname "$d")"
    done
}

# A setup.env about to be sourced: regular file owned by root, without group or
# other write, in a directory only root controls. Prints the verified physical
# path: the caller sources that one, not the path as it arrived.
_source_guard_check_env_file() {  # $1=path
    local dir
    dir="$(cd "$(dirname "$1")" && pwd -P)" || \
        die "Source guard: cannot resolve the directory of $1."
    _source_guard_check_ancestors "$dir"
    _source_guard_check "$dir/$(basename "$1")" -
    printf '%s' "$dir/$(basename "$1")"
}

_SOURCE_GUARD_LAYOUT="Supported layout: a root-owned checkout (e.g. /root/orcaster, chown -R root:root and chmod -R go-w), separate from INSTALL_DIR (/opt/orcaster by default), from which the installer is run; 03 copies it to INSTALL_DIR. See setup-scripts/debian13/README.md."

require_root_owned_source() {
    [ "$(id -u)" = 0 ] || return 0
    local f
    if [ "$_SCRIPT_DIR_INVOKED" != "$SCRIPT_DIR" ]; then
        die "Source guard: the installer was invoked through $_SCRIPT_DIR_INVOKED, which goes through symbolic links to $SCRIPT_DIR. Run it through its real path. $_SOURCE_GUARD_LAYOUT"
    fi
    _source_guard_check_ancestors "$SCRIPT_DIR"
    for f in "$SCRIPT_DIR"/*.sh "$REPO_ROOT/Makefile" "$ENV_FILE"; do
        # lib.sh and run-all.sh always exist; the rest only if present.
        [ -e "$f" ] || [ -L "$f" ] || continue
        _source_guard_check "$f" -
    done
    _source_guard_check "$SCRIPT_DIR/lib.sh" -
    _source_guard_check "$SCRIPT_DIR/run-all.sh" -
}

# Aborts if REPO_ROOT and INSTALL_DIR are the same directory or one contains
# the other: 03 leaves INSTALL_DIR owned by APP_USER, so the installer would
# end up inside a tree APP_USER writes. It always runs (with or without root):
# it is about the shape of the deployment. Requires INSTALL_DIR (called by
# load_env).
require_source_outside_install_dir() {
    local src inst
    case "$INSTALL_DIR" in
        /*) ;;
        *) die "INSTALL_DIR must be an absolute path (got: '$INSTALL_DIR')." ;;
    esac
    # No '.' or '..' components: _physical_path does not normalize those that
    # follow a directory that does not exist yet.
    case "/$INSTALL_DIR/" in
        */./*|*/../*) die "INSTALL_DIR cannot have '.' or '..' components (got: '$INSTALL_DIR')." ;;
    esac
    src="${REPO_ROOT%/}/"
    inst="$(_physical_path "$INSTALL_DIR")"
    inst="${inst%/}/"
    case "$src" in "$inst"*)
        die "Source guard: the installer runs from $REPO_ROOT, which is inside INSTALL_DIR ($INSTALL_DIR). INSTALL_DIR is left owned by the service user, so root would run code that user can change. $_SOURCE_GUARD_LAYOUT" ;;
    esac
    case "$inst" in "$src"*)
        die "Source guard: INSTALL_DIR ($INSTALL_DIR) is inside the checkout the installer runs from ($REPO_ROOT). $_SOURCE_GUARD_LAYOUT" ;;
    esac
}

require_root_owned_source

# --- Environment guarantees --------------------------------------------------

# Requires root. If not root, re-runs the calling script via sudo, keeping its args.
need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null 2>&1 || die "Must run as root and 'sudo' was not found."
        log "Elevating with sudo…"
        exec sudo --preserve-env=DEBIAN_FRONTEND bash "$0" "$@"
    fi
}

# Requires `sudo`. The installer drops from root with `sudo -u` to run as
# APP_USER (as_app) and as 'postgres' (psql_su in 01). A minimal Debian 13 does
# not ship it and logging in over SSH directly as root is normal: 00 installs
# it, and every use checks first so that its absence reads as an installer
# error and not as the shell's `sudo: command not found` (exit 127).
require_sudo() {
    command -v sudo >/dev/null 2>&1 \
        || die "'sudo' is missing: the installer uses it to run commands as '${APP_USER:-orcaster}' and as 'postgres'. 00-preflight.sh installs it; on this host install it with 'apt-get install -y sudo' and run the step again."
}

# Checks that we are on Debian 13. It does not abort on other versions, it only
# warns: this allows testing on derivatives while making clear it is outside
# what is supported.
require_debian13() {
    [ -r /etc/os-release ] || die "Cannot find /etc/os-release; is this Debian?"
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${ID:-}" != "debian" ]; then
        warn "ID=${ID:-?} is not 'debian'. The scripts assume Debian 13 (Trixie)."
    elif [ "${VERSION_ID:-}" != "13" ]; then
        warn "VERSION_ID=${VERSION_ID:-?} is not '13'. Tested only on Trixie."
    fi
}

# --- Configuration -----------------------------------------------------------

# Defaults of the body limits. Single source of these numbers in the setup
# scripts: load_env and 06-caddy.sh take them from here. The four body limits
# repeat the defaults of server/orcaster_server/config.py (the app applies each
# number as is, like Caddy). ARTIFACT_MAX_FILE_BYTES does NOT repeat the one in
# the code (50 MiB): the setup raises it to 200 MiB and 03 writes it to
# server/.env.
# shellcheck disable=SC2034  # used by 06-caddy.sh
DEFAULT_MAX_REQUEST_BODY_BYTES=4194304            # 4 MiB
# shellcheck disable=SC2034  # used by 06-caddy.sh
DEFAULT_MAX_OUTPUT_BODY_BYTES=33554432            # 32 MiB
# shellcheck disable=SC2034  # used by 06-caddy.sh
DEFAULT_ARTIFACT_UPLOAD_OVERHEAD_BYTES=8388608    # 8 MiB
# shellcheck disable=SC2034  # used by 06-caddy.sh
DEFAULT_ARTIFACT_MAX_TOTAL_BYTES=209715200        # 200 MiB
DEFAULT_ARTIFACT_MAX_FILE_BYTES=209715200         # 200 MiB

# OPTIONAL body limits: no default in load_env, to tell "declared in setup.env"
# from "absent" (resolve_limit and 03-app-install.sh depend on that
# difference). ARTIFACT_MAX_FILE_BYTES is not here: it is always written to
# server/.env, with its default.
BODY_LIMIT_KEYS=(
    MAX_REQUEST_BODY_BYTES
    MAX_OUTPUT_BODY_BYTES
    ARTIFACT_UPLOAD_OVERHEAD_BYTES
    ARTIFACT_MAX_TOTAL_BYTES
)

# Requires a positive integer of digits only: no sign, spaces or suffixes
# (Caddy would read "4MB" as 4,000,000 B and the app would reject it) and no
# leading zeros (bash takes them as octal in $(( ))). At most 18 digits so that
# the sum of two limits fits in bash's 64-bit integer.
require_positive_int() {  # $1=name  $2=value  $3=origin
    [[ "$2" =~ ^[1-9][0-9]{0,17}$ ]] || \
        die "$1 must be a positive integer of bytes, digits only, without suffixes (MB, MiB) or leading zeros (got: '$2', origin: $3)."
}

# Strips leading and trailing whitespace.
_trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    printf '%s' "${s%"${s##*[![:space:]]}"}"
}

# Value of what follows the "=" in a server/.env line, as python-dotenv (the
# parser the app uses) reads it: between double or single quotes, the content,
# and after the closing quote only a comment is allowed; without quotes, it is
# cut at the first " #" (space and hash). It also strips surrounding
# whitespace. $1 = text after the "=" (without CR).
_dotenv_scalar() {
    local rest val dq sq
    rest="$(_trim "$1")"
    dq='^"([^"]*)"[[:space:]]*(#.*)?$'
    sq="^'([^']*)'[[:space:]]*(#.*)?\$"
    if [[ "$rest" =~ $dq ]]; then
        val="${BASH_REMATCH[1]}"
    elif [[ "$rest" =~ $sq ]]; then
        val="${BASH_REMATCH[1]}"
    else
        val="${rest%%[[:space:]]#*}"
    fi
    _trim "$val"
}

# --- Reading server/.env as root ----------------------------------------------
# server/.env lives inside INSTALL_DIR, which belongs to APP_USER: APP_USER can
# replace it with a symbolic link to a root-owned file (the installer would
# read it and print it in a warn/die) or with a FIFO or device (the read
# blocks). For that reason root never opens it directly:
#   1. guard_server_env rejects, without following links, anything that is not
#      a regular file (type from `ls -lnd`, like the source guard); a FIFO
#      dies here, before any read;
#   2. cat_server_env reads it as APP_USER (as_app), which cannot see anything
#      APP_USER cannot see; the content is parsed from a variable, never with
#      `done < "$SERVER_ENV"`.
# Without root there is no boundary to cross: it is read directly, like
# _source_guard_*. Messages name the path and the key, never a line of the file.
#
# guard_server_env returns 1 if the file does not exist (treated as "no
# value") and dies if it is a symbolic link or not a regular file. It must be
# called in the main shell: a die inside $(...) only kills the subshell.
guard_server_env() {
    local perms
    [ -L "$SERVER_ENV" ] || [ -e "$SERVER_ENV" ] || return 1
    perms="$(_source_guard_stat "$SERVER_ENV")" || \
        die "Cannot read the type of $SERVER_ENV; the installer does not read it without checking it. It must be a regular file owned by $APP_USER inside $SERVER_DIR."
    perms="${perms% *}"
    if [ -L "$SERVER_ENV" ] || [ "${perms:0:1}" != "-" ]; then
        die "$SERVER_ENV is not a regular file (type '${perms:0:1}', mode $perms; a symbolic link, a FIFO or a device is not accepted). The installer runs as root and does not read it: $APP_USER could point it at a root-owned file or block the read. It must be a regular file inside $SERVER_DIR; delete it and run 03-app-install.sh again to regenerate it."
    fi
}

# Content of server/.env, read as APP_USER when the process is root.
cat_server_env() {
    if [ "$(id -u)" -eq 0 ]; then
        as_app cat -- "$SERVER_ENV"
    else
        cat -- "$SERVER_ENV"
    fi
}

# Prints the value of KEY in server/.env and returns 0; returns 1 if there is no
# line (or no file). Reproduces what python-dotenv (the app's parser) accepts as
# an assignment: `KEY=v`, `export KEY=v`, leading indentation, `KEY = v`,
# surrounding quotes, CRLF and a trailing ` # ...` comment. If there are
# several, the last one wins, as in the app. It does not validate the value:
# the caller does that. Explicit > implicit:
#   - a line that mentions the key but is not an assignment (`KEY: v`, a bare
#     `KEY`) is ignored just like the app does, but with a warn;
#   - a value that does not reduce to digits also warns; the die comes from the
#     caller's require_positive_int.
server_env_value() {  # $1=key
    local key="$1" line s val="" found=1 re_assign re_mention content
    guard_server_env || return 1
    content="$(cat_server_env)"
    re_assign="^${key}[[:space:]]*=(.*)\$"
    re_mention="^${key}([^A-Za-z0-9_]|\$)"
    while IFS= read -r line || [ -n "$line" ]; do
        s="$(_trim "${line%$'\r'}")"
        case "$s" in ''|'#'*) continue ;; esac
        if [[ "$s" =~ ^export[[:space:]]+(.*)$ ]]; then
            s="${BASH_REMATCH[1]}"
        fi
        if [[ "$s" =~ $re_assign ]]; then
            val="$(_dotenv_scalar "${BASH_REMATCH[1]}")"
            found=0
        elif [[ "$s" =~ $re_mention ]]; then
            warn "$SERVER_ENV: a line mentions $key but is not a KEY=value assignment; the app ignores it."
        fi
    done <<< "$content"
    [ "$found" -eq 0 ] || return 1
    if [[ ! "$val" =~ ^[0-9]+$ ]]; then
        warn "$SERVER_ENV: $key does not reduce to an integer of digits only."
    fi
    printf '%s\n' "$val"
}

# Resolves the effective value of a body limit and leaves it in variable $1:
#   server/.env  (what the app really reads; on a live host it may carry
#                 manual adjustments)
#   > setup.env  (already loaded by load_env; only if the key is declared)
#   > default    ($3, from DEFAULT_* above)
# Validates the chosen value and aborts if it is not a positive integer: a
# declared but empty value does not fall through to the next level either
# (explicit > implicit).
resolve_limit() {  # $1=output variable  $2=key  $3=default
    local out="$1" key="$2" default="$3" val
    # In the main shell: a die inside server_env_value (in $(...)) would not abort.
    guard_server_env || true
    if [ -e "$SERVER_ENV" ] && [ ! -r "$SERVER_ENV" ]; then
        die "Cannot read $SERVER_ENV to resolve $key."
    fi
    if val="$(server_env_value "$key")"; then
        require_positive_int "$key" "$val" "$SERVER_ENV"
    elif [ -n "${!key+x}" ]; then
        val="${!key}"
        require_positive_int "$key" "$val" "setup.env"
    else
        val="$default"
    fi
    printf -v "$out" '%s' "$val"
}

# Prints the server/.env block with the OPTIONAL body limits declared in
# setup.env (nothing if there are none: the app uses its defaults). It never
# writes a default blindly. The heredoc in 03-app-install.sh interpolates it
# right after the last quota, without a newline of its own: so, if there is
# something to write, it starts with two newlines (closes the previous line and
# leaves a blank one) and, if not, it leaves no extra line.
body_limit_env_block() {
    local key lines=""
    for key in "${BODY_LIMIT_KEYS[@]}"; do
        if [ -n "${!key+x}" ]; then
            lines+="${key}=${!key}"$'\n'
        fi
    done
    [ -n "$lines" ] || return 0
    printf '\n\n# Body limits: only those declared in setup.env; absent ones use\n'
    printf '# the app default. 06-caddy.sh reads the same value from here for the Caddyfile.\n%s' "$lines"
}

# Names that 03-app-install.sh already writes to server/.env on its own: they
# cannot travel through SERVER_ENV_PASSTHROUGH (they would be duplicated and
# the last one would win silently).
SERVER_ENV_RESERVED_KEYS=(
    DATABASE_URL BIND_HOST BIND_PORT ED25519_PRIVATE_KEY_PATH MIGRATE_ON_STARTUP
    WEBAUTHN_RP_ID WEBAUTHN_RP_NAME WEBAUTHN_ORIGIN SESSION_COOKIE_SECURE
    PUBLIC_BASE_URL SSE_ENABLED ARTIFACT_MAX_FILE_BYTES
    ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES TASK_SECRETS_KEYS_PATH
    "${BODY_LIMIT_KEYS[@]}"
)

# Validates SERVER_ENV_PASSTHROUGH: a space-separated list of names of
# setup.env variables that 03-app-install.sh copies as is to server/.env. It is
# the only channel for server settings the installer does not know about (e.g.
# those added by a deployment plugin): the installer names none of them.
# Each name must be an UPPERCASE identifier, not reserved, and declared in
# setup.env (even if empty); the value cannot contain newlines or a single
# quote.
validate_server_env_passthrough() {
    local key reserved
    for key in ${SERVER_ENV_PASSTHROUGH:-}; do
        [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || \
            die "SERVER_ENV_PASSTHROUGH: '$key' is not a valid variable name (UPPERCASE, digits and _)."
        for reserved in "${SERVER_ENV_RESERVED_KEYS[@]}"; do
            [ "$key" != "$reserved" ] || \
                die "SERVER_ENV_PASSTHROUGH: '$key' is already written by the installer from its own setup.env variable; it does not belong in the list."
        done
        [ -n "${!key+x}" ] || \
            die "SERVER_ENV_PASSTHROUGH lists $key but setup.env does not declare it (an empty value counts as declared)."
        case "${!key}" in
            *$'\n'*|*"'"*) die "SERVER_ENV_PASSTHROUGH: the value of $key cannot contain newlines or a single quote." ;;
        esac
    done
}

# server/.env block with the SERVER_ENV_PASSTHROUGH variables, in declared
# order and without duplicates. The value goes in single quotes if it has
# spaces, '#' or '$' (python-dotenv does not expand inside single quotes);
# otherwise as is. Empty if the list is empty.
server_env_passthrough_block() {
    local key value lines="" seen=" "
    for key in ${SERVER_ENV_PASSTHROUGH:-}; do
        case "$seen" in *" $key "*) continue ;; esac
        seen+="$key "
        value="${!key}"
        case "$value" in
            *[[:space:]\#\$]*) value="'$value'" ;;
        esac
        lines+="${key}=${value}"$'\n'
    done
    [ -n "$lines" ] || return 0
    printf '\n\n# SERVER_ENV_PASSTHROUGH variables (setup.env): copied as is, without the\n'
    printf '# installer knowing them.\n%s' "$lines"
}

# Installs an already validated Caddyfile: saves the current one as
# "<dest>.prev" (cp -p, only if it exists) and replaces it with mv (atomic:
# same directory). It does not reload caddy yet; that is
# caddy_activate_or_rollback.  $1=new  $2=dest
install_caddyfile() {
    local new="$1" dest="$2"
    if [ -f "$dest" ]; then
        cp -p "$dest" "${dest}.prev"
    fi
    mv -f "$new" "$dest"
}

# Reloads caddy (reload, and restart if that fails). If both fail it restores
# the Caddyfile from "<dest>.prev" and exits with an error: it never leaves a
# config caddy could not load without saying so. Without ".prev" (there was no
# current Caddyfile) there is nothing to restore and it says so.  $1=dest
caddy_activate_or_rollback() {
    local dest="$1" prev="${1}.prev"
    systemctl enable caddy >/dev/null 2>&1 || true
    if systemctl reload caddy 2>/dev/null || systemctl restart caddy; then
        return 0
    fi
    if [ -f "$prev" ]; then
        mv -f "$prev" "$dest"
        die "caddy neither reloaded nor restarted with the new Caddyfile; restored the previous one at $dest. Check: systemctl status caddy; journalctl -u caddy -n 50."
    fi
    die "caddy neither reloaded nor restarted with the new Caddyfile and there was no previous one to restore ($dest keeps the new one). Check: systemctl status caddy; journalctl -u caddy -n 50."
}

# Sources setup.env and validates the required settings. Applies defaults for the optional ones.
load_env() {
    # ENV_FILE_OVERRIDE allows pointing at another setup.env (used by the bats tests).
    local env_file="${ENV_FILE_OVERRIDE:-$ENV_FILE}"
    [ -f "$env_file" ] || die "Missing $ENV_FILE. Copy setup.env.example and fill it in:
    cp $SCRIPT_DIR/setup.env.example $ENV_FILE && \$EDITOR $ENV_FILE"
    # Root only sources a setup.env that no other user can change.
    if [ "$(id -u)" = 0 ]; then
        env_file="$(_source_guard_check_env_file "$env_file")" || exit 1
    fi
    # shellcheck disable=SC1090
    . "$env_file"

    : "${DOMAIN:?DOMAIN is required in setup.env}"
    : "${ADMIN_EMAIL:?ADMIN_EMAIL is required in setup.env}"
    [ "$DOMAIN" = "orcaster.example.com" ] && \
        die "DOMAIN still has the example value. Edit $ENV_FILE."

    # Defaults for the optional settings.
    DB_NAME="${DB_NAME:-orcaster}"
    DB_USER="${DB_USER:-orcaster}"
    APP_USER="${APP_USER:-orcaster}"
    INSTALL_DIR="${INSTALL_DIR:-/opt/orcaster}"
    require_source_outside_install_dir
    BIND_PORT="${BIND_PORT:-8000}"
    UVICORN_WORKERS="${UVICORN_WORKERS:-2}"
    WEBAUTHN_RP_NAME="${WEBAUTHN_RP_NAME:-Orcaster}"
    # Startup timeout of the systemd unit (TimeoutStartSec). See setup.env.example.
    # Validates that it is an integer: a typo (e.g. "fast") would be interpolated
    # raw into the unit and systemd would silently discard it, reverting the
    # timeout to its default.
    SERVER_START_TIMEOUT="${SERVER_START_TIMEOUT:-120}"
    [[ "$SERVER_START_TIMEOUT" =~ ^[0-9]+$ ]] || \
        die "SERVER_START_TIMEOUT must be an integer of seconds (got: '$SERVER_START_TIMEOUT')."

    # Artifact quotas (see setup.env.example). Default (var ABSENT) = the value
    # 03-app-install.sh writes to server/.env, so a setup.env that does not
    # declare them produces the same .env.
    #
    # Unlike SERVER_START_TIMEOUT, the default applies only if the var is
    # UNDECLARED ('-', not ':-'): a var that is declared but EMPTY does not fall
    # back to the default, it aborts. An empty value (operator typo, or a
    # template that interpolated empty) silently reverting the production quota
    # would be exactly that bug; explicit > implicit, so it fails hard instead
    # of covering up the empty value.
    # They are also validated as integers: they are interpolated raw into the
    # .env and a typo (e.g. "200MB") would be taken by the server as an invalid
    # value instead of the quota.
    ARTIFACT_MAX_FILE_BYTES="${ARTIFACT_MAX_FILE_BYTES-$DEFAULT_ARTIFACT_MAX_FILE_BYTES}"
    require_positive_int ARTIFACT_MAX_FILE_BYTES "$ARTIFACT_MAX_FILE_BYTES" "setup.env"
    ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES="${ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES-21474836480}"
    [[ "$ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES" =~ ^[0-9]+$ ]] || \
        die "ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES must be an integer of bytes (got: '$ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES')."

    # Optional body limits: no default here; if declared (even empty) they must
    # be positive integers. A typo such as "4MB" would be read by Caddy as
    # 4,000,000 B and by the app as invalid.
    local key
    for key in "${BODY_LIMIT_KEYS[@]}"; do
        if [ -n "${!key+x}" ]; then
            require_positive_int "$key" "${!key}" "setup.env"
        fi
    done

    # Task secrets keys path: optional; undeclared or empty = feature disabled.
    # It is declared here so that the heredoc in 03-app-install.sh interpolates
    # it under set -u. A rerun that finds it in server/.env but not here, or
    # with a different value, stops before writing (see
    # check_task_secrets_keys_rerun). The value is passed as is (not validated):
    # the server expands $CREDENTIALS_DIRECTORY and validates the file at
    # startup.
    TASK_SECRETS_KEYS_PATH="${TASK_SECRETS_KEYS_PATH:-}"

    # Extra server variables (see validate_server_env_passthrough): optional
    # list; empty = server/.env is generated as usual.
    SERVER_ENV_PASSTHROUGH="${SERVER_ENV_PASSTHROUGH:-}"
    validate_server_env_passthrough

    # Server plugins installed on top of the locked venv (see
    # install_plugin_packages). Both optional: empty = standard installation.
    PLUGIN_PACKAGES="${PLUGIN_PACKAGES:-}"
    PLUGIN_GIT_TOKEN_FILE="${PLUGIN_GIT_TOKEN_FILE:-}"
    validate_plugin_packages
    validate_plugin_git_token_file

    # Files that exist only on the host inside the server's static directory
    # (see host_only_static_excludes). Optional: empty = none.
    HOST_ONLY_STATIC="${HOST_ONLY_STATIC:-}"
    validate_host_only_static

    # Derived paths, stable across reruns.
    SECRETS_DIR="$INSTALL_DIR/secrets"
    ED25519_PRIV="$SECRETS_DIR/ed25519.pem"
    ED25519_PUB="$SECRETS_DIR/ed25519.pub"
    SERVER_DIR="$INSTALL_DIR/server"
    SERVER_ENV="$SERVER_DIR/.env"
    UV_BIN="/usr/local/bin/uv"
    UV_CACHE="$INSTALL_DIR/.uv-cache"
}

# HOST_ONLY_STATIC: names (or rsync globs) of files the operator placed by hand
# in the server's static directory and that are not in git (robots.txt,
# downloads served from there). Without excluding them, the rsync --delete in 03
# removes them on every deploy. Each entry is a base name, without '/', without
# '..' and not starting with '-' (rsync would read it as an option).
validate_host_only_static() {
    local p
    # set -f: '*.whl' is an rsync pattern, not a glob of this shell.
    set -f
    for p in $HOST_ONLY_STATIC; do
        case "$p" in
            */*|*..*|-*)
                set +f
                die "HOST_ONLY_STATIC: '$p' is not a file name in the static directory (no '/', no '..', no leading '-')." ;;
        esac
    done
    set +f
}

# rsync exclusions for HOST_ONLY_STATIC, for both possible server packages:
# orcaster_server and app (the legacy layout). Excluding both protects the
# files from the --delete in 03 while server-layout.sh migrate-static moves them
# from app/static to orcaster_server/static. Prints one exclusion per line,
# ready for mapfile.
host_only_static_excludes() {
    local p pkg
    set -f
    for p in $HOST_ONLY_STATIC; do
        for pkg in orcaster_server app; do
            printf -- '--exclude=/server/%s/static/%s\n' "$pkg" "$p"
        done
    done
    set +f
}

# Normalizes a value as a .env file reads it: strips surrounding whitespace
# and, if the result is enclosed in single or double quotes of the same kind,
# those quotes too. It expands nothing ($VAR stays literal). Used by
# check_task_secrets_keys_rerun with the already sourced setup.env value (bash
# already removed any comment there); server/.env is read with env_file_value.
normalize_env_value() {  # $1=value
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s' "$value"
}

# Value of KEY in a .env-style text. Reads the last uncommented line that
# assigns KEY and reduces the value with _dotenv_scalar: surrounding
# whitespace, single or double quotes and a trailing ` # ...` comment, which
# python-dotenv discards. Accepts the same forms as python-dotenv, which is what
# the server uses to read server/.env: `KEY=v`, `export KEY=v`, `KEY = v` and
# `export KEY = v` (spaces or tabs). A key that only shares a prefix
# (`KEY_OTHER=`), a commented line or an `export KEY` without `=` do not count.
# An empty value gives an empty string (no value). Without a file, an empty
# string.
env_text_value() {  # $1=content  $2=key
    local text="$1" key="$2" line trimmed rest value=""
    while IFS= read -r line || [ -n "$line" ]; do
        trimmed="${line#"${line%%[![:space:]]*}"}"
        case "$trimmed" in
            export[[:space:]]*)
                trimmed="${trimmed#export}"
                trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
                ;;
        esac
        case "$trimmed" in
            "$key"*)
                rest="${trimmed#"$key"}"
                rest="${rest#"${rest%%[![:space:]]*}"}"
                case "$rest" in
                    =*) value="${rest#=}" ;;
                esac
                ;;
        esac
    done <<< "$text"
    _dotenv_scalar "${value%$'\r'}"
}

env_file_value() {  # $1=file  $2=key
    local file="$1" key="$2"
    [ -f "$file" ] || return 0
    env_text_value "$(cat -- "$file")" "$key"
}

# Rerun of 03-app-install.sh: server/.env is rewritten from setup.env on every
# run. It fails closed in two cases: it aborts before writing anything and
# server/.env stays intact.
#   1. server/.env already has TASK_SECRETS_KEYS_PATH and setup.env does not
#      declare it: the rewrite would delete it and tasks with stored secrets
#      would stop being delivered.
#   2. Both have it and the values differ: the rewrite would make the server
#      load a different keys file on its next restart, and the stored secrets,
#      encrypted with the keys of the current file, might no longer decrypt.
#      setup.env used to win silently.
# With equal values, or with the key only in setup.env or in neither, it
# continues. The comparison is of literal strings, without reading or expanding
# anything, after stripping surrounding whitespace and quotes on both sides;
# from server/.env a trailing ` # ...` comment is also discarded, as
# python-dotenv does. setup.env is sourced (single quotes leave
# $CREDENTIALS_DIRECTORY literal in the variable) and the heredoc in
# 03-app-install.sh writes it as is, so the variable's value is already the
# literal that would end up in server/.env. A line removed from server/.env
# counts as no previous value and does not trigger the guard.
# Prints the two paths, never the content of the keys file.
# Requires load_env to have run (SERVER_ENV, TASK_SECRETS_KEYS_PATH).
check_task_secrets_keys_rerun() {
    local existing declared setup_env_file
    setup_env_file="${ENV_FILE_OVERRIDE:-$ENV_FILE}"
    existing=""
    if guard_server_env; then
        existing="$(env_text_value "$(cat_server_env)" TASK_SECRETS_KEYS_PATH)"
    fi
    declared="$(normalize_env_value "${TASK_SECRETS_KEYS_PATH:-}")"
    [ -n "$existing" ] || return 0
    if [ -z "$declared" ]; then
        die "$SERVER_ENV sets TASK_SECRETS_KEYS_PATH=$existing, but $setup_env_file does not declare it.
    Rerunning would regenerate server/.env without it: task secrets would be disabled and tasks that declare stored secret names would stop being delivered.
    server/.env was not modified. Declare the same value in $setup_env_file, in single quotes when it uses \$CREDENTIALS_DIRECTORY, e.g.:
        TASK_SECRETS_KEYS_PATH='\$CREDENTIALS_DIRECTORY/task-secrets-keys'
    then run this script again."
    fi
    if [ "$existing" != "$declared" ]; then
        die "$SERVER_ENV sets TASK_SECRETS_KEYS_PATH=$existing, but $setup_env_file sets a different value: TASK_SECRETS_KEYS_PATH=$declared.
    Rerunning would regenerate server/.env with the setup.env value: the server would load a different keys file on its next restart, and stored task secrets encrypted with the keys of the current file may no longer decrypt.
    server/.env was not modified. Make both values equal and run this script again:
      - To keep the current keys file, set the server/.env value in $setup_env_file, in single quotes:
            TASK_SECRETS_KEYS_PATH='$existing'
      - To move to another keys file on purpose, first make sure that file holds the keys that encrypted the stored secrets, then set the same new value in both $setup_env_file and $SERVER_ENV."
    fi
}

# Persists a KEY="value" pair back into setup.env (idempotent). Without sed:
# removes the previous line for that key (if any) and rewrites it at the end
# with printf (literal, without interpreting metacharacters in the value). Used
# by 01-postgresql.sh to save the autogenerated password.
persist_env() {
    local key="$1" val="$2" tmp
    tmp="$(mktemp)"
    if [ -f "$ENV_FILE" ]; then
        grep -v "^${key}=" "$ENV_FILE" > "$tmp" || true
    fi
    printf '%s="%s"\n' "$key" "$val" >> "$tmp"
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
}

# Runs a command as APP_USER with the minimal environment uv needs.
# It prepends `env` to set HOME/UV_CACHE_DIR/PATH reliably: sudo may reset
# those variables (secure_path, always_set_home), `env` does not.
#
# The `cd /` is not cosmetic: sudo inherits the caller's cwd, and if root
# re-runs the suite from /root (mode 700) the subcommand dies with
# "cannot chdir" before it runs. With `set -euo pipefail` that aborts the
# deploy on a healthy machine, i.e. a false negative. The subshell scopes it to
# the invocation and leaves the calling script's cwd untouched. Twin of the
# guard in as_worker in setup-scripts/debian13-worker/lib.sh.
as_app() {
    require_sudo
    ( cd / && sudo -u "$APP_USER" env \
        HOME="$INSTALL_DIR" \
        UV_CACHE_DIR="$UV_CACHE" \
        PATH="/usr/local/bin:/usr/bin:/bin" \
        "$@" )
}

# Resolves the server dependencies with `uv sync --frozen`.
# uv.lock is a mandatory deploy artifact: it is committed at server/uv.lock and
# the rsync in 03-app-install.sh does not exclude it. If it is missing, the
# tree that reached the host is broken and the deploy stops before invoking uv.
# There is no fallback to `uv sync` without --frozen: it would re-resolve
# against the index and leave on the host a dependency tree nobody reviewed or
# committed.
sync_server_deps() {
    if [ ! -f "$SERVER_DIR/uv.lock" ]; then
        die "$SERVER_DIR/uv.lock does not exist. The deploy does not resolve dependencies without the committed lock; check that the tree copied to the host includes server/uv.lock."
    fi
    as_app "$UV_BIN" sync --project "$SERVER_DIR" --frozen
}

# --- Server plugins ----------------------------------------------------------
#
# `uv sync --frozen` (sync_server_deps) is exact: it removes from the venv every
# package that is not in server/uv.lock, so a hand-installed plugin disappears
# on every 03 run. That is why plugins are declared in setup.env
# (PLUGIN_PACKAGES) and 03 reinstalls them right after the sync, with the lock
# versions as constraints. `uv run` (systemd unit, 04, 09) syncs inexactly and
# does not remove them. A manual `uv sync --frozen` on the host does remove
# them: just run 03 again.

# Owner required of the PLUGIN_GIT_TOKEN_FILE file (numeric uid). Set when
# lib.sh is sourced, not inherited from the environment; only the tests
# reassign it after sourcing, because they cannot create root-owned files.
PLUGIN_TOKEN_FILE_OWNER_UID=0

# PATH of the git that fetches the plugins. That git runs as root with an
# empty environment: it only sees this PATH and the variables _plugin_git sets.
# Set when lib.sh is sourced; only the tests reassign it, to prepend their stubs.
PLUGIN_GIT_PATH="/usr/local/bin:/usr/bin:/bin"

# Accepted forms of a PLUGIN_PACKAGES requirement. All of them pin the content:
#   - git: name@git+https://host/org/repo@<sha>. The ref is the full commit SHA
#     (40 lowercase hex digits). A tag or a branch can move on the remote and
#     each run would install different code, so they are not accepted. https
#     only. Groups: 1=name, 2=URL, 3=SHA.
#   - index: name==<exact version>, with optional extras (name[x]==1.0).
#     Without `==` the name would be resolved against the index on every run
#     (for a private plugin, a way for someone to publish that name on PyPI).
#   - local wheel: absolute path ending in .whl.
PLUGIN_GIT_SPEC_RE='^([A-Za-z0-9][A-Za-z0-9._-]*)@git\+(https://[^@#?[:space:]]+)@([0-9a-f]{40})$'
PLUGIN_INDEX_SPEC_RE='^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9][A-Za-z0-9._+!-]*$'
PLUGIN_WHEEL_SPEC_RE='^/[^@#?[:space:]]*\.whl$'

# Splits PLUGIN_PACKAGES into the global array PLUGIN_SPECS (spaces, tabs or
# newlines), without glob expansion.
_plugin_specs() {
    PLUGIN_SPECS=()
    [ -n "${PLUGIN_PACKAGES:-}" ] || return 0
    IFS=$' \t\n' read -r -d '' -a PLUGIN_SPECS <<< "$PLUGIN_PACKAGES" || true
}

# The requirement as shown in an error: without the URL's query string or
# userinfo, so that a token written by mistake in setup.env does not end up in
# the installer logs.
_plugin_spec_shown() {  # $1=requirement
    local s="$1" head rest authority
    if [[ "$s" == *\?* ]]; then
        s="${s%%\?*}?<omitted>"
    fi
    if [[ "$s" == *://* ]]; then
        head="${s%%://*}://"
        rest="${s#*://}"
        authority="${rest%%/*}"
        if [[ "$authority" == *@* ]]; then
            s="${head}<omitted>@${authority##*@}${rest#"$authority"}"
        fi
    fi
    printf '%s' "$s"
}

# Diagnosis of a git requirement (`git+` in any position) that does not match
# PLUGIN_GIT_SPEC_RE: says what is missing. Always ends with die.
# $1=requirement  $2=requirement to display
_plugin_git_spec_die() {
    local spec="$1" shown="$2" form='name@git+https://host/org/repo@<40-hex-sha>'
    local named_re='^[A-Za-z0-9][A-Za-z0-9._-]*@git\+https://[^@#?[:space:]]+'
    if [[ "$spec" == *git+http://* ]]; then
        die "PLUGIN_PACKAGES: '$shown' uses git+http://: the code would be fetched unencrypted. Only git+https:// is accepted. Form: $form."
    fi
    if [[ "$spec" != *git+https://* ]]; then
        die "PLUGIN_PACKAGES: '$shown' uses a git scheme that is not accepted. Only git+https://. Form: $form."
    fi
    if [[ "$spec" == git+* ]]; then
        die "PLUGIN_PACKAGES: '$shown' has no package name ('name@' is missing before git+https://). Form: $form."
    fi
    if [[ "$spec" =~ ${named_re}$ ]]; then
        die "PLUGIN_PACKAGES: '$shown' pins no ref ('@<sha>' is missing at the end). Form: $form."
    fi
    if [[ "$spec" =~ ${named_re}@(.*)$ ]]; then
        die "PLUGIN_PACKAGES: '$shown' pins the ref '${BASH_REMATCH[1]}', which is not a full commit SHA (40 lowercase hex digits). A tag or a branch can move on the remote: pin the commit SHA. Form: $form."
    fi
    die "PLUGIN_PACKAGES: '$shown' does not have the form $form."
}

# Validates PLUGIN_PACKAGES: pip requirements separated by spaces, each
# without internal spaces. Only three forms are accepted, all with pinned
# content (see PLUGIN_GIT_SPEC_RE): `name@git+https://host/org/repo@<40-hex-sha>`,
# `name==1.2.3` and the absolute path of a wheel (`/path/x.whl`). It fails
# closed, naming the requirement (without query string or userinfo), on:
#   - an item starting with '-': uv would take it as an option;
#   - a stray '@', or at the start or end of an item: it is the PEP 508 form
#     `name @ url` written with spaces, which the space splitting breaks into
#     three and which is not accepted (write `name@url`). It is checked before
#     the rest, so that the error states the cause;
#   - a URL with credentials (`scheme://user[:password]@host/...`) or with a
#     query string (`?token=...`): the secret would end up in setup.env and in
#     the uv logs. For private repositories use PLUGIN_GIT_TOKEN_FILE;
#   - a git requirement that is not name@git+https://...@<40-hex sha>: git+http,
#     another scheme, no name, no ref, or a ref that is a tag or a branch;
#   - any other requirement that is not `name==<exact version>` or an absolute
#     wheel (name without version, `>=`, `~=`, `==1.*`, direct URLs...).
validate_plugin_packages() {
    local spec shown rest authority
    _plugin_specs
    # First the PEP 508 form `name @ url` (with spaces): the space splitting
    # breaks it into `name`, `@` and `url`, and `name` alone would be rejected
    # for having no version, with a message that does not state the cause. It is
    # not accepted: the setup.env format is `name@url`, without spaces.
    for spec in ${PLUGIN_SPECS[@]+"${PLUGIN_SPECS[@]}"}; do
        case "$spec" in
            @|@*|*@) die "PLUGIN_PACKAGES: '$(_plugin_spec_shown "$spec")' has a stray '@' or one at an edge: it looks like the PEP 508 form 'name @ url' with spaces, which is not accepted (requirements are separated by spaces). Write 'name@url' without spaces: name@git+https://host/org/repo@<40-hex-sha>." ;;
        esac
    done
    for spec in ${PLUGIN_SPECS[@]+"${PLUGIN_SPECS[@]}"}; do
        shown="$(_plugin_spec_shown "$spec")"
        case "$spec" in
            -*) die "PLUGIN_PACKAGES: '$shown' starts with '-'; only package requirements are accepted, not uv options." ;;
        esac
        # A git scheme other than https is rejected before looking at the userinfo:
        # `git+ssh://git@host/...` carries no credentials, it carries another scheme.
        case "$spec" in
            git+*|*@git+*) [[ "$spec" == *git+https://* ]] || _plugin_git_spec_die "$spec" "$shown" ;;
        esac
        if [[ "$spec" == *://* ]]; then
            rest="${spec#*://}"
            authority="${rest%%/*}"
            case "$authority" in
                *@*) die "PLUGIN_PACKAGES: '$shown' carries credentials in the URL (user@host). They are not accepted: for a private repository declare PLUGIN_GIT_TOKEN_FILE." ;;
            esac
        fi
        case "$spec" in
            *\?*) die "PLUGIN_PACKAGES: '$shown' carries a query string in the URL (e.g. ?token=...). It is not accepted: uv prints the URL and the value would end up in setup.env and in the logs. For a private repository declare PLUGIN_GIT_TOKEN_FILE." ;;
        esac
        case "$spec" in
            git+*|*@git+*)
                [[ "$spec" =~ $PLUGIN_GIT_SPEC_RE ]] || _plugin_git_spec_die "$spec" "$shown"
                ;;
            *)
                [[ "$spec" =~ $PLUGIN_INDEX_SPEC_RE ]] && continue
                [[ "$spec" =~ $PLUGIN_WHEEL_SPEC_RE ]] && continue
                case "$spec" in
                    */*|*:*) die "PLUGIN_PACKAGES: '$shown' is not an accepted requirement. Forms: name@git+https://host/org/repo@<40-hex-sha>, name==<exact version> or the absolute path of a wheel (/path/x.whl)." ;;
                    *) die "PLUGIN_PACKAGES: '$shown' does not pin an exact version. Write name==X.Y.Z: a name without '==' (or with >=, ~=, '*') would be resolved against the index on every run." ;;
                esac
                ;;
        esac
    done
}

# Physical directory of a path (resolves links in the directory); if it does not
# exist, the path as is.
_physical_dir() {  # $1=directory
    if [ -d "$1" ]; then
        (cd "$1" && pwd -P)
    else
        printf '%s' "$1"
    fi
}

# Physical path of $1 even if it does not exist yet: resolves the deepest
# existing ancestor (links and '..' included) and appends the rest.
_physical_path() {  # $1=absolute path
    local p="$1" rest=""
    while [ ! -d "$p" ]; do
        rest="/$(basename "$p")$rest"
        p="$(dirname "$p")"
    done
    p="$(cd "$p" && pwd -P)"
    printf '%s' "${p%/}$rest"
}

# Validates PLUGIN_GIT_TOKEN_FILE when declared: absolute path to a regular
# file (not a link), outside REPO_ROOT and INSTALL_DIR, owned by root
# (PLUGIN_TOKEN_FILE_OWNER_UID), without group or other permissions, with a
# single non-empty line and no spaces. It never prints the content.
validate_plugin_git_token_file() {
    local f="${PLUGIN_GIT_TOKEN_FILE:-}" dir root perms uid lines first
    [ -n "$f" ] || return 0
    [ -n "${PLUGIN_PACKAGES:-}" ] || \
        warn "PLUGIN_GIT_TOKEN_FILE is declared but PLUGIN_PACKAGES is empty: the token is not used."
    case "$f" in
        /*) ;;
        *) die "PLUGIN_GIT_TOKEN_FILE must be an absolute path (got: '$f')." ;;
    esac
    [ ! -L "$f" ] || die "PLUGIN_GIT_TOKEN_FILE ($f) is a symbolic link; point at the real file."
    [ -f "$f" ] || die "PLUGIN_GIT_TOKEN_FILE ($f) does not exist or is not a regular file."
    dir="$(_physical_dir "$(dirname "$f")")/"
    for root in "$REPO_ROOT" "$INSTALL_DIR"; do
        root="$(_physical_dir "$root")/"
        case "$dir" in
            "$root"*) die "PLUGIN_GIT_TOKEN_FILE ($f) is inside $root: the rsync in 03 and the chown to APP_USER would reach it. Move it outside (e.g. /etc/<service>/plugin-git-token)." ;;
        esac
    done
    read -r perms _ uid _ < <(ls -ln "$f")
    [ "$uid" = "$PLUGIN_TOKEN_FILE_OWNER_UID" ] || \
        die "PLUGIN_GIT_TOKEN_FILE ($f) must be owned by root (uid $PLUGIN_TOKEN_FILE_OWNER_UID); its owner is uid $uid. Fix with: chown root:root $f"
    case "${perms:4:6}" in
        ------) ;;
        *) die "PLUGIN_GIT_TOKEN_FILE ($f) has group or other permissions ($perms). Fix with: chmod 600 $f" ;;
    esac
    lines="$(grep -c '' "$f" || true)"
    IFS= read -r first < "$f" || true
    first="${first%$'\r'}"
    if [ "$lines" != "1" ] || [ -z "$first" ] || [[ "$first" =~ [[:space:]] ]]; then
        die "PLUGIN_GIT_TOKEN_FILE ($f) must have exactly one non-empty line, without spaces, holding the token."
    fi
}

# Writes to $1 the git askpass for private plugin URLs, mode 700. It contains
# no secret: the token arrives in ORCASTER_PLUGIN_GIT_TOKEN, which only
# _plugin_git sets. It lives in lib.sh and not in a separate file: lib.sh is
# the only thing the source guard verifies before sourcing. It is written to
# root's private directory of each installation and deleted with it.
#
# git invokes it with the prompt text as its only argument, e.g.
#   Username for 'https://github.com':
#   Password for 'https://x-access-token@github.com':
# It only answers for https://github.com. For any other host it prints nothing
# and exits 0: git fails the authentication and the token never reaches another
# host.
write_plugin_git_askpass() {  # $1=destination path
    ( umask 077 && cat > "$1" ) <<'ASKPASS'
#!/bin/sh
# git askpass for server plugins (written by lib.sh).
case "${1:-}" in
    "Username for 'https://github.com'"*)
        printf '%s\n' "x-access-token"
        ;;
    "Password for 'https://github.com'"*|"Password for 'https://x-access-token@github.com'"*)
        printf '%s\n' "${ORCASTER_PLUGIN_GIT_TOKEN:-}"
        ;;
esac
exit 0
ASKPASS
    chmod 700 "$1"
}

# Runs git as root to fetch a plugin's code into $_PLUGIN_ROOT_TMP.
# Nothing git reads or runs is under APP_USER's control:
#   - empty environment, with PATH=$PLUGIN_GIT_PATH: inside a subshell all
#     exported variables are removed (readonly ones, which `unset` cannot
#     remove, lose the export attribute; an `unset` that exposes a variable
#     from an outer scope is repeated) and all functions stop being exported
#     (BASH_FUNC_*). If anything is still exported after that, git does not
#     run. `env -i` does not work because the token would have to go in its
#     argv;
#   - HOME is an empty root-owned directory inside $_PLUGIN_ROOT_TMP,
#     GIT_CONFIG_GLOBAL=/dev/null and GIT_CONFIG_NOSYSTEM=1: no git config
#     (and therefore no core.askPass or credential.helper) gets in;
#   - GIT_TERMINAL_PROMPT=0 makes git fail instead of asking and an empty
#     credential.helper prevents git from storing the credential.
# With PLUGIN_GIT_TOKEN_FILE, the token is read here and travels in
# ORCASTER_PLUGIN_GIT_TOKEN only in this git's environment, which asks the
# askpass in $_PLUGIN_ROOT_TMP (root-owned, mode 700) for it: never in argv,
# nor in a file, nor in the output, and never in uv's environment. Without a
# token, git runs anyway but without askpass.
_plugin_git() {  # $@ = git arguments
    local token=""
    if [ -n "${PLUGIN_GIT_TOKEN_FILE:-}" ]; then
        IFS= read -r token < "$PLUGIN_GIT_TOKEN_FILE" || true
        token="${token%$'\r'}"
        if [ -z "$token" ]; then
            err "PLUGIN_GIT_TOKEN_FILE ($PLUGIN_GIT_TOKEN_FILE) is empty."
            return 1
        fi
    fi
    (
        cd / || exit 1
        local v
        # An `unset` can expose another exported variable of the same name
        # (one from an outer scope, e.g. with `VAR=x function`): it is repeated
        # until none is left, with a cap on rounds.
        for _ in 1 2 3 4 5 6 7 8; do
            [ -n "$(compgen -e)" ] || break
            for v in $(compgen -e); do
                unset "$v" 2>/dev/null || export -n "${v?}"
            done
        done
        for v in $(compgen -A function); do
            declare +x -f "$v"
        done
        if [ -n "$(compgen -e)" ] || [ -n "$(declare -Fx)" ]; then
            err "Plugins: could not empty git's environment (exported variables or functions remain)."
            exit 1
        fi
        export PATH="$PLUGIN_GIT_PATH" \
            HOME="$_PLUGIN_ROOT_TMP/home" \
            GIT_CONFIG_GLOBAL=/dev/null \
            GIT_CONFIG_NOSYSTEM=1 \
            GIT_TERMINAL_PROMPT=0 \
            LC_ALL=C
        if [ -n "$token" ]; then
            export GIT_ASKPASS="$_PLUGIN_ROOT_TMP/plugin-git-askpass.sh"
            export ORCASTER_PLUGIN_GIT_TOKEN="$token"
        fi
        exec git -c credential.helper= "$@"
    )
}

# Temporary directories of install_plugin_packages: root's (git checkouts,
# askpass, git's HOME) and APP_USER's (copies for uv, constraints).
_PLUGIN_ROOT_TMP=""
_PLUGIN_APP_TMP=""

# Deletes the temporary directories of install_plugin_packages.
_plugin_tmp_cleanup() {
    [ -z "$_PLUGIN_ROOT_TMP" ] || rm -rf "$_PLUGIN_ROOT_TMP"
    [ -z "$_PLUGIN_APP_TMP" ] || rm -rf "$_PLUGIN_APP_TMP"
    _PLUGIN_ROOT_TMP=""
    _PLUGIN_APP_TMP=""
}

# Aborts install_plugin_packages: deletes the temporary directories and calls die.
_plugin_fail() {
    _plugin_tmp_cleanup
    die "$@"
}

# Installs the PLUGIN_PACKAGES plugins into the server venv, after
# sync_server_deps, in three phases:
#   1. Each `name@git+https://...@<sha>` requirement is fetched with git, as
#      root, into a root-owned temporary directory, mode 700 and outside
#      INSTALL_DIR (git init + fetch --depth 1 of the SHA + checkout of
#      FETCH_HEAD; see _plugin_git). The URL is passed to fetch without storing
#      it as a remote. It is the only phase that sees the token. When done, the
#      checkout's .git is deleted.
#   2. Each checkout (without .git) is copied, with tar, into a temporary
#      directory created by APP_USER; the files of the copy belong to APP_USER.
#      APP_USER never writes into root's directory.
#   3. `uv pip install` as APP_USER, without the token in the environment, with
#      those requirements rewritten to `name@file://<copy>` (same order) and the
#      others unchanged. The server/uv.lock versions (`uv export --frozen`) go in
#      as --constraint: if a plugin requires another version of a locked
#      dependency, the installation fails instead of moving the lock.
# It fails closed: any error aborts the deploy naming the step that failed. The
# temporary directories are always deleted: every failure goes through
# _plugin_fail and, if the caller has no EXIT trap of its own, a temporary one
# deletes them on an unexpected exit too (removed on return). A foreign EXIT
# trap is not replaced: inside a subshell, `trap -p` shows the parent's and
# restoring it there would run it twice. Without PLUGIN_PACKAGES it does
# nothing. It validates PLUGIN_PACKAGES again (load_env already does): what
# reaches git and uv never depends on the caller having gone through load_env
# first.
install_plugin_packages() {
    local constraints spec name url ref src dest tmp_phys root n=0 rc=0 prev_trap
    local -a cmd final=() copy_rc=()
    local git_re="$PLUGIN_GIT_SPEC_RE"
    local missing_note="the venv keeps the server/uv.lock set and the server will not start if ORCASTER_PLUGINS names a missing plugin."
    validate_plugin_packages
    _plugin_specs
    if [ "${#PLUGIN_SPECS[@]}" -eq 0 ]; then
        log "PLUGIN_PACKAGES is empty: no plugins declared."
        return 0
    fi
    prev_trap="$(trap -p EXIT)"
    [ -n "$prev_trap" ] || trap _plugin_tmp_cleanup EXIT
    # mktemp -d creates the directory with mode 700; root creates it, its owner.
    # It is created in /tmp (root-owned, with sticky bit) and not in $TMPDIR: in
    # a TMPDIR that APP_USER could write without a sticky bit, it could rename
    # the just-created directory and put its own in place before the askpass is
    # written.
    _PLUGIN_ROOT_TMP="$(mktemp -d /tmp/orcaster-plugins.XXXXXXXXXX)" || \
        _plugin_fail "Plugins: could not create a root temporary directory. No plugin was installed; $missing_note"
    # If INSTALL_DIR or the repo were in /tmp, APP_USER could reach root's
    # directory: it is rejected.
    tmp_phys="$(_physical_dir "$_PLUGIN_ROOT_TMP")/"
    for root in "$REPO_ROOT" "$INSTALL_DIR"; do
        root="$(_physical_dir "$root")/"
        case "$tmp_phys" in
            "$root"*) _plugin_fail "Plugins: root's temporary directory ($_PLUGIN_ROOT_TMP) is inside $root. No plugin was installed; $missing_note" ;;
        esac
    done
    if ! mkdir -m 700 "$_PLUGIN_ROOT_TMP/home"; then
        _plugin_fail "Plugins: could not prepare root's temporary directory. No plugin was installed; $missing_note"
    fi
    if [ -n "${PLUGIN_GIT_TOKEN_FILE:-}" ] \
        && ! write_plugin_git_askpass "$_PLUGIN_ROOT_TMP/plugin-git-askpass.sh"; then
        _plugin_fail "Plugins: could not write the git askpass. No plugin was installed; $missing_note"
    fi
    # APP_USER's directory (copies for uv and constraints) is created by
    # APP_USER, also with mode 700.
    _PLUGIN_APP_TMP="$(as_app mktemp -d)" || \
        _plugin_fail "Plugins: could not create a temporary directory as $APP_USER. No plugin was installed; $missing_note"

    for spec in ${PLUGIN_SPECS[@]+"${PLUGIN_SPECS[@]}"}; do
        if [[ "$spec" =~ $git_re ]]; then
            name="${BASH_REMATCH[1]}"; url="${BASH_REMATCH[2]}"; ref="${BASH_REMATCH[3]}"
            n=$((n + 1))
            src="$_PLUGIN_ROOT_TMP/src-$n"
            dest="$_PLUGIN_APP_TMP/src-$n"
            log "Plugins: fetching $name ($ref) with git…"
            if ! _plugin_git init --quiet "$src" \
                || ! _plugin_git -C "$src" fetch --quiet --depth 1 --no-tags "$url" "$ref" \
                || ! _plugin_git -C "$src" checkout --quiet --detach FETCH_HEAD; then
                _plugin_fail "Plugins: git failed fetching $name ($url, ref $ref). Check the ref and, if the repository requires authentication, set PLUGIN_GIT_TOKEN_FILE. No plugin was installed; $missing_note"
            fi
            # Without .git, the tree APP_USER receives carries no config, objects
            # or anything git stored during the fetch. The copy is written by
            # APP_USER (tar as root-side reader, tar as APP_USER-side writer):
            # the files are theirs and it never touches root's directory. Both
            # sides of the pipe are checked, with or without pipefail.
            if ! rm -rf "$src/.git" || ! as_app mkdir "$dest"; then
                _plugin_fail "Plugins: could not prepare the copy of $name for $APP_USER. No plugin was installed; $missing_note"
            fi
            # Inside `{ …; } || true`, set -e does not cut in before PIPESTATUS is read.
            { tar -C "$src" -cf - . | as_app tar -C "$dest" -xf -
              copy_rc=("${PIPESTATUS[@]}"); } || true
            if [ "${copy_rc[0]:-1}" -ne 0 ] || [ "${copy_rc[1]:-1}" -ne 0 ]; then
                _plugin_fail "Plugins: could not copy the code of $name into $APP_USER's directory. No plugin was installed; $missing_note"
            fi
            final+=("$name@file://$dest")
        else
            final+=("$spec")
        fi
    done
    # The checkouts were already copied: root's directory is not needed for uv.
    rm -rf "$_PLUGIN_ROOT_TMP"
    _PLUGIN_ROOT_TMP=""

    constraints="$_PLUGIN_APP_TMP/constraints.txt"
    log "Plugins: exporting the server/uv.lock versions as constraints…"
    if ! as_app "$UV_BIN" export --project "$SERVER_DIR" --frozen --no-hashes \
            --no-emit-project --quiet -o "$constraints"; then
        _plugin_fail "Plugins: 'uv export --frozen' failed (constraints from server/uv.lock). No plugin was installed; $missing_note"
    fi
    log "Plugins: installing ${#final[@]} PLUGIN_PACKAGES requirement(s)…"
    cmd=("$UV_BIN" pip install --python "$SERVER_DIR/.venv/bin/python"
        --constraint "$constraints" "${final[@]}")
    as_app "${cmd[@]}" || rc=$?
    [ "$rc" -eq 0 ] || \
        _plugin_fail "Plugins: 'uv pip install' of PLUGIN_PACKAGES failed (rc=$rc). Check uv's output: a plugin that requires another version of a server/uv.lock dependency is not installed. Since uv resolves before installing, $missing_note"
    _plugin_tmp_cleanup
    [ -n "$prev_trap" ] || trap - EXIT
}

# Checks, before restarting the server, that each name in ORCASTER_PLUGINS
# (setup.env, comma-separated) exists as an entry point of the
# orcaster.plugins group in the venv. It anticipates the rejection the server
# does at startup (which is kept). It also aborts if ORCASTER_PLUGINS is
# declared but not listed in SERVER_ENV_PASSTHROUGH: it would not reach
# server/.env and the server would silently ignore it. The names travel as
# argv, never interpolated into the Python code.
check_configured_plugins_installed() {
    local raw="${ORCASTER_PLUGINS:-}" part key rc=0 missing listed=0
    local -a parts=() names=() passthrough=()
    IFS=',' read -r -a parts <<< "$raw" || true
    for part in ${parts[@]+"${parts[@]}"}; do
        part="$(_trim "$part")"
        [ -z "$part" ] || names+=("$part")
    done
    [ "${#names[@]}" -gt 0 ] || return 0
    # The list is split like the rest of the installer (any space, tab or
    # newline), without glob expansion.
    IFS=$' \t\n' read -r -d '' -a passthrough <<< "${SERVER_ENV_PASSTHROUGH:-}" || true
    for key in ${passthrough[@]+"${passthrough[@]}"}; do
        [ "$key" != "ORCASTER_PLUGINS" ] || listed=1
    done
    [ "$listed" -eq 1 ] || \
        die "ORCASTER_PLUGINS is declared in setup.env but not listed in SERVER_ENV_PASSTHROUGH: it would not reach server/.env and the server would ignore it. Add it to SERVER_ENV_PASSTHROUGH."
    missing="$(as_app "$SERVER_DIR/.venv/bin/python" -c '
import sys
from importlib.metadata import entry_points
found = {ep.name for ep in entry_points(group="orcaster.plugins")}
missing = [n for n in sys.argv[1:] if n not in found]
print(" ".join(missing))
sys.exit(3 if missing else 0)
' "${names[@]}")" || rc=$?
    case "$rc" in
        0) ;;
        3) die "ORCASTER_PLUGINS names plugins that are not installed in $SERVER_DIR/.venv (entry points of the orcaster.plugins group): $missing. Declare their package in PLUGIN_PACKAGES or fix ORCASTER_PLUGINS." ;;
        *) die "Could not query the entry points of the orcaster.plugins group in $SERVER_DIR/.venv (rc=$rc)." ;;
    esac
}

# Non-interactive apt. Avoids debconf prompts on the host.
apt_install() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y --no-install-recommends "$@"
}

# Retries a command up to RETRY_MAX times (default 5) with RETRY_DELAY seconds
# (default 5) between attempts. Returns the command's exit code on the last
# attempt. Analogous to the retry in the worker's lib.sh (same RETRY_MAX/
# RETRY_DELAY signature).
retry() {
    local max="${RETRY_MAX:-5}" delay="${RETRY_DELAY:-5}" attempt=1 rc=0
    while true; do
        rc=0
        "$@" || rc=$?
        [ "$rc" -eq 0 ] && return 0
        [ "$attempt" -ge "$max" ] && return "$rc"
        warn "Attempt $attempt/$max of '$1' failed (rc=$rc); retrying in ${delay}s…"
        sleep "$delay"
        attempt=$((attempt + 1))
    done
}
