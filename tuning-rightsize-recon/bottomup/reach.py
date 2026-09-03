"""Import-graph reachability over jasper/ seeded from every runtime entry point.
Outputs per-seed closure sizes within the tuning scope and the ghost set."""
import ast, re, sys, json, pathlib, collections, tomllib
ROOT = pathlib.Path('/home/user/JTS')
SCOPE_PREFIX = ('jasper.active_speaker','jasper.audio_measurement','jasper.correction','jasper.attribution','jasper.calibration_agent')
SCOPE_FILES_EXTRA = re.compile(r'^jasper\.(web\.(correction_|active_speaker_flow|balance_)|cli\.(active_speaker|audition|active_speaker_attempts_replay|crossover_prescriber|project_ring|classify_features|read_distortion|round_views|round_bank|round|angle_capture|arm_walk|active_speaker_emit_bench|basic_profile|seat_level|delay_sweep|forward_model|gate_sweep|close_reference|measure|null_door|bass_extension_bench|declare_geometry|correction_bundle|measurement_mic|_refusal|_report|_unit_pair|_logging)$)')
def in_scope(m): return m.startswith(SCOPE_PREFIX) or bool(SCOPE_FILES_EXTRA.match(m))
mods={}
for p in (ROOT/'jasper').rglob('*.py'):
    rel=p.relative_to(ROOT).with_suffix('')
    parts=list(rel.parts)
    if parts[-1]=='__init__': parts=parts[:-1]
    mods['.'.join(parts)]=p
lines={m:sum(1 for _ in open(p,errors='ignore')) for m,p in mods.items()}
def resolve(name):
    if name in mods: return name
    # a submodule attribute import: jasper.a.b.C -> jasper.a.b
    while '.' in name:
        name=name.rsplit('.',1)[0]
        if name in mods: return name
    return None
edges=collections.defaultdict(set)
for m,p in mods.items():
    try: tree=ast.parse(p.read_text(errors='ignore'))
    except Exception: continue
    pkg=m if (p.name=='__init__.py') else m.rsplit('.',1)[0]
    for n in ast.walk(tree):
        if isinstance(n,ast.Import):
            for a in n.names:
                r=resolve(a.name)
                if r: edges[m].add(r)
        elif isinstance(n,ast.ImportFrom):
            if n.level:
                base=pkg.split('.')
                base=base[:len(base)-(n.level-1)] if n.level>1 else base
                modname='.'.join(base+([n.module] if n.module else []))
            else: modname=n.module or ''
            r=resolve(modname)
            if r: edges[m].add(r)
            for a in n.names:
                r2=resolve(modname+'.'+a.name)
                if r2: edges[m].add(r2)
    # package __init__ implicitly reachable when submodule imported
for m in list(mods):
    parts=m.split('.')
    for i in range(1,len(parts)):
        parent='.'.join(parts[:i])
        if parent in mods: edges[m].add(parent)
def closure(seeds):
    seen=set(); stack=list(seeds)
    while stack:
        x=stack.pop()
        if x in seen or x not in mods: continue
        seen.add(x); stack.extend(edges[x])
    return seen
# seeds
py=tomllib.loads((ROOT/'pyproject.toml').read_text())
scripts=py['project']['scripts']
entry={k:v.split(':')[0] for k,v in scripts.items()}
roster=[]
src=(ROOT/'scripts/generate-tuning-tool-menu.py').read_text()
m=re.search(r'TUNING_TOOL_MODULES[^\n]*\(\n(.*?)\n\)',src,re.S)
roster=re.findall(r'"([\w.]+)"',m.group(1))
assert roster, 'roster empty'
systemd=set(re.findall(r'ExecStart=\S*?(jasper-[a-z0-9-]+)',' '.join(p.read_text(errors='ignore') for p in (ROOT/'deploy/systemd').rglob('*') if p.is_file())))
lap_scripts={}
for p in (ROOT/'scripts').glob('*.py'):
    try: tree=ast.parse(p.read_text(errors='ignore'))
    except Exception: continue
    s=set()
    for n in ast.walk(tree):
        if isinstance(n,ast.ImportFrom) and n.module and n.module.startswith('jasper'):
            r=resolve(n.module); 
            if r: s.add(r)
            for a in n.names:
                r2=resolve(n.module+'.'+a.name)
                if r2: s.add(r2)
        if isinstance(n,ast.Import):
            for a in n.names:
                r=resolve(a.name)
                if r: s.add(r)
    if s: lap_scripts[p.name]=s
scope_mods={m for m in mods if in_scope(m)}
scope_total=sum(lines[m] for m in scope_mods)
def scope_size(cl): return sum(lines[m] for m in cl if m in scope_mods)
rows=[]
reached_by=collections.defaultdict(set)
def add(kind,name,seedmods):
    cl=closure(seedmods)
    for x in cl:
        if x in scope_mods: reached_by[x].add(name)
    rows.append((kind,name,len([x for x in cl if x in scope_mods]),scope_size(cl)))
