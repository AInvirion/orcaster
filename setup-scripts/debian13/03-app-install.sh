#!/usr/bin/env bash
# 03 — App: syncs the code to INSTALL_DIR, resolves dependencies with uv,
# generates the Ed25519 signing key (if missing) and writes server/.env derived
# from setup.env. Idempotent. The .env is rewritten on every run (everything
# derives from setup.env); the Ed25519 key is NEVER overwritten. The optional
# body limits and the SERVER_ENV_PASSTHROUGH variables are only written if
# setup.env declares them.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "03 · Application install"

# --- TASK_SECRETS_KEYS_PATH guard ---------------------------------------
# Before any side effect: if server/.env already has the keys path and setup.env
# does not declare it, or declares it with a different value, abort without
# touching anything. With the same value in both it continues. See
# check_task_secrets_keys_rerun.
check_task_secrets_keys_rerun

# --- Sync the code to INSTALL_DIR --------------------------------------------
# It is always copied: the root checkout the installer runs from is never
# INSTALL_DIR (the source guard in lib.sh requires that) and never changes owner.
#
# INSTALL_DIR belongs to APP_USER: a root rsync inside it could follow a
# symbolic link planted there and write or delete outside the tree. That is why
# there are two steps: root copies the checkout to its own staging (in /tmp, which
# is root-owned with the sticky bit; mode 750, group APP_USER) and APP_USER syncs
# that staging into INSTALL_DIR, with its own permissions.
#
# Exclusions (the same in both steps): preserve .env, secrets and .venv at the
# destination; exclude development noise. The *.env token files of external
# processes live at the destination and --delete would remove them if they were
# not excluded. '*.env' has no slash: rsync matches it against the basename at any
# depth, so it covers every <name>.env token file (and .env too) without naming
# each tool. No versioned file ends in .env (the templates are *.env.example);
# the bats tests in this directory check it with a real rsync --delete.
#
# setup.env and its backups live only in root's checkout, next to
# setup.env.example: setup-scripts/debian13/setup.env*, except the template, is
# not copied to INSTALL_DIR (a backup with the database password would end up
# owned by APP_USER) nor deleted from there with --delete. rsync applies the first
# matching rule: the template's --include goes before the --exclude.
#
# __pycache__ goes as a perishable rule (-p, man rsync: "perishable"): it stays
# out of the transfer just like with --exclude, but --delete removes it together
# with its directory once that directory no longer exists at the source. With a
# plain --exclude the bytecode protected from --delete every directory of a
# renamed or removed package (server/app/*, server/alembic/*): rsync printed
# "cannot delete non-empty directory" and the old tree survived with only
# __pycache__ inside.
#
# INSTALL_DIR is the service user's HOME (Environment=HOME=... in the unit), so
# tools write their state there: ~/.cache (uv cache), ~/.local and ~/.config. It
# is not in git and --delete would remove it file by file on every deploy. They
# are anchored to the root (/.cache) so they do not match any versioned path
# further down.
RSYNC_EXCLUDES=(
    --exclude '.git'
    --exclude '.venv'
    --filter '-p __pycache__'
    --exclude '/.cache'
    --exclude '/.local'
    --exclude '/.config'
    --exclude '.pytest_cache'
    --exclude '.ruff_cache'
    --exclude 'planning'
    --exclude '*.png'
    --include 'setup-scripts/debian13/setup.env.example'
    --exclude 'setup-scripts/debian13/setup.env*'
    --exclude '*.env'
    --exclude 'server/.env'
    --exclude 'secrets'
    --exclude '.uv-cache'
)
# Static files that only exist on the host (HOST_ONLY_STATIC in setup.env):
# kept out of the rsync in both possible server packages, so --delete does not
# remove them. See host_only_static_excludes in lib.sh.
mapfile -t _HOST_ONLY_EXCLUDES < <(host_only_static_excludes)
RSYNC_EXCLUDES+=("${_HOST_ONLY_EXCLUDES[@]}")
log "Syncing the repo → $INSTALL_DIR (rsync via staging, as $APP_USER)…"
SRC_STAGE="$(mktemp -d /tmp/orcaster-src.XXXXXXXXXX)"
trap 'rm -rf "$SRC_STAGE"' EXIT
rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$REPO_ROOT"/ "$SRC_STAGE"/
chmod -R u+rwX,g+rX,g-w,o-rwx "$SRC_STAGE"
chgrp -R "$APP_USER" "$SRC_STAGE"
# Trees from earlier installations may contain root-owned files; APP_USER has to
# be able to replace them. chown -R does not follow symbolic links (-P is the
# default with -R), but it DOES change the owner of a hardlink's target: it is
# safe only with fs.protected_hardlinks=1, which 00-preflight.sh requires.
chown -R "$APP_USER:$APP_USER" "$INSTALL_DIR"
as_app rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SRC_STAGE"/ "$INSTALL_DIR"/
rm -rf "$SRC_STAGE"
trap - EXIT

# After the server package rename (app -> orcaster_server) the rsync leaves in
# server/app/ only what was excluded: the HOST_ONLY_STATIC files.
# server-layout.sh moves them into the installed package as APP_USER and never
# overwrites a different file. On a tree without the rename it is a no-op. The
# versioned shim server/app/__init__.py (compat for old plugins) also reaches
# server/app/ with the rsync: the one from the source tree is passed so that
# migrate-static recognizes it (byte for byte) and does not treat it as a leftover.
log "Migrating the host files into the installed package (server-layout.sh migrate-static)…"
bash "$SCRIPT_DIR/server-layout.sh" migrate-static \
    --install-dir "$INSTALL_DIR" --app-user "$APP_USER" \
    --shim-source "$REPO_ROOT/server/app/__init__.py"

