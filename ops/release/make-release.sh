#!/usr/bin/env bash
# make-release: builds the worker deploy package from a clean ref of the
# repo. It runs on the operator's machine and touches no network or host.
#
# What is reproducible is the release identity: the same ref always gives the
# same version= and the same commit= in RELEASE. The tarball bytes are not,
# because RELEASE also carries built=, the build timestamp, which changes on
# every run on purpose (it tells when the package was generated, not only
# which commit it comes from). That field is not dropped just to be able to
# say "reproducible": the word applies to the identity, not to the file bytes.
#
# The format is a `git archive` tar.gz and NOT a `git bundle`: a bundle leaves
# a .git on the host, which is exactly what this deploy removes. The release
# identity travels embedded with --add-virtual-file (git >= 2.37), so the host
# can prove which code it has without needing git.
#
# Usage:
#   ops/release/make-release.sh                 # HEAD, output in dist/
#   ops/release/make-release.sh --ref vX.Y.Z --out /tmp/rel
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REF="HEAD"
OUT="$REPO_ROOT/dist"
PREFIX="orcaster"

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref needs a value}"; shift ;;
        --out) OUT="${2:?--out needs a value}"; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1 (use --help)" >&2; exit 1 ;;
    esac
    shift
done

command -v git >/dev/null 2>&1 || { echo "ERROR: git is missing from PATH." >&2; exit 1; }

# Same detection as install-release.sh, resolved HERE (before building
# anything) so no orphan tarball is left without its .sha256: `shasum` comes
# from perl (on Debian the `perl` package ships it, not `perl-base`), so a
# minimal Linux operator machine does not have it. GNU ships `sha256sum`;
# Darwin ships `shasum`. The output format is the same in both
# ("<hash>  <file>"), which is what install-release.sh consumes with
# `awk '{print $1}'`.
if command -v sha256sum >/dev/null 2>&1; then
    HASH_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    HASH_CMD="shasum -a 256"
else
    echo "ERROR: neither sha256sum nor shasum found in PATH; cannot sign the artifact." >&2
    exit 1
fi

# --add-virtual-file exists since git 2.37 (not since 2.32: that version has
# --add-file, a different option that cannot embed inline content). Without
# --add-virtual-file, the release identity would have to be written into the
# repo tree, which is exactly what this deploy avoids: the host checkout stays
# clean and comparable.
git_version="$(git --version | awk '{print $3}')"
case "$(printf '%s\n2.37.0\n' "$git_version" | sort -V | head -n1)" in
    2.37.0) ;;
    *) echo "ERROR: git $git_version is older than 2.37 (--add-virtual-file)." >&2; exit 1 ;;
esac

# `git archive` packs the COMMIT tree, never the working tree. With the default
# (--ref HEAD) that is a silent trap: an operator who patches something to fix
# the fleet and runs push-release.sh without committing sees ALL the success
# signals (the printed version=/commit=, the embedded RELEASE, the
# verification criterion comparing that commit against the repo) and the host
# ends up running code without the patch. It warns, it does not abort: building
# a release from a commit with a dirty tree is legitimate (the remaining
# changes may be work in progress unrelated to the release), and this script's
# header already states that what gets packaged is a clean ref; what was
# missing was saying so when it is not.
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
    echo "WARNING: the working tree has uncommitted changes and the tarball comes from COMMIT $REF, not from the tree: those changes do NOT travel to the host. Commit them (or pass --ref with the commit that has them) if you expected to push them." >&2
fi

