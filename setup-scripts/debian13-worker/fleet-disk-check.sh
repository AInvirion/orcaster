#!/usr/bin/env bash
# fleet-disk-check: measures over SSH, on each host of the fleet, the free
# space of the cache_root volume of each worker WITH THE SAME CRITERION as the
# worker's disk gate (pre-rollout gate).
#
# The dashboard shows `disk_mb`/`disk_used_mb`, but that pair does NOT
# reproduce the gate criterion near the threshold: `used` excludes the
# filesystem's reserved blocks (5% on ext4 by default), so `used + free !=
# total` and the subtraction disagrees exactly where it matters. This script
# measures exactly what the worker measures:
#
#   - `psutil.disk_usage(cache_root)` (client/src/orcaster_worker/sysinfo.py:112),
#     which on POSIX is `os.statvfs()`: `total = f_blocks * f_frsize`,
#     `free = f_bavail * f_frsize` (NOT `f_bfree`: `f_bavail` is what is
#     available to an unprivileged user, the same field the gate uses).
#   - `free_fraction = free / total` in bytes, in the same measurement
#     (sysinfo.py:120).
#   - The gate closes if `free_fraction < floor`, with
#     `floor = ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION` when that env var
#     is SET (any value, including `0.0`: it is the documented escape hatch
#     that disables the gate), or otherwise
#     `ORCH_ORCASTER_CACHE_MIN_FREE_FRACTION` (default `0.20`)
#     (config.py:230-256 and 368-382; main.py:1629-1631).
#
# READ-ONLY: per host it runs `podman ps -a` / `podman exec printenv` / `podman
# exec python -c '...os.statvfs...'`. It does not delete cache, restart
# containers, or modify any host or any configuration.
#
# Usage:
#   fleet-disk-check.sh [options] [host ...]
#
# See --help for the details of the options, the report format and the exit
# codes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

# --- Options / test seams ------------------------------------------------
# WORKER_USER: system user that runs rootless Podman on each host (same
# default as the rest of the suite, see lib.sh).
WORKER_USER="${WORKER_USER:-orcaster}"
# SSH_OPTS: "-" form (not ":-") on purpose, same as RUN_AS_WORKER in
# run-fleet.sh: an explicit EMPTY value (the one the test sets, so that the ssh
# stub receives only host+command with no flags in between) is respected and
# the default does not override it.
SSH_OPTS="${SSH_OPTS--o BatchMode=yes -o ConnectTimeout=10}"
SSH_BIN="${SSH_BIN:-ssh}"
HOSTS_FILE=""
declare -a HOSTS=()

usage() {
    cat <<'EOF'
Usage: fleet-disk-check.sh [options] [host ...]

Measures, over SSH and against EACH host of the fleet, the free space of the
cache_root volume of each worker, with the same criterion as the worker's disk
gate (free/total in bytes via statvfs). Read-only: it does not modify any
host.

Hosts:
  They are accepted as positional arguments (user@host or host, one or
  several), through --hosts-file, and/or through the FLEET_HOSTS environment
  variable (space-separated). The three sources are combined. There is no
  hardcoded list: if none is provided, the script aborts asking for one.

Options:
  --hosts-file FILE     One host per line (user@host or host). Empty lines
                         and comments ('#') are ignored.
  --worker-user USER    User that runs rootless Podman on each host
                         (default: orcaster).
  --ssh-opts "OPTS"     Extra options for ssh, as a single string
                         (default: "-o BatchMode=yes -o ConnectTimeout=10").
  -h, --help            Show this help.

Output per worker: host, container name, cache_root, total, free, free
fraction, effective floor and a readable verdict.

Exit codes:
  0  The whole measured fleet has ample space (or has the gate disabled on
     purpose, floor=0) and there were no unreachable hosts.
  1  At least one worker is below the gate floor (the real gate is already
     closed, or would close, there). Use it as a rollout gate.
  2  No worker is below the floor, but the state of the whole fleet could not
     be confirmed (host unreachable over SSH, worker stopped, or measurement
     lost on some worker). There is no evidence that the fleet is ready.

Example:
  fleet-disk-check.sh --hosts-file fleet-hosts.txt
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --hosts-file)
            [ $# -ge 2 ] || die "--hosts-file requires an argument."
            HOSTS_FILE="$2"; shift 2 ;;
        --worker-user)
            [ $# -ge 2 ] || die "--worker-user requires an argument."
            WORKER_USER="$2"; shift 2 ;;
        --ssh-opts)
            [ $# -ge 2 ] || die "--ssh-opts requires an argument."
            SSH_OPTS="$2"; shift 2 ;;
        -h|--help)
            usage; exit 0 ;;
        --)
            shift; break ;;
        -*)
            die "Unknown option: $1 (see --help)." ;;
        *)
            HOSTS+=("$1"); shift ;;
    esac
