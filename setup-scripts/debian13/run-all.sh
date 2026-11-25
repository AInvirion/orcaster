#!/usr/bin/env bash
# run-all — Orchestrates the full server deployment on Debian 13 by running
# scripts 00..08 in order. Idempotent: re-runnable without adverse effects.
#
# Usage:
#   sudo bash run-all.sh                 # everything (preflight → smoke test)
#   sudo bash run-all.sh --skip-hardening # skips 07 (firewall + ssh)
#   sudo bash run-all.sh --skip-caddy     # skips 06 (no public TLS)
#   sudo bash run-all.sh --from 04        # resumes from script 04
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"

SKIP_HARDENING=0
SKIP_CADDY=0
FROM=0
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-hardening) SKIP_HARDENING=1 ;;
        --skip-caddy)     SKIP_CADDY=1 ;;
        --from)           FROM="${2:-0}"; shift ;;
        -h|--help)
            sed -n '2,12p' "$0"; exit 0 ;;
        *) die "Unknown argument: $1 (use --help)" ;;
    esac
    shift
done

# Validate the configuration before touching the system.
load_env
printf "\n${_C_BLUE}=== orcaster deployment · Debian 13 ===${_C_RST}\n"
echo   "    domain:     $DOMAIN"
echo   "    install:    $INSTALL_DIR"
echo   "    db:         $DB_NAME (user $DB_USER)"
echo   "    hardening:  $([ $SKIP_HARDENING -eq 1 ] && echo 'skipped' || echo 'yes')"
echo   "    caddy/TLS:  $([ $SKIP_CADDY -eq 1 ] && echo 'skipped' || echo 'yes')"

# Ordered list: number → script.
SCRIPTS=(
    "00:00-preflight.sh"
    "01:01-postgresql.sh"
    "02:02-python-uv.sh"
    "03:03-app-install.sh"
    "04:04-migrate.sh"
    "05:05-systemd.sh"
    "06:06-caddy.sh"
    "07:07-firewall-ssh.sh"
    "08:08-smoke-test.sh"
)

for entry in "${SCRIPTS[@]}"; do
    num="${entry%%:*}"; script="${entry#*:}"
    # shellcheck disable=SC2086
    [ "$((10#$num))" -lt "$((10#$FROM))" ] && continue
    [ "$num" = "06" ] && [ $SKIP_CADDY -eq 1 ]     && { log "Skipping $script (--skip-caddy)";     continue; }
    [ "$num" = "07" ] && [ $SKIP_HARDENING -eq 1 ] && { log "Skipping $script (--skip-hardening)"; continue; }
    bash "$SCRIPT_DIR/$script"
done

printf "\n${_C_GREEN}=== Deployment complete ===${_C_RST}\n"
echo "Dashboard:  https://$DOMAIN"
echo "API docs:   https://$DOMAIN/docs (requires login in the dashboard)"
echo "Server public key (copy it to each worker as server-ed25519.pub):"
echo "    $SECRETS_DIR/ed25519.pub"
echo "Server logs:  journalctl -u orcaster.service -f"
