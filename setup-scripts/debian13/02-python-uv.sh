#!/usr/bin/env bash
# 02 — uv: installs uv system-wide in /usr/local/bin so that both the scripts
# and the systemd unit find it without depending on the user's HOME.
# Idempotent: if uv is already there, it only reports the version.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "02 · uv (Python environment manager)"

if command -v "$UV_BIN" >/dev/null 2>&1; then
    ok "uv already installed: $("$UV_BIN" --version)"
else
    log "Installing uv in /usr/local/bin…"
    # The official installer honors UV_INSTALL_DIR. It does not modify shell profiles.
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
    [ -x "$UV_BIN" ] || die "uv is not at $UV_BIN after the installation."
    ok "uv installed: $("$UV_BIN" --version)"
fi

# uv cache inside INSTALL_DIR (the unit runs with ProtectHome and HOME there).
# APP_USER creates it: INSTALL_DIR is theirs, and a root `install -d` over a
# symbolic link planted there would change the owner of the link's target.
as_app install -d -m 755 "$UV_CACHE"
ok "uv cache: $UV_CACHE"
