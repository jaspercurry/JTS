import ast, os, sys, json
from pathlib import Path
ROOT=Path("/home/user/JTS")
mods={}
for p in ROOT.rglob("*.py"):
    rel=p.relative_to(ROOT)
    parts=rel.with_suffix("").parts
    if parts[0] not in ("jasper","scripts","experiments"): continue
    if parts[-1]=="__init__": name=".".join(parts[:-1])
    else: name=".".join(parts)
    mods[name]=p

def edges(path, name, toplevel_only=True):
    src=path.read_text(errors="replace")
    try: t=ast.parse(src)
    except SyntaxError: return set()
    lines=src.splitlines()
    out=set()
    pkg=name.rsplit(".",1)[0] if "." in name else name
    # determine package for relative resolution
    if path.name=="__init__.py": pkg=name
    for n in ast.walk(t):
        if isinstance(n,(ast.Import,ast.ImportFrom)):
            indent=len(lines[n.lineno-1])-len(lines[n.lineno-1].lstrip())
            if toplevel_only and indent: continue
            if isinstance(n,ast.Import):
                for a in n.names: out.add(a.name)
            else:
                base = n.module or ""
                if n.level:
                    p2=pkg.split(".")
                    p2=p2[:len(p2)-(n.level-1)] if n.level>1 else p2
                    base=".".join([x for x in p2 if x]+([base] if base else []))
                out.add(base)
                for a in n.names: out.add(base+"."+a.name)
    return out

graph={n:edges(p,n) for n,p in mods.items()}
lc={n:len(p.read_text(errors="replace").splitlines()) for n,p in mods.items()}
graph_lazy={n:edges(p,n,False) for n,p in mods.items()}

def closure(start, g):
    seen=set(); stack=[start]
    while stack:
        m=stack.pop()
        if m in seen: continue
        seen.add(m)
        for tgt in g.get(m,()):
            if tgt in mods and tgt not in seen: stack.append(tgt)
    return seen

SCOPE=lambda m: (m.startswith("jasper.active_speaker") or m.startswith("jasper.audio_measurement")
    or m.startswith("jasper.correction") or m.startswith("jasper.attribution")
    or m.startswith("jasper.calibration_agent") or m.startswith("jasper.web.correction")
    or m.startswith("jasper.bass_extension"))

for entry in ["jasper.voice_daemon","jasper.mux","jasper.cli.doctor","jasper.control.daemon","jasper.web.server"]:
    if entry not in mods:
        print("missing",entry); continue
    for label,g in (("TOPLEVEL",graph),("WITH-LAZY",graph_lazy)):
        c=closure(entry,g)
        sc=[m for m in c if SCOPE(m)]
        print(f"{entry:28s} {label:10s} total {len(c):4d} mods / {sum(lc[m] for m in c):7d} lines | SCOPE {len(sc):3d} mods / {sum(lc[m] for m in sc):7d} lines")
    c=closure(entry,graph)
    sc=sorted([m for m in c if SCOPE(m)], key=lambda m:-lc[m])
    print("   top-level scope modules:", ", ".join(f"{m.split('.')[-1]}({lc[m]})" for m in sc[:25]))
    print()
