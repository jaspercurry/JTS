#!/usr/bin/env python3
"""Table 5: refusal-vocabulary census.

Counts, per file:
  - defs (function/method) named _refuse*, _refused*, _gate*, _issue*,
    _blocked*, _disclose*, _verdict*
  - module-level constants matching ^(REFUSE|REASON|VERDICT|PHASE|NULL|SCREEN|
    CODE|STATUS)_[A-Z0-9_]+ =
  - classes whose name ends in Error/Refused/Refusal/Blocked/Failure/Exception

For the exception-like classes: list with base class, and whether __init__
body is identical (source text) to another's.

Usage: python3 refusal_census.py <scope_files.txt> > table_5.md
"""
import ast
import re
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

REPO = Path("/home/user/JTS")

DEF_PATTERNS = [
    re.compile(r"^_refuse"),
    re.compile(r"^_refused"),
    re.compile(r"^_gate"),
    re.compile(r"^_issue"),
    re.compile(r"^_blocked"),
    re.compile(r"^_disclose"),
    re.compile(r"^_verdict"),
]
CONST_RE = re.compile(r"^(REFUSE|REASON|VERDICT|PHASE|NULL|SCREEN|CODE|STATUS)_[A-Z0-9_]+$")
CLASS_SUFFIXES = ("Error", "Refused", "Refusal", "Blocked", "Failure", "Exception")


def def_matches(name: str) -> bool:
    return any(p.match(name) for p in DEF_PATTERNS)


def base_name(base) -> str:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return ast.dump(base)


def get_init_source(cls_node, src_lines):
    for item in cls_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
            start = item.lineno - 1
            end = item.end_lineno
            seg = "\n".join(src_lines[start:end])
            return textwrap.dedent(seg)
    return None


def main():
    list_path = Path(sys.argv[1])
    files = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]

    per_file_defs = defaultdict(int)
    per_file_consts = defaultdict(int)
    per_file_classes = defaultdict(int)
    total_defs = 0
    total_consts = 0
    exception_classes = []  # (file, name, base, init_src)

    def_names_by_file = defaultdict(list)
    const_names_by_file = defaultdict(list)
    # per-pattern breakdown: pattern label -> set(files), count
    pattern_files = defaultdict(set)
    pattern_count = defaultdict(int)

    def pattern_label(name):
        # Prefixes overlap (_refuse is a prefix of _refused): pick the longest
        # matching literal prefix so a def is attributed to its most specific
        # bucket instead of always the first pattern in DEF_PATTERNS.
        candidates = [p.pattern.lstrip("^") for p in DEF_PATTERNS if p.match(name)]
        if not candidates:
            return "?"
        return max(candidates, key=len)

    for rel in files:
        path = REPO / rel
        src = path.read_text(errors="replace")
        src_lines = src.splitlines()
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and def_matches(node.name):
                per_file_defs[rel] += 1
                total_defs += 1
                def_names_by_file[rel].append(node.name)
                lbl = pattern_label(node.name)
                pattern_files[lbl].add(rel)
                pattern_count[lbl] += 1
            elif isinstance(node, ast.ClassDef):
                if node.name.endswith(CLASS_SUFFIXES):
                    per_file_classes[rel] += 1
                    bases = [base_name(b) for b in node.bases]
                    init_src = get_init_source(node, src_lines)
                    exception_classes.append((rel, node.name, ", ".join(bases) or "object", init_src))

        # module-level constant assignments only (top-level Assign nodes)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and CONST_RE.match(target.id):
                        per_file_consts[rel] += 1
                        total_consts += 1
                        const_names_by_file[rel].append(target.id)

    print("### Table 5a0 — refusal-vocabulary defs, per naming pattern\n")
    print("| pattern | definitions | distinct files |")
    print("|---|---:|---:|")
    for lbl in sorted(pattern_count, key=lambda k: -pattern_count[k]):
        print(f"| `{lbl}` | {pattern_count[lbl]} | {len(pattern_files[lbl])} |")

    print("\n### Table 5a — refusal-vocabulary def counts per file\n")
    all_files_with_any = sorted(
        set(per_file_defs) | set(per_file_consts) | set(per_file_classes),
        key=lambda f: -(per_file_defs.get(f, 0) + per_file_consts.get(f, 0) + per_file_classes.get(f, 0)),
    )
    print("| file | refusal-vocab defs | REFUSE/REASON/... consts | Error/Refused/.../Exception classes |")
    print("|---|---:|---:|---:|")
    for f in all_files_with_any:
        print(f"| {f} | {per_file_defs.get(f,0)} | {per_file_consts.get(f,0)} | {per_file_classes.get(f,0)} |")
    print(f"| **TOTAL** | {total_defs} | {total_consts} | {len(exception_classes)} |")

    print(f"\n(defs matched across {len(set(per_file_defs))} files; consts matched across {len(set(per_file_consts))} files)\n")

    print(f"\n### Table 5b — exception-like classes ({len(exception_classes)}), base class, __init__ dup check\n")
    # group by init source text
    init_groups = defaultdict(list)
    for f, name, base, init_src in exception_classes:
        key = init_src if init_src is not None else None
        init_groups[key].append((f, name, base))

    print("| file | class | base | __init__ identical to |")
    print("|---|---|---|---|")
    # map each class to a representative group id
    group_id_of = {}
    gid = 0
    for key, members in init_groups.items():
        if key is None:
            continue
        gid += 1
        for f, name, base in members:
            group_id_of[(f, name)] = (gid, len(members))

    for f, name, base, init_src in exception_classes:
        if init_src is None:
            dup_note = "(no explicit __init__)"
        else:
            gid_, size = group_id_of[(f, name)]
            dup_note = f"group #{gid_} ({size} identical)" if size > 1 else "unique __init__"
        print(f"| {f} | {name} | {base} | {dup_note} |")

    dup_group_count = sum(1 for members in init_groups.values() if len(members) > 1)
    print(f"\n{dup_group_count} groups of classes share byte-identical `__init__` bodies.\n")


if __name__ == "__main__":
    main()
