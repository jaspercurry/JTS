import ast, io, sys, tokenize
from pathlib import Path

def metrics(p):
    src = Path(p).read_text()
    tree = ast.parse(src)
    doc = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                doc.update(range(b[0].lineno, b[0].end_lineno + 1))
    com = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            com.add(tok.start[0])
    tests = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
    asserts = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Assert))
    raises = src.count("pytest.raises")
    total = len(src.splitlines())
    return dict(file=p, lines=total, prose=len(doc | com), doc=len(doc), com=len(com),
                tests=len(tests), asserts=asserts, raises=raises,
                pct=round(100 * len(doc | com) / max(total, 1), 1))

rows = [metrics(p) for p in sys.argv[1:]]
w = max(len(r["file"]) for r in rows)
print(f'{"file".ljust(w)} {"lines":>6} {"prose":>6} {"pct":>5} {"tests":>5} {"asrt":>5} {"raises":>6}')
for r in rows:
    print(f'{r["file"].ljust(w)} {r["lines"]:6} {r["prose"]:6} {r["pct"]:5} {r["tests"]:5} {r["asserts"]:5} {r["raises"]:6}')
print("TOTAL", sum(r["lines"] for r in rows), sum(r["prose"] for r in rows), sum(r["tests"] for r in rows), sum(r["asserts"] for r in rows))
