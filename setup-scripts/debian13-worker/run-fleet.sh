#!/usr/bin/env bash
# run-fleet: closes the zero-touch cycle that run-all-worker.sh leaves open.
# For each worker (1..WORKER_COUNT) it stamps its env.i in CONFIG_DIR (formerly
# client/.env.i, moved out of the checkout) with the shared multi-use token
# and its name (the ORCH_WORKER_NAME already stamped in env.i if there is one,
# otherwise PREFIX-i), starts it with 'make up client N=i' and enables its
# autostart. Idempotent.
#
# Usage (normally invoked by cloud-init-worker.sh, not by hand):
#   sudo bash run-fleet.sh
set -euo pipefail
# LIB_OVERRIDE and AUTOSTART_SCRIPT are test seams: as root they are ignored
# and the code in this same directory is used, the one whose origin guard in
# lib.sh is verified. Root neither sources nor executes a path that arrived
# through the environment.
if [ "$(id -u)" = 0 ]; then
    unset LIB_OVERRIDE AUTOSTART_SCRIPT
fi
# shellcheck disable=SC1090
source "${LIB_OVERRIDE:-$(dirname "$0")/lib.sh}"

# Test seams / overrides.
# RUN_AS_WORKER: uses the "-" form (not ":-"/":=") so that an explicit EMPTY
# value (as set by the integration test, to run 'make' directly against the
# mock in PATH) is respected and not overridden by the default.
# In production (cloud-init-worker.sh does not set it) it stays unset and falls
# back to 'as_worker'.
RUN_AS_WORKER="${RUN_AS_WORKER-as_worker}"
# Physical (pwd -P), same as in lib.sh: with the lib.sh of this directory it is
# the same path the origin guard already verified, and 06-autostart.sh runs as
# root from here.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
: "${AUTOSTART_SCRIPT:=$SCRIPT_DIR/06-autostart.sh}"
# ENROLL_CHECK: command that answers 0 if worker N (its only arg) already enrolled.
# Same "-" convention as RUN_AS_WORKER: an empty value/test mock is respected.
ENROLL_CHECK="${ENROLL_CHECK-worker_enrolled}"

# Does worker N already have credentials issued by the server? Enrollment
# persists state.json (worker_id/api_key) in the data volume: its presence
# proves that registration really happened (the server issued the api_key),
# that is, that the worker is already visible in the dashboard.
#
# It requires BOTH keys with a non-empty value, the same rule the worker applies
# on startup (client/src/orcaster_worker/state.py, `load`): with only one, or
# with an empty value, the worker discards the file and asks for the token
# again. That way "enrolled" means here the same as for the worker, and both
# step 2/2 and the early credentials check in the loop use it.
#
# Only `grep -q`: the content of state.json (the api_key) is never printed.
worker_enrolled() {
    local n="$1" vol mp key
    vol="$(worker_volume "$n")"
    # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
    mp="$($RUN_AS_WORKER podman volume inspect "$vol" --format '{{.Mountpoint}}' 2>/dev/null)" || return 1
    [ -n "$mp" ] || return 1
    # podman unshare: the container runs as USER orcaster (internal uid 1000),
    # so in rootless mode state.json ends up 0600 owned by a host SUBUID,
    # unreadable by the host's orcaster (a direct grep would give EACCES and
    # step 2/2 would always time out). Inside the userns the file is readable.
    for key in worker_id api_key; do
        # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
        $RUN_AS_WORKER podman unshare grep -Eq "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]" \
            "$mp/state.json" 2>/dev/null || return 1
    done
}

# Existence probe for a systemd --user unit, with THREE answers rather than two:
# 0 = the unit exists, 1 = this host has no unit file for it, 2 = it could not
# be asked and the reason systemd gave is left in $unit_probe_err. The two
# prune disables use it so they do not disable what does not exist without
# going silent when the problem is something else.
#
# `show -p LoadState` and not `systemctl --user cat` (which is what the
# sidecar probe used to do): `cat` returns != 0 both when there is no unit file
# and when the worker's --user bus does not answer, and `as_worker` reaches
# systemd precisely through that bus (DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus,
# see lib.sh). With the output hidden by `2>/dev/null` both cases are the same
# exit code, so "this host never had egress" (staying quiet is correct) was
# confused with "I could not ask systemd", which is a real failure: the
# sidecar disable was skipped silently while the worker disable of the SAME
# index failed loudly in the same run.
#
# It decides the VALUE first and only then the exit code: a nonexistent unit
# is recognised by LoadState=not-found, whatever code this systemctl version
# exits with. If noise appeared on stderr in a successful probe (it is captured
# together with the value, because the message is the only thing that makes
# case 2 useful), the value stops being not-found and the probe falls on the
# safe side: it answers "exists", the disable is attempted and its result is
# visible.
#
# It also does not probe the unit file by hand (~/.config/systemd/user/$unit, as
# the Makefile does in its own `down`): there the `~` is the worker's because
# the recipe already runs as the worker, and run-fleet runs as root.
unit_probe_err=""
unit_probe() {
    local unit="$1" load rc=0
    unit_probe_err=""
    # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
    load="$($RUN_AS_WORKER systemctl --user show -p LoadState --value "$unit" 2>&1)" || rc=$?
    case "$load" in
        not-found) return 1 ;;
    esac
    if [ "$rc" -ne 0 ]; then
        unit_probe_err="$load"
        return 2
    fi
    return 0
}

