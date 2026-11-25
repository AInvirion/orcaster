#!/usr/bin/env bash
# 09 — Bootstrap of the first owner. Issues an owner invitation and prints the
# /auth/accept/{token} link so you can register your passkey. It is NOT part of
# run-all: it is a post-deploy step, manual and one-time (the link expires in
# ~15 min). Re-runnable: with the Default workspace it is idempotent.
#
# Usage:
#   sudo bash 09-bootstrap-owner.sh                 # adopts the Default workspace
#   sudo bash 09-bootstrap-owner.sh --name "Acme"   # creates a new one (not idempotent)
#   make bootstrap debian13 ARGS="--name Acme"      # equivalent via the Makefile
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "09 · Bootstrap of the first owner"

DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}"

log "Issuing owner invitation (runs as '$APP_USER')…"
# --directory sets the cwd to server/ so that pydantic reads .env and resolves
# orcaster-bootstrap-owner. The operator's args (--name/--label) are passed to the script.
as_app env DATABASE_URL="$DATABASE_URL" \
    "$UV_BIN" run --project "$SERVER_DIR" --directory "$SERVER_DIR" \
    orcaster-bootstrap-owner "$@"

echo
ok "Open the link above in the browser (valid for ≈15 min) and register your passkey."
ok "Then sign in at https://${DOMAIN}/auth/login with that passkey."
