#!/usr/bin/env python3
"""Link checker for the repository.
link-check: ignore-file (it names the manifest path it reads)

Usage: check_links.py [TREE] [--manifest FILE] [--report-only] [--list-limit N]

Walks TREE (default: the current directory; `git ls-files` when it is a git
checkout, a plain walk otherwise) and fails when a SHIPPED file refers to a path
that (a) an optional manifest excludes ("excluded") or (b) does not exist in the
tree ("missing").

What counts as a reference:
  * Markdown links and images `[t](target)`, `![a](target)` and reference
    definitions `[x]: target`, outside code fences; relative targets resolve
    against the file's directory, `/x` against the tree root;
  * inline code spans in Markdown that start with `./` or `../`;
  * path strings that start with `docs/` or `ops/`, in any text file (code
    comments, messages, docs), resolved against the tree root.

Shipped = every file the manifest does not exclude. Files that the manifest
excludes are not scanned. The manifest is optional (--manifest); without one
only "missing" is checked.
A line that contains `link-check: ignore` is skipped; a file with
`link-check: ignore-file` in its first five lines is skipped (test fixtures).

Not checked (see NOT_CHECKED, printed with every report):
see below. Exit status: 1 when there is any finding, 0 otherwise, or always 0
with --report-only. Standard library only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

NOT_CHECKED = (
    "URLs (http, https, mailto, ...): no network access, not resolved.",
    "Anchors and fragments (`#section`, `?query`): the file is checked, not the heading.",
    "Path strings that do not start with `docs/` or `ops/` (e.g. `server/orcaster_server/x.py`,"
    " `./x.sh`) outside Markdown links and `./`/`../` code spans in .md files.",
    "Paths in templates or patterns (`*`, `?`, `<x>`, `{x}`, `$x`) and paths built at runtime.",
    "Markdown links in non-Markdown files; HTML `href`/`src`; `[a][ref]` uses (only the"
    " definition `[ref]: target` is checked).",
    "Links inside code fences (they are not links), and `docs/`/`ops/` mentions in prose that"
    " are not paths.",
    "Binary files, files over 2 MiB, lock files, and files the manifest excludes.",
    "Whether a file that exists is meaningful for the reader (only existence and exclusion).",
)

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".worktrees", ".mypy_cache"}
SKIP_NAMES = {"uv.lock", "go.sum", "package-lock.json"}
MAX_BYTES = 2 * 1024 * 1024
IGNORE_MARK = "link-check: ignore"
IGNORE_FILE_MARK = "link-check: ignore-file"  # in the first lines: skip the whole file
# No default manifest: without --manifest the checker verifies "missing" only.
MANIFEST_REL: str | None = None

PATH_CHARS = r"[\w.@+\-/]"
# `docs/` or `ops/` not preceded by a path/word character or `:` (URL), ending on a
# word character or `/`; a pattern character right after the match voids it.
PATH_RE = re.compile(
    rf"(?<![\w.@+\-/~:$]){'(?:docs|ops)'}/(?:{PATH_CHARS}*[\w/])?(?![\w.@+\-/*?<>{{}}$\[\]])"
)
PATTERN_AFTER = re.compile(r"[*?<>{}$\[]")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]*)[^)]*\)")
REFDEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
CODE_SPAN_RE = re.compile(r"`+([^`\n]+)`+")
REL_SPAN_RE = re.compile(r"^\.{1,2}/[\w.@+\-/]*[\w/]$")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
FENCE_RE = re.compile(r"^\s{0,3}(```+|~~~+)")


@dataclass
class Manifest:
    allow: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(list(data.get("allow", [])), list(data.get("exclude", [])))

    @staticmethod
    def _matches(entry: str, path: str) -> bool:
        if entry.endswith("/"):
            return path.startswith(entry) or path == entry[:-1]
        return path == entry

    def is_excluded(self, path: str) -> bool:
        """Longest matching entry wins, as in the export; no match = shipped."""
        best, excluded = -1, False
        for entries, is_exc in ((self.allow, False), (self.exclude, True)):
            for e in entries:
                if self._matches(e, path) and len(e) > best:
                    best, excluded = len(e), is_exc
        return excluded


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    kind: str  # "excluded" | "missing"
    target: str


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    scanned: int = 0
    manifest_used: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.findings else 0

    def count(self, kind: str) -> int:
        return sum(1 for f in self.findings if f.kind == kind)

    def files(self, kind: str) -> int:
        return len({f.file for f in self.findings if f.kind == kind})


def list_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        ).stdout
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-prefix"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        if top == "":  # root is the repository top, so tracked paths are tree-relative
            names = [n for n in out.decode().split("\0") if n]
            return sorted(n for n in names if (root / n).is_file())
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
    return sorted(found)


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
        if b"\0" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _clean_target(raw: str) -> str | None:
    t = raw.strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].strip()
    t = re.split(r"[#?]", t, maxsplit=1)[0]
    if not t or SCHEME_RE.match(t) or t.startswith("//"):
        return None
    return t


def _resolve(file_rel: str, target: str) -> str | None:
    if target.startswith("/"):
        joined = posixpath.normpath(target.lstrip("/"))
    else:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(file_rel), target))
    if joined.startswith("..") or joined == ".":
        return None  # leaves the tree or is the root itself
    return joined


def extract_refs(rel: str, text: str) -> list[tuple[int, str]]:
    """Return (line, tree-relative path) pairs for one file, deduplicated per line."""
    is_md = rel.lower().endswith((".md", ".markdown"))
    refs: list[tuple[int, str]] = []
    in_fence = False
    fence = ""
    for n, line in enumerate(text.splitlines(), 1):
        if IGNORE_MARK in line:
            continue
        found: list[str] = []
        if is_md:
            m = FENCE_RE.match(line)
            if m:
                marker = m.group(1)
                if not in_fence:
                    in_fence, fence = True, marker[0]
                elif marker[0] == fence:
                    in_fence = False
                continue
            if not in_fence:
                raw = [m.group(1) for m in LINK_RE.finditer(line)]
                d = REFDEF_RE.match(line)
                if d:
                    raw.append(d.group(1))
                for r in raw:
                    t = _clean_target(r)
                    if t and (p := _resolve(rel, t)):
                        found.append(p)
                for span in CODE_SPAN_RE.finditer(line):
                    s = span.group(1).strip()
                    if REL_SPAN_RE.match(s) and (p := _resolve(rel, s)):
                        found.append(p)
        for m in PATH_RE.finditer(line):
            if PATTERN_AFTER.match(line[m.end() : m.end() + 1] or " "):
                continue
            if re.search(r"(^|/)\.{3,}(/|$)", m.group(0)):
                continue  # `docs/.../x` is an elision in prose, not a path
            found.append(posixpath.normpath(m.group(0)))
        seen: set[str] = set()
        for p in found:
            if p not in seen:
                seen.add(p)
                refs.append((n, p))
    return refs


def check_tree(
    root: Path | str, manifest_path: Path | str | None = None, *, use_manifest: bool = True
) -> Result:
    root = Path(root)
    result = Result()
    manifest = Manifest()
    mp = Path(manifest_path) if manifest_path else (root / MANIFEST_REL if MANIFEST_REL else None)
    if use_manifest and mp is not None and mp.is_file():
        manifest = Manifest.load(mp)
        result.manifest_used = True
    files = list_files(root)
    for rel in files:
        parts = rel.split("/")
        if SKIP_DIRS.intersection(parts[:-1]) or parts[-1] in SKIP_NAMES:
            continue
        if manifest.is_excluded(rel):
            continue
        text = read_text(root / rel)
        if text is None:
            continue
        if any(IGNORE_FILE_MARK in ln for ln in text.splitlines()[:5]):
            continue
        result.scanned += 1
        for line, target in extract_refs(rel, text):
            if manifest.is_excluded(target):
                result.findings.append(Finding(rel, line, "excluded", target))
            elif not (root / target).exists():
                result.findings.append(Finding(rel, line, "missing", target))
    result.findings.sort(key=lambda f: (f.file, f.line, f.kind, f.target))
    return result


def format_report(result: Result, list_limit: int | None = None) -> str:
    out: list[str] = []
    shown = result.findings if list_limit is None else result.findings[:list_limit]
    for f in shown:
        out.append(f"{f.file}:{f.line}: {f.kind}: {f.target}")
    if len(shown) < len(result.findings):
        out.append(f"... {len(result.findings) - len(shown)} more findings not listed")
    out.append("")
    out.append(f"scanned {result.scanned} shipped files")
    if not result.manifest_used:
        out.append("no manifest: exclusion not checked, only missing targets")
    for kind in ("excluded", "missing"):
        out.append(f"{kind}: {result.count(kind)} references in {result.files(kind)} files")
    out.append("")
    out.append("Not checked:")
    out.extend(f"  - {item}" for item in NOT_CHECKED)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fail on links to excluded or missing files.")
    ap.add_argument("tree", nargs="?", default=".")
    default_note = f" (default: TREE/{MANIFEST_REL})" if MANIFEST_REL else ""
    ap.add_argument("--manifest", help=f"manifest TOML{default_note}")
    ap.add_argument("--report-only", action="store_true", help="always exit 0")
    ap.add_argument("--list-limit", type=int, help="list at most N findings (counts stay full)")
    args = ap.parse_args(argv)
    root = Path(args.tree)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    result = check_tree(root, args.manifest)
    print(format_report(result, args.list_limit))
    return 0 if args.report_only else result.exit_code


if __name__ == "__main__":
    sys.exit(main())
