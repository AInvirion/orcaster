#!/usr/bin/env bats
# Checks the egress network topology: the internal network `orcaster-egress`
# (--internal) for the worker, the `orcaster-egress-out` network with real
# outbound access for the proxy sidecar, and the worker pointing its
# HTTP(S)_PROXY at the sidecar. It starts no real container: most tests only
# inspect the command that `make -n` would print (same pattern as
# test_up_client_flags); the `real:` ones run the whole recipe, but against a
# podman_stub() that never touches the real podman.
#
# Egress isolation is the authoritative DEFAULT (EGRESS_ISOLATION=1): with the
# default `up-client` adds the whole topology; the EGRESS_ISOLATION=0 opt-out
# disables it completely (for dev/CI without the egress infrastructure).
#
# Note: a `[[ ... ]]` that is not the last statement of a @test does NOT fail
# the test in bats (known bash errexit bug). That is why every assert runs
# through a function, whose `return 1` DOES gate.
assert_contains() {
    [[ "$1" == *"$2"* ]] || { echo "missing: $2"; echo "in: $1"; return 1; }
}

assert_not_contains() {
    [[ "$1" != *"$2"* ]] || { echo "unexpected: $2"; echo "in: $1"; return 1; }
}

# The WORKER's `podman run` block is always the LAST one printed by
# `make -n up-client` (the proxy sidecar's, when it appears as a prerequisite
# of up-proxy, is printed earlier). We extract from the last occurrence of
# "podman run -d" onwards so as not to confuse the proxy's
# `--network orcaster-egress-out` with the worker's.
worker_run_block() {
    printf '%s\n' "$1" | awk '/podman run -d/{start=NR} {line[NR]=$0} END{for(i=start;i<=NR;i++) print line[i]}'
}

setup() {
    cd "$BATS_TEST_DIRNAME/../../.."
    TMPENV="$(mktemp -d)"
}

teardown() {
    if [ -n "${TMPENV:-}" ]; then rm -rf "$TMPENV"; fi
}

# podman stub to test the REAL outcome of `rm -f`, without starting any
# container (the rest of the suite runs under `make -n`, which executes
# nothing; these specific tests run the whole recipe, but against this fake
# binary instead of the real podman). `ps` always returns the same fixed name;
# `rm` exits with the requested code (0 = deleted, non-zero = refused);
# everything else (purge-client's `volume` calls) exits with an error so those
# loops only say "does not exist" and do not interfere with the assertion on
# the container.
podman_stub() {
    rm_exit="${1:-0}"
    cat > "$TMPENV/podman" <<EOF
#!/bin/sh
case "\$1" in
    ps) echo "orcaster-worker" ;;
    rm) exit $rm_exit ;;
    *) exit 1 ;;
esac
EOF
    chmod +x "$TMPENV/podman"
}

# Stub of a fleet on the host: `ps` returns the unsuffixed pair, two suffixed
# workers with their proxies and a foreign container; each `rm` exits 0 and is
# recorded in $TMPENV/rm.log, to test which names the real recipe deletes.
podman_fleet_stub() {
    cat > "$TMPENV/podman" <<EOF
#!/bin/sh
case "\$1" in
    ps) printf 'orcaster-worker\norcaster-worker-1\norcaster-worker-2\norcaster-proxy\norcaster-proxy-1\norcaster-proxy-2\nother-app\n' ;;
    rm) shift; echo "\$*" >> "$TMPENV/rm.log"; exit 0 ;;
    *) exit 1 ;;
esac
EOF
    chmod +x "$TMPENV/podman"
    : > "$TMPENV/rm.log"
}

@test "up-client EGRESS_ISOLATION=0 (opt-out) adds no topology flag" {
    run make -n up-client EGRESS_ISOLATION=0 WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null ORCH_HOST=orch.example
    [ "$status" -eq 0 ]
    assert_not_contains "$output" "orcaster-proxy"
    assert_not_contains "$output" "egress_proxy"
    assert_not_contains "$output" "--network orcaster-egress"
    assert_not_contains "$output" "--dns=none"
    assert_not_contains "$output" "HTTPS_PROXY"
    assert_not_contains "$output" "HTTP_PROXY"
    assert_not_contains "$output" "orcaster-egress-vol"
    assert_not_contains "$output" "aardvark"
    # The hardening is independent and must remain present.
    assert_contains "$output" "--cap-drop=ALL"
    assert_contains "$output" "--read-only"
}

