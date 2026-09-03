import ast, os, re, subprocess, sys, json

ROOT="/home/user/JTS"
AREA=["commissioning_admission","commissioning_apply","commissioning_capture","commissioning_coordinator","commissioning_evidence","commissioning_evidence_store","commissioning_host","commissioning_isolated_producer","commissioning_lifecycle","commissioning_receipt","commissioning_run","commissioning_runtime","commissioning_service","commissioning_verification","commission_ramp","commission_wiring","camilla_yaml","runtime_contract","startup_load","startup_hold","staging","profile","baseline_profile","passive_profile","revalidation","reset","restore_wait","setup_status","web_commissioning","web_measurement","wizard_client","tuning_handoff","volume_latch","session_volume_plan","playback","playback_route","safe_playback","program_playback","path_safety","driver_safety","driver_protection","graph_safety","excitation_safety_plan","bringup","bundles","environment","_common"]

# build a corpus: all text files in repo (py, sh, toml, service, js, md) split into prod vs tests
prod_files=[]
test_files=[]
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in {'.git','node_modules','.venv','__pycache__','target'}]
    for fn in filenames:
        p=os.path.join(dirpath,fn)
        if not re.search(r'\.(py|sh|toml|service|cfg|ini|json|js|md|yml|yaml|rules|conf|bash)$|/bin/[^/]+$', p):
            continue
        rel=os.path.relpath(p, ROOT)
        (test_files if rel.startswith('tests/') else prod_files).append(p)

def load(fs):
    out={}
    for p in fs:
        try: out[p]=open(p, encoding='utf-8', errors='ignore').read()
        except Exception: pass
    return out

prod=load(prod_files); tests=load(test_files)
print(f"# corpus: {len(prod)} prod files, {len(tests)} test files", file=sys.stderr)

results=[]
for mod in AREA:
    path=os.path.join(ROOT,'jasper/active_speaker',mod+'.py')
    src=open(path,encoding='utf-8').read()
    tree=ast.parse(src)
    names=[]
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            names.append((node.name,node.lineno,type(node).__name__))
        elif isinstance(node,ast.Assign):
            for t in node.targets:
                if isinstance(t,ast.Name) and t.id.isupper():
                    names.append((t.id,node.lineno,'CONST'))
    for name,lineno,kind in names:
        pat=re.compile(r'\b'+re.escape(name)+r'\b')
        prod_hits=[]
        for p,txt in prod.items():
            if p==path: continue
            if pat.search(txt): prod_hits.append(os.path.relpath(p,ROOT))
        # self-uses inside the module beyond the def line
        self_uses = len(pat.findall(src)) - 1
        test_hits=sum(1 for p,txt in tests.items() if pat.search(txt))
        results.append(dict(mod=mod,name=name,line=lineno,kind=kind,prod=prod_hits,self=self_uses,tests=test_hits))

json.dump(results, open('/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/symbols.json','w'))
INIT='jasper/active_speaker/__init__.py'
dead=[r for r in results if not [h for h in r['prod'] if h!=INIT] and r['self']<=0]
print(f"# total symbols: {len(results)}; zero external prod refs & no self-use: {len(dead)}")
for r in sorted(dead,key=lambda r:(r['mod'],r['line'])):
    exported = INIT in r['prod']
    print(f"{r['mod']}.py:{r['line']:5} {r['kind']:9} {r['name']:55} tests={r['tests']:3} exported_in_init={exported}")
