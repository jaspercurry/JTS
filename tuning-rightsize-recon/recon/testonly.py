import ast,json,pathlib,re,collections
ROOT=pathlib.Path(".")
d=json.load(open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/ratio.json"))
prod=d["prod"]
# all production source (whole repo, excluding tests)
prod_txt={}
for p in list(ROOT.glob("jasper/**/*.py"))+list(ROOT.glob("deploy/**/*.py"))+list(ROOT.glob("scripts/**/*.py"))+list(ROOT.glob("experiments/**/*.py")):
    prod_txt[str(p)]=p.read_text(errors="ignore")
tests_txt={}
for p in ROOT.glob("tests/**/*.py"):
    tests_txt[str(p)]=p.read_text(errors="ignore")
alltest=" ".join(tests_txt.values())
res=[]
for rel,l in prod.items():
    try: tree=ast.parse((ROOT/rel).read_text(errors="ignore"))
    except Exception: continue
    for n in tree.body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            nm=n.name
            if nm.startswith("__"): continue
            rx=re.compile(r'\b'+re.escape(nm)+r'\b')
            pcount=0
            for f,t in prod_txt.items():
                if f.endswith(rel): 
                    pcount+=len(rx.findall(t))-1  # its own def
                else:
                    pcount+=len(rx.findall(t))
            tcount=len(rx.findall(alltest))
            if pcount<=0 and tcount>0:
                res.append((rel,nm,(n.end_lineno-n.lineno+1),tcount))
res.sort(key=lambda r:-r[2])
print(f"production symbols in scope with NO production reference but referenced in tests: {len(res)}, {sum(r[2] for r in res)} prod lines")
c=collections.Counter(r[0] for r in res)
for f,n in c.most_common(15): print(f"   {n:3d} syms  {f}")
print()
for r in res[:20]: print(f"   {r[2]:4d}L  {r[0]}::{r[1]}  (test refs {r[3]})")