@test "up-client (default, no flag) enables the egress topology" {
    run make -n up-client WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null ORCH_HOST=orch.example
    [ "$status" -eq 0 ]
    worker_cmd="$(worker_run_block "$output")"
    assert_contains "$worker_cmd" "--network orcaster-egress "
    assert_contains "$worker_cmd" "HTTPS_PROXY=http://orcaster-proxy:3128"
    assert_contains "$output" "orcaster-proxy"
    assert_contains "$output" "egress_proxy"
    assert_contains "$output" "aardvark"
}

@test "up-client EGRESS_ISOLATION=1 connects the worker to the internal network with proxy env and an rw,U egress volume" {
    run make -n up-client EGRESS_ISOLATION=1 WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null ORCH_HOST=orch.example
    [ "$status" -eq 0 ]
    worker_cmd="$(worker_run_block "$output")"
    assert_contains "$worker_cmd" "--network orcaster-egress "
    assert_not_contains "$worker_cmd" "orcaster-egress-out"
    # --dns=none prevented resolving the sidecar (aardvark is not published to
    # the worker); the --internal network already guarantees default-deny.
    assert_not_contains "$worker_cmd" "--dns=none"
    assert_contains "$worker_cmd" "HTTPS_PROXY=http://orcaster-proxy:3128"
    assert_contains "$worker_cmd" "HTTP_PROXY=http://orcaster-proxy:3128"
    assert_contains "$worker_cmd" "NO_PROXY=localhost,127.0.0.1"
    # `:U` chowns the volume to the worker uid: without it the worker cannot
    # write allow.txt and egress_allowlist has no effect (silently).
    assert_contains "$worker_cmd" "orcaster-egress-vol:/var/lib/orcaster-egress:rw,U"
}

@test "up-client EGRESS_ISOLATION=1 depends on up-proxy (start order)" {
    run make -n up-client EGRESS_ISOLATION=1 WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null ORCH_HOST=orch.example
    [ "$status" -eq 0 ]
    assert_contains "$output" "orcaster-proxy"
    assert_contains "$output" "egress_proxy"
    # Through up-proxy it also pulls in the aardvark-dns check (precondition).
    assert_contains "$output" "aardvark"
}

# Precondition: without aardvark-dns the worker does not resolve the sidecar in
# the --internal network; with < 1.11.0 the DNS of --internal networks is
# forwarded upstream. `make -n` only prints the recipe (it does not run it), so
# the assertion is on the text of the check, not on its result (the CI/dev
# machine may not have aardvark accessible).
@test "up-proxy includes the aardvark-dns check via podman info with minimum 1.11.0" {
    run make -n up-proxy WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null ORCH_HOST=orch.example
    [ "$status" -eq 0 ]
    assert_contains "$output" "{{.Host.NetworkBackendInfo.DNS.Version}}"
    assert_contains "$output" "aardvark-dns not detected"
    assert_contains "$output" "1.11.0"
    assert_contains "$output" "sort -V"
}

@test "up-client EGRESS_ISOLATION=1 N=2 suffixes the proxy and the egress volume per worker" {
    run make -n up-client EGRESS_ISOLATION=1 N=2 WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null ORCH_HOST=orch.example
    [ "$status" -eq 0 ]
    worker_cmd="$(worker_run_block "$output")"
    assert_contains "$output" "orcaster-proxy-2"
    assert_contains "$output" "orcaster-egress-vol-2"
    assert_contains "$worker_cmd" "HTTPS_PROXY=http://orcaster-proxy-2:3128"
    assert_contains "$worker_cmd" "orcaster-egress-vol-2:/var/lib/orcaster-egress:rw,U"
}

