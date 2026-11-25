#!/usr/bin/env bash
# Access to the `systemd --user` manager of the running user.
#
# Why it exists: a `sudo -iu orcaster` shell does not open a logind session, so
# it brings neither XDG_RUNTIME_DIR nor DBUS_SESSION_BUS_ADDRESS. Without them
# every `systemctl --user` fails to connect to the bus, with the same non-zero
# exit as an inactive unit. Read as "inactive", `make up client` skipped the
# stop, recreated the container and left the manager with the old unit loaded
# and a MainPID that no longer exists; the next `daemon-reload` that did reach
# the bus stopped the worker.
#
# Two uses:
#   - as a library (`source user-bus.sh`): 06-autostart.sh uses
#     user_bus_derive_env and user_bus_explain;
#   - as a command, from the Makefile recipes:
#       `bash user-bus.sh stop-unit <unit> <container>`: the step before the
#       `podman rm -f` of `up-client` and `up-proxy`;
#       `bash user-bus.sh check <unit> <container>`: only the probe, without
#       stop. `up-client` runs it as a prerequisite BEFORE `up-proxy`, so that
#       an unreachable manager stops things before the sidecar is recreated.
#
# ALWAYS runs as the owner of the --user unit (the worker user). It does not
# elevate or drop privileges: the path from root is as_worker (lib.sh), which
# already sets both variables.

# Base directory of the runtime dirs. Test seam, same name that
# cloud-init-worker.sh uses.
USER_BUS_RUN_BASE="${RUN_USER_BASE:-/run/user}"

# If missing, derives XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS with the
# as_worker convention (lib.sh): /run/user/<uid> and its `bus` socket. Only when
# that directory exists: logind creates it when it starts the user's manager
# (linger), so its absence means there is no manager to talk to. A variable
# that is already set is NOT touched (the release tooling and run-fleet set
# them).
user_bus_derive_env() {
    local dir
    USER_BUS_DERIVED=0
    if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
        dir="$USER_BUS_RUN_BASE/$(id -u)"
        if [ -d "$dir" ]; then
            export XDG_RUNTIME_DIR="$dir"
            USER_BUS_DERIVED=1
        fi
    fi
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -n "${XDG_RUNTIME_DIR:-}" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    fi
}

# Cause and remedy of an unreachable manager, to stderr. $1 = what systemctl
# said (may be empty). It distinguishes three cases, because the remedy is not
# the same: there is no runtime dir (linger is missing), the variables were
# derived by this script (the manager is down: repeating the same environment
# fixes nothing), or they came from the caller's environment (they may point
# elsewhere). Remedies with `sudo` are announced as such: the worker user
# normally does not have sudo.
user_bus_explain() {
    local detail="${1:-}" uid user dir
    uid="$(id -u)"
    user="$(id -un)"
    dir="$USER_BUS_RUN_BASE/$uid"
    if [ -n "$detail" ]; then
        echo "       systemctl replied: $detail" >&2
    fi
    echo "       Environment used: XDG_RUNTIME_DIR='${XDG_RUNTIME_DIR:-}' DBUS_SESSION_BUS_ADDRESS='${DBUS_SESSION_BUS_ADDRESS:-}'" >&2
    if [ ! -d "$dir" ]; then
        echo "       Cause: $dir does not exist, so there is no systemd --user manager of $user running" >&2
        echo "       and the variables could not be derived (a 'sudo -iu $user' shell does not bring them)." >&2
        echo "       Remedy, from an account with sudo: enable linger and retry:" >&2
        echo "         sudo loginctl enable-linger $user" >&2
    elif [ "${USER_BUS_DERIVED:-0}" = "1" ]; then
        echo "       Cause: the variables were derived from $dir (it exists), but the systemd --user manager" >&2
        echo "       of $user does not respond there: it is down or starting." >&2
        echo "       Remedy, from an account with sudo: check and start the manager, and retry:" >&2
        echo "         sudo systemctl status user@$uid.service" >&2
        echo "         sudo systemctl start user@$uid.service" >&2
    else
        echo "       Cause: the variables came from the caller's environment and the systemd --user manager" >&2
        echo "       of $user does not respond there (they point elsewhere, or the manager is down)." >&2
        echo "       Remedy: retry without those variables from 'sudo -iu $user' (they are derived from $dir)," >&2
        echo "       or, from an account with sudo, check the manager: sudo systemctl status user@$uid.service" >&2
    fi
}

