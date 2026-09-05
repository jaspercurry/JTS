# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def test_a_deleted_doc_falls_back_to_checking_the_whole_tree(tmp_path):
    """A delete has no post-diff content of its own to check, but an
    untouched file's now-broken inbound link must still be caught (#4036)."""
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "user.name", "JTS Tests")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "keep.md").write_text("[to a](a.md)\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    (tmp_path / "a.md").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "delete a.md")
    head = _git(tmp_path, "rev-parse", "HEAD")

    # keep.md is untouched by the diff, so a plain changed-files check would
    # never look at its now-broken link to the deleted a.md.
    assert docs_linkcheck.main(["--base", base, "--head", head]) == 1


def test_external_links_are_ignored(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    doc.write_text(
        "[External](https://example.com/nope#still-ignored)\n",
        encoding="utf-8",
    )

    assert docs_linkcheck.check_file(doc) == ()


def test_links_inside_code_are_ignored(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    doc.write_text(
        "```md\n[Missing](missing.md)\n```\n[External](https://example.com)\n"
        'Call `canonical_header("x",\n  back_href="/rooms/")` or `<a href="/rooms/">`.\n',
        encoding="utf-8",
    )

    assert docs_linkcheck.check_file(doc) == ()


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("target.md#hello-world", ()),
        ("target.md#no-such-anchor", ("markdown anchor missing",)),
        ("missing.md", ("local link target missing",)),
    ],
)
def test_wrapped_link_text_is_checked(tmp_path, target, expected):
    """A link whose text wraps at the prose margin was invisible to the
    scanner, so its target went unvalidated — see issue #2442. The passing
    case is the control."""

    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    doc.write_text(f"Prose before a [wrapped\nlink]({target}) and after.\n", encoding="utf-8")
    (tmp_path / "target.md").write_text("# Hello, World!\n", encoding="utf-8")

    assert tuple(issue.message for issue in docs_linkcheck.check_file(doc)) == expected
    assert docs_linkcheck.main(["--changed-file", "doc.md"]) == (1 if expected else 0)


def test_local_line_suffix_passes(tmp_path):
    docs_linkcheck = load_docs_linkcheck()
    docs_linkcheck.ROOT = tmp_path.resolve()
    doc = tmp_path / "doc.md"
    target = tmp_path / "target.py"
    doc.write_text("[Source](target.py:42)\n", encoding="utf-8")
    target.write_text("print('ok')\n", encoding="utf-8")

    assert docs_linkcheck.check_file(doc) == ()


def test_adr_numbers_are_unique() -> None:
    by_number: dict[str, list[str]] = {}
    for path in sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]*.md")):
        by_number.setdefault(path.name[:4], []).append(path.name)

    duplicates = {
        number: names
        for number, names in by_number.items()
        if len(names) > 1
    }

    assert duplicates == {}
