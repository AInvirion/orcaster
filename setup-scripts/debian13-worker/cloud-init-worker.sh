#!/usr/bin/env bash
# cloud-init-worker: non-interactive entry point for the "user-data" field of
# DigitalOcean / Vultr / any VPS. Provisions and starts N workers zero-touch,
# reusing the debian13-worker suite. Idempotent. It is a golden-snapshot
# script: it REQUIRES the host to already have the code installed (the RELEASE
# file in INSTALL_DIR) and the worker image baked in.
#
# It runs from the root-owned tree that install-release.sh leaves behind (by
# default /root/orcaster), never from INSTALL_DIR: the user-data does
#   exec bash /root/orcaster/setup-scripts/debian13-worker/cloud-init-worker.sh
# and the suite (lib.sh, run-all-worker.sh, run-fleet.sh) comes from the same
# directory as this script. INSTALL_DIR only supplies the RELEASE and the code
# that the containers mount. Deployment is by tarball, so this script does not
# clone or download anything: on a bare host it aborts and asks the operator
# to push a release with ops/release/push-release.sh.
#
# Config: template the CONFIG block below per fleet before creating the VPS.
set -euo pipefail

# ── CONFIG (template per fleet) ──────────────────────────────────────────────
# Be careful with secrets templated here: the user-data stays stored in the
# provider metadata until the VPS is destroyed, so everything in this block
# outlives the deployment (known limitation; see the snapshot migration). Today
# that covers ORCH_ENROLLMENT_TOKEN and any runtime secret that travels in
# EXTRA_ENV (e.g. API_TOKEN). There is no clone PAT in the list anymore:
# tarball deployment removed the need for one.
: "${ORCH_BASE_URL:=https://orcaster.example.com}"
: "${ORCH_ENROLLMENT_TOKEN:=}"        # MULTI-USE token, the same for the whole fleet
: "${SERVER_ED25519_PUBKEY:=}"         # PEM block (PUBLIC key)
: "${WORKER_COUNT:=1}"
: "${EGRESS_ISOLATION:=1}"             # egress isolation (authoritative default; 0 = opt-out)
: "${WORKER_MEMORY:=}"                 # memory cap per container (podman --memory, e.g. 28g); empty = no cap
: "${WORKER_RO_MOUNTS:=}"              # host dirs mounted :ro at the same path (e.g. "/opt/example-ro"); empty = none
: "${WORKER_NAME_PREFIX:=}"            # empty => VPS hostname
: "${EXTRA_ENV:=}"                     # multiline KEY=VALUE -> every .env.i
: "${ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION:=}"   # disk gate floor; empty = worker default
: "${ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS:=}"    # gate fail-open deadline; empty = worker default (1800s)
: "${ORCH_ENFORCE_ISOLATION:=}"        # strict isolation of the task environment: true|false; empty = resolved by stamp_worker_env (new host => true)
: "${ISOLATION_REQUIRED_ENV:=}"        # variable names that require strict isolation, comma- or space-separated; empty = none
: "${INSTALL_DIR:=/opt/orcaster}"   # code installed by push-release.sh
: "${CONFIG_DIR:=/etc/orcaster}"       # host config, outside the checkout
# The export is mandatory: this script starts two children, run-all-worker.sh
# --from 02 and run-fleet.sh (step 4 below), which resolve $CONFIG_DIR from
# their own environment (lib.sh:19 defaults it to /etc/orcaster if they do not
# see it). Without the export, an operator who templates
# CONFIG_DIR=/srv/orcaster above would still see those children look in
# /etc/orcaster and die in load_env asking for the migration runbook on a host
# that was never migrated. INSTALL_DIR does not need this export because it
# travels INSIDE setup.env (step 2 below): the children read it from there.
# CONFIG_DIR cannot travel that way because it is the path TO setup.env, not
# a value inside it.
export CONFIG_DIR
: "${CLOUD_INIT_LOG:=/var/log/orcaster-cloud-init.log}"
# ─────────────────────────────────────────────────────────────────────────────

