#!/usr/bin/env bash
# release-notes: print the CHANGELOG.md section of one version, the release
# notes of the GitHub release.
#
# The section is the lines after the heading `## [X.Y.Z]` (optionally followed
# by ` - YYYY-MM-DD`) up to the next `## ` heading or link definition, without
# leading and trailing blank lines. Fails when the heading is missing or the
# section is empty, so a release never goes out with empty notes.
#
# Usage:
#   ops/release/release-notes.sh 1.2.3                 # reads ./CHANGELOG.md
#   ops/release/release-notes.sh v1.2.3 path/to/CHANGELOG.md
set -euo pipefail

VERSION="${1:?usage: release-notes.sh X.Y.Z [CHANGELOG.md]}"
VERSION="${VERSION#v}"
CHANGELOG="${2:-CHANGELOG.md}"
[ -f "$CHANGELOG" ] || { echo "ERROR: $CHANGELOG not found." >&2; exit 1; }

awk -v version="$VERSION" '
    BEGIN { heading = "## [" version "]"; state = 0 }
    state == 1 && (/^## / || /^\[[^]]+\]:[ \t]/) { state = 2 }
    state == 1 { lines[++n] = $0 }
    state == 0 && index($0, heading) == 1 {
        rest = substr($0, length(heading) + 1)
        if (rest == "" || rest ~ /^[ \t]/) state = 1
    }
    END {
        if (state == 0) {
            printf "ERROR: no \"## [%s]\" heading in %s.\n", version, FILENAME > "/dev/stderr"
            exit 1
        }
        first = 1; last = n
        while (first <= last && lines[first] ~ /^[ \t]*$/) first++
        while (last >= first && lines[last] ~ /^[ \t]*$/) last--
        if (first > last) {
            printf "ERROR: the \"## [%s]\" section of %s is empty.\n", version, FILENAME > "/dev/stderr"
            exit 1
        }
        for (i = first; i <= last; i++) print lines[i]
    }
' "$CHANGELOG"
