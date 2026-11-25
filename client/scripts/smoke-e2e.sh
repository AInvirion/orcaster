#!/usr/bin/env bash
# =============================================================================
# client/scripts/smoke-e2e.sh
# =============================================================================
# End-to-end smoke test for the orcaster worker.
#
# Validates the full loop: worker image build, container start, registration,
# task claim, execution, and output verification against a running server.
#
# Usage:
#   cd client && ./scripts/smoke-e2e.sh
#
# Environment variables:
#   SERVER_URL          URL of the orchestrator server (default: http://localhost:8000)
#   DATABASE_URL        Database URL for migrations and verification queries.
#                       If unset, falls back to POSTGRES_DSN.
#   POSTGRES_DSN        Alternative to DATABASE_URL.
#   SKIP_BUILD          If non-empty, skip building the worker Docker image.
#   SKIP_SERVER         If non-empty, assume the server is already running
#                       at SERVER_URL (skip migrations + server startup).
#   WORKER_NETWORK_MODE If set to "host", the worker container uses host
#                       networking (useful in CI where host.docker.internal
#                       may not resolve).
# =============================================================================

set -euo pipefail

SERVER_URL="${SERVER_URL:-http://localhost:8000}"
DATABASE_URL="${DATABASE_URL:-${POSTGRES_DSN:-}}"
SKIP_BUILD="${SKIP_BUILD:-}"
SKIP_SERVER="${SKIP_SERVER:-}"
WORKER_NETWORK_MODE="${WORKER_NETWORK_MODE:-}"
DOCKER="${DOCKER:-docker}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_DIR="$SCRIPT_DIR/.."
SERVER_DIR="$CLIENT_DIR/../server"

PG_CONTAINER_NAME=""
SERVER_PID=""
TASK_ID=""
PY_SCRIPT=""
WORKER_CONTAINER_NAME=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info() { echo "[smoke] $*"; }
error() { echo "[smoke] ERROR: $*" >&2; }

