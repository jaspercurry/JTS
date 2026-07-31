# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HANDOFF docs carry the freshness metadata the doc workflow runs on.

Pins two rules from AGENTS.md "Documentation paradigm":

* Rule 3 — "Every HANDOFF ends with `Last verified: YYYY-MM-DD`."
  `scripts/doc-freshness.sh` keys its staleness report off that footer
  (same `^Last verified: <date>` regex as here); a doc without one
  degrades the report to git-commit-date guesswork and is listed as a
  defect every run.
* Rule 10 — frozen session-pickup narratives are tagged with a
  `> **Status: historical.**` callout "immediately under the H1 title",
  so a reader hits the don't-trust-specific-facts warning before any
  content. A tag buried mid-file fails its one job.

  Rule 10 also admits one hybrid shape: a doc that is live at the top
  and carries a frozen campaign as an appendix. There the tag opens the
  appendix's own heading instead, and the H1 orientation note must warn
  the reader that part of the file is historical — so the warning still
  arrives before any content, just once per scope. Both shapes are
  checked below; a tag floating under neither a heading nor an
  orientation note is still the failure this test exists to catch.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Mirrors scripts/doc-freshness.sh's discovery (find docs -maxdepth 2
# -name 'HANDOFF-*.md'); rglob is a superset, which is what we want —
# a HANDOFF nested deeper would silently escape the freshness report.
HANDOFFS = tuple(sorted((ROOT / "docs").rglob("HANDOFF-*.md")))

# Same shape doc-freshness.sh greps for.
_FOOTER_RE = re.compile(r"^Last verified: \d{4}-\d{2}-\d{2}", re.M)
_HISTORICAL_TAG = "> **Status: historical.**"


def test_every_handoff_has_a_last_verified_footer():
    assert len(HANDOFFS) >= 10, "expected the HANDOFF corpus to scan"
    offenders = [
        str(p.relative_to(ROOT))
        for p in HANDOFFS
        if not _FOOTER_RE.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "HANDOFF docs missing the `Last verified: YYYY-MM-DD` footer "
        "(AGENTS.md doc rule 3; scripts/doc-freshness.sh reads it):\n"
        + "\n".join(offenders)
    )


def _tag_placement_ok(lines: list[str]) -> bool:
    """True when every historical tag warns a reader before its content.

    Two legal shapes, per rule 10:

    * whole-doc — the tag opens the line immediately under the H1;
    * appendix — the tag opens the line immediately under a section
      heading, and the H1 orientation note (the first non-empty line
      after the title) says part of the file is historical.

    Anything else is a tag a reader can walk straight past.
    """

    if not lines or not lines[0].startswith("# "):
        return False
    if len(lines) > 1 and lines[1].startswith(_HISTORICAL_TAG):
        return True
    # Appendix shape: the H1 note must itself flag the historical scope,
    # so the warning is not deferred to a section the reader may skip.
    # The note is the whole blockquote under the title, not just its
    # first line — these callouts routinely run several lines.
    note = []
    for ln in lines[1:]:
        if not ln.startswith(">"):
            break
        note.append(ln)
    if not note or "historical" not in " ".join(note).lower():
        return False
    return all(
        lines[i - 1].startswith("#")
        for i, ln in enumerate(lines)
        if i and ln.startswith(_HISTORICAL_TAG)
    )


def test_historical_tag_sits_immediately_under_the_title():
    tagged = [
        p for p in HANDOFFS
        if _HISTORICAL_TAG in p.read_text(encoding="utf-8")
    ]
    assert tagged, "expected at least one historical HANDOFF (rule 10)"
    offenders = [
        str(p.relative_to(ROOT))
        for p in tagged
        if not _tag_placement_ok(
            [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        )
    ]
    assert offenders == [], (
        "historical HANDOFFs whose Status tag warns nobody — it must open "
        "the line under the H1, or (appendix shape) the line under its own "
        "section heading with the H1 note flagging the historical scope "
        "(AGENTS.md doc rule 10):\n" + "\n".join(offenders)
    )


def test_a_mid_file_tag_under_no_heading_still_fails():
    """The guard's one job: a tag a reader can walk past is a defect.

    Pins the extension above against the obvious regression — loosening
    "immediately under the H1" into "anywhere, as long as the H1 says
    historical somewhere."
    """

    orientation = "> Part of this file is **historical**; see the appendix."
    assert _tag_placement_ok(["# T", orientation, "## Appendix", _HISTORICAL_TAG])
    # Same doc, tag floating in prose rather than opening a section.
    assert not _tag_placement_ok(
        ["# T", orientation, "## Appendix", "Some prose.", _HISTORICAL_TAG]
    )
    # Appendix shape without the H1 warning is still a buried tag.
    assert not _tag_placement_ok(
        ["# T", "> An ordinary note.", "## Appendix", _HISTORICAL_TAG]
    )