# Probe: does the manager respond? `show-environment` is a side-effect-free
# query that requires the bus connection. Leaves in USER_BUS_ERR what systemctl
# said.
user_bus_reachable() {
    USER_BUS_ERR=""
    if USER_BUS_ERR="$(systemctl --user show-environment 2>&1 >/dev/null)"; then
        return 0
    fi
    return 1
}

# Path of the --user unit file. HOME may be missing (an `env -i`): in that
# case the path does not exist and the step is a no-op, as it always was.
user_bus_unit_file() {
    printf '%s/systemd/user/%s\n' "${XDG_CONFIG_HOME:-${HOME:-}/.config}" "$1"
}

# Manager probe for a unit that exists on disk. Exits non-zero, with cause and
# remedy, if the manager does not respond. No-op without systemctl or without
# the unit file (dev/CI, or a worker not yet handed over to systemd).
user_bus_check_unit() {
    local unit="$1" container="$2" ucfg
    command -v systemctl >/dev/null 2>&1 || return 0
    ucfg="$(user_bus_unit_file "$unit")"
    [ -f "$ucfg" ] || return 0

    user_bus_derive_env
    if ! user_bus_reachable; then
        echo "ERROR: could not reach the systemd --user manager and the unit $unit ($ucfg) exists." >&2
        echo "       Recreating the container without stopping the unit leaves the manager with a stale MainPID:" >&2
        echo "       the next 'systemctl --user daemon-reload' would stop the container." >&2
        user_bus_explain "$USER_BUS_ERR"
        echo "       If this host no longer uses autostart at boot, that file is a leftover: removing it" >&2
        echo "       (rm '$ucfg') turns this step back into a no-op." >&2
        echo "       The container $container was not touched." >&2
        return 1
    fi
}

# Step before recreating a container supervised by a --user unit: stops the
# unit unless it is at rest (avoids the race with its Restart). Exits non-zero,
# BEFORE the recipe touches the container, if the manager does not respond or
# the stop fails.
user_bus_stop_unit() {
    local unit="$1" container="$2" ucfg state rc
    command -v systemctl >/dev/null 2>&1 || return 0
    ucfg="$(user_bus_unit_file "$unit")"
    [ -f "$ucfg" ] || return 0

    user_bus_check_unit "$unit" "$container" || return 1

    # `is-active` prints the state even when it exits non-zero, and only exits
    # 0 for active/reloading/refreshing. Three readings:
    #   - no state and non-zero exit: the query itself failed (second layer of
    #     the probe), it is NOT "inactive";
    #   - inactive / failed / unknown: at rest, nothing to stop;
    #   - anything else (active, activating, deactivating, reloading...): it is
    #     stopped. `activating` includes the auto-restart of a worker in a
    #     crash loop, which is exactly the race with Restart this step avoids.
    rc=0
    state="$(systemctl --user is-active "$unit" 2>/dev/null)" || rc=$?
    if [ -z "$state" ]; then
        if [ "$rc" -ne 0 ]; then
            echo "ERROR: could not query the state of the unit $unit (systemctl --user is-active exited $rc without reporting a state)." >&2
            user_bus_explain "$(systemctl --user is-active "$unit" 2>&1 >/dev/null || true)"
            echo "       The container $container was not touched." >&2
            return 1
        fi
        state="active"
    fi
    case "$state" in
        inactive|failed|unknown) return 0 ;;
    esac

    echo ">> Autostart $state ($unit): stopping the unit before recreating (avoids the race with Restart)"
    if ! systemctl --user stop "$unit"; then
        echo "ERROR: could not stop the unit $unit (systemctl --user stop failed, reason above)." >&2
        echo "       With the unit active, recreating the container races against its Restart." >&2
        echo "       Check it with: systemctl --user status $unit" >&2
        echo "       The container $container was not touched." >&2
        return 1
    fi
}

# Executed (not sourced): dispatches the subcommand.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -euo pipefail
    usage="usage: user-bus.sh {stop-unit|check} <unit> <container>"
    [ "$#" -eq 3 ] || { echo "$usage" >&2; exit 2; }
    case "$1" in
        stop-unit) user_bus_stop_unit "$2" "$3" ;;
        check)     user_bus_check_unit "$2" "$3" ;;
        *)         echo "$usage" >&2; exit 2 ;;
    esac
fi
