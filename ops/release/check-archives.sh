#!/usr/bin/env bash
# check-archives: verify the assets of a release before anything is published.
#
# DIST is the GoReleaser dist directory (CLI archives and the checksums file);
# COMPONENTS is the directory that ops/release/build-components.sh wrote (server
# and worker files). Fails unless:
#   - there is at least one CLI archive orcaster-cli-<version>-<os>-<arch>
#     (.tar.gz, or .zip for windows), and no archive with another name;
#   - every CLI archive holds LICENSE, NOTICE, THIRD_PARTY, README.md and the
#     binary `orcaster` (`orcaster.exe` in the windows .zip), at its root;
#   - COMPONENTS holds exactly orcaster-server-<version>.tar.gz,
#     orcaster-worker-<version>.tar.gz and
#     orcaster_worker-<version>-py3-none-any.whl;
#   - orcaster-<version>-checksums.txt lists every one of those files with its
#     SHA-256.
#
# Usage:
#   ops/release/check-archives.sh DIST VERSION COMPONENTS
#   ops/release/check-archives.sh orcaster/dist 2.0.0 "$ORCASTER_COMPONENTS_DIR"
set -euo pipefail

usage="usage: check-archives.sh DIST_DIR VERSION COMPONENTS_DIR"
DIST="${1:?$usage}"
VERSION="${2:?$usage}"
VERSION="${VERSION#v}"
COMPONENTS="${3:?$usage}"
[ -d "$DIST" ] || { echo "ERROR: not a directory: $DIST" >&2; exit 1; }
[ -d "$COMPONENTS" ] || { echo "ERROR: not a directory: $COMPONENTS" >&2; exit 1; }
SUMS_NAME="orcaster-$VERSION-checksums.txt"
SUMS="$DIST/$SUMS_NAME"
[ -f "$SUMS" ] || { echo "ERROR: missing $SUMS" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then
    sha256() { sha256sum "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$1" | awk '{print $1}'; }
else
    echo "ERROR: neither sha256sum nor shasum is in PATH." >&2
    exit 1
fi

status=0

# check_listed FILE: the checksums file lists FILE's name with its SHA-256.
check_listed() {
    local name listed
    name="$(basename "$1")"
    listed="$(awk -v n="$name" '$2 == n {print $1}' "$SUMS")"
    if [ -z "$listed" ]; then
        echo "ERROR: $name is not listed in $SUMS_NAME." >&2
        status=1
    elif [ "$listed" != "$(sha256 "$1")" ]; then
        echo "ERROR: checksum of $name does not match $SUMS_NAME." >&2
        status=1
    fi
}

count=0
for archive in "$DIST"/*; do
    name="$(basename "$archive")"
    case "$name" in
        *.tar.gz) listing="$(tar -tzf "$archive")"; binary=orcaster ;;
        *.zip) listing="$(unzip -Z1 "$archive")"; binary=orcaster.exe ;;
        *) continue ;;
    esac
    count=$((count + 1))
    stem="${name%.tar.gz}"
    stem="${stem%.zip}"
    case "$stem" in
        "orcaster-cli-$VERSION-"*) platform="${stem#"orcaster-cli-$VERSION-"}" ;;
        *) platform="" ;;
    esac
    if ! printf '%s\n' "$platform" | grep -Eq '^[a-z0-9]+-[a-z0-9]+$'; then
        echo "ERROR: $name does not follow orcaster-cli-$VERSION-<os>-<arch>." >&2
        status=1
    fi
    case "$platform" in
        windows-*) [ "${name##*.}" = zip ] || { echo "ERROR: $name: windows archives are .zip." >&2; status=1; } ;;
    esac
    for required in LICENSE NOTICE THIRD_PARTY README.md "$binary"; do
        if ! printf '%s\n' "$listing" | grep -Fxq "$required"; then
            echo "ERROR: $name does not contain $required." >&2
            status=1
        fi
    done
    check_listed "$archive"
done
if [ "$count" -eq 0 ]; then
    echo "ERROR: no orcaster-cli-* archive in $DIST." >&2
    status=1
fi

expected="orcaster-server-$VERSION.tar.gz orcaster-worker-$VERSION.tar.gz orcaster_worker-$VERSION-py3-none-any.whl"
for name in $expected; do
    if [ -f "$COMPONENTS/$name" ]; then
        check_listed "$COMPONENTS/$name"
    else
        echo "ERROR: missing $COMPONENTS/$name." >&2
        status=1
    fi
done
for file in "$COMPONENTS"/* "$COMPONENTS"/.[!.]*; do
    [ -e "$file" ] || continue
    name="$(basename "$file")"
    case " $expected " in
        *" $name "*) ;;
        *) echo "ERROR: unexpected file $name in $COMPONENTS: every file there becomes a release asset." >&2; status=1 ;;
    esac
done

if [ "$status" -ne 0 ]; then
    echo "FAILED: release assets of $VERSION" >&2
    exit 1
fi
echo "OK  $VERSION: $count CLI archives (names, LICENSE, NOTICE, THIRD_PARTY, README.md, binary), server and worker bundles, worker wheel, all in $SUMS_NAME"
