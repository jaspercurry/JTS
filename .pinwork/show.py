"""Print the enclosing top-level def for each given file:line."""

import ast
import sys
from pathlib import Path


def enclosing(path: Path, lineno: int):
    tree = ast.parse(path.read_text())
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                if best is None or node.lineno > best.lineno:
                    best = node
    return best


for arg in sys.argv[1:]:
    fn, _, ln = arg.rpartition(":")
    p = Path(fn)
    node = enclosing(p, int(ln))
    lines = p.read_text().splitlines()
    if node is None:
        print(f"=== {fn}:{ln} (module level) ===")
        lo = max(0, int(ln) - 8)
        print("\n".join(lines[lo : int(ln) + 6]))
        continue
    start = node.lineno - 1
    for d in node.decorator_list:
        start = min(start, d.lineno - 1)
    print(f"=== {fn}:{node.lineno}-{node.end_lineno} {node.name} ===")
    print("\n".join(lines[start : node.end_lineno]))
    print()
