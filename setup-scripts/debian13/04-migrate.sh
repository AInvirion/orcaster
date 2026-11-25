#!/usr/bin/env bash
# 04 — Migrations: applies the schema with Alembic (an explicit, auditable step).
# The server also migrates on startup (MIGRATE_ON_STARTUP=true), but running it
# here fails early and loudly if the DB or the credentials are wrong.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "04 · Migrations (alembic upgrade head)"

DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:5432/${DB_NAME}"

log "Applying migrations to head…"
# Runs inside server/ (where alembic.ini and .env live). DATABASE_URL is passed
# explicitly in addition to .env in case env.py prefers it from the environment.
as_app env DATABASE_URL="$DATABASE_URL" \
    "$UV_BIN" run --project "$SERVER_DIR" --directory "$SERVER_DIR" \
    orcaster-migrate

ok "Schema at head."