done
for h in "$@"; do HOSTS+=("$h"); done

# Trims leading and trailing whitespace (without depending on xargs/sed).
_trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

if [ -n "$HOSTS_FILE" ]; then
    [ -f "$HOSTS_FILE" ] || die "--hosts-file does not exist: $HOSTS_FILE"
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        line="$(_trim "$line")"
        [ -n "$line" ] && HOSTS+=("$line")
    done < "$HOSTS_FILE"
fi

if [ -n "${FLEET_HOSTS:-}" ]; then
    # shellcheck disable=SC2206 # intentional word-split: space-separated list
    HOSTS+=($FLEET_HOSTS)
fi

if [ "${#HOSTS[@]}" -eq 0 ]; then
    die "No hosts. Pass at least one as an argument, via --hosts-file, or through the FLEET_HOSTS variable.
    Example: $0 --hosts-file fleet-hosts.txt
    (There is no host inventory declared in the repo today — the script does not assume one.)"
fi

# --- Remote script (read-only) ---------------------------------------------
# Sent over ssh's stdin to 'bash -s -- <worker_user>' on the remote host.
# It never writes anything; its only effect is to read podman/printenv/statvfs.
# Emits one line per result, TSV, with a "type" in the first field:
#   HOSTERR<TAB>message
#   WORKERDOWN<TAB>name<TAB>state
#   WORKERERR<TAB>name<TAB>cache_root<TAB>message
#   WORKEROK<TAB>name<TAB>cache_root<TAB>total_bytes<TAB>free_bytes<TAB>floor
remote_probe_script() {
    cat <<'REMOTE'
set -u
worker_user="${1:-orcaster}"
# runas_path: PATH seen by the elevated command inside runas(). Configurable
# via the 2nd arg ONLY so that tests can inject the stubs directory; in
# production it is always the default (same fixed value as as_worker in
# lib.sh).
runas_path="${2:-/usr/local/bin:/usr/bin:/bin}"
emit() { printf '%s\n' "$*"; }

uid="$(id -u "$worker_user" 2>/dev/null)" || {
    emit "HOSTERR	user '$worker_user' does not exist on this host"
    exit 0
}
home="$(getent passwd "$worker_user" 2>/dev/null | cut -d: -f6)"
[ -n "$home" ] || home="/home/$worker_user"

# Same pattern as as_worker in lib.sh: 'cd /' before sudo, because a root SSH
# session lands in /root (mode 700) and rootless Podman dies with "cannot
# chdir" before running if the cwd is not forced.
runas() {
    ( cd / && sudo -u "$worker_user" env \
        HOME="$home" \
        XDG_RUNTIME_DIR="/run/user/$uid" \
        PATH="$runas_path" \
        "$@" )
}

if ! command -v podman >/dev/null 2>&1; then
    emit "HOSTERR	podman is not in this host's PATH"
    exit 0
fi

# '-a': 'podman ps' (without -a) only lists RUNNING containers. If a host has
# more than one worker and one is stopped while another runs with ample space,
# that stopped worker contributed no row and the final exit could be 0 ("no
# evidence against") with part of the fleet never checked. With '-a' the
# stopped ones are discovered too, and each one is emitted as WORKERDOWN
# below (exit 2: incomplete coverage, never silently 0). Note: this covers
# "container exists but is stopped"; a completely removed container (podman
# rm) is still undetectable without an external inventory of expected workers
# (see README).
names="$(runas podman ps -a --format '{{.Names}}	{{.State}}' 2>/dev/null)" || {
    emit "HOSTERR	could not list containers (check sudo -u $worker_user / XDG_RUNTIME_DIR)"
    exit 0
}
[ -n "$names" ] || {
    emit "HOSTERR	no containers (neither running nor stopped)"
    exit 0
}

found=0
while IFS=$'\t' read -r name state; do
    [ -n "$name" ] || continue
    # Workers only (orcaster-worker or orcaster-worker-N); excludes the
    # orcaster-proxy(-N) sidecar and any other container on the host.
    case "$name" in
        orcaster-worker|orcaster-worker-[0-9]*) ;;
        *) continue ;;
    esac
    found=1

    if [ "$state" != "running" ]; then
        emit "WORKERDOWN	$name	${state:-unknown}"
        continue
    fi

    cache_root="$(runas podman exec "$name" printenv ORCH_ORCASTER_CACHE_ROOT 2>/dev/null)" || cache_root=""
    [ -n "$cache_root" ] || cache_root="/var/cache/orcaster"

    min_free="$(runas podman exec "$name" printenv ORCH_ORCASTER_CACHE_MIN_FREE_FRACTION 2>/dev/null)" || min_free=""
    [ -n "$min_free" ] || min_free="0.20"

    # resolved_claim_min_free_fraction (config.py:368-382): an EXPLICIT
    # override always wins, including "0" (the documented escape hatch that
    # disables the gate) — not only when it is > 0. Only the ABSENCE of the env
    # var (unset) inherits min_free. Do not confuse "set to 0" with "absent".
    claim_free="$(runas podman exec "$name" printenv ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION 2>/dev/null)" || claim_free=""
    if [ -n "$claim_free" ]; then
        floor="$claim_free"
    else
        floor="$min_free"
    fi

    # Same calculation as sysinfo.py:112-120 (psutil.disk_usage == statvfs on
    # POSIX): total = f_blocks*f_frsize, free = f_bavail*f_frsize (NOT
    # f_bfree). It runs INSIDE the worker container, on the effective
    # cache_root of THAT worker (same path, same mount namespace).
    stat_out="$(runas podman exec "$name" python -c '