@test "up-proxy connects to the internal and outbound networks, hardened, serves egress_proxy" {
    run make -n up-proxy WORKER_ENV=/dev/null WORKER_PUBKEY=/dev/null ORCH_HOST=orch.example
    [ "$status" -eq 0 ]
    assert_contains "$output" "--network orcaster-egress "
    assert_contains "$output" "--network orcaster-egress-out"
    assert_contains "$output" "--cap-drop=ALL"
    assert_contains "$output" "--security-opt=no-new-privileges"
    assert_contains "$output" "--read-only"
    assert_contains "$output" "orcaster-egress-vol:/var/lib/orcaster-egress:ro"
    assert_contains "$output" "ORCASTER_PROXY_BASE_ALLOW=\"orch.example,pypi.org,files.pythonhosted.org\""
    assert_contains "$output" "egress_proxy"
}

@test "egress-net creates networks and volume idempotently" {
    run make -n egress-net
    [ "$status" -eq 0 ]
    assert_contains "$output" "network exists orcaster-egress "
    assert_contains "$output" "network create --internal orcaster-egress"
    assert_contains "$output" "network create orcaster-egress-out"
    assert_contains "$output" "volume create orcaster-egress-vol"
}

# --- The sidecar goes away with its worker -----------------------------------
# `down-client` only deleted containers matching the worker's name, so under
# EGRESS_ISOLATION=1 the $(PROXY_NAME) sidecar survived the down: it kept
# occupying the egress networks and, on a tarball-deployed host, its code bind
# mount pointed at the inode of the previous release after the swap.
# run-fleet.sh dismantles surplus workers with `make down client N=n`, so that
# path left an orphaned proxy for every pruned worker.

@test "down-client deletes the egress sidecar as well as the worker" {
    run make -n down-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "^(orcaster-worker|orcaster-proxy)$"
    # The same idiom already used for the worker: list by name, report what was
    # removed, do not fail when there is none.
    assert_contains "$output" "ps -a --format '{{.Names}}'"
    assert_contains "$output" "removed: "
    assert_contains "$output" "(none found)"
}

# --- down-client reports the outcome of each container -----------------------
# The recipe had an `xargs -r podman rm -f >/dev/null 2>&1 || true` that
# discarded podman's stderr, with the `|| true` swallowing the exit code: a
# container that podman refused to delete (locked storage, broken state) was
# reported anyway as "removed" by the unconditional `echo ... | sed 's/^/
# removed: /'` that ran next. Now each name is deleted and reported separately.
# The first test below checks the recipe text under `make -n` (like the rest of
# the suite); the second and third run the real recipe against the podman stub,
# to test the runtime behaviour and not just the text.

@test "down-client reports the outcome of each container instead of swallowing it" {
    run make -n down-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "for n in \$names"
    assert_contains "$output" "Could not remove container"
    # The old idiom that swallowed podman's exit code must not reappear.
    assert_not_contains "$output" "xargs -r"
}

@test "down-client real: a container that podman refuses to delete is not reported as removed" {
    podman_stub 1
    run make down-client PODMAN="$TMPENV/podman"
    [ "$status" -eq 0 ]
    assert_contains "$output" "Could not remove container orcaster-worker"
    assert_not_contains "$output" "removed: orcaster-worker"
}

@test "down-client real: a container that podman does delete is reported as removed" {
    podman_stub 0
    run make down-client PODMAN="$TMPENV/podman"
    [ "$status" -eq 0 ]
    assert_contains "$output" "removed: orcaster-worker"
    assert_not_contains "$output" "Could not remove container"
}

@test "down-client N=2 anchors the worker and its sidecar exactly, without the fleet suffix" {
    # With N set, the operator asks for ONE worker: the pattern is anchored
    # exactly like purge-client, without the `(-[0-9]+)?` alternative that only
    # serves the sweep of the whole fleet (ALL=1) and that would also
    # over-match a hypothetical orcaster-worker-2-3.
    run make -n down-client N=2
    [ "$status" -eq 0 ]
    assert_contains "$output" "^(orcaster-worker-2|orcaster-proxy-2)$"
    assert_not_contains "$output" "(-[0-9]+)?"
    # The echo does not announce the suffixes the pattern no longer sweeps either.
    assert_not_contains "$output" "orcaster-worker-2-*"
    assert_not_contains "$output" "orcaster-proxy-2-*"
}

