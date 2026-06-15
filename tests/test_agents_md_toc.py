"""Keep AGENTS.md's anchor table of contents complete and live.

AGENTS.md (PR #698) grew a "## Contents" anchor ToC under its canonical
banner. docs-linkcheck already catches a *dangling* ToC anchor (a link to a
heading that does not exist), but drift is asymmetric: ADDING a new `## ` H2
with no matching ToC entry passes that check silently, so the ToC rots out of
completeness over time. These tests pin both directions:

  (a) every `## ` H2 heading (except "Contents" itself) has a ToC entry, and
  (b) every anchor the ToC links to resolves to a real heading in the file.

The anchor algorithm is the repo's own (scripts/docs-linkcheck.py), loaded the
same way tests/test_docs_linkcheck.py loads it — the script is not an
installable package.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = ROOT / "AGENTS.md"


def load_docs_linkcheck():
    path = ROOT / "scripts" / "docs-linkcheck.py"
    spec = importlib.util.spec_from_file_location("docs_linkcheck", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _h2_headings(docs_linkcheck, path: Path) -> list[str]:
    """Return the text of every level-2 (`## `) heading, fenced code skipped."""
    headings: list[str] = []
    for _line_no, line in docs_linkcheck.iter_non_fenced_lines(path):
        match = docs_linkcheck.HEADING_RE.match(line)
        if match and len(match.group(1)) == 2:
            headings.append(match.group(2))
    return headings


def _toc_anchors(docs_linkcheck, path: Path) -> list[str]:
    """Return the anchor slugs the "## Contents" ToC links to.

    The ToC is the list-item links between the "## Contents" heading and the
    first horizontal rule (`---`) that follows it.
    """
    anchors: list[str] = []
    in_toc = False
    for _line_no, line in docs_linkcheck.iter_non_fenced_lines(path):
        heading = docs_linkcheck.HEADING_RE.match(line)
        if heading and docs_linkcheck.markdown_anchor_slug(heading.group(2)) == "contents":
            in_toc = True
            continue
        if not in_toc:
            continue
        if line.strip() == "---":
            break
        for link in docs_linkcheck.INLINE_LINK_RE.finditer(line):
            target = docs_linkcheck.clean_target(link.group(1))
            if target.startswith("#"):
                anchors.append(target[1:].lower())
    return anchors


def test_every_h2_has_a_toc_entry():
    docs_linkcheck = load_docs_linkcheck()
    headings = _h2_headings(docs_linkcheck, AGENTS_MD)
    toc_anchors = set(_toc_anchors(docs_linkcheck, AGENTS_MD))

    assert toc_anchors, "AGENTS.md ToC parsed empty — parser or document drift"

    missing = []
    for heading in headings:
        slug = docs_linkcheck.markdown_anchor_slug(heading)
        if slug == "contents":
            continue
        if slug not in toc_anchors:
            missing.append(heading)

    assert not missing, (
        "AGENTS.md '## Contents' ToC is missing an entry for these H2 "
        f"section(s): {missing}. Add a matching link under '## Contents'."
    )


def test_every_toc_anchor_resolves_to_a_heading():
    docs_linkcheck = load_docs_linkcheck()
    heading_anchors = docs_linkcheck.anchors_for(AGENTS_MD)
    toc_anchors = _toc_anchors(docs_linkcheck, AGENTS_MD)

    assert toc_anchors, "AGENTS.md ToC parsed empty — parser or document drift"

    dangling = [anchor for anchor in toc_anchors if anchor not in heading_anchors]

    assert not dangling, (
        "AGENTS.md '## Contents' ToC links to anchor(s) with no matching "
        f"heading: {dangling}."
    )
