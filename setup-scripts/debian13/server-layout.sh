#!/usr/bin/env bash
# =============================================================================
# server-layout.sh — Aligns the host with the Python package that the tree
# installed in INSTALL_DIR ships: `orcaster_server` (since the rename) or `app`
# (earlier releases). Called by 03-app-install.sh and deploy.sh; it can also be
# run by hand.
#
# Subcommands:
#   align-unit      Rewrites the systemd unit's ExecStart module
#                   (`uvicorn app.main:app` <-> `uvicorn orcaster_server.main:app`)
#                   so it matches the installed package. Backs up the unit
#                   before touching it and runs `systemctl daemon-reload`. It does
#                   not touch the drop-ins directory (<unit>.d/, e.g. artifacts.conf).
#                   Works both ways: a rollback to a release prior to the package
#                   rename sets the ExecStart back to `app.main:app`.
#   restore-unit F  Restores the unit from backup F and runs daemon-reload.
#   migrate-static  Moves into the installed package the files left in the
#                   other package's tree (server/app/ after the rename). After
#                   03's rsync --delete only the files that exist only on the
#                   host survive there (excluded via HOST_ONLY_STATIC), so every
#                   regular file left over belongs to the host. It never overwrites
#                   a different file: it leaves it where it is and exits with an error.
#                   The versioned shim server/app/__init__.py (alias app.* ->
#                   orcaster_server.*) does reach server/app/ with the
#                   rsync: it is tolerated only if it is byte for byte the one in
#                   --shim-source and is never moved or deleted. Without
#                   --shim-source, or with a different __init__.py (e.g. a full
#                   old app/), server/app/ with Python code still aborts.
#
# Options:
#   --install-dir DIR   default /opt/orcaster
#   --app-user USER     owner of INSTALL_DIR; default orcaster
#   --unit FILE         default /etc/systemd/system/orcaster.service
#   --backup-dir DIR    unit backups; default /var/backups/orcaster
#   --shim-source FILE  server/app/__init__.py of the tree being deployed (03 passes
#                       $REPO_ROOT/server/app/__init__.py); migrate-static only
#   --no-reload         does not run daemon-reload (the caller will)
#   --dry-run           only shows what it would do
#
# Runs as root. It is self-contained (it does not source lib.sh) because deploy.sh
# sends it through `ssh ... sudo bash -s` from the operator's checkout: this way a
# rollback to a release without this script also aligns the unit.
# Inside INSTALL_DIR (owned by APP_USER) it moves and deletes as APP_USER, not as
# root: root would follow a symbolic link planted there to outside the tree.
# =============================================================================
set -euo pipefail

INSTALL_DIR="/opt/orcaster"
APP_USER="orcaster"
UNIT_FILE="/etc/systemd/system/orcaster.service"
BACKUP_DIR="/var/backups/orcaster"
RELOAD=1
DRY_RUN=0
SHIM_SOURCE=""
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

