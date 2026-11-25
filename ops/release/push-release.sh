#!/usr/bin/env bash
# push-release: pushes a release package to the fleet hosts and installs it.
# Runs on the operator's machine.
#
# Code flow is one-way: local repo, tarball with manifest, scp, host. The
# host never reaches out to GitHub and needs no credential.
#
# Inventory: deploy.local.env (not versioned, see this suite's README.md for
# the details of the keys), with
#   WORKER_HOSTS="worker-1 worker-2"
#   WORKER_COUNT_worker_1=1
#   CONFIG_DIR_worker_1=/srv/orcaster
#
# In the per-host keys, hyphens AND DOTS in the host name are translated to
# underscores (worker.example.com -> CONFIG_DIR_worker_example_com): shell
# variable names allow neither of them.
#
# CONFIG_DIR_<host> is optional (without the key the host uses the default
# /etc/orcaster of install-release.sh) and is the ONLY channel for the config
# directory of a host that does not keep it there: it is propagated as
# --config-dir, because neither `sudo` with env_reset nor ssh propagate the
# CONFIG_DIR environment variable to the host.
#
# Usage:
#   ops/release/push-release.sh --ref vX.Y.Z
#   ops/release/push-release.sh --hosts worker-1 --dry-run
#   ops/release/push-release.sh --out /tmp/release-dist   # same --out as make-release.sh
#   ops/release/push-release.sh --hosts worker-1 --keep-staging
#
# The installer and the tarball are copied to a private directory on the host
# (mktemp -d, 0700, owned by the ssh user), never to a fixed name in /tmp.
# It is always deleted, on success or failure. --keep-staging keeps it ONLY if
# the deploy fails (never on success) to diagnose it: it prints host, path and
# the command `ssh <host> "rm -rf -- '<dir>'"` to delete it later.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REF="HEAD"
HOSTS=""
DRY=0
KEEP_STAGING=0
OUT="$REPO_ROOT/dist"

# Template of the staging directory on the host (expanded THERE: the ssh
# user's TMPDIR or /tmp). The real name is chosen by `mktemp -d`.
# shellcheck disable=SC2016
STAGE_TEMPLATE='${TMPDIR:-/tmp}/push-release.XXXXXXXXXX'
# Live host and staging directory; the EXIT trap deletes them if the script
# aborts (failed scp, failed install, Ctrl-C). Empty = nothing to clean up.
STAGE_HOST=""
STAGE_DIR=""

cleanup_stage() {
    # First statement: $? is the status the script exits with.
    local rc=$?
    [ -n "$STAGE_DIR" ] || return 0
    if [ "$KEEP_STAGING" -eq 1 ] && [ "$rc" -ne 0 ]; then
        echo "WARNING: --keep-staging: the deploy failed and the staging directory is kept at $STAGE_HOST:$STAGE_DIR. Delete it afterwards with: ssh $STAGE_HOST \"rm -rf -- '$STAGE_DIR'\"." >&2
        return 0
    fi
    # shellcheck disable=SC2029 # $STAGE_DIR was already validated when created.
    if ! ssh "$STAGE_HOST" "rm -rf -- '$STAGE_DIR'"; then
        echo "WARNING: could not delete the staging directory $STAGE_DIR on $STAGE_HOST. Delete it by hand with: ssh $STAGE_HOST \"rm -rf -- '$STAGE_DIR'\"." >&2
    fi
}
trap cleanup_stage EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref needs a value}"; shift ;;
        --hosts) HOSTS="${2:?--hosts needs a value}"; shift ;;
        --out) OUT="${2:?--out needs a value}"; shift ;;
        --dry-run) DRY=1 ;;
        --keep-staging) KEEP_STAGING=1 ;;
        -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1 (use --help)" >&2; exit 1 ;;
    esac
    shift
done

# Test seam: allows redirecting the inventory without touching the operator's
# real deploy.local.env, which may not even exist in the checkout (not
# versioned). Same idiom as ENV_FILE_OVERRIDE in lib.sh for load_env. It is
# not the same as a generic --out per environment: there is no honest
# command-line interface for "which inventory file to use" (the file is not an
# operator argument, it is fixed infrastructure of the machine), so the
# variable seam is the right choice here.
deploy_env_file="${DEPLOY_ENV_FILE_OVERRIDE:-$REPO_ROOT/deploy.local.env}"

