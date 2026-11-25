# shellcheck shell=bash
# =============================================================================
# Helpers shared by the WORKER setup scripts on Debian 13.
# It is sourced, not executed:  source "$(dirname "$0")/lib.sh"
#
# Analogous to setup-scripts/debian13/lib.sh (server), but for the worker, which
# is a rootless Podman container (not a native uvicorn service).
# =============================================================================

# Paths. SCRIPT_DIR = .../setup-scripts/debian13-worker ; REPO_ROOT = root of the
# tree. Both are physical (pwd -P): whatever root sources or runs after the
# source guard is resolved without going through symbolic links.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
# Path the script was invoked through (it may go through links); the source
# guard requires it to match SCRIPT_DIR.
_SCRIPT_DIR_INVOKED="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -L)"
# WORKER_RO_MOUNTS validator. Its own path, not derived from SCRIPT_DIR at the
# time of use: run-fleet.sh reassigns SCRIPT_DIR after sourcing this file.
RO_MOUNTS_CHECK="$SCRIPT_DIR/check-ro-mounts.sh"

# Host configuration directory. The host's LIVE config must not live inside the
# checkout: the code directory is replaced entirely on every release (tarball
# deployment), so any host file left inside it is lost in the swap. There are
# four and all four live here: setup.env, the base env, the fleet's env.N files
# and the server's public key.
# Environment override: this is a production channel, not a test seam
# (cloud-init exports it to its children).
CONFIG_DIR="${CONFIG_DIR:-/etc/orcaster}"
ENV_FILE="$CONFIG_DIR/setup.env"

# make arguments that the printed instructions append to `make up client`.
# The Makefile resolves the worker's env and public key from /etc/orcaster on
# its own (same paths as worker_envfile and CLIENT_PUBKEY), so WORKER_ENV and
# WORKER_PUBKEY no longer need to be passed. Only a host with the config in
# another path needs CONFIG_DIR, and the Makefile guard requires
# ETC_SETUP_ENV_FILE together with it.
# shellcheck disable=SC2034 # read by 05-smoke-test.sh and run-all-worker.sh
MAKE_CFG=""
[ "${CONFIG_DIR%/}" = /etc/orcaster ] || MAKE_CFG=" CONFIG_DIR=$CONFIG_DIR ETC_SETUP_ENV_FILE=$ENV_FILE"

# Worker image tag. It MUST match the WORKER_IMAGE default of the root Makefile:
# the scripts build through 'make build client' (which uses that default) and
# verify/detect with this value. It is not an override: changing it only here
# would break golden snapshot detection and the verifications.
# shellcheck disable=SC2034 # read by 04-build.sh, 05-smoke-test.sh and cloud-init-worker.sh
WORKER_IMAGE="localhost/orcaster-worker:local"

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
# The suite runs as root. If the worker user (WORKER_USER) could write any file
# that root sources or executes, a compromised worker process would change it
# and the next run of the suite would execute it as root. For that reason the
# suite only runs from a root-owned tree, separate from INSTALL_DIR (by default
# /root/orcaster, which install-release.sh leaves next to
# INSTALL_DIR). INSTALL_DIR does belong to WORKER_USER: the container mounts
# it. The guard does not repair a tree that was already modified: it prevents
# running from a tree that WORKER_USER can modify.
#
# require_root_owned_source runs when this file is sourced, before any other
# effect, and aborts if:
#   - the script was invoked through a path with symbolic links;
#   - SCRIPT_DIR or any directory between SCRIPT_DIR and / (REPO_ROOT included)
#     is not owned by root, or has group or other write permission, or has an
#     ACL;
#   - lib.sh, run-all-worker.sh, any other *.sh in SCRIPT_DIR or the Makefile in
#     REPO_ROOT (if they exist) are not regular files owned by root with no
#     group or other write permission and no ACL.
# load_env repeats the check on the setup.env it is about to source (also with
# CONFIG_DIR or ENV_FILE_OVERRIDE) and then require_source_outside_install_dir
# requires REPO_ROOT and INSTALL_DIR to be disjoint. No effect happens before
# load_env.
#
# It only acts when the process is root (id -u = 0): without root there is no
# boundary to cross (06-autostart.sh runs as the worker from the Makefile, and
# fleet-disk-check.sh on the operator's machine), and need_root re-executes the
# script with sudo, where the guard runs again before any effect.
#
# Owner and mode come from `ls -lnd` (same format on GNU and BSD; a '+' after
# the permissions marks an ACL). `ls` and `id` are resolved through PATH, like
# any other command of the suite: root's PATH is trusted. The tests prepend
# `ls` and `id` stubs to PATH, because they cannot create root-owned files; no
# environment variable relaxes the guard. For the same reason, the test seams
# LIB_OVERRIDE (run-fleet.sh, 06-autostart.sh) and AUTOSTART_SCRIPT
# (run-fleet.sh) are ignored when the process is root.

# Owner (uid) and permissions of a path, without following links. Prints
# "<perms> <uid>" or fails if the path does not exist.
_source_guard_stat() {  # $1=path
    local perms uid
    read -r perms _ uid _ < <(ls -lnd -- "$1" 2>/dev/null) || return 1
    [ -n "$perms" ] && [ -n "$uid" ] || return 1
    printf '%s %s' "$perms" "$uid"
}

