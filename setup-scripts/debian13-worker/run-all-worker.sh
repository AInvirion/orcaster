#!/usr/bin/env bash
# run-all-worker — Orchestrates the full WORKER provisioning on Debian 13 by
# running the chain 00 → 02 → 03 → 04 → 05 in order (01 used to fetch the code
# with git and was dropped with the tarball deployment: the code is already on
# the host when this runs, pushed with ops/release/push-release.sh). Idempotent:
# safe to re-run. Leaves the host ready except for ORCH_ENROLLMENT_TOKEN (the
# operator places it) and does NOT start the container.
#
# Usage:
#   sudo bash run-all-worker.sh           # everything (preflight → smoke test)
#   sudo bash run-all-worker.sh --from 03 # resume from script 03
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_root "$@"

FROM=0
while [ $# -gt 0 ]; do
    case "$1" in
        # The `${2:-0}` it had before was dead code and also silent: with a
        # valueless `--from`, this `shift` leaves $#=0, the closing `shift` of
        # the while fails, and `set -e` kills the script with exit 1 and NO
        # output (the operator is left believing the suite ran and failed at
        # some step). Same idiom as ops/release/{make,push,install}-release.sh.
        --from) FROM="${2:?--from needs a value (e.g. --from 03)}"; shift ;;
        -h|--help)
            sed -n '2,11p' "$0"; exit 0 ;;
        *) die "Unknown argument: $1 (use --help)" ;;
    esac
    shift
done

# Validate configuration before touching the system.
load_env
printf '\n%b=== Worker provisioning orcaster · Debian 13 (rootless) ===%b\n' "$_C_BLUE" "$_C_RST"
echo   "    orchestrator: $ORCH_BASE_URL"
echo   "    install:      $INSTALL_DIR"
echo   "    user:         $WORKER_USER (home $WORKER_HOME)"
# Identity of the installed code. Replaces the `repo ref:` line, which printed
# the checkout's ref: with no git on the host, the tarball's RELEASE is the only
# signal of WHICH code is being provisioned, and leaving the log without any
# would lose the identity, not just the comparison. Same format as
# install-release.sh:613-614 (header + indented manifest) so the three
# deployment scripts speak the same way.
# It is read once, and as root only through read_release_file: INSTALL_DIR
# belongs to the worker user.
release_text="$(read_release_file "$INSTALL_DIR" "$WORKER_USER")" || release_text=""
if release_commit_of "$release_text" >/dev/null; then
    echo   "    release:"
    printf '%s\n' "$release_text" | sed 's/^/        /'
else
    warn "No readable $INSTALL_DIR/RELEASE: cannot declare which code is being provisioned (did the code not arrive through ops/release/push-release.sh?)."
fi

# Ordered list: number → script. 01 (code fetch with git) no longer exists: the
# operator pushes the code, and the numbering is kept so as not to break the
# `--from NN` that the operator and cloud-init-worker.sh already use.
SCRIPTS=(
    "00:00-preflight.sh"
    "02:02-server-pubkey.sh"
    "03:03-worker-env.sh"
    "04:04-build.sh"
    "05:05-smoke-test.sh"
)

for entry in "${SCRIPTS[@]}"; do
    num="${entry%%:*}"; script="${entry#*:}"
    # shellcheck disable=SC2086
    [ "$((10#$num))" -lt "$((10#$FROM))" ] && continue
    bash "$SCRIPT_DIR/$script"
done

printf '\n%b=== Provisioning complete (token still missing) ===%b\n' "$_C_GREEN" "$_C_RST"
echo "Place ORCH_ENROLLMENT_TOKEN in $CLIENT_ENV and start:"
echo "    cd /   # sudo inherits the cwd: from /root (700) the subcommand does not start"
echo "    sudo -u $WORKER_USER env XDG_RUNTIME_DIR=/run/user/\$(id -u $WORKER_USER) \\"
echo "         make -C $INSTALL_DIR up client$MAKE_CFG"
echo "Then enable autostart at boot:"
echo "    sudo bash $SCRIPT_DIR/06-autostart.sh"
