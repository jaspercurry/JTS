import ast,json,pathlib,collections,re
ROOT=pathlib.Path(".")
tbl=json.load(open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/tbl.json"))
recs=[]
for r in tbl:
    p=ROOT/r["path"]
    try: tree=ast.parse(p.read_text(errors="ignore"))
    except Exception: continue
    for node in ast.walk(tree):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith("test"):
            nl=(node.end_lineno or node.lineno)-node.lineno+1
            has_param=any("parametrize" in ast.unparse(d) for d in node.decorator_list)
            recs.append((r["path"],node.name,nl,has_param))
# cluster by file + first 3 name tokens
g=collections.defaultdict(list)
for path,name,nl,hp in recs:
    toks=name.split("_")
    key=(path,"_".join(toks[:4]))
    g[key].append((name,nl,hp))
clusters=[(k,v) for k,v in g.items() if len(v)>=5 and not any(x[2] for x in v)]
clusters.sort(key=lambda kv:-sum(x[1] for x in kv[1]))
print(f"unparametrized name-prefix clusters (>=5): {len(clusters)}; tests {sum(len(v) for _,v in clusters)}; lines {sum(x[1] for _,v in clusters for x in v)}")
for (path,pref),v in clusters[:22]:
    print(f"\n  x{len(v):3d}  {sum(x[1] for x in v):5d} lines  {path}  prefix '{pref}_*'")
    for n,l,_ in v[:3]: print(f"        {n} ({l}L)")
