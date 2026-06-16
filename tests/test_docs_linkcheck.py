from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_docs_linkcheck():
    path = ROOT / "scripts" / "docs-linkcheck.py"
    spec = importlib.util.spec_from_file_location("docs_linkcheck", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_local_file_and_anchor_pass(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    target = tmp_path / "target.md"
    doc.write_text("[Target](target.md#hello-world)\n", encoding="utf-8")
    target.write_text("# Hello, World!\n", encoding="utf-8")

    assert docs_linkcheck.check_file(doc) == ()


def test_all_markdown_files_excludes_vendored_dirs(tmp_path):
    """`--all` must check the repo's own docs, not third-party Markdown under
    .venv/site-packages or node_modules — otherwise a populated venv injects
    false link failures (the openai SDK's docs were the real-world offender)."""

    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()

    (tmp_path / "docs").mkdir()
    repo_doc = tmp_path / "docs" / "real.md"
    repo_doc.write_text("# Real\n", encoding="utf-8")

    vendored = tmp_path / ".venv" / "lib" / "site-packages" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "README.md").write_text("[broken](does-not-exist.md)\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.md").write_text("# dep\n", encoding="utf-8")

    found = {p.resolve() for p in docs_linkcheck.all_markdown_files()}

    assert repo_doc.resolve() in found
    assert not any(
        ".venv" in p.parts or "node_modules" in p.parts for p in found
    )


def test_missing_local_file_fails(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    doc.write_text("[Missing](missing.md)\n", encoding="utf-8")

    issues = docs_linkcheck.check_file(doc)

    assert len(issues) == 1
    assert issues[0].message == "local link target missing"


def test_missing_anchor_fails(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    target = tmp_path / "target.md"
    doc.write_text("[Target](target.md#not-here)\n", encoding="utf-8")
    target.write_text("# Different Heading\n", encoding="utf-8")

    issues = docs_linkcheck.check_file(doc)

    assert len(issues) == 1
    assert issues[0].message == "markdown anchor missing"


def test_external_links_are_ignored(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    doc.write_text(
        "[External](https://example.com/nope#still-ignored)\n",
        encoding="utf-8",
    )

    assert docs_linkcheck.check_file(doc) == ()


def test_links_inside_fenced_code_are_ignored(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    doc.write_text(
        "```md\n[Missing](missing.md)\n```\n[External](https://example.com)\n",
        encoding="utf-8",
    )

    assert docs_linkcheck.check_file(doc) == ()


def test_local_line_suffix_passes(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    target = tmp_path / "target.py"
    doc.write_text("[Source](target.py:42)\n", encoding="utf-8")
    target.write_text("print('ok')\n", encoding="utf-8")

    assert docs_linkcheck.check_file(doc) == ()
