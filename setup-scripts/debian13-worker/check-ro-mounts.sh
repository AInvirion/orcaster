#!/usr/bin/env bash
# check-ro-mounts: validates WORKER_RO_MOUNTS, the list of HOST directories
# that the worker container receives as read-only bind mounts at the SAME path
# (`-v <dir>:<dir>:ro`). Use case: a wheel in /opt/example-ro that a task
# installs with `file:///opt/example-ro/<wheel>`, which is resolved against
# the container filesystem, not the host's.
#
# Usage:
#   check-ro-mounts.sh '<list>'          # syntax only
#   check-ro-mounts.sh --host '<list>'   # syntax + host state
#
# Exits 0 if the list is valid (an empty list is valid: no extra mounts), 1 if
# it is not, with a message on stderr naming the offending entry, and 2 on a
# usage error.
#
# This is the ONLY implementation of the rule. It is used by load_env (lib.sh),
# the CONFIG of cloud-init-worker.sh, the pre-loop probe of run-fleet.sh, the
# pre-swap gate of ops/release/install-release.sh and the Makefile target that
# runs before the `podman rm -f` of `up-client`. It is executed (not sourced)
# and depends on nothing in the repo, so that install-release.sh can run it as
# $WORKER_USER by piping it through stdin (`bash -s`) without that user having
# to be able to read the extracted release tree.
#
# Syntax (without --host):
#   - absolute path;
#   - only characters [A-Za-z0-9._/-]: this excludes ':' and ',' (separators of
#     podman's -v option) and spaces, which are also the list separator;
#     quoting a path with spaces through bash, make's `$(shell)` and
#     `podman run` adds risk with no current need;
#   - canonical form: no '/', no double slash, no trailing slash, no '.' or
#     '..' components. The comparison against reserved paths is textual, so
#     '//etc' or '/opt/../etc' would evade it if accepted;
#   - neither equal to, below, nor a parent of a reserved path: those the
#     container already uses (code in /app, state in /var/lib/orcaster, cache,
#     egress allowlist, tmpfs HOME) and system ones. A mount there would hide
#     worker code, state or binaries;
#   - no duplicates: podman rejects two mounts with the same destination, and
#     it would do so AFTER the `podman rm -f` of the previous container.
#
# Host (--host). Two identities are involved and the script checks each rule
# against the right one:
#
#   - whoever runs the script (orcaster in production, which is who runs `make
#     up client`): rootless podman resolves the mount SOURCE as that user.
#     With that identity: it exists and is a directory (which requires
#     traversing all its ancestors), it can be read and traversed, and its
#     physical path (symlinks resolved) does not collide with a reserved one
#     either.
#
#   - the process INSIDE the container: it runs as uid 1000 of the image
#     (`USER orcaster` in client/Dockerfile) without --userns=keep-id. In
#     rootless podman, container uid 0 is the host's orcaster and uid 1000 is
#     a host SUBUID, which is neither the owner nor in the group of anything
#     the operator created: inside, the owner appears as root and the group as
#     0. Only the "other" bits reach it. So the directory has to be o+rx and
#     EVERYTHING under it o+r (files) or o+rx (directories). A
#     `0750 root:orcaster` or a `0700 orcaster` would pass a `test -r` run as
#     the host's orcaster and still be unreadable inside. An unreadable mount
#     does NOT fail at `podman run`: it only fails in the task that needs it,
#     with EACCES.
set -euo pipefail

RESERVED="/app /var/lib/orcaster /var/cache/orcaster /var/lib/orcaster-egress /home/orcaster /proc /sys /dev /etc /usr /bin /sbin /lib /lib64 /var /tmp /root /run"

fail() {
    printf 'ERROR: WORKER_RO_MOUNTS: %s\n' "$*" >&2
    exit 1
}