# Sentinel to tell "the file exists but could not be sourced" (unclosed quote,
# invalid syntax, or bash itself missing) apart from "it was sourced fine but
# the key is not declared". Same idiom as SETUP_ENV_MISREAD in the Makefile
# (a repo-root variable that solves the same problem for setup.env): a value
# that no legitimate inventory key could ever take.
DEPLOY_ENV_MISREAD="__deploy_env_misread__"

# Checks that deploy.local.env is sourceable ONCE, early, before reading any
# key and before building the tarball. The check lives here and not inside
# read_inventory_var (further below) or the per-host loop: if it lived there,
# a broken file would produce one message or one abort per key read
# (WORKER_HOSTS, and then a WORKER_COUNT_<host> for each host in the fleet)
# instead of just one.
#
# It is a FATAL ERROR, not a silent default: a deploy.local.env that is
# present but has invalid syntax is, if not checked, indistinguishable from a
# well-formed file that simply does not declare the key, so the WHOLE
# inventory would be silently ignored (empty WORKER_HOSTS, every
# WORKER_COUNT_<host> falling back to "the host resolves it" as if the
# operator had configured nothing). It aborts before invoking make-release.sh:
# there is no point in building a release only to not know which hosts to send
# it to or with how many workers.
if [ -f "$deploy_env_file" ]; then
    deploy_env_probe="$(bash -c '. "$1" >/dev/null 2>&1 && printf %s ok' _ \
        "$deploy_env_file" 2>/dev/null || printf %s "$DEPLOY_ENV_MISREAD")"
    if [ "$deploy_env_probe" != "ok" ]; then
        # The probe cannot tell "the source failed" apart from "the source
        # worked but its last command returned non-zero" (the `&&` does not
        # distinguish), so the message names the three possible causes instead
        # of asserting only the first: an inventory that ends in, for example,
        # `[ -n "$X" ] && export Y=1` with X empty lands here without being
        # broken.
        echo "ERROR: $deploy_env_file exists but could not be sourced cleanly (invalid syntax, bash missing, or the file's last command returned non-zero). Check it with: bash -n '$deploy_env_file' and then with: bash -c '. \"$deploy_env_file\"'; echo \$?. No tarball was built; fix the file before retrying." >&2
        exit 1
    fi
fi

# Reads a single inventory key by sourcing the file in a SEPARATE bash, never
# in this shell. deploy.local.env is operator config and this script does not
# control it: sourcing it directly here would silently overwrite
# REF/OUT/HOSTS/DRY if the file happened to declare a variable with that name
# (`push-release.sh --ref vX.Y.Z` against an inventory that defines REF would
# build and push a different release than the requested one, without saying
# anything). Same idiom as read_setup_env_var in install-release.sh for the
# host's setup.env.
#
# Sourceability was already verified above, once: here an empty string means
# the key is not declared (or is declared empty), never that the file is
# broken. The `|| return 0` is a safety net for a race (the file changes
# between the probe above and this read), not a way of swallowing the
# "unsourceable" state: that state already aborted before getting here.
read_inventory_var() {
    local name="$1"
    [ -f "$deploy_env_file" ] || return 0
    bash -c '. "$1" >/dev/null 2>&1 && printf %s "${!2-}"' _ \
        "$deploy_env_file" "$name" 2>/dev/null || return 0
}

# Tests whether an inventory key is DECLARED, regardless of its value.
# read_inventory_var returns the same empty string for "not declared" and for
# "declared empty", and the two cases do not mean the same thing: without the
# key the host falls back to the /etc/orcaster default of install-release.sh
# on purpose, whereas an empty `CONFIG_DIR_<host>=` is a half-written
# inventory that would fall into that SAME default without a word in the log,
# that is, with the operator's override silently lost. `${!2+...}`
# distinguishes being declared from having a value; the `${!2-}` of
# read_inventory_var does not.
#
# Sourceability was already verified above: as in read_inventory_var, a
# failure of the child bash is treated as "not declared" (a safety net for the
# race where the file changes between the probe and this read), never as a way
# of swallowing a broken file, which already aborted before getting here.
inventory_var_is_declared() {
    local name="$1" probe
    [ -f "$deploy_env_file" ] || return 1
    probe="$(bash -c '. "$1" >/dev/null 2>&1 && printf %s "${!2+declarada}"' _ \
        "$deploy_env_file" "$name" 2>/dev/null || printf %s "")"
    [ "$probe" = "declarada" ]
}