# Source guard before any side effect: when sourced as root, lib.sh aborts if
# this tree is not root-owned or if the script was invoked through a path with
# links (see require_root_owned_source). It goes after the CONFIG block, which
# only assigns variables, because lib.sh defaults CONFIG_DIR on its own and a
# CONFIG_DIR templated above has to win. It goes before the tee below, even
# though that leaves a guard abort outside $CLOUD_INIT_LOG: the tee creates and
# writes that file, which is exactly the kind of effect that cannot happen
# before the guard. The abort still lands in the output that cloud-init keeps
# on its own (/var/log/cloud-init-output.log).
# shellcheck source=lib.sh disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
SUITE_DIR="$SCRIPT_DIR"
# This script does not call load_env (its INSTALL_DIR comes from the CONFIG
# block), so the other half of the guard goes here: the tree it runs from and
# INSTALL_DIR must be disjoint.
require_source_outside_install_dir

# The log is 0600: it holds the entire provisioning output. The umask is inside
# the process substitution, so it only reaches the tee that creates the file
# and not the child scripts. A log from an earlier run (created 0644 by
# previous versions) is adjusted before more is written to it.
if [ -e "$CLOUD_INIT_LOG" ] && ! chmod 0600 "$CLOUD_INIT_LOG"; then
    warn "Could not set $CLOUD_INIT_LOG to 0600; it keeps its current mode."
fi
exec > >(umask 077; exec tee -a "$CLOUD_INIT_LOG") 2>&1
echo "=== cloud-init-worker $(date -u +%FT%TZ 2>/dev/null || echo '') ==="

# The script's only network operation is the preflight curl, which uses
# retry() from lib.sh (RETRY_MAX/RETRY_DELAY contract).

[ "$(id -u)" -eq 0 ] || { echo "ERROR: must run as root (user-data does)."; exit 1; }
[ -n "$ORCH_ENROLLMENT_TOKEN" ] || { echo "ERROR: ORCH_ENROLLMENT_TOKEN is empty in the CONFIG."; exit 1; }
case "$ORCH_BASE_URL" in
    ''|https://orcaster.example.com)
        echo "ERROR: ORCH_BASE_URL not templated in the CONFIG (the example placeholder is still there)."
        exit 1 ;;
esac
# Only 0|1: the Makefile enables the topology with an EXACT match on '1'
# (ifeq), so any other "truthy" value ('true', 'yes', '2') would silently
# disable isolation, which is exactly what an authoritative default must
# prevent. Fail fast here, before provisioning, instead of letting load_env
# abort halfway through the deployment (load_env only runs in the child
# scripts).
case "$EGRESS_ISOLATION" in
    0|1) ;;
    *)
        echo "ERROR: EGRESS_ISOLATION must be 0 or 1 (got: '$EGRESS_ISOLATION')."
        exit 1 ;;
esac
# Same fail-fast as load_env (which only runs in the child scripts): a
# malformed cap must not reach the fleet's podman run. The suffix is
# mandatory: a bare '28' is almost certainly a typo for '28g' (28 bytes is
# below podman's minimum and the container is not even created). Zero ('0g')
# is rejected for the same minimum.
if [ -n "$WORKER_MEMORY" ] && ! [[ "$WORKER_MEMORY" =~ ^0*[1-9][0-9]*[bkmgBKMG]$ ]]; then
    echo "ERROR: WORKER_MEMORY must be a number greater than zero with a b/k/m/g suffix, e.g. 28g (got: '$WORKER_MEMORY')."
    exit 1
fi
# Same fail-fast as load_env, for the same reason: a malformed value stamped
# into /etc/orcaster/env.N would make Settings() blow up when the container
# starts (crash loop), and the 2/2 check below does not catch it (it only
# checks for the presence of state.json, which persists in the volume from a
# previous enrollment). Empty = worker default.
if [ -n "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" ] && \
   ! [[ "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION" =~ ^(0(\.[0-9]+)?|1(\.0*)?|\.[0-9]+)$ ]]; then
    echo "ERROR: ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION must be a number between 0.0 and 1.0 (got: '$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION')."
    exit 1
