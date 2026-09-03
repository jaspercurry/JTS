#!/usr/bin/env python3
"""Table 8: __all__ census.

For each scope file defining a module-level __all__ list/tuple of string
literals: report the count of names, and how many of those names have zero
importers outside the defining file — checked via whole-repo grep for
`from <module> import <name>` (or `import *`) and `<module>.<name>` where
<module> is the dotted module path.

Usage: python3 all_census.py <scope_files.txt> > table_8.md
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/user/JTS")


def module_dotted_path(rel: str) -> str:
    p = Path(rel)
    if p.name == "__init__.py":
        parts = p.parent.parts
    else:
        parts = p.with_suffix("").parts
    return ".".join(parts)


def find_all_names(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        names = []
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                names.append(elt.value)
                        return names
    return None


def main():
    list_path = Path(sys.argv[1])
    files = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]

    rows = []  # (file, num_names, unused_names list)
    for rel in files:
        path = REPO / rel
        src = path.read_text(errors="replace")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        names = find_all_names(tree)
        if names is None:
            continue
        mod = module_dotted_path(rel)
        # Candidate files that import from this module at all (any style, incl.
        # multi-line parenthesized imports and `import *`) — cheap first pass.
        mod_escaped = mod.replace(".", r"\.")
        candidate_proc = subprocess.run(
            ["rg", "-l", "-e", rf"from {mod_escaped} import", "-g", "*.py", str(REPO)],
            capture_output=True, text=True,
        )
        candidate_files = [Path(p) for p in candidate_proc.stdout.split() if Path(p).resolve() != path.resolve()]
        star_import_files = set()
        import_block_text = {}
        for cf in candidate_files:
            try:
                ctext = cf.read_text(errors="replace")
            except Exception:
                continue
            for m in re.finditer(rf"from {mod_escaped} import\s*(\([^)]*\)|[^\n]+)", ctext):
                block = m.group(1)
                if "*" in block:
                    star_import_files.add(cf)
                import_block_text.setdefault(cf, []).append(block)

        unused = []
        for name in names:
            hit = False
            if star_import_files:
                hit = True
            if not hit:
                name_re = re.compile(rf"\b{re.escape(name)}\b")
                for cf, blocks in import_block_text.items():
                    if any(name_re.search(b) for b in blocks):
                        hit = True
                        break
            if not hit:
                # attribute-style usage: module.name (dotted access after `import module`)
                attr_proc = subprocess.run(
                    ["rg", "-l", "-F", f"{mod}.{name}", "-g", "*.py", str(REPO)],
                    capture_output=True, text=True,
                )
                hit_files = {p for p in attr_proc.stdout.split() if Path(p).resolve() != path.resolve()}
                hit = bool(hit_files)
            if not hit:
                unused.append(name)
        rows.append((rel, len(names), unused))

    rows.sort(key=lambda r: -r[1])
    total_names = sum(r[1] for r in rows)
    total_unused = sum(len(r[2]) for r in rows)

    print(f"### Table 8 — `__all__` census ({len(rows)} files declare `__all__`; {total_names} names total; {total_unused} with zero outside importers)\n")
    print("| file | names in __all__ | names with zero outside importers |")
    print("|---|---:|---:|")
    for rel, n, unused in rows:
        print(f"| {rel} | {n} | {len(unused)} |")

    print("\n#### Detail — unused names by file\n")
    for rel, n, unused in rows:
        if unused:
            print(f"- `{rel}`: {', '.join(unused)}")


if __name__ == "__main__":
    main()