load_env

PREFIX="${WORKER_NAME_PREFIX:-$(hostname -s 2>/dev/null || hostname)}"

# $CONFIG_DIR as 0750 root:$WORKER_USER, fatal if it cannot be done. It used to
# be `2>/dev/null || true`: `-o root` does not work when the test suite runs as
# a normal user, so the silence existed for the tests and took the real case
# along with it. Today the seam is the `install` and `chown` mocks in the tests'
# PATH, and here the failure is visible.
#
# It goes HERE and not inside the fleet loop: the directory is the same for all
# workers, so inside it was repeated once per worker (eight identical
# `install -d` with WORKER_COUNT=8, each with its implicit chmod/chown) to do
# the same work. Outside it runs once and the loop body keeps a single
# responsibility: the env.i of THAT worker.
install -d -m 0750 -o root -g "$WORKER_USER" "$CONFIG_DIR" \
    || die "Could not create $CONFIG_DIR as 0750 root:$WORKER_USER.
    Without that directory the fleet env files have nowhere to live."

# Host state of WORKER_RO_MOUNTS, BEFORE the loop. load_env already validated
# the syntax, but it runs as root, and root reads any mode: what matters is
# that the worker user can traverse each directory (podman resolves the source
# as that user) and that the container uid can read it through the "other"
# bits (check-ro-mounts.sh explains why).
# The Makefile repeats the probe before its `podman rm -f`, but that is not
# enough here: the loop stops the worker's systemd unit BEFORE calling make, so
# a missing directory would leave that worker stopped. It is measured once for
# the whole fleet, as the worker user, and the validator travels over stdin so
# it does not depend on that user being able to read $SCRIPT_DIR.
if [ -n "$WORKER_RO_MOUNTS" ]; then
    # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
    if ! $RUN_AS_WORKER bash -s -- --host "$WORKER_RO_MOUNTS" < "$RO_MOUNTS_CHECK"; then
        die "WORKER_RO_MOUNTS did not pass the host probe (reason above). No worker was touched."
    fi
fi

printf "\n=== run-fleet · %s worker(s) · prefix '%s' ===\n" "$WORKER_COUNT" "$PREFIX"