# Translates a host name to the suffix of its inventory keys. Shell variable
# names only allow letters, digits and underscore, so hyphens AND DOTS are
# translated. Dots matter because a host can be an FQDN: worker.example.com
# produced the name `CONFIG_DIR_worker.example.com`, which is not a valid
# identifier, and the indirect expansion `${!2-}` of read_inventory_var
# returns an empty string with status 0 for an invalid name. That is: the
# operator declared CONFIG_DIR_worker_example_com (the only form writable as a
# shell assignment), the value was silently ignored, the deploy fell back to
# the default /etc/orcaster and aborted only at the remote host's pre-swap
# gate, which is exactly the failure that CONFIG_DIR_<host> exists to close.
# Translating dots cannot break any existing inventory: a key with a dot in
# its name was never writable, so it never worked.
#
# A name that is still not a valid identifier after translation (a
# `user@host`, a bracketed IPv6) is a FATAL ERROR and not "no key": letting it
# through returns empty and loses the override silently again, which is the
# defect above in another disguise. An empty suffix is rejected for the same
# reason: it would produce the keys `WORKER_COUNT_`/`CONFIG_DIR_`, valid
# identifiers but not this host's.
host_key_suffix() {
    local host="$1" suffix
    suffix="${host//[-.]/_}"
    case "$suffix" in
        ""|*[!A-Za-z0-9_]*)
            echo "ERROR: the host '$host' cannot be translated to keys of $deploy_env_file: they would be 'WORKER_COUNT_$suffix' and 'CONFIG_DIR_$suffix', which are not valid shell variable names (only letters, digits and underscore), so that host's inventory would ALWAYS be read as empty and its override would be silently lost. Give the host an alias in ~/.ssh/config with only letters, digits, hyphens or dots, use it in WORKER_HOSTS (or in --hosts) and declare the keys with that alias." >&2
            return 1 ;;
    esac
    printf %s "$suffix"
}

# Resolves a host's worker count: prints the normalized inventory
# WORKER_COUNT_<host>, or nothing if the key is not declared.
#
# --workers is optional (contract with install-release.sh): the host is the
# source of truth, through the WORKER_COUNT of its own setup.env. The
# operator's inventory is an override, not a mute default. Without that
# contract, a host without the key would fall into `${!var:-1}`: on a host
# with 3 workers that would pass `--workers 1` without saying anything, and
# install-release.sh would recreate only worker 1, leaving the other two on
# the bind mount of the old code. This function printing empty is what lets
# the host resolve it on its own: the caller omits the argument entirely,
# never passes "--workers 1" or "--workers ''".
#
# The value comes from the operator's deploy.local.env (a file this script
# explicitly does not control) and ends up interpolated into the string that
# runs on the remote host, so it is validated HERE, at the sender, with the
# same criterion as validate_workers in install-release.sh (integer >= 1).
# Without this validation a value that is not a number stops being an argument
# and becomes loose words on the remote side: with
# `WORKER_COUNT_host="1; echo x"` the host's shell runs a compound `A; B`, and
# `ssh` returns the status of B, so a FAILED install would reach the "OK
# $host". Validating on the host side (install-release.sh) is not enough: by
# then the string has already been split.
resolve_worker_count() {
    local host="$1" suffix var count
    suffix="$(host_key_suffix "$host")" || return 1
    var="WORKER_COUNT_$suffix"
    count="$(read_inventory_var "$var")"
    [ -n "$count" ] || return 0
    case "$count" in
        *[!0-9]*)
            echo "ERROR: $var in $deploy_env_file must be an integer >= 1 (it is: '$count')." >&2
            return 1 ;;
    esac
    [ "$((10#$count))" -ge 1 ] \
        || { echo "ERROR: $var in $deploy_env_file must be >= 1 (it is: '$count')." >&2; return 1; }
    printf %s "$((10#$count))"
}