# The `^{commit}` PEELS the ref down to the commit, and it is not cosmetic:
# the release tags of this repo are ANNOTATED, so `git rev-parse vX.Y.Z`
# returns the SHA of the TAG OBJECT and not that of the commit (measured: the
# tag and the commit give different SHAs). Without peeling, the RELEASE
# manifest stores a SHA that names no commit in a field called `commit=`, and
# that is the only thing a host can use to prove which code it has now that
# the tarball deploy removes its .git. The post-deploy verification does not
# catch it because it compares the host's RELEASE against the same value this
# script printed: both sides carry the error and the comparison passes anyway.
# The tarball bytes were never wrong, because the `git archive` below peels
# the tag on its own. `^{commit}` is a no-op on a ref that already names a
# commit, so HEAD, a branch or a raw SHA keep working the same.
commit="$(git -C "$REPO_ROOT" rev-parse "${REF}^{commit}")"
short="$(git -C "$REPO_ROOT" rev-parse --short=8 "${REF}^{commit}")"
# The version comes from the REF's pyproject and not from a tag: it is the same
# source the repo release uses, and an untagged ref still produces a valid
# artifact.
#
# The content is fetched into a variable and NOT chained as `git show | grep
# -m1`: with `set -o pipefail`, a consumer that closes the pipe at the first
# match (`grep -m1`, `head -n1`, a `sed` with `q`) makes `git show` die from
# SIGPIPE and that 141 aborts the script without printing a single line of
# cause. The file is under 1 KiB, so the variable is more than enough.
pyproject="$(git -C "$REPO_ROOT" show "$REF:client/pyproject.toml")"
# The `sed` consumes ALL of its stdin (there is no `q`) and may print more than
# one line; the first one is chosen later, with parameter expansion. Both TOML
# quote forms are accepted: with the previous pattern, which only looked at
# `"`, a `version = '1.2.3'` (valid TOML) did not match the substitution and
# `sed` let the WHOLE LINE through, so the version ended up as
# `version = '1.2.3'`: a file name with spaces and quotes, and a `--tarball`
# line that the host shell splits into words.
version="$(printf '%s\n' "$pyproject" \
    | sed -n -E "s/^version[[:space:]]*=[[:space:]]*[\"']([^\"']+)[\"'].*/\1/p")"
version="${version%%$'\n'*}"
# This guard CAN fire now (before it was unreachable: if the grep did not
# match, pipefail killed the script one line earlier, silently). It names the
# real state: a pyproject with `dynamic = ["version"]`, an indented key, or
# the key moved to another table.
[ -n "$version" ] || { echo "ERROR: could not read the version from client/pyproject.toml at $REF: no top-level 'version = \"...\"' line found." >&2; exit 1; }
# The version becomes a file name and travels to the `--tarball` that runs on
# the host, so any character that there stops being data and becomes syntax is
# rejected.
case "$version" in
    *[!A-Za-z0-9._+-]*)
        echo "ERROR: the version read from client/pyproject.toml at $REF is not usable as a file name: '$version'." >&2
        exit 1 ;;
esac

built="$(date -u +%FT%TZ)"
name="orcaster-${version}-${short}.tgz"
mkdir -p "$OUT"
# Make it absolute HERE, right after creating it, and not before: if "$OUT" was
# relative, "git -C "$REPO_ROOT" archive -o "$OUT/$name"" below resolves it
# against REPO_ROOT (because of the -C), not against the cwd of the operator
# who ran mkdir. Without this, a relative --out points to two different
# directories depending on which line touches it, and "git archive" dies with
# ENOENT even though mkdir already ran (leaving an orphan directory). Making it
# absolute after the mkdir (and not before, next to the argument parsing)
# keeps the property that nothing is created on disk if git/hash/version fail
# first.
OUT="$(cd "$OUT" && pwd)"