# --- Server dependencies ------------------------------------------------------
log "Resolving server dependencies with uv (uv sync --frozen)…"
# Without server/uv.lock it aborts: there is no fallback to a sync without a lock.
sync_server_deps
ok "Virtual environment ready at $SERVER_DIR/.venv"

# --- Server plugins -----------------------------------------------------------
# The previous sync is exact and removes every package outside the lock: the
# PLUGIN_PACKAGES plugins are reinstalled here on every run, with the lock's
# versions as a constraint. Fails closed. Then it checks that ORCASTER_PLUGINS
# only names installed plugins (the server would refuse to start).
log "Installing the plugins declared in PLUGIN_PACKAGES…"
install_plugin_packages
check_configured_plugins_installed
ok "Server plugins verified."

# --- Ed25519 signing key -----------------------------------------------------
# Like the rsync: everything written inside INSTALL_DIR is written by APP_USER
# (as_app). INSTALL_DIR is theirs and could have a symbolic link in place of
# secrets/ or of a file; a root chown, chmod or write would follow it outside
# the tree.
as_app install -d -m 700 "$SECRETS_DIR"
if [ -f "$ED25519_PRIV" ]; then
    ok "Ed25519 private key already exists (unchanged): $ED25519_PRIV"
else
    log "Generating the server's Ed25519 signing key…"
    as_app openssl genpkey -algorithm Ed25519 -out "$ED25519_PRIV"
    as_app chmod 600 "$ED25519_PRIV"
    ok "Private key created: $ED25519_PRIV"
fi
# Derive/refresh the public key (workers need it).
as_app openssl pkey -in "$ED25519_PRIV" -pubout -out "$ED25519_PUB"
ok "Public key derived: $ED25519_PUB  (copy it to each worker as server-ed25519.pub)"

# --- Derived server/.env -----------------------------------------------------
log "Writing $SERVER_ENV (derived from setup.env)…"
DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}"
umask 077
# shellcheck disable=SC2016  # $1 is expanded by APP_USER's sh, not by this script
as_app sh -c 'umask 077 && cat > "$1" && chmod 600 "$1"' _ "$SERVER_ENV" <<EOF
# Generated by setup-scripts/debian13/03-app-install.sh — DO NOT edit by hand.
# Change the values in setup.env and run the script again.
DATABASE_URL=${DATABASE_URL}
BIND_HOST=127.0.0.1
BIND_PORT=${BIND_PORT}
ED25519_PRIVATE_KEY_PATH=${ED25519_PRIV}
MIGRATE_ON_STARTUP=true

# WebAuthn / passkeys (dashboard). rp_id = domain without scheme or port.
WEBAUTHN_RP_ID=${DOMAIN}
WEBAUTHN_RP_NAME=${WEBAUTHN_RP_NAME}
WEBAUTHN_ORIGIN=https://${DOMAIN}
SESSION_COOKIE_SECURE=true

# Public base URL used to build the status page share links (config
# PUBLIC_BASE_URL, see server/orcaster_server/config.py). Without it, it falls
# back to the default http://localhost:8000 and the link shown to the operator is
# useless. It is derived from the same DOMAIN as WEBAUTHN_ORIGIN (same public
# domain).
PUBLIC_BASE_URL=https://${DOMAIN}

# Real-time dashboard via SSE + Postgres LISTEN/NOTIFY.
SSE_ENABLED=true

# Artifact quotas: override of the code defaults for this host's disk. The values
# derive from setup.env (default = 200 MiB per file, 20 GiB per workspace; see
# load_env in lib.sh and setup.env.example).
# Note: ARTIFACT_MAX_TOTAL_BYTES (total per execution) is only written here if
# setup.env declares it; otherwise the app default (200 MiB) applies and a
# single 200 MiB file consumes the whole execution quota.
ARTIFACT_MAX_FILE_BYTES=${ARTIFACT_MAX_FILE_BYTES}
ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES=${ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES}$(body_limit_env_block)

# Task secret keys: this file is regenerated on every run: the path is declared
# in setup.env, not here nor by hand in server/.env. If server/.env has it and
# setup.env does not declare it or declares it differently, the run aborts; with
# the same value in both it continues. Empty = feature disabled (the server
# starts with a WARNING and tasks use each worker's environment). With systemd's
# LoadCredentialEncrypted=, declare it in setup.env in single quotes so that
# \$CREDENTIALS_DIRECTORY arrives literally:
#   TASK_SECRETS_KEYS_PATH='\$CREDENTIALS_DIRECTORY/task-secrets-keys'
TASK_SECRETS_KEYS_PATH=${TASK_SECRETS_KEYS_PATH}$(server_env_passthrough_block)
EOF
ok "server/.env written (RP_ID=$DOMAIN, secure cookie, migration on startup)."

# --- systemd unit: ExecStart module -------------------------------------------
# A re-deploy does not rewrite the unit (05-systemd.sh does that). If the server
# package was renamed (app -> orcaster_server), the old ExecStart does not start.
# server-layout.sh align-unit changes only the uvicorn module, with a backup of
# the unit in /var/backups/orcaster/, keeps the drop-ins (orcaster.service.d/)
# and runs daemon-reload. Without a unit (first install, before 05) it does
# nothing.
log "Aligning the unit's ExecStart with the installed package (server-layout.sh align-unit)…"
bash "$SCRIPT_DIR/server-layout.sh" align-unit \
    --install-dir "$INSTALL_DIR" --app-user "$APP_USER"
