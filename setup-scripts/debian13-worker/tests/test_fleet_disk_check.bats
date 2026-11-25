#!/usr/bin/env bats
# fleet-disk-check.sh: parsing/format/verdict/exit code, with ssh mocked (stub
# on PATH, never against real hosts) and with podman/sudo/id/getent mocked
# inside the "remote host" simulated by the ssh stub.
#
# One test runs the real `python -c '...os.statvfs...'` snippet against a real
# directory (without mocking python), to verify against a real filesystem that
# the total/free calculation in bytes works as in production.

load ../../debian13/tests/stub_guard

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../fleet-disk-check.sh"
    T="$(mktemp -d)"
    CALLS="$T/calls.log"; export CALLS
    : > "$CALLS"
    FAKEBIN="$T/bin"; mkdir -p "$FAKEBIN"

    # --- ssh stub: host=$1, remote command=$2 (SSH_OPTS="" in the tests, no
    # flags in between). UNREACHABLE_HOSTS (space-separated) simulates a
    # connection that never gets to run anything remotely (rc=255, like real
    # ssh on "Connection refused").
    cat > "$FAKEBIN/ssh" <<'S'
#!/usr/bin/env bash
echo "ssh $*" >> "$CALLS"
host="$1"; shift
cmd="$1"
case " ${UNREACHABLE_HOSTS:-} " in
    *" $host "*) echo "ssh: connect to host $host port 22: Connection refused" >&2; exit 255 ;;
esac
# Session noise (sshd banner, known_hosts warning): goes to stderr, like real
# ssh, and NEVER to the stdout that carries the report.
[ -n "${SSH_STDERR_NOISE:-}" ] && echo "$SSH_STDERR_NOISE" >&2
exec bash -c "$cmd"
S
    chmod +x "$FAKEBIN/ssh"

    # --- podman stub: simulates 'ps'/'ps -a', 'exec <c> printenv <VAR>' and
    # 'exec <c> python -c ... <path>'. FAKE_CONTAINERS (one line per
    # container, default one worker) controls the listing: each line is
    # "name" (implicit state "running") or "name:state" (to simulate a
    # stopped container, e.g. "orcaster-worker-1:exited").
    # FAKE_CACHE_ROOT/FAKE_MIN_FREE/FAKE_CLAIM_FREE_SET+FAKE_CLAIM_FREE
    # control the env vars the real container would report; FAKE_STAT_OUT, if
    # set, replaces the real python invocation (for deterministic tests that
    # do not depend on the real disk of the test machine); if it is not set,
    # the real 'python' on the PATH (shim to python3 below) is run against the
    # given path, just like in production.
    cat > "$FAKEBIN/podman" <<'P'
#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case "$1" in
    ps)
        shift
        all=0
        for a in "$@"; do [ "$a" = "-a" ] && all=1; done
        while IFS= read -r entry; do
            [ -n "$entry" ] || continue
            name="${entry%%:*}"
            if [ "$name" = "$entry" ]; then state="running"; else state="${entry#*:}"; fi
            if [ "$all" = "1" ]; then
                printf '%s\t%s\n' "$name" "$state"
            elif [ "$state" = "running" ]; then
                printf '%s\n' "$name"
            fi
        # '-' and not ':-': FAKE_CONTAINERS="" means "this host has no
        # container" (a case a test exercises), not "use the default". With
        # ':-' that test still fell into the one-worker default and passed for
        # the wrong reason (and broke on any machine where
        # /var/cache/orcaster actually existed).
        done <<< "${FAKE_CONTAINERS-orcaster-worker-1}"
        ;;
    exec)
        shift; name="$1"; shift
        case "$1" in
            printenv)
                var="$2"
                case "$var" in
                    ORCH_ORCASTER_CACHE_ROOT)
                        [ -n "${FAKE_CACHE_ROOT:-}" ] && echo "$FAKE_CACHE_ROOT" ;;
                    ORCH_ORCASTER_CACHE_MIN_FREE_FRACTION)
                        [ -n "${FAKE_MIN_FREE:-}" ] && echo "$FAKE_MIN_FREE" ;;
                    ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION)
                        [ "${FAKE_CLAIM_FREE_SET:-0}" = "1" ] && echo "${FAKE_CLAIM_FREE:-0}" ;;
                esac
                exit 0
                ;;
            python)
                if [ -n "${FAKE_STAT_OUT:-}" ]; then
                    echo "$FAKE_STAT_OUT"
                    exit 0
                fi
                # No shift: "$@" is still ('python' '-c' '<script>' '<path>'), so
                # exec resolves 'python' via PATH (the shim above).
                exec "$@"
                ;;
        esac
        ;;
