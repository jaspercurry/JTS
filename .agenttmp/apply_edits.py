"""Apply line-range replacements to a file.

Usage: python3 apply_edits.py <target.py> <edits.json>
edits.json: [[start, end, "replacement text or empty string"], ...]
start/end are 1-indexed inclusive line numbers in the ORIGINAL file.
Replacement text is inserted verbatim (no trailing newline needed).
Ranges must be disjoint; they are applied from the bottom up.
"""
import json
import sys

target, edits_path = sys.argv[1], sys.argv[2]
edits = json.load(open(edits_path))
lines = open(target, encoding="utf-8").read().split("\n")

edits.sort(key=lambda e: e[0])
for i in range(1, len(edits)):
    if edits[i][0] <= edits[i - 1][1]:
        raise SystemExit(f"overlapping edits: {edits[i - 1][:2]} and {edits[i][:2]}")

for start, end, repl in reversed(edits):
    new = repl.split("\n") if repl != "" else []
    lines[start - 1:end] = new

open(target, "w", encoding="utf-8").write("\n".join(lines))
print(f"applied {len(edits)} edits to {target}")