# --- Scope of down-client --------------------------------------------------
# Without N, `down client` swept orcaster-worker and every orcaster-worker-<n>
# (with their proxies), so the single-worker recipe `make down client &&
# make up client` brought the whole fleet down and `up client` started only
# one. Now the default is anchored to the unsuffixed pair, and the sweep is
# requested with ALL=1.

@test "down-client without N is anchored to the unsuffixed pair, without the fleet sweep" {
    run make -n down-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "^(orcaster-worker|orcaster-proxy)$"
    assert_not_contains "$output" "(-[0-9]+)?"
    assert_not_contains "$output" "orcaster-worker-*"
    assert_not_contains "$output" "containers to remove"
}

@test "down-client real without N: the suffixed workers and proxies survive" {
    podman_fleet_stub
    run make down-client PODMAN="$TMPENV/podman"
    [ "$status" -eq 0 ]
    assert_contains "$output" "removed: orcaster-worker"
    assert_contains "$output" "removed: orcaster-proxy"
    run cat "$TMPENV/rm.log"
    [ "${#lines[@]}" -eq 2 ]
    [ "${lines[0]}" = "-f orcaster-worker" ]
    [ "${lines[1]}" = "-f orcaster-proxy" ]
    run grep -E -- '-[0-9]+$|other-app' "$TMPENV/rm.log"
    [ "$status" -ne 0 ]
}

@test "down-client real N=2: deletes only that worker and its sidecar" {
    podman_fleet_stub
    run make down-client PODMAN="$TMPENV/podman" N=2
    [ "$status" -eq 0 ]
    run cat "$TMPENV/rm.log"
    [ "${#lines[@]}" -eq 2 ]
    [ "${lines[0]}" = "-f orcaster-worker-2" ]
    [ "${lines[1]}" = "-f orcaster-proxy-2" ]
}

@test "down-client ALL=1 keeps the fleet suffix for the full sweep" {
    run make -n down-client ALL=1
    [ "$status" -eq 0 ]
    assert_contains "$output" "^(orcaster-worker|orcaster-proxy)(-[0-9]+)?$"
    assert_contains "$output" "orcaster-worker + orcaster-worker-*"
    assert_contains "$output" "containers to remove (ALL=1)"
}

@test "down-client real ALL=1: lists the containers and then sweeps the whole fleet" {
    podman_fleet_stub
    run make down-client PODMAN="$TMPENV/podman" ALL=1
    [ "$status" -eq 0 ]
    assert_contains "$output" "containers to remove (ALL=1)"
    assert_contains "$output" "     - orcaster-worker-2"
    assert_contains "$output" "     - orcaster-proxy-1"
    assert_not_contains "$output" "other-app"
    # The list comes BEFORE the first deletion.
    list_line="$(printf '%s\n' "$output" | grep -n 'containers to remove' | head -1 | cut -d: -f1)"
    rm_line="$(printf '%s\n' "$output" | grep -n 'removed: ' | head -1 | cut -d: -f1)"
    [ "$list_line" -lt "$rm_line" ]
    run cat "$TMPENV/rm.log"
    [ "${#lines[@]}" -eq 6 ]
    run grep -q 'other-app' "$TMPENV/rm.log"
    [ "$status" -ne 0 ]
}

@test "down-client ALL=1 with N is rejected without deleting anything" {
    podman_fleet_stub
    run make down-client PODMAN="$TMPENV/podman" ALL=1 N=2
    [ "$status" -ne 0 ]
    assert_contains "$output" "mutually exclusive"
    [ ! -s "$TMPENV/rm.log" ]
}

@test "down-client with an ALL other than 1 is rejected without deleting anything" {
    podman_fleet_stub
    run make down-client PODMAN="$TMPENV/podman" ALL=yes
    [ "$status" -ne 0 ]
    assert_contains "$output" "ALL=1"
    [ ! -s "$TMPENV/rm.log" ]
}

