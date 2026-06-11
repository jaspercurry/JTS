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


def test_historical_tag_sits_immediately_under_the_title():
    tagged = [
        p for p in HANDOFFS
        if _HISTORICAL_TAG in p.read_text(encoding="utf-8")
    ]
    assert tagged, "expected at least one historical HANDOFF (rule 10)"
    offenders = []
    for p in tagged:
        lines = [
            ln for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        # First non-empty line is the H1; the tag opens the very next
        # non-empty line ("immediately under the H1 title").
        if not (
            lines
            and lines[0].startswith("# ")
            and len(lines) > 1
            and lines[1].startswith(_HISTORICAL_TAG)
        ):
            offenders.append(str(p.relative_to(ROOT)))
    assert offenders == [], (
        "historical HANDOFFs whose Status tag is not immediately under "
        "the H1 title (AGENTS.md doc rule 10):\n" + "\n".join(offenders)
    )
