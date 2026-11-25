#!/usr/bin/env bash
# dry-run: build every asset and the notes of a release locally, as the release
# workflow does, without creating a tag and without publishing anything.
#
# Steps (the same as .github/workflows/release.yml, minus the tag check, the
# publish and the attestation):
#   1. ops/release/build-components.sh: server and worker bundles, worker wheel;
#   2. ops/release/release-body.sh: the release notes;
#   3. goreleaser release --snapshot: CLI archives, SBOMs, checksums file;
#   4. ops/release/check-archives.sh: names, contents and checksums.
# Prints the release title, the asset list and where the notes are.
#
# VERSION defaults to the version in client/pyproject.toml; the CHANGELOG.md
# must have its `## [X.Y.Z]` section. Without syft in PATH the SBOMs are skipped
# with a warning (the workflow always builds them).
#
# Usage:
#   ops/release/dry-run.sh [vX.Y.Z] [--out DIR]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG=""
OUT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="${2:?--out needs a value}"; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        -*) echo "ERROR: unknown argument: $1 (see --help)" >&2; exit 1 ;;
        *) [ -z "$TAG" ] || { echo "ERROR: one version only." >&2; exit 1; }; TAG="$1" ;;
    esac
    shift
done

command -v goreleaser >/dev/null 2>&1 || { echo "ERROR: goreleaser is missing from PATH." >&2; exit 1; }

if [ -z "$TAG" ]; then
    TAG="v$(sed -n -E "s/^version[[:space:]]*=[[:space:]]*[\"']([^\"']+)[\"'].*/\1/p" "$REPO_ROOT/client/pyproject.toml" | head -n1)"
fi
VERSION="${TAG#v}"
[ -n "$OUT" ] || OUT="$(mktemp -d "${TMPDIR:-/tmp}/release-dry-run.XXXXXX")"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
COMPONENTS="$OUT/components"
NOTES="$OUT/release-notes.md"

echo "== 1. server and worker components" >&2
"$REPO_ROOT/ops/release/build-components.sh" "$TAG" "$COMPONENTS"

echo "== 2. release notes" >&2
"$REPO_ROOT/ops/release/release-body.sh" "$TAG" "$REPO_ROOT/CHANGELOG.md" > "$NOTES"

echo "== 3. goreleaser snapshot" >&2
skip=()
if ! command -v syft >/dev/null 2>&1; then
    echo "WARNING: syft is not in PATH; the SBOMs are skipped in this dry run (the release workflow builds them)." >&2
    skip=(--skip=sbom)
fi
(
    cd "$REPO_ROOT/orcaster"
    GORELEASER_CURRENT_TAG="$TAG" ORCASTER_COMPONENTS_DIR="$COMPONENTS" \
        goreleaser release --snapshot --clean ${skip[@]+"${skip[@]}"} \
        --release-notes "$NOTES" >&2
)

echo "== 4. check assets" >&2
"$REPO_ROOT/ops/release/check-archives.sh" "$REPO_ROOT/orcaster/dist" "$VERSION" "$COMPONENTS"

title="$(sed -n 's/^  name_template: "\(Orcaster .*\)"$/\1/p' "$REPO_ROOT/orcaster/.goreleaser.yaml")"
echo
echo "Release title: ${title//\{\{ .Tag \}\}/$TAG}"
echo "Assets:"
(
    cd "$REPO_ROOT/orcaster/dist"
    ls -1 orcaster-cli-* "orcaster-$VERSION-checksums.txt"
    ls -1 "$COMPONENTS"
) | sort | sed 's/^/  /'
echo "Notes: $NOTES"
echo "Nothing was tagged or published."
