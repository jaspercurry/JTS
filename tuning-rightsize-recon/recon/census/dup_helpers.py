#!/usr/bin/env python3
"""Table 4: duplicate-helper census.

Finds every top-level function AND method definition whose name matches one of
the target patterns, groups by exact name, and for each group with >=2
definitions reports byte-identical / near-identical (difflib ratio > 0.8) /
different, using the def's source segment (via ast end_lineno) with the
signature line and body, whitespace-normalized only by dedent.

Usage: python3 dup_helpers.py <scope_files.txt> > table_4.md
"""
import ast
import difflib
import re
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

REPO = Path("/home/user/JTS")

PATTERNS = [
    re.compile(r"^_text$"),
    re.compile(r"^_sha256"),
    re.compile(r".*fingerprint.*", re.IGNORECASE),
    re.compile(r"^_mapping$"),
    re.compile(r"^_float$"),
    re.compile(r"^_int$"),
    re.compile(r"^_str$"),
    re.compile(r"^_bool$"),
    re.compile(r"^_iso"),
    re.compile(r"^_now"),
    re.compile(r"^_utc"),
    re.compile(r"^_json"),
    re.compile(r"^_read_json"),
    re.compile(r"^_write_json"),
    re.compile(r"^_atomic"),
    re.compile(r"^_load"),
    re.compile(r"^_dump"),
]


def matches(name: str) -> bool:
    return any(p.match(name) for p in PATTERNS)


def get_source_segment(src_lines, node) -> str:
    start = node.lineno - 1
    end = node.end_lineno
    seg = "\n".join(src_lines[start:end])
    try:
        return textwrap.dedent(seg)
    except Exception:
        return seg


def classify_pair(a: str, b: str) -> str:
    if a == b:
        return "identical"
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio > 0.8:
        return f"near-identical ({ratio:.2f})"
    return "different"


def main():
    list_path = Path(sys.argv[1])
    files = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]

    # name -> list of (file, lineno, source_text)
    groups = defaultdict(list)

    for rel in files:
        path = REPO / rel
        src = path.read_text(errors="replace")
        src_lines = src.splitlines()
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and matches(node.name):
                seg = get_source_segment(src_lines, node)
                groups[node.name].append((rel, node.lineno, seg))

    print(f"### Table 4 — duplicate-helper census ({sum(1 for v in groups.values() if len(v) > 1)} names with >=2 defs; {len(groups)} distinct matching names total)\n")

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    for name in sorted(dup_groups, key=lambda k: -len(dup_groups[k])):
        defs = dup_groups[name]
        print(f"#### `{name}` — {len(defs)} definitions\n")
        print("| file | line |")
        print("|---|---:|")
        for f, ln, _ in defs:
            print(f"| {f} | {ln} |")
        # pairwise comparison summary vs. the first def (representative), plus overall
        bodies = [seg for _, _, seg in defs]
        identical_groups = []
        seen = [False] * len(bodies)
        for i in range(len(bodies)):
            if seen[i]:
                continue
            cluster = [i]
            seen[i] = True
            for j in range(i + 1, len(bodies)):
                if seen[j]:
                    continue
                if bodies[i] == bodies[j]:
                    cluster.append(j)
                    seen[j] = True
            identical_groups.append(cluster)
        if len(identical_groups) == 1:
            verdict = f"byte-identical across all {len(bodies)}"
        else:
            # check near-identical across cluster representatives
            reps = [c[0] for c in identical_groups]
            near_all = True
            for i in range(len(reps)):
                for j in range(i + 1, len(reps)):
                    ratio = difflib.SequenceMatcher(None, bodies[reps[i]], bodies[reps[j]]).ratio()
                    if ratio <= 0.8:
                        near_all = False
            sizes = ", ".join(str(len(c)) for c in identical_groups)
            if near_all:
                verdict = f"{len(identical_groups)} identical-clusters (sizes {sizes}), near-identical (ratio>0.8) to each other"
            else:
                verdict = f"{len(identical_groups)} identical-clusters (sizes {sizes}), bodies differ meaningfully"
        print(f"\nVerdict: **{verdict}**\n")

    single = {k: v for k, v in groups.items() if len(v) == 1}
    if single:
        print(f"#### Single-definition matches (no duplication) — {len(single)} names\n")
        print("| name | file | line |")
        print("|---|---|---:|")
        for name in sorted(single):
            f, ln, _ = single[name][0]
            print(f"| `{name}` | {f} | {ln} |")


if __name__ == "__main__":
    main()