import os, sys
try:
    st = os.statvfs(sys.argv[1])
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    print(f"{total} {free}")
except OSError as e:
    print("ERR " + str(e))
' "$cache_root" 2>/dev/null)" || stat_out=""

    if [ -z "$stat_out" ]; then
        emit "WORKERERR	$name	$cache_root	no output from 'podman exec ... python' (container without python in PATH, or exec failed?)"
    elif [ "${stat_out#ERR }" != "$stat_out" ]; then
        emit "WORKERERR	$name	$cache_root	${stat_out#ERR }"
    else
        total_b="${stat_out%% *}"
        free_b="${stat_out##* }"
        # Both total_b AND free_b are validated equally: validating only
        # total_b let a corrupt free_b through unchecked. awk converts a
        # non-numeric value to 0 in _to_gib/_fraction, so a corrupt free_b with
        # a valid total_b produced fraction 0.000 and a fabricated BELOW FLOOR
        # (FAIL) verdict, indistinguishable from a real measurement. Either
        # corrupt field falls into WORKERERR (lost measurement, fail-open),
        # never into FAIL.
        case "$total_b" in
            ''|*[!0-9]*) emit "WORKERERR	$name	$cache_root	unexpected statvfs output: $stat_out" ;;
            *)
                case "$free_b" in
                    ''|*[!0-9]*) emit "WORKERERR	$name	$cache_root	unexpected statvfs output: $stat_out" ;;
                    *) printf 'WORKEROK\t%s\t%s\t%s\t%s\t%s\n' "$name" "$cache_root" "$total_b" "$free_b" "$floor" ;;
                esac
                ;;
        esac
    fi
done <<< "$names"

[ "$found" -eq 1 ] || emit "HOSTERR	containers present but none matches orcaster-worker(-N)"
# The exit code of ssh must reflect ONLY connectivity/auth, never the content
# of the report: that is always encoded in the emitted lines.
exit 0
REMOTE
}