for i in $(seq 1 "$WORKER_COUNT"); do
    env_file="$(worker_envfile "$i" "$CONFIG_DIR")"
    # Name: a non-empty ORCH_WORKER_NAME already stamped in env.N wins; only a
    # new slot (no env.N, or with an empty name) gets PREFIX-i. Without this a
    # rerun renamed the worker (worker-1 became worker-1-1): the worker sends
    # `name` on every poll and the server overwrites the one it had. It also
    # wins over an explicit WORKER_NAME_PREFIX, on purpose: changing the prefix
    # does not rename already enrolled workers. To rename, edit or empty
    # ORCH_WORKER_NAME in env.N and run again. Same idiom as stamp_worker_env
    # for the previous api_key and token.
    name=""
    if [ -f "$env_file" ]; then
        name="$(sed -n 's/^ORCH_WORKER_NAME=//p' "$env_file" | head -n1)"
    fi
    # An env.N edited with CRLF leaves a trailing \r: it is stripped so it is
    # not restamped. A name made only of spaces counts as empty: otherwise it
    # would become permanent and the worker would send it blank to the server.
    name="${name%$'\r'}"
    case "$name" in *[![:space:]]*) ;; *) name="" ;; esac
    [ -n "$name" ] || name="$PREFIX-$i"
    # Disk gate: resolved by load_env from setup.env, its own block separate
    # from EXTRA_ENV. It survives this rerun because it comes from setup.env,
    # not from the previous env.i. The isolation block also comes from
    # load_env; the flag falls back to the previous env.i (or to 'false' on an
    # existing host, 'true' on a new one) only when setup.env and EXTRA_ENV do
    # not declare it: see lib.sh::stamp_worker_env.
    stamp_worker_env "$env_file" "$ORCH_BASE_URL" "$ORCH_ENROLLMENT_TOKEN" "$name" "$EXTRA_ENV" \
        "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" \
        "$ORCH_ENFORCE_ISOLATION" "$ISOLATION_REQUIRED_ENV"
    # env.i carries the enrollment token: 0640 root:$WORKER_USER, no more and
    # no less. Failing silently here leaves the worker unable to read its own
    # env and the container crash-loops until the step 2/2 timeout, far from
    # the cause; the message of that timeout tells you to look at the token,
    # which is fine.
    if ! chown "root:$WORKER_USER" "$env_file" || ! chmod 0640 "$env_file"; then
        die "Could not set $env_file to 0640 root:$WORKER_USER.
    The worker runs as $WORKER_USER and reads it through the group: without this it does not start."
    fi

    # Without credentials (no token in setup.env, no previous ORCH_API_KEY/token
    # in the .env, no state.json with worker_id/api_key in its volume) the
    # container can only crash-loop until the step 2/2 timeout: abort here,
    # early and with the cause, before starting anything.
    #
    # state.json is in the list: an already enrolled worker does not need a
    # token, because `ensure_credentials` (client/src/orcaster_worker/
    # main.py) loads its credentials from there and returns before looking at
    # it. An empty token in setup.env was already covered: stamp_worker_env
    # keeps the previous token of env.N. This branch covers an env.N with
    # neither token nor api key (deleted, regenerated or with the token cleared
    # by hand) and the volume still enrolled, which used to abort even though
    # the worker would start fine. It is queried ONLY when the other two are
    # missing, so podman is not touched when not needed. It goes through
    # $ENROLL_CHECK, the same probe (and the same test seam) as step 2/2.
    if ! grep -q '^ORCH_API_KEY=.\+$' "$env_file" && \
       ! grep -q '^ORCH_ENROLLMENT_TOKEN=.\+$' "$env_file"; then
        # shellcheck disable=SC2086 # intentional word-split of ENROLL_CHECK
        if $ENROLL_CHECK "$i"; then
            echo ">> Worker $i: no token or ORCH_API_KEY, but state.json in $(worker_volume "$i") already holds worker_id and api_key: reusing that identity."
        else
            die "Worker $i has no credentials: ORCH_ENROLLMENT_TOKEN is empty in setup.env,
    $env_file has no previous ORCH_API_KEY/token and the volume $(worker_volume "$i")
    has no state.json with worker_id and api_key (or it could not be read as $WORKER_USER).
    Mint a multi-use token and put it in setup.env."
        fi
    fi

    echo ">> Worker $i/$WORKER_COUNT ($name): up + autostart"
    # Rerun on an already autostarted worker: 'make up' does 'podman rm -f' on a
    # container that the systemd --user unit supervises with Restart=always, and
    # systemd relaunches it in parallel with the make's 'podman run' (race:
    # "container name is already in use"). Clean stop first.
    unit="$(worker_unit "$i")"
    # shellcheck disable=SC2086 # $RUN_AS_WORKER must expand to 0 or 1 words
    # (intentional word-split): quoting it would pass an empty command name
    # when RUN_AS_WORKER is set to "" (test seam).
    if $RUN_AS_WORKER systemctl --user is-active --quiet "$unit" 2>/dev/null; then
        echo ">> Unit $unit active: stopping before recreating."
        $RUN_AS_WORKER systemctl --user stop "$unit"
    fi
    # Under egress the proxy sidecar also runs under its systemd --user unit
    # (06-autostart persists it). Same reason as the worker: 'make up client'
    # will do 'podman rm -f' on the proxy that systemd supervises with Restart,
    # and since here we pass AUTOSTART_SYNC=0 (see below) the stop guard of
    # up-proxy does not run. We stop it ourselves before recreating.
    if [ "$EGRESS_ISOLATION" != "0" ]; then
        punit="$(proxy_unit "$i")"
        # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
        if $RUN_AS_WORKER systemctl --user is-active --quiet "$punit" 2>/dev/null; then
            echo ">> Unit $punit active: stopping before recreating."
            # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
            $RUN_AS_WORKER systemctl --user stop "$punit"
        fi
    fi
    # AUTOSTART_SYNC=0: run-fleet invokes 06-autostart.sh explicitly on the
    # next line; the auto-sync hook of `up-client` would be redundant and, worse,
    # would re-enable the unit we just stopped above before the hand-off.
    # EGRESS_ISOLATION is propagated so the fleet inherits the default.
    # WORKER_MEMORY is propagated even if empty: with a value it applies --memory
    # to podman run, and empty is the explicit opt-out (it wins over the
    # fallback that `up-client` makes against setup.env). Propagating it anyway
    # keeps this script as the single source of the decision: it reads setup.env
    # with `load_env`, which also validates the format before touching the fleet.
    # WORKER_ENV/WORKER_PUBKEY are NOT passed: the Makefile resolves
    # $CONFIG_DIR/env.N and $CONFIG_DIR/server-ed25519.pub on its own, the
    # single definition of those paths, and they are the files stamped above
    # (worker_envfile) and by 02-server-pubkey.sh. What it needs is the config
    # dir itself: as_worker does not propagate CONFIG_DIR, so it goes on the
    # line, together with ETC_SETUP_ENV_FILE as the Makefile guard requires.
    # worker_config_make_args adds the two overrides back only for a Makefile
    # without that resolution (older release), which would look in client/.
    # WORKER_RO_MOUNTS is explicit for the same reason as WORKER_MEMORY: this
    # script is the single source of the decision (load_env validates it), and
    # the value it validated is the one the containers get. Empty = no mounts:
    # the Makefile adds no -v and the `podman run` stays as it was before.
    worker_cfg_args=()
    while IFS= read -r _cfg_arg; do worker_cfg_args+=("$_cfg_arg"); done \
        < <(worker_config_make_args "$INSTALL_DIR" "$i" "$CONFIG_DIR")
    # shellcheck disable=SC2086 # see above: intentional word-split
    $RUN_AS_WORKER make -C "$INSTALL_DIR" up client N="$i" AUTOSTART_SYNC=0 EGRESS_ISOLATION="$EGRESS_ISOLATION" WORKER_MEMORY="$WORKER_MEMORY" CONFIG_DIR="$CONFIG_DIR" ETC_SETUP_ENV_FILE="$CONFIG_DIR/setup.env" ${worker_cfg_args[@]+"${worker_cfg_args[@]}"} WORKER_RO_MOUNTS="$WORKER_RO_MOUNTS"
    # CONFIG_DIR is passed EXPLICITLY, as on the make line above:
    # 06-autostart.sh resolves the host's setup.env with its own
    # `load_env`, which falls back to the /etc/orcaster default of lib.sh if the
    # variable does not arrive. lib.sh keeps it unexported, so this child only
    # inherited it because cloud-init-worker.sh exports it by hand; running
    # run-fleet.sh directly with an unexported CONFIG_DIR, the autostart read a
    # different directory than the rest of this script, and silently.
    #
    # EGRESS_ISOLATION is passed EXPLICITLY: 06-autostart decides whether to
    # persist the sidecar according to the opt-out, and without this signal it
    # would keep re-blessing it by presence on every rerun after lowering
    # EGRESS_ISOLATION from 1 to 0. It is the same single source that already
    # feeds `make up client` above (load_env).
    CONFIG_DIR="$CONFIG_DIR" N="$i" EGRESS_ISOLATION="$EGRESS_ISOLATION" bash "$AUTOSTART_SCRIPT"