fi
if [ -n "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" ] && \
   ! [[ "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    echo "ERROR: ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS must be a number >= 0 (got: '$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS')."
    exit 1
fi

# Same fail-fast as load_env: only true|false. Any other value written to
# setup.env would make load_env die in every child script, after provisioning
# has already started.
case "$ORCH_ENFORCE_ISOLATION" in
    ''|true|false) ;;
    *)
        echo "ERROR: ORCH_ENFORCE_ISOLATION must be true or false (got: '$ORCH_ENFORCE_ISOLATION')."
        exit 1 ;;
esac

# The script's cwd has to be traversable by orcaster: `sudo` inherits the
# caller's cwd, so a manual rerun over SSH from /root (mode 700) kills with
# "cannot chdir" every subcommand that does not change directory on its own.
# Here that is the `podman image exists` that decides the golden-snapshot fast
# path, which also fails silently (`2>/dev/null`) and degrades to "bootstrap"
# on a healthy snapshot. as_worker has its own guard, but the call site below
# cannot use it (load_env has not run yet). Under cloud-init the cwd is already
# `/`: this only aligns the manual rerun with the environment tested at boot.
# All the script's paths are absolute ($INSTALL_DIR / $SUITE_DIR), so the
# change affects nothing else (SUITE_DIR is already absolute: it comes from
# pwd -P in lib.sh).
cd /

# --- 1. Check for installed code ------------------------------------------
# The decision uses should_bootstrap (lib.sh, the same implementation the
# tests cover) on INSTALL_DIR: the RELEASE and the worker image must both be
# present, even though the suite runs from the root tree.
need_bootstrap=1
# RELEASE contents, read once and as orcaster (read_release_file): INSTALL_DIR
# belongs to that user, so root does not read it directly. The same text is
# used to decide and to print the release identity.
release_text=""
if id orcaster >/dev/null 2>&1; then
    uid_orcaster="$(id -u orcaster)"

    # Race between cloud-init and the lingering user session: cloud-init runs
    # early in the boot, and /run/user/<uid> is created by the user manager
    # that systemd-logind starts because of lingering (enabled by
    # 00-preflight.sh in the reference snapshot); there is no guaranteed order
    # between the two when an instance cloned from the golden snapshot boots.
    # If it does not exist yet, the rootless podman check below fails,
    # should_bootstrap returns "bootstrap" as a false negative, and the script
    # ends up aborting for a missing release on a snapshot that is actually
    # fine. We allow a few seconds of margin before deciding; if the deadline
    # runs out, we carry on with the normal detection (the abort fallback
    # already carries a clear message for that case).
    : "${RUN_USER_BASE:=/run/user}"
    : "${RUNTIME_DIR_WAIT:=30}"
    for _ in $(seq 1 "$RUNTIME_DIR_WAIT"); do
        [ -d "$RUN_USER_BASE/$uid_orcaster" ] && break
        sleep 1
    done

    # -H sets HOME to orcaster's home without depending on the sudoers
    # env_reset (with env_keep += HOME, rootless podman would inherit root's
    # HOME, look at the wrong storage, and a good snapshot would give a false
    # "bootstrap"). It does not use as_worker: load_env has not run yet here.
    # should_bootstrap already requires the RELEASE of the installed release,
    # so "skip" implies the code is present: there is no need to re-check it
    # here. There is also no code identity check: the user-data CONFIG declares
    # no ref, because the operator decides which code runs on the host when
    # pushing the release (install-release.sh verifies the RELEASE it
    # installs).
    release_text="$(read_release_file "$INSTALL_DIR" orcaster)" || release_text=""
    if [ "$(should_bootstrap "$release_text" \
            sudo -u orcaster -H env XDG_RUNTIME_DIR="$RUN_USER_BASE/$uid_orcaster" \
            podman image exists "$WORKER_IMAGE")" = "skip" ]; then
        need_bootstrap=0
        echo ">> Installed code (RELEASE) and image present: continuing with provisioning."
        # Identity of what is about to start. On a VPS created from a golden
        # snapshot this is the ONLY moment when anyone can see which code is
        # going to enroll the fleet: cloud-init runs on first boot and ends
        # with the workers already working, long before the operator pushes
        # anything. Format of install-release.sh :613-614 (header + indented
        # manifest).
        printf '%s\n' "$release_text" | sed 's/^/   /'
    fi