# --- SSH (the only impure part; stubbed in tests with SSH_BIN) -------------
# REMOTE_RUNAS_PATH: PATH that runas() uses INSIDE the remote host (test seam,
# so that tests can point to their podman/python stubs; in production it is
# never set and the remote probe uses its own fixed default).
REMOTE_RUNAS_PATH="${REMOTE_RUNAS_PATH:-/usr/local/bin:/usr/bin:/bin}"
PROBE_OUT=""
PROBE_ERR=""
PROBE_RC=0
ssh_probe_host() {
    local host="$1"
    local remote_cmd errfile
    # env_quote (lib.sh): robust quoting ('\'' escaping inner quotes), NOT
    # direct interpolation inside single quotes. WORKER_USER/REMOTE_RUNAS_PATH
    # travel as a single argument of 'bash -s --' on the remote host; a value
    # with an embedded single quote (typo or copy-paste) used to break the
    # quoting and inject arbitrary commands on that host — reproduced with
    # --worker-user "x'; touch /tmp/PWNED_MARKER; echo '". env_quote() already
    # solves exactly this problem for setup.env (lib.sh:359); it is the same
    # POSIX quoting, reused here.
    remote_cmd="bash -s -- $(env_quote "$WORKER_USER") $(env_quote "$REMOTE_RUNAS_PATH")"
    PROBE_RC=0
    PROBE_ERR=""
    # stderr SEPARATE from the report (not '2>&1'): ssh's stdout is the TSV
    # protocol that gets parsed, and any session noise (an sshd `Banner`,
    # "Warning: Permanently added ... to the list of known hosts", "Shared
    # connection closed") used to sneak in as a report line, fall into the
    # "unrecognized line" branch and downgrade a perfectly healthy fleet to
    # exit 2. stderr is kept in PROBE_ERR for the detail of the hosts that did
    # fail.
    errfile="$(mktemp)"
    # "$? after ||" (not inside an 'if !') so that the captured status is
    # unambiguously that of the command substitution in EVERY bash version:
    # with '! CMD; then $?' the value of $? inside the then depends on how
    # that bash version propagates the status through the negation.
    # shellcheck disable=SC2086 # SSH_OPTS: intentional word-split (multiple flags)
    PROBE_OUT="$(remote_probe_script | "$SSH_BIN" $SSH_OPTS "$host" "$remote_cmd" 2>"$errfile")" || PROBE_RC=$?
    PROBE_ERR="$(<"$errfile")"
    rm -f "$errfile"
}

# --- Calculation / formatting (pure, testable without ssh) -----------------
_to_gib() { awk -v b="$1" 'BEGIN{printf "%.2f", b/1073741824}'; }
# For DISPLAY only. The verdict NEVER compares this string: rounding the
# fraction before comparing it fabricates verdicts right at the edge, which is
# where this script exists to be exact (see _below_floor).
_fraction() {
    awk -v f="$1" -v t="$2" 'BEGIN{ if ((t+0)==0) {print "n/a"} else {printf "%.4f", f/t} }'
}
_is_zero() { awk -v x="$1" 'BEGIN{exit !((x+0)==0)}'; }
# _below_floor FREE_BYTES TOTAL_BYTES FLOOR — compares the raw BYTES and the
# raw floor, in doubles, exactly like the worker (`du.free / du.total <
# floor`, main.py:1629-1631). Comparing values already formatted to %.3f gave
# a FALSE "OK (ample space)" at the edge: free/total = 0.1996 with floor 0.20
# rounds to "0.200", is not < "0.200", and the script gave the green light to
# the rollout while the real gate was already closed on that worker.
_below_floor() {
    awk -v f="$1" -v t="$2" -v fl="$3" 'BEGIN{exit !((t+0)>0 && (f/t)<(fl+0))}'
}

declare -a ROWS=()
ANY_FAIL=0
ANY_UNKNOWN=0

# add_row HOST WORKER CACHE_ROOT TOTAL_GIB FREE_GIB FRACTION FLOOR VERDICT
add_row() {
    ROWS+=("$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' \
        "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8")")
}