done

# --- Prune: surplus workers from a rerun with a reduced WORKER_COUNT ---------
# Dismantles unit + container + env.N of every N > WORKER_COUNT. The volumes
# (orcaster-data-N: the worker's identity/credentials) are kept on purpose: if
# the fleet grows again, worker N reuses its already enrolled identity instead
# of consuming another use of the token.
for env_file in "$CONFIG_DIR"/env.*; do
    [ -e "$env_file" ] || continue
    n="${env_file##*/env.}"
    case "$n" in ''|*[!0-9]*) continue ;; esac
    # Explicit base 10, the same `10#` idiom that lib.sh documents and uses for
    # WORKER_COUNT. It goes AFTER the digit filter above, so `10#` only sees
    # digits and can never give the bash syntax error, and BEFORE the `-gt` and
    # everything that derives a name from this index. Without it,
    # `${env_file##*/env.}` keeps leading zeros: an env.08 left n=08, and with
    # that raw value `worker_unit` and the `down client N=` below name
    # container-orcaster-worker-08.service and orcaster-worker-08, names this
    # script NEVER creates because it iterates `seq 1 "$WORKER_COUNT"` (which
    # gives 8, not 08). Result: the unit probe answered rc=1 (does not exist),
    # the `down` removed nothing, the `rm -f` deleted env.08 anyway and the
    # operator read "pruning surplus worker 08" while the real
    # orcaster-worker-8 stayed alive taking tasks, already invisible to the
    # next rerun's glob.
    n=$((10#$n))
    [ "$n" -gt "$WORKER_COUNT" ] || continue
    echo ">> Pruning surplus worker $n (unit + container; the volume is kept)."
    # Both steps warn and continue, but no longer silently: a half prune is
    # worse than none and it used to be invisible. If the unit is not disabled,
    # systemd relaunches the container that the `down` below just stopped and
    # the surplus worker stays alive taking tasks. It does not abort: pruning
    # is cleanup and must not bring down a deployment that already started all
    # the requested workers.
    #
    # The output of the two commands is not captured: the `2>/dev/null` that
    # used to be here also hid the reason systemd gives, and saving it in a
    # variable for the message would hide the normal output of `make`. Each one
    # speaks for itself and the warn adds the context.
    unit_n="$(worker_unit "$n")"
    # Existence probe before the disable, the same as the sidecar one below:
    # without it the warn of this disable lies in the same case the sidecar
    # comment uses to justify its own probe. A host migrated to $CONFIG_DIR by
    # hand may have the env.N copied without ever having run 06-autostart.sh
    # for that index: there is no unit, the disable always fails and the notice
    # would talk about a container that systemd will relaunch when there is no
    # unit to relaunch it.
    #
    # rc=1 (no unit file for this index) prints nothing: there is nothing to
    # disable and the `down` below takes care of the container.
    unit_probe_rc=0
    unit_probe "$unit_n" || unit_probe_rc=$?
    case "$unit_probe_rc" in
        0)
            # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
            if ! $RUN_AS_WORKER systemctl --user disable --now "$unit_n"; then
                warn "Could not disable $unit_n (surplus worker $n).
    If the unit is still active, systemd will relaunch the container. Check it by hand."
            fi
            ;;
        2)
            warn "Could not tell whether $unit_n exists (surplus worker $n): $unit_probe_err
    Not attempting the disable, so as not to claim anything that was not
    verified. If the unit is enabled, systemd will relaunch the container that
    the down below stops. Check it by hand as $WORKER_USER."
            ;;
    esac
    # The pruned worker may ALSO have its egress sidecar, with its own unit
    # container-orcaster-proxy-N.service (06-autostart.sh persists it when the
    # worker was created with EGRESS_ISOLATION=1). The `down client` below
    # removes both containers, but if the proxy unit is still enabled systemd
    # relaunches it just like the worker: a proxy without a worker is left
    # occupying the egress networks and, after a tarball release swap, with its
    # bind mount of client/src/orcaster_worker over an already replaced inode.
    #
    # The disable goes BEFORE the `down` for the same reason as the worker's
    # (see above): the other way around, systemd relaunches what the down just
    # stopped.
    #
    # It probes the existence of the unit (`unit_probe`, above) instead of
    # gating on $EGRESS_ISOLATION: today's value in setup.env says nothing about
    # this worker, which may have been created with isolation on and only now
    # turned off. And without the probe the warn would lie: on a host that never
    # had egress the unit does not exist, the disable always fails and the
    # notice would talk about a container that systemd will relaunch when there
    # is no container at all.
    #
    # The probe distinguishes "no unit" from "could not ask": it used to be a
    # `cat` with the output hidden, which answers the same in both cases, so a
    # failed --user bus skipped this disable in total silence. Same split as the
    # worker one above: rc=1 stays quiet, rc=2 warns with the reason.
    punit_n="$(proxy_unit "$n")"
    punit_probe_rc=0
    unit_probe "$punit_n" || punit_probe_rc=$?
    case "$punit_probe_rc" in
        0)
            # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
            if ! $RUN_AS_WORKER systemctl --user disable --now "$punit_n"; then
                warn "Could not disable $punit_n (sidecar of surplus worker $n).
    If the unit is still enabled, systemd will relaunch the proxy that the down
    below removes, and it stays occupying the egress networks. Check it by hand."
            fi
            ;;
        2)
            warn "Could not tell whether $punit_n exists (sidecar of surplus worker $n): $unit_probe_err
    Not attempting the disable, so as not to claim anything that was not
    verified. If the unit is enabled, systemd will relaunch the proxy that the
    down below removes, and it stays occupying the egress networks. Check it by
    hand as $WORKER_USER."
            ;;
    esac
    # shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
    if ! $RUN_AS_WORKER make -C "$INSTALL_DIR" down client N="$n"; then
        warn "Could not stop the container of surplus worker $n.
    $env_file is deleted anyway, so the next rerun will no longer prune it."
    fi
    # The `rm` goes on the ORIGINAL $env_file of the glob and not on a path
    # rebuilt with the already normalised $n: the file on disk may be named
    # env.08 even though the index is 8, and a rebuilt env.$n would delete
    # nothing (leaving the surplus for the next rerun to prune, that is never,
    # because by then the `down` already stopped it and the container does not
    # exist for it to see).
    rm -f "$env_file"
