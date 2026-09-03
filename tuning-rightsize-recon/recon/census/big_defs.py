#!/usr/bin/env python3
"""Table 3: functions > 150 lines and classes > 800 lines across scope files.

Usage: python3 big_defs.py <scope_files.txt> > table_3.md
"""
import ast
import sys
from pathlib import Path

REPO = Path("/home/user/JTS")


def node_line_count(node) -> int:
    end = getattr(node, "end_lineno", None)
    if end is None:
        return 0
    return end - node.lineno + 1


def qualify(stack):
    return ".".join(stack)


def walk(tree, path_rel, funcs, classes):
    stack = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                n = node_line_count(child)
                if n > 800:
                    classes.append((path_rel, qualify(stack + [child.name]), n, child.lineno))
                visit(child, stack + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n = node_line_count(child)
                if n > 150:
                    funcs.append((path_rel, qualify(stack + [child.name]), n, child.lineno))
                visit(child, stack + [child.name])
            else:
                visit(child, stack)

    visit(tree, stack)


def main():
    list_path = Path(sys.argv[1])
    files = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]
    funcs = []
    classes = []
    for rel in files:
        path = REPO / rel
        src = path.read_text(errors="replace")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        walk(tree, rel, funcs, classes)

    funcs.sort(key=lambda x: -x[2])
    classes.sort(key=lambda x: -x[2])

    print(f"### Table 3a — functions/methods > 150 lines ({len(funcs)} found)\n")
    print("| file | name | lines | starts at line |")
    print("|---|---|---:|---:|")
    for f, name, n, ln in funcs:
        print(f"| {f} | {name} | {n} | {ln} |")

    print(f"\n### Table 3b — classes > 800 lines ({len(classes)} found)\n")
    print("| file | name | lines | starts at line |")
    print("|---|---|---:|---:|")
    for f, name, n, ln in classes:
        print(f"| {f} | {name} | {n} | {ln} |")


if __name__ == "__main__":
    main()