wait_for_url() {
    local url="$1" max_wait="${2:-30}"
    for ((i = 1; i <= max_wait; i++)); do
        if curl -sf --max-time 2 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

cleanup() {
    local exit_code=$?
    info "Cleaning up..."

    # Stop worker container
    if [[ -n "$WORKER_CONTAINER_NAME" ]]; then
        $DOCKER rm -f "$WORKER_CONTAINER_NAME" 2>/dev/null || true
    fi

    # Kill server
    if [[ -n "$SERVER_PID" ]]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi

    # Stop temp PostgreSQL
    if [[ -n "$PG_CONTAINER_NAME" ]]; then
        $DOCKER rm -f "$PG_CONTAINER_NAME" 2>/dev/null || true
    fi

    # Remove temp Python script
    if [[ -n "$PY_SCRIPT" && -f "$PY_SCRIPT" ]]; then
        rm -f "$PY_SCRIPT"
    fi

    exit "$exit_code"
}

trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. PostgreSQL
# ---------------------------------------------------------------------------

if [[ -z "$DATABASE_URL" ]]; then
    info "Starting temporary PostgreSQL container..."
    PG_CONTAINER_NAME="orcaster-smoke-db-$$"
    $DOCKER run --rm -d \
        --name "$PG_CONTAINER_NAME" \
        -e POSTGRES_USER=orcaster \
        -e POSTGRES_PASSWORD=orcaster \
        -e POSTGRES_DB=orcaster \
        -p 5433:5432 \
        postgres:16

    for ((i = 1; i <= 30; i++)); do
        if $DOCKER exec "$PG_CONTAINER_NAME" pg_isready -U orcaster -d orcaster >/dev/null 2>&1; then
            info "PostgreSQL is ready"
            break
        fi
        if [[ $i -eq 30 ]]; then
            error "PostgreSQL did not become ready in time"
            exit 1
        fi
        sleep 1
    done

    DATABASE_URL="postgresql+asyncpg://orcaster:orcaster@localhost:5433/orcaster"
else
    info "Using provided DATABASE_URL"
fi

# ---------------------------------------------------------------------------
# 2. Server migrations + startup (unless SKIP_SERVER)
# ---------------------------------------------------------------------------

if [[ -z "$SKIP_SERVER" ]]; then
    info "Running server migrations..."
    cd "$SERVER_DIR"
    DATABASE_URL="$DATABASE_URL" uv run orcaster-migrate

    info "Starting server in background..."
    cd "$SERVER_DIR"
    DATABASE_URL="$DATABASE_URL" uv run uvicorn orcaster_server.main:app --host 0.0.0.0 --port 8000 &
    SERVER_PID=$!

    if ! wait_for_url "$SERVER_URL/healthz" 30; then
        error "Server did not become ready in time"
        exit 1
    fi
    info "Server is ready"
else
    info "SKIP_SERVER set — assuming server is running at $SERVER_URL"
    if ! curl -sf --max-time 5 "$SERVER_URL/healthz" >/dev/null 2>&1; then
        error "Server at $SERVER_URL is not reachable"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 3. Build worker image (unless SKIP_BUILD)
# ---------------------------------------------------------------------------

if [[ -z "$SKIP_BUILD" ]]; then
    info "Building worker image..."
    cd "$CLIENT_DIR"
    $DOCKER build -t orcaster-worker:smoke -f Dockerfile .
else
    info "SKIP_BUILD set — skipping image build"
fi

# ---------------------------------------------------------------------------
# 4. Start worker container (plain podman/docker run, no compose)
# ---------------------------------------------------------------------------

WORKER_CONTAINER_NAME="orcaster-worker-smoke-$$"

if [[ "$WORKER_NETWORK_MODE" == "host" ]]; then
    WORKER_SERVER_URL="$SERVER_URL"
    NETWORK_ARG="--network=host"
else
    # Podman 4+ exposes the host as `host.containers.internal` by default.
    # Docker Desktop uses `host.docker.internal`. If DOCKER is podman, use
    # containers.internal; otherwise use docker.internal.
    if [[ "${DOCKER##*/}" == "podman"* ]]; then
        WORKER_HOST_NAME="host.containers.internal"
    else
        WORKER_HOST_NAME="host.docker.internal"
    fi
    WORKER_SERVER_URL="${SERVER_URL/localhost/$WORKER_HOST_NAME}"
    WORKER_SERVER_URL="${WORKER_SERVER_URL/127.0.0.1/$WORKER_HOST_NAME}"
    NETWORK_ARG=""
fi

info "Starting worker container ($WORKER_CONTAINER_NAME) → $WORKER_SERVER_URL..."
$DOCKER run -d \
    --name "$WORKER_CONTAINER_NAME" \
    $NETWORK_ARG \
    -e ORCH_BASE_URL="$WORKER_SERVER_URL" \
    -e ORCH_WORKER_NAME="$WORKER_CONTAINER_NAME" \
    -v "$CLIENT_DIR/server-ed25519.pub:/var/lib/orcaster/ed25519.pub:ro" \
    orcaster-worker:smoke

# ---------------------------------------------------------------------------
# 5. SQL helper: psql through the PG container (fast) or uv run python
# (fallback for an external DATABASE_URL). $1 = query; returns stdout without
# headers.
# ---------------------------------------------------------------------------

PY_SCRIPT=$(mktemp)
cat > "$PY_SCRIPT" <<'PYEOF'
import asyncio, sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def main() -> None:
    db_url = sys.argv[1]
    query = sys.argv[2]
    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        result = await conn.execute(text(query))
        for r in result:
            print("|".join("" if v is None else str(v) for v in r))
    await engine.dispose()

asyncio.run(main())
PYEOF

sql_query() {
    local query="$1"
    if [[ -n "$PG_CONTAINER_NAME" ]]; then
        $DOCKER exec "$PG_CONTAINER_NAME" psql -U orcaster -d orcaster -t -A -F '|' -c "$query" 2>/dev/null
    else
        (cd "$SERVER_DIR" && DATABASE_URL="$DATABASE_URL" uv run python "$PY_SCRIPT" "$DATABASE_URL" "$query" 2>/dev/null)
    fi
}

# ---------------------------------------------------------------------------
# 5b. Wait for worker registration
# ---------------------------------------------------------------------------

info "Waiting for worker to register..."

WORKER_ID=""
for ((i = 1; i <= 60; i++)); do
    WORKER_ID=$(sql_query "SELECT id::text FROM workers WHERE status = 'online' LIMIT 1" | head -n1)
    if [[ -n "$WORKER_ID" ]]; then
        info "Worker registered: ${WORKER_ID:0:8}..."
        break
    fi
    if [[ $i -eq 60 ]]; then
        error "Worker did not register within 60 seconds"
        info "--- worker container logs ---"
        $DOCKER logs "$WORKER_CONTAINER_NAME" 2>&1 | tail -30
        exit 1
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 6. Create a task
# ---------------------------------------------------------------------------

info "Creating task..."
TASK_RESPONSE=$(curl -s --max-time 10 -X POST \
    -H "Content-Type: application/json" \
    -d '{"payload":{"requirements":[],"commands":["echo hello from worker"]},"target":"1"}' \
    "$SERVER_URL/admin/tasks")

TASK_ID=$(echo "$TASK_RESPONSE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("task_id",""))' 2>/dev/null || true)

if [[ -z "$TASK_ID" ]]; then
    error "Failed to create task. Response: $TASK_RESPONSE"
    exit 1
fi

info "Task created: $TASK_ID"

# ---------------------------------------------------------------------------
# 7. Wait for task completion
# ---------------------------------------------------------------------------

info "Waiting for task completion..."
TASK_STATUS=""
for ((i = 1; i <= 60; i++)); do
    TASK_STATUS=$(sql_query "SELECT status FROM tasks WHERE id = $TASK_ID" | head -n1)

    if [[ "$TASK_STATUS" == "completed" ]]; then
        info "Task completed"
        break
    elif [[ "$TASK_STATUS" == "failed" ]]; then
        error "Task failed"
        exit 1
    fi

    if [[ $i -eq 60 ]]; then
        error "Task did not complete within 60 seconds (last status: ${TASK_STATUS:-unknown})"
        exit 1
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 8. Verify output
# ---------------------------------------------------------------------------

info "Verifying execution output..."
STDOUT=$(sql_query "SELECT stdout FROM task_executions WHERE task_id = $TASK_ID LIMIT 1")

if echo "$STDOUT" | grep -q "hello from worker"; then
    info "Output verified: 'hello from worker' found in stdout"
else
    error "Output verification failed — expected 'hello from worker'"
    error "stdout: $STDOUT"
    exit 1
fi

info "=== Happy-path SMOKE PASSED ==="

# ---------------------------------------------------------------------------
# 9. Sticky auto-retry: a task with auto_retry.max_attempts=3 whose script
#    exits 75 must be retried 3 times on the same worker and end in a
#    terminal state on the 4th attempt (4 executions in total).
# ---------------------------------------------------------------------------

info "Creating auto-retry task (max_attempts=3, command exits 75)..."
RETRY_RESPONSE=$(curl -s --max-time 10 -X POST \
    -H "Content-Type: application/json" \
    -d '{"payload":{"requirements":[],"commands":["echo throttled; exit 75"]},"target":"1","auto_retry":{"max_attempts":3,"backoff_seconds":0}}' \
    "$SERVER_URL/admin/tasks")

RETRY_TASK_ID=$(echo "$RETRY_RESPONSE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("task_id",""))' 2>/dev/null || true)
if [[ -z "$RETRY_TASK_ID" ]]; then
    error "Failed to create auto-retry task. Response: $RETRY_RESPONSE"
    exit 1
fi
info "Auto-retry task created: $RETRY_TASK_ID"

info "Waiting for auto-retry budget exhaustion (up to 180s)..."
RETRY_DONE=""
for ((i = 1; i <= 180; i++)); do
    ROW=$(sql_query "SELECT status, auto_retry_attempts_used FROM tasks WHERE id = $RETRY_TASK_ID" | head -n1)
    if [[ -n "$ROW" ]]; then
        STATUS=$(echo "$ROW" | cut -d'|' -f1)
        USED=$(echo "$ROW" | cut -d'|' -f2)
        if [[ "$STATUS" == "failed" || "$STATUS" == "completed" ]]; then
            info "Auto-retry task terminal: status=$STATUS attempts_used=$USED"
            RETRY_DONE=1
            break
        fi
        if (( i % 10 == 0 )); then
            info "  [t=${i}s] status=$STATUS attempts_used=$USED"
        fi
    fi
    sleep 1
done

if [[ -z "$RETRY_DONE" ]]; then
    error "Auto-retry task did not reach terminal in 180s"
    exit 1
fi

USED=$(sql_query "SELECT auto_retry_attempts_used FROM tasks WHERE id = $RETRY_TASK_ID" | head -n1)
if [[ "$USED" != "3" ]]; then
    error "Auto-retry: expected attempts_used=3, got $USED"
    exit 1
fi

EXEC_ROW=$(sql_query "SELECT count(*), count(DISTINCT worker_id) FROM task_executions WHERE task_id = $RETRY_TASK_ID" | head -n1)
EXEC_N=$(echo "$EXEC_ROW" | cut -d'|' -f1)
EXEC_WORKERS=$(echo "$EXEC_ROW" | cut -d'|' -f2)
if [[ "$EXEC_N" != "4" ]]; then
    error "Auto-retry: expected 4 executions, got $EXEC_N"
    exit 1
fi
if [[ "$EXEC_WORKERS" != "1" ]]; then
    error "Auto-retry: expected sticky (1 worker), got $EXEC_WORKERS distinct workers"
    exit 1
fi

info "Auto-retry verified: 4 executions on 1 worker, attempts_used=3"

info "=== SMOKE TEST PASSED ==="
