import ast,sys,collections
NUMPY={'np','numpy','scipy'}
def body_lines(n,src_lines):
    # count non-blank, non-docstring, non-comment lines within function span
    lo,hi=n.lineno,n.end_lineno
    doc=set()
    b=n.body
    if b and isinstance(b[0],ast.Expr) and isinstance(b[0].value,ast.Constant) and isinstance(b[0].value.value,str):
        doc=set(range(b[0].lineno,b[0].end_lineno+1))
    c=0
    for i in range(lo,hi+1):
        if i in doc: continue
        s=src_lines[i-1].strip()
        if not s or s.startswith('#'): continue
        c+=1
    return c
agg=collections.Counter(); aggn=collections.Counter()
rows=[]
for p in sys.argv[1:]:
    src=open(p,encoding='utf-8').read(); sl=src.split('\n')
    t=ast.parse(src)
    for n in ast.walk(t):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
            has=False
            for x in ast.walk(n):
                if isinstance(x,ast.Name) and x.id in NUMPY: has=True;break
                if isinstance(x,ast.Attribute) and isinstance(x.value,ast.Name) and x.value.id in NUMPY: has=True;break
            bl=body_lines(n,sl)
            rows.append((p,n.name,bl,has))
            agg[p]+= bl if has else 0
            aggn[p]+= 0 if has else bl
tn=sum(agg.values()); tf=sum(aggn.values())
for p in sorted(set(list(agg)+list(aggn)),key=lambda k:-(agg[k]+aggn[k])):
    print(f"{agg[p]:6d} {aggn[p]:6d}  {p}")
print(f"{tn:6d} {tf:6d}  TOTAL numpy-fn-lines / non-numpy-fn-lines")
