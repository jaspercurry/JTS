import ast,sys,tokenize,io
from pathlib import Path
def stats(p):
    src=Path(p).read_text(errors="replace")
    lines=src.splitlines()
    total=len(lines)
    blank=sum(1 for l in lines if not l.strip())
    doc=set(); 
    try: t=ast.parse(src)
    except SyntaxError: return total,0,0,blank
    for n in ast.walk(t):
        if isinstance(n,(ast.Module,ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            b=getattr(n,'body',None)
            if b and isinstance(b[0],ast.Expr) and isinstance(b[0].value,ast.Constant) and isinstance(b[0].value.value,str):
                for i in range(b[0].lineno,b[0].end_lineno+1): doc.add(i)
    com=0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type==tokenize.COMMENT: com+=1
    except Exception: pass
    return total,len(doc),com,blank
for p in sys.argv[1:]:
    t,d,c,b=stats(p)
    print(f"{t:6d} {d:5d} {c:5d} {b:5d} {(d+c)*100//max(t,1):3d}%  {p}")
