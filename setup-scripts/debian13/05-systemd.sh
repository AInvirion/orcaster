#!/usr/bin/env bash
# 05 — systemd: installs the unit that keeps uvicorn alive, enables it at boot and
# starts it. Idempotent.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "05 · systemd service (uvicorn)"

# Directory for downloadable artifacts. It lives outside INSTALL_DIR and
# therefore outside the original ReadWritePaths. It is created here so that
# provisioning leaves it ready before the unit starts.
ARTIFACTS_DIR=/var/lib/orcaster-artifacts
log "Creating artifacts directory $ARTIFACTS_DIR…"
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "$ARTIFACTS_DIR"
ok "$ARTIFACTS_DIR created (owner ${APP_USER}:${APP_USER}, mode 0750)."

UNIT=/etc/systemd/system/orcaster.service
log "Writing $UNIT…"
cat > "$UNIT" <<EOF
[Unit]
Description=Orcaster server (FastAPI/uvicorn)
Documentation=https://github.com/AInvirion/orcaster
After=network-online.target postgresql.service
Wants=network-online.target
Requires=postgresql.service

[Service]
Type=exec
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${SERVER_DIR}
Environment=HOME=${INSTALL_DIR}
Environment=UV_CACHE_DIR=${UV_CACHE}
Environment=PATH=/usr/local/bin:/usr/bin:/bin
# The process always runs in UTC even if the host TZ changes: persisted
# timestamps and DB comparisons must not depend on the environment.
Environment=TZ=UTC
# pydantic-settings reads ${SERVER_ENV} from WorkingDirectory.
ExecStart=${UV_BIN} run uvicorn orcaster_server.main:app \\
    --host 127.0.0.1 --port ${BIND_PORT} \\
    --workers ${UVICORN_WORKERS} --no-access-log
Restart=on-failure
RestartSec=3
TimeoutStartSec=${SERVER_START_TIMEOUT}

# --- Sandboxing ---
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
# Writable trees: the installation (venv, uv cache, secrets) and the directory
# for downloadable artifacts. ProtectSystem=strict leaves the rest of the
# filesystem read-only, so /var/lib/orcaster-artifacts must be declared
# explicitly here for the server to be able to write artifacts.
ReadWritePaths=${INSTALL_DIR} /var/lib/orcaster-artifacts

[Install]
WantedBy=multi-user.target
EOF

log "Reloading systemd and enabling the service…"
systemctl daemon-reload
systemctl enable orcaster.service
systemctl restart orcaster.service

# Brief wait for it to come up, then report the state.
sleep 2
if systemctl is-active --quiet orcaster.service; then
    ok "orcaster.service active."
else
    err "orcaster.service is not active. Last journal lines:"
    journalctl -u orcaster.service -n 30 --no-pager || true
    die "Check the log. Typical causes: DATABASE_URL, Ed25519 key or port in use."
fi
