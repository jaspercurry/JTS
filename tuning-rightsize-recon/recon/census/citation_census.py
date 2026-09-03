#!/usr/bin/env python3
"""Table 7: citation / stale-language census, per file (top 30 each).

- count of #\\d{3,4} issue citations
- count of ADR-\\d{4} citations
- count of lines containing any of a stale-language wordlist (case-sens for
  SUPERSEDED as its own token, case-insensitive overall list per BRIEF)

Usage: python3 citation_census.py <scope_files.txt> > table_7.md
"""
import re
import sys
from pathlib import Path

REPO = Path("/home/user/JTS")

ISSUE_RE = re.compile(r"#\d{3,4}\b")
ADR_RE = re.compile(r"ADR-\d{4}")
STALE_WORDS = [
    "superseded", "owner ruling", "ruling", "historically", "used to",
    "no longer", "legacy", "deprecated", "archaeology", "kept for",
]
STALE_RE = re.compile("|".join(re.escape(w) for w in STALE_WORDS), re.IGNORECASE)


def main():
    list_path = Path(sys.argv[1])
    files = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]

    issue_counts = {}
    adr_counts = {}
    stale_counts = {}

    for rel in files:
        path = REPO / rel
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        lines = text.splitlines()
        issue_hits = sum(len(ISSUE_RE.findall(l)) for l in lines)
        adr_hits = sum(len(ADR_RE.findall(l)) for l in lines)
        stale_hits = sum(1 for l in lines if STALE_RE.search(l))
        if issue_hits:
            issue_counts[rel] = issue_hits
        if adr_hits:
            adr_counts[rel] = adr_hits
        if stale_hits:
            stale_counts[rel] = stale_hits

    print(f"### Table 7a — `#NNN` issue citations per file, top 30 (total {sum(issue_counts.values())} across {len(issue_counts)} files)\n")
    print("| file | count |")
    print("|---|---:|")
    for f in sorted(issue_counts, key=lambda k: -issue_counts[k])[:30]:
        print(f"| {f} | {issue_counts[f]} |")

    print(f"\n### Table 7b — `ADR-NNNN` citations per file, top 30 (total {sum(adr_counts.values())} across {len(adr_counts)} files)\n")
    print("| file | count |")
    print("|---|---:|")
    for f in sorted(adr_counts, key=lambda k: -adr_counts[k])[:30]:
        print(f"| {f} | {adr_counts[f]} |")

    print(f"\n### Table 7c — stale-language lines per file, top 30 (total {sum(stale_counts.values())} across {len(stale_counts)} files)\n")
    print("(wordlist: " + ", ".join(STALE_WORDS) + ", case-insensitive; one line counted once even if it matches multiple words)\n")
    print("| file | lines matched |")
    print("|---|---:|")
    for f in sorted(stale_counts, key=lambda k: -stale_counts[k])[:30]:
        print(f"| {f} | {stale_counts[f]} |")


if __name__ == "__main__":
    main()