fi

if [ "$need_bootstrap" -eq 1 ]; then
    echo "ERROR: this host has no installed code ($INSTALL_DIR/RELEASE is missing)"
    echo "  or the worker image is not built."
    echo "  The worker code is no longer cloned: it arrives by operator push."
    echo "  From the operator machine:"
    echo "      ops/release/push-release.sh --hosts <this-host>"
    echo "  If the VPS was created from a golden snapshot, the image was not"
    echo "  baked in: rebake the reference snapshot."
    # This block also triggers when the code is ALREADY pushed and what is
    # missing is provisioning (the worker image, or the orcaster user): asking
    # that operator to push again does not get them out of the hole.
    echo "  If the code is already installed and what is missing is provisioning"
    echo "  the host, run the full suite here:"
    echo "      sudo bash $SUITE_DIR/run-all-worker.sh"
    exit 1
fi

# WORKER_RO_MOUNTS syntax, before writing setup.env and before touching any
# container. It cannot go next to WORKER_MEMORY, above: the rule lives in
# check-ro-mounts.sh (the same one used by load_env, run-fleet.sh, the Makefile
# and the install-release.sh gate), and before this point it is not known
# whether the code is installed. The host state (it exists, orcaster can
# traverse it, the container uid can read it via "other") is measured by
# run-fleet.sh as the worker user before its loop.
if [ -n "$WORKER_RO_MOUNTS" ] && ! bash "$RO_MOUNTS_CHECK" "$WORKER_RO_MOUNTS"; then
    echo "ERROR: WORKER_RO_MOUNTS invalid in the CONFIG (reason above). Nothing on the host was touched."
    exit 1
fi

# ISOLATION_REQUIRED_ENV grammar, before writing setup.env: the rule lives in
# lib.sh (normalize_isolation_required_env, the same one load_env applies), so
# it can only be checked with the installed code.
if ! ISOLATION_REQUIRED_ENV="$(normalize_isolation_required_env "$ISOLATION_REQUIRED_ENV")"; then
    echo "ERROR: ISOLATION_REQUIRED_ENV invalid in the CONFIG (reason above). Nothing on the host was touched."
    exit 1
fi

