#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Re-deploys the server on an already installed host, from the
# operator's machine.
#
# It follows the install layout of lib.sh: the installer runs as root from a
# root checkout (SOURCE_DIR, /root/orcaster) separate from
# INSTALL_DIR, and 03-app-install.sh copies that checkout to INSTALL_DIR.
#
#   1. Builds the tree to deploy: `git archive <ref>` of this script's repo
#      (default HEAD; only what is committed travels), or an already extracted
#      directory with --source.
#   2. rsync --delete of that tree to VPS:$SOURCE_DIR (as root via sudo) and
#      chown root:root + chmod go-w. Preserves setup.env and its backups.
#   3. 03-app-install.sh from SOURCE_DIR: rsync to INSTALL_DIR, uv sync,
#      server/.env, migration of the host files (HOST_ONLY_STATIC) and the
#      unit's ExecStart.
#   4. server-layout.sh align-unit, sent over ssh from THIS checkout: aligns
#      the ExecStart even if the deployed tree is from a release that does not
#      ship the script (rollback to a tag older than the package rename).
#   5. systemctl restart, stability check (active and without restarts
#      for DEPLOY_STABLE_SECONDS) and 08-smoke-test.sh. Any failure
#      shows the journal and ends with an error.
#
# Schema migrations run at startup (MIGRATE_ON_STARTUP=true).
# Do not use --migrate in production: it applies the new schema while the old
# process is still serving.
#
# Usage:
#   setup-scripts/debian13/deploy.sh                  # HEAD
#   setup-scripts/debian13/deploy.sh --ref v1.0.0     # a tag or commit
#   setup-scripts/debian13/deploy.sh --source DIR     # already extracted tree
#   setup-scripts/debian13/deploy.sh --dry-run        # rsync only, dry run
#   setup-scripts/debian13/deploy.sh --no-restart     # no restart or smoke
#   setup-scripts/debian13/deploy.sh --no-smoke
#   setup-scripts/debian13/deploy.sh --migrate        # explicit 04-migrate.sh
#
# Config: deploy.local.env at the repo root (outside git), or the path in
# DEPLOY_ENV. A template is created on the first run.
# =============================================================================
set -euo pipefail

DEPLOY_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEPLOY_REPO_ROOT="$(cd "$DEPLOY_SCRIPT_DIR/../.." && pwd -P)"
CONFIG_FILE="${DEPLOY_ENV:-$DEPLOY_REPO_ROOT/deploy.local.env}"