# Aborts if $1 is not owned by root, has group or other write permission, or has
# an ACL. $2 = required type: 'd' (directory) or '-' (regular file).
_source_guard_check() {  # $1=path  $2=type
    local p="$1" want="$2" st perms uid
    st="$(_source_guard_stat "$p")" || \
        die "Source guard: cannot read the owner or mode of $p."
    perms="${st% *}"; uid="${st##* }"
    if [ "${perms:0:1}" != "$want" ]; then
        die "Source guard: $p is not a $([ "$want" = d ] && echo directory || echo 'regular file') (mode $perms; a symbolic link is not allowed). $_SOURCE_GUARD_LAYOUT"
    fi
    if [ "$uid" != 0 ] || [ "${perms:5:1}" = w ] || [ "${perms:8:1}" = w ] \
        || [ "${perms:10:1}" = + ]; then
        die "Source guard: the suite runs as root and does not execute code that another user can write. $p has owner uid $uid and mode $perms (required: owner root, no group or other write permission, no ACL). $_SOURCE_GUARD_LAYOUT"
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

# A setup.env that is about to be sourced: a regular file owned by root, with no
# group or other write permission, in a directory that only root controls. The
# group may read: /etc/orcaster is 0750 root:orcaster and setup.env is 0640
# root:orcaster. Prints the verified physical path: the caller sources that one
# and not the logical path, which could resolve differently between the check
# and the source. Since it runs inside a $(...), die only ends the subshell: the
# caller has to stop with `|| exit 1`.
_source_guard_check_env_file() {  # $1=path
    local dir
    dir="$(cd "$(dirname "$1")" && pwd -P)" || \
        die "Source guard: cannot resolve the directory of $1."
    _source_guard_check_ancestors "$dir"
    _source_guard_check "$dir/$(basename "$1")" -
    printf '%s\n' "${dir%/}/$(basename "$1")"
}

# Same as _source_guard_check but without aborting: rc 0 if $1 is of type $2,
# owned by root, with no group or other write permission and no ACL.
_source_guard_root_only() {  # $1=path  $2=type
    local st perms uid
    st="$(_source_guard_stat "$1")" || return 1
    perms="${st% *}"; uid="${st##* }"
    [ "${perms:0:1}" = "$2" ] && [ "$uid" = 0 ] && [ "${perms:5:1}" != w ] \
        && [ "${perms:8:1}" != w ] && [ "${perms:10:1}" != + ]
}

_SOURCE_GUARD_LAYOUT="Supported installation: a root-owned tree (by default /root/orcaster, which install-release.sh leaves; owner root, no group or other write permission), separate from INSTALL_DIR (/opt/orcaster by default, owned by the worker user), from which run-all-worker.sh, run-fleet.sh, cloud-init-worker.sh and the numbered scripts are run. See setup-scripts/debian13-worker/README.md."

require_root_owned_source() {
    [ "$(id -u)" = 0 ] || return 0
    local f
    if [ "$_SCRIPT_DIR_INVOKED" != "$SCRIPT_DIR" ]; then
        die "Source guard: the suite was invoked through $_SCRIPT_DIR_INVOKED, which goes through symbolic links to $SCRIPT_DIR. Run it through its real path. $_SOURCE_GUARD_LAYOUT"
    fi
    _source_guard_check_ancestors "$SCRIPT_DIR"
    for f in "$SCRIPT_DIR"/*.sh "$REPO_ROOT/Makefile"; do
        # lib.sh and run-all-worker.sh always exist; the rest only if present.
        [ -e "$f" ] || [ -L "$f" ] || continue
        _source_guard_check "$f" -
    done
    _source_guard_check "$SCRIPT_DIR/lib.sh" -
    _source_guard_check "$SCRIPT_DIR/run-all-worker.sh" -
}

# Aborts if REPO_ROOT and INSTALL_DIR are the same directory or one contains the
# other: INSTALL_DIR belongs to the worker user, so the suite would end up
# inside a tree that user can write. It always runs (with or without root): it
# is about the shape of the deployment. Requires INSTALL_DIR (load_env calls it).
require_source_outside_install_dir() {
    local src inst
    case "$INSTALL_DIR" in
        /*) ;;
        *) die "INSTALL_DIR must be an absolute path (is: '$INSTALL_DIR')." ;;
    esac
    # A '.' or '..' component is rejected (as in install-release.sh): the path
    # that is compared and the one printed in the messages must be the same,
    # with no traversals to resolve.
    case "$INSTALL_DIR/" in
        */./*|*/../*)
            die "INSTALL_DIR does not allow '.' or '..' as a path component (is: '$INSTALL_DIR'). Declare the already resolved path in setup.env." ;;
    esac
    src="${REPO_ROOT%/}/"
    inst="$(_physical_path "$INSTALL_DIR")"
    inst="${inst%/}/"
    case "$src" in "$inst"*)
        die "Source guard: the suite runs from $REPO_ROOT, which is inside INSTALL_DIR ($INSTALL_DIR). INSTALL_DIR belongs to the worker user, so root would execute code that user can change. $_SOURCE_GUARD_LAYOUT" ;;
    esac
    case "$inst" in "$src"*)
        die "Source guard: INSTALL_DIR ($INSTALL_DIR) is inside the tree the suite runs from ($REPO_ROOT). $_SOURCE_GUARD_LAYOUT" ;;
    esac
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

require_root_owned_source

# --- Environment guarantees --------------------------------------------------

# Requires root. If not root, re-executes the calling script through sudo,
# keeping its arguments.
need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null 2>&1 || die "This must run as root and 'sudo' was not found."
        log "Elevating with sudo…"
        exec sudo --preserve-env=DEBIAN_FRONTEND bash "$0" "$@"
    fi
}

# Checks that this is Debian 13. It does not abort on other versions, it only
# warns: this allows testing on derivatives while making clear it is outside
# what is supported.
# OS_RELEASE_FILE: override for tests only (bats does not run on real Debian);
# in production it is always /etc/os-release.
require_debian13() {
    local release_file="${OS_RELEASE_FILE:-/etc/os-release}"
    [ -r "$release_file" ] || die "Cannot find $release_file; is this Debian?"
    # shellcheck disable=SC1090,SC1091
    . "$release_file"
    if [ "${ID:-}" != "debian" ]; then
        warn "ID=${ID:-?} is not 'debian'. The scripts assume Debian 13 (Trixie)."
    elif [ "${VERSION_ID:-}" != "13" ]; then
        warn "VERSION_ID=${VERSION_ID:-?} is not '13'. Tested only on Trixie."
    fi
}

# --- Configuration -----------------------------------------------------------

# Sources setup.env and validates the required values. Applies defaults to the
# optional ones.
load_env() {
    local env_file="${ENV_FILE_OVERRIDE:-$ENV_FILE}"
    [ -f "$env_file" ] || die "Missing $env_file.
    New host: cp $SCRIPT_DIR/setup.env.example $env_file && \$EDITOR $env_file
    Host that was installed with the older layout: the config moved from the
    checkout to $CONFIG_DIR.
    Copy the setup.env from the old checkout to $CONFIG_DIR (and its secrets,
    if any) before retrying."
    # Root only sources a setup.env that no other user can change.
    if [ "$(id -u)" = 0 ]; then
        env_file="$(_source_guard_check_env_file "$env_file")" || exit 1
    fi
    # shellcheck disable=SC1090
    . "$env_file"

    : "${ORCH_BASE_URL:?ORCH_BASE_URL is required in setup.env (public URL of the server)}"
    [ "$ORCH_BASE_URL" = "https://orcaster.example.com" ] && \
        die "ORCH_BASE_URL still has the example value. Edit $ENV_FILE."

    # Defaults for the optional values.
    WORKER_USER="${WORKER_USER:-orcaster}"
    WORKER_HOME="${WORKER_HOME:-/home/orcaster}"
    INSTALL_DIR="${INSTALL_DIR:-/opt/orcaster}"
    require_source_outside_install_dir
    # The three clone variables that used to be resolved next to INSTALL_DIR
    # (code origin, ref and read-token file) no longer exist: the code is
    # pushed by the operator with ops/release/push-release.sh and the host does
    # not talk to GitHub. An old setup.env that still declares them is
    # harmless: nothing reads them.
    ORCH_WORKER_NAME="${ORCH_WORKER_NAME:-}"
    SERVER_ED25519_PUBKEY="${SERVER_ED25519_PUBKEY:-}"

    # Fleet parameters (cloud-init / bulk deployment).
    WORKER_COUNT="${WORKER_COUNT:-1}"
    WORKER_NAME_PREFIX="${WORKER_NAME_PREFIX:-}"
    EXTRA_ENV="${EXTRA_ENV:-}"
    ORCH_ENROLLMENT_TOKEN="${ORCH_ENROLLMENT_TOKEN:-}"

    # Egress isolation: the authoritative default is on. The fleet deployment
    # propagates it to `make up client`; opt out with EGRESS_ISOLATION=0.
    EGRESS_ISOLATION="${EGRESS_ISOLATION:-1}"
    # Only 0|1: the Makefile enables the topology with an EXACT match on '1'
    # (ifeq), so any other "truthy" value ('true', 'yes', '2') would silently
    # disable isolation, which is exactly what an authoritative default must
    # prevent. Abort early with the cause.
    case "$EGRESS_ISOLATION" in
        0|1) ;;
        *) die "EGRESS_ISOLATION must be 0 or 1 (is: '$EGRESS_ISOLATION')." ;;
    esac

    # Memory cap per container (podman --memory), propagated by the fleet
    # deployment to `make up client`. Empty = no cap (same opt-in as the
    # Makefile default). The b/k/m/g suffix is REQUIRED even though podman
    # accepts plain bytes: a bare '28' is almost certainly a typo for '28g', and
    # 28 bytes is below podman's minimum: the container would not even be
    # created and the deployment would fail halfway through the fleet, which is
    # exactly what this fail-fast avoids. Zero ('0g') is also rejected: it
    # matches the format but is below the same podman minimum.
    WORKER_MEMORY="${WORKER_MEMORY:-}"
    if [ -n "$WORKER_MEMORY" ] && ! [[ "$WORKER_MEMORY" =~ ^0*[1-9][0-9]*[bkmgBKMG]$ ]]; then
        die "WORKER_MEMORY must be a number greater than zero with a b/k/m/g suffix, e.g. 28g (is: '$WORKER_MEMORY')."
    fi

    # Host directories mounted read-only at the SAME path inside the container,
    # propagated by the fleet deployment to `make up client`. Empty = no extra
    # mounts. Only the SYNTAX is validated here: the host state (it exists, the
    # worker user can traverse it, the container uid can read it through
    # "other") is measured as that user in run-fleet.sh and in the Makefile,
    # because load_env runs as root and root reads any mode. The rule lives in a
    # single place, check-ro-mounts.sh, which cloud-init-worker.sh, the pre-swap
    # gate of install-release.sh and the Makefile also use.
    WORKER_RO_MOUNTS="${WORKER_RO_MOUNTS:-}"
    if [ -n "$WORKER_RO_MOUNTS" ] && ! bash "$RO_MOUNTS_CHECK" "$WORKER_RO_MOUNTS"; then
        die "WORKER_RO_MOUNTS invalid in $env_file (the reason is on the line above)."
    fi

    # Worker disk gate: a declarative channel in setup.env, DISTINCT from
    # EXTRA_ENV (that one is for the runtime secrets of the TASKS; this is
    # config of the worker itself). Literal names of the worker's env vars
    # (ORCH_ORCASTER_CACHE_CLAIM_*, see client/src/orcaster_worker/config.py):
    # no mapping layer, so that setup.env, the .env and the user manual use the
    # same key. Empty (default) => the line is not stamped in the .env, and the
    # worker applies its own default (floor: inherits cache_min_free_fraction,
    # 0.20 today; deadline: 1800s). The source of truth for the default remains
    # the worker code, not this script: only the FORMAT is validated here (not
    # the range against cache_min_free_fraction, which would require hardcoding
    # that value here); an out-of-range value is rejected by Settings() when the
    # container starts.
    ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION="${ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION:-}"
    if [ -n "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" ] && \
       ! [[ "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" =~ ^(0(\.[0-9]+)?|1(\.0*)?|\.[0-9]+)$ ]]; then
        die "ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION must be a number between 0.0 and 1.0 (is: '$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION')."
    fi
    ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS="${ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS:-}"
    if [ -n "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" ] && \
       ! [[ "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        die "ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS must be a number >= 0 (is: '$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS')."
    fi

    # Warning (NOT fail-fast) if the same gate key is declared in BOTH channels:
    # the new channel above (already validated) AND a leftover in EXTRA_ENV
    # (an older workaround, e.g.
    # EXTRA_ENV="ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.10"). The stamped
    # .env would end up with the key duplicated; stamp_worker_env writes the
    # gate block AFTER EXTRA_ENV so that the value from here wins (last in the
    # file == the one that wins with podman's --env-file, see
    # stamp_worker_env), but without a warning the operator never learns that a
    # leftover was not cleaned up. It is checked here, in load_env, because it
    # is the one that resolves BOTH values from setup.env and therefore the only
    # point where the duplicate is observable without re-parsing anything. Mind
    # the frequency: load_env runs once per script of the suite (00-06,
    # run-all-worker.sh and run-fleet.sh), so on a fresh cloud-init the warning
    # appears ~9 times; putting it in stamp_worker_env would print it
    # 1+WORKER_COUNT times. Here is still preferred: the repetition is constant
    # (it does not grow with the fleet) and the warning sits next to the
    # validation of the same value. It is not fail-fast on purpose: a .env with
    # the old workaround is a state that works TODAY, and aborting the
    # deployment of an entire fleet over a duplicate that now resolves correctly
    # would be worse than the problem being reported.
    #
    # The grep tolerates indentation (^[[:space:]]*): a multiline EXTRA_ENV
    # written with indentation in the user-data is still honored by podman
    # --env-file (which does a TrimSpace of each line), so the duplicate is real
    # and must be reported anyway.
    if [ -n "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" ] && \
       printf '%s\n' "$EXTRA_ENV" | grep -qE '^[[:space:]]*ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION='; then
        warn "ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION is declared both in EXTRA_ENV and in the disk gate of setup.env; in the stamped .env the gate value wins ('$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION'). Remove the leftover from EXTRA_ENV."
    fi
    if [ -n "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" ] && \
       printf '%s\n' "$EXTRA_ENV" | grep -qE '^[[:space:]]*ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS='; then
        warn "ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS is declared both in EXTRA_ENV and in the disk gate of setup.env; in the stamped .env the gate value wins ('$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS'). Remove the leftover from EXTRA_ENV."
    fi

    # Strict isolation of the tasks' environment: a declarative channel in
    # setup.env, separate from EXTRA_ENV, like the disk gate. Empty = not
    # declared; the resolution of the stamped value (setup.env, EXTRA_ENV,
    # previous .env, existing host or new installation) lives in
    # stamp_worker_env. Only true|false: any other "truthy" value ('1', 'yes')
    # would be accepted by the worker, but the pre-swap gate and the conflict
    # check below compare text, and two spellings of the same value would hide a
    # real disagreement.
    ORCH_ENFORCE_ISOLATION="${ORCH_ENFORCE_ISOLATION:-}"
    case "$ORCH_ENFORCE_ISOLATION" in
        ''|true|false) ;;
        *) die "ORCH_ENFORCE_ISOLATION must be true or false (is: '$ORCH_ENFORCE_ISOLATION')." ;;
    esac
    # List of variables that require strict isolation: exact names separated
    # by commas or spaces. It is normalized to a comma-joined list, which is
    # what gets stamped as ORCH_ISOLATION_REQUIRED_ENV and what the worker and
    # the pre-swap gate read. A malformed name aborts: a fail-closed check that
    # is disabled by a typo protects nothing.
    local isolation_required_normalized
    isolation_required_normalized="$(normalize_isolation_required_env "${ISOLATION_REQUIRED_ENV:-}")" || \
        die "ISOLATION_REQUIRED_ENV invalid in $env_file (the reason is on the line above)."
    ISOLATION_REQUIRED_ENV="$isolation_required_normalized"
    # A single channel for the list: if declared inside EXTRA_ENV, a later line
    # of the .env could replace or empty it and turn the guard off without
    # notice. The grep tolerates indentation, like the one for the disk gate.
    if printf '%s\n' "$EXTRA_ENV" | grep -qE '^[[:space:]]*ORCH_ISOLATION_REQUIRED_ENV='; then
        die "EXTRA_ENV declares ORCH_ISOLATION_REQUIRED_ENV. That list goes only in ISOLATION_REQUIRED_ENV in setup.env (a single channel, so that no other line replaces or empties it). Move it and remove it from EXTRA_ENV."
    fi
    # Explicit conflict: setup.env and EXTRA_ENV declare the flag with different
    # values. One is not chosen silently: in the stamped .env the one from
    # setup.env would win (it comes later), and if it is 'false' it would weaken
    # what the operator asked for in EXTRA_ENV. The last occurrence in EXTRA_ENV
    # is compared (the one that wins with --env-file), without trailing spaces
    # or CR.
    if [ -n "$ORCH_ENFORCE_ISOLATION" ]; then
        local extra_enforce
        extra_enforce="$(printf '%s\n' "$EXTRA_ENV" | sed -n 's/^[[:space:]]*ORCH_ENFORCE_ISOLATION=//p' | tail -n1)"
        extra_enforce="${extra_enforce%"${extra_enforce##*[![:space:]]}"}"
        if printf '%s\n' "$EXTRA_ENV" | grep -qE '^[[:space:]]*ORCH_ENFORCE_ISOLATION=' && \
           [ "$extra_enforce" != "$ORCH_ENFORCE_ISOLATION" ]; then
            die "ORCH_ENFORCE_ISOLATION has different values in setup.env ('$ORCH_ENFORCE_ISOLATION') and in EXTRA_ENV ('$extra_enforce'). Declare the flag in a single place (preferably ORCH_ENFORCE_ISOLATION in setup.env) and run again."
        fi
    fi

    case "$WORKER_COUNT" in
        ''|*[!0-9]*) die "WORKER_COUNT must be an integer >= 1 (is: '$WORKER_COUNT')." ;;
    esac
    # 10#$WORKER_COUNT: forces base 10 so that leading zeros (e.g. '08') are not
    # interpreted as octal, which would fail with a confusing bash error.
    [ "$((10#$WORKER_COUNT))" -ge 1 ] || die "WORKER_COUNT must be >= 1 (is: '$WORKER_COUNT')."

    # Derived paths, stable across reruns. They live in CONFIG_DIR and NOT in
    # the checkout: the tarball deployment replaces $INSTALL_DIR entirely.
    # shellcheck disable=SC2034 # read by 03-worker-env.sh and 05-smoke-test.sh
    CLIENT_ENV="$CONFIG_DIR/env"
    # shellcheck disable=SC2034 # read by 02-server-pubkey.sh and 05-smoke-test.sh
    CLIENT_PUBKEY="$CONFIG_DIR/server-ed25519.pub"
}

# Normalizes the list of variables that require strict isolation. Input: names
# separated by commas and/or spaces (tabs and newlines included); empty tokens
# are ignored. Each name must match ^[A-Za-z_][A-Za-z0-9_]*$, the same grammar
# the worker applies (ORCH_ISOLATION_REQUIRED_ENV). Output: the names joined by
# commas, without duplicates and in input order; empty if the list is empty. A
# malformed token writes the reason to stderr and returns 1 without printing
# anything to stdout. No globbing: `read -a` does not expand wildcards, so a '*'
# arrives as a token and is rejected.
normalize_isolation_required_env() {
    local raw="${1-}" tok out="" seen=","
    local -a toks=()
    raw="${raw//,/ }"
    raw="${raw//$'\n'/ }"
    read -r -a toks <<< "$raw" || true
    for tok in ${toks[@]+"${toks[@]}"}; do
        if ! [[ "$tok" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            printf 'ISOLATION_REQUIRED_ENV: invalid variable name: %q (expected names [A-Za-z_][A-Za-z0-9_]* separated by commas or spaces).\n' "$tok" >&2
            return 1
        fi
        case "$seen" in *",$tok,"*) continue ;; esac
        seen="$seen$tok,"
        out="${out:+$out,}$tok"
    done
    printf '%s\n' "$out"
}

# --- Per-worker name derivation (mirror of the Makefile's WORKER_SUFFIX) ------
# The Makefile derives: WORKER_SUFFIX = -$(N) if N is non-empty, else empty.
# These functions replicate that convention for 06-autostart.sh and run-fleet.sh.

worker_name() {
    local n="${1:-}"
    if [ -n "$n" ]; then printf 'orcaster-worker-%s\n' "$n"; else printf 'orcaster-worker\n'; fi
}

worker_unit() {
    printf 'container-%s.service\n' "$(worker_name "${1:-}")"
}

# Per-worker egress sidecar (PROXY_NAME = orcaster-proxy$(WORKER_SUFFIX) in the
# Makefile). It only exists when the worker was recreated with
# EGRESS_ISOLATION=1; 06-autostart.sh detects its presence to persist its unit
# as well and to order startup (the worker must start AFTER the proxy).
proxy_name() {
    local n="${1:-}"
    if [ -n "$n" ]; then printf 'orcaster-proxy-%s\n' "$n"; else printf 'orcaster-proxy\n'; fi
}

proxy_unit() {
    printf 'container-%s.service\n' "$(proxy_name "${1:-}")"
}

worker_envfile() {
    local n="${1:-}" config_dir="$2"
    if [ -n "$n" ]; then printf '%s/env.%s\n' "$config_dir" "$n"; else printf '%s/env\n' "$config_dir"; fi
}

# make arguments that select the worker env file and pubkey for
# `make -C <install_dir> up client N=<n>`. The Makefile
# resolves both from CONFIG_DIR on its own (WORKER_ENV_HOST/WORKER_PUBKEY_HOST),
# which is the single definition of those paths for a running worker:
# this prints nothing and the scripts no longer pass WORKER_ENV/WORKER_PUBKEY.
# A Makefile without that resolution (a rollback to an older release) still
# defaults to client/ inside the replaceable checkout; only
# then the two overrides are printed, one per line, built from worker_envfile.
# Args: $1=install dir  $2=worker index (may be empty)  $3=config dir.
worker_config_make_args() {
    local install_dir="$1" n="${2:-}" config_dir="$3"
    if grep -q '^WORKER_ENV_HOST ' "$install_dir/Makefile" 2>/dev/null; then
        return 0
    fi
    printf 'WORKER_ENV=%s\n' "$(worker_envfile "$n" "$config_dir")"
    printf 'WORKER_PUBKEY=%s/server-ed25519.pub\n' "$config_dir"
}

# Worker data volume (WORKER_DATA = orcaster-data$(WORKER_SUFFIX) in the
# Makefile). state.json (worker_id/api_key) persists there: its presence is
# proof that enrollment against the server really happened.
worker_volume() {
    local n="${1:-}"
    if [ -n "$n" ]; then printf 'orcaster-data-%s\n' "$n"; else printf 'orcaster-data\n'; fi
}

# Reads the commit of the installed release. The RELEASE file is embedded by
# make-release.sh inside the tarball: it replaces `git rev-parse` as proof of
# which code is installed, now that the host has no git.
#
# Fails (rc=1) if there is no file OR if the file does not declare a commit: a
# truncated or zero-byte RELEASE is NOT installed code, and returning rc=0 with
# an empty string would leave a caller with `c="$(read_release_commit d)" || die`
# believing it had read an identity.
#
# Stopping at the first match is done with the block /^commit=/{...;q;} and not
# with a pipe to `head -n1`: the production callers run with `set -o pipefail`,
# where the SIGPIPE that head sends to sed can turn into a failure of the
# function. With a three-line manifest the race is almost never lost (sed
# finishes first), which is exactly why it is better to remove it than to rely
# on timing. Beware of the tempting form `s/^commit=//p;/^commit=/q`: it does
# not stop anything, because by the time the second command is evaluated the
# substitution has already deleted the prefix from the pattern space.
read_release_commit() {
    local install_dir="$1"
    [ -f "$install_dir/RELEASE" ] || return 1
    release_commit_of "$(cat -- "$install_dir/RELEASE")"
}

# Commit of a RELEASE that was already read (its content in $1), with the same
# rule as read_release_commit: fails if it does not declare a commit.
release_commit_of() {  # $1=content of RELEASE
    local commit
    commit="$(sed -n '/^commit=/{s/^commit=//p;q;}' <<<"$1")"
    [ -n "$commit" ] || return 1
    printf '%s\n' "$commit"
}

# Prints the content of the RELEASE in $1 (INSTALL_DIR) without root following
# whatever the worker user left in that tree: a RELEASE that was a link to a
# root-only readable file would end up copied into the log.
#   - Without root: the process reads it as is.
#   - Root with the worker user ($2) already created: that user reads it
#     (sudo -u), so only what it could already read comes out.
#   - Root without that user (bootstrap, before 00-preflight.sh): the tree was
#     left by install-release.sh as root, and root reads it only if INSTALL_DIR,
#     its ancestors and the RELEASE are owned by root with no group or other
#     write permission. Otherwise it fails: there is no unprivileged user to
#     read it with.
# Fails if there is no readable RELEASE. It is read only once and the caller
# validates and prints the text: two reads could see different files.
read_release_file() {  # $1=install_dir  $2=user that owns INSTALL_DIR
    local dir="$1" user="$2" phys d
    if [ "$(id -u)" != 0 ]; then
        cat -- "$dir/RELEASE" 2>/dev/null
        return
    fi
    if id -u "$user" >/dev/null 2>&1; then
        ( cd / && sudo -u "$user" cat -- "$dir/RELEASE" ) 2>/dev/null
        return
    fi
    phys="$(cd "$dir" 2>/dev/null && pwd -P)" || return 1
    d="$phys"
    while :; do
        _source_guard_root_only "$d" d || return 1
        [ "$d" = / ] && break
        d="$(dirname "$d")"
    done
    _source_guard_root_only "$phys/RELEASE" - || return 1
    cat -- "$phys/RELEASE" 2>/dev/null
}

# Decides whether the host needs bootstrap (pushed code + built image) or was
# created from a golden snapshot. The image check command is received as args so
# it can be mocked in tests; in production the caller passes:
# as_worker podman image exists <ref>. The "code present" marker is the RELEASE
# and not .git: with tarball deployment the host has no repo. The commit must
# really be readable, not just the file existing: a truncated manifest leaves a
# host that cannot declare which code it runs, and that is exactly what this
# check has to catch. The first argument is the CONTENT of the RELEASE
# (read_release_file), not the path: the caller reads it once and uses the same
# text to decide and to print the release identity.
should_bootstrap() {
    local release="$1"; shift
    if release_commit_of "$release" >/dev/null && "$@" >/dev/null 2>&1; then
        echo skip
    else
        echo bootstrap
    fi
}

# Writes a worker's env file. A previous ORCH_API_KEY is always preserved
# (idempotent rerun on an already enrolled worker). ORCH_ENROLLMENT_TOKEN: the
# token passed in wins if it is non-empty; the previous one from the .env only
# wins when the caller does not pass one (manual flow of 03-worker-env.sh, which
# passes ""). Thus, in a fleet (run-fleet.sh) a rerun with a valid token stamps
# it, allowing an expired/exhausted token to be rotated without losing the rest
# of the .env. It does NOT chown: the caller (03-worker-env.sh / run-fleet.sh)
# sets owner and permissions.
#
# claim_min_free_fraction/claim_gate_max_seconds: values ALREADY resolved by
# load_env from setup.env (a channel separate from EXTRA_ENV, with its own
# commented block). Unlike the token/api_key, they are NOT read from the
# previous .env: their source of truth is setup.env on every run, so as long as
# the operator does not touch it, the value survives any rerun by construction.
# Empty => the line is not stamped (an empty line would break pydantic-settings
# float parsing; omitting it lets the worker apply its own default).
#
# enforce_isolation/isolation_required_env: values ALREADY validated by load_env
# from setup.env (ORCH_ENFORCE_ISOLATION, true|false|empty, and
# ISOLATION_REQUIRED_ENV normalized to comma-joined names). The stamped
# ORCH_ENFORCE_ISOLATION is resolved in this order:
#   1. setup.env declares it => that value.
#   2. otherwise, EXTRA_ENV declares it => no line of its own is stamped: the
#      one from EXTRA_ENV remains, and it is never weakened by a 'false'
#      stamped afterwards.
#   3. otherwise, the previous .env has an ORCH_ENFORCE_ISOLATION= line with a
#      value => it is kept (last occurrence, the one that won with --env-file).
#   4. otherwise, the .env already exists (host installed with the old default)
#      => 'false', explicit: the worker behaves as before and the decision is
#      written in a file the operator can read. The default in the worker code
#      is now 'true', so without this line the host would change behavior on
#      upgrade.
#   5. otherwise (new installation) => 'true'.
# The setup.env vs EXTRA_ENV conflict is rejected by load_env before reaching
# here. ORCH_ISOLATION_REQUIRED_ENV is always stamped (empty if the list is), so
# that the worker and the pre-swap gate read a single value from env.N.
stamp_worker_env() {
    local env_path="$1" base_url="$2" token="$3" name="$4" extra="${5:-}"
    local claim_min_free_fraction="${6:-}" claim_gate_max_seconds="${7:-}"
    local enforce_isolation="${8:-}" isolation_required_env="${9:-}"
    local prev_api_key="" prev_token="" prev_enforce="" env_existed=0
    if [ -f "$env_path" ]; then
        env_existed=1
        prev_api_key="$(sed -n 's/^ORCH_API_KEY=//p' "$env_path" | head -n1)"
        prev_token="$(sed -n 's/^ORCH_ENROLLMENT_TOKEN=//p' "$env_path" | head -n1)"
        # Last occurrence, tolerating indentation and stripping trailing spaces
        # and CR (podman --env-file does a TrimSpace of each line).
        prev_enforce="$(sed -n 's/^[[:space:]]*ORCH_ENFORCE_ISOLATION=//p' "$env_path" | tail -n1)"
        prev_enforce="${prev_enforce%"${prev_enforce##*[![:space:]]}"}"
    fi
    [ -z "$token" ] && token="$prev_token"
    local stamp_enforce
    if [ -n "$enforce_isolation" ]; then
        stamp_enforce="$enforce_isolation"
    elif printf '%s\n' "$extra" | grep -qE '^[[:space:]]*ORCH_ENFORCE_ISOLATION='; then
        stamp_enforce=""
    elif [ -n "$prev_enforce" ]; then
        stamp_enforce="$prev_enforce"
    elif [ "$env_existed" -eq 1 ]; then
        stamp_enforce="false"
    else
        stamp_enforce="true"
    fi
    # umask 077 in a subshell: scoped to writing the file, it does not leak into
    # the rest of the caller's script (umask is not scoped to the function by
    # itself).
    (
        umask 077
        {
            echo "# Generated by setup-scripts/debian13-worker. Do not edit except the token and ORCH_WORKER_NAME."
            echo "ORCH_BASE_URL=${base_url}"
            echo "ORCH_API_KEY=${prev_api_key}"
            echo "ORCH_ENROLLMENT_TOKEN=${token}"
            echo "ORCH_ED25519_PUBLIC_KEY_PATH=/var/lib/orcaster/ed25519.pub"
            echo "ORCH_WORKER_NAME=${name}"
            if [ -n "$extra" ]; then
                echo "# --- EXTRA_ENV (runtime secrets of the tasks) ---"
                printf '%s\n' "$extra"
            fi
            # The gate block is stamped AFTER EXTRA_ENV on purpose: the worker
            # consumes this file through `podman run --env-file` (Makefile), and
            # with a duplicated key in an --env-file the LAST occurrence in the
            # file wins (same behavior as python-dotenv, which Settings uses as
            # a fallback through env_file=".env" in
            # client/src/orcaster_worker/config.py). If an operator left an old
            # workaround in EXTRA_ENV (e.g.
            # EXTRA_ENV="ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0.10") and
            # now also declares the new validated channel, the new channel must
            # win the last-wins rule, not the unvalidated leftover. load_env
            # also warns on stderr when it detects the key in both channels, so
            # the operator cleans up the leftover.
            if [ -n "$claim_min_free_fraction" ] || [ -n "$claim_gate_max_seconds" ]; then
                echo "# --- Worker disk gate (see setup.env.example) ---"
                [ -n "$claim_min_free_fraction" ] && \
                    echo "ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=${claim_min_free_fraction}"
                [ -n "$claim_gate_max_seconds" ] && \
                    echo "ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=${claim_gate_max_seconds}"
            fi
            # Isolation block, also AFTER EXTRA_ENV: with last-wins, the value
            # resolved here is the one the worker reads. No line of its own for
            # the flag when only EXTRA_ENV declares it (step 2 of the resolution
            # above).
            echo "# --- Task environment isolation (see setup.env.example) ---"
            if [ -n "$stamp_enforce" ]; then
                echo "ORCH_ENFORCE_ISOLATION=${stamp_enforce}"
            fi
            echo "ORCH_ISOLATION_REQUIRED_ENV=${isolation_required_env}"
            # Final 'true': if the last command of the group were a `[ -n .. ]
            # && echo` (like those of the disk gate), its exit status would be
            # FALSE (1) when the condition is not met: a caller under 'set -e'
            # (03-worker-env.sh, run-fleet.sh) would abort there even though the
            # file was written correctly. It forces the group to succeed without
            # altering the stamped content, whatever the final block is.
            true
        } > "$env_path"
    )
}

# Quotes a value to write it into a file that is later re-sourced (setup.env):
# single quotes with ' escaped => the source does not expand $/`/", does not
# execute $(...) or backticks, and preserves newlines (PEM/EXTRA_ENV are
# arbitrary operator content).
env_quote() {
    # sq/esc in variables: quotes embedded in ${v//...} under double quotes are
    # interpreted as literals (not as quoting), so they do not work there.
    local v="${1-}" sq="'" esc="'\\''"
    v="${v//$sq/$esc}"
    printf "'%s'" "$v"
}

# Runs a command as WORKER_USER with the minimal environment that rootless
# Podman needs (XDG_RUNTIME_DIR + DBUS for systemd --user). It prepends `env` to
# set HOME/PATH reliably: sudo may reset them (secure_path, always_set_home),
# `env` does not.
#
# The `cd /` is not cosmetic: sudo inherits the caller's cwd, and if root
# invokes the suite from /root (mode 700) the subcommand dies with "cannot
# chdir" before running: a false failure that is not visible under cloud-init
# (cwd `/`) but is on every manual rerun over SSH. The subshell scopes it to the
# invocation and leaves the calling script's cwd untouched.
#
# This guard only covers what goes through here. The `sudo -u orcaster` calls
# that cannot use as_worker (cloud-init-worker.sh, before load_env runs) and the
# ones the operator copies from the README are protected in their own place.
as_worker() {
    local uid
    uid="$(id -u "$WORKER_USER")"
    ( cd / && sudo -u "$WORKER_USER" env \
        HOME="$WORKER_HOME" \
        XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        PATH="/usr/local/bin:/usr/bin:/bin" \
        "$@" )
}

# Non-interactive apt. Avoids debconf prompts on the host.
apt_install() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y --no-install-recommends "$@"
}

# Retries a command up to RETRY_MAX times (default 5) with RETRY_DELAY seconds
# between attempts (default 5): a transient DNS/network blip on first boot must
# not leave the host half provisioned, because cloud-init does not re-run a
# user-data that already failed.
#
# Only call site today: the `curl` of the connectivity preflight in
# cloud-init-worker.sh. The `apt-get` in 00-preflight.sh runs bare, without
# retry. Do not name here a retry that does not exist: whoever reads this to
# diagnose an apt failure on first boot would conclude that five attempts were
# already made and would rule out the transient blip as a cause.
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
