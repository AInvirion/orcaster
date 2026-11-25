#!/usr/bin/env bash
# 04 — Build of the worker image (rootless), through the repo Makefile.
# Equivalent to: podman build --os linux --network=host -t $WORKER_IMAGE client/
# (the tag lives in lib.sh::WORKER_IMAGE, aligned with the Makefile default).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "04 · Worker image build (rootless)"

log "Building the image as '$WORKER_USER'…"
as_worker make -C "$INSTALL_DIR" build client

as_worker podman image exists "$WORKER_IMAGE" \
    || die "Image $WORKER_IMAGE was not built."

ok "Image ready: $WORKER_IMAGE"
