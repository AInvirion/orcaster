#!/usr/bin/env bash
# 00 — Preflight: validates the environment, updates apt, installs the packages
# for rootless Podman and creates the dedicated user that will run the worker.
# Idempotent.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"
require_debian13
load_env

step "00 · Preflight and base system (rootless worker)"

# fs.protected_hardlinks=1 (the Debian default) is a precondition for root's
# `chown -R` over trees controlled by the service user: chown -R does not follow
# symlinks, but it DOES change the owner of a hardlink's target. With the sysctl
# at 0, that user can hardlink a root-owned file it can read and get root to give
# it away. Fails closed: if the value is not exactly 1 (or cannot be read), it
# stops before touching anything. `sysctl` is resolved through PATH.
require_protected_hardlinks() {
    local val
    val="$(sysctl -n fs.protected_hardlinks 2>/dev/null)" ||
        die "Could not read the sysctl fs.protected_hardlinks (failed: sysctl -n fs.protected_hardlinks). This installer runs chown -R as root over $INSTALL_DIR, which $WORKER_USER controls, and that is only safe with fs.protected_hardlinks=1. Check it with: sysctl fs.protected_hardlinks"
    [ "$val" = "1" ] ||
        die "fs.protected_hardlinks=$val (1 is required). This installer runs chown -R as root over $INSTALL_DIR, which $WORKER_USER controls: with the sysctl set to anything other than 1, that user can hardlink a root-owned file and get chown -R to change its owner. Fix it with: sysctl -w fs.protected_hardlinks=1, persist it in /etc/sysctl.d/ (e.g. echo 'fs.protected_hardlinks = 1' > /etc/sysctl.d/60-protected-links.conf) and retry this same step."
}
require_protected_hardlinks

log "Updating apt indexes and the base system…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

log "Installing packages (rootless Podman + deployment toolchain)…"
# podman/uidmap/slirp4netns/fuse-overlayfs → rootless container runtime.
# passt → provides 'pasta', the network backend rootless Podman 5.4.2 uses by
#         default; without it 'make up client' fails with "could not find pasta".
# dbus-user-session → enables systemd --user (linger, unit autostart).
# make → drives build/run through the repo Makefile (the code arrives by
#         operator push, see ops/release/).
# curl/openssl/ca-certificates → smoke test and pubkey validation.
# aardvark-dns → DNS for named podman networks; apt_install uses
#         --no-install-recommends and on Debian it is only a Recommends of
#         netavark, so it is left out unless listed explicitly. It is a
#         precondition of EGRESS_ISOLATION=1: without it the worker cannot
#         resolve the orcaster-proxy sidecar on the --internal network.
# nftables → provides the 'nft' binary that netavark invokes to build the
#         rules of the egress sidecar's --internal podman network. Without it,
#         EGRESS_ISOLATION=1 fails with "netavark: nftables error: unable to
#         execute nft: No such file or directory" (same kind of gap as
#         aardvark-dns above).
apt_install ca-certificates curl make openssl \
    podman uidmap slirp4netns passt fuse-overlayfs dbus-user-session \
    aardvark-dns nftables

# Dedicated regular user (NOT --system): systemd --user and rootless lingering
# work better with a normal user that has its own home.
if id "$WORKER_USER" >/dev/null 2>&1; then
    ok "User '$WORKER_USER' already exists."
else
    log "Creating user '$WORKER_USER' (home=$WORKER_HOME)…"
    useradd --create-home --home-dir "$WORKER_HOME" --shell /bin/bash "$WORKER_USER"
fi

# subuid/subgid ranges for user mapping inside the rootless container.
# useradd does not always assign them; they are guaranteed idempotently here.
if grep -q "^$WORKER_USER:" /etc/subuid 2>/dev/null; then
    ok "subuid already configured for '$WORKER_USER'."
else
    log "Assigning subuid to '$WORKER_USER'…"
    usermod --add-subuids 100000-165535 "$WORKER_USER"
