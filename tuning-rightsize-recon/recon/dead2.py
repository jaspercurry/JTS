import ast, os, re, sys, json, collections

ROOT="/home/user/JTS"
AREA=["commissioning_admission","commissioning_apply","commissioning_capture","commissioning_coordinator","commissioning_evidence","commissioning_evidence_store","commissioning_host","commissioning_isolated_producer","commissioning_lifecycle","commissioning_receipt","commissioning_run","commissioning_runtime","commissioning_service","commissioning_verification","commission_ramp","commission_wiring","camilla_yaml","runtime_contract","startup_load","startup_hold","staging","profile","baseline_profile","passive_profile","revalidation","reset","restore_wait","setup_status","web_commissioning","web_measurement","wizard_client","tuning_handoff","volume_latch","session_volume_plan","playback","playback_route","safe_playback","program_playback","path_safety","driver_safety","driver_protection","graph_safety","excitation_safety_plan","bringup","bundles","environment","_common"]
TOK=re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
index=collections.defaultdict(set)   # name -> set of rel paths
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in {'.git','node_modules','.venv','__pycache__','target','htmlcov'}]
    for fn in filenames:
        p=os.path.join(dirpath,fn)
        if not re.search(r'\.(py|sh|toml|service|cfg|ini|json|js|md|yml|yaml|rules|conf|bash|txt)$', p) and '/bin/' not in p:
            continue
        rel=os.path.relpath(p,ROOT)
        try: txt=open(p,encoding='utf-8',errors='ignore').read()
        except Exception: continue
        for name in set(TOK.findall(txt)):
            index[name].add(rel)
print(f"# indexed {len(index)} identifiers", file=sys.stderr)

INIT='jasper/active_speaker/__init__.py'
rows=[]
for mod in AREA:
    rel=f'jasper/active_speaker/{mod}.py'
    src=open(os.path.join(ROOT,rel),encoding='utf-8').read()
    tree=ast.parse(src)
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            nm,ln,kd=node.name,node.lineno,('class' if isinstance(node,ast.ClassDef) else 'def')
        elif isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id.isupper():
            nm,ln,kd=node.targets[0].id,node.lineno,'CONST'
        elif isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name) and node.target.id.isupper():
            nm,ln,kd=node.target.id,node.lineno,'CONST'
        else: continue
        files=index.get(nm,set())
        prod=[f for f in files if not f.startswith('tests/')]
        tst=[f for f in files if f.startswith('tests/')]
        ext=[f for f in prod if f!=rel]
        selfuses=len(re.findall(r'\b'+re.escape(nm)+r'\b',src))-1
        rows.append(dict(mod=mod,name=nm,line=ln,kind=kd,ext=ext,self=selfuses,tests=len(tst)))
json.dump(rows,open('/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/symbols.json','w'))
dead=[r for r in rows if not [f for f in r['ext'] if f!=INIT] and r['self']<=0]
print(f"# symbols scanned: {len(rows)}; no external prod ref and no self-use: {len(dead)}")
for r in sorted(dead,key=lambda r:(r['mod'],r['line'])):
    print(f"{r['mod']}.py:{r['line']:<5} {r['kind']:6} {r['name']:52} tests={r['tests']:<3} in_init={INIT in r['ext']}")