# Resolves a host's config directory: prints the inventory's CONFIG_DIR_<host>,
# or nothing if the key is not declared (the default: falls back to the
# /etc/orcaster of install-release.sh).
#
# This key exists because the environment is NOT a channel to the host: the
# invocation further down is `sudo bash <staging>/install-release.sh` (sudo with
# env_reset, the Debian default, does not propagate variables) and it travels
# over ssh (which does not accept CONFIG_DIR either: it is not in AcceptEnv).
# Without a per-host --config-dir, a host whose config lives outside
# /etc/orcaster (the hosts that cloud-init-worker.sh provisions with a templated
# CONFIG_DIR are exactly that case) could not be deployed through the supported
# path: the pre-swap gate of install-release.sh aborted asking for
# /etc/orcaster/setup.env and pointing to the tarball migration, on a host that
# never had to migrate.
#
# Same reason and same rigor as the $count validation: the value comes from the
# operator's deploy.local.env and ends up interpolated into the string that runs
# on the host, so a value with spaces or metacharacters stops being one argument
# and becomes loose words on the remote side. With
# `CONFIG_DIR_host="/x; echo y"` the host shell runs a compound `A; B` and `ssh`
# returns the status of B, so a FAILED install would reach the "OK $host".
# Validating it only on the host side (install-release.sh validates CONFIG_DIR
# with this same character set) is not enough: by then the string has already
# been split. Here an absolute path of safe characters is required and, like the
# receiver, the root and '..' as a component are rejected, so that a bad
# inventory value aborts in the pre-flight and not halfway through the deploy.
# Trailing slashes are still stripped by install-release.sh; here they are only
# trimmed for the root guard's verdict, never for what is propagated.
resolve_config_dir() {
    local host="$1" suffix var config_dir
    suffix="$(host_key_suffix "$host")" || return 1
    var="CONFIG_DIR_$suffix"
    config_dir="$(read_inventory_var "$var")"
    if [ -z "$config_dir" ]; then
        # Absent is a legitimate choice (the installer's default); declared
        # but empty is a half-written inventory, and both used to read as the
        # same empty string. That is why an empty key is fatal here instead of
        # silently falling back to the default: that silence left the
        # operator's override lost and the host's pre-swap gate aborting, which
        # is the very failure CONFIG_DIR_<host> was meant to close.
        if inventory_var_is_declared "$var"; then
            echo "ERROR: $var is declared in $deploy_env_file but empty, and an empty key says nothing about where the config of $host lives: the deploy would fall back to the default /etc/orcaster and only abort at the host's pre-swap gate. Give it the absolute path of that host's config directory, or delete the key if the host uses /etc/orcaster." >&2
            return 1
        fi
        return 0
    fi
    case "$config_dir" in
        *[!A-Za-z0-9/._-]*)
            echo "ERROR: $var in $deploy_env_file only allows letters, digits and the characters / . _ - (it is: '$config_dir')." >&2
            return 1 ;;
    esac
    case "$config_dir" in
        /*) ;;
        *)
            echo "ERROR: $var in $deploy_env_file must be an absolute path (it is: '$config_dir')." >&2
            return 1 ;;
    esac
    # The two guards below judge the value with trailing slashes trimmed,
    # without overwriting the one that is propagated: it is deliberately not
    # normalized here (the receiver strips them), but a bare '= "/"' would
    # let '//' through, which on the host collapses to '/' and falls into the
    # root guard of install-release.sh. Same technique as the comparison in
    # install-release.sh: trim to judge, not to emit. The trim mirrors
    # rtrim_slashes (in install-release.sh): it keeps the '/'.
    local trimmed="$config_dir"
    while [ "$trimmed" != "/" ] && [ "${trimmed%/}" != "$trimmed" ]; do trimmed="${trimmed%/}"; done
    if [ "$trimmed" = "/" ]; then
        echo "ERROR: $var in $deploy_env_file cannot be the root '/' (it is: '$config_dir'). install-release.sh rejects it at the host gate, so the deploy would abort midway through the fleet: give it the absolute path of that host's config directory, or delete the key if it uses the default /etc/orcaster." >&2
        return 1
    fi
    # '..' per COMPONENT, not as a substring: a directory 'a..b' is legal. The
    # value starts with '/' (guard above), so a trailing '*/..' is enough and a
    # leading '..' falls into '*/../*' all the same.
    case "$trimmed" in
        */../*|*/..)
            echo "ERROR: $var in $deploy_env_file does not allow '..' as a path component (it is: '$config_dir'). install-release.sh rejects it at the host gate, so the deploy would abort midway through the fleet: pass the already resolved path. A directory named 'a..b' is legal: the guard measures components, not substrings." >&2
            return 1 ;;
    esac
    printf %s "$config_dir"
}

