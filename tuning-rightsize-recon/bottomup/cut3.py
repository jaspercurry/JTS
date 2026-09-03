import ast
from pathlib import Path
ROOT=Path("/home/user/JTS")
mods={}
for p in ROOT.rglob("*.py"):
    parts=p.relative_to(ROOT).with_suffix("").parts
    if parts[0]!="jasper": continue
    name=".".join(parts[:-1]) if parts[-1]=="__init__" else ".".join(parts)
    mods[name]=p
lc={n:len(p.read_text(errors="replace").splitlines()) for n,p in mods.items()}
def edges(name,path,toponly):
    src=path.read_text(errors="replace")
    try: t=ast.parse(src)
    except SyntaxError: return set()
    lines=src.splitlines()
    pkg=name if path.name=="__init__.py" else name.rsplit(".",1)[0]
    out=set()
    for n in ast.walk(t):
        if not isinstance(n,(ast.Import,ast.ImportFrom)): continue
        ind=len(lines[n.lineno-1])-len(lines[n.lineno-1].lstrip())
        if toponly and ind: continue
        if isinstance(n,ast.Import):
            for a in n.names: out.add(a.name)
        else:
            base=n.module or ""
            if n.level:
                p2=pkg.split("."); p2=p2[:len(p2)-(n.level-1)] if n.level>1 else p2
                base=".".join([x for x in p2 if x]+([base] if base else []))
            out.add(base)
            for a in n.names: out.add(base+"."+a.name)
    res={m for m in out if m in mods}
    # importing a submodule also executes its package __init__
    extra=set()
    for m in res:
        parts=m.split(".")
        for i in range(1,len(parts)):
            pk=".".join(parts[:i])
            if pk in mods: extra.add(pk)
    return res|extra
g={n:edges(n,p,True) for n,p in mods.items()}
SC=lambda m: m.startswith(("jasper.active_speaker","jasper.audio_measurement","jasper.correction","jasper.attribution","jasper.calibration_agent","jasper.bass_extension"))
def closure(start,drop=frozenset()):
    seen=set();st=[start]
    while st:
        m=st.pop()
        if m in seen: continue
        seen.add(m)
        if m in drop: continue
        for t2 in g.get(m,()):
            if t2 not in seen: st.append(t2)
    return seen
DROP=frozenset({"jasper.active_speaker","jasper.audio_measurement","jasper.correction"})
for start in ["jasper.voice_daemon","jasper.mux","jasper.cli.active_speaker","jasper.active_speaker.runtime_contract","jasper.cli.doctor.audio","jasper.control.state_aggregate","jasper.web.sound_setup"]:
    if start not in mods: print("missing",start); continue
    for lbl,d in (("as-is",frozenset()),("thin __init__",DROP)):
        c=closure(start,d); s=[m for m in c if SC(m)]
        print(f"{start:36s} {lbl:14s} scope {len(s):3d} mods {sum(lc[m] for m in s):7d} | total {sum(lc[m] for m in c):7d}")
    c=closure(start,DROP); s=sorted([m for m in c if SC(m)],key=lambda m:-lc[m])
    print("    ->", ", ".join(f"{m.split('.')[-1]}({lc[m]})" for m in s[:18])); print()
