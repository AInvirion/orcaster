#!/usr/bin/env bash
# 05 — Smoke test: checks that the host is ready to start the worker.
# It does NOT start the container: without ORCH_ENROLLMENT_TOKEN the worker
# aborts at boot.
set -euo pipefail
# shellcheck source=lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "05 · Smoke test"

log "Rootless Podman operational…"
as_worker podman info >/dev/null || die "podman info failed as '$WORKER_USER' (rootless not operational)."
ok "Rootless Podman OK."

log "Worker image present…"
as_worker podman image exists "$WORKER_IMAGE" \
    || die "Missing image $WORKER_IMAGE (run 04-build.sh)."
ok "Image present."

log "Configuration artifacts…"
[ -f "$CLIENT_ENV" ]    || die "Missing $CLIENT_ENV (run 03-worker-env.sh)."
[ -s "$CLIENT_PUBKEY" ] || die "Missing $CLIENT_PUBKEY (run 02-server-pubkey.sh)."
# Content, not only presence: a truncated or non-PEM pubkey passes the `-s`
# above and only shows up at runtime, with the worker silently rejecting every
# signed task. This is the last point where the failure is still cheap.
# Without openssl there is no check, so it aborts naming the cause instead of
# taking the pubkey as good; 00-preflight.sh installs it.
command -v openssl >/dev/null 2>&1 \
    || die "'openssl' is missing from PATH and without it I cannot validate $CLIENT_PUBKEY. Run 00-preflight.sh."
openssl pkey -pubin -in "$CLIENT_PUBKEY" -noout >/dev/null 2>&1 \
    || die "$CLIENT_PUBKEY exists but its content is not a valid public key.
    Paste the correct PEM block into SERVER_ED25519_PUBKEY inside setup.env and
    run 02-server-pubkey.sh, which replaces the invalid file."
ok "$CLIENT_ENV present and $CLIENT_PUBKEY validated."

log "Connectivity to the orchestrator (${ORCH_BASE_URL%/}/healthz)…"
if curl -fsS --max-time 15 "${ORCH_BASE_URL%/}/healthz" >/dev/null 2>&1; then
    ok "Orchestrator reachable."
else
    warn "Could not reach ${ORCH_BASE_URL%/}/healthz (DNS/network/server?). Check it before starting."
fi

uid="$(id -u "$WORKER_USER")"
printf '\n%b=== Worker provisioned (token still missing) ===%b\n' "$_C_GREEN" "$_C_RST"
echo "Final steps (as operator):"
echo "  1. Place the enrollment token from the dashboard:"
echo "       \$EDITOR $CLIENT_ENV        # ORCH_ENROLLMENT_TOKEN=<token>"
echo "  2. Start the worker (rootless, as '$WORKER_USER'):"
echo "       cd /   # sudo inherits the cwd: from /root (700) the subcommand does not start"
echo "       sudo -u $WORKER_USER env XDG_RUNTIME_DIR=/run/user/$uid \\"
echo "            make -C $INSTALL_DIR up client$MAKE_CFG"
echo "  3. Enable autostart at boot (after startup):"
echo "       sudo bash $SCRIPT_DIR/06-autostart.sh"
echo "  4. Follow the logs:"
echo "       sudo -u $WORKER_USER env XDG_RUNTIME_DIR=/run/user/$uid \\"
echo "            make -C $INSTALL_DIR logs client$MAKE_CFG"
echo
echo "The worker identity (worker_id/api_key) is persisted in the volume"
echo "orcaster-data after the first registration; it does not re-enroll on later starts."