@test "down-client does not touch volumes nor networks (that is purge-client)" {
    # The down is reversible with an `up client` and so it cannot be
    # destructive: the worker's volumes hold its identity (state.json) and
    # deleting them is purge-client's job. The egress networks are deleted by
    # NEITHER of the two: they are shared by the whole fleet (they carry no N
    # suffix), so cleaning them up when bringing one worker down cuts the
    # egress of the rest.
    run make -n down-client
    [ "$status" -eq 0 ]
    assert_not_contains "$output" "volume rm"
    assert_not_contains "$output" "network rm"
}

# --- purge-client does not leave the sidecar orphaned either ----------------
# `down-client` learned to delete the sidecar, but `purge-client` kept deleting
# only the worker container and its two volumes: `make purge client N=n` left
# $(PROXY_NAME) running and $(EGRESS_VOL) dangling, i.e. the same kind of
# orphan through a sanctioned path. Both are PER-WORKER (they carry the N
# suffix), so they go away with the worker; the networks are shared and stay.

@test "purge-client deletes the egress sidecar as well as the worker" {
    run make -n purge-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "^(orcaster-worker|orcaster-proxy)$"
    assert_contains "$output" "ps -a --format '{{.Names}}'"
}

@test "purge-client also deletes the worker's allowlist volume" {
    run make -n purge-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "for v in orcaster-data orcaster-cache orcaster-egress-vol;"
}

@test "purge-client does not delete the egress networks, which are shared" {
    # The networks carry no N suffix: they are the only pieces of the topology
    # shared by the whole fleet, and deleting them when purging ONE worker would
    # cut the egress of the others. They are recreated on their own by
    # egress-net, which is idempotent.
    run make -n purge-client
    [ "$status" -eq 0 ]
    assert_not_contains "$output" "network rm"
}

@test "purge-client N=2 purges only the requested worker and does not sweep the suffixes" {
    run make -n purge-client N=2
    [ "$status" -eq 0 ]
    assert_contains "$output" "^(orcaster-worker-2|orcaster-proxy-2)$"
    assert_contains "$output" "for v in orcaster-data-2 orcaster-cache-2 orcaster-egress-vol-2;"
    # Deliberately WITHOUT the `(-[0-9]+)?` of down-client ALL=1: purge is
    # destructive and its volumes are per-N, so sweeping the containers of all
    # the workers while deleting only one's volumes would leave the rest's data
    # orphaned.
    assert_not_contains "$output" "(-[0-9]+)?"
}

@test "purge-client reports the outcome of each volume instead of swallowing it" {
    # A silent `volume rm ... >/dev/null || true` leaves the operator believing
    # the worker's identity was purged when podman refused because the volume is
    # still used by another container: the next `up client` revives the old
    # state.json and the symptom shows up far from the cause.
    run make -n purge-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "volume exists"
    assert_contains "$output" "removed: volume"
    assert_contains "$output" "Could not delete volume"
}

# --- purge-client reports the outcome of each container ----------------------
# The same `xargs -r podman rm -f >/dev/null 2>&1 || true` followed by an
# unconditional `echo ... | sed 's/^/   removed: /'` that the previous test
# closed for the volumes was still alive for the containers of this SAME
# recipe: `make purge client N=n` with a container that podman refused to
# delete still printed "removed" for that name. As in down-client, the first
# test checks the text under `make -n`; the second and third run the real
# recipe against the podman stub.

@test "purge-client reports the outcome of each container instead of swallowing it" {
    run make -n purge-client
    [ "$status" -eq 0 ]
    assert_contains "$output" "for n in \$names"
    assert_contains "$output" "Could not remove container"
    assert_not_contains "$output" "xargs -r"
}

@test "purge-client real: a container that podman refuses to delete is not reported as removed" {
    podman_stub 1
    run make purge-client PODMAN="$TMPENV/podman"
    [ "$status" -eq 0 ]
    assert_contains "$output" "Could not remove container orcaster-worker"
    assert_not_contains "$output" "removed: orcaster-worker"
}

@test "purge-client real: a container that podman does delete is reported as removed" {
    podman_stub 0
    run make purge-client PODMAN="$TMPENV/podman"
    [ "$status" -eq 0 ]
    assert_contains "$output" "removed: orcaster-worker"
    assert_not_contains "$output" "Could not remove container"
}