log()  { printf '>> %s\n' "$*"; }
ok()   { printf 'OK %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { printf 'ERROR %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# Runs as APP_USER. If we already are APP_USER (tests, or a run without root)
# it runs directly.
as_owner() {
    if [ "$(id -un)" = "$APP_USER" ]; then
        ( cd / && "$@" )
    else
        ( cd / && sudo -u "$APP_USER" "$@" )
    fi
}

# Package shipped by the installed tree: prints "<new> <other>".
detect_package() {
    local srv="$INSTALL_DIR/server"
    if [ -f "$srv/orcaster_server/main.py" ]; then
        echo "orcaster_server app"
    elif [ -f "$srv/app/main.py" ]; then
        echo "app orcaster_server"
    else
        die "Cannot find the server package in $srv (neither orcaster_server/main.py nor app/main.py). Has the rsync of 03-app-install.sh run yet?"
    fi
}

# Is $1 the versioned shim? Only if it is a regular file (not a link)
# byte for byte identical to SHIM_SOURCE. An arbitrary __init__.py does not count.
is_shim() {
    [ -n "$SHIM_SOURCE" ] && [ -f "$SHIM_SOURCE" ] \
        && [ -f "$1" ] && [ ! -L "$1" ] && cmp -s "$SHIM_SOURCE" "$1"
}

# Script path for the messages. Through `ssh ... bash -s` (deploy.sh) $0 is
# "bash", not a file: the copy in the root checkout is named instead.
self_path() {
    if [ -f "$0" ] && [ "$(basename "$0")" = server-layout.sh ]; then
        printf '%s' "$0"
    else
        printf '%s' "<root checkout>/setup-scripts/debian13/server-layout.sh"
    fi
}

reload_units() {
    [ "$RELOAD" -eq 1 ] || { log "daemon-reload skipped (--no-reload)."; return 0; }
    "$SYSTEMCTL" daemon-reload
}

cmd_align_unit() {
    local pkg other current backup tmp
    read -r pkg other < <(detect_package)
    if [ ! -f "$UNIT_FILE" ]; then
        log "$UNIT_FILE does not exist (first install: 05-systemd.sh writes it). Nothing to align."
        return 0
    fi
    # `|| true`: with no matches grep exits 1 and, with pipefail, set -e would
    # cut the script silently instead of reaching the die below.
    current="$( { grep -oE 'uvicorn[[:space:]]+(app|orcaster_server)\.main:app' "$UNIT_FILE" || true; } \
        | sed -E 's/^uvicorn[[:space:]]+//; s/\.main:app$//' | sort -u)"
    case "$current" in
        "$pkg")
            ok "ExecStart of $UNIT_FILE already points to $pkg.main:app."
            return 0 ;;
        "$other") ;;
        "")
            die "The ExecStart of $UNIT_FILE runs neither 'uvicorn app.main:app' nor 'uvicorn orcaster_server.main:app'; I am not touching it. Check it by hand or rewrite it with 05-systemd.sh." ;;
        *)
            die "$UNIT_FILE names more than one uvicorn module ($(echo "$current" | tr '\n' ' ')); I am not touching it." ;;
    esac
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[dry-run] would change the ExecStart of $UNIT_FILE: $other.main:app -> $pkg.main:app, with a backup in $BACKUP_DIR."
        return 0
    fi
    install -d -m 700 "$BACKUP_DIR"
    backup="$BACKUP_DIR/$(basename "$UNIT_FILE").$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$UNIT_FILE" "$backup"
    # Temporary file in the same directory: mv is atomic and systemd never sees
    # a half-written unit. cp -p keeps the original's mode and owner.
    tmp="$(mktemp "$(dirname "$UNIT_FILE")/.$(basename "$UNIT_FILE").XXXXXX")"
    cp -p "$UNIT_FILE" "$tmp"
    sed -E "s/(uvicorn[[:space:]]+)$other\.main:app/\1$pkg.main:app/" "$UNIT_FILE" > "$tmp"
    mv -f "$tmp" "$UNIT_FILE"
    reload_units
    ok "ExecStart of $UNIT_FILE: $other.main:app -> $pkg.main:app. Backup: $backup"
    log "To roll back: sudo bash $(self_path) restore-unit '$backup' --unit '$UNIT_FILE'"
}

cmd_restore_unit() {
    local src="$1"
    [ -f "$src" ] || die "Backup $src does not exist."
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[dry-run] would restore $UNIT_FILE from $src."
        return 0
    fi
    cp -p "$src" "$UNIT_FILE"
    reload_units
    ok "$UNIT_FILE restored from $src."
}

