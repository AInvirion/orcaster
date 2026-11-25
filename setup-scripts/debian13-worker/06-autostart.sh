#!/usr/bin/env bash
# 06 — Autostart at boot. Enables automatic start of the rootless worker after
# a VPS reboot, by generating a systemd --user unit from the already existing
# container. It is NOT part of run-all: it is a post-startup step, analogous to
# 09-bootstrap-owner.sh of the server. Run it AFTER 'make up client', when the
# 'orcaster-worker' container already exists.
#
# Why it is needed: 'make up client' starts with '--restart unless-stopped',
# but podman-restart.service filters restart-policy=always and does NOT restart
# that container at boot. The systemd --user unit (on top of 'loginctl
# enable-linger') does guarantee automatic start.
#
# Egress isolation: if the worker was recreated with EGRESS_ISOLATION=1, there
# is also the proxy sidecar 'orcaster-proxy$(SUFFIX)' (started with
# '--restart unless-stopped', which podman-restart.service does not restart at
# boot either). This script detects that sidecar and ALSO persists its unit, and
# orders the worker's unit AFTER the proxy's (--after/--wants): after a reboot
# the worker must not be left on the internal network with the proxy down (no
# egress).
#
# Adoption mode (without --new): the generated unit does 'podman start <name>' /
# 'podman stop', ADOPTING the container that 'make up client' already created
# instead of re-running the whole 'podman run'. That way the hand-off does not
# take the worker down and bring it back up (no double creation). The run
# command (networks, mounts, egress flags) is set by the Makefile, not the unit.
#
# Why it remains re-runnable after a recreate: the adoption unit embeds a
# 'PIDFile' with the container ID. When recreated ('make up client' does 'podman
# rm -f' + 'podman run'), the ID changes, so the unit must be regenerated to
# point at the new PIDFile; otherwise systemd would run 'podman start' but look
# for the PIDFile of a container that no longer exists and the unit would fail
# (that was the shape the gap took under egress isolation). The Makefile
# ('up-client', AUTOSTART_SYNC hook) re-invokes this script after recreating when
# the unit already exists, so regeneration does not depend on a manual step.
#
# Usage:
#   sudo bash 06-autostart.sh            # normal path (root -> as_worker)
#   RUN_AS= bash 06-autostart.sh         # already running as the worker user
set -euo pipefail
# LIB_OVERRIDE is a test seam: as root it is ignored and the code of this very
# directory is used, the one that lib.sh's origin guard verifies. Root does not
# source or execute a path that arrived through the environment.
if [ "$(id -u)" = 0 ]; then
    unset LIB_OVERRIDE
fi
# shellcheck disable=SC1090
source "${LIB_OVERRIDE:-$(dirname "$0")/lib.sh}"

# RUN_AS seam (same idiom as run-fleet.sh's RUN_AS_WORKER): by default
# 'as_worker' (root dropping to WORKER_USER through sudo). The Makefile hook sets
# it to "" when it is already running AS the worker user (make up client runs as
# orcaster in production), avoiding a sudo to itself. Tests also set it to
# record the calls. The "-" form (not ":-") respects an explicit "".
RUN_AS="${RUN_AS-as_worker}"

# need_root only applies to the as_worker path (it needs sudo to drop to the
# worker). Already running as the worker (RUN_AS="") there is no need to elevate.
# Explicit `if` (not `&&`): with RUN_AS="" a `[ ... ] && need_root` would return
# non-zero and set -e would abort the script.
if [ -n "$RUN_AS" ]; then
    need_root "$@"
fi
load_env

N="${N:-}"

# Egress opt-out. run-fleet.sh propagates it EXPLICITLY in its invocation of this
# script; when run by hand, load_env already set the authoritative default.
# UNSET is treated as "no opt-out" to preserve the historical behaviour (persist
# the sidecar by presence): only an explicit EGRESS_ISOLATION=0 changes the
# decision. Recapturing it here also covers the test path, where the load_env
# stub does not touch this variable.
EGRESS_ISOLATION="${EGRESS_ISOLATION:-1}"

