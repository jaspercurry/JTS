import ast, io, sys, tokenize, os

files = sys.argv[1:]
print(f"{'file':44} {'lines':>6} {'code':>6} {'doc':>6} {'cmt':>6} {'blank':>6} {'prose%':>7}")
tot=[0,0,0,0,0]
for f in files:
    src = open(f, encoding='utf-8').read()
    lines = src.splitlines()
    n = len(lines)
    blank = sum(1 for l in lines if not l.strip())
    # comment lines
    cmt = 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                cmt += 1
    except Exception:
        pass
    # docstring lines
    doc = 0
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                b = getattr(node, 'body', None)
                if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                    d = b[0]
                    doc += d.end_lineno - d.lineno + 1
    except Exception:
        pass
    code = n - blank - cmt - doc
    prose = (doc+cmt)/n*100 if n else 0
    print(f"{os.path.basename(f):44} {n:6} {code:6} {doc:6} {cmt:6} {blank:6} {prose:6.1f}%")
    tot[0]+=n; tot[1]+=code; tot[2]+=doc; tot[3]+=cmt; tot[4]+=blank
n,code,doc,cmt,blank = tot
print(f"{'TOTAL':44} {n:6} {code:6} {doc:6} {cmt:6} {blank:6} {(doc+cmt)/max(n,1)*100:6.1f}%")