cmd_migrate_static() {
    local pkg other srv old new rel dest conflicts=0 moved=0 f
    read -r pkg other < <(detect_package)
    srv="$INSTALL_DIR/server"
    old="$srv/$other"
    new="$srv/$pkg"
    if [ ! -e "$old" ] && [ ! -L "$old" ]; then
        ok "$old no longer exists: nothing to migrate."
        return 0
    fi
    [ -L "$old" ] && die "$old is a symbolic link; I am not following it. Check it by hand."
    # A .py outside __pycache__ means the old tree is still whole (the rsync did
    # not run, or ran without --delete): it is not a host leftover.
    # Only exception: the versioned shim (is_shim), which the rsync leaves there on purpose.
    while IFS= read -r -d '' f; do
        if [ "$f" = "$old/__init__.py" ] && is_shim "$f"; then
            continue
        fi
        die "$old still has Python code ($f): the tree is half synchronized. I am not moving anything; run the rsync of 03-app-install.sh first."
    done < <(find "$old" -name '*.py' -not -path '*/__pycache__/*' -print0)
    while IFS= read -r -d '' f; do
        warn "Symbolic link in the old tree, not following or moving it: $f"
        conflicts=$((conflicts + 1))
    done < <(find "$old" -type l -print0)
    while IFS= read -r -d '' f; do
        rel="${f#"$old"/}"
        # The shim stays where it is: it is not a host leftover.
        if [ "$f" = "$old/__init__.py" ] && is_shim "$f"; then
            continue
        fi
        dest="$new/$rel"
        if [ -e "$dest" ] || [ -L "$dest" ]; then
            if [ -f "$dest" ] && [ ! -L "$dest" ] && cmp -s "$f" "$dest"; then
                if [ "$DRY_RUN" -eq 1 ]; then
                    log "[dry-run] $rel is already identical in $pkg; would delete the old copy."
                else
                    as_owner rm -f -- "$f"
                fi
            else
                warn "Conflict: $dest already exists and differs from $f. I am not touching either; decide by hand which one stays."
                conflicts=$((conflicts + 1))
            fi
            continue
        fi
        if [ "$DRY_RUN" -eq 1 ]; then
            log "[dry-run] would move $other/$rel -> $pkg/$rel"
        else
            as_owner mkdir -p -- "$(dirname "$dest")"
            as_owner mv -n -- "$f" "$dest"
            log "Moved: $other/$rel -> $pkg/$rel"
        fi
        moved=$((moved + 1))
    done < <(find "$old" -type f -not -path '*/__pycache__/*' -print0)
    if [ "$DRY_RUN" -eq 0 ]; then
        as_owner find "$old" -depth -type d -name __pycache__ -exec rm -rf -- {} +
        as_owner find "$old" -depth -type d -empty -delete
    fi
    if [ "$conflicts" -gt 0 ]; then
        die "$conflicts file(s) of $old not migrated (see warnings). They stay in place."
    fi
    # Empty except for the shim (if it is the versioned one): anything else is a leftover.
    if [ -e "$old" ] && [ "$DRY_RUN" -eq 0 ] \
        && find "$old" -mindepth 1 -not -path "$old/__init__.py" -print -quit | grep -q .; then
        die "$old was not left empty after the migration; check it by hand."
    fi
    ok "Migration from $other to $pkg: $moved host file(s)."
}

main() {
    local sub="${1:-}" restore_src=""
    [ -n "$sub" ] || usage 1
    shift
    case "$sub" in
        -h|--help) usage 0 ;;
        align-unit|migrate-static) ;;
        restore-unit)
            restore_src="${1:-}"
            [ -n "$restore_src" ] || die "restore-unit needs the path of the backup."
            shift ;;
        *) die "Unknown subcommand: $sub (use --help)." ;;
    esac
    while [ $# -gt 0 ]; do
        case "$1" in
            --install-dir) INSTALL_DIR="${2:?--install-dir needs a value}"; shift ;;
            --app-user)    APP_USER="${2:?--app-user needs a value}"; shift ;;
            --unit)        UNIT_FILE="${2:?--unit needs a value}"; shift ;;
            --backup-dir)  BACKUP_DIR="${2:?--backup-dir needs a value}"; shift ;;
            --shim-source) SHIM_SOURCE="${2:?--shim-source needs a value}"; shift ;;
            --no-reload)   RELOAD=0 ;;
            --dry-run)     DRY_RUN=1 ;;
            *) die "Unknown option: $1 (use --help)." ;;
        esac
        shift
    done
    case "$INSTALL_DIR" in
        /*) ;;
        *) die "--install-dir must be an absolute path (got: '$INSTALL_DIR')." ;;
    esac
    INSTALL_DIR="${INSTALL_DIR%/}"
    case "$sub" in
        align-unit)     cmd_align_unit ;;
        restore-unit)   cmd_restore_unit "$restore_src" ;;
        migrate-static) cmd_migrate_static ;;
    esac
}

main "$@"