WNAME="$(worker_name "$N")"
WUNIT="$(worker_unit "$N")"
PNAME="$(proxy_name "$N")"
PUNIT="$(proxy_unit "$N")"

step "06 · Autostart at boot (systemd --user) — worker: $WNAME"

# systemd --user manager. When already running as the worker (RUN_AS=""), nobody
# set XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS if the shell comes from
# `sudo -iu`: they are derived here with the same convention as as_worker. With
# as_worker it is not needed, it sets them itself.
# shellcheck disable=SC1091
source "$(dirname "$0")/user-bus.sh"
if [ -z "$RUN_AS" ]; then
    user_bus_derive_env
fi

# Probe BEFORE writing anything: if the manager does not respond, the
# `daemon-reload` further below would fail with the unit file ALREADY rewritten,
# and disk and manager would diverge (new unit on disk, old unit loaded with a
# MainPID that does not exist). That state does not fail at the moment: it stops
# the worker on the next `daemon-reload` that does reach the bus.
# shellcheck disable=SC2086 # intentional word-split of RUN_AS
if ! bus_err="$($RUN_AS systemctl --user show-environment 2>&1 >/dev/null)"; then
    echo "ERROR: could not reach the systemd --user manager of the worker; no unit was regenerated." >&2
    if [ -z "$RUN_AS" ]; then
        user_bus_explain "$bus_err"
    else
        echo "       systemctl replied: $bus_err" >&2
        echo "       Remedy: check linger (sudo loginctl enable-linger $WORKER_USER) and retry." >&2
    fi
    exit 1
fi

# The container must exist: the unit is derived from its run command.
# shellcheck disable=SC2086 # intentional word-split of RUN_AS (0 or 1 words)
$RUN_AS podman container exists "$WNAME" \
    || die "Container $WNAME does not exist. Start it first: make up client${N:+ N=$N}."

# Does the worker run under the egress topology? The sidecar only exists if it
# was recreated with EGRESS_ISOLATION=1. Its presence is a NECESSARY condition
# to persist its unit, but no longer a sufficient one (see below).
proxy_present=0
# shellcheck disable=SC2086 # intentional word-split of RUN_AS
if $RUN_AS podman container exists "$PNAME" 2>/dev/null; then
    proxy_present=1
fi

# Sidecar persistence decision. It used to be enough for the container to exist
# (persist-by-presence): we regenerated its unit, enabled it and hung the worker
# behind it on EVERY rerun. The bug: if the host lowered EGRESS_ISOLATION from 1
# to 0, the sidecar SURVIVES from the previous isolated run, and that
# persist-by-presence RE-blessed it on every rerun, silently contradicting the
# opt-out. Under the opt-out we do NOT persist it: no unit, no enable, no
# --wants/--after. And we do NOT disable it ourselves: that would be the
# destructive option that was NOT chosen; run-fleet REPORTS the stale sidecar and
# the operator decides to take it down. UNSET = no opt-out (historical behaviour).
persist_proxy=0
if [ "$proxy_present" -eq 1 ] && [ "$EGRESS_ISOLATION" != "0" ]; then
    persist_proxy=1
fi

# Dependency flags for the worker unit. It uses --wants + --after (not
# --requires): --wants pulls the proxy's start in at boot and --after orders the
# worker behind it, without the tight coupling of --requires (which would knock
# the worker over if the operator stops/recreates the proxy for maintenance). If
# the proxy goes down while running, the worker loses egress and crash-loops on
# its own until the proxy (Restart) comes back; there is no need to take it down
# through a hard dependency. Under the opt-out (persist_proxy=0) they stay empty:
# we do not hang the worker off a sidecar that the host no longer wants.
worker_dep_flags=""
if [ "$persist_proxy" -eq 1 ]; then
    worker_dep_flags="--wants $PUNIT --after $PUNIT"
fi

# shellcheck disable=SC2086 # intentional word-split of RUN_AS
$RUN_AS bash -c "mkdir -p ~/.config/systemd/user"

