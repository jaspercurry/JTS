import ast, sys, io, tokenize, os
def stats(path):
    src=open(path,encoding='utf-8').read()
    lines=src.count('\n')+ (0 if src.endswith('\n') else 1)
    tree=ast.parse(src)
    doc=set()
    for n in ast.walk(tree):
        if isinstance(n,(ast.Module,ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            b=n.body
            if b and isinstance(b[0],ast.Expr) and isinstance(b[0].value,ast.Constant) and isinstance(b[0].value.value,str):
                for i in range(b[0].lineno,b[0].end_lineno+1): doc.add(i)
    com=0; comlines=set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type==tokenize.COMMENT: comlines.add(tok.start[0])
    blank=sum(1 for l in src.split('\n') if not l.strip())
    # count decorator-only / import lines
    code = lines - len(doc) - len(comlines - doc) - blank
    return lines, len(doc), len(comlines-doc), blank, code
tot=[0]*5
rows=[]
for p in sys.argv[1:]:
    try:
        s=stats(p)
    except Exception as e:
        print("ERR",p,e); continue
    rows.append((p,)+s)
    tot=[a+b for a,b in zip(tot,s)]
rows.sort(key=lambda r:-r[1])
for r in rows:
    print(f"{r[1]:6d} {r[2]:6d} {r[3]:6d} {r[4]:6d} {r[5]:6d}  {r[0]}")
print(f"{tot[0]:6d} {tot[1]:6d} {tot[2]:6d} {tot[3]:6d} {tot[4]:6d}  TOTAL   (lines doc comment blank code)")
