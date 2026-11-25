#!/usr/bin/env bash
# install-release: installs on the host a package produced by make-release.sh.
# push-release.sh copies it next to the tarball on every push, from the
# operator's working tree, NOT from the --ref used to build the tarball: it is
# not pinned to the release version, it only travels with it (in practice this
# does not matter: this script depends on nothing that changes from release to
# release).
#
# Invariant: it NEVER deletes the installed code before the new one is complete
# and verified, NOR BEFORE VERIFYING THAT THE HOST HAS WHAT THE RECREATION WILL
# NEED. If the scp, the tar or the host check failed, the host keeps what it
# already had and the script aborts with the cause.
#
# The tarball carries the `orcaster/` prefix at its root. Without it, the
# script aborts without touching the installed code.
#
# Usage (as root on the host):
#   install-release.sh --tarball orcaster-X.Y.Z-abc12345.tgz
#   install-release.sh --tarball ... --workers 2   # operator override
#   install-release.sh --tarball ... --no-restart  # bootstrap: leaves containers alone
#   install-release.sh --tarball ... --config-dir /srv/orcaster
#   install-release.sh --tarball ... --install-dir /srv/orcaster
#   install-release.sh --tarball ... --source-dir /root/orcaster
#
# --workers is OPTIONAL. Without it, the number of workers to recreate comes
# from WORKER_COUNT in the host's OWN $CONFIG_DIR/setup.env, which is the same
# source run-fleet.sh iterates to stamp the env.N files and create the
# containers. Passing it explicitly wins (the operator is in charge) but warns
# if it does not match what the host declares.
#
# --config-dir is OPTIONAL and defaults to /etc/orcaster. It is the host's
# config directory: setup.env, the fleet's env.N files and the server's public
# key. The CONFIG_DIR environment variable does the same, but it is only useful
# for tests: in production this script is invoked with `sudo bash` (sudo with
# env_reset does not propagate the environment) and through ssh (which does not
# accept the variable: it is not in AcceptEnv), so the environment is NOT a
# usable channel. Without the option, a host whose config does not live in
# /etc/orcaster could not be deployed through the sanctioned path: the pre-swap
# gate aborted asking for /etc/orcaster/setup.env on a host that never had to
# migrate. Hosts that cloud-init-worker.sh provisions with a templated
# CONFIG_DIR are exactly that case. push-release.sh propagates it per host
# with CONFIG_DIR_<host> from deploy.local.env.
#
# --install-dir is OPTIONAL and defaults to /opt/orcaster. It is the
# CODE directory, the one the swap replaces entirely on every release. Without
# the option, the value comes from INSTALL_DIR in the host's OWN
# $CONFIG_DIR/setup.env, which is the same path that cloud-init-worker.sh
# (it stamps it when provisioning), run-fleet.sh and 06-autostart.sh read.
# Passing it explicitly wins (the operator is in charge) but warns if it does
# not match what the host declares: that difference means one of the two is
# wrong, and choosing silently leaves the tree the host declares frozen at the
# previous release. It is the option the abort messages further down name as
# the remedy, so it has to be in this --help: undocumented, the operator reads
# "pass it with --install-dir" and cannot find it in the command's own help.
#
# --source-dir is OPTIONAL and defaults to /root/orcaster. It is
# root's copy of the same release: owned by root:root and with no group or
# other write permission. INSTALL_DIR stays owned by the worker user (the
# container mounts it), so root executes nothing from there: the provisioning
# suite (run-all-worker.sh, run-fleet.sh, cloud-init-worker.sh, 06-autostart.sh
# as root) runs from SOURCE_DIR/setup-scripts/debian13-worker, and this script
# sources the lib.sh of that copy. Order: --source-dir, the SOURCE_DIR
# environment variable, SOURCE_DIR from the host's setup.env (only without
# --no-restart, like INSTALL_DIR) and the default. SOURCE_DIR has to be an
# absolute path that is not a link, outside INSTALL_DIR and the staging area,
# and its parent directory and every ancestor have to be owned by root with no
# group or other write permission; otherwise it aborts before touching the
# installed code. The new copy is built in SOURCE_DIR.new before the swap and
# replaces SOURCE_DIR after the INSTALL_DIR swap. A SOURCE_DIR that is a git
# checkout (it has .git) is not replaced: it aborts.
set -euo pipefail

TARBALL=""
# Empty = "the operator did not say": it is resolved from the host's setup.env,
# and only under RESTART=1 (step 2.5). With --no-restart it is not used
# anywhere, and resolving it anyway would force requiring a setup.env that the
# bootstrap does not have yet.
WORKERS=""
RESTART=1
# Same "empty = not said" idiom for the other two knobs that the host's
# setup.env also declares (setup.env.example and load_env in
# lib.sh:89/:91). They used to have the default hardcoded here, so this script
# read three variables from the host's file (WORKER_COUNT, EGRESS_ISOLATION,
# WORKER_MEMORY) and discarded precisely the two that decide WHERE and AS WHOM
# it installs. The case is not theoretical: cloud-init-worker.sh STAMPS
# `INSTALL_DIR=...` inside the setup.env it generates, so every host
# provisioned by cloud-init declares the knob that was being ignored. And the
# environment override saves nothing in production: push-release.sh invokes
# `sudo bash install-release.sh`, and sudo with env_reset (the default on
# Debian) does not propagate variables (nor does ssh accept them: they are not
# in AcceptEnv).
INSTALL_DIR=""
WORKER_USER="${WORKER_USER:-}"
CONFIG_DIR="${CONFIG_DIR:-/etc/orcaster}"
# Root's copy of the release (see --help). The option wins over the environment
# variable; both win over the host's setup.env; and all of them win over the
# default.
SOURCE_DIR_FLAG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --tarball) TARBALL="${2:?--tarball needs a value}"; shift ;;
        --install-dir) INSTALL_DIR="${2:?--install-dir needs a value}"; shift ;;
        --source-dir) SOURCE_DIR_FLAG="${2:?--source-dir needs a value}"; shift ;;
        --config-dir) CONFIG_DIR="${2:?--config-dir needs a value}"; shift ;;
        --workers) WORKERS="${2:?--workers needs a value}"; shift ;;
        --no-restart) RESTART=0 ;;
        # Range closed by hand: from line 2 (first line of the header block) to
        # the last commented line, the one right before `set -euo pipefail`.
        # If you add or remove header lines it has to be recalculated, or
        # --help starts cutting the documentation midway or spitting out code
        # (check with: bash install-release.sh --help).
        -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1 (use --help)" >&2; exit 1 ;;
    esac
    shift
done
SOURCE_DIR="${SOURCE_DIR_FLAG:-${SOURCE_DIR:-}}"

# Root guard. It goes here, after argument parsing (so --help keeps working
# without privileges: it leaves the while loop above before reaching this
# line) and before touching the tarball or the installed code. push-release.sh
# always invokes this script with `sudo bash /tmp/install-release.sh`; root is
# the only way it is expected to run. Without this guard, the script would not
# fail on the first line but halfway through (the `mv` of $INSTALL_DIR or the
# `chown -R`), which is the worst place to stop: that is where the code
# directory can end up moved but not replaced. It does not re-execute with sudo
# on its own (unlike need_root in lib.sh): if root is missing at this point,
# something in the transport broke, and the right thing is to abort with the
# cause, not to elevate.
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: install-release.sh must run as root." >&2
    echo "       Run: sudo bash $0 --tarball ... (or via push-release.sh, which already invokes it that way)." >&2
    exit 1
fi

[ -n "$TARBALL" ] || { echo "ERROR: --tarball is missing." >&2; exit 1; }
[ -f "$TARBALL" ] || { echo "ERROR: $TARBALL does not exist." >&2; exit 1; }

# Trailing slashes removed, in a single place: $CONFIG_DIR and $INSTALL_DIR
# need it when normalized, and ALSO the comparison of the host's INSTALL_DIR
# against the one from --install-dir, which runs before that normalization.
# Three copies of the same `while` was the way for one of them to fall behind.
# It uses neither `realpath` nor `readlink -f`: the path may not exist yet on
# this host (first delivery), and collapsing symlinks here would destroy
# exactly what the step 3 guard needs to see.
rtrim_slashes() {
    local p="$1"
    while [ "$p" != "/" ] && [ "${p%/}" != "$p" ]; do p="${p%/}"; done
    printf '%s\n' "$p"
}

# $CONFIG_DIR is consumed as a path prefix (setup.env, the env.N files, the
# public key) and travels all the way to the `make` line of step 4, so a
# malformed value is not cosmetic: a relative path would be resolved against
# this script's cwd (the `sudo` one, not the operator's) and a value with
# metacharacters would split the make line into loose words. A closed
# character set and an absolute path are required, in the same idiom as
# validate_workers: early, before touching anything on the host.
case "$CONFIG_DIR" in
    *[!A-Za-z0-9/._-]*)
        echo "ERROR: CONFIG_DIR only accepts letters, digits and the characters / . _ - (got: '$CONFIG_DIR')." >&2
        exit 1 ;;
esac
case "$CONFIG_DIR" in
    /*) ;;
    *)
        echo "ERROR: CONFIG_DIR must be an absolute path (got: '$CONFIG_DIR'). Pass it with --config-dir /absolute/path." >&2
        exit 1 ;;
esac
# Trailing slashes removed: without this, paths and messages show '//', which
# works but reads like a script error.
CONFIG_DIR="$(rtrim_slashes "$CONFIG_DIR")"

# The two guards below go AFTER rtrim_slashes, and not next to the two above,
# on purpose: they measure the NORMALIZED value. With '--config-dir //' a
# comparison made before normalizing sees nothing odd (the string is '//', not
# '/') and the rtrim leaves it as '/' anyway, so the guard would be inert in
# exactly one of the two inputs it is meant to cover.
#
# The root is rejected outright instead of being normalized. rtrim_slashes
# deliberately keeps the '/' (emptying the variable would be worse, and
# $INSTALL_DIR shares the function), so the root is the ONLY input where the
# '//' that this loop exists to remove survives: '/' plus '/setup.env' gives
# '//setup.env'. It opens the same file, but it shows up like that in the error
# messages and in the CONFIG_DIR= of the autostart_cmd command, which is where
# the rtrim comment says it "reads like a script error". A config directory at
# the root is not a legitimate case, so rejecting it is more honest than
# normalizing it.
if [ "$CONFIG_DIR" = "/" ]; then
    echo "ERROR: CONFIG_DIR cannot be the root '/' (got: '$CONFIG_DIR'). The host's setup.env, the fleet's env.N files and the server's public key would be looked up there, and the paths would end up with the double slash that this script normalizes in every other value ('//setup.env'). The default is /etc/orcaster: pass a directory of your own with --config-dir /absolute/path." >&2
    exit 1
fi
# '..' is rejected per path COMPONENT and not as a substring: the character set
# above admits dots for good reason (config paths use them) and a directory
# named 'a..b' has to remain legal.
#
# This is not a vulnerability and must not be sold as one: this script runs as
# root by design, it does not confine $CONFIG_DIR to any tree that a '..' could
# escape from, and the value is written by the operator. What it leaves is a
# single directory with two spellings, both recorded in the deploy log: the one
# interpolated into the autostart_cmd remedy command, which is the one the
# operator copies and pastes, and the one that travels in CONFIG_DIR to the
# make line of step 4.
#
# The trailing '*/..' is enough without a leading '../*' because the absolute
# path guard already ran: the value starts with '/', so a '..' in the first
# position falls into '*/../*' anyway.
case "$CONFIG_DIR" in
    */../*|*/..)
        echo "ERROR: CONFIG_DIR does not accept '..' as a path component (got: '$CONFIG_DIR'). Pass the already resolved path with --config-dir /absolute/path, so that the same directory has a single spelling in the deploy log and in the autostart remedy command. A directory named 'a..b' is legal: the guard measures components, not substrings." >&2
        exit 1 ;;
esac

# The number of workers has to be an integer >= 1. Same idiom as load_env
# (setup-scripts/debian13-worker/lib.sh:187-192: `case *[!0-9]*` + `10#` so
# that '08' is not interpreted as octal). This is not theoretical defense: it
# feeds two `seq 1 "$WORKERS"` (the pre-swap validation and the recreation),
# and with a value <= 0 BOTH loops iterate zero times on GNU seq. The result
# would be the worst of all: swap done, no container recreated, exit 0 and not
# a single message; the workers would keep running on the bind mount of the old
# code (dead inodes, see the step 4 comment).
#
# It is a function and not one copy per source because the value can come from
# two places and both deserve the same rigor: the command line (the operator's
# inventory) and the host's setup.env WORKER_COUNT (step 2.5). This script does
# not control that file; validating it only when it arrives as an argument
# would be trusting the side that gets reviewed the least.
validate_workers() {
    local value="$1" source_label="$2"
    case "$value" in
        ''|*[!0-9]*)
            echo "ERROR: $source_label must be an integer >= 1 (got: '$value')." >&2
            return 1 ;;
    esac
    [ "$((10#$value))" -ge 1 ] \
        || { echo "ERROR: $source_label must be >= 1 (got: '$value')." >&2; return 1; }
}

# An explicit --workers is validated HERE, before touching anything on the
# host: it is an operator error and there is no need to even extract the
# tarball to detect it. The setup.env value, on the other hand, is read and
# validated in step 2.5, where there is already staging to clean up.
if [ -n "$WORKERS" ]; then
    validate_workers "$WORKERS" "--workers" || exit 1
    WORKERS="$((10#$WORKERS))"
    WORKERS_EXPLICIT=1
    # --workers together with --no-restart is a contradictory combination, and
    # it used to be silently discarded: the value is only consumed inside the
    # `if [ "$RESTART" -eq 1 ]` of step 4, so the script validated the number,
    # recreated nothing and exited 0 without mentioning it.
    # It does not abort (the command's result is correct: installing the code
    # and leaving every container alone is exactly what --no-restart asks for),
    # but it cannot stay mute either: the operator who passed the number
    # believes the deploy is going to recreate that many workers.
    if [ "$RESTART" -eq 0 ]; then
        echo "WARNING: $WORKERS worker(s) were requested with --workers together with --no-restart, which recreates no container: the number is ignored. Only the code is installed, nothing else. To recreate the workers, run again without --no-restart (then the number comes from WORKER_COUNT in the host's setup.env, or from --workers if you pass it)." >&2
    fi
