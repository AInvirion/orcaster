#!/usr/bin/env bash
# scripts/smoke-test.sh — smoke tests of the Orcaster.
#
# Exercises the paths that justify the orchestrator: fan-out, atomic claim,
# long-running tasks with stdout capture, cooperative cancellation, a real
# sandbox, push of signed commands (Ed25519) and reclaim of an expired lease.
#
# Contract of the unified `/poll` endpoint:
#   - The worker talks ONLY to `POST /v1/workers/{id}/poll` (claim + heartbeat
#     + cancel signals in a single round trip).
#   - Removed endpoints (must return 404):
#       * POST /v1/tasks/claim
#       * POST /v1/workers/{id}/heartbeat
#       * POST /v1/commands/{uuid}/ack
#   - Push commands no longer live in `worker_commands` (table dropped). They
#     are regular tasks with `delivery_mode='push'`, `signed=true`,
#     `target_worker_id=<wid>`. The ACK is `task_executions.status='completed'`.
#
# The smoke tests do not call the /poll endpoint directly: they let the worker
# container run its normal loop and observe the effect in the DB.
#
# Requires:
#   - server up                 (make run server)
#   - dev Postgres up           (make db-up)
#   - 1+ worker(s) online       (make up client)  — T2/T4/T8 need more
#   - jq, curl, podman, python3 in PATH
#
# Commands follow the runner contract:
#   command = argv[0]           (binary available inside python:3.13-slim)
#   payload.args = argv[1:]     (list of strings)
# All tasks are built with `python -c '...'`, `sh -c '...'` or `sleep` so they
# do not depend on heavy binaries (gcc, stress-ng) that do NOT exist in the
# worker base image. The image does ship `curl`, `coreutils` (sha256sum) and
# `git`; real tasks can use them without installing them.
#
# Environment variables:
#   SERVER         orchestrator URL         (default: http://localhost:8000)
#   PG_NAME        dev Postgres container   (default: orcaster-pg-dev)
#   PG_USER        Postgres user            (default: orcaster)
#   PG_DB          Postgres database        (default: orcaster)
#   WORKER_NAME    worker container         (default: orcaster-worker)
#   TESTS          subset (csv) to run      (default: t1..t8, t9=legacy-404)
#
# Usage:
#   ./scripts/smoke-test.sh
#   TESTS=t1,t4 ./scripts/smoke-test.sh
#   SERVER=http://other:8000 ./scripts/smoke-test.sh

set -euo pipefail

SERVER="${SERVER:-http://localhost:8000}"
PG_NAME="${PG_NAME:-orcaster-pg-dev}"
PG_USER="${PG_USER:-orcaster}"
PG_DB="${PG_DB:-orcaster}"
WORKER_NAME="${WORKER_NAME:-orcaster-worker}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; NC=$'\033[0m'

pass=0; fail=0; skip=0
created_task_ids=()

log()   { printf '%s[smoke]%s %s\n' "$DIM" "$NC" "$*"; }
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$NC" "$*"; pass=$((pass+1)); }
bad()   { printf '  %s✗%s %s\n' "$RED"   "$NC" "$*"; fail=$((fail+1)); }
warn()  { printf '  %s!%s %s\n' "$YELLOW" "$NC" "$*"; }
skipt() { printf '  %s~%s %s\n' "$YELLOW" "$NC" "$*"; skip=$((skip+1)); }

require() { command -v "$1" >/dev/null || { echo "missing tool: $1" >&2; exit 1; }; }
require curl; require jq; require podman; require python3

psql_q() {
  podman exec "$PG_NAME" psql -U "$PG_USER" -d "$PG_DB" -t -A -c "$1" 2>/dev/null | tr -d '\r'
}

count_online_workers() { psql_q "SELECT count(*) FROM workers WHERE status='online'"; }

# create_task <command> <args-json-array> <target>
create_task() {
  local cmd="$1" args="$2" target="${3:-1}"
  local body resp id
  body=$(jq -nc --arg c "$cmd" --argjson a "$args" --arg t "$target" \
    '{command:$c, payload:{args:$a}, target:$t}')
  resp=$(curl -fsS -X POST "$SERVER/admin/tasks" \
    -H 'Content-Type: application/json' -d "$body")
  id=$(jq -r .task_id <<<"$resp")
  created_task_ids+=("$id")
  echo "$id"
}

