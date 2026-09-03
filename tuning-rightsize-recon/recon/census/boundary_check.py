#!/usr/bin/env python3
"""Table 9: import-graph boundary check.

Checks (via ast, not text grep, to avoid string-literal false positives):
  A) any `from jasper.web import ...` / `import jasper.web[.x]` inside
     active_speaker/, audio_measurement/, correction/, attribution/
  B) any `from jasper.active_speaker import ...` or
     `from jasper.correction import ...` (or `import jasper.active_speaker...`
     / `import jasper.correction...`) inside audio_measurement/
  C) any import of `crossover_v2` (module or symbol) from correction/

Usage: python3 boundary_check.py <scope_files.txt> > table_9.md
"""
import ast
import sys
from pathlib import Path

REPO = Path("/home/user/JTS")

TRUTH_DIRS = ("jasper/active_speaker/", "jasper/audio_measurement/", "jasper/correction/", "jasper/attribution/")


def imports_of(tree):
    """Yield (kind, module_or_name) for each Import/ImportFrom in the module."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(("import", alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            level = node.level
            out.append(("from", ("." * level) + mod, node.lineno, [a.name for a in node.names]))
    return out


def main():
    list_path = Path(sys.argv[1])
    files = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]

    violations_a = []
    violations_b = []
    violations_c = []

    for rel in files:
        if not rel.startswith(TRUTH_DIRS):
            continue
        path = REPO / rel
        src = path.read_text(errors="replace")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue

        for entry in imports_of(tree):
            if entry[0] == "import":
                _, name, lineno = entry
                if name == "jasper.web" or name.startswith("jasper.web."):
                    violations_a.append((rel, lineno, f"import {name}"))
            else:
                _, mod, lineno, names = entry
                if mod == "jasper.web" or mod.startswith("jasper.web."):
                    violations_a.append((rel, lineno, f"from {mod} import {', '.join(names)}"))

        if rel.startswith("jasper/audio_measurement/"):
            for entry in imports_of(tree):
                if entry[0] == "import":
                    _, name, lineno = entry
                    if name.startswith("jasper.active_speaker") or name.startswith("jasper.correction"):
                        violations_b.append((rel, lineno, f"import {name}"))
                else:
                    _, mod, lineno, names = entry
                    if mod.startswith("jasper.active_speaker") or mod.startswith("jasper.correction"):
                        violations_b.append((rel, lineno, f"from {mod} import {', '.join(names)}"))

        if rel.startswith("jasper/correction/"):
            for entry in imports_of(tree):
                if entry[0] == "import":
                    _, name, lineno = entry
                    if "crossover_v2" in name:
                        violations_c.append((rel, lineno, f"import {name}"))
                else:
                    _, mod, lineno, names = entry
                    if "crossover_v2" in mod or any("crossover_v2" in n for n in names):
                        violations_c.append((rel, lineno, f"from {mod} import {', '.join(names)}"))

    print(f"### Table 9a — `jasper.web` imported from truth-layer dirs (active_speaker/audio_measurement/correction/attribution) — {len(violations_a)} found\n")
    print("| file | line | import |")
    print("|---|---:|---|")
    for f, ln, s in violations_a:
        print(f"| {f} | {ln} | `{s}` |")

    print(f"\n### Table 9b — `jasper.active_speaker` / `jasper.correction` imported from audio_measurement/ — {len(violations_b)} found\n")
    print("| file | line | import |")
    print("|---|---:|---|")
    for f, ln, s in violations_b:
        print(f"| {f} | {ln} | `{s}` |")

    print(f"\n### Table 9c — `crossover_v2` imported from correction/ — {len(violations_c)} found\n")
    print("| file | line | import |")
    print("|---|---:|---|")
    for f, ln, s in violations_c:
        print(f"| {f} | {ln} | `{s}` |")


if __name__ == "__main__":
    main()
