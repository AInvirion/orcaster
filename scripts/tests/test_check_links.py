"""Tests of scripts/check_links.py on synthetic trees in tmp_path.
link-check: ignore-file

The names of excluded paths are built in parts (`DOCS`, `ADR`) so that this
file does not count as a reference when the checker walks the repo.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_links  # noqa: E402

DOCS = "docs"
ADR = DOCS + "/" + "adr"
MANIFEST_FILE = "export-manifest.toml"
# The fixture manifest lives inside the synthetic tree, so it opts out of the scan
# (its own `docs/...` entries are not references).
MANIFEST = f"""# link-check: ignore-file
allow = ["README.md", "docs/guide.md", "src/", "ops/release/"]
exclude = ["{ADR}/", "{DOCS}/internal.md"]
"""


def make_tree(root: Path, files: dict[str, str], manifest: str | None = MANIFEST) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if manifest is not None:
        (root / MANIFEST_FILE).write_text(manifest, encoding="utf-8")
    return root


def manifest_arg(root: Path) -> Path | None:
    """The fixture manifest of `root` when make_tree wrote one, else None."""
    m = root / MANIFEST_FILE
    return m if m.is_file() else None


def run(root: Path, **kw):
    kw.setdefault("manifest_path", manifest_arg(root))
    return check_links.check_tree(root, **kw)


def kinds(result):
    return sorted((f.file, f.kind, f.target) for f in result.findings)


BASE = {
    f"{ADR}/0001.md": "# adr\n",
    "docs/guide.md": "# guide\n",
    "README.md": "# r\n",
}


def test_clean_tree_passes(tmp_path):
    make_tree(tmp_path, {**BASE, "README.md": "[g](docs/guide.md) ![i](docs/guide.md#x)\n"})
    r = run(tmp_path)
    assert r.findings == []
    assert r.exit_code == 0


def test_markdown_link_to_excluded(tmp_path):
    make_tree(tmp_path, {**BASE, "README.md": f"see [adr]({ADR}/0001.md)\n"})
    r = run(tmp_path)
    assert kinds(r) == [("README.md", "excluded", f"{ADR}/0001.md")]
    assert r.exit_code == 1


def test_markdown_link_relative_to_file_dir(tmp_path):
    make_tree(tmp_path, {**BASE, "docs/guide.md": "[a](adr/0001.md) [b](../README.md)\n"})
    r = run(tmp_path)
    assert kinds(r) == [("docs/guide.md", "excluded", f"{ADR}/0001.md")]


def test_markdown_link_missing(tmp_path):
    make_tree(tmp_path, {**BASE, "README.md": "[x](docs/nope.md) ![y](img/a.png)\n"})
    r = run(tmp_path)
    assert kinds(r) == [
        ("README.md", "missing", "docs/nope.md"),
        ("README.md", "missing", "img/a.png"),
    ]


def test_urls_anchors_mailto_ignored(tmp_path):
    text = "[a](https://x.org/docs/y) [b](#sec) [c](mailto:a@b.c) [d](docs/guide.md#frag)\n"
    make_tree(tmp_path, {**BASE, "README.md": text})
    assert run(tmp_path).findings == []


def test_reference_definition_and_title_and_angle(tmp_path):
    text = f'[a][1]\n\n[1]: {ADR}/0001.md "t"\n[b](<docs/nope.md> "t")\n'
    make_tree(tmp_path, {**BASE, "README.md": text})
    r = run(tmp_path)
    assert kinds(r) == [
        ("README.md", "excluded", f"{ADR}/0001.md"),
        ("README.md", "missing", "docs/nope.md"),
    ]


def test_links_in_code_fences_are_not_links(tmp_path):
    text = "```\n[a](x/nope.md)\n```\n"
    make_tree(tmp_path, {**BASE, "README.md": text})
    assert run(tmp_path).findings == []


def test_path_string_in_code_comment_and_message(tmp_path):
    files = {
        **BASE,
        "src/a.py": f'# see {ADR}/0001.md\nprint("run ops/release/x.sh or {DOCS}/internal.md")\n',
        "ops/release/x.sh": "#!/bin/sh\n",
    }
    make_tree(tmp_path, files)
    r = run(tmp_path)
    assert kinds(r) == [
        ("src/a.py", "excluded", f"{ADR}/0001.md"),
        ("src/a.py", "excluded", f"{DOCS}/internal.md"),
    ]
    assert [f.line for f in r.findings] == [1, 2]


def test_path_string_missing_and_patterns_skipped(tmp_path):
    files = {
        **BASE,
        "src/a.sh": "# ops/release/gone.sh\n# ops/release/*.sh ops/<tool>/x ops/${V}/y\n",
        "ops/release/x.sh": "",
    }
    make_tree(tmp_path, files)
    r = run(tmp_path)
    assert kinds(r) == [("src/a.sh", "missing", "ops/release/gone.sh")]


def test_elided_path_skipped(tmp_path):
    make_tree(tmp_path, {**BASE, "src/a.py": "# docs/.../design and ops/.../x\n"})
    assert run(tmp_path).findings == []


def test_url_and_embedded_path_not_matched(tmp_path):
    files = {**BASE, "src/a.py": "# https://e.org/docs/zzz  /opt/x/docs/zzz  a-docs/zzz\n"}
    make_tree(tmp_path, files)
    assert run(tmp_path).findings == []


def test_relative_code_span_in_markdown(tmp_path):
    make_tree(tmp_path, {**BASE, "docs/guide.md": "use `./nope.md` and `../README.md`\n"})
    r = run(tmp_path)
    assert kinds(r) == [("docs/guide.md", "missing", "docs/nope.md")]


def test_excluded_files_are_not_scanned(tmp_path):
    make_tree(tmp_path, {**BASE, f"{ADR}/0002.md": "[x](nope.md) docs/nope.md\n"})
    assert run(tmp_path).findings == []


def test_excluded_directory_reference(tmp_path):
    make_tree(tmp_path, {**BASE, "README.md": f"see `{ADR}/`\nand `{ADR}`\n"})
    r = run(tmp_path)
    assert [f.kind for f in r.findings] == ["excluded", "excluded"]


def test_ignore_file_marker(tmp_path):
    text = f"<!-- link-check: ignore-file -->\n[a]({ADR}/0001.md)\n"
    make_tree(tmp_path, {**BASE, "README.md": text})
    assert run(tmp_path).findings == []


def test_ignore_marker(tmp_path):
    make_tree(tmp_path, {**BASE, "src/a.py": f"# {ADR}/0001.md  link-check: ignore\n"})
    assert run(tmp_path).findings == []


def test_no_manifest_only_checks_missing(tmp_path):
    files = {"a/README.md": "[x](c/0001.md) [y](b.md)\n", "a/b.md": ""}
    make_tree(tmp_path, files, manifest=None)
    r = run(tmp_path)
    assert kinds(r) == [("a/README.md", "missing", "a/c/0001.md")]
    assert r.manifest_used is False


def test_without_default_manifest_only_checks_missing(tmp_path, monkeypatch):
    # There is no default manifest (MANIFEST_REL is None) and the CLI is
    # given no --manifest: exclusion is not checked, only missing targets.
    monkeypatch.setattr(check_links, "MANIFEST_REL", None)
    make_tree(tmp_path, {**BASE, "README.md": f"[a]({ADR}/0001.md) [b](docs/nope.md)\n"})
    r = check_links.check_tree(tmp_path)
    assert r.manifest_used is False
    assert kinds(r) == [("README.md", "missing", "docs/nope.md")]


def test_counts_and_summary(tmp_path):
    files = {
        **BASE,
        "README.md": f"[a]({ADR}/0001.md)\n[b]({ADR}/0001.md)\n[c](docs/nope.md)\n",
        "docs/guide.md": "[a](adr/0001.md)\n",
    }
    make_tree(tmp_path, files)
    r = run(tmp_path)
    assert (r.count("excluded"), r.files("excluded")) == (3, 2)
    assert (r.count("missing"), r.files("missing")) == (1, 1)
    out = check_links.format_report(r)
    assert "excluded: 3 references in 2 files" in out
    assert "missing: 1 references in 1 files" in out
    assert "Not checked" in out


def test_cli_exit_codes(tmp_path):
    make_tree(tmp_path, {**BASE, "README.md": f"[a]({ADR}/0001.md)\n"})
    script = Path(check_links.__file__)
    manifest = str(manifest_arg(tmp_path))
    p = subprocess.run(
        [sys.executable, "-I", str(script), str(tmp_path), "--manifest", manifest],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 1
    assert "README.md:1" in p.stdout
    ok = subprocess.run(
        [sys.executable, "-I", str(script), str(tmp_path), "--manifest", manifest, "--report-only"],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0


@pytest.mark.parametrize(
    "entry,path,expected",
    [
        (f"{ADR}/", f"{ADR}/x.md", True),
        (f"{DOCS}/internal.md", f"{DOCS}/internal.md", True),
        (f"{DOCS}/internal.md", f"{DOCS}/internal.md.bak", False),
    ],
)
def test_manifest_matching(entry, path, expected):
    m = check_links.Manifest(allow=["docs/"], exclude=[entry])
    assert m.is_excluded(path) is expected
