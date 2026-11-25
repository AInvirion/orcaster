#!/usr/bin/env bash
# check-tags: fail unless the Go module tag orcaster/vX.Y.Z exists on the
# remote and points at the commit of the release tag vX.Y.Z.
#
# The release workflow runs it before building: a release without the module
# tag would publish binaries whose version `go install` cannot resolve. See
# tag-release.sh for the two-tag scheme.
#
# Usage:
#   ops/release/check-tags.sh vX.Y.Z <commit> [remote]
set -euo pipefail

TAG="${1:?usage: check-tags.sh vX.Y.Z COMMIT [REMOTE]}"
COMMIT="${2:?usage: check-tags.sh vX.Y.Z COMMIT [REMOTE]}"
REMOTE="${3:-origin}"
GO_TAG="orcaster/$TAG"

if ! printf '%s\n' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'; then
    echo "ERROR: $TAG is not a release tag vX.Y.Z." >&2
    exit 1
fi

refs="$(git ls-remote --tags "$REMOTE")"
# An annotated tag is listed twice: the tag object, then `^{}` with its commit.
peeled="$(printf '%s\n' "$refs" | awk -v r="refs/tags/$GO_TAG^{}" '$2 == r {print $1}')"
direct="$(printf '%s\n' "$refs" | awk -v r="refs/tags/$GO_TAG" '$2 == r {print $1}')"
target="${peeled:-$direct}"

if [ -z "$target" ]; then
    echo "ERROR: $GO_TAG does not exist on $REMOTE. Push both tags with ops/release/tag-release.sh $TAG --push." >&2
    exit 1
fi
if [ "$target" != "$COMMIT" ]; then
    echo "ERROR: $GO_TAG points at $target, not at $COMMIT (the commit of $TAG)." >&2
    exit 1
fi
echo "OK  $GO_TAG and $TAG point at $COMMIT"