done

# --- Orphan probe: containers the prune cannot see ---------------------------
# The prune above is guided by the env.N files in $CONFIG_DIR, which are the
# ones this script stamps: it is its own bookkeeping, not an inventory of the
# host. A orcaster-worker-N container (or its orcaster-proxy-N egress sidecar)
# with N > WORKER_COUNT whose env.N never existed in $CONFIG_DIR falls outside
# the glob, like the orcaster-worker / orcaster-proxy WITHOUT suffix, which no
# `env.N` can name. All of them keep running with their systemd unit enabled:
# the worker keeps taking tasks and the proxy keeps occupying the egress
# networks. Three real paths lead there:
#   - the migration of a host with more than one worker to $CONFIG_DIR: the
#     operator copies only the env.N files to keep and lowers WORKER_COUNT in
#     the same move, so the surplus indexes were never stamped here. After the
#     tarball swap those containers run over already replaced inodes;
#   - a prune `down` that failed: the env.N is deleted anyway (see the warn
#     above), and with that the orphan stops being visible to the next rerun's
#     glob.
#   - convergence to the fleet layout that the README tells you to do
#     (setup-scripts/debian13-worker/README.md, section on later releases):
#     a host finished by hand through the Bootstrap is left with the env
#     WITHOUT suffix and the orcaster-worker container WITHOUT suffix, which
#     push-release.sh does not know how to serve, so the operator runs THIS
#     script to converge. run-fleet stamps env.1 and creates orcaster-worker-1,
#     and the old orcaster-worker keeps running with its
#     container-orcaster-worker.service enabled: two workers asking the same
#     server for tasks, and after the next tarball swap the unsuffixed one runs
#     over already replaced inodes. The prune's `env.*` glob cannot see it
#     either, because that host's env has no suffix: it does not match.
# It warns and does NOT prune: env.N is the proof that this script owns that
# index, and a bare container is not (it may come from a manual `make up client
# N=`). But it cannot stay silent, which is what happened until now.
#
# Two lists and one notice per kind, instead of a single list of indexes: the
# remedy names the unit to disable before the `down`, and only the unit of the
# container that was actually seen can be named. A single notice with both
# units would send you to disable the worker's on a host where the only thing
# left is the proxy (the case of the failed `down` under egress), and that is
# claiming something the probe did not verify. For the same reason the two
# containers WITHOUT suffix carry their own flag and their own notice,
# independent of each other and of the numeric lists.
#
# The suffix of the numeric containers is reported and compared RAW, without
# the `n=$((10#$n))` that the prune above does apply. The asymmetry is
# deliberate and not an oversight: here the index does not name a file of this
# script but a container that exists on the host with that literal name, so a
# orcaster-worker-08 must be reported as 08 — normalising it to 8 would make
# the remedy tell you to disable and stop a container that does not exist,
# which is exactly the defect the normalisation fixes in the prune. The
# comparison tolerates the raw value: `[ 08 -gt 1 ]` exits 0 without noise and
# `[ 08 -gt 9 ]` exits 1, that is, `test` reads the operand in base 10 and not
# as octal. Measured on the TWO versions that matter: macOS bash 3.2.57 (where
# the bats suite runs) and Debian 13 bash 5.2.37 (where this script runs in
# production). It is `[[ ]]` and `(( ))` that blow up with "value too great for
# base", so the comparison has to stay with `[ ]` and nobody should
# "modernise" it.
orphans=""
proxy_orphans=""
# IN-FLEET orcaster-proxy-N sidecars (N <= WORKER_COUNT) that survive from a
# run with EGRESS_ISOLATION=1 when the host went down to EGRESS_ISOLATION=0.
# Its OWN category, distinct from proxy_orphans: they are not orphans by index
# (their N is inside the fleet), they are OBSOLETE by config. They deserve
# their own reason and their own recipe, without lying about the cause. It is
# only filled under the opt-out.
stale_proxies=""
# Both flags are initialised HERE, before the `if`: with `set -u` the branch of
# the failing `podman ps -a` skips the loop, and an uninitialised flag would
# kill the script at the notice check, hiding the warn of the real failure.
bare_worker_orphan=0
bare_proxy_orphan=0
# Unsuffixed names derived from lib.sh, which owns the convention
# (worker_name/proxy_name replicate the WORKER_SUFFIX of the Makefile): with no
# argument they return exactly the names that the Makefile builds with an empty N.
bare_worker_name="$(worker_name)"
bare_proxy_name="$(proxy_name)"
# shellcheck disable=SC2086 # intentional word-split of RUN_AS_WORKER
if ! host_containers="$($RUN_AS_WORKER podman ps -a --format '{{.Names}}')"; then
    warn "Could not list the host containers ('podman ps -a' as $WORKER_USER):
    cannot verify whether surplus workers were left outside the env.N files of $CONFIG_DIR."