else
    WORKERS_EXPLICIT=0
fi

# Layout of the fleet's env files inside $CONFIG_DIR. The single definition
# lives in setup-scripts/debian13-worker/lib.sh:worker_envfile, and it is the
# one run-fleet.sh uses to STAMP exactly the files this script consumes (the
# step 2.5 pre-swap gate and the --env-file of the `make up client` of step 4).
# This stub exists because the pre-swap gate runs BEFORE lib.sh is available:
# the only lib.sh reachable at that point is the one in the extracted tree,
# still unverified, and this script sources it on purpose only AFTER the swap
# (see the long comment in step 4). Without the stub, the path was rebuilt by
# hand in three places and a layout change in lib.sh became a silent drift: the
# gate would measure files that the fleet no longer stamps. The
# `source "$_lib"` of step 4 replaces it with the one from lib.sh, which is what
# is wanted: the production definition wins as soon as it is available.
worker_envfile() {
    local n="${1:-}" config_dir="$2"
    if [ -n "$n" ]; then printf '%s/env.%s\n' "$config_dir" "$n"; else printf '%s/env\n' "$config_dir"; fi
}

# Same stub rule for lib.sh:worker_config_make_args: step 4 needs it
# even on the branch that does not source lib.sh. A Makefile that resolves the
# worker env and pubkey from CONFIG_DIR (WORKER_ENV_HOST) gets no override;
# only an older one (a --ref rollback) gets WORKER_ENV/WORKER_PUBKEY, one per
# line, because its defaults point into client/.
worker_config_make_args() {
    local install_dir="$1" n="${2:-}" config_dir="$3"
    if grep -q '^WORKER_ENV_HOST ' "$install_dir/Makefile" 2>/dev/null; then
        return 0
    fi
    printf 'WORKER_ENV=%s\n' "$(worker_envfile "$n" "$config_dir")"
    printf 'WORKER_PUBKEY=%s/server-ed25519.pub\n' "$config_dir"
}

# Owner and mode of paths that root is going to source or in which it is going
# to leave code that it later executes. Same rule and same source as the origin
# guard of setup-scripts/debian13-worker/lib.sh: `ls -lnd` (it does not follow
# links; a '+' after the permissions marks an ACL), resolved by PATH like any
# other command in this script. The tests prepend a stub `ls` to PATH, because
# they cannot create root-owned files; no environment variable relaxes the
# check. Returns 1 and leaves the reason in ROOT_ONLY_REASON.
ROOT_ONLY_REASON=""
check_root_only() {  # $1=path  $2=required type: 'd' (directory) or '-' (regular file)
    local p="$1" want="$2" perms="" uid=""
    read -r perms _ uid _ < <(ls -lnd -- "$p" 2>/dev/null) || true
    if [ -z "$perms" ] || [ -z "$uid" ]; then
        ROOT_ONLY_REASON="cannot read the owner or the mode of $p"
        return 1
    fi
    if [ "${perms:0:1}" != "$want" ]; then
        ROOT_ONLY_REASON="$p is not a $([ "$want" = d ] && echo directory || echo 'regular file') (mode $perms; a symbolic link is not accepted)"
        return 1
    fi
    if [ "$uid" != 0 ] || [ "${perms:5:1}" = w ] || [ "${perms:8:1}" = w ] \
        || [ "${perms:10:1}" = + ]; then
        ROOT_ONLY_REASON="$p has owner uid $uid and mode $perms (required: owner root, no group or other write permission, no ACL)"
        return 1
    fi
}

# Chain of directories from $1 (absolute physical path) up to /, including $1.
check_root_only_ancestors() {  # $1=directory
    local d="$1"
    while :; do
        check_root_only "$d" d || return 1
        [ "$d" = / ] && break
        d="$(dirname "$d")"
    done
}

# Physical path of $1 even if it does not exist yet: resolves the deepest
# existing ancestor (links and '..' included) and appends the rest. Same
# function as _physical_path in lib.sh.
physical_path() {  # $1=absolute path
    local p="$1" rest=""
    while [ ! -d "$p" ]; do
        rest="/$(basename "$p")$rest"
        p="$(dirname "$p")"
    done
    p="$(cd "$p" && pwd -P)"
    printf '%s' "${p%/}$rest"
}

# True if $1 and $2 (physical paths) are the same or one contains the other.
paths_overlap() {  # $1 $2
    local a="${1%/}/" b="${2%/}/"
    case "$a" in "$b"*) return 0 ;; esac
    case "$b" in "$a"*) return 0 ;; esac
    return 1
}

# 1. Artifact integrity. Nothing on the host is touched before this.
#
# We do not use `sha256sum -c` / `shasum -c`: the native Darwin sha256sum
# (/sbin/sha256sum, different from the coreutils one) returns exit 0 with only
# a WARNING when the .sha256 line is malformed, instead of failing (verified by
# hand on the host where these bats run). Trusting the exit code of `-c` is
# implicit and depends on which sha256sum implementation wins the PATH. We
# compute the hash ourselves and compare it as text, which is explicit and
# portable across GNU and BSD/Darwin.
sha_file="$TARBALL.sha256"
[ -f "$sha_file" ] || { echo "ERROR: the sha256 file is missing at $sha_file." >&2; exit 1; }
#
# The three branches are explicit, as in make-release.sh (the other side of this
# same contract). The `else` that blindly fell back to `shasum` left a minimal
# Debian host without `perl` (shasum comes from the `perl` package, not
# `perl-base`) dying with a "command not found" and the exit 127 of `set -e`:
# the operator learned that "something" failed, not that the artifact's
# integrity verification never ran.
if command -v sha256sum >/dev/null 2>&1; then
    HASH_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    HASH_CMD="shasum -a 256"
else
    echo "ERROR: neither sha256sum nor shasum was found in PATH, so the integrity of $TARBALL cannot be verified; the installed code was not touched. Install coreutils (sha256sum) or perl (shasum) on this host and retry." >&2
    exit 1
fi
expected_sha="$(awk '{print $1}' "$sha_file")"
actual_sha="$($HASH_CMD "$TARBALL" | awk '{print $1}')"
[ -n "$expected_sha" ] && [ "$expected_sha" = "$actual_sha" ] \
    || { echo "ERROR: the sha256 of $TARBALL does not match (expected $expected_sha, computed $actual_sha); the installed code was not touched." >&2; exit 1; }

# 1.5. Host knobs that must be resolved BEFORE any path is computed.
# They go here, and not in the step 2.5 gate, because $INSTALL_DIR is consumed
# on the next lines ($staging and $previous derive from it): resolving it in
# 2.5 would be too late and the swap would end up in the wrong directory.
#
# This does not move the gate: step 2.5 is still the one that ABORTS if
# setup.env is missing, with its own message. Here the file is read only if it
# exists and is readable; otherwise the defaults apply and the gate further
# down still stops before $INSTALL_DIR is touched.
setup_env="$CONFIG_DIR/setup.env"

# Reads one variable from the host setup.env by sourcing it in a separate bash
# (never in this shell: setup.env is operator config and could overwrite any
# variable of this script) and leaves the result in SETUP_ENV_VALUE. It returns
# through a global variable and not through stdout because it has to be able to
# abort, and a `$(...)` would run it in a subshell where `exit 1` stops nobody.
#
# Three states, not two. The `|| return 0` it used to have collapsed the last
# two into an empty string, and with that a corrupt setup.env passed the whole
# gate: WORKER_MEMORY was never validated (the failure only showed up in
# `podman run`, with the previous container already removed) and a deliberate
# EGRESS_ISOLATION=0 on the host silently fell back to the Makefile's `?= 1`.
#   - file missing or unreadable: step 2.5 aborts; this function is not called.
#   - file present but unsourceable: sentinel, and it is FATAL. The Makefile
#     (114-133) resolves the same case with a $(warning) because `make help`
#     cannot abort; here the whole purpose is to be the pre-swap gate, so
#     aborting is the right thing.
#   - variable not declared in a file that WAS sourced: empty string, and that
#     is legitimate (it is what a host that does not declare it does).
# The sentinel is the only way to tell the last two apart: the empty string
# alone cannot.
SETUP_ENV_MISREAD="__setup_env_misread__"
SETUP_ENV_VALUE=""
read_setup_env_var() {
    local name="$1" raw setup_env_dir
    # Root sources this file: only if no other user can change it (same rule as
    # load_env in lib.sh). It is checked on EVERY read, not once at the start:
    # the file can appear or change between one read and the next. The physical
    # path that was just verified is the one that gets sourced.
    setup_env_dir="$(cd "$(dirname "$setup_env")" 2>/dev/null && pwd -P)" || setup_env_dir=""
    if [ -z "$setup_env_dir" ]; then
        ROOT_ONLY_REASON="cannot resolve the directory of $setup_env"
    fi
    if [ -z "$setup_env_dir" ] || ! check_root_only_ancestors "$setup_env_dir" \
        || ! check_root_only "$setup_env_dir/$(basename "$setup_env")" -; then
        [ -n "${staging:-}" ] && rm -rf "$staging"
        echo "ERROR: not sourcing $setup_env as root: $ROOT_ONLY_REASON. Leave it as the installer expects: chown root:${WORKER_USER:-orcaster} '$setup_env' && chmod 0640 '$setup_env' (and $CONFIG_DIR as 0750 root:${WORKER_USER:-orcaster}). The installed code was not touched." >&2
        exit 1
    fi
    # `set -euo pipefail` AND the source as a STANDALONE statement, not as the
    # left side of an `&&`. Both together, because each one without the other
    # leaves the probe inert:
    #   - without errexit, a top-level command that fails IN THE MIDDLE of the
    #     file does not abort the source (`.` returns the status of the LAST
    #     command, which is usually an assignment and gives 0), so the probe
    #     said "ok" and load_env died later, with the swap already done.
    #     Measured: a setup.env with a failing `grep` in the middle passed the
    #     whole gate and load_env exited rc=2.
    #   - with errexit but keeping the `&&`, nothing changes in production:
    #     bash suspends errexit on the whole left side of an `&&`, and that
    #     suspension reaches the sourced file. Measured on Debian 13 (bash 5.2,
    #     the host's) the same fixture kept passing with `&&`; on macOS (bash
    #     3.2) it aborted, so the half fix looked green in the bats and was
    #     broken on the host. It is the same trap the lib.sh probe further
    #     down already documents.
    # This way the probe is equivalent to what load_env does (lib.sh sources
    # with set -e active), which is exactly what its message promises.
    raw="$(bash -c 'set -euo pipefail; . "$1" >/dev/null 2>&1; printf %s "${!2-}"' _ \
        "$setup_env_dir/$(basename "$setup_env")" "$name" 2>/dev/null \
        || printf %s "$SETUP_ENV_MISREAD")"
    if [ "$raw" = "$SETUP_ENV_MISREAD" ]; then
        # $staging may not exist yet (this function also runs in step 1.5,
        # before the extraction): it is cleaned up only if already created.
        [ -n "${staging:-}" ] && rm -rf "$staging"
        echo "ERROR: $setup_env exists but bash could not source it (invalid syntax, or a top-level command that fails). Without being able to read it, the memory cap and the egress isolation that the recreation will use cannot be validated, and load_env will die for the same reason on the next 06-autostart.sh. Check it with: bash -n '$setup_env'. The installed code was not touched." >&2
        exit 1
    fi
    SETUP_ENV_VALUE="$raw"
}

# Only under RESTART=1: with --no-restart (first delivery, Bootstrap) there is
# no setup.env to read yet, and that path still needs an explicit --install-dir
# if the host does not use the default path. After that it comes from the file.
if [ "$RESTART" -eq 1 ] && [ -f "$setup_env" ] && [ -r "$setup_env" ]; then
    # read_setup_env_var checks the owner and mode of setup.env before sourcing it.
    read_setup_env_var INSTALL_DIR
    host_install_dir="$SETUP_ENV_VALUE"
    if [ -n "$INSTALL_DIR" ]; then
        # The two paths are compared WITHOUT a trailing slash: '/opt/x/' and
        # '/opt/x' are the same directory, and a "the host and the inventory
        # disagree" warning about a difference that does not exist is exactly
        # the kind of message that states something false. The normalization of
        # $INSTALL_DIR runs further down (it needs the value already resolved
        # by this same block), so this comparison normalizes on its own and
        # does NOT overwrite either value: the WARNING has to show the operator
        # what each side declares, exactly as written.
        if [ -n "$host_install_dir" ] \
            && [ "$(rtrim_slashes "$host_install_dir")" != "$(rtrim_slashes "$INSTALL_DIR")" ]; then
            echo "WARNING: installing into $INSTALL_DIR was requested via --install-dir, but this host declares INSTALL_DIR=$host_install_dir in $setup_env. The explicit one wins. If the host is the one that is wrong, fix its setup.env: it is the path that cloud-init-worker.sh, run-fleet.sh and 06-autostart.sh read." >&2
        fi
    elif [ -n "$host_install_dir" ]; then
        INSTALL_DIR="$host_install_dir"
    fi
    read_setup_env_var WORKER_USER
    host_worker_user="$SETUP_ENV_VALUE"
    if [ -n "$WORKER_USER" ]; then
        if [ -n "$host_worker_user" ] && [ "$host_worker_user" != "$WORKER_USER" ]; then
            echo "WARNING: user $WORKER_USER will be used (environment override), but this host declares WORKER_USER=$host_worker_user in $setup_env. The explicit one wins." >&2
        fi
    elif [ -n "$host_worker_user" ]; then
        WORKER_USER="$host_worker_user"
    fi
    read_setup_env_var SOURCE_DIR
    host_source_dir="$SETUP_ENV_VALUE"
    if [ -n "$SOURCE_DIR" ]; then
        if [ -n "$host_source_dir" ] \
            && [ "$(rtrim_slashes "$host_source_dir")" != "$(rtrim_slashes "$SOURCE_DIR")" ]; then
            echo "WARNING: the root copy of the release is placed at $SOURCE_DIR (--source-dir or environment variable), but this host declares SOURCE_DIR=$host_source_dir in $setup_env. The explicit one wins." >&2
        fi
    elif [ -n "$host_source_dir" ]; then
        SOURCE_DIR="$host_source_dir"
    fi