roster_bins={k for k,v in entry.items() if v in roster}
for k,v in sorted(entry.items()):
    kind='MENU' if v in roster else ('SYSTEMD' if k in systemd else 'BINARY')
    add(kind,k,[v])
for name,s in sorted(lap_scripts.items()):
    add('LAPTOP-SCRIPT','scripts/'+name,list(s))
# report
print(f"scope: {len(scope_mods)} modules, {scope_total} lines")
print("\n== per entry point: scope modules reached / scope lines reached ==")
for kind,name,n,l in sorted(rows,key=lambda r:-r[3]):
    if l: print(f"{kind:14s} {name:45s} {n:4d} mods {l:7d} lines")
ghost=[m for m in scope_mods if not reached_by[m]]
print(f"\n== GHOST: scope modules reached by NO entry point: {len(ghost)} modules, {sum(lines[m] for m in ghost)} lines ==")
for m in sorted(ghost,key=lambda m:-lines[m]): print(f"  {lines[m]:6d} {m}")
menu_only=closure([entry[k] for k in roster_bins])
menu_lines=scope_size(menu_only)
web_seeds=[entry[k] for k in entry if k in ('jasper-correction-web','jasper-sound-web')]
web_cl=closure(web_seeds)
print(f"\n== union of MENU tools: {len([x for x in menu_only if x in scope_mods])} mods, {menu_lines} scope lines")
print(f"== union of web (correction-web + sound-web): {len([x for x in web_cl if x in scope_mods])} mods, {scope_size(web_cl)} scope lines")
both=menu_only|web_cl
print(f"== union MENU+web: {scope_size(both)} scope lines; reached ONLY by other entry points (doctor/voice/control/etc): {sum(lines[m] for m in scope_mods if m not in both and reached_by[m])}")
print("\n== scope modules reached ONLY by non-menu, non-web entry points ==")
for m in sorted([m for m in scope_mods if m not in both and reached_by[m]],key=lambda m:-lines[m])[:60]:
    print(f"  {lines[m]:6d} {m:60s} <- {', '.join(sorted(reached_by[m]))[:80]}")
json.dump({'reached_by':{m:sorted(v) for m,v in reached_by.items()},'lines':{m:lines[m] for m in scope_mods},'edges':{m:sorted(v) for m,v in edges.items() if m in scope_mods}},open(sys.argv[1] if len(sys.argv)>1 else '/dev/null','w'))

print("\nROSTER:",len(roster),roster[:3])
base=closure([entry['jasper-voice']])
base_scope=sorted([m for m in base if m in scope_mods],key=lambda m:-lines[m])
print(f"\n== L0 substrate pulled in by jasper-voice: {len(base_scope)} scope mods, {sum(lines[m] for m in base_scope)} lines ==")
for m in base_scope[:40]: print(f"  {lines[m]:6d} {m}")
import collections as C
def path(src,dst):
    prev={src:None}; q=C.deque([src])
    while q:
        x=q.popleft()
        if x==dst: break
        for y in sorted(edges[x]):
            if y not in prev: prev[y]=x; q.append(y)
    if dst not in prev: return None
    out=[]; x=dst
    while x: out.append(x); x=prev[x]
    return out[::-1]
print("\n== how jasper-voice reaches the tuning scope (shortest import paths) ==")
for tgt in base_scope[:6]:
    pth=path(entry['jasper-voice'],tgt)
    print("  "+" -> ".join(pth) if pth else "  ?")
menu=closure([entry[k] for k in roster_bins]); menu_ex={m for m in menu if m in scope_mods}-set(base_scope)
web=closure(web_seeds); web_ex={m for m in web if m in scope_mods}-set(base_scope)-menu_ex
print(f"\n== L1 menu tools beyond L0: {len(menu_ex)} mods {sum(lines[m] for m in menu_ex)} lines")
print(f"== L2 web beyond L0+L1: {len(web_ex)} mods {sum(lines[m] for m in web_ex)} lines")
rest={m for m in scope_mods if reached_by[m]}-set(base_scope)-menu_ex-web_ex
print(f"== L3 reached only by other entries: {len(rest)} mods {sum(lines[m] for m in rest)} lines")
print("\n== L2 web-only modules (top 40) ==")
for m in sorted(web_ex,key=lambda m:-lines[m])[:40]: print(f"  {lines[m]:6d} {m}")
print("\n== L3 modules ==")
for m in sorted(rest,key=lambda m:-lines[m]): print(f"  {lines[m]:6d} {m:55s} <- {', '.join(sorted(reached_by[m]))[:70]}")