else
    while IFS= read -r cname; do
        case "$cname" in
            # Suffixed. The pattern and the trimming are literal on purpose: they
            # have to match character by character so it can be checked at a
            # glance. The prefix equals that of worker_name/proxy_name in lib.sh,
            # which own the convention; if it changes there, it changes here.
            orcaster-worker-[0-9]*) kind=worker; n="${cname#orcaster-worker-}" ;;
            orcaster-proxy-[0-9]*) kind=proxy; n="${cname#orcaster-proxy-}" ;;
            # WITHOUT suffix: its own branch of the `case`, with `continue`, with
            # no digit filter and no comparison against WORKER_COUNT. It is not a
            # shortcut.
            # First, because the index here is the empty string and the numeric
            # path does not support it: the `''|*[!0-9]*` filter would drop it
            # silently and `[ "" -gt "$WORKER_COUNT" ]` would give a `test`
            # operand error.
            # And second, because the comparison would not be needed even if it
            # worked: on a host that run-fleet owns, the unsuffixed container
            # ALWAYS is surplus, since this script only stamps env.N with N>=1
            # (it iterates `seq 1 "$WORKER_COUNT"`) and only starts suffixed
            # containers, so there is no WORKER_COUNT that could legitimise it.
            "$bare_worker_name") bare_worker_orphan=1; continue ;;
            "$bare_proxy_name") bare_proxy_orphan=1; continue ;;
            *) continue ;;
        esac
        case "$n" in ''|*[!0-9]*) continue ;; esac
        # N > WORKER_COUNT: orphan by index, the usual path (unchanged).
        if [ "$n" -gt "$WORKER_COUNT" ]; then
            if [ "$kind" = worker ]; then
                orphans="$orphans $n"
            else
                proxy_orphans="$proxy_orphans $n"
            fi
            continue
        fi
        # N <= WORKER_COUNT: IN-FLEET. A worker here is healthy and is not
        # reported. A PROXY here is only reported under the opt-out: with
        # isolation active it is the legitimate sidecar of THAT worker; with
        # EGRESS_ISOLATION=0 it survives from a previous isolated run and is
        # obsolete by config. The comparison above reads base 10 with `[ ]` (not
        # `[[ ]]`/`(( ))`, which blow up with "value too great for base"), so a
        # orcaster-proxy-08 with WORKER_COUNT>=8 lands correctly on this side;
        # the suffix is reported RAW (same criterion as proxy_orphans: it names
        # the container that really exists on the host).
        if [ "$kind" = proxy ] && [ "$EGRESS_ISOLATION" = "0" ]; then
            stale_proxies="$stale_proxies $n"
        fi
    done <<< "$host_containers"