log()  { printf '>> %s\n' "$*"; }
ok()   { printf 'OK %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { printf 'ERROR %s\n' "$*" >&2; exit 1; }
step() { printf '\n── %s ──\n' "$*"; }

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

bootstrap_config() {
    cat > "$CONFIG_FILE" <<'EOF'
# Config for setup-scripts/debian13/deploy.sh. NOT versioned (.gitignore).
# SSH target: "user@host" or an alias from ~/.ssh/config. The user needs
# passwordless sudo on the host (or must be root).
VPS_SSH="user@orcaster.example.com"
# Root checkout the installer runs from (lib.sh).
SOURCE_DIR="/root/orcaster"
# INSTALL_DIR and APP_USER from the host's setup.env.
INSTALL_DIR="/opt/orcaster"
APP_USER="orcaster"
SERVICE="orcaster.service"
DISTRO="debian13"
# SSH port if you do not use an alias (empty = 22).
SSH_PORT=""
# Seconds the service must stay active and without restarts after the restart.
#DEPLOY_STABLE_SECONDS=20
EOF
    chmod 600 "$CONFIG_FILE"
}

# rsync exclusions for the root checkout. The tree comes from git archive, so
# it carries no local files; these rules protect the host state that lives in
# SOURCE_DIR from --delete: setup.env and its backups (03 excludes them the
# same way), and any <name>.env. rsync applies the first matching rule: the
# template's --include goes first.
deploy_rsync_excludes() {
    local distro="$1"
    printf '%s\n' \
        "--include=/setup-scripts/$distro/setup.env.example" \
        "--exclude=/setup-scripts/$distro/setup.env*" \
        "--exclude=*.env" \
        "--exclude=.git"
}

# Repository paths that are not deployed. Documentation is not needed at
# runtime and should not live on a host that could be handed over to a third
# party. They are removed from the extracted tree; since both rsync calls (this
# one and the one in 03) use --delete, they also disappear from the host.
deploy_omitted_paths() {
    printf '%s\n' \
        docs \
        llms.txt
}

# Tree to deploy in a temporary directory: git archive of REF, minus
# deploy_omitted_paths.
build_tree() {
    local ref="$1" dest="$2"
    git -C "$DEPLOY_REPO_ROOT" rev-parse --verify --quiet "$ref^{commit}" >/dev/null \
        || die "Ref '$ref' does not exist in $DEPLOY_REPO_ROOT."
    git -C "$DEPLOY_REPO_ROOT" archive --format=tar "$ref" | tar -x -C "$dest"
    local p
    while IFS= read -r p; do
        rm -rf "${dest:?}/$p"
    done < <(deploy_omitted_paths)
}

main() {
    local ref="HEAD" source="" dry_run=0 do_restart=1 do_smoke=1 do_migrate=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref)        ref="${2:?--ref needs a value}"; shift ;;
            --source)     source="${2:?--source needs a directory}"; shift ;;
            --dry-run)    dry_run=1; do_restart=0; do_smoke=0 ;;
            --no-restart) do_restart=0; do_smoke=0 ;;
            --no-smoke)   do_smoke=0 ;;
            --migrate)    do_migrate=1 ;;
            --fast)
                die "--fast no longer exists: with the root checkout the code only reaches INSTALL_DIR through 03-app-install.sh. Run without flags." ;;
            -h|--help)    usage 0 ;;
            *) die "Unknown flag: $1 (use --help)." ;;
        esac
        shift
    done

    command -v rsync >/dev/null 2>&1 || die "'rsync' is missing from PATH."
    command -v ssh   >/dev/null 2>&1 || die "'ssh' is missing from PATH."

    if [ ! -f "$CONFIG_FILE" ]; then
        bootstrap_config
        die "Created the template $CONFIG_FILE. Edit it (above all VPS_SSH) and run again."
    fi
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
    : "${VPS_SSH:?Define VPS_SSH in $CONFIG_FILE}"
    [ "$VPS_SSH" = "user@orcaster.example.com" ] && die "VPS_SSH still has the example value. Edit $CONFIG_FILE."
    SOURCE_DIR="${SOURCE_DIR:-/root/orcaster}"
    INSTALL_DIR="${INSTALL_DIR:-/opt/orcaster}"
    APP_USER="${APP_USER:-orcaster}"
    SERVICE="${SERVICE:-orcaster.service}"
    DISTRO="${DISTRO:-debian13}"
    # Seconds the service must stay active and without restarts after the restart.
    local stable_seconds="${DEPLOY_STABLE_SECONDS:-20}"
    [[ "$stable_seconds" =~ ^[0-9]+$ ]] && [ "$stable_seconds" -gt 0 ] \
        || die "DEPLOY_STABLE_SECONDS must be a positive integer (got: '$stable_seconds')."
    case "$SOURCE_DIR" in
        /*) ;;
        *) die "SOURCE_DIR must be an absolute path (got: '$SOURCE_DIR')." ;;
    esac
    case "${SOURCE_DIR%/}/" in
        "${INSTALL_DIR%/}/"*)
            die "SOURCE_DIR ($SOURCE_DIR) is inside INSTALL_DIR ($INSTALL_DIR). The installer runs from a separate root checkout (lib.sh)." ;;
    esac
    case "${INSTALL_DIR%/}/" in
        "${SOURCE_DIR%/}/"*)
            die "INSTALL_DIR ($INSTALL_DIR) is inside SOURCE_DIR ($SOURCE_DIR)." ;;
    esac

    local ssh_cmd=(ssh) rsync_rsh="ssh"
    if [ -n "${SSH_PORT:-}" ]; then
        ssh_cmd=(ssh -p "$SSH_PORT"); rsync_rsh="ssh -p $SSH_PORT"
    fi
    remote() { "${ssh_cmd[@]}" "$VPS_SSH" "$@"; }
    fail_with_journal() {
        warn "Last journal lines of $SERVICE:"
        remote "sudo journalctl -u '$SERVICE' -n 30 --no-pager" >&2 || true
        die "$1"
    }

    local setup_remote="$SOURCE_DIR/setup-scripts/$DISTRO"
    local tree tmp=""
    if [ -n "$source" ]; then
        [ -d "$source" ] || die "--source $source is not a directory."
        tree="$(cd "$source" && pwd -P)"
        log "Tree to deploy: $tree (--source)"
    else
        tmp="$(mktemp -d)"
        # shellcheck disable=SC2064  # tmp is fixed here on purpose
        trap "rm -rf '$tmp'" EXIT
        build_tree "$ref" "$tmp"
        tree="$tmp"
        log "Tree to deploy: git archive $ref ($(git -C "$DEPLOY_REPO_ROOT" rev-parse --short "$ref^{commit}"))"
    fi
    [ -f "$tree/setup-scripts/$DISTRO/03-app-install.sh" ] \
        || die "The tree does not contain setup-scripts/$DISTRO/03-app-install.sh."

    step "0 · Preconditions on $VPS_SSH"
    log "Target: $VPS_SSH (checkout $SOURCE_DIR -> INSTALL_DIR $INSTALL_DIR)"
    remote "sudo test -f '$setup_remote/setup.env'" \
        || die "There is no $setup_remote/setup.env on the host. One-time migration to the root checkout: move setup.env and its backups from INSTALL_DIR to the root checkout, chown root:root and chmod 600. See setup-scripts/$DISTRO/README.md."

    step "1 · rsync of the tree -> $VPS_SSH:$SOURCE_DIR"
    local excludes=() opts=(-rlptD --delete --human-readable --itemize-changes)
    local x
    # No mapfile: deploy.sh runs on the operator's machine, which may have
    # bash 3.2 (macOS).
    while IFS= read -r x; do excludes+=("$x"); done < <(deploy_rsync_excludes "$DISTRO")
    [ "$dry_run" -eq 1 ] && opts+=(--dry-run)
    rsync "${opts[@]}" --rsync-path="sudo rsync" -e "$rsync_rsh" "${excludes[@]}" \
        "$tree"/ "$VPS_SSH:$SOURCE_DIR"/
    if [ "$dry_run" -eq 1 ]; then
        ok "Dry-run: nothing changed on the host. What 03 does in INSTALL_DIR is not visible here."
        return 0
    fi
    # rsync with -rlptD does not preserve the owner: the checkout ends up owned
    # by root, without group or other write, as the lib.sh guard requires.
    remote "sudo chown -R root:root '$SOURCE_DIR' && sudo chmod -R go-w '$SOURCE_DIR'"
    ok "Root checkout up to date."

    step "2 · 03-app-install.sh (INSTALL_DIR, uv sync, server/.env, host files, unit)"
    remote "sudo bash '$setup_remote/03-app-install.sh'"

    step "3 · Unit ExecStart (server-layout.sh align-unit, from this checkout)"
    remote "sudo bash -s -- align-unit --install-dir '$INSTALL_DIR' --app-user '$APP_USER' --unit '/etc/systemd/system/$SERVICE'" \
        < "$DEPLOY_SCRIPT_DIR/server-layout.sh"

    if [ "$do_migrate" -eq 1 ]; then
        step "4 · Explicit migrations (04-migrate.sh)"
        remote "sudo bash '$setup_remote/04-migrate.sh'"
    fi

    if [ "$do_restart" -eq 1 ]; then
        step "5 · Restart of $SERVICE and stability (${stable_seconds} s)"
        remote "sudo systemctl restart '$SERVICE'"
        # With Type=exec the unit reports "active" as soon as the process starts,
        # even if uvicorn dies later and systemd relaunches it (Restart=on-failure).
        # An immediate is-active does not detect that crash loop: the service is
        # watched for stable_seconds and it fails if it stops being active or if
        # NRestarts goes up. POSIX sh: the remote shell may not be bash.
        local checks=$(( (stable_seconds + 4) / 5 )) rc=0
        remote "svc='$SERVICE'; base=\$(systemctl show -p NRestarts --value \"\$svc\"); i=0; while [ \$i -lt $checks ]; do sleep 5; systemctl is-active --quiet \"\$svc\" || exit 2; n=\$(systemctl show -p NRestarts --value \"\$svc\"); [ \"\$n\" = \"\$base\" ] || exit 3; i=\$((i + 1)); done" \
            || rc=$?
        case "$rc" in
            0) ok "$SERVICE active and without restarts for ${stable_seconds} s." ;;
            2) fail_with_journal "$SERVICE stopped being active after the restart." ;;
            3) fail_with_journal "$SERVICE restarted on its own after the restart (crash-loop)." ;;
            *) fail_with_journal "Could not check the state of $SERVICE (ssh rc=$rc)." ;;
        esac
    fi

    if [ "$do_smoke" -eq 1 ]; then
        step "6 · Smoke test (08-smoke-test.sh)"
        # 08 retries the local /healthz (~60 s by default) and exits non-zero if
        # it does not respond or the unit is not active; public HTTPS is only a
        # warning. A failure here is a failed deploy.
        remote "sudo bash '$setup_remote/08-smoke-test.sh'" \
            || fail_with_journal "The smoke test failed: the local /healthz did not respond or the service is not active."
    elif [ "$do_restart" -eq 1 ]; then
        warn "--no-smoke: only checked that $SERVICE stayed active for ${stable_seconds} s; nobody queried /healthz. Verify it by hand."
    fi
    ok "Deploy finished."
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