# The inventory is ALWAYS read when it exists, also with --hosts. `--hosts`
# replaces the host LIST, not the whole inventory: the WORKER_COUNT_<host> keys
# live in the same file and apply just as well to the subset being pushed.
# Reading it only in the branch without --hosts left `push-release.sh --hosts
# worker-1` (the single-host form) without the host's worker count. Without the
# file, --hosts still works: the inventory is optional when the list comes from
# the command line.
if [ -z "$HOSTS" ]; then
    [ -f "$deploy_env_file" ] \
        || { echo "ERROR: deploy.local.env is missing and --hosts was not given." >&2; exit 1; }
    HOSTS="$(read_inventory_var WORKER_HOSTS)"
    [ -n "$HOSTS" ] \
        || { echo "ERROR: deploy.local.env does not define WORKER_HOSTS." >&2; exit 1; }
fi

# Pre-flight: validates the inventory of ALL fleet hosts BEFORE building the
# tarball and, above all, before the first ssh. These validations would
# otherwise live inside the per-host loop below, i.e. be lazy per host: with
# WORKER_HOSTS="host-a host-b" and a malformed CONFIG_DIR_host_b, host-a would
# already have the release installed and its workers recreated (the `ssh ...
# sudo bash /tmp/install-release.sh` of the loop) when the script aborted on
# host-b. The result would be a half-deployed fleet caused by an inventory
# typo, which is information available before touching anything. Same idiom as
# the sourceability check above: what can be known about the inventory is known
# early and all at once.
#
# The output is discarded: only the abort matters here. The per-host loop calls
# the same resolvers again to USE the value, so what gets interpolated into the
# remote string is always a validated value, even if the inventory changed
# between the two reads.
for host in $HOSTS; do
    resolve_worker_count "$host" >/dev/null || exit 1
    resolve_config_dir "$host" >/dev/null || exit 1
done

echo ">> 1 · Building the package from $REF" >&2
# make-release.sh prints the artifact path as the only stdout line (the rest
# goes to stderr, a contract pinned by its own test), so it is captured
# directly. Picking it afterwards by name from dist/ would be a lottery: with
# two releases there, `sort` is lexicographic and not chronological, and the
# SHA suffix decides which one wins.
tgz="$("$REPO_ROOT/ops/release/make-release.sh" --ref "$REF" --out "$OUT")"
[ -n "$tgz" ] && [ -f "$tgz" ] \
    || { echo "ERROR: make-release did not return a usable tarball." >&2; exit 1; }
base="$(basename "$tgz")"

