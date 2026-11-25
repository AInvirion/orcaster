# shellcheck shell=bash
# Stub guard: a suite that fakes ssh, scp or rsync must fail before running any
# test if the command PATH resolves is not its stub. A stub without execute
# permission (a forgotten chmod +x) makes bash skip it and use the real system
# binary: the suite would talk to a real host.
#
# Usage, after creating the stubs and prepending their directory to PATH:
#   assert_stubbed "$FAKE_BIN" ssh scp
# Stubs created inside a test are verified the same way, right after creating
# them. Fails (return 1) naming each wrongly resolved command and its path.

assert_stubbed() {  # $1=stubs directory, $2...=commands
    local dir="$1" cmd resolved want got bad=0
    shift
    want="$(cd -P -- "$dir" 2>/dev/null && pwd -P)" || {
        echo "stub_guard: the stubs directory does not exist: $dir" >&2
        return 1
    }
    for cmd in "$@"; do
        if [ ! -x "$dir/$cmd" ]; then
            echo "stub_guard: the stub $dir/$cmd does not exist or is not executable" >&2
            bad=1
        fi
        resolved="$(command -v "$cmd" 2>/dev/null)" || resolved=""
        got=""
        # The physical path is compared (on macOS /var is a link to /private/var).
        [ -n "$resolved" ] && got="$(cd -P -- "$(dirname -- "$resolved")" 2>/dev/null && pwd -P)/$(basename -- "$resolved")"
        if [ "$got" != "$want/$cmd" ]; then
            echo "stub_guard: '$cmd' resolves to '${resolved:-<nothing>}', not to $dir/$cmd" >&2
            bad=1
        fi
    done
    return "$bad"
}
