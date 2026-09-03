import ast,sys
from pathlib import Path
for p in sys.argv[1:]:
    src=Path(p).read_text(errors="replace"); t=ast.parse(src)
    print("=====",p)
    rows=[]
    for n in t.body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            rows.append((n.end_lineno-n.lineno+1, n.name, type(n).__name__[:5], n.lineno))
    rows.sort(reverse=True)
    for L,name,k,ln in rows[:40]: print(f"  {L:5d} {k} {name} @{ln}")
    print(f"  ({len(rows)} top-level defs, {sum(r[0] for r in rows)} lines in defs)")