fi
# The same defaults as load_env (setup-scripts/debian13-worker/lib.sh:89 and
# :91), so that this script and the provisioning suite resolve the same way.
INSTALL_DIR="${INSTALL_DIR:-/opt/orcaster}"
WORKER_USER="${WORKER_USER:-orcaster}"

# The same five guards as $CONFIG_DIR, and for worse reasons: $INSTALL_DIR is
# the path that the swap REPLACES. It goes here and not next to the CONFIG_DIR
# ones because the value can arrive through two channels and the second one is
# resolved only above: the operator's --install-dir and the INSTALL_DIR in the
# host's setup.env, which this script does not control. Validating only the
# argument would mean trusting the side that gets reviewed the least, which is
# exactly the one that cloud-init stamps on its own.
#
# It also goes BEFORE the `dirname` on the line below, because $staging and
# $previous come from it: with a relative path both would be resolved against
# the cwd of this script (that of the `sudo`, not the operator's) and the
# `rm -rf` would land next to the wrong directory.
#
# The trailing slash is not cosmetic here: with INSTALL_DIR='/opt/orcaster/'
# and that path being a symlink, `[ -L ]` is FALSE (the trailing slash resolves the link) and
# `[ -e ] && [ ! -d ]` passes too, so BOTH step 3 guards were inert; the
# `rm -rf "$previous"` took away the .orcaster.old (the manual rollback that
# this script promises) and only then did the `mv` die with the bare coreutils
# message, whose rename() rejects a trailing slash.
case "$INSTALL_DIR" in
    *[!A-Za-z0-9/._-]*)
        echo "ERROR: INSTALL_DIR only allows letters, digits and the characters / . _ - (got: '$INSTALL_DIR'). Fix INSTALL_DIR in $setup_env, or pass the path with --install-dir." >&2
        exit 1 ;;
esac
case "$INSTALL_DIR" in
    /*) ;;
    *)
        echo "ERROR: INSTALL_DIR must be an absolute path (got: '$INSTALL_DIR'). A relative path would be resolved against the cwd of this script, not the operator's. Fix INSTALL_DIR in $setup_env, or pass it with --install-dir /absolute/path." >&2
        exit 1 ;;
esac
INSTALL_DIR="$(rtrim_slashes "$INSTALL_DIR")"

# The two guards below go AFTER rtrim_slashes, like those of CONFIG_DIR (see
# the earlier block): they measure the NORMALIZED value, because
# '--install-dir //' reads as '//' before the rtrim and as '/' after it, so a comparison made before
# normalizing would be inert on exactly that input.
#
# The root matters more here than in CONFIG_DIR: $parent comes from
# $INSTALL_DIR with `dirname /` = '/', and from there $staging='//.orcaster.new'
# and $previous='//.orcaster.old'. Without this guard the script would extract
# the release into //.orcaster.new and run `rm -rf //.orcaster.old` BEFORE the
# `mv /` dies with EBUSY (the root is a mount point), leaving a message that
# blames permissions or a read-only remount instead of naming the root. It is
# not destructive (the root is neither moved nor deleted), but it is the same
# class of message that states the wrong cause, and it is what this guard
# prevents.
if [ "$INSTALL_DIR" = "/" ]; then
    echo "ERROR: INSTALL_DIR cannot be the root '/' (got: '$INSTALL_DIR'). It is the path that the swap replaces with a mv: with the root the release would be extracted into //.orcaster.new and rm -rf //.orcaster.old would run before the mv of the root fails. The default is /opt/orcaster: fix INSTALL_DIR in $setup_env, or pass a directory of your own with --install-dir /absolute/path." >&2
    exit 1
fi
# '..' is rejected per path COMPONENT and not as a substring: a directory
# named 'a..b' must stay legal. It is not a bypass of the step 3 usage guards
# (`[ -L ]` looks at the last component and an intermediate '..' is resolved as
# a normal traversal); what it pollutes is the host/inventory disagreement
# WARNING about a difference that does not exist, and the remedy
# commands that the operator copies and pastes (the `mv '$previous'
# '$INSTALL_DIR'`, the `chown -R` and the autostart_cmd), which would carry the
# unresolved path. The trailing '*/..' is enough without a leading '../*'
# because the absolute-path guard already ran: the value starts with '/', so a
# '..' in first position falls under '*/../*' anyway.
case "$INSTALL_DIR" in
    */../*|*/..)
        echo "ERROR: INSTALL_DIR does not allow '..' as a path component (got: '$INSTALL_DIR'). Pass the already resolved path with --install-dir /absolute/path, or fix INSTALL_DIR in $setup_env, so that the host/inventory disagreement warning and the remedy commands in the log show a single path. A directory named 'a..b' is legal: the guard measures components, not substrings." >&2
        exit 1 ;;
esac

parent="$(dirname "$INSTALL_DIR")"
staging="$parent/.orcaster.new"
previous="$parent/.orcaster.old"

# 1.6. SOURCE_DIR, the root copy of the release (see --help). It is validated
# here, before extracting anything: a misplaced SOURCE_DIR cannot leave the host
# with INSTALL_DIR replaced and the root suite without a copy, nor leave code
# that root executes in a place that another user can write. Same format guards
# as INSTALL_DIR, and in addition: it is not a link, its parent exists, and the
# parent and each ancestor (resolved physically) are owned by root without group
# or other write, and it does not overlap INSTALL_DIR, the staging or the backup.
SOURCE_DIR="${SOURCE_DIR:-/root/orcaster}"
case "$SOURCE_DIR" in
    *[!A-Za-z0-9/._-]*)
        echo "ERROR: SOURCE_DIR only allows letters, digits and the characters / . _ - (got: '$SOURCE_DIR'). Pass it with --source-dir, or fix SOURCE_DIR in $setup_env. The installed code was not touched." >&2
        exit 1 ;;
