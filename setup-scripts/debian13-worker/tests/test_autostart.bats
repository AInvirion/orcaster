#!/usr/bin/env bats
# Verifies that 06-autostart.sh resolves the right container/unit according to
# N, persists the egress sidecar when it exists and orders the worker behind it,
# and respects the RUN_AS seam. It mocks everything privileged (RUN_AS,
# need_root, load_env) or isolates it in a temporary HOME with fake binaries.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../06-autostart.sh"
    STUBDIR="$(mktemp -d)"
    CALLS="$STUBDIR/calls.log"
    export CALLS
    # lib.sh stub: defines the minimal helpers + a $RUN_AS recorder. 06 does
    # `source "$(dirname "$0")/lib.sh"`, so we intercept with a fake lib.sh via
    # LIB_OVERRIDE. EXISTING_CONTAINERS controls which containers "exist" for the
    # `podman container exists` check (that way we tell a present proxy from an
    # absent one without a real podman).
    export LIB_OVERRIDE="$STUBDIR/lib.sh"
    cat > "$LIB_OVERRIDE" <<'LIB'
step() { :; }
log()  { :; }
ok()   { :; }
warn() { :; }
die()  { echo "DIE: $*" >&2; exit 1; }
need_root() { echo "need_root $*" >>"$CALLS"; }
load_env() { INSTALL_DIR=/opt/orcaster; WORKER_USER=orcaster; WORKER_HOME=/home/orcaster; }
worker_name() { local n="${1:-}"; if [ -n "$n" ]; then printf 'orcaster-worker-%s\n' "$n"; else printf 'orcaster-worker\n'; fi; }
worker_unit() { printf 'container-%s.service\n' "$(worker_name "${1:-}")"; }
proxy_name() { local n="${1:-}"; if [ -n "$n" ]; then printf 'orcaster-proxy-%s\n' "$n"; else printf 'orcaster-proxy\n'; fi; }
proxy_unit() { printf 'container-%s.service\n' "$(proxy_name "${1:-}")"; }
# RUN_AS recorder: records the invocation and, for `podman container exists
# <name>`, answers according to EXISTING_CONTAINERS (space-separated list).
as_worker() {
    echo "as_worker $*" >>"$CALLS"
    if [ "${1:-}" = "podman" ] && [ "${2:-}" = "container" ] && [ "${3:-}" = "exists" ]; then
        case " ${EXISTING_CONTAINERS:-} " in *" ${4:-} "*) return 0 ;; *) return 1 ;; esac
    fi
    return 0
}
LIB
}

teardown() { rm -rf "$STUBDIR"; }

@test "06-autostart: N=2 operates on orcaster-worker-2 and its unit" {
    EXISTING_CONTAINERS="orcaster-worker-2" N=2 bash "$SCRIPT"
    grep -q 'orcaster-worker-2' "$CALLS"
    grep -q 'container-orcaster-worker-2.service' "$CALLS"
    ! grep -qE 'orcaster-worker([^-]|$)' "$CALLS"   # does not touch the base name
}

@test "06-autostart: without N it keeps backward compatibility (base name)" {
    EXISTING_CONTAINERS="orcaster-worker" bash "$SCRIPT"
    grep -q 'orcaster-worker' "$CALLS"
    ! grep -q 'orcaster-worker-' "$CALLS"
}

@test "06-autostart: without a sidecar it generates no proxy unit and no ordering (backward compatibility)" {
    EXISTING_CONTAINERS="orcaster-worker" bash "$SCRIPT"
    # The worker unit is generated in adoption mode and without deps. The
    # 'systemd +--name' pattern (nothing in between) already proves the absence
    # of --new, --wants and --after in the generation line.
    grep -qE 'podman generate systemd +--name orcaster-worker' "$CALLS"
    # Negations via run+status: a `! grep` in the middle of a @test is IGNORED by
    # bats (errexit does not fire in negated pipelines unless they are the last
    # line). NOTE: 'orcaster-proxy' alone cannot be negated — the `podman
    # container exists orcaster-proxy` check always mentions it. We negate that
    # its unit is GENERATED/ENABLED, and that the worker's carries ordering
    # dependencies.
    run grep -q 'generate systemd.*orcaster-proxy' "$CALLS";     [ "$status" -ne 0 ]
    run grep -q 'enable.*container-orcaster-proxy' "$CALLS";     [ "$status" -ne 0 ]
    run grep -q -- '--wants' "$CALLS";                           [ "$status" -ne 0 ]
    run grep -q -- '--after' "$CALLS";                           [ "$status" -ne 0 ]
}

@test "06-autostart: with a sidecar it persists its unit and orders the worker behind it" {
    EXISTING_CONTAINERS="orcaster-worker orcaster-proxy" bash "$SCRIPT"
    # Proxy unit generated in adoption mode (without --new).
    grep -q 'podman generate systemd --name orcaster-proxy' "$CALLS"
    # Worker unit with a dependency + ordering on the proxy's.
    grep -q 'podman generate systemd --wants container-orcaster-proxy.service --after container-orcaster-proxy.service --name orcaster-worker' "$CALLS"
    # Both units enabled; the proxy before the worker.
    grep -q 'systemctl --user enable --now container-orcaster-proxy.service' "$CALLS"
    grep -q 'systemctl --user enable --now container-orcaster-worker.service' "$CALLS"
    proxy_line="$(grep -n 'enable --now container-orcaster-proxy.service' "$CALLS" | head -1 | cut -d: -f1)"
    worker_line="$(grep -n 'enable --now container-orcaster-worker.service' "$CALLS" | head -1 | cut -d: -f1)"
    [ "$proxy_line" -lt "$worker_line" ]
}

