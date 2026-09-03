"""Dump every docstring and comment block of a file with line ranges and context."""
import ast, io, sys, tokenize
from pathlib import Path

path = sys.argv[1]
src = Path(path).read_text()
lines = src.splitlines()
tree = ast.parse(src)

blocks = []  # (start, end, kind, owner)
for n in ast.walk(tree):
    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        b = n.body
        if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
            owner = "MODULE" if isinstance(n, ast.Module) else n.name
            blocks.append((b[0].lineno, b[0].end_lineno, "DOC", owner))

comment_lines = []
for tok in tokenize.generate_tokens(io.StringIO(src).readline):
    if tok.type == tokenize.COMMENT:
        comment_lines.append(tok.start[0])
comment_lines.sort()
run = []
for ln in comment_lines:
    if run and ln == run[-1] + 1:
        run.append(ln)
    else:
        if run:
            blocks.append((run[0], run[-1], "COM", ""))
        run = [ln]
if run:
    blocks.append((run[0], run[-1], "COM", ""))

blocks.sort()
for start, end, kind, owner in blocks:
    n = end - start + 1
    print(f"@@ {start}-{end} {kind} n={n} {owner}")
    for i in range(start, end + 1):
        print(f"{i}| {lines[i-1]}")
    # one line of following code for context
    j = end
    shown = 0
    while j < len(lines) and shown < 2:
        j += 1
        if j <= len(lines) and lines[j-1].strip():
            print(f">> {j}| {lines[j-1]}")
            shown += 1
    print()
