import json, ast, pathlib, re, collections
ROOT=pathlib.Path("/home/user/JTS")
d=json.load(open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/ratio.json"))
prod=d["prod"]
tbl=json.load(open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/tbl.json"))
testtxt={r["path"]:(ROOT/r["path"]).read_text(errors="ignore") for r in tbl}
tlines={r["path"]:r["lines"] for r in tbl}
allsyms={}
for p in prod:
    try: tree=ast.parse((ROOT/p).read_text(errors="ignore"))
    except Exception: continue
    s=[n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and not n.name.startswith("_")]
    allsyms[p]=s
# build reverse index token -> tests
attr=collections.Counter(); hits=collections.defaultdict(set)
for tp,txt in testtxt.items():
    toks=set(re.findall(r'\b\w{4,}\b',txt))
    for p,syms in allsyms.items():
        m=[s for s in syms if s in toks]
        if m: hits[p].add(tp)
res=[]
for p,pl in prod.items():
    ts=hits.get(p,set())
    tl=sum(tlines[t] for t in ts)
    res.append((p,pl,len(ts),tl,len(allsyms.get(p,[]))))
untested=[r for r in res if r[2]==0]
untested.sort(key=lambda r:-r[1])
print(f"NO test references any public symbol: {len(untested)} files / {sum(r[1] for r in untested)} lines")
for r in untested[:30]: print(f"   {r[1]:5d} lines, {r[4]:2d} public syms  {r[0]}")
json.dump({p:[list(hits.get(p,[])),prod[p]] for p in prod},open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/sym.json","w"))