for host in $HOSTS; do
    # Both values already went through the pre-flight above, for the WHOLE
    # fleet: here they are resolved again to USE them, and the `|| exit 1` is
    # not decorative (the inventory could have changed between the two reads,
    # and the value interpolated into the remote string has to be a validated
    # one).
    count="$(resolve_worker_count "$host")" || exit 1
    config_dir="$(resolve_config_dir "$host")" || exit 1
    # A note is appended to the host's log line only when the key is declared:
    # for the default (without the key) the output stays unchanged.
    cfg_note=""
    if [ -n "$config_dir" ]; then
        cfg_note=", config-dir: $config_dir (from deploy.local.env)"
    fi
    # --tarball is prepended further down: the path depends on this host's
    # staging directory, which in a real deploy only exists after the remote
    # mktemp.
    install_args=""
    if [ -n "$count" ]; then
        install_args="--workers $count"
        echo ">> 2 · $host (workers: $count, from deploy.local.env$cfg_note)" >&2
    else
        echo ">> 2 · $host (workers: resolved by the host, via WORKER_COUNT in its setup.env$cfg_note)" >&2
    fi
    # Appended AFTER --workers so that the host string stays in the same order
    # in which install-release.sh --help documents it, and above all so that
    # the --dry-run preview (which prints $install_args as is) shows the exact
    # string that would run as root, --config-dir included: that is the purpose
    # of the dry run.
    if [ -n "$config_dir" ]; then
        install_args="${install_args:+$install_args }--config-dir $config_dir"
    fi
    if [ "$DRY" -eq 1 ]; then
        # The preview shows the REAL pattern of the staging directory and the
        # EXACT string that would run as root (`sudo bash` included): the
        # --dry-run exists to audit what is going to run on the fleet. The
        # real name is chosen by `mktemp -d` on the host, so the pattern goes
        # here.
        dry_stage="$STAGE_TEMPLATE"
        echo "   [dry-run] ssh $host \"umask 077; mktemp -d $dry_stage\"  (0700 directory of the ssh user)" >&2
        echo "   [dry-run] scp $base + sha256 + install-release.sh -> $host:$dry_stage/" >&2
        echo "   [dry-run] ssh $host \"cd $dry_stage && sha256sum -c $base.sha256 && sudo bash $dry_stage/install-release.sh --tarball $dry_stage/$base${install_args:+ $install_args}\"" >&2
        if [ "$KEEP_STAGING" -eq 1 ]; then
            echo "   [dry-run] --keep-staging: the staging is deleted on success and KEPT if the deploy fails (host, path and the ssh rm -rf to delete it are printed)" >&2
        else
            echo "   [dry-run] ssh $host \"rm -rf -- $dry_stage\"  (remote trap on exit, success or failure)" >&2
        fi
        continue
    fi
    # Nothing is EVER deleted on the host from here, neither .git nor anything
    # else: install-release.sh is what decides how to replace the installed
    # code.
    #
    # Private staging: the installer and the tarball that root runs and
    # extracts travel through a directory with a random name, mode 0700 and
    # owned by the ssh user, NEVER through a predictable name in /tmp
    # (writable by anyone: another local user could get ahead or replace the
    # file between the scp and the sudo). umask 077 means mktemp -d never has
    # even a moment with more open permissions.
    # shellcheck disable=SC2016 # ${TMPDIR:-/tmp} is expanded by the host, not this shell.
    stage="$(ssh "$host" 'umask 077; mktemp -d "${TMPDIR:-/tmp}/push-release.XXXXXXXXXX"')" \
        || { echo "ERROR: could not create the staging directory on $host (mktemp -d failed); nothing was copied." >&2; exit 1; }
    # The path comes back from the host and is interpolated into commands that
    # run there (the rm -rf included): it is validated before use. Absolute,
    # of safe characters, without '..' and with the exact name the template
    # produces.
    if ! [[ "$stage" =~ ^/[A-Za-z0-9/._-]*/push-release\.[A-Za-z0-9]{10}$ ]] || [[ "$stage" == *..* ]]; then
        echo "ERROR: host $host returned an unexpected staging path ('$stage'); it is not used and nothing is deleted." >&2
        exit 1
    fi
    # From here on the local cleanup (EXIT trap) deletes $stage if the script
    # aborts before the install ssh finishes successfully.
    STAGE_HOST="$host"
    STAGE_DIR="$stage"
    # Owner and mode are checked on the host, with the already validated path:
    # a directory that does not belong to the ssh user or is not 0700 is not
    # used.
    # shellcheck disable=SC2029 # $stage is the client's on purpose, already validated.
    ssh "$host" "[ -d '$stage' ] && [ ! -L '$stage' ] && [ -O '$stage' ] && [ \"\$(stat -c %a '$stage')\" = 700 ]" \
        || { echo "ERROR: the staging $stage on $host is not a 0700 directory of the ssh user; nothing was copied." >&2; exit 1; }
    scp "$tgz" "$tgz.sha256" "$REPO_ROOT/ops/release/install-release.sh" "$host:$stage/"
    install_args="--tarball $stage/$base${install_args:+ $install_args}"
    # A single ssh session: the remote trap deletes the staging on exit
    # (success, failure or disconnection), the sha256 verification runs on the
    # host BEFORE the `sudo bash`, and install-release.sh requires root and
    # does not re-execute itself with sudo on its own: that is why the
    # invocation is explicitly `sudo bash`.
    # Under `set -e` a failure aborts here (fail-fast on purpose: it does not
    # continue with the next host) and the local EXIT trap does the backup
    # deletion in case the session was cut before the remote trap ran.
    if [ "$KEEP_STAGING" -eq 1 ]; then
        # With --keep-staging the remote trap deletes only if the session ends well.
        remote_trap="trap 'rc=\$?; if [ \"\$rc\" -eq 0 ]; then rm -rf -- \"\$d\"; fi' EXIT"
    else
        remote_trap="trap 'rm -rf -- \"\$d\"' EXIT HUP INT TERM"
    fi
    # shellcheck disable=SC2029 # the remote command is built with $stage, $base, $count and $config_dir on the client side on purpose, already validated.
    ssh "$host" "d='$stage'; $remote_trap; cd '$stage' && sha256sum -c '$base.sha256' && sudo bash $stage/install-release.sh $install_args"
    STAGE_HOST=""
    STAGE_DIR=""
    echo "   OK $host" >&2
done

echo "OK  release pushed to: $HOSTS" >&2