# The manifest enters the archive as a virtual file (--add-virtual-file, below):
# it never touches the operator's working tree. It is built DIRECTLY in this
# variable and not in a temporary file: the temporary file was written with a
# group of `echo`s and read back with a `cat` six lines later, without anyone
# else ever using it. It cost a file on disk, an extra entry in the trap, two
# forks, and a window between the `mktemp` and the trap installation in which
# the temporary file leaked if something failed in between.
#
# The FINAL newline is part of the content, not cosmetic: without it, RELEASE
# ends without a terminator on the last line ("built=..."), and a `while read`
# on the host that consumes it silently drops that line. With the explicit
# concatenation the terminator is visible, instead of having to restore it
# after a command substitution trims it.
manifest_content="version=$version"$'\n'"commit=$commit"$'\n'"built=$built"$'\n'
# $tmp_tgz and $tmp_sha are declared before the trap so the trap can clean them
# up even if the failure happens before they are assigned (see the archiving
# and hashing below).
tmp_tgz=""
tmp_sha=""
trap 'rm -f ${tmp_tgz:+"$tmp_tgz"} ${tmp_sha:+"$tmp_sha"}' EXIT

# It archives to a temporary file and publishes with `mv`, instead of writing
# directly over "$OUT/$name". The name is deterministic per commit, so a second
# run on the same commit points to the SAME file, and `git archive -o`
# truncates it to 0 bytes before starting: if that run dies halfway (disk full,
# corrupt object, Ctrl-C), `set -e` aborts before rehashing and $OUT is left
# with an empty tarball next to the valid, old .sha256 of the good build.
# Nothing on the operator side detects that mismatch; only install-release.sh
# catches it on the host. With the temporary file, a failed archive does not
# touch the good pair.
tmp_tgz="$OUT/.$name.tmp"
# tar.umask=0022: git's default is 0002 and produced 0775/0664. The host
# extracts as root, which keeps those modes, and the INSTALL_DIR of the first
# delivery (owned by root until the worker user exists) ended up group-writable.
# With 0022 the result is 0755/0644, which is what the WARNING of
# install-release.sh says. It goes with -c so an operator's tar.umask cannot
# change it.
git -c tar.umask=0022 -C "$REPO_ROOT" archive \
    --format=tar.gz \
    --prefix="$PREFIX/" \
    --add-virtual-file="$PREFIX/RELEASE:$manifest_content" \
    -o "$tmp_tgz" \
    "$REF"
mv "$tmp_tgz" "$OUT/$name"
tmp_tgz=""

# The .sha256 has the same problem the tarball's temporary file solved, above:
# "$name.sha256" is a name deterministic per commit, and writing it directly
# with `>` truncates it BEFORE $HASH_CMD finishes reading the whole tarball.
# The tarball `mv` has already run by the time we get here, so if $HASH_CMD
# dies halfway (disk full, signal, read-only mount) `set -e` aborts leaving in
# $OUT a valid NEW tarball paired with an empty .sha256 or, worse, the one from
# the previous build (same name, different content because `built=` changes on
# every run): exactly the operator-invisible mismatch the tarball's temporary
# file exists to avoid, never having been extended to the checksum. It is
# published with the same temporary file + `mv`.
#
# An irreducible window remains between the two `mv`s: the new tarball is
# already published at "$OUT/$name" before this `mv` runs, so in that fraction
# of a second the .sha256 on disk is still the one from the PREVIOUS build
# (complete and valid, but for other content). It is not closed by hashing the
# tarball's temporary file before its `mv`: $HASH_CMD records in its output the
# NAME it was given, so hashing "$tmp_tgz" would force rewriting that column by
# hand to say "$name", with no observable gain for the operator.
tmp_sha="$OUT/.$name.sha256.tmp"
# shellcheck disable=SC2086 # $HASH_CMD expands to 1 or 2 words: intentional word-splitting
( cd "$OUT" && $HASH_CMD "$name" > "$tmp_sha" )
mv "$tmp_sha" "$OUT/$name.sha256"
tmp_sha=""

echo "    version=$version commit=$commit" >&2
# Contract with push-release.sh: the artifact path is the ONLY stdout line, so
# the caller captures it without guessing. Everything else goes to stderr.
# Without this, the caller would have to pick the tarball from the output
# directory by name, and there is no reliable order there: `sort` is
# lexicographic, not chronological, so with two releases in dist/ the SHA
# suffix decides which one wins and the push could push the previous one.
printf '%s\n' "$OUT/$name"