esac
case "$SOURCE_DIR" in
    /*) ;;
    *)
        echo "ERROR: SOURCE_DIR must be an absolute path (got: '$SOURCE_DIR'). Pass it with --source-dir /absolute/path. The installed code was not touched." >&2
        exit 1 ;;
esac
SOURCE_DIR="$(rtrim_slashes "$SOURCE_DIR")"
case "$SOURCE_DIR" in
    /|*/../*|*/..|*/.|*/./*)
        echo "ERROR: SOURCE_DIR cannot be the root nor have '.' or '..' as a component (got: '$SOURCE_DIR'). Pass the already resolved path with --source-dir /absolute/path. The installed code was not touched." >&2
        exit 1 ;;
esac
if [ -L "$SOURCE_DIR" ]; then
    echo "ERROR: SOURCE_DIR ($SOURCE_DIR) is a symbolic link (it points to $(readlink "$SOURCE_DIR")). The root copy of the release is replaced with a mv and the lib.sh that root sources comes from there: declare the real path with --source-dir or in SOURCE_DIR of $setup_env. The installed code was not touched." >&2
    exit 1
fi
if [ -e "$SOURCE_DIR" ] && [ ! -d "$SOURCE_DIR" ]; then
    echo "ERROR: SOURCE_DIR ($SOURCE_DIR) exists and is not a directory. Check what it is before deleting it, move it out of the way or pass another path with --source-dir. The installed code was not touched." >&2
    exit 1
fi
if [ -e "$SOURCE_DIR/.git" ]; then
    echo "ERROR: SOURCE_DIR ($SOURCE_DIR) is a git checkout (it has .git): this script did not leave it there and replacing it would delete that checkout. Pass another path with --source-dir (or SOURCE_DIR in $setup_env). The installed code was not touched." >&2
    exit 1
fi
source_parent="$(dirname "$SOURCE_DIR")"
if [ ! -d "$source_parent" ]; then
    echo "ERROR: the parent directory of SOURCE_DIR does not exist ($source_parent). Create it as root (owned by root, without group or other write) or pass another path with --source-dir. The installed code was not touched." >&2
    exit 1
fi
source_parent="$(cd "$source_parent" && pwd -P)"
if ! check_root_only_ancestors "$source_parent"; then
    echo "ERROR: the root copy of the release cannot live at $SOURCE_DIR: $ROOT_ONLY_REASON. Root runs the provisioning suite from there, so no other user may be able to change that directory or the ones above it. Pass another path with --source-dir (the default is /root/orcaster). The installed code was not touched." >&2
    exit 1
fi
# Physical path: it is the one that gets sourced (lib.sh requires that the path
# it is invoked through matches the physical one) and the one printed as the
# origin of the suite.
SOURCE_DIR="${source_parent%/}/$(basename "$SOURCE_DIR")"
source_new="$SOURCE_DIR.new"
source_old="$SOURCE_DIR.old"
for _src in "$SOURCE_DIR" "$source_new" "$source_old"; do
    for _other in "$INSTALL_DIR" "$staging" "$previous"; do
        if paths_overlap "$_src" "$(physical_path "$_other")"; then
            echo "ERROR: SOURCE_DIR ($SOURCE_DIR) overlaps with $_other (INSTALL_DIR is $INSTALL_DIR). INSTALL_DIR stays owned by the worker user, and the staging and the backup come from its parent directory: the root copy has to be outside all three, and none of them inside it. Pass another path with --source-dir (the default is /root/orcaster). The installed code was not touched." >&2
            exit 1
        fi
    done
done

# 1.7. The parent directory of INSTALL_DIR must be root-only: the release is
# extracted there ($staging), the root copy comes from it (the cp -a below), and
# the backup ($previous) stays there. A user who could write in that directory
# would change what was extracted before root copies it to SOURCE_DIR and then
# executes it. If the parent does not exist yet (the mkdir -p below creates it,
# as root), its deepest existing ancestor is measured.
parent_check="$(physical_path "$parent")"
while [ ! -d "$parent_check" ]; do
    parent_check="$(dirname "$parent_check")"
done
if ! check_root_only_ancestors "$parent_check"; then
    echo "ERROR: the parent directory of INSTALL_DIR ($parent) must be root-only: $ROOT_ONLY_REASON. The release is extracted there before being copied to SOURCE_DIR ($SOURCE_DIR), and another user with write access there could change what was extracted before root copies it. Pass an INSTALL_DIR under a root directory with --install-dir (the default is /opt/orcaster). The installed code was not touched." >&2
    exit 1
fi

# 2. Extraction into a new directory. A tar that fails halfway dies here, with
# the installed code intact.
rm -rf "$staging"
mkdir -p "$staging"
tar xzf "$TARBALL" -C "$staging" \
    || { rm -rf "$staging"; echo "ERROR: extraction failed; the installed code was not touched." >&2; exit 1; }
# No group or other write in what was extracted. make-release.sh already
# builds 0755/0644, but tar as root keeps the modes that the tarball carries,
# and one built another way (git archive with its tar.umask 0002 by default, a
# hand-made tar) would leave INSTALL_DIR group-writable while owned by root, on
# the first delivery, and would give a 0775 parent to a misplaced --source-dir
# inside it. chmod -R does not follow the links of the tree.
chmod -R go-w "$staging" \
    || { rm -rf "$staging"; echo "ERROR: could not remove group and other write from what was extracted (chmod -R go-w $staging). The installed code was not touched." >&2; exit 1; }

# Tarball prefix: `orcaster/`. Anything else is a foreign artifact. What is
# inside (the CLI, the Python distributions) travels as is in the tree: this
# script does not install or link any binary.
if [ -d "$staging/orcaster" ]; then
    extracted="$staging/orcaster"
else
    rm -rf "$staging"
    echo "ERROR: the tarball does not have the orcaster/ prefix. The installed code was not touched." >&2
    exit 1
fi
[ -f "$extracted/RELEASE" ] \
    || { rm -rf "$staging"; echo "ERROR: the tarball has no RELEASE; invalid artifact." >&2; exit 1; }

new_release="$(cat "$extracted/RELEASE")"

# The file EXISTING is not enough: a zero-byte or truncated RELEASE (an
# interrupted scp over the working tree the tarball comes from, a
# make-release.sh interrupted before writing the manifest) installed with exit 0
# and left a host that cannot declare which code it runs. read_release_commit
# from lib.sh treats a RELEASE without a `commit=` line as "no installed code"
# and returns rc=1, so the damage shows up later and far away: should_bootstrap
# in cloud-init-worker.sh reports "bootstrap" on a healthy host (and redoes the
# whole provisioning) and run-all-worker.sh cannot name the provisioned release.
#
# The expression is the SAME as read_release_commit
# (setup-scripts/debian13-worker/lib.sh) and matches the manifest that
# make-release.sh stamps, the three sides of the same contract. It is
# replicated inline and lib.sh is NOT sourced to reuse it: at this point the
# only reachable lib.sh is the one in the freshly extracted tree, still
# unverified, and this script sources it on purpose only AFTER the swap (see
# the long comment of step 4). Entrusting the artifact check to the artifact's
# own code would be letting the suspect sign its own alibi.
new_release_commit="$(sed -n '/^commit=/{s/^commit=//p;q;}' "$extracted/RELEASE")"
[ -n "$new_release_commit" ] \
    || { rm -rf "$staging"; echo "ERROR: the RELEASE of the tarball declares no commit (empty or truncated file); invalid artifact. With that manifest, read_release_commit from lib.sh returns rc=1 and should_bootstrap would report 'bootstrap' on a healthy host, redoing the whole provisioning, while run-all-worker.sh cannot say which code is installed. Rebuild the tarball with ops/release/make-release.sh. The installed code was not touched." >&2; exit 1; }

# 2.5. Pre-swap validation of the HOST STATE (distinct from the integrity of
# the artifact above: here it is not the tarball that can be wrong, it is the
# host that may lack what step 4 (recreation) is going to need).
# Without this, a host not migrated to $CONFIG_DIR, a --workers that does not
# match the real number of workers on that host, or a user that does not exist
# yet aborted only in step 4, with the swap ALREADY done and the old container
# running over a moved inode: exactly the state that the header invariant
# exists to avoid. They go here, before touching $INSTALL_DIR.
#
# Gated entirely by RESTART=1: setup.env, env.N, the pubkey, the user and
# EGRESS_ISOLATION are only used further down, inside the `if [ "$RESTART" -eq 1
# ]` of step 4; with --no-restart none of this is read or needed. This
# includes the setup.env/env.N/pubkey check (not only the user one): it is
# precisely the path of the first delivery on a new host (Bootstrap,
# setup-scripts/debian13-worker/README.md), which runs with --no-restart
# precisely because $CONFIG_DIR and the user "$WORKER_USER" do not exist yet,
# and it has to keep working without demanding in advance what this same
# command does not have yet.
egress_isolation=""
worker_ro_mounts=""

# Reads an env file (podman --env-file format: KEY=VALUE per line, '#'
# comments) from stdin and returns 3 if any variable in the list of variables
# that require isolation has a value and ORCH_ENFORCE_ISOLATION is not set to
# a true value, 0 otherwise. The list comes from the LAST ORCH_ISOLATION_REQUIRED_ENV
# line of the same file (names separated by commas or spaces, each
# [A-Za-z_][A-Za-z0-9_]*, the worker's grammar); without that line, or with it
# empty, there is nothing to protect. Returns 4 if the list is malformed (fails
# closed, like the worker). With 3 it prints to stdout the names found,
# separated by commas, and nothing else; with any other code it prints nothing.
# Any other code is a failure of awk itself (missing binary, runtime error,
# unreadable stdin) and the caller treats it as a failure, never as "clean": a
# broken awk must not let the deploy through. Values are compared inside awk
# and never leave it. LC_ALL=C keeps awk on bytes, with no multibyte
# conversion (macOS awk with a UTF-8 locale exits with code 2 on an invalid
# byte and repeats the line on stderr), and stderr goes to /dev/null in case
# another awk prints its input when it fails. The rules and their rationale
# are in the step 2.5 gate, which is what uses it.
isolation_vars_status() {
    LC_ALL=C awk '
        {
            line = $0
            sub(/^[ \t]+/, "", line)
            if (line ~ /^#/) next
            eq = index(line, "=")
            if (eq == 0) next
            name = substr(line, 1, eq - 1)
            val = substr(line, eq + 1)
            vals[name] = val
            if (toupper(name) == "ORCH_ISOLATION_REQUIRED_ENV") list = val
            else if (toupper(name) == "ORCH_ENFORCE_ISOLATION") flag = val
        }
        END {
            gsub(/[,[:space:]]+/, " ", list)
            gsub(/^ +| +$/, "", list)
            n = split(list, names, " ")
            for (i = 1; i <= n; i++)
                if (names[i] !~ /^[A-Za-z_][A-Za-z0-9_]*$/) exit 4
            found = ""
            for (i = 1; i <= n; i++) {
                v = (names[i] in vals) ? vals[names[i]] : ""
                gsub(/^[ \t\r]+|[ \t\r]+$/, "", v)
                if (v != "" && index("," found ",", "," names[i] ",") == 0)
                    found = (found == "" ? names[i] : found "," names[i])
            }
            if (found == "") exit 0
            sub(/\r$/, "", flag)
            if (tolower(flag) ~ /^(1|true|yes|on|t|y)$/) exit 0
            print found
            exit 3
        }
    ' 2>/dev/null
}

# Aborts the deploy if isolation_vars_status did not exit clean. $1 is its
# exit code, $2 the origin the messages name (never the value), $3 why that
# origin would leave the workers in a crash loop and $4 the names found
# (only with code 3).
isolation_vars_gate() {
    case "$1" in
        0) return 0 ;;
        3)
            rm -rf "$staging"
            echo "ERROR: $2 declares with a value a variable from the list of variables that require isolation ($4) and does not declare ORCH_ENFORCE_ISOLATION with a true value (1, true, yes, on, t, y, without quotes or surrounding spaces). The new release's worker refuses to start that way: $3 Remedy (same as the worker's ERROR): either move $4 to the workspace's central secret and remove it from the worker's environment (tasks that need it must declare it in task_env; first drain or recreate the queued tasks without it, or they will run without it), or drain the queue of tasks without task_env and set ORCH_ENFORCE_ISOLATION=true in $setup_env, either in the ORCH_ENFORCE_ISOLATION variable or inside EXTRA_ENV (in only one of the two places), and run run-fleet.sh, which re-stamps the env.N files. Then retry the deploy. The installed code was not touched." >&2
            exit 1
            ;;
        4)
            rm -rf "$staging"
            echo "ERROR: $2 has a malformed ORCH_ISOLATION_REQUIRED_ENV list (expected names [A-Za-z_][A-Za-z0-9_]* separated by commas or spaces, without quotes or patterns). The worker refuses to start with it and the gate cannot know which variables to protect, so it aborts. Fix ISOLATION_REQUIRED_ENV in $setup_env and run run-fleet.sh. The installed code was not touched." >&2
            exit 1
            ;;
        *)
            rm -rf "$staging"
            echo "ERROR: could not evaluate $2 for variables from the isolation list without ORCH_ENFORCE_ISOLATION (the read or awk exited with code $1). Without that evaluation the gate cannot rule out that the new release's workers end up in a crash loop, so it aborts instead of continuing. Check that awk is in PATH and that the file can be read. The installed code was not touched." >&2
            exit 1
            ;;
    esac
}

if [ "$RESTART" -eq 1 ]; then
    # ORDER OF THE BLOCK, deliberate and different from what it used to be:
    # setup.env goes FIRST because WORKER_COUNT comes from it, and without the
    # number of workers there is no way to know which env.N files to require.
    # Then, in this order: the EXISTENCE of each env.N, the probe for the extra
    # env, the EXISTENCE of the pubkey, the user, its uid and its home, and only
    # then the probes for READABILITY AS THE WORKER (setup.env, each env.N and
    # the pubkey). They cover the format of EGRESS_ISOLATION and WORKER_MEMORY,
    # the protected variables without isolation and WORKER_RO_MOUNTS. Do not
    # reorder it back.
    #
    # The readability probes go at the end, AFTER `id -u`, for the same reason
    # documented for the setup.env probe below: without the user, `sudo -u`
    # would fail for another cause and the message would lie. And they go after
    # the existence checks because the message from those is more specific
    # ("env.N is missing ..."): a file that is not there must abort for being
    # absent, not for being unreadable.
    #
    # setup.env is the only file the tarball can NEVER provide (it is in
    # .gitignore, `git archive` does not package it) and the one on which the
    # two things step 4 needs depend. Without it, on a host that otherwise
    # looks migrated (it has env.N and the pubkey), the deploy passed the gate
    # and broke AFTER the swap, in two places and both silent: (a)
    # SETUP_ENV_FILE in the Makefile falls back to the tree's setup.env, which
    # the tarball does not ship, so WORKER_MEMORY ends up empty and the
    # container is recreated without --memory; (b) `make up client` calls
    # autostart-sync-client, which runs 06-autostart.sh, whose load_env dies
    # because setup.env is absent, and the Makefile used to swallow that
    # failure with an `|| printf`: the systemd unit was left stopped, the
    # container ran anyway, and the host stopped surviving a reboot without
    # anything saying so. Today that failure is no longer silent (the unit
    # check in step 4 reports it), but it only arrives after the swap: the gate
    # is still what prevents it.
    # $setup_env and read_setup_env_var are defined in step 1.5: INSTALL_DIR
    # and WORKER_USER also come from that file, and they must be resolved
    # before computing the swap paths. This check is the one that ABORTS on its
    # absence, and it still lives here, in the gate.
    if [ ! -f "$setup_env" ] || [ ! -r "$setup_env" ]; then
        rm -rf "$staging"
        echo "ERROR: $setup_env is missing (or cannot be read); it is the host config and the tarball never ships it. Without that file the container would be recreated without a memory cap and the autostart systemd unit would be left stopped, both silently. If the host has not yet migrated its config to $CONFIG_DIR, move setup.env and the env.N files there (setup-scripts/debian13-worker/README.md, Bootstrap section); if this is the first delivery to a new host, run with --no-restart (setup-scripts/debian13-worker/README.md, Bootstrap section). The installed code was not touched." >&2
        exit 1
    fi

    # Number of workers: the HOST is the source of truth. WORKER_COUNT in its
    # own setup.env is what run-fleet.sh:108 iterates to stamp the env.N files
    # and create the containers, so it is also what has to be recreated.
    # Keeping the number in the operator's deploy.local.env as well left two
    # copies drifting with nothing to reconcile them, and the drift was only
    # caught by the extra-env probe below, which goes quiet as soon as someone
    # prunes the old env.N files. The operator's inventory still counts, but
    # as an explicit override, not as a silent default.
    read_setup_env_var WORKER_COUNT
    host_workers="$SETUP_ENV_VALUE"
    if [ -n "$host_workers" ]; then
        host_workers_src="WORKER_COUNT from $setup_env"
        if ! validate_workers "$host_workers" "WORKER_COUNT in $setup_env"; then
            rm -rf "$staging"
            echo "       It is the same format that load_env requires (setup-scripts/debian13-worker/lib.sh) when the provisioning suite runs on this host: with that value, run-fleet.sh would not start either. Check setup-scripts/debian13-worker/README.md, Bootstrap section. The installed code was not touched." >&2
            exit 1
        fi
        host_workers="$((10#$host_workers))"
    else
        # A sourceable setup.env that does not declare WORKER_COUNT: load_env
        # defaults it to 1 (setup-scripts/debian13-worker/lib.sh:101,
        # `WORKER_COUNT="${WORKER_COUNT:-1}"`), so this script defaults the
        # same way. Mirroring it is not cosmetic: hosts that are already in
        # steady state were provisioned with that default and do not declare
        # the variable; requiring it here would break them with nothing wrong
        # on their side.
        host_workers=1
        host_workers_src="the load_env default (lib.sh:101); $setup_env does not declare WORKER_COUNT"
    fi
    if [ "$WORKERS_EXPLICIT" -eq 1 ]; then
        workers_src="--workers (command line)"
        # The explicit value wins: the operator is in charge. But not silently,
        # because a difference here means the inventory and the host do not say
        # the same thing and one of them is wrong.
        if [ "$WORKERS" -ne "$host_workers" ]; then
            echo "WARNING: $WORKERS worker(s) were requested via --workers (command line, or WORKER_COUNT_<host> from deploy.local.env), but this host declares $host_workers ($host_workers_src). The explicit value wins: $WORKERS will be recreated. If the host is the one that is wrong, fix WORKER_COUNT in $setup_env; if it is the inventory, fix deploy.local.env." >&2
        fi
    else
        WORKERS="$host_workers"
        workers_src="$host_workers_src"
    fi

    for i in $(seq 1 "$WORKERS"); do
        env_i="$(worker_envfile "$i" "$CONFIG_DIR")"
        [ -f "$env_i" ] \
            || { rm -rf "$staging"; echo "ERROR: $env_i is missing (worker $i/$WORKERS, number taken from $workers_src). Does the number not match the real number of workers on this host, or has the host not yet migrated its config to $CONFIG_DIR? Check setup-scripts/debian13-worker/README.md, Bootstrap section. The installed code was not touched." >&2; exit 1; }
    done
    # The case above covers a number HIGHER than what the host has configured
    # (env.N is missing, abort). The opposite case is silent: only worker 1
    # would be recreated and workers 2..N would be left running on the old
    # code's bind mount (dead inodes after the swap, see the step 4 comment)
    # with the script returning exit 0 and saying nothing. Since the number
    # comes from the host's setup.env, this probe also catches the new case: a
    # WORKER_COUNT that has gone stale relative to the env.N files the host
    # really has. It warns without aborting, because a host can legitimately be
    # shrinking its fleet (and then the leftover is an old, unpruned env.N);
    # aborting the whole deploy for that reason would be worse than warning. It
    # is up to the operator to confirm the number is the one they want.
    next_env="$(worker_envfile "$((WORKERS+1))" "$CONFIG_DIR")"
    if [ -f "$next_env" ]; then
        echo "WARNING: $WORKERS worker(s) are being recreated, but $next_env exists: this host has more env.N files than the workers this deploy will recreate, and those left unrecreated keep running on the previous code. If this is not intentional, fix WORKER_COUNT in $setup_env (it is what run-fleet.sh iterates on this host) or pass an explicit --workers with the real number. If the host is deliberately shrinking its fleet, the leftover is the old, unpruned env.N." >&2
    fi
    [ -f "$CONFIG_DIR/server-ed25519.pub" ] \
        || { rm -rf "$staging"; echo "ERROR: $CONFIG_DIR/server-ed25519.pub is missing. The installed code was not touched." >&2; exit 1; }
    if ! id -u "$WORKER_USER" >/dev/null 2>&1; then
        rm -rf "$staging"
        echo "ERROR: user $WORKER_USER does not exist on this host. If this is the first delivery to a new host (no user '$WORKER_USER' yet), run with --no-restart (see setup-scripts/debian13-worker/README.md, Bootstrap section). The installed code was not touched." >&2
        exit 1
    fi
    # $uid and $home are resolved HERE, in the pre-swap gate, and not in step 4
    # where they are consumed: both are host state the recreation needs, just
    # like the user itself, so their absence must abort BEFORE touching
    # $INSTALL_DIR. run_as_worker passes them to every `podman`/`systemctl
    # --user`/`make` of the deploy (HOME=$home and XDG_RUNTIME_DIR=/run/user/$uid).
    #
    # The home is not taken as good for free: getent can return an empty field
    # 6 (a user created by hand without a home, or an NSS that does not provide
    # it) and with an empty HOME rootless podman resolves its storage against
    # /.local/share/containers, which means it fails or, worse, creates a new,
    # empty storage where the worker image is not present. That happened INSIDE
    # the post-swap window and the operator only saw podman's message, with no
    # hint that the home had arrived empty.
    #
    # The getent goes in its own `if` and not in a bare assignment: with
    # `pipefail` active, `home="$(getent ... | cut ...)"` aborts the script with
    # no message at all if getent exits non-zero.
    uid="$(id -u "$WORKER_USER")"
    if ! home="$(getent passwd "$WORKER_USER" | cut -d: -f6)"; then
        rm -rf "$staging"
        echo "ERROR: 'getent passwd $WORKER_USER' failed on this host, so I cannot resolve the home of the user that runs the worker. Without it, all of the deploy's rootless podman and systemd --user would run with an empty HOME. The installed code was not touched." >&2
        exit 1
    fi
    case "$home" in
        /*) ;;
        *)
            rm -rf "$staging"
            echo "ERROR: the home of $WORKER_USER is not an absolute path (field 6 of 'getent passwd $WORKER_USER' is: '$home'). Rootless podman resolves its storage against HOME and systemd --user needs it too: with that value the deploy would fail after the swap, with a podman message that does not name the cause. Give the user a valid home (usermod -d /home/$WORKER_USER -m $WORKER_USER) and retry. The installed code was not touched." >&2
            exit 1 ;;
    esac
    # A well-formed absolute path is not enough either, because field 6 can
    # name a directory that does not exist on disk: a user created with
    # `useradd` without -m, a /home deleted by hand, a /home that was not
    # mounted. That case passed both guards above, and it is WORSE than the
    # empty home: rootless podman resolves its storage against an absent HOME,
    # creates a new EMPTY one where the worker image is not present, and that
    # happens inside the post-swap window, with the new code already installed
    # and the old container destroyed. The operator only sees podman's message,
    # which does not name the cause.
    #
    # There is no conflict with bootstrapping a new host: this whole block
    # lives inside `if [ "$RESTART" -eq 1 ]` and the first delivery runs with
    # --no-restart, which never reaches here. Under RESTART=1 the user already
    # exists and its containers are already running, so a missing home is host
    # damage and never a legitimate first delivery.
    if [ ! -d "$home" ]; then
        rm -rf "$staging"
        echo "ERROR: the home of $WORKER_USER ($home) does not exist on this host, or is not a directory. Rootless podman keeps its storage there, which is where the worker image lives, and systemd --user needs it too: with that HOME the deploy would fail AFTER the swap, with a podman message that does not name the cause. Create it with its owner (mkdir -p '$home' && chown $WORKER_USER:$WORKER_USER '$home'), or give the user another home with usermod -d /home/$WORKER_USER -m $WORKER_USER, and retry. The installed code was not touched." >&2
        exit 1
    fi
    # The `[ -r ]` above measures ROOT, and root reads any mode: it says
    # nothing about the TWO real consumers of the file, which read it as
    # $WORKER_USER via run_as_worker (the Makefile's WORKER_MEMORY fallback
    # and the load_env of 06-autostart.sh). Measured on Debian 13: with
    # setup.env at 0600 root:root the gate passed, the swap happened, and then
    # the container was recreated without a memory cap (with only a WARNING
    # buried in the make log) while load_env died with "Permission denied" and
    # the Makefile swallowed that failure with its `|| echo`, leaving the
    # systemd unit stopped. It goes AFTER `id -u`: without the user, `sudo -u`
    # would fail for another reason and the message would lie. The probe also
    # covers traversal of $CONFIG_DIR (0750 root:$WORKER_USER), which is the
    # other way the worker can fail to reach the file.
    if ! sudo -u "$WORKER_USER" test -r "$setup_env"; then
        rm -rf "$staging"
        echo "ERROR: $setup_env exists but $WORKER_USER cannot read it (file mode or owner, or that of $CONFIG_DIR). Root can read it, so the existence check is not enough: the ones that read it as $WORKER_USER are the Makefile's WORKER_MEMORY fallback (container without a memory cap) and the load_env of 06-autostart.sh (systemd unit stopped, with the failure swallowed by the Makefile). Leave it as the installer expects with: chown root:$WORKER_USER '$setup_env' && chmod 0640 '$setup_env' (and $CONFIG_DIR at 0750 root:$WORKER_USER). The installed code was not touched." >&2
        exit 1
    fi
    # Probe of the systemd --user manager BEFORE the swap. Step 4 stops each
    # unit before recreating its container, and `make up client` stops without
    # touching anything if it cannot reach the manager (user-bus.sh). But that
    # happens AFTER the swap: on a host with the unit file and no manager
    # (without linger /run/user/<uid> does not exist, or user@<uid>.service is
    # down) the step 4 pre-stop read the `is-active` failure as "unit
    # inactive", worker 1's `make` stopped, and ALL the workers were left
    # running on a bind mount whose inodes the swap had already replaced. Here
    # it aborts with the installed code intact.
    #
    # Only if some autostart unit of the worker or its sidecar exists: a host
    # that has not yet handed its containers to systemd does not need a
    # manager, and requiring one would break a deploy that works today. Same
    # environment as run_as_worker (defined only in step 4) and the same
    # side-effect-free query as user-bus.sh. It goes after resolving $uid and
    # $home, which it uses.
    bus_units=""
    for unit_path in "$home/.config/systemd/user"/container-orcaster-worker*.service \
                     "$home/.config/systemd/user"/container-orcaster-proxy*.service; do
        if [ -f "$unit_path" ]; then
            bus_units="$bus_units $(basename "$unit_path")"
        fi
    done
    # The step 4 re-sync runs 06-autostart.sh as root with
    # WORKER_USER=$WORKER_USER and WORKER_HOME=$home in the environment, but
    # its load_env sources setup.env and then applies its defaults (lib.sh:
    # WORKER_USER=orcaster, WORKER_HOME=/home/orcaster): a declaration in the
    # file, even an empty one, overrides what is passed in. If the account or
    # home that 06-autostart.sh would end up using are not those of this
    # deploy, it would regenerate the units of ANOTHER user, or look for the
    # podman storage in another home, and the units of $WORKER_USER would be
    # left stopped. What load_env sees is computed (same source, same
    # environment, same defaults) and compared before the swap. It only
    # matters if there are autostart units to re-sync; without them the
    # override keeps winning as before.
    if [ -n "$bus_units" ]; then
        export WORKER_USER WORKER_HOME="$home"
        read_setup_env_var WORKER_USER
        helper_user="${SETUP_ENV_VALUE:-orcaster}"
        read_setup_env_var WORKER_HOME
        helper_home="${SETUP_ENV_VALUE:-/home/orcaster}"
        if [ "$helper_user" != "$WORKER_USER" ] \
            || [ "$(rtrim_slashes "$helper_home")" != "$(rtrim_slashes "$home")" ]; then
            rm -rf "$staging"
            echo "ERROR: this deploy recreates the containers as $WORKER_USER (home $home), but 06-autostart.sh, which re-syncs the autostart, would resolve WORKER_USER=$helper_user and WORKER_HOME=$helper_home from $setup_env (a declaration in the file, even an empty one, overrides the environment; without a value the lib.sh default applies). This host has autostart units:$bus_units." >&2
            echo "       With the two different, the units of $WORKER_USER would be left stopped after recreating. Declare WORKER_USER=$WORKER_USER and WORKER_HOME=$home in $setup_env (or remove those lines if they are empty), or run without the environment override." >&2
            echo "       The installed code was not touched." >&2
            exit 1
        fi
    fi
    if [ -n "$bus_units" ]; then
        if ! bus_err="$( ( cd / && sudo -u "$WORKER_USER" env \
                HOME="$home" \
                XDG_RUNTIME_DIR="/run/user/$uid" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
                PATH="/usr/local/bin:/usr/bin:/bin" \
                systemctl --user show-environment ) 2>&1 >/dev/null )"; then
            rm -rf "$staging"
            echo "ERROR: could not reach the systemd --user manager of $WORKER_USER and this host has autostart units:$bus_units." >&2
            echo "       systemctl replied: $bus_err" >&2
            echo "       Without the manager each unit cannot be stopped before recreating its container: the deploy would stop after the swap, with all the workers on the already-replaced code." >&2
            # ORCASTER_RUN_USER_BASE is only a test hook: the bats tests point
            # it at a temporary dir to decide the branch without depending on
            # whether the host running them has /run/user/<uid> (a CI runner
            # has it; a development container does not). In production it never
            # arrives (sudo with env_reset does not propagate it) and it is
            # /run/user. It only decides the diagnosis: the probe above always
            # uses /run/user.
            run_user_base="${ORCASTER_RUN_USER_BASE:-/run/user}"
            if [ ! -d "$run_user_base/$uid" ]; then
                echo "       Cause: $run_user_base/$uid does not exist, so the manager of $WORKER_USER is not running. Remedy: sudo loginctl enable-linger $WORKER_USER, and retry." >&2
            else
                echo "       Cause: $run_user_base/$uid exists but the manager does not respond. Remedy: sudo systemctl status user@$uid.service (and sudo systemctl start user@$uid.service), and retry." >&2
            fi
            echo "       The installed code was not touched." >&2
            exit 1
        fi
    fi
    # The env.N files above only had the `[ -f ]`, which measures ROOT. Their
    # real consumer is podman, which opens them as
    # $WORKER_USER: the step 4 `make up client N=i` passes them through
    # --env-file (Makefile) via run_as_worker. With an env.N at 0600 root:root
    # the gate passed, the swap happened, and `make up client` died there: the
    # container of worker i already destroyed, its systemd unit stopped by the
    # pre-stop, and workers i+1..N running on the previous code's bind mount
    # (dead inodes). That is, the mid-way failure this block exists to avoid,
    # triggered by a file mode that root does not see. Same form as the
    # setup.env probe above, and so also AFTER `id -u`.
    for i in $(seq 1 "$WORKERS"); do
        env_i="$(worker_envfile "$i" "$CONFIG_DIR")"
        if ! sudo -u "$WORKER_USER" test -r "$env_i"; then
            rm -rf "$staging"
            echo "ERROR: $env_i exists but $WORKER_USER cannot read it (file mode or owner, or that of $CONFIG_DIR). Root can read it, so the existence check is not enough: what opens it is podman, as $WORKER_USER, through the --env-file of the 'make up client' of worker $i/$WORKERS; with that mode worker $i is not recreated and the following workers stay on the previous code. Leave it as the installer expects with: chown root:$WORKER_USER '$env_i' && chmod 0640 '$env_i' (and $CONFIG_DIR at 0750 root:$WORKER_USER). The installed code was not touched." >&2
            exit 1
        fi
    done
    # The pubkey is the worst of the three cases, and the only entirely silent
    # one: it does not arrive through --env-file but through a bind mount (-v
    # $(WORKER_PUBKEY):/var/lib/
    # orcaster/ed25519.pub:ro,z in the Makefile), and mounting a file the
    # process inside cannot read does NOT fail in `podman run`. The container
    # comes up, becomes healthy, and the worker rejects EVERY signed task
    # without an error appearing anywhere: not in the deploy, not in
    # `podman ps`, not in the systemd unit. It only shows up as tasks that do
    # not progress. Here it is a pre-swap abort with the cause.
    pubkey="$CONFIG_DIR/server-ed25519.pub"
    if ! sudo -u "$WORKER_USER" test -r "$pubkey"; then
        rm -rf "$staging"
        echo "ERROR: $pubkey exists but $WORKER_USER cannot read it (file mode or owner, or that of $CONFIG_DIR). Root can read it, so the existence check is not enough: the Makefile mounts it in the container by bind mount and that mount does NOT fail when the process inside cannot read the file, so the container would come up healthy and the worker would reject every signed task silently. Leave it as the installer expects with: chown root:$WORKER_USER '$pubkey' && chmod 0640 '$pubkey' (and $CONFIG_DIR at 0750 root:$WORKER_USER). The installed code was not touched." >&2
        exit 1
    fi
    # Same format that setup-scripts/debian13-worker/lib.sh:113-116
    # (load_env) requires: the Makefile matches with `ifeq ($(EGRESS_ISOLATION),1)`,
    # an EXACT match, so a 'true'/'yes'/'2' would silently disable isolation.
    # It is read here (not in step 4) so that an invalid value aborts BEFORE the
    # swap, not after.
    read_setup_env_var EGRESS_ISOLATION
    egress_isolation="$SETUP_ENV_VALUE"
    case "$egress_isolation" in
        ''|0|1) ;;
        *) rm -rf "$staging"; echo "ERROR: EGRESS_ISOLATION must be 0 or 1 in $setup_env (it is: '$egress_isolation'). The installed code was not touched." >&2; exit 1 ;;
    esac

    # WORKER_MEMORY travels through the Makefile fallback, which reads this
    # same setup.env, but ONLY because step 4 passes it
    # ETC_SETUP_ENV_FILE="$CONFIG_DIR/setup.env": the Makefile has that path
    # hardcoded to /etc/orcaster and does not see $CONFIG_DIR, because the
    # `sudo -u` in run_as_worker does not propagate the environment. Without
    # that bridge, a host with a non-default CONFIG_DIR validated the cap HERE
    # and recreated the workers without --memory, with the Makefile warning
    # giving the wrong reason ("no readable setup.env", naming two paths that
    # are not the host's). With the default CONFIG_DIR the value is identical
    # to the Makefile's, so always passing it is a no-op there. This script
    # does not propagate the VALUE, but it does propagate the PATH; and it
    # validates the value, for the same reason as EGRESS_ISOLATION: the value
    # ends up in the `--memory` of the `podman run` in step 4, and an invalid
    # format (e.g. '28' with no suffix, which is below podman's minimum) means
    # the container is NOT created, and that happens after `up-client` has
    # already done `podman rm -f` on the previous one. That is exactly the
    # halfway failure this pre-swap block exists to prevent. Same regex as
    # load_env (lib.sh) and cloud-init-worker.sh, so that all three reject the
    # same things.
    read_setup_env_var WORKER_MEMORY
    worker_memory="$SETUP_ENV_VALUE"
    if [ -n "$worker_memory" ] && ! [[ "$worker_memory" =~ ^0*[1-9][0-9]*[bkmgBKMG]$ ]]; then
        rm -rf "$staging"
        echo "ERROR: WORKER_MEMORY must be a number greater than zero with a b/k/m/g suffix, e.g. 28g, in $setup_env (it is: '$worker_memory'). The installed code was not touched." >&2
        exit 1
    fi

    # Deploy-time mirror of the worker's startup guard
    # (_ensure_strict_isolation_or_die in client/src/orcaster_worker/main.py):
    # a worker with a non-empty variable from the list of variables that
    # require isolation (ORCH_ISOLATION_REQUIRED_ENV) in its environment and
    # ORCH_ENFORCE_ISOLATION turned off exits with code 1 on startup. Without
    # this gate, step 4 did `podman rm -f` + `podman run` on each worker and
    # all of them ended up in a crash loop, while run-fleet.sh kept reporting
    # the fleet as enrolled (the previous release's state.json is still on the
    # volume). The only signal was the container log.
    #
    # The two channels that reach the worker's environment on a fleet host are
    # measured:
    #   - each env.N: this is what the `make up client` of step 4 hands to
    #     podman through --env-file, as is, without re-stamping.
    #   - what the next run-fleet.sh re-stamps into each env.N
    #     (stamp_worker_env in lib.sh), which arrives through TWO setup.env
    #     channels:
    #       * EXTRA_ENV (multiline, KEY=VALUE per line), stamped as is;
    #       * ORCH_ENFORCE_ISOLATION (true|false, its own channel), which
    #         stamp_worker_env writes AFTER EXTRA_ENV and which therefore,
    #         with the last occurrence winning in the --env-file, overrides
    #         whatever EXTRA_ENV declares. Without reading this second channel
    #         the gate evaluated EXTRA_ENV alone and aborted every run of the
    #         documented configuration (a protected variable in EXTRA_ENV and
    #         its own ORCH_ENFORCE_ISOLATION=true) even though the worker
    #         would have started fine.
    #     That is why EXTRA_ENV is evaluated followed by
    #     ORCH_ENFORCE_ISOLATION=<value> when setup.env declares it non-empty
    #     (empty or absent, as on an old host, leaves EXTRA_ENV as the only
    #     source, just like stamp_worker_env).
    #     It is measured without mixing it with env.N: an
    #     ORCH_ENFORCE_ISOLATION=true added by hand to env.N does not survive
    #     that re-stamping.
    # client/.env is not considered: the tarball deploy does not use it
    # (`make up client` resolves the env.N in $CONFIG_DIR) and the tarball
    # does not ship it.
    #
    # Detection is by NAME, never by value, and the messages do not print
    # values. Same rules as the worker: each name in the list (exact, like
    # os.environ) counts if its value has anything other than whitespace;
    # quotes included, because podman does not strip them from the --env-file
    # and the worker sees them as part of the value. ORCH_ENFORCE_ISOLATION
    # (name is case-insensitive, like pydantic-settings) turns the check off
    # with a pydantic-truthy value: 1, true, yes, on, t, y, case-insensitive.
    # The value is compared as podman delivers it: at most one trailing \r is
    # stripped (CRLF file) and nothing else, neither spaces nor quotes,
    # because pydantic does not strip them either: ` YES ` or `"on"` make
    # Settings() fail on startup and the gate treats them as not truthy. With
    # a repeated key the LAST occurrence wins, as in podman's --env-file.
    #
    # The list comes from the last ORCH_ISOLATION_REQUIRED_ENV line of each
    # env.N; for EXTRA_ENV, which cannot declare it, it comes from
    # ISOLATION_REQUIRED_ENV in setup.env (appended as the last line). An
    # empty list protects nothing; a malformed one aborts.
    #
    # It does not depend on the release being installed: it also stops a
    # rollback to an earlier version, which would start but would leave the
    # variable within reach of any task without task_env.
    read_setup_env_var EXTRA_ENV
    extra_env="$SETUP_ENV_VALUE"
    read_setup_env_var ORCH_ENFORCE_ISOLATION
    if [ -n "$SETUP_ENV_VALUE" ]; then
        extra_env="$extra_env"$'\n'"ORCH_ENFORCE_ISOLATION=$SETUP_ENV_VALUE"
    fi
    read_setup_env_var ISOLATION_REQUIRED_ENV
    isolation_required="${SETUP_ENV_VALUE//$'\n'/ }"
    isolation_vars_rc=0
    isolation_vars_found="$(printf '%s\nORCH_ISOLATION_REQUIRED_ENV=%s\n' "$extra_env" "$isolation_required" \
        | isolation_vars_status)" || isolation_vars_rc=$?
    isolation_vars_gate "$isolation_vars_rc" "EXTRA_ENV of $setup_env" \
        "run-fleet.sh re-stamps EXTRA_ENV into each env.N and discards anything added by hand there, so an ORCH_ENFORCE_ISOLATION set only in env.N does not survive and the workers would end up in a crash loop on the next run-fleet.sh run, even though today's env.N lets them start." \
        "$isolation_vars_found"
    for i in $(seq 1 "$WORKERS"); do
        env_i="$(worker_envfile "$i" "$CONFIG_DIR")"
        isolation_vars_rc=0
        isolation_vars_found="$(isolation_vars_status < "$env_i")" || isolation_vars_rc=$?
        isolation_vars_gate "$isolation_vars_rc" "$env_i (worker $i/$WORKERS)" \
            "step 4 would recreate it and it would end up in a crash loop, with run-fleet.sh reporting the fleet as healthy." \
            "$isolation_vars_found"
    done

    # WORKER_RO_MOUNTS: host directories that step 4 mounts read-only at the
    # same path inside each worker. Same reason as WORKER_MEMORY for validating
    # it HERE: an invalid entry, a missing directory or one that $WORKER_USER
    # cannot read must abort before the swap, not in the `make up client` of
    # step 4, with the worker unit already stopped by the pre-stop.
    #
    # The rule is NOT replicated here: it lives in check-ro-mounts.sh and the
    # one from the NEW RELEASE is run, which is exactly the one its Makefile
    # will run before `podman rm -f`, so the gate and the recreation cannot
    # disagree. This does not contradict the criterion of the RELEASE check
    # above (do not trust the artifact to verify itself): the sha256 already
    # matched in step 1, and here it is not the artifact being verified but
    # the HOST. It runs in a separate process (never sourced into this shell)
    # and as $WORKER_USER, which is how podman resolves the source of the
    # mounts (readability for the container's uid is measured by the validator
    # through the "other" bits); the script travels over stdin so that this
    # user is not required to read the staging directory.
    #
    # A release that predates WORKER_RO_MOUNTS does not ship the validator and
    # its Makefile does not know the variable: the workers are recreated
    # without the mounts. This is the rollback path, so it does not abort, but
    # it does not stay silent either.
    read_setup_env_var WORKER_RO_MOUNTS
    worker_ro_mounts="$SETUP_ENV_VALUE"
    if [ -n "$worker_ro_mounts" ]; then
        ro_check="$extracted/setup-scripts/debian13-worker/check-ro-mounts.sh"
        if [ ! -f "$ro_check" ]; then
            echo "WARNING: $setup_env declares WORKER_RO_MOUNTS='$worker_ro_mounts', but this release does not ship setup-scripts/debian13-worker/check-ro-mounts.sh: its Makefile does not know the variable and the workers are recreated WITHOUT those mounts." >&2
        elif ! ( cd / && sudo -u "$WORKER_USER" bash -s -- --host "$worker_ro_mounts" ) < "$ro_check"; then
            rm -rf "$staging"
            echo "ERROR: WORKER_RO_MOUNTS in $setup_env did not pass validation as $WORKER_USER (reason above). With that value the 'make up client' of step 4 would stop with the worker unit already stopped. Fix the list or the permissions and retry. The installed code was not touched." >&2
            exit 1
        fi
    fi
fi

# 3. Swap. The previous one stays at hand in .orcaster.old for a manual
# rollback. Plain `mv` and not `mv -T`: the option is GNU coreutils and does
# not exist in macOS's mv, where this repository's bats tests run. With the
# destination just deleted the two forms do the same thing, because there is
# no directory inside which to move.
#
# The two guards below run BEFORE the `rm -rf "$previous"`. The other way
# around, an abort for a symlink or a regular file had already taken away the
# .orcaster.old of the previous delivery, which is the manual rollback this
# script promises and that its own handlers name, while the message claimed
# "The installed code was not touched": true of $INSTALL_DIR and false of the
# state the operator was going to turn to afterwards. The `rm` stays attached
# to the `mv` that needs it, which is the only thing that the proximity to the
# `mv -T` comment above had to guarantee.
# A symlink can NOT be swapped with this mechanism: `[ -d ]` follows it and
# gives true, but `mv` moves the LINK. The result would be silent and worse
# than a failure: $previous is left as a dangling symlink, the real tree it
# pointed to remains complete and intact (so the whole reason for step 4,
# recreating because the inode died, does not apply and nobody says so), and
# the rollback this script prints would restore a link to a tree that a later
# `rm -rf` of .orcaster.old would delete. It aborts before the swap, naming
# the remedy: the real path is declared in INSTALL_DIR of the host's setup.env
# (or with --install-dir).
if [ -L "$INSTALL_DIR" ]; then
    rm -rf "$staging"
    echo "ERROR: $INSTALL_DIR is a symlink (it points to $(readlink "$INSTALL_DIR")). This deploy replaces the code directory with a mv, so it would move the link and leave the real tree untouched, silently. Declare the REAL path in INSTALL_DIR of $setup_env (or pass it with --install-dir) and retry. The installed code was not touched." >&2
    exit 1
fi
# $INSTALL_DIR exists but is NOT a directory: a regular file (a `>` to the
# wrong path, a leftover from an aborted installation), a fifo, a device. The
# symlink has its own guard above; this case had none and ended up claiming
# something false: the
# backup was not made (`[ -d ]` false), the `mv` of the new release failed
# with ENOTDIR and its handler, seeing that there was no $previous either,
# printed "there was no previous code to restore: it was a new installation"
# about a host that DID have something at that path and still has it.
if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
    rm -rf "$staging"
    echo "ERROR: $INSTALL_DIR exists and is not a directory (it is a regular file, a fifo or a device). The swap replaces the code directory with a mv, which on that path fails with ENOTDIR. Look at what it is before deleting it, move it out of the way, or declare the real path in INSTALL_DIR of $setup_env (or pass it with --install-dir), and retry. The installed code was not touched." >&2
    exit 1
fi

# 3.1. Root's copy of the release, built BEFORE the swap: if it fails, the
# installed code was not touched. `cp -a` and not rsync: worker hosts do not
# ship rsync and this script runs before 00-preflight.sh in the Bootstrap. The
# chown -R and the chmod -R go-w do not follow links inside the tree (both
# operate on the link, not on its target), and tar as root keeps whatever uid
# the tarball carries: without the chown, the copy would be left owned by the
# machine that built the release.
rm -rf "$source_new"
if ! cp -a "$extracted" "$source_new" \
    || ! chown -R root:root "$source_new" \
    || ! chmod -R go-w "$source_new"; then
    rm -rf "$source_new" "$staging"
    echo "ERROR: could not build root's copy of the release in $source_new (cp -a, chown -R root:root or chmod -R go-w failed: disk, quota or permissions). What was left half-done was deleted. The installed code ($INSTALL_DIR) and the previous root copy ($SOURCE_DIR) were not touched." >&2
    exit 1
fi

rm -rf "$previous"
if [ -d "$INSTALL_DIR" ]; then
    # The FIRST mv cannot be a bare statement under `set -e` either: it runs
    # right after the `rm -rf "$previous"`, that is,
    # with the previous delivery's .orcaster.old ALREADY deleted, so a failure
    # here left the operator with mv's raw stderr and not knowing that they
    # had just lost that rollback. The installed code is still intact (the
    # only good thing about failing at this exact point) and the message has
    # to say both things, not just the good one.
    if ! mv "$INSTALL_DIR" "$previous"; then
        rm -rf "$staging" "$source_new"
        echo "ERROR: backing up the installed code failed ('mv $INSTALL_DIR $previous'): permissions, quota, or a read-only remount of the filesystem. NOTHING was replaced: $INSTALL_DIR is intact and still has the previous release running. What did disappear is the .orcaster.old tree of the PREVIOUS delivery, which this same step deletes before writing the new one: that rollback path no longer exists. The new release's staging was deleted, so nothing is left half-done in $parent: fix the cause and retry this same command." >&2
        exit 1
    fi
fi
# The second mv is the most fragile step of the invariant: if the first has
# already run, $INSTALL_DIR does not exist, and a failure here (permissions,
# quota, a read-only remount, a stray file at the destination path) would
# leave the host WITHOUT installed code, neither the old nor the new, seeing
# only mv's generic stderr.
# That is why it has its own handler instead of being a bare statement under
# `set -e`: it tries to put the previous code back in its place before giving
# up, and if that also fails, the message says exactly where each thing is
# instead of forcing the operator to deduce it.
if ! mv "$extracted" "$INSTALL_DIR"; then
    if [ -d "$previous" ]; then
        if mv "$previous" "$INSTALL_DIR"; then
            echo "ERROR: installing the new release in $INSTALL_DIR failed; the previous code was restored. The new release was left uninstalled in $extracted (check it before retrying)." >&2
        else
            echo "ERROR: installing the new release in $INSTALL_DIR failed and the automatic restore also failed. $INSTALL_DIR DOES NOT EXIST. The previous code is still intact in $previous; restore it by hand with: mv '$previous' '$INSTALL_DIR'" >&2
        fi
    else
        echo "ERROR: installing the new release in $INSTALL_DIR failed (there was no previous code to restore: it was a new installation). $INSTALL_DIR DOES NOT EXIST. The extracted release was left in $extracted." >&2
    fi
    rm -rf "$source_new"
    echo "       Root's copy was not touched: $SOURCE_DIR still has the previous release (if there was one)." >&2
    exit 1
fi
rm -rf "$staging"

# 3.2. Replacement of root's copy, only now that INSTALL_DIR has the new
# release: both end up with the same release. Same pattern as the INSTALL_DIR
# swap (set aside, move, delete what was set aside), with messages that say
# where each thing ended up.
if [ -d "$SOURCE_DIR" ]; then
    rm -rf "$source_old"
    if ! mv "$SOURCE_DIR" "$source_old"; then
        rm -rf "$source_new"
        echo "ERROR: the new release is ALREADY installed in $INSTALL_DIR (the previous one was left in $previous), but I could not set aside the previous root copy ('mv $SOURCE_DIR $source_old'): $SOURCE_DIR still has the PREVIOUS release and no container was recreated. The provisioning suite run from there does not match the installed code. Fix the cause (permissions, quota, read-only remount) and retry this same command." >&2
        exit 1
    fi
fi
if ! mv "$source_new" "$SOURCE_DIR"; then
    if [ -d "$source_old" ] && mv "$source_old" "$SOURCE_DIR"; then
        echo "ERROR: the new release is ALREADY installed in $INSTALL_DIR (the previous one was left in $previous), but putting its root copy in $SOURCE_DIR failed; the previous copy was restored, which does NOT match the installed code. The new copy was left in $source_new. No container was recreated: fix the cause and retry this same command." >&2
    elif [ -d "$source_old" ]; then
        echo "ERROR: the new release is ALREADY installed in $INSTALL_DIR (the previous one was left in $previous), but putting its root copy in $SOURCE_DIR failed and restoring the previous one also failed. $SOURCE_DIR DOES NOT EXIST: the previous copy is in $source_old and the new one in $source_new. Put it in place by hand with: mv '$source_new' '$SOURCE_DIR' (or restore the previous one with: mv '$source_old' '$SOURCE_DIR'). No container was recreated." >&2
    else
        echo "ERROR: the new release is ALREADY installed in $INSTALL_DIR (the previous one was left in $previous), but putting its root copy in $SOURCE_DIR failed (there was no previous copy). $SOURCE_DIR DOES NOT EXIST; the new copy was left in $source_new. Put it in place by hand with: mv '$source_new' '$SOURCE_DIR'. No container was recreated." >&2
    fi
    exit 1
fi
rm -rf "$source_old"

# Ownership of the installed tree. This script is the one that has to set the
# tree's owner: no other provisioning script does it on a tarball deploy. The
# `chown -R` of 00-preflight.sh (which dies with `die` if it fails) only runs
# in a COMPLETE pass of the provisioning, and the cloud-init startup enters
# through `--from 02`. A tarball deploy runs neither of the two. That is why
# there are two explicit, distinct paths, and not a single best-effort that
# warns the same way in both cases:
#
#   - user present (host in steady state, every deploy through
#     push-release.sh): the chown is the normal condition and its failure is
#     FATAL. A tree the worker cannot read brings up no container, and
#     finding out through a WARNING in the middle of the deploy log is finding
#     out late. Under RESTART=1 the user was already verified pre-swap; this
#     `id` covers the --no-restart path.
#   - user absent (first delivery, the README's Bootstrap): there is nobody to
#     chown to yet. It is skipped explicitly, saying in what real state the
#     tree is left and with what exact command to reconcile it. Automatic
#     reconciliation exists: 00-preflight.sh does the same `chown -R` as soon
#     as it creates the user, which is the next Bootstrap step. The WARNING
#     still gives the manual command because the operator can stop between the
#     two steps, and then the tree stays root-owned with nothing else running.
#
# This chown -R depends on fs.protected_hardlinks=1: it does not follow
# symbolic links, but it DOES change the owner of a hardlink's target, and
# $WORKER_USER controls the tree. This script does not go through
# 00-preflight.sh, so it verifies it here, before the chown and with `sysctl`
# from PATH. It fails closed: anything other than 1, or unreadable, is FATAL
# (the new release is ALREADY installed, same as if the chown failed).
if id -u "$WORKER_USER" >/dev/null 2>&1; then
    if ! hardlinks="$(sysctl -n fs.protected_hardlinks 2>/dev/null)"; then
        echo "ERROR: could not read the sysctl fs.protected_hardlinks (sysctl -n fs.protected_hardlinks failed). The chown -R $WORKER_USER:$WORKER_USER on $INSTALL_DIR, which $WORKER_USER controls, is only safe with fs.protected_hardlinks=1. The new release is ALREADY installed there (the previous one was left in $previous) but nothing was chowned and no container was recreated. Check it with: sysctl fs.protected_hardlinks and retry this same command." >&2
        exit 1
    fi
    if [ "$hardlinks" != "1" ]; then
        echo "ERROR: fs.protected_hardlinks=$hardlinks (1 is required). The chown -R $WORKER_USER:$WORKER_USER on $INSTALL_DIR, which $WORKER_USER controls, with the sysctl other than 1 lets that user make a hardlink to a root-owned file and get the chown to change its owner. The new release is ALREADY installed there (the previous one was left in $previous) but nothing was chowned and no container was recreated. Fix it with: sysctl -w fs.protected_hardlinks=1 and persist it in /etc/sysctl.d/ (e.g. echo 'fs.protected_hardlinks = 1' > /etc/sysctl.d/60-protected-links.conf), and retry this same command." >&2
        exit 1
    fi
    if ! chown -R "$WORKER_USER:$WORKER_USER" "$INSTALL_DIR"; then
        echo "ERROR: the chown -R $WORKER_USER:$WORKER_USER on $INSTALL_DIR failed. The new release is ALREADY installed there (the previous one was left in $previous), but its owner is not $WORKER_USER, which is the user that runs the worker: no container was recreated. Fix it with: chown -R '$WORKER_USER:$WORKER_USER' '$INSTALL_DIR' and retry this same command." >&2
        exit 1
    fi
else
    echo "WARNING: user $WORKER_USER does not exist yet on this host (first delivery, see setup-scripts/debian13-worker/README.md section Bootstrap): $INSTALL_DIR is left owned by root. That is enough to continue with the provisioning: the tree has no group or other write (directories 0755, files 0644, executables 0755) and anyone can read it. But as soon as the suite creates the user, give it back to its owner with: chown -R '$WORKER_USER:$WORKER_USER' '$INSTALL_DIR'" >&2
fi

echo ">> Installed in $INSTALL_DIR"
printf '%s\n' "$new_release" | sed 's/^/   /'
echo ">> Root's copy of the release in $SOURCE_DIR. The provisioning suite (run-all-worker.sh, run-fleet.sh, cloud-init-worker.sh) is run from $SOURCE_DIR/setup-scripts/debian13-worker, never from $INSTALL_DIR."

# 4. Recreate the containers. A restart is NOT enough: the container reads the
# code through a live bind mount from this directory (WORKER_SRC mounted with
# -v in the Makefile), so after the swap the old container's mount points to
# a dead inode (the previous directory no longer exists under that name). The
# old container keeps running on those deleted inodes until it is recreated;
# a `restart` does not redo the mount, it only relaunches the process inside
# the same container. That is why the order is swap and then `make up client`,
# never the other way around.
if [ "$RESTART" -eq 1 ]; then
    # worker_unit/proxy_unit: same systemd unit naming convention that
    # run-fleet.sh uses for the pre-stop (container-orcaster-worker[-N]
    # .service / container-orcaster-proxy[-N].service). We take them from the
    # new release's lib.sh, from root's copy ($SOURCE_DIR, physical path) and
    # never from $INSTALL_DIR, which belongs to the worker user. That lib.sh
    # runs its origin guard when sourced as root, and the freshly built copy
    # passes it. If some future release does not ship it, we replicate the
    # convention inline: keep it in sync with
    # setup-scripts/debian13-worker/lib.sh.
    _lib="$SOURCE_DIR/setup-scripts/debian13-worker/lib.sh"
    if [ -f "$_lib" ]; then
        # Probe in a separate bash BEFORE actually sourcing, for two reasons.
        # (1) `if ! source ...` does not work as a handler: bash suspends
        # errexit in the whole compound command of a condition, and that
        # suspension reaches the sourced file, so a top-level command that
        # fails halfway through lib.sh would keep executing the rest and could
        # return 0.
        # (2) without the probe, a broken lib.sh kills this script with bash's
        # bare message, right after the swap, without saying that the new code
        # is already installed and that no container was recreated. The probe
        # runs with -euo pipefail inside, so either way of failing (syntax or
        # command) exits the same way.
        if ! _lib_err="$(bash -c 'set -euo pipefail; . "$1"' _ "$_lib" 2>&1 >/dev/null)"; then
            [ -n "$_lib_err" ] && printf '%s\n' "$_lib_err" >&2
            echo "ERROR: $_lib could not be sourced (invalid syntax, a top-level command that fails, or its origin guard; the reason, if it gave one, is above). The new release is ALREADY installed in $INSTALL_DIR and the previous one was left in $previous, but NO container was recreated: the workers keep running on the previous code, whose bind mount points to inodes that were already replaced. Fix the file in the release and retry, or go back to the previous one with: rm -rf '$INSTALL_DIR' && mv '$previous' '$INSTALL_DIR'. That command does not touch root's copy: $SOURCE_DIR already has the new release and the previous copy is not kept, so the provisioning suite run from there would be the new release's. To bring both back to the previous one, reinstall the previous tarball with this same script." >&2
            exit 1
        fi
        # lib.sh is sourced in THIS shell: any top-level assignment in it
        # overrides this script's variables. Today lib.sh guards CONFIG_DIR
        # with `${CONFIG_DIR:-...}`, but that is a property of the newly
        # installed release, not a guarantee of this script: if a future
        # release assigned it bare, this deploy would start reading the env.N
        # files and the pubkey from the wrong directory AFTER the swap. Copies
        # are saved before and re-set after: it is cheap and does not depend on
        # lib.sh behaving well.
        _keep_config_dir="$CONFIG_DIR"
        _keep_worker_user="$WORKER_USER"
        _keep_install_dir="$INSTALL_DIR"
        _keep_source_dir="$SOURCE_DIR"
        _keep_workers="$WORKERS"
        _keep_egress="$egress_isolation"
        _keep_ro_mounts="$worker_ro_mounts"
        _keep_uid="$uid"
        _keep_home="$home"
        # shellcheck source=/dev/null
        source "$_lib"
        CONFIG_DIR="$_keep_config_dir"
        WORKER_USER="$_keep_worker_user"
        INSTALL_DIR="$_keep_install_dir"
        SOURCE_DIR="$_keep_source_dir"
        WORKERS="$_keep_workers"
        egress_isolation="$_keep_egress"
        worker_ro_mounts="$_keep_ro_mounts"
        uid="$_keep_uid"
        home="$_keep_home"
    else
        worker_unit() { if [ -n "${1:-}" ]; then printf 'container-orcaster-worker-%s.service\n' "$1"; else printf 'container-orcaster-worker.service\n'; fi; }
        proxy_unit() { if [ -n "${1:-}" ]; then printf 'container-orcaster-proxy-%s.service\n' "$1"; else printf 'container-orcaster-proxy.service\n'; fi; }
        # worker_envfile is NOT repeated here: its stub is already defined at
        # the start of this script (the pre-swap gate of step 2.5 needs it, and
        # it runs long before this block) and is still alive in this branch,
        # because a `source` that does not get to run does not undefine
        # anything. An identical copy here would be dead code and, worse, a
        # trap: the day someone deduplicates it by deleting "the one above",
        # the pre-swap gate breaks.
    fi

    # EGRESS_ISOLATION: the Makefile declares it with a bare `?= 1`, with no
    # fallback to setup.env (unlike WORKER_MEMORY, whose fallback DOES read
    # setup.env). Without propagating it, a host that opted out
    # (EGRESS_ISOLATION=0 in its setup.env) would silently return to the
    # default 1 on every tarball deploy. We already read and validated it in
    # step 2.5 (pre-swap); $egress_isolation is that same value, without
    # re-reading setup.env. If it is not declared, we propagate nothing: the
    # Makefile's `?= 1` remains the source of truth, we do not force it by hand.
    egress_arg=""
    [ -n "$egress_isolation" ] && egress_arg="EGRESS_ISOLATION=$egress_isolation"

    # sudo -u wrapper with the minimal environment that rootless podman and
    # systemd --user need (XDG_RUNTIME_DIR + DBUS). The `cd /` keeps sudo from
    # inheriting a cwd that WORKER_USER cannot access (same reason documented
    # in lib.sh:as_worker).
    run_as_worker() {
        ( cd / && sudo -u "$WORKER_USER" env \
            HOME="$home" \
            XDG_RUNTIME_DIR="/run/user/$uid" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
            PATH="/usr/local/bin:/usr/bin:/bin" \
            "$@" )
    }

    # Exact command that regenerates worker N's systemd --user unit against
    # the freshly created container. It is the only thing that fixes an
    # adoption unit after a recreate (its PIDFile names the previous
    # container), so the messages below print it instead of a
    # `systemctl --user start` that cannot work. `sudo env VAR=...` and not
    # `sudo VAR=...`: sudo rejects assignments on the command line unless
    # sudoers allows it, and both N and CONFIG_DIR are ENVIRONMENT variables
    # that 06-autostart.sh reads, not positional arguments. Same idiom as the
    # rest of a host's migration.
    #
    # Each value goes through %q: the home comes from passwd and is only
    # validated to be absolute, so it may contain spaces; unquoted, the
    # command the operator copies and pastes would split into another word. A
    # value without special characters comes out the same as with %s.
    autostart_cmd() {
        printf 'sudo env CONFIG_DIR=%q N=%q WORKER_USER=%q WORKER_HOME=%q bash %q' \
            "$CONFIG_DIR" "$1" "$WORKER_USER" "$home" \
            "$SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh"
    }

    # systemd --user units that this loop stopped and that did NOT become
    # active again. A space-separated string and not an array: this
    # repository's bats tests run on macOS, with bash 3.2, where expanding an
    # empty array under `set -u` blows up. Unit names contain no spaces.
    down_units=""
    # Indexes of the workers with at least one unit in $down_units. Kept
    # separately because the remedy is applied per WORKER (one run of
    # 06-autostart.sh regenerates the worker's unit and its sidecar's), not
    # per unit: without this list, the final summary would have to guess the
    # index from the unit name, or print the command for the whole fleet.
    down_workers=""

    # The `stop` used to be a bare statement under `set -e` INSIDE the
    # post-swap window, the only step of that block without a handler (the
    # second mv, the chown, the lib.sh probe and `make up client` all have
    # one). A failing stop is plausible without anything on the host being
    # broken: the job dies on TimeoutStopSec, the unit enters failed, or the
    # user bus is absent. Observed: the operator saw ONLY systemd's stderr,
    # without learning that the new code was already installed, that the
    # previous one was left in $previous, or that no worker had been
    # recreated. Aggravating factor: dying before
    # `iter_units="$iter_units $unit"`, the unit was never recorded, so no
    # later handler could name it.
    stop_unit_or_die() {
        local u="$1" pending
        if run_as_worker systemctl --user stop "$u"; then return 0; fi
        echo "ERROR: 'systemctl --user stop $u' failed in the pre-stop of worker $i/$WORKERS (job timeout, unit in failed state, or the user bus is down). The new release is ALREADY installed in $INSTALL_DIR (the previous one was left in $previous), but worker $i was NOT recreated: it is still on the bind mount of the previous code, which after the swap points to inodes that have been replaced." >&2
        echo "       Check the unit with: sudo -u $WORKER_USER XDG_RUNTIME_DIR=/run/user/$uid systemctl --user status $u" >&2
        # shellcheck disable=SC2086 # intentional word-split: list of unit names separated by spaces
        for pending in $down_units $iter_units $u; do
            echo "       Unit stopped (or half-stopped) by this deploy and not back up: $pending. Bring it up with: sudo -u $WORKER_USER XDG_RUNTIME_DIR=/run/user/$uid systemctl --user start $pending" >&2
        done
        if [ "$i" -lt "$WORKERS" ]; then
            echo "       Workers $((i+1))..$WORKERS were not recreated either. Retry this same command once the cause is fixed." >&2
        fi
        exit 1
    }

    # State of a unit according to `systemctl --user is-active`, which prints
    # it even when it exits non-zero. Empty if the query itself failed (the
    # bus was already probed before the swap).
    unit_state() {
        run_as_worker systemctl --user is-active "$1" 2>/dev/null || true
    }
    # rc 0 if the state is not an idle one (same table as user-bus.sh).
    unit_running() {
        case "$1" in
            ''|inactive|failed|unknown) return 1 ;;
        esac
        return 0
    }

    for i in $(seq 1 "$WORKERS"); do
        # Units stopped in THIS iteration. Kept apart from $down_units because
        # those from earlier iterations were already verified: only the ones
        # that really remain down stay in $down_units.
        iter_units=""
        # Pre-stop of the active unit(s) before recreating (same reason
        # documented in run-fleet.sh:136-139): `make up client`
        # does a `podman rm -f` of a container that the unit supervises with
        # Restart=always, and systemd relaunches it in parallel with the
        # make's `podman run` (a "container name is already in use" race). On
        # a host in steady state (autostart already enabled by
        # 06-autostart.sh, the normal condition of the fleet) that race can
        # take down the very step that brings the new code online, and this
        # script is the only place where that can be headed off in a tarball
        # deploy.
        #
        # Everything that is not idle is stopped, not just `active`: same
        # rule as user-bus.sh stop-unit. A unit in activating (the
        # auto-restart of a crash-looping worker) or deactivating also races
        # against the `podman run`, and if only the Makefile's stop stopped
        # it, it would not end up in $iter_units and nobody would require it
        # to come back.
        unit="$(worker_unit "$i")"
        state="$(unit_state "$unit")"
        if unit_running "$state"; then
            echo ">> Unit $unit $state: stop before recreating."
            stop_unit_or_die "$unit"
            iter_units="$iter_units $unit"
        fi
        if [ "$egress_isolation" != "0" ]; then
            punit="$(proxy_unit "$i")"
            state="$(unit_state "$punit")"
            if unit_running "$state"; then
                echo ">> Unit $punit $state: stop before recreating."
                stop_unit_or_die "$punit"
                iter_units="$iter_units $punit"
            fi
        fi

        echo ">> Recreating worker $i/$WORKERS"
        # `make up client` goes in an `if !` and not bare under `set -e`: a
        # failure here (bad env.i, podman out of space, the image missing)
        # leaves container i down, its systemd unit stopped by the pre-stop
        # above, and workers i+1..N on the previous code with the bind mount
        # pointing to replaced inodes. With a plain `set -e` the script
        # exited non-zero without naming any of that, and the units the
        # deploy itself had stopped stayed down with no way back. Same
        # criterion as the second `mv` handler: say where each thing was left.
        # WORKER_ENV and WORKER_PUBKEY are not passed: the Makefile
        # resolves $CONFIG_DIR/env.N and $CONFIG_DIR/server-ed25519.pub on its
        # own, the single definition of those paths, so this line only has to
        # name the config dir. worker_config_make_args adds the two overrides
        # back only when the installed Makefile predates that resolution
        # (no WORKER_ENV_HOST), as on a rollback with --ref to an older release.
        #
        # CONFIG_DIR is the path bridge of this line. `make up client` ends up calling
        # autostart-sync-client, which runs 06-autostart.sh, whose load_env
        # resolves $CONFIG_DIR on its own: with nothing telling it, it falls
        # back to the default /etc/orcaster from lib.sh. On a host with the
        # config outside that path, 06-autostart.sh died asking for
        # /etc/orcaster/setup.env and the Makefile swallowed the failure with
        # its `|| echo`, so the systemd unit was NOT regenerated and its
        # PIDFile kept naming the container that this very deploy had just
        # destroyed. It goes as a make COMMAND-LINE variable, not through the
        # environment: run_as_worker's `env` only propagates the allowlist
        # above.
        #
        # ETC_SETUP_ENV_FILE resolves something else and is still needed: it
        # is the Makefile's WORKER_MEMORY fallback, which has the path
        # hardcoded to /etc/orcaster because run_as_worker's `sudo -u` does
        # not propagate the environment. With the default CONFIG_DIR the two
        # are a no-op there.
        #
        # WORKER_RO_MOUNTS is passed explicitly, as in run-fleet.sh: it is the
        # value the pre-swap gate has just validated as $WORKER_USER, and this
        # way the Makefile uses that and not a re-read. Empty = no mounts: the
        # Makefile adds no -v and the `podman run` stays as before.
        #
        # AUTOSTART_RESYNC=0: the Makefile's autostart-sync-client runs
        # 06-autostart.sh from $(SETUP_WORKER_DIR), which here is
        # $INSTALL_DIR, and that script's load_env aborts on the origin guard
        # (require_source_outside_install_dir) because the suite runs from
        # inside INSTALL_DIR. With the hook active it always failed on this
        # path and left the unit stopped. The re-sync is done by this script
        # further down, as root and from root's copy ($SOURCE_DIR), the same
        # as run-fleet.sh. It is NOT AUTOSTART_SYNC=0: that also turns off the
        # manager probe and user-bus.sh's stops, which stop the units in
        # activating/deactivating that the pre-stop above (is-active --quiet,
        # only `active`) lets through.
        #
        # AUTOSTART_SYNC_FATAL=0 stays: push-release.sh ships THIS installer
        # with the tarball of the requested --ref, which may be older
        # (rollback, or a deploy of an earlier release) and bring a Makefile
        # that does not know AUTOSTART_RESYNC. There the hook runs anyway,
        # hits the guard and, without this variable, `make` would exit
        # non-zero and cut the loop after the first worker. With 0 it only
        # warns, and the re-sync further down does the work.
        worker_cfg_args=()
        while IFS= read -r _cfg_arg; do worker_cfg_args+=("$_cfg_arg"); done \
            < <(worker_config_make_args "$INSTALL_DIR" "$i" "$CONFIG_DIR")
        # shellcheck disable=SC2086 # $egress_arg expands to 0 or 1 words: intentional word-split (same pattern as run-fleet.sh)
        if ! run_as_worker make -C "$INSTALL_DIR" up client N="$i" \
            ${worker_cfg_args[@]+"${worker_cfg_args[@]}"} \
            ETC_SETUP_ENV_FILE="$CONFIG_DIR/setup.env" \
            CONFIG_DIR="$CONFIG_DIR" \
            WORKER_RO_MOUNTS="$worker_ro_mounts" \
            AUTOSTART_RESYNC=0 \
            AUTOSTART_SYNC_FATAL=0 \
            $egress_arg; then
            echo "ERROR: 'make up client' failed for worker $i/$WORKERS. The new release is ALREADY installed in $INSTALL_DIR (the previous one was left in $previous)." >&2
            # Units this deploy stopped and that remain down. The remedy is NOT
            # `systemctl --user start`: this was the
            # only one of the three exits of this block that still
            # recommended it, exactly what the definition of autostart_cmd and
            # the closing section further down state cannot work. The
            # `up-client` recipe of the Makefile does `podman rm -f` before its
            # `podman run`, so in the normal failures of this path (disk,
            # missing image, invalid --memory)
            # the container that the adoption unit's PIDFile names no longer
            # exists. It is fixed by fixing the cause and retrying the deploy,
            # which recreates the container and re-syncs the autostart; if the
            # unit still does not come back, it is REGENERATED per worker with
            # 06-autostart.sh.
            # shellcheck disable=SC2086 # intentional word-split: the list is a string of unit names separated by spaces
            for u in $down_units $iter_units; do
                echo "       Unit stopped by this deploy and not back up: $u" >&2
            done
            if [ -n "$down_units$iter_units" ]; then
                echo "       Those units are not brought up with 'systemctl --user start': their PIDFile names the container this deploy destroyed. Fix the cause and retry this same command; if any is still down, regenerate it:" >&2
                # $i is included only if THIS iteration's pre-stop stopped
                # something: with $iter_units empty, worker $i has no unit to
                # regenerate and its command would be noise.
                iter_worker=""
                if [ -n "$iter_units" ]; then
                    iter_worker="$i"
                fi
                # shellcheck disable=SC2086 # intentional word-split: see above
                for n in $down_workers $iter_worker; do
                    echo "       $(autostart_cmd "$n")" >&2
                done
            fi
            if [ "$i" -lt "$WORKERS" ]; then
                echo "       Workers $((i+1))..$WORKERS were NOT recreated: they keep running on the bind mount of the previous code, which after the swap points to inodes that have been replaced. Retry this same command once the cause is fixed." >&2
            fi
            exit 1
        fi

        # Autostart re-sync. The adoption unit embeds the PIDFile of the
        # container that `make up client` has just destroyed, so it must be
        # regenerated with 06-autostart.sh. It runs as root and from
        # $SOURCE_DIR, root's copy that passes the origin guard, in the same
        # form as the autostart_cmd remedy (verified by hand on a worker):
        # 06-autostart.sh drops to the worker user by itself with as_worker.
        # Same condition that autostart-sync-client had: only if the worker's
        # unit file already exists; a host that has not handed its containers
        # over to systemd does not gain a unit through this deploy. A failure
        # does not cut the loop (the following workers would stay on replaced
        # inodes): it warns, and the unit check below records it and exits
        # non-zero at the end.
        ucfg="$home/.config/systemd/user"
        autostart_sh="$SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh"
        resync_failed=0
        if [ -f "$ucfg/$(worker_unit "$i")" ]; then
            echo ">> Autostart already enabled ($(worker_unit "$i")): re-syncing the systemd --user unit from $SOURCE_DIR"
            if [ ! -f "$autostart_sh" ]; then
                echo "WARNING: $autostart_sh does not exist: could not re-sync the autostart of worker $i." >&2
                resync_failed=1
            elif ! ( cd / && env CONFIG_DIR="$CONFIG_DIR" N="$i" \
                    WORKER_USER="$WORKER_USER" WORKER_HOME="$home" \
                    ${egress_isolation:+EGRESS_ISOLATION="$egress_isolation"} \
                    bash "$autostart_sh" ); then
                echo "WARNING: the autostart re-sync of worker $i failed ($autostart_sh, reason above)." >&2
                resync_failed=1
            fi
        fi

        # The container may end up healthy with the systemd unit stopped: the
        # re-sync above can fail and does not cut the loop, so this check is
        # the one that detects it; without it, the symptom would be silent
        # until the next reboot. The units that the pre-stop stopped are
        # required to come back; one that was idle beforehand is not
        # required: nobody promised to bring it up. A failed re-sync (or one
        # that could not run) counts as down all the units that have a file
        # for the worker plus the ones that were stopped, even if systemd sees
        # one as active: its PIDFile may still name the destroyed container.
        # This way the deploy exits non-zero even if the unit had been idle
        # before.
        iter_down=0
        want_units="$iter_units"
        if [ "$resync_failed" -eq 1 ]; then
            for u in "$(worker_unit "$i")" "$(proxy_unit "$i")"; do
                if [ -f "$ucfg/$u" ]; then
                    case " $want_units " in *" $u "*) ;; *) want_units="$want_units $u" ;; esac
                fi
            done
            # shellcheck disable=SC2086 # intentional word-split: see above
            for u in $want_units; do
                down_units="$down_units $u"
            done
            [ -z "$want_units" ] || iter_down=1
            want_units=""
        fi
        # shellcheck disable=SC2086 # intentional word-split: see above
        for u in $want_units; do
            if ! run_as_worker systemctl --user is-active --quiet "$u" 2>/dev/null; then
                echo "WARNING: unit $u of worker $i did NOT become active after recreating it. The container runs the new code, but this host no longer survives a reboot." >&2
                echo "       Starting it is not enough: it is an adoption unit and its PIDFile names the container this deploy destroyed, so it must be REGENERATED against the new one. Run: $(autostart_cmd "$i")" >&2
                down_units="$down_units $u"
                iter_down=1
            fi
        done
        if [ "$iter_down" -eq 1 ]; then
            down_workers="$down_workers $i"
        fi
    done

    # The script does not abort in the middle of the loop when a unit does not
    # come back (leaving the following workers on dead inodes is worse than a
    # stopped unit with a healthy container), but it does exit non-zero at the
    # end: push-release.sh is fail-fast per host, so a non-zero exit is what
    # stops the fleet and puts the problem in front of the operator. With
    # exit 0, "this host no longer survives a reboot" would be lost among the
    # rest of the log.
    if [ -n "$down_units" ]; then
        echo "ERROR: the deploy recreated all the containers, but some systemd --user units remain stopped:$down_units" >&2
        echo "       They are regenerated with 06-autostart.sh, one run per affected worker, and NOT with 'systemctl --user start': the adoption unit embeds the PIDFile of the container the deploy destroyed (see the header of 06-autostart.sh)." >&2
        # shellcheck disable=SC2086 # intentional word-split: see above
        for n in $down_workers; do
            echo "       $(autostart_cmd "$n")" >&2
        done
        echo "       The new code is installed and the workers are running, but the ones that lost their unit do not come back on their own after a reboot." >&2
        exit 1
    fi
fi

echo "OK  release installed."