fi
if grep -q "^$WORKER_USER:" /etc/subgid 2>/dev/null; then
    ok "subgid already configured for '$WORKER_USER'."
else
    log "Assigning subgid to '$WORKER_USER'…"
    usermod --add-subgids 100000-165535 "$WORKER_USER"
fi

# Reconcile rootless storage after subuid/subgid changes. Best-effort.
as_worker podman system migrate >/dev/null 2>&1 \
    || warn "podman system migrate failed (may be harmless on first boot)."

# Lingering: keeps the user manager alive without an interactive session, so
# the container (and its restart policy) survives logout and reboot.
loginctl enable-linger "$WORKER_USER"

# Wait for the user manager to have its runtime dir (created by linger).
uid="$(id -u "$WORKER_USER")"
log "Waiting for /run/user/$uid (user manager)…"
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -d "/run/user/$uid" ] && break
    sleep 1
done
if [ -d "/run/user/$uid" ]; then
    ok "User runtime available."
else
    warn "/run/user/$uid did not appear; systemctl --user may need a retry."
fi

install -d -o "$WORKER_USER" -g "$WORKER_USER" "$INSTALL_DIR"

# Converge ownership of the installed code tree. INSTALL_DIR belongs to the
# worker user because the container mounts it; root no longer executes anything
# from there (the suite runs from the root-owned copy that install-release.sh
# leaves, see the origin guard in lib.sh), so leaving it to $WORKER_USER does not
# give that user code that root will run. chown -R without -L/-H does not follow
# symlinks inside: it changes the link, not its target. The install -d above
# only adjusts the root directory, not what is inside. In the README's Bootstrap
# flow, install-release.sh --no-restart runs BEFORE this script creates
# $WORKER_USER (see the useradd block above), so its own chown -R is explicitly
# skipped and leaves the tree root:root; without this line, which is the only
# one that always runs AFTER the user exists, those hosts would stay root:root
# forever.
#
# Fails hard (die), not warn: at this point $WORKER_USER certainly EXISTS, so a
# chown -R that fails here is a filesystem anomaly (permissions, full disk), not
# an expected state like the one install-release.sh handles (which does
# distinguish a missing user); letting it pass silently under --no-restart
# leaves a container that cannot read its own code.
#
# Depends on fs.protected_hardlinks=1: chown -R does not follow symlinks but it
# does change the owner of a hardlink's target, and $WORKER_USER controls the
# tree. require_protected_hardlinks (above) already required the sysctl to be 1.
#
# Cost/idempotence: runs on every pass of run-all-worker.sh/run-fleet.sh over a
# tree of thousands of small files (a code checkout, not user data), not
# gigabytes; over the empty directory that the install -d above leaves on a new
# host it is practically instantaneous.
chown -R "$WORKER_USER:$WORKER_USER" "$INSTALL_DIR" ||
    die "chown -R $WORKER_USER:$WORKER_USER $INSTALL_DIR failed. The code tree in $INSTALL_DIR may have been left owned by someone other than $WORKER_USER, the user that runs the worker."

# Host configuration directory. Outside the checkout, because the tarball
# deployment replaces all of $INSTALL_DIR on every release. 0750 root:orcaster:
# the worker runs as orcaster and rootless podman reads the --env-file as that
# user, so it needs to traverse the directory.
install -d -m 0750 -o root -g "$WORKER_USER" "$CONFIG_DIR"

# Reconcile ownership and mode of setup.env if it already exists. Safety net
# for hosts provisioned by earlier versions: cloud-init-worker.sh's chown/chmod
# used to be best-effort (`|| true`) and could leave the file root:root 0600,
# unreadable by the worker. Today cloud-init aborts if it cannot leave it 0640
# root:orcaster, so on a new host this is a no-op; it stays here because it is
# the only point that always runs AFTER the user is created.
if [ -f "$CONFIG_DIR/setup.env" ]; then
    chown root:"$WORKER_USER" "$CONFIG_DIR/setup.env"
    chmod 0640 "$CONFIG_DIR/setup.env"
fi

ok "Preflight complete."
