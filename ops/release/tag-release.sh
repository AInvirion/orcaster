#!/usr/bin/env bash
# tag-release: create the two tags of a release at one commit and, with
# --push, push both in one atomic push.
#
#   vX.Y.Z           the release number of the whole system; it starts the
#                    release workflow (trigger `v*`) and names the release.
#   orcaster/vX.Y.Z  the version of the Go module in orcaster/. The Go proxy
#                    versions a module that lives in a subdirectory only by
#                    tags prefixed with that directory, so without this tag
#                    `go install github.com/AInvirion/orcaster/orcaster@vX.Y.Z`
#                    fails and `@latest` resolves to a pseudo-version.
#
# A tag that already exists at the same commit is kept; one that exists at
# another commit is an error (tags are never moved). The release workflow
# fails when orcaster/vX.Y.Z does not point at the commit of vX.Y.Z.
#
# Usage:
#   ops/release/tag-release.sh v1.2.3                  # tags HEAD, prints the push
#   ops/release/tag-release.sh v1.2.3 --ref <commit> --remote origin --push
set -euo pipefail

TAG=""
REF="HEAD"
REMOTE="origin"
PUSH=0

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref needs a value}"; shift ;;
        --remote) REMOTE="${2:?--remote needs a value}"; shift ;;
        --push) PUSH=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "ERROR: unknown argument: $1 (see --help)" >&2; exit 1 ;;
        *) [ -z "$TAG" ] || { echo "ERROR: one tag only." >&2; exit 1; }; TAG="$1" ;;
    esac
    shift
done

[ -n "$TAG" ] || { echo "usage: tag-release.sh vX.Y.Z [--ref REF] [--remote NAME] [--push]" >&2; exit 1; }
if ! printf '%s\n' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'; then
    echo "ERROR: $TAG is not a release tag vX.Y.Z." >&2
    exit 1
fi
GO_TAG="orcaster/$TAG"

commit="$(git rev-parse --verify --quiet "$REF^{commit}")" \
    || { echo "ERROR: unknown ref: $REF" >&2; exit 1; }

for tag in "$TAG" "$GO_TAG"; do
    existing="$(git rev-parse --verify --quiet "refs/tags/$tag^{commit}" || true)"
    if [ -z "$existing" ]; then
        git tag -a "$tag" -m "Orcaster $TAG" "$commit"
        echo "created $tag at $commit" >&2
    elif [ "$existing" = "$commit" ]; then
        echo "kept    $tag at $commit (already there)" >&2
    else
        echo "ERROR: $tag already exists at $existing, not at $commit; tags are never moved." >&2
        exit 1
    fi
done

if [ "$PUSH" -eq 1 ]; then
    git push --atomic "$REMOTE" "refs/tags/$TAG" "refs/tags/$GO_TAG"
    echo "OK  pushed $TAG and $GO_TAG to $REMOTE" >&2
else
    echo "not pushed; run: git push --atomic $REMOTE refs/tags/$TAG refs/tags/$GO_TAG" >&2
fi
