#!/usr/bin/env bash
# 08 — Smoke test: checks that the server responds locally and, if the DNS/cert are
# already ready, that the dashboard responds over public HTTPS. Mutates nothing.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env   # does not require root: it only reads and makes requests

step "08 · Smoke test"

fail=0

# --- Local: uvicorn behind the proxy ----------------------------------------
# The server does not accept connections until the FastAPI lifespan finishes; if
# MIGRATE_ON_STARTUP=true, the migrations run there (a heavy backfill can take a
# while). With Type=exec systemd already marks the service active before that, so
# a single curl would give a false negative. We probe with retries.
# --max-time bounds EACH attempt: with UVICORN_WORKERS>1 the parent process binds
# the socket before the workers accept, so without a limit curl would hang for
# the whole migration instead of retrying. Default budget:
# 30 attempts × (up to 5s + 2s) ≈ 210s. A LOCAL probe failure is fatal (sets
# fail=1 → `die` at the end); only the public HTTPS probe below is best-effort.
_probe_healthz() {
    curl -fsS --max-time "${SMOKE_HEALTH_MAX_TIME:-5}" \
        "http://127.0.0.1:${BIND_PORT}/healthz" >/dev/null 2>&1
}
# shellcheck disable=SC2034  # RETRY_MAX/RETRY_DELAY are read by retry() (lib.sh, sourced).
RETRY_MAX="${SMOKE_HEALTH_RETRIES:-30}"
# shellcheck disable=SC2034
RETRY_DELAY="${SMOKE_HEALTH_DELAY:-2}"
log "GET http://127.0.0.1:${BIND_PORT}/healthz (with retries)…"
if retry _probe_healthz; then
    ok "Local server responds on /healthz."
else
    err "Local server does NOT respond after the retries. Look at: journalctl -u orcaster.service -n 50"
    fail=1
fi

# --- systemd ------------------------------------------------------------------
if systemctl is-active --quiet orcaster.service; then
    ok "orcaster.service active."
else
    err "orcaster.service is not active."
    fail=1
fi

# --- Public: HTTPS via Caddy (best-effort; depends on DNS+ACME) --------------
log "GET https://${DOMAIN}/healthz (public) …"
if curl -fsS --max-time 15 "https://${DOMAIN}/healthz" >/dev/null 2>&1; then
    ok "Dashboard reachable over HTTPS: https://${DOMAIN}"
    ok "API docs: https://${DOMAIN}/docs (requires login in the dashboard)"
else
    warn "https://${DOMAIN}/healthz does not respond yet. Normal causes right after"
    warn "installing: DNS not propagated or Caddy still issuing the cert. Retry in"
    warn "a few minutes:  curl -I https://${DOMAIN}/healthz"
fi

echo
if [ "$fail" -eq 0 ]; then
    ok "Smoke test OK. Next step: register the first passkey at /auth (bootstrap)."
else
    die "Smoke test had local failures (see above)."
fi
