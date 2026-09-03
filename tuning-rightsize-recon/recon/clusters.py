import ast,json,pathlib,collections,re,hashlib
ROOT=pathlib.Path(".")
tbl=json.load(open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/tbl.json"))
class Norm(ast.NodeTransformer):
    def visit_Constant(self,n): return ast.copy_location(ast.Constant(value=0),n)
    def visit_Name(self,n): return n
groups=collections.defaultdict(list)
bodies={}
for r in tbl:
    p=ROOT/r["path"]
    try: tree=ast.parse(p.read_text(errors="ignore"))
    except Exception: continue
    for node in ast.walk(tree):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith("test"):
            body=list(node.body)
            if body and isinstance(body[0],ast.Expr) and isinstance(body[0].value,ast.Constant) and isinstance(body[0].value.value,str):
                doclen=body[0].value.value.count("\n")+1; body=body[1:]
            else: doclen=0
            if not body: continue
            mod=ast.Module(body=[Norm().visit(ast.parse(ast.unparse(ast.Module(body=body,type_ignores=[])))) ],type_ignores=[])
            try: s=ast.unparse(mod)
            except Exception: continue
            s=re.sub(r'\b\d+(\.\d+)?\b','N',s)
            h=hashlib.md5(s.encode()).hexdigest()
            nl=(node.end_lineno or node.lineno)-node.lineno+1
            groups[h].append((r["path"],node.name,nl,doclen))
big=sorted((g for g in groups.values() if len(g)>=4),key=lambda g:-sum(x[2] for x in g))
print(f"identical-shape clusters (>=4 members): {len(big)}; total tests in them {sum(len(g) for g in big)}; total lines {sum(x[2] for g in big for x in g)}")
for g in big[:25]:
    files=collections.Counter(x[0] for x in g)
    print(f"\n  x{len(g)} ({sum(x[2] for x in g)} lines)  {list(files)[0]}  ...")
    for x in g[:4]: print(f"       {x[1]}   ({x[0].split('/')[-1]}, {x[2]}L, doc{x[3]})")
