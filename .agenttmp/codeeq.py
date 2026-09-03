"""Verify two versions of a .py file have identical CODE.

Compares the token stream with COMMENT / NL / INDENT / DEDENT / NEWLINE dropped
and docstring STRING tokens replaced by a placeholder. Any difference in real
code, string literals, names, or __all__ shows up as a mismatch.

Usage: python3 codeeq.py <old_file> <new_file>
"""
import ast
import io
import sys
import tokenize


def docstring_spans(src):
    spans = set()
    tree = ast.parse(src)

    def consider(body):
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            n = body[0].value
            spans.add((n.lineno, n.col_offset))

    consider(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            consider(node.body)
    return spans


def stream(path):
    src = open(path, encoding="utf-8").read()
    spans = docstring_spans(src)
    out = []
    skip = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
            tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in skip:
            continue
        if tok.type == tokenize.STRING and tok.start in spans:
            out.append(("DOCSTRING",))
            continue
        out.append((tok.type, tok.string))
    return out


a, b = stream(sys.argv[1]), stream(sys.argv[2])
if a == b:
    print(f"CODE IDENTICAL ({len(a)} tokens)")
else:
    print(f"MISMATCH: {len(a)} vs {len(b)} tokens")
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            print("first diff at token", i, x, "!=", y)
            print("context old:", a[max(0, i - 6):i + 6])
            print("context new:", b[max(0, i - 6):i + 6])
            break
    else:
        print("prefix equal; tail differs:", a[len(b):] or b[len(a):])
    sys.exit(1)