# Prints the reserved path that $1 collides with (equal, below or parent) and
# returns 0; returns 1 if it collides with none.
reserved_conflict() {
    local p="$1" r
    for r in $RESERVED; do
        case "$p" in
            "$r"|"$r"/*) printf '%s' "$r"; return 0 ;;
        esac
        case "$r" in
            "$p"/*) printf '%s' "$r"; return 0 ;;
        esac
    done
    return 1
}

host=0
if [ "${1:-}" = "--host" ]; then
    host=1
    shift
fi
if [ "$#" -ne 1 ]; then
    echo "usage: check-ro-mounts.sh [--host] '<space-separated list of directories>'" >&2
    exit 2
fi

# Split the list into words with the default IFS (space, tab, newline; make's
# `$(shell)` already turns newlines into spaces) and WITHOUT glob expansion: a
# '*' has to reach the character validation as is, not turn into the files of
# the cwd.
set -f
# shellcheck disable=SC2086 # intentional word-split of the list
set -- $1
set +f
[ "$#" -gt 0 ] || exit 0

# First pass: syntax of ALL entries before looking at the host, so that a
# malformed entry is reported for what it is and not as "does not exist".
seen=" "
for e in "$@"; do
    case "$e" in
        /*) ;;
        *) fail "'$e' is not an absolute path. Each entry is a host directory that is mounted at the same path inside the container, e.g. /opt/example-ro." ;;
    esac
    [[ "$e" =~ ^[A-Za-z0-9._/-]+$ ]] \
        || fail "'$e' has characters outside [A-Za-z0-9._/-]. ':' and ',' separate the options of 'podman run -v' and spaces separate the entries of the list: rename or link the directory to a simple path."
    [[ "$e" =~ ^/+$ ]] \
        && fail "'$e' is the host root. Mount only the directory the task needs, e.g. /opt/example-ro."
    case "/$e/" in
        */./*|*/../*) fail "'$e' has a '.' or '..' component. Write the already resolved path." ;;
    esac
    case "$e" in
        *//*|*/) fail "'$e' is not in canonical form (double slash or trailing slash). Write it without extra slashes, e.g. /opt/example-ro." ;;
    esac
    if hit="$(reserved_conflict "$e")"; then
        fail "'$e' is, is under, or contains $hit, a path the container already uses or a system path. Mounting it would hide worker code, state or binaries."
    fi
    case "$seen" in
        *" $e "*) fail "'$e' is duplicated. podman rejects two mounts with the same destination." ;;
    esac
    seen="$seen$e "
done

[ "$host" -eq 1 ] || exit 0

user="$(id -un 2>/dev/null || id -u)"
for e in "$@"; do
    [ -d "$e" ] \
        || fail "'$e' does not exist on this host, is not a directory, or user $user cannot reach it. Create it (with the content the tasks expect) or remove it from WORKER_RO_MOUNTS."
    if [ ! -r "$e" ] || [ ! -x "$e" ]; then
        fail "user $user cannot read or traverse '$e'. podman resolves the mount source as that user. Grant permissions, e.g. chmod 0755 '$e'."
    fi
    real="$(cd "$e" && pwd -P)" \
        || fail "could not resolve the physical path of '$e' as user $user."
    if [ "$real" != "$e" ] && hit="$(reserved_conflict "$real")"; then
        fail "'$e' resolves to $real (symlink), which is, is under, or contains $hit. Mounting it would hide worker code, state or binaries."
    fi

    # Readability from the container uid: "other" bits (see header). We look
    # for the FIRST entry that does not comply: a directory without o+rx
    # (-perm -005) or a non-directory without o+r (-perm -004). Octal rather
    # than `o=rx` because BSD find (macOS, where the dev tests run) and GNU
    # find read it the same way. The outer parentheses matter: without them
    # `-print -quit` would apply only to the second branch of the -o.
    #
    # Symlinks: `-H` follows only the entry's own (podman mounts the resolved
    # target, and that target is what gets walked). Symlinks INSIDE the tree
    # are not followed (`-P`, find's default) and are not judged either: their
    # mode bits mean nothing (Linux does not consult them for access) and the
    # container resolves them against ITS filesystem. One that points inside
    # the same tree lands on an entry this find already checks on its own; one
    # that points outside refers to a container path, not a host path, and
    # host permissions say nothing about it.
    findrc=0
    bad="$(find -H "$e" \( \( -type d ! -perm -005 \) -o \( ! -type d ! -type l ! -perm -004 \) \) -print -quit 2>/dev/null)" \
        || findrc=$?
    if [ -n "$bad" ]; then
        fail "'$bad' is not readable by \"other\" (directories need o+rx and files o+r). The container process runs as uid 1000 of the image, which in rootless podman is a host subuid: it is not $user nor in its group, only the \"other\" bits reach it. An unreadable mount does not fail at creation, it only fails in the task. Fix it, e.g. chmod -R o+rX '$e'."
    fi
    if [ "$findrc" -ne 0 ]; then
        fail "could not walk all of '$e' as user $user (find exited with $findrc): some subdirectory cannot be read and what is below it could not be checked. Fix it, e.g. chmod -R o+rX '$e'."
    fi
done
exit 0