if [ "$persist_proxy" -eq 1 ]; then
    log "Egress sidecar detected ($PNAME): persisting its unit and ordering the worker behind it."
    # Adoption (without --new): the unit will do 'podman start $PNAME' on the
    # sidecar that 'up-proxy' already created, without recreating it.
    # shellcheck disable=SC2086 # intentional word-split of RUN_AS
    $RUN_AS bash -c "podman generate systemd --name $PNAME > ~/.config/systemd/user/$PUNIT"
elif [ "$proxy_present" -eq 1 ]; then
    # The sidecar exists but the host declared EGRESS_ISOLATION=0: I do NOT
    # persist it nor hang the worker behind it. I do not disable it (non-
    # destructive): if an enabled unit was left from the previous isolated run,
    # taking it down is the operator's decision (run-fleet reports the stale
    # sidecar with the recipe).
    log "EGRESS_ISOLATION=0: the sidecar $PNAME exists but the host opted out of egress isolation; I do NOT persist its unit nor order the worker behind it. If $PUNIT was left enabled from before, take it down by hand: systemctl --user disable --now $PUNIT (as the worker user)."
fi

log "Generating the worker's systemd --user unit from the existing container…"
# Adoption (without --new): the unit does 'podman start $WNAME' / 'podman stop'
# on the container that 'make up client' already created, instead of re-running
# the whole 'podman run'. systemd ADOPTS the existing container (no double
# creation in the hand-off). The run command (env-file, volumes, mounts,
# --no-healthcheck, egress flags) is set by the Makefile, not the unit. 'podman
# generate systemd' is deprecated in favour of Quadlet, but it still works in
# 5.4.2 and avoids rewriting the topology by hand.
# shellcheck disable=SC2086 # intentional word-split of RUN_AS and worker_dep_flags
$RUN_AS bash -c "podman generate systemd $worker_dep_flags --name $WNAME > ~/.config/systemd/user/$WUNIT"

# shellcheck disable=SC2086 # intentional word-split of RUN_AS
$RUN_AS systemctl --user daemon-reload

# Adoption hand-off: 'make up client' (and 'up-proxy') left the containers
# ALREADY RUNNING under those names. With adoption units they must NOT be taken
# down: 'enable --now' runs the ExecStart 'podman start <name>', which on an
# already active container is a no-op and leaves systemd supervising it (through
# its PIDFile). That avoids the down+up cycle of the worker on every recreation.
log "Handing the container(s) over to systemd --user (adoption)…"
# The proxy first: the worker's unit has --wants/--after on it, so enabling both
# is enough and systemd respects the order at boot. While running both are
# already up; enable --now only adopts them.
if [ "$persist_proxy" -eq 1 ]; then
    # shellcheck disable=SC2086 # intentional word-split of RUN_AS
    $RUN_AS systemctl --user enable --now "$PUNIT"
fi
# shellcheck disable=SC2086 # intentional word-split of RUN_AS
$RUN_AS systemctl --user enable --now "$WUNIT"

log "Verifying…"
# shellcheck disable=SC2086 # intentional word-split of RUN_AS
$RUN_AS systemctl --user is-enabled "$WUNIT" >/dev/null \
    || die "Unit $WUNIT was not left enabled."
if [ "$persist_proxy" -eq 1 ]; then
    # shellcheck disable=SC2086 # intentional word-split of RUN_AS
    $RUN_AS systemctl --user is-enabled "$PUNIT" >/dev/null \
        || warn "The proxy unit $PUNIT was not left enabled; check 'systemctl --user status $PUNIT'."
fi

# With adoption the container is not recreated (it already existed before the
# enable); this check is defensive, with a short retry in case systemd is slow to
# reflect the state after the enable.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    # shellcheck disable=SC2086 # intentional word-split of RUN_AS
    $RUN_AS podman container exists "$WNAME" && break
    sleep 1
done
# shellcheck disable=SC2086 # intentional word-split of RUN_AS
$RUN_AS podman container exists "$WNAME" \
    || warn "The container does not appear; check 'systemctl --user status $WUNIT'."

ok "Autostart enabled: $WUNIT (systemd --user, on top of linger)."
if [ "$persist_proxy" -eq 1 ]; then
    ok "Egress sidecar persisted: $PUNIT (worker ordered behind it via --after)."
fi
ok "To verify startup at boot, just reboot the VPS."
