#!/usr/bin/env bash
# 03 — Worker configuration (base env in CONFIG_DIR, outside the checkout).
# It is regenerated from setup.env, but PRESERVES ORCH_API_KEY and
# ORCH_ENROLLMENT_TOKEN if they already had a value: that way a rerun does not
# break an already enrolled worker. The operator places the token out-of-band.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "03 · Worker configuration ($CONFIG_DIR/env)"

install -d -m 0750 -o root -g "$WORKER_USER" "$CONFIG_DIR"

# Writes the base .env through the shared helper (same format the fleet's env.i
# files use; see lib.sh::stamp_worker_env). The enrollment token is NOT stamped
# here: in the manual flow the operator places it, and in the fleet flow
# (cloud-init) it lives only in the CONFIG_DIR env.N files that run-fleet.sh
# stamps: nobody consumes the base .env, and the multi-use token must not sit at
# rest in it. stamp_worker_env still preserves a previous token/api_key if the
# operator had already placed them (rerun). The disk gate is propagated too: it
# comes from setup.env through load_env, not from the previous .env, so a rerun
# keeps it as long as setup.env does not change. The isolation block follows the
# resolution in lib.sh::stamp_worker_env: setup.env, EXTRA_ENV, previous .env,
# 'false' if the file already existed and 'true' on a new install.
stamp_worker_env "$CLIENT_ENV" "$ORCH_BASE_URL" "" "$ORCH_WORKER_NAME" "$EXTRA_ENV" \
    "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" \
    "$ORCH_ENFORCE_ISOLATION" "$ISOLATION_REQUIRED_ENV"
chown "root:$WORKER_USER" "$CLIENT_ENV"
chmod 0640 "$CLIENT_ENV"

if grep -q '^ORCH_ENROLLMENT_TOKEN=.\+$' "$CLIENT_ENV" || grep -q '^ORCH_API_KEY=.\+$' "$CLIENT_ENV"; then
    ok "$CLIENT_ENV regenerated (credentials preserved)."
else
    ok "$CLIENT_ENV written. ORCH_ENROLLMENT_TOKEN still has to be placed before 'make up client'."
    ok "(In the cloud-init/fleet flow this notice is expected: run-fleet.sh stamps the token into each env.N in CONFIG_DIR.)"
fi
