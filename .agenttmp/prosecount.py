#!/usr/bin/env python3
"""Prose census by absolute path: total/code/docstring/comment/blank."""
import ast, sys, tokenize
from pathlib import Path


def doc_lines(tree):
    out = set()
    def consider(body):
        if not body: return
        f = body[0]
        if isinstance(f, ast.Expr) and isinstance(f.value, ast.Constant) and isinstance(f.value.value, str):
            for ln in range(f.value.lineno, getattr(f.value, "end_lineno", f.value.lineno) + 1):
                out.add(ln)
    consider(tree.body)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            consider(n.body)
    return out


def analyze(p: Path):
    src = p.read_text(errors="replace")
    lines = src.splitlines()
    total = len(lines)
    blank = {i + 1 for i, l in enumerate(lines) if not l.strip()}
    comment = set()
    for tok in tokenize.generate_tokens(iter(src.splitlines(keepends=True)).__next__):
        if tok.type == tokenize.COMMENT:
            ln = tok.start[0]
            if lines[ln - 1].strip().startswith("#"):
                comment.add(ln)
    docs = doc_lines(ast.parse(src)) - comment - blank
    prose = len(docs) + len(comment)
    code = total - prose - len(blank)
    return total, code, len(docs), len(comment), len(blank), prose


print(f"{'file':<40}{'total':>7}{'code':>7}{'doc':>7}{'cmt':>7}{'blank':>7}{'prose':>7}{'prose%':>8}")
for a in sys.argv[1:]:
    p = Path(a)
    t, c, d, m, b, pr = analyze(p)
    print(f"{p.name:<40}{t:>7}{c:>7}{d:>7}{m:>7}{b:>7}{pr:>7}{pr/t*100:>7.1f}%")