esac
P
    chmod +x "$FAKEBIN/podman"

    # --- sudo stub: 'sudo -u USER env K=V... CMD ARGS' -> runs CMD directly
    # (without real privileges; not needed in tests).
    cat > "$FAKEBIN/sudo" <<'U'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
shift 2
exec "$@"
U
    chmod +x "$FAKEBIN/sudo"

    # --- id stub: only 'id -u <user>' (used by the remote probe).
    cat > "$FAKEBIN/id" <<'I'
#!/usr/bin/env bash
[ "${1:-}" = "-u" ] && { echo 1000; exit 0; }
exit 1
I
    chmod +x "$FAKEBIN/id"

    # --- getent stub: only 'getent passwd <user>' (used for $HOME).
    cat > "$FAKEBIN/getent" <<'G'
#!/usr/bin/env bash
[ "${1:-}" = "passwd" ] && { echo "orcaster:x:1000:1000::/home/orcaster:/bin/bash"; exit 0; }
exit 1
G
    chmod +x "$FAKEBIN/getent"

    # --- python -> real python3 shim: the worker image (Dockerfile) provides
    # 'python' via the venv; this test machine may not have it.
    cat > "$FAKEBIN/python" <<'PY'
#!/usr/bin/env bash
exec python3 "$@"
PY
    chmod +x "$FAKEBIN/python"

    export PATH="$FAKEBIN:$PATH"
    # Fails here, before any test, if the PATH reaches the real ssh.
    assert_stubbed "$FAKEBIN" ssh
    export SSH_OPTS=""
    export WORKER_USER="orcaster"
    # runas() in the remote probe sets its own PATH (same pattern as
    # as_worker in lib.sh); without this seam the stubs above would fall
    # outside the PATH seen by 'sudo -u orcaster env PATH=... podman ...'.
    export REMOTE_RUNAS_PATH="$FAKEBIN:/usr/local/bin:/usr/bin:/bin"
}

teardown() { rm -rf "$T"; }

@test "fleet-disk-check: without hosts it aborts with a clear message and does not call ssh" {
    run bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"No hosts"* ]]
    run grep -q "ssh " "$CALLS"
    [ "$status" -ne 0 ]
}

@test "fleet-disk-check: hosts-file ignores comments and empty lines" {
    cat > "$T/hosts.txt" <<'H'
# test fleet
hostA

hostB   # inline
H
    export FAKE_CONTAINERS=""   # no containers => HOSTERR on both hosts
    run bash "$SCRIPT" --hosts-file "$T/hosts.txt"
    [ "$status" -eq 2 ]
    grep -q "ssh hostA " "$CALLS"
    grep -q "ssh hostB " "$CALLS"
    [[ "$output" == *"hostA"* ]]
    [[ "$output" == *"hostB"* ]]
    [[ "$output" == *"NOT MEASURED"* ]]
}

@test "fleet-disk-check: nonexistent hosts-file aborts with a clear message" {
    run bash "$SCRIPT" --hosts-file "$T/does-not-exist.txt"
    [ "$status" -ne 0 ]
    [[ "$output" == *"does-not-exist.txt"* ]]
}

@test "fleet-disk-check: an unreachable host is reported differently and not as 0 free" {
    export UNREACHABLE_HOSTS="bad-host"
    export FAKE_STAT_OUT="1000000000 800000000"   # 0.8 free, ample space
    run bash "$SCRIPT" bad-host good-host
    [ "$status" -eq 2 ]
    [[ "$output" == *"UNREACHABLE VIA SSH"* ]]
    [[ "$output" == *"OK (ample space)"* ]]
    bad_line="$(printf '%s\n' "$output" | grep 'bad-host')"
    [[ "$bad_line" != *"0.000"* ]]
}

@test "fleet-disk-check: a worker below the floor reports FAIL and exits with status 1" {
    export FAKE_STAT_OUT="1000000000 100000000"   # 0.10 free, default floor 0.20
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 1 ]
    [[ "$output" == *"BELOW FLOOR (FAIL)"* ]]
}