process_host() {
    local host="$1"
    ssh_probe_host "$host"

    local detail
    # The diagnosis comes from stderr (ssh's actual message); stdout is only
    # used as a fallback when stderr came back empty.
    detail="$(printf '%s' "${PROBE_ERR:-$PROBE_OUT}" | tr '\n' ' ' | cut -c1-160)"

    if [ "$PROBE_RC" -ne 0 ]; then
        ANY_UNKNOWN=1
        add_row "$host" "-" "-" "-" "-" "-" "-" "HOST UNREACHABLE VIA SSH (rc=$PROBE_RC): ${detail:-no output}"
        return 0
    fi
    if [ -z "$PROBE_OUT" ]; then
        ANY_UNKNOWN=1
        add_row "$host" "-" "-" "-" "-" "-" "-" \
            "NO RESPONSE FROM HOST (empty output; check the remote script)${detail:+: $detail}"
        return 0
    fi

    while IFS=$'\t' read -r kind f2 f3 f4 f5 f6; do
        [ -n "$kind" ] || continue
        case "$kind" in
            HOSTERR)
                ANY_UNKNOWN=1
                add_row "$host" "-" "-" "-" "-" "-" "-" "NOT MEASURED: $f2"
                ;;
            WORKERDOWN)
                # f2=name f3=state
                ANY_UNKNOWN=1
                add_row "$host" "$f2" "-" "-" "-" "-" "-" \
                    "NOT MEASURED (container stopped, state=$f3 — this worker could not be confirmed)"
                ;;
            WORKERERR)
                # f2=name f3=cache_root f4=message
                ANY_UNKNOWN=1
                add_row "$host" "$f2" "$f3" "-" "-" "-" "-" \
                    "NOT MEASURED (the real gate does NOT close without a measurement — fail-open — but this check cannot confirm the host either): $f4"
                ;;
            WORKEROK)
                # f2=name f3=cache_root f4=total_bytes f5=free_bytes f6=floor
                local total_gib free_gib frac floor verdict
                total_gib="$(_to_gib "$f4")"
                free_gib="$(_to_gib "$f5")"
                frac="$(_fraction "$f5" "$f4")"
                floor="$(awk -v x="$f6" 'BEGIN{printf "%.4f", x+0}')"
                if _is_zero "$f6"; then
                    # The effective floor can be 0 because of an explicit
                    # override of CLAIM_MIN_FREE_FRACTION or because
                    # MIN_FREE_FRACTION (inherited when there is no override)
                    # is 0. The probe does not distinguish which of the two
                    # put it there, so the verdict does not attribute the
                    # cause to one variable.
                    verdict="OK (gate disabled: effective floor=0)"
                elif [ "$frac" = "n/a" ]; then
                    ANY_UNKNOWN=1
                    verdict="NOT MEASURED (total reported as 0)"
                elif _below_floor "$f5" "$f4" "$f6"; then
                    ANY_FAIL=1
                    verdict="BELOW FLOOR (FAIL) — the real gate is closed or would close here"
                else
                    verdict="OK (ample space)"
                fi
                add_row "$host" "$f2" "$f3" "${total_gib} GiB" "${free_gib} GiB" "$frac" "$floor" "$verdict"
                ;;
            *)
                ANY_UNKNOWN=1
                add_row "$host" "-" "-" "-" "-" "-" "-" "NOT MEASURED (unrecognized line from the remote probe: $kind)"
                ;;
        esac
    done <<< "$PROBE_OUT"
}

render_report() {
    printf '%-24s  %-22s  %-28s  %10s  %10s  %8s  %8s  %s\n' \
        "HOST" "WORKER" "CACHE_ROOT" "TOTAL" "FREE" "FRACTION" "FLOOR" "VERDICT"
    local row
    for row in "${ROWS[@]}"; do
        IFS=$'\t' read -r h w c t f fr fl v <<< "$row"
        printf '%-24s  %-22s  %-28s  %10s  %10s  %8s  %8s  %s\n' \
            "$h" "$w" "$c" "$t" "$f" "$fr" "$fl" "$v"
    done
}

main() {
    local host
    for host in "${HOSTS[@]}"; do
        process_host "$host"
    done

    render_report

    echo
    if [ "$ANY_FAIL" -eq 1 ]; then
        err "At least one worker is below the disk gate floor. The fleet is NOT ready for the rollout."
        exit 1
    elif [ "$ANY_UNKNOWN" -eq 1 ]; then
        warn "Could not confirm the full state of the fleet (unreachable host or lost measurement). There is no evidence that it is ready."
        exit 2
    else
        ok "The whole measured fleet has ample space (or has the gate disabled on purpose). No evidence against the rollout."
        exit 0
    fi
}

main
