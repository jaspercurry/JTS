import ast, io, os, re, sys, tokenize, json
from collections import defaultdict
PKG='/home/user/JTS/jasper/active_speaker/crossover_v2'
files=sorted(f for f in os.listdir(PKG) if f.endswith('.py'))
mods=[f[:-3] for f in files]
stats={}
imports=defaultdict(set)
for f in files:
    p=os.path.join(PKG,f)
    src=open(p).read()
    lines=src.count('\n')+ (0 if src.endswith('\n') else 1)
    tree=ast.parse(src)
    # docstring lines
    doc=0
    for node in ast.walk(tree):
        if isinstance(node,(ast.Module,ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            d=ast.get_docstring(node,clean=False)
            if d is not None:
                # find the constant node
                b=node.body[0]
                doc += (b.end_lineno-b.lineno+1)
    com=0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type==tokenize.COMMENT: com+=1
    except Exception: pass
    blank=sum(1 for l in src.split('\n') if not l.strip())
    # imports within package
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom):
            m=node.module or ''
            if node.level>0:
                # relative
                if node.level==1 and m=='' :
                    for a in node.names:
                        if a.name in mods: imports[f[:-3]].add(a.name)
                elif node.level==1:
                    base=m.split('.')[0]
                    if base in mods: imports[f[:-3]].add(base)
            else:
                if 'crossover_v2.' in m:
                    tail=m.split('crossover_v2.')[1].split('.')[0]
                    if tail in mods: imports[f[:-3]].add(tail)
        elif isinstance(node,ast.Import):
            for a in node.names:
                if 'crossover_v2.' in a.name:
                    tail=a.name.split('crossover_v2.')[1].split('.')[0]
                    if tail in mods: imports[f[:-3]].add(tail)
    # counts
    ncls=sum(1 for n in ast.walk(tree) if isinstance(n,ast.ClassDef))
    ndc=sum(1 for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and any(
        (isinstance(d,ast.Name) and d.id=='dataclass') or (isinstance(d,ast.Attribute) and d.attr=='dataclass') or (isinstance(d,ast.Call) and ((isinstance(d.func,ast.Name) and d.func.id=='dataclass') or (isinstance(d.func,ast.Attribute) and d.func.attr=='dataclass'))) for d in n.decorator_list))
    nexc=sum(1 for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and any(isinstance(b,ast.Name) and ('Error' in b.id or 'Exception' in b.id or 'Refus' in b.id) for b in n.bases))
    nfun=sum(1 for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)))
    todict=len(re.findall(r'def to_dict|def as_dict|def to_payload|def to_json|def to_mapping',src))
    fromdict=len(re.findall(r'def from_dict|def from_mapping|def from_payload|def from_json',src))
    sha=len(re.findall(r'sha256',src))
    stats[f[:-3]]=dict(lines=lines,doc=doc,com=com,blank=blank,prose=round(100.0*(doc+com)/lines,1),
        classes=ncls,dataclasses=ndc,exc=nexc,funcs=nfun,todict=todict,fromdict=fromdict,sha=sha)
importedby=defaultdict(set)
for a,bs in imports.items():
    for b in bs: importedby[b].add(a)
out=[]
for m in sorted(stats,key=lambda k:-stats[k]['lines']):
    s=stats[m]
    out.append(dict(mod=m,**s,imports=sorted(imports[m]),imported_by=sorted(importedby[m]),
                    n_imports=len(imports[m]),n_importedby=len(importedby[m])))
json.dump(out,open('/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/stats.json','w'),indent=1)
for o in out:
    print(f"{o['mod']:32s} {o['lines']:5d} prose{o['prose']:5.1f}% dc{o['dataclasses']:3d} exc{o['exc']:3d} fn{o['funcs']:4d} td{o['todict']:3d} fd{o['fromdict']:3d} sha{o['sha']:3d} in{o['n_imports']:3d} by{o['n_importedby']:3d}")
tot=sum(s['lines'] for s in stats.values()); pd=sum(s['doc'] for s in stats.values()); pc=sum(s['com'] for s in stats.values())
print('TOTAL',tot,'doc',pd,'comment',pc,'prose%',round(100*(pd+pc)/tot,1))