@test "fleet-disk-check: a fraction just below the floor is FAIL, it is not rounded to the floor" {
    # Regression: the verdict compared the fraction ALREADY formatted to %.3f
    # against the formatted floor. 199600000/1000000000 = 0.1996 < 0.20 (the
    # real gate is closed there), but "0.200" is not < "0.200" and the script
    # gave exit 0, green light to the rollout, right at the edge where it
    # exists to be exact. The comparison is made on the raw bytes.
    export FAKE_STAT_OUT="1000000000 199600000"
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 1 ]
    [[ "$output" == *"BELOW FLOOR (FAIL)"* ]]
}

@test "fleet-disk-check: a fraction exactly equal to the floor is OK (the gate uses strict <)" {
    # main.py:1629-1631 closes with strict '<': free_fraction == floor does NOT
    # close the gate. The script must reproduce the same inequality.
    export FAKE_STAT_OUT="1000000000 200000000"   # exactly 0.20
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 0 ]
    report="$output"
    [[ "$report" == *"OK (ample space)"* ]]
    run grep -q "BELOW FLOOR" <<< "$report"
    [ "$status" -ne 0 ]
}

@test "fleet-disk-check: ssh noise on stderr is not parsed as a report nor does it degrade the exit" {
    # Regression: the probe captured with '2>&1', so an sshd Banner or the
    # "Warning: Permanently added ..." sneaked in as a report line, fell into
    # "unrecognized line" and produced exit 2 on a healthy fleet. stderr goes
    # apart from the TSV protocol.
    export SSH_STDERR_NOISE="Warning: Permanently added 'solo-host' (ED25519) to the list of known hosts."
    export FAKE_STAT_OUT="1000000000 900000000"
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 0 ]
    report="$output"
    [[ "$report" == *"OK (ample space)"* ]]
    run grep -q "NOT MEASURED" <<< "$report"
    [ "$status" -ne 0 ]
}

@test "fleet-disk-check: an explicit floor of zero disables the gate even if the disk is almost full" {
    # Regression of the resolved_claim_min_free_fraction bug: an EXPLICIT
    # override to 0 (config.py:368-382) turns the gate off ALWAYS, not only
    # when it is greater than 0. With 0.001 free this would FAIL if the script
    # (wrongly) inherited the GC's 0.20 instead of respecting the explicit 0.
    export FAKE_STAT_OUT="1000000000 1000000"     # 0.001 free
    export FAKE_CLAIM_FREE_SET=1 FAKE_CLAIM_FREE=0
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 0 ]
    report="$output"
    [[ "$report" == *"gate disabled"* ]]
    run grep -q "BELOW FLOOR" <<< "$report"
    [ "$status" -ne 0 ]
}

@test "fleet-disk-check: an explicit non-zero floor lower than the default is respected" {
    export FAKE_STAT_OUT="1000000000 100000000"   # 0.10 free
    export FAKE_MIN_FREE="0.20" FAKE_CLAIM_FREE_SET=1 FAKE_CLAIM_FREE="0.05"
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 0 ]
    report="$output"
    [[ "$report" == *"OK (ample space)"* ]]
    run grep -q "BELOW FLOOR" <<< "$report"
    [ "$status" -ne 0 ]
}

@test "fleet-disk-check: a lost measurement is distinguished from unreachable and from 0 free" {
    export FAKE_STAT_OUT="ERR [Errno 2] No such file or directory: '/var/cache/orcaster'"
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 2 ]
    report="$output"
    [[ "$report" == *"NOT MEASURED"* ]]
    [[ "$report" == *"fail-open"* ]]
    # 'run' reassigns $output/$status: each later check uses $report, the copy
    # taken above, not the $output already overwritten by the previous grep.
    run grep -q "UNREACHABLE" <<< "$report"
    [ "$status" -ne 0 ]
    run grep -q "0.000" <<< "$report"
    [ "$status" -ne 0 ]
}

@test "fleet-disk-check: reports one row per worker when there are several on a host" {
    export FAKE_CONTAINERS=$'orcaster-worker-1\norcaster-worker-2'
    export FAKE_STAT_OUT="1000000000 900000000"
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 0 ]
    [ "$(grep -c 'orcaster-worker-1' <<< "$output")" -ge 1 ]
    [ "$(grep -c 'orcaster-worker-2' <<< "$output")" -ge 1 ]
    [ "$(grep -c 'OK (ample space)' <<< "$output")" -eq 2 ]
}

