#!/usr/bin/env bash
# build-components: build the server and worker assets of a release.
#
# Writes to OUT_DIR (created if missing, must hold no other file):
#   orcaster-server-X.Y.Z.tar.gz             server bundle: the repository tree
#                                            at REF with the `orcaster/` prefix
#                                            and a RELEASE manifest, the tree
#                                            the Debian 13 installer runs from
#                                            (setup-scripts/debian13);
#   orcaster-worker-X.Y.Z.tar.gz             worker bundle: the same tree, the
#                                            package that
#                                            ops/release/install-release.sh
#                                            installs on a worker host;
#   orcaster_worker-X.Y.Z-py3-none-any.whl   worker wheel, built from that tree
#                                            (pip name, PEP 427: it cannot
#                                            carry the orcaster-worker- prefix).
# Both bundles come from one ops/release/make-release.sh run, so they hold the
# same bytes; each has its own name so a download says which component it is.
#
# Fails unless the version of the tag equals `version` in server/pyproject.toml
# and client/pyproject.toml at REF: server, worker and CLI share one version.
#
# The release workflow runs it before GoReleaser, which adds the files of
# OUT_DIR to the release and to its checksums file (ORCASTER_COMPONENTS_DIR).
# Needs git >= 2.37 and uv.
#
# Usage:
#   ops/release/build-components.sh vX.Y.Z OUT_DIR [--ref REF]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG=""
OUT=""
REF="HEAD"

usage() { echo "usage: build-components.sh vX.Y.Z OUT_DIR [--ref REF]" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref needs a value}"; shift ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        -*) echo "ERROR: unknown argument: $1 (see --help)" >&2; exit 1 ;;
        *)
            if [ -z "$TAG" ]; then TAG="$1"
            elif [ -z "$OUT" ]; then OUT="$1"
            else usage
            fi ;;
    esac
    shift
done
[ -n "$TAG" ] && [ -n "$OUT" ] || usage

if ! printf '%s\n' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'; then
    echo "ERROR: $TAG is not a release tag vX.Y.Z." >&2
    exit 1
fi
VERSION="${TAG#v}"

git -C "$REPO_ROOT" rev-parse --verify --quiet "${REF}^{commit}" >/dev/null \
    || { echo "ERROR: unknown ref: $REF" >&2; exit 1; }

# One version for every component: the tag, the server and the worker agree.
status=0
for pyproject in server/pyproject.toml client/pyproject.toml; do
    content="$(git -C "$REPO_ROOT" show "$REF:$pyproject")"
    found="$(printf '%s\n' "$content" \
        | sed -n -E "s/^version[[:space:]]*=[[:space:]]*[\"']([^\"']+)[\"'].*/\1/p")"
    found="${found%%$'\n'*}"
    if [ "$found" != "$VERSION" ]; then
        echo "ERROR: $pyproject at $REF has version '${found}', not $VERSION (the tag $TAG). Server, worker and CLI share one version: set it in both pyproject.toml files before tagging." >&2
        status=1
    fi
done
[ "$status" -eq 0 ] || exit 1

command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is missing from PATH (it builds the worker wheel)." >&2; exit 1; }

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
if [ -n "$(ls -A "$OUT")" ]; then
    echo "ERROR: $OUT is not empty; every file in it becomes a release asset." >&2
    exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/build-components.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Bundle: the same package as the tarball deploy (prefix, RELEASE manifest).
bundle="$("$REPO_ROOT/ops/release/make-release.sh" --ref "$REF" --out "$WORK/bundle")"
cp "$bundle" "$OUT/orcaster-server-$VERSION.tar.gz"
cp "$bundle" "$OUT/orcaster-worker-$VERSION.tar.gz"

# Wheel: built from the bundle's own tree, never from the working tree.
mkdir "$WORK/tree"
tar -xzf "$bundle" -C "$WORK/tree"
uv build --quiet --wheel --out-dir "$WORK/wheel" "$WORK/tree/orcaster/client" >&2
wheel="orcaster_worker-$VERSION-py3-none-any.whl"
[ -f "$WORK/wheel/$wheel" ] || {
    echo "ERROR: uv build did not write $wheel; it wrote: $(ls "$WORK/wheel")" >&2
    exit 1
}
cp "$WORK/wheel/$wheel" "$OUT/$wheel"

echo "OK  components of $TAG in $OUT:" >&2
ls -1 "$OUT" >&2