fi
if [ -n "$orphans" ]; then
    warn "Surplus worker(s) the prune did NOT see:$orphans.
    This rerun started $WORKER_COUNT worker(s) and those indexes have no env.N in
    $CONFIG_DIR, so their container and their systemd unit are still alive taking
    tasks (after a migration, on the previous code). Stop them by hand, in this
    order (the unit first, or Restart=always relaunches what the down stops):
        systemctl --user disable --now container-orcaster-worker-<n>.service
        make -C $INSTALL_DIR down client N=<n>
    both as $WORKER_USER."
fi
if [ -n "$proxy_orphans" ]; then
    warn "Surplus egress sidecar(s) the prune did NOT see:$proxy_orphans.
    This rerun started $WORKER_COUNT worker(s) and those indexes have no env.N in
    $CONFIG_DIR, so the proxy container and its systemd unit are still alive
    occupying the egress networks (after a migration or a release swap, with its
    bind mount of client/src/orcaster_worker over an already replaced inode).
    Stop them by hand, in this order (the unit first, or Restart=always
    relaunches what the down stops):
        systemctl --user disable --now container-orcaster-proxy-<n>.service
        make -C $INSTALL_DIR down client N=<n>
    both as $WORKER_USER."
fi
if [ -n "$stale_proxies" ]; then
    # CONCRETE recipe per N (not a template with <n>): each block names the real
    # container and volume, ready to paste. The unit first, or Restart
    # relaunches what you remove. The `podman volume rm` is OPTIONAL: it deletes
    # the egress per-task allowlist, which is no longer used without isolation,
    # but it is not needed to stop the sidecar. proxy_unit/proxy_name derive the
    # names from lib.sh (owner of the convention); the volume follows the
    # Makefile's EGRESS_VOL (orcaster-egress-vol$(WORKER_SUFFIX)).
    stale_recipe=""
    for sp in $stale_proxies; do
        stale_recipe="$stale_recipe
        systemctl --user disable --now $(proxy_unit "$sp")
        podman rm -f $(proxy_name "$sp")
        podman volume rm orcaster-egress-vol-$sp   # optional"
    done
    warn "Egress sidecar(s) obsolete by config (EGRESS_ISOLATION=0):$stale_proxies.
    This host declares EGRESS_ISOLATION=0, but those orcaster-proxy-N sidecars
    (N <= WORKER_COUNT) are still alive from a previous run with isolation
    active (EGRESS_ISOLATION=1). run-fleet does NOT touch them ('warn, do not
    prune' policy) and 06-autostart already stopped re-enabling their unit in
    this rerun; the final removal is up to you. For each N, in this order:$stale_recipe
    all as $WORKER_USER."
fi
# The two notices below carry a DIFFERENT remedy from the suffixed ones, and it
# is not a matter of form: the `down` goes WITHOUT N=. The Makefile defines an
# empty `N ?=` and `WORKER_SUFFIX := $(if $(strip $(N)),-$(strip $(N)),)`, so
# omitting N is precisely what points the recipe at the unsuffixed container;
# passing any N= would send it to stop another one. The unit name comes from
# worker_unit / proxy_unit with no argument (lib.sh), not from a string rebuilt
# here.
if [ "$bare_worker_orphan" = 1 ]; then
    warn "Unsuffixed worker ($bare_worker_name) the prune did NOT see.
    This host was converged to the fleet layout (README of setup-scripts/
    debian13-worker): this rerun stamped $CONFIG_DIR/env.1 and created the
    suffixed worker, but the unsuffixed container from the manual bootstrap is
    still alive with its systemd unit enabled. They are two workers asking the
    same server for tasks, and after the next release swap the unsuffixed one
    runs over already replaced inodes. Stop it by hand, in this order (the unit
    first, or Restart=always relaunches what the down stops):
        systemctl --user disable --now $(worker_unit)
        make -C $INSTALL_DIR down client
    both as $WORKER_USER. The 'down' goes without N= on purpose: that is how
    the Makefile names the unsuffixed container."
fi
if [ "$bare_proxy_orphan" = 1 ]; then
    warn "Unsuffixed egress sidecar ($bare_proxy_name) the prune did NOT see.
    It is the proxy of the unsuffixed worker from the manual bootstrap, which
    was left out when converging this host to the fleet layout (README of
    setup-scripts/debian13-worker). Its systemd unit is still enabled, so it
    keeps occupying the egress networks and, after a release swap, with its
    bind mount of client/src/orcaster_worker over an already replaced inode.
    Stop it by hand, in this order (the unit first, or Restart=always
    relaunches what the down stops):
        systemctl --user disable --now $(proxy_unit)
        make -C $INSTALL_DIR down client
    both as $WORKER_USER. The 'down' goes without N= on purpose: that is how
    the Makefile names the unsuffixed container."
fi

printf "\n=== 1/2 · Client deployment: %s container(s) started ===\n" "$WORKER_COUNT"

# --- Step 2/2: enrollment verification ----------------------------------------
# "Started" is not "enrolled": with an expired or exhausted token the container
# ends up in a crash loop and the deployment would look successful anyway. The
# deployment is only complete when every worker has credentials from the server
# (that is, it already appears in the dashboard). ENROLL_WAIT/ENROLL_POLL are
# adjustable via setup.env.
: "${ENROLL_WAIT:=120}"
: "${ENROLL_POLL:=5}"
elapsed=0
while true; do
    pending=""
    for i in $(seq 1 "$WORKER_COUNT"); do
        # shellcheck disable=SC2086 # intentional word-split of ENROLL_CHECK
        $ENROLL_CHECK "$i" || pending="$pending $i"
    done
    [ -z "$pending" ] && break
    if [ "$elapsed" -ge "$ENROLL_WAIT" ]; then
        die "Enrollment timeout (${ENROLL_WAIT}s): worker(s) not enrolled:$pending.
    Check the token (expired or max_uses exhausted?) and the container logs:
    podman logs orcaster-worker-<N> (as the worker user)."
    fi
    sleep "$ENROLL_POLL"
    elapsed=$((elapsed + ENROLL_POLL))
done

printf "\n=== 2/2 · Fleet enrolled: %s worker(s) visible on the server ===\n" "$WORKER_COUNT"
