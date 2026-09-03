import json, re, pathlib, ast, collections
ROOT=pathlib.Path("/home/user/JTS")
rows=json.load(open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/scope.json"))
out=[]
for path,n,hits in rows:
    f=ROOT/path
    txt=f.read_text()
    try: tree=ast.parse(txt)
    except Exception: tree=None
    ntest=nparam=0; ncls=0
    if tree:
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith("test"):
                ntest+=1
                for d in node.decorator_list:
                    s=ast.unparse(d)
                    if "parametrize" in s: nparam+=1
            if isinstance(node,ast.ClassDef): ncls+=1
    real=[h for h in hits if not h.endswith("*(str)")]
    # dominant subject: most-mentioned top-3-level module
    cnt=collections.Counter()
    for h in real:
        parts=h.split(".")
        key=".".join(parts[:3]) if len(parts)>=3 else h
        cnt[key]+=txt.count(h)
    subj=cnt.most_common(1)[0][0] if cnt else (real[0] if real else "?")
    out.append(dict(path=path,lines=n,tests=ntest,param=nparam,classes=ncls,subject=subj,imports=real))
json.dump(out,open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/tbl.json","w"))
out.sort(key=lambda r:-r["lines"])
print(f"{'lines':>6} {'tests':>5} {'param':>5} subject                                   path")
for r in out[:45]:
    print(f"{r['lines']:>6} {r['tests']:>5} {r['param']:>5} {r['subject'][:40]:<40} {r['path']}")
print("TOTAL files",len(out),"lines",sum(r['lines'] for r in out),"tests",sum(r['tests'] for r in out),"param",sum(r['param'] for r in out))
