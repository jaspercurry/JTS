"""astmove.py OLD NEW_SAME NEW_DEST...: which top-level names left OLD, and are they AST-identical in a DEST."""
import ast, sys
def defs(path):
    t=ast.parse(open(path).read()); out={}
    for n in t.body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            if n.body and isinstance(n.body[0],ast.Expr) and isinstance(getattr(n.body[0],'value',None),ast.Constant) and isinstance(n.body[0].value.value,str): n.body=n.body[1:] or [ast.Pass()]
            out[n.name]=ast.dump(n)
        elif isinstance(n,(ast.Assign,ast.AnnAssign)):
            tgs = n.targets if isinstance(n,ast.Assign) else [n.target]
            for tg in tgs:
                if isinstance(tg,ast.Name): out[tg.id]=ast.dump(n)
    return out
old=defs(sys.argv[1]); new=defs(sys.argv[2]); dest={}
for p in sys.argv[3:]: dest.update(defs(p))
gone=[k for k in old if k not in new]
print("left the old module:", len(gone))
print("moved + AST-identical:", [k for k in gone if k in dest and dest[k]==old[k]])
print("NOT identical / missing:", [k for k in gone if not (k in dest and dest[k]==old[k])])
print("changed in place in old module:", [k for k in old if k in new and new[k]!=old[k]])
print("new names in dest not from old:", [k for k in dest if k not in old])