# wait_terminal <task_id> <timeout_s>
wait_terminal() {
  local id="$1" timeout="${2:-60}" t=0 s
  while (( t < timeout )); do
    s=$(psql_q "SELECT status FROM tasks WHERE id=$id")
    case "$s" in completed|cancelled|failed) echo "$s"; return 0 ;; esac
    sleep 1; t=$((t+1))
  done
  echo "TIMEOUT"; return 1
}

exec_summary() {
  local id="$1" out
  out=$(psql_q "SELECT status::text || '=' || count(*)::text
                  FROM task_executions WHERE task_id=$id GROUP BY status ORDER BY 1")
  echo "${out//$'\n'/ }"
}

cancel_task() {
  local id="$1"
  curl -fsS -X POST "$SERVER/admin/tasks/$id/cancel" \
    -H 'Content-Type: application/json' -d '{"reason":"smoke-test"}' >/dev/null
}

# --- preflight ----
log "preflight: server + db + workers"
curl -fsS "$SERVER/healthz" >/dev/null \
  || { bad "server not responding at $SERVER/healthz"; exit 1; }
podman exec "$PG_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null \
  || { bad "Postgres not responding (container '$PG_NAME')"; exit 1; }
n_workers=$(count_online_workers)
[[ -z "$n_workers" ]] && n_workers=0
if (( n_workers < 1 )); then
  bad "no workers online — start one with 'make up client'"
  exit 1
fi
ok "server up · Postgres up · $n_workers worker(s) online"

# ==========================================================================
# T1 — fan-out all-available: distributed latency
# ==========================================================================
t1() {
  log "T1 — fan-out all-available (each worker measures HTTP latency)"
  local code='import urllib.request,time
t=time.monotonic()
urllib.request.urlopen("http://example.com",timeout=10).read()
print(f"{(time.monotonic()-t)*1000:.1f} ms",flush=True)'
  local id; id=$(create_task python "$(jq -nc --arg c "$code" '["-c",$c]')" "all-available")
  log "  task_id=$id"
  # Timeout 120s: covers the worst case of 30s idle poll × 3 staggered workers +
  # variable urlopen latency against example.com.
  local s; s=$(wait_terminal "$id" 120) || { bad "T1 timeout (s=$s) — $(exec_summary "$id")"; return; }
  local total ok_n
  total=$(psql_q "SELECT count(*) FROM task_executions WHERE task_id=$id")
  ok_n=$(psql_q  "SELECT count(*) FROM task_executions WHERE task_id=$id AND status='completed'")
  if [[ "$s" == "completed" && "$ok_n" == "$total" && "$total" -gt 0 ]]; then
    ok "T1 completed: $ok_n/$total executions ok · $(exec_summary "$id")"
  else
    bad "T1 final=$s ($ok_n/$total ok) · $(exec_summary "$id")"
  fi
}

# ==========================================================================
# T2 — fan-out by count: the same task delivered to N workers
# ==========================================================================
t2() {
  log "T2 — fan-out count=3 (open-claim up to 3 executions)"
  local need=3
  if (( $(count_online_workers) < need )); then
    skipt "T2 — needs >=$need workers (there are $(count_online_workers))"
    return
  fi
  local id; id=$(create_task sleep '["2"]' 3)
  # Timeout 90s: covers the worst case of 30s idle poll × 3 staggered workers + execution.
  local s; s=$(wait_terminal "$id" 90) || { bad "T2 timeout"; return; }
  local n; n=$(psql_q "SELECT count(*) FROM task_executions WHERE task_id=$id AND status='completed'")
  if [[ "$s" == "completed" && "$n" == "3" ]]; then
    ok "T2 completed: exactly 3 executions ok"
  else
    bad "T2 final=$s completed=$n · $(exec_summary "$id")"
  fi
}

# ==========================================================================
# T3 — long-running with output streaming
# ==========================================================================
t3() {
  log "T3 — long-running ~30s; checks heartbeats + stdout capture"
  local code='import time
for i in range(15):
    print(f"tick {i}",flush=True)
    time.sleep(2)'
  local id; id=$(create_task python "$(jq -nc --arg c "$code" '["-c",$c]')" 1)
  sleep 6
  local running
  running=$(psql_q "SELECT count(*) FROM task_executions
                     WHERE task_id=$id AND status IN ('claimed','running')")
  [[ "${running:-0}" == "0" ]] && warn "T3 no execution running after 6s"
  local s; s=$(wait_terminal "$id" 60) || { bad "T3 timeout"; return; }
  local stdout_len
  stdout_len=$(psql_q "SELECT coalesce(char_length(stdout),0)
                         FROM task_executions WHERE task_id=$id LIMIT 1")
  if [[ "$s" == "completed" && "${stdout_len:-0}" -ge 60 ]]; then
    ok "T3 completed · stdout captured=${stdout_len}B (>=60)"
  else
    bad "T3 final=$s · stdout=${stdout_len}B · $(exec_summary "$id")"
  fi
}

# ==========================================================================
# T4 — cooperative cancellation of a CPU burn
# ==========================================================================
t4() {
  log "T4 — signed cancellation propagates and kills the process group (target=2)"
  local need=2
  if (( $(count_online_workers) < need )); then
    skipt "T4 — needs >=$need workers"
    return
  fi
  local id; id=$(create_task python '["-c","import time\nwhile True: time.sleep(0.01)"]' 2)
  # Wait up to 45s for at least 1 worker to claim the task (covers the worst
  # case of the server's 30s idle poll).
  local n=0 t=0
  while (( t < 45 )); do
    n=$(psql_q "SELECT count(*) FROM task_executions
                  WHERE task_id=$id AND status IN ('claimed','running')")
    (( n >= 1 )) && break
    sleep 3; t=$((t+3))
  done
  (( n < 1 )) && { bad "T4 no worker claimed the task after 45s"; return; }
  cancel_task "$id"
  local s; s=$(wait_terminal "$id" 30) || { bad "T4 cancel did not propagate (timeout)"; return; }
  local can; can=$(psql_q "SELECT count(*) FROM task_executions
                             WHERE task_id=$id AND status='cancelled'")
  if [[ "$s" == "cancelled" && "${can:-0}" -ge 1 ]]; then
    ok "T4 cancelled · executions cancelled=$can"
  else
    bad "T4 final=$s · $(exec_summary "$id")"
  fi
}

# ==========================================================================
# T5 — sandbox: writing /etc/passwd must fail inside the container
# ==========================================================================
t5() {
  log "T5 — real sandbox (writing to /etc/passwd must fail)"
  local code='import sys
try:
    open("/etc/passwd","a").write("x")
    print("BREACH",flush=True)
    sys.exit(0)
except PermissionError as e:
    print(f"blocked: {e}",flush=True)
    sys.exit(13)'
  local id; id=$(create_task python "$(jq -nc --arg c "$code" '["-c",$c]')" 1)
  local s; s=$(wait_terminal "$id" 30) || { bad "T5 timeout"; return; }
  local ec; ec=$(psql_q "SELECT exit_code FROM task_executions WHERE task_id=$id LIMIT 1")
  if [[ "$s" == "failed" && "${ec:-0}" != "0" ]]; then
    ok "T5 failed · exit_code=$ec (sandbox blocked the write)"
  else
    bad "T5 UNEXPECTED · final=$s exit_code=$ec — the write was NOT blocked"
  fi
}

# ==========================================================================
# T6 — push of an Ed25519-signed command (delivery_mode='push' + signed=true)
# ==========================================================================
# After the endpoint unification: the push command lives as a regular task with
# delivery_mode='push', target_worker_id=<wid>, signed=true. The server signs
# it and returns `task_id`. The ACK is task_executions.status changing to
# 'completed' once the worker picks it up via /poll and runs it.
t6() {
  log "T6 — signed admin push command (run_batch via tasks delivery_mode='push')"
  local wid; wid=$(psql_q "SELECT id FROM workers WHERE status='online' LIMIT 1")
  [[ -z "$wid" ]] && { bad "T6 no worker online"; return; }
  local expires
  expires=$(python3 -c 'from datetime import datetime,timedelta,timezone
print((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())')
  local body http
  body=$(jq -nc --arg e "$expires" \
    '{command:"run_batch",
      payload:{commands:["echo hello-from-batch"]},
      expires_at:$e}')
  http=$(curl -sS -o /tmp/t6.json -w '%{http_code}' \
    -X POST "$SERVER/admin/workers/$wid/command" \
    -H 'Content-Type: application/json' -d "$body")
  if [[ "$http" == "503" ]]; then
    # Feature disabled by config (ED25519_PRIVATE_KEY_PATH not set).
    # The server tells "feature off" from "error" by returning 503 with
    # Retry-After. Not a bug — skip cleanly.
    skipt "T6 — signed commands disabled (server has no Ed25519 key configured): $(cat /tmp/t6.json 2>/dev/null)"
    return
  fi
  if [[ "$http" != "200" ]]; then
    bad "T6 push failed (HTTP $http): $(cat /tmp/t6.json 2>/dev/null)"
    return
  fi
  # The endpoint returns task_id (string) after the cutover. Register it so
  # the final cleanup cancels/deletes it like any other task.
  local tid
  tid=$(jq -r '.task_id // empty' /tmp/t6.json)
  if [[ -z "$tid" ]]; then
    bad "T6 response without task_id: $(cat /tmp/t6.json)"
    return
  fi
  created_task_ids+=("$tid")
  log "  task_id=$tid (delivery_mode=push, signed=true) · waiting for execution..."

  # Check in the DB that the task was created with the right flags.
  local row
  row=$(psql_q "SELECT delivery_mode || '|' || signed::text || '|' ||
                       coalesce(target_worker_id::text,'')
                  FROM tasks WHERE id=$tid")
  if [[ "$row" != "push|t|$wid" ]]; then
    bad "T6 unexpected task DB row: '$row' (expected 'push|t|$wid')"
    return
  fi

  # ACK = task completed. The worker receives it on the next /poll and runs
  # it via dispatch_run_batch. Allow up to 30s.
  local s
  s=$(wait_terminal "$tid" 30) || { bad "T6 timeout waiting for execution"; return; }
  local n_ok
  n_ok=$(psql_q "SELECT count(*) FROM task_executions
                  WHERE task_id=$tid AND status='completed' AND worker_id='$wid'")
  if [[ "$s" == "completed" && "${n_ok:-0}" -ge 1 ]]; then
    ok "T6 push command executed (Ed25519 signature verified by worker)"
  else
    bad "T6 final=$s · executions_ok=$n_ok · $(exec_summary "$tid")"
  fi
}

# ==========================================================================
# T9 — cutover contract: legacy endpoints must return 404
# ==========================================================================
# Ensures the endpoint unification cutover is complete: the 3 removed
# endpoints must answer 404, not 422/405/500.
t9() {
  log "T9 — legacy endpoints return 404 (claim/heartbeat/commands ack)"
  local wid; wid=$(psql_q "SELECT id FROM workers WHERE status='online' LIMIT 1")
  [[ -z "$wid" ]] && wid="00000000-0000-0000-0000-000000000000"

  local c1 c2 c3 fails=0
  c1=$(curl -sS -o /dev/null -w '%{http_code}' \
        -X POST "$SERVER/v1/tasks/claim" \
        -H 'Content-Type: application/json' -d '{}')
  c2=$(curl -sS -o /dev/null -w '%{http_code}' \
        -X POST "$SERVER/v1/workers/$wid/heartbeat" \
        -H 'Content-Type: application/json' -d '{}')
  c3=$(curl -sS -o /dev/null -w '%{http_code}' \
        -X POST "$SERVER/v1/commands/00000000-0000-0000-0000-000000000000/ack" \
        -H 'Content-Type: application/json' -d '{}')

  [[ "$c1" == "404" ]] || { warn "POST /v1/tasks/claim returned $c1 (expected 404)"; fails=$((fails+1)); }
  [[ "$c2" == "404" ]] || { warn "POST /v1/workers/{id}/heartbeat returned $c2 (expected 404)"; fails=$((fails+1)); }
  [[ "$c3" == "404" ]] || { warn "POST /v1/commands/{uuid}/ack returned $c3 (expected 404)"; fails=$((fails+1)); }

  if (( fails == 0 )); then
    ok "T9 cutover OK: claim=$c1 heartbeat=$c2 commands-ack=$c3 (all 404)"
  else
    bad "T9 cutover incomplete: $fails legacy endpoint(s) still respond"
  fi
}

# ==========================================================================
# T7 — claim race: target=1 with all workers polling
# ==========================================================================
t7() {
  log "T7 — claim race (target=1, exactly 1 execution wins)"
  local id; id=$(create_task sh '["-c","echo hello; sleep 1"]' 1)
  local s; s=$(wait_terminal "$id" 30) || { bad "T7 timeout"; return; }
  local n; n=$(psql_q "SELECT count(*) FROM task_executions WHERE task_id=$id")
  if [[ "$s" == "completed" && "$n" == "1" ]]; then
    ok "T7 completed with exactly 1 execution (SKIP LOCKED OK)"
  else
    bad "T7 final=$s executions=$n (must be 1) · $(exec_summary "$id")"
  fi
}

# ==========================================================================
# T8 — reclaim of an expired lease: kill the worker container and wait for 'expired'
# ==========================================================================
t8() {
  log "T8 — reclaim of an expired lease (kills the worker container)"
  if ! podman container exists "$WORKER_NAME"; then
    skipt "T8 — container '$WORKER_NAME' does not exist (set WORKER_NAME)"
    return
  fi
  if (( $(count_online_workers) < 2 )); then
    skipt "T8 — needs >=2 workers (one victim, the others reclaim)"
    return
  fi
  local id; id=$(create_task sleep '["180"]' 1)
  # Idle workers get next_poll_after_seconds=30 from the server; wait up to
  # 75s for some worker to take the task (covers the worst case of an idle poll
  # after several earlier tests without engagement: 30s since the last poll +
  # 30s for the next + margin).
  local victim="" t=0
  while (( t < 75 )); do
    victim=$(psql_q "SELECT worker_id::text FROM task_executions
                      WHERE task_id=$id AND status IN ('claimed','running') LIMIT 1")
    [[ -n "$victim" ]] && break
    sleep 3; t=$((t+3))
  done
  [[ -z "$victim" ]] && { bad "T8 no worker running the task after 75s"; return; }
  log "  victim=$victim · killing container '$WORKER_NAME' ..."
  podman kill "$WORKER_NAME" >/dev/null 2>&1 || true

  log "  waiting for the lease to expire and another worker to reclaim (up to 150s) ..."
  local t=0 expired=0
  while (( t < 150 )); do
    expired=$(psql_q "SELECT count(*) FROM task_executions
                       WHERE task_id=$id AND status='expired'")
    [[ "${expired:-0}" -ge 1 ]] && break
    sleep 5; t=$((t+5))
  done
  if [[ "${expired:-0}" -ge 1 ]]; then
    ok "T8 execution marked expired by reclaim · $(exec_summary "$id")"
  else
    bad "T8 no reclaim in 150s · $(exec_summary "$id")"
  fi
  log "  restarting '$WORKER_NAME' ..."
  podman start "$WORKER_NAME" >/dev/null 2>&1 || true
  cancel_task "$id" 2>/dev/null || true
}

cleanup() {
  log "cleanup: cancel + delete created tasks"
  for id in "${created_task_ids[@]:-}"; do
    [[ -z "$id" ]] && continue
    curl -sS -o /dev/null -X POST "$SERVER/admin/tasks/$id/cancel" \
      -H 'Content-Type: application/json' -d '{"reason":"cleanup"}' || true
    for _ in 1 2 3 4 5 6 7 8; do
      s=$(psql_q "SELECT status FROM tasks WHERE id=$id")
      case "$s" in completed|cancelled|failed) break ;; esac
      sleep 1
    done
    curl -sS -o /dev/null -X DELETE "$SERVER/admin/tasks/$id" || true
  done
}
trap cleanup EXIT

RUN="${TESTS:-t1,t2,t3,t4,t5,t6,t7,t8,t9}"
IFS=',' read -ra TS <<<"$RUN"
for fn in "${TS[@]}"; do
  if declare -f "$fn" >/dev/null; then
    "$fn"
  else
    warn "test '$fn' not defined"
  fi
done

echo
printf '%sresults:%s %s%d ok%s · %s%d fail%s · %d skipped\n' \
  "$DIM" "$NC" "$GREEN" "$pass" "$NC" "$RED" "$fail" "$NC" "$skip"
(( fail == 0 ))