# Under the egress opt-out (EGRESS_ISOLATION=0), 06-autostart must NOT re-bless
# the sidecar that survives from a previous isolated run: neither generate its
# unit, nor enable it, nor hang the worker behind it. It does NOT disable it
# (that would be the destructive option that was NOT chosen): it only stops
# re-enabling it, and run-fleet reports the stale sidecar so the operator
# decides to take it down.
@test "06-autostart: EGRESS_ISOLATION=0 does not persist the sidecar even if the container exists" {
    EXISTING_CONTAINERS="orcaster-worker orcaster-proxy" EGRESS_ISOLATION=0 bash "$SCRIPT"
    # The worker unit is generated in adoption mode and WITHOUT ordering deps.
    grep -qE 'podman generate systemd +--name orcaster-worker' "$CALLS"
    # It neither generates nor enables the proxy unit, nor hangs the worker behind
    # it. Negations via run+status (a `! grep` in the middle of a @test does not
    # fail it).
    run grep -q 'generate systemd.*orcaster-proxy' "$CALLS";  [ "$status" -ne 0 ]
    run grep -q 'enable.*container-orcaster-proxy' "$CALLS";  [ "$status" -ne 0 ]
    run grep -q -- '--wants' "$CALLS";                        [ "$status" -ne 0 ]
    run grep -q -- '--after' "$CALLS";                        [ "$status" -ne 0 ]
    # Non-destructive: it does not disable the existing proxy unit either.
    run grep -q 'disable.*container-orcaster-proxy' "$CALLS"; [ "$status" -ne 0 ]
}

@test "06-autostart: EGRESS_ISOLATION=1 preserves persistence by sidecar presence" {
    EXISTING_CONTAINERS="orcaster-worker orcaster-proxy" EGRESS_ISOLATION=1 bash "$SCRIPT"
    grep -q 'podman generate systemd --name orcaster-proxy' "$CALLS"
    grep -q 'podman generate systemd --wants container-orcaster-proxy.service --after container-orcaster-proxy.service --name orcaster-worker' "$CALLS"
    grep -q 'systemctl --user enable --now container-orcaster-proxy.service' "$CALLS"
    grep -q 'systemctl --user enable --now container-orcaster-worker.service' "$CALLS"
}

@test "06-autostart: with a sidecar and N=2 it suffixes the proxy and the proxy unit" {
    EXISTING_CONTAINERS="orcaster-worker-2 orcaster-proxy-2" N=2 bash "$SCRIPT"
    grep -q 'podman generate systemd --name orcaster-proxy-2' "$CALLS"
    grep -q -- '--wants container-orcaster-proxy-2.service --after container-orcaster-proxy-2.service' "$CALLS"
}

@test "06-autostart: the hand-off adopts the container without taking it down (no 'podman rm -f' nor 'make down client')" {
    EXISTING_CONTAINERS="orcaster-worker" bash "$SCRIPT"
    # Adoption mode: the unit is enabled with enable --now (ExecStart=podman
    # start, a no-op on the container that `make up client` already created); the
    # container is NOT destroyed → no double creation in the hand-off.
    grep -q 'systemctl --user enable --now container-orcaster-worker.service' "$CALLS"
    # Negations via run+status (see note above: `! grep` in the middle of a @test
    # does not fail the test). The container is NOT destroyed nor taken down with
    # `make down`.
    run grep -q 'podman rm -f' "$CALLS";       [ "$status" -ne 0 ]
    run grep -q 'make .*down client' "$CALLS"; [ "$status" -ne 0 ]
}

@test "06-autostart: aborts if the worker container does not exist" {
    run env EXISTING_CONTAINERS="" bash "$SCRIPT"
    [ "$status" -ne 0 ]
    [[ "$output" == *"Container orcaster-worker does not exist"* ]]
}

@test "06-autostart: an empty RUN_AS does not elevate with need_root and generates the units as the current user" {
    # Path used by the Makefile's AUTOSTART_SYNC hook: it already runs AS the
    # worker. We isolate in a temporary HOME with fake binaries so as not to
    # touch the system nor require a real podman/systemctl.
    FAKEHOME="$STUBDIR/home"; mkdir -p "$FAKEHOME"
    BIN="$STUBDIR/bin"; mkdir -p "$BIN"
    for tool in podman systemctl; do
        cat > "$BIN/$tool" <<'T'
#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$CALLS"
# `podman container exists <name>`: 0 if it is in EXISTING_CONTAINERS.
if [ "$(basename "$0")" = "podman" ] && [ "${1:-}" = "container" ] && [ "${2:-}" = "exists" ]; then
    case " ${EXISTING_CONTAINERS:-} " in *" ${3:-} "*) exit 0 ;; *) exit 1 ;; esac
fi
exit 0
T
        chmod +x "$BIN/$tool"
    done
    run env -i HOME="$FAKEHOME" PATH="$BIN:/usr/bin:/bin" CALLS="$CALLS" \
        LIB_OVERRIDE="$LIB_OVERRIDE" EXISTING_CONTAINERS="orcaster-worker" \
        RUN_AS= bash "$SCRIPT"
    [ "$status" -eq 0 ]
    # It does not elevate: need_root was never called. Negation via run+status (a
    # `! grep` in the middle of a @test does NOT fail it: errexit does not fire on
    # a negated command that is not the last line).
    run grep -q 'need_root' "$CALLS"; [ "$status" -ne 0 ]
    # The unit was really written in the temporary HOME.
    [ -f "$FAKEHOME/.config/systemd/user/container-orcaster-worker.service" ]
    grep -q 'systemctl --user enable --now container-orcaster-worker.service' "$CALLS"
}
