#!/usr/bin/env bash
# release-body: print the notes of the GitHub release of a version.
#
# The notes are ops/release/release-body.md (one section per component: Server,
# Worker, CLI, with the asset names and the install and upgrade commands) with
# @VERSION@ replaced by X.Y.Z, followed by the `## [X.Y.Z]` section of
# CHANGELOG.md (ops/release/release-notes.sh). Fails when that section is
# missing or empty, so a release never goes out without its changes.
#
# Usage:
#   ops/release/release-body.sh vX.Y.Z [CHANGELOG.md]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${1:?usage: release-body.sh vX.Y.Z [CHANGELOG.md]}"
VERSION="${VERSION#v}"
CHANGELOG="${2:-CHANGELOG.md}"
TEMPLATE="$HERE/release-body.md"

if ! printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'; then
    echo "ERROR: $1 is not a version X.Y.Z." >&2
    exit 1
fi
[ -f "$TEMPLATE" ] || { echo "ERROR: $TEMPLATE not found." >&2; exit 1; }

# Read the changes first: a missing section fails before anything is printed.
changes="$("$HERE/release-notes.sh" "$VERSION" "$CHANGELOG")"

# The version holds only [0-9A-Za-z.-]: safe in the sed replacement.
sed "s/@VERSION@/$VERSION/g" "$TEMPLATE"
printf '\n%s\n' "$changes"