@test "fleet-disk-check: excludes containers that do not match the worker pattern" {
    export FAKE_CONTAINERS=$'orcaster-proxy-1\norcaster-worker-1'
    export FAKE_STAT_OUT="1000000000 900000000"
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 0 ]
    report="$output"
    # 'run' reassigns $output/$status: we save $report before reusing 'run'
    # for the negative grep, otherwise the check below would verify the
    # (empty) output of grep, not the script's real report.
    run grep -q "orcaster-proxy-1" <<< "$report"
    [ "$status" -ne 0 ]
    [[ "$report" == *"orcaster-worker-1"* ]]
}

@test "fleet-disk-check: FAIL takes priority over a simultaneously unreachable host" {
    export UNREACHABLE_HOSTS="bad-host"
    export FAKE_STAT_OUT="1000000000 50000000"    # 0.05 free, below the default floor
    run bash "$SCRIPT" bad-host fail-host
    [ "$status" -eq 1 ]
    [[ "$output" == *"UNREACHABLE VIA SSH"* ]]
    [[ "$output" == *"BELOW FLOOR (FAIL)"* ]]
}

@test "fleet-disk-check: propagates worker-user to the remote command" {
    export FAKE_STAT_OUT="1000000000 900000000"
    run bash "$SCRIPT" --worker-user myworker solo-host
    [ "$status" -eq 0 ]
    grep -q "bash -s -- 'myworker'" "$CALLS"
}

@test "fleet-disk-check: measures a real filesystem without mocking python (local verification)" {
    # Without FAKE_STAT_OUT: the podman stub runs the script's real 'python -c'
    # against a real directory on this machine (the test tmp), the same
    # statvfs that runs inside the container in production.
    unset FAKE_STAT_OUT
    export FAKE_CACHE_ROOT="$T"
    run bash "$SCRIPT" solo-host
    [[ "$output" == *"GiB"* ]]
    run grep -q "NOT MEASURED" <<< "$output"
    [ "$status" -ne 0 ]
    run grep -q "UNREACHABLE" <<< "$output"
    [ "$status" -ne 0 ]
}

@test "fleet-disk-check: a stopped worker on a host with more than one is not lost in the exit" {
    # Finding 1: 'podman ps' (without -a) only lists running containers. A
    # host with two workers, one stopped and one running with ample space,
    # must not exit 0 (no evidence against) just because the stopped one never
    # contributed a row.
    export FAKE_CONTAINERS=$'orcaster-worker-1:exited\norcaster-worker-2'
    export FAKE_STAT_OUT="1000000000 900000000"   # the one that is running has ample space
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 2 ]
    report="$output"
    [[ "$report" == *"orcaster-worker-1"* ]]
    [[ "$report" == *"stopped"* ]]
    [[ "$report" == *"orcaster-worker-2"* ]]
    [[ "$report" == *"OK (ample space)"* ]]
}

@test "fleet-disk-check: worker-user with a single quote does not inject commands on the remote host" {
    # Finding 2: WORKER_USER is interpolated unescaped inside single quotes in
    # the ssh command. A value with an embedded single quote (typo or
    # copy-paste) must not be able to run arbitrary code on the host. The real
    # oracle is the absence of the marker; the id/sudo stubs of this suite are
    # permissive (they do not validate the user name), so with the quoting
    # fixed the probe follows its normal course treating the value as data
    # (odd but inert) instead of breaking the syntax of the remote command. It
    # is also verified that the probe ran end to end (it reached 'podman ps -a'
    # and brought back a real measurement), so that this test does not pass
    # vacuously because of an unrelated early failure.
    export FAKE_STAT_OUT="1000000000 900000000"
    marker="$T/PWNED_MARKER"
    run bash "$SCRIPT" --worker-user "x'; touch $marker; echo '" solo-host
    [ ! -e "$marker" ]
    grep -q "podman ps -a" "$CALLS"
    [[ "$output" == *"GiB"* ]]
}

@test "fleet-disk-check: a corrupt free_b is not reported as a fabricated FAIL" {
    # Finding 3: only total_b is validated as all digits; free_b never is. If
    # free_b arrives corrupt, awk converts it to 0 and the verdict comes out
    # BELOW FLOOR (FAIL) even though it was actually a parse failure.
    export FAKE_STAT_OUT="1000000000 abc123"
    run bash "$SCRIPT" solo-host
    [ "$status" -eq 2 ]
    report="$output"
    [[ "$report" == *"NOT MEASURED"* ]]
    run grep -q "BELOW FLOOR" <<< "$report"
    [ "$status" -ne 0 ]
}
