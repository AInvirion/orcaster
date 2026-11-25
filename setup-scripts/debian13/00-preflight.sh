#!/usr/bin/env bash
# 00 — Preflight: validates the environment, updates apt, installs base packages and
# creates the system user that will run the orchestrator. Idempotent.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"

# fs.protected_hardlinks=1 (the Debian default) is a precondition for the root
# `chown -R` over trees the service user controls: chown -R does not follow
# symbolic links, but it DOES change the owner of a hardlink's target. With the
# sysctl at 0, that user can hardlink a root-owned file it can read and get root
# to hand it over. Fails closed: if it is not exactly 1 (or cannot be read), the
# script stops before touching anything (hence it runs before load_env and apt).
# `sysctl` is resolved through PATH.
require_protected_hardlinks() {
    local val
    val="$(sysctl -n fs.protected_hardlinks 2>/dev/null)" ||
        die "Could not read the fs.protected_hardlinks sysctl (failed: sysctl -n fs.protected_hardlinks). This installer runs chown -R as root over ${INSTALL_DIR:-INSTALL_DIR}, which ${APP_USER:-the service user} controls, and that is only safe with fs.protected_hardlinks=1. Check it with: sysctl fs.protected_hardlinks"
    [ "$val" = "1" ] ||
        die "fs.protected_hardlinks=$val (1 is required). This installer runs chown -R as root over ${INSTALL_DIR:-INSTALL_DIR}, which ${APP_USER:-the service user} controls: with the sysctl set to anything other than 1, that user can hardlink a root-owned file and make chown -R change its owner. Fix it with: sysctl -w fs.protected_hardlinks=1, persist it under /etc/sysctl.d/ (e.g. echo 'fs.protected_hardlinks = 1' > /etc/sysctl.d/60-protected-links.conf) and retry this same step."
}
require_protected_hardlinks
require_debian13
load_env

step "00 · Preflight and system base"

log "Updating apt indexes and the base system…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

log "Installing base packages…"
# curl/ca-certificates/gnupg → install uv and the Caddy repo.
# git/rsync → fetch and sync the code.
# python3 (3.13 on Trixie) → system interpreter, uv fallback.
# sudo → as_app and psql_su drop from root with `sudo -u`. A minimal Debian 13
# does not ship it, and the README allows direct SSH as root.
apt_install ca-certificates curl gnupg git rsync python3 openssl sudo
require_sudo

# System user without login, with its home in INSTALL_DIR (outside /home, so the
# unit's ProtectHome hardening does not affect it). --system: no password expiry.
if id "$APP_USER" >/dev/null 2>&1; then
    ok "User '$APP_USER' already exists."
else
    log "Creating system user '$APP_USER' (home=$INSTALL_DIR)…"
    useradd --system --create-home --home-dir "$INSTALL_DIR" \
            --shell /usr/sbin/nologin "$APP_USER"
fi

install -d -o "$APP_USER" -g "$APP_USER" "$INSTALL_DIR"

ok "Preflight complete."