# --- 2. Materialize setup.env from the CONFIG -------------------------------
# env_quote (lib.sh): values in escaped single quotes => when load_env
# re-sources setup.env it does not expand $/`/" or execute anything (EXTRA_ENV
# and the PEM are arbitrary operator content) and newlines are preserved.
#
# The directory has to exist before the redirect, with its final permissions:
# 0750 root:orcaster, because the worker runs as orcaster and rootless podman
# reads the --env-file as that user, so it needs to traverse it.
#
# No fallback. The `|| install -d -m 0750 "$CONFIG_DIR"` that used to be here
# kept the mode but dropped -o/-g, which left the directory root:root 0750:
# exactly the state the worker CANNOT traverse. It silently manufactured the
# failure it claimed to avoid. The orcaster group always exists at this point
# (getting here requires need_bootstrap=0, and that detection requires
# `id orcaster`), so a real failure of this install is a host problem and has
# to be visible.
install -d -m 0750 -o root -g orcaster "$CONFIG_DIR" || {
    echo "ERROR: could not create $CONFIG_DIR as 0750 root:orcaster."
    echo "  The orcaster group exists (the detection above requires it), so"
    echo "  this points to the filesystem or to a pre-existing $CONFIG_DIR with a"
    echo "  foreign owner. Fix it on the host and rerun cloud-init."
    exit 1
}
umask 077
{
    echo "ORCH_BASE_URL=$(env_quote "$ORCH_BASE_URL")"
    echo "ORCH_ENROLLMENT_TOKEN=$(env_quote "$ORCH_ENROLLMENT_TOKEN")"
    echo "SERVER_ED25519_PUBKEY=$(env_quote "$SERVER_ED25519_PUBKEY")"
    echo "INSTALL_DIR=$(env_quote "$INSTALL_DIR")"
    echo "WORKER_COUNT=$(env_quote "$WORKER_COUNT")"
    echo "EGRESS_ISOLATION=$(env_quote "$EGRESS_ISOLATION")"
    echo "WORKER_MEMORY=$(env_quote "$WORKER_MEMORY")"
    echo "WORKER_RO_MOUNTS=$(env_quote "$WORKER_RO_MOUNTS")"
    echo "WORKER_NAME_PREFIX=$(env_quote "$WORKER_NAME_PREFIX")"
    echo "EXTRA_ENV=$(env_quote "$EXTRA_ENV")"
    echo "ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=$(env_quote "$ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION")"
    echo "ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS=$(env_quote "$ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS")"
    # Isolation: only if the CONFIG declares them. Without the line,
    # stamp_worker_env resolves the flag on its own (new installation =>
    # true), and an empty line would add nothing.
    if [ -n "$ORCH_ENFORCE_ISOLATION" ]; then
        echo "ORCH_ENFORCE_ISOLATION=$(env_quote "$ORCH_ENFORCE_ISOLATION")"
    fi
    if [ -n "$ISOLATION_REQUIRED_ENV" ]; then
        echo "ISOLATION_REQUIRED_ENV=$(env_quote "$ISOLATION_REQUIRED_ENV")"
    fi
} > "$CONFIG_DIR/setup.env"
# Fatal, not best-effort. The defense for the `|| true` that used to be here
# was that 00-preflight.sh reconciles ownership and mode on any full rerun. On
# THIS path it does not: step 4 below calls run-all-worker.sh --from 02, which
# skips exactly 00-preflight. With umask 077 the file is born 0600 root:root,
# so a chown/chmod that fails silently leaves setup.env unreadable for
# orcaster and the whole fleet dies in load_env, far from the cause.
if ! chown root:orcaster "$CONFIG_DIR/setup.env" \
    || ! chmod 0640 "$CONFIG_DIR/setup.env"; then
    echo "ERROR: could not set $CONFIG_DIR/setup.env to 0640 root:orcaster."
    echo "  The worker runs as orcaster and reads it through the group: without"
    echo "  this none of them starts. Fix it on the host and rerun cloud-init."
    exit 1
fi

# --- 3. Connectivity preflight --------------------------------------------
if ! retry curl -fsS --max-time 15 "${ORCH_BASE_URL%/}/healthz" >/dev/null; then
    echo "ERROR: cannot reach ${ORCH_BASE_URL%/}/healthz. Check ORCH_BASE_URL and the network."
    exit 1
fi

# --- 4. Provisioning (02-05) + fleet start (zero-touch) ----------------
# No alternative branch: past the check above, need_bootstrap is 0 by
# construction (case 1 aborts). `--from 02` skips 00-preflight (apt and user),
# which has already run on a host with a baked image, but it STILL runs 02-05
# (pubkey/env/build/smoke-test): it does not skip the build. It is re-run
# anyway because it is idempotent and, with the image already present,
# 04-build.sh (make build client) resolves from podman's layer cache: in
# practice it is a quick no-op, not a real rebuild.
bash "$SUITE_DIR/run-all-worker.sh" --from 02

bash "$SUITE_DIR/run-fleet.sh"

# We only get here if run-fleet.sh confirmed step 2/2 (all workers have
# credentials from the server); an expired/exhausted token aborts earlier.
echo "=== cloud-init-worker COMPLETE (2/2): $WORKER_COUNT worker(s) deployed and enrolled ==="
