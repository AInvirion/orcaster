#!/usr/bin/env bash
# check-licenses: licence gate of the orcaster command-line client.
#
# For every release target (GOOS x GOARCH of .goreleaser.yaml) it lists the
# third-party Go modules linked into the binary and fails when:
#   1. a module has a licence outside the allowlist (Apache-2.0, MIT,
#      BSD-2-Clause, BSD-3-Clause, ISC) or a licence that cannot be identified;
#   2. a linked module, at the version of go.mod, has no entry in the
#      third-party notice file (a block whose first line is the module path and
#      whose next line is `  Version:    <version>`).
#
# The module graph changes with every dependency bump; the second check keeps
# the notice that ships in the release archives in step with it.
#
# Needs `go` and `go-licenses` (go install github.com/google/go-licenses/v2@v2.0.1).
#
# Usage:
#   ops/release/check-licenses.sh                         # notice: ./THIRD_PARTY
#   ops/release/check-licenses.sh --notice path/to/THIRD_PARTY
#   ops/release/check-licenses.sh --module-dir orcaster --notice THIRD_PARTY
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODULE_DIR="$REPO_ROOT/orcaster"
NOTICE="$REPO_ROOT/THIRD_PARTY"
GO_LICENSES="${GO_LICENSES:-go-licenses}"
ALLOWED="Apache-2.0 MIT BSD-2-Clause BSD-3-Clause ISC"
# Same targets as the builds in .goreleaser.yaml.
TARGETS="linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64 freebsd/amd64"

while [ $# -gt 0 ]; do
    case "$1" in
        --notice) NOTICE="${2:?--notice needs a value}"; shift ;;
        --module-dir) MODULE_DIR="${2:?--module-dir needs a value}"; shift ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
    shift
done

command -v go >/dev/null 2>&1 || { echo "ERROR: go is not in PATH." >&2; exit 1; }
command -v "$GO_LICENSES" >/dev/null 2>&1 || {
    echo "ERROR: $GO_LICENSES is not in PATH (go install github.com/google/go-licenses/v2@v2.0.1)." >&2
    exit 1
}
[ -f "$MODULE_DIR/go.mod" ] || { echo "ERROR: no go.mod in $MODULE_DIR." >&2; exit 1; }
[ -f "$NOTICE" ] || {
    echo "ERROR: third-party notice not found: $NOTICE (pass --notice PATH)." >&2
    exit 1
}
NOTICE="$(cd "$(dirname "$NOTICE")" && pwd)/$(basename "$NOTICE")"
echo "notice: $NOTICE"

cd "$MODULE_DIR"
main_module="$(go list -m)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

for target in $TARGETS; do
    goos="${target%/*}" goarch="${target#*/}"
    # Licence per package; the main module has no licence file of its own in
    # the module directory (it is the LICENSE at the repository root).
    if ! GOOS="$goos" GOARCH="$goarch" "$GO_LICENSES" report ./... \
            --ignore "$main_module" >>"$work/report.csv" 2>"$work/report.err"; then
        echo "ERROR: go-licenses report failed for $target:" >&2
        cat "$work/report.err" >&2
        exit 1
    fi
    # Module and version of every linked package outside the main module.
    GOOS="$goos" GOARCH="$goarch" go list -deps \
        -f '{{with .Module}}{{if not .Main}}{{.Path}} {{.Version}}{{end}}{{end}}' ./... \
        >>"$work/modules.txt"
done

status=0
while IFS=, read -r package _url licence; do
    [ -n "$package" ] || continue
    case " $ALLOWED " in
        *" $licence "*) ;;
        *)
            echo "ERROR: $package has licence '$licence', not in the allowlist ($ALLOWED)." >&2
            status=1 ;;
    esac
done < <(sort -u "$work/report.csv")

while read -r module version; do
    [ -n "$module" ] || continue
    if ! awk -v m="$module" -v v="$version" '
            found { if ($1 == "Version:" && $2 == v) ok = 1; found = 0 }
            $0 == m { found = 1 }
            END { exit ok ? 0 : 1 }' "$NOTICE"; then
        echo "ERROR: $module $version has no entry in $NOTICE (a line '$module' followed by '  Version:    $version')." >&2
        status=1
    fi
done < <(grep -v '^$' "$work/modules.txt" | sort -u)

if [ "$status" -ne 0 ]; then
    echo "FAILED: licence check of $main_module" >&2
    exit 1
fi
echo "OK  $(sort -u "$work/modules.txt" | grep -vc '^$') modules, licences in the allowlist, all listed in $(basename "$NOTICE")"
