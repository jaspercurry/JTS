import json, re, pathlib, collections
ROOT=pathlib.Path("/home/user/JTS")
tbl=json.load(open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/tbl.json"))
# production modules in scope
prod={}
globs=["jasper/active_speaker/**/*.py","jasper/audio_measurement/**/*.py","jasper/correction/**/*.py",
       "jasper/attribution/**/*.py","jasper/calibration_agent/**/*.py","experiments/usb-turntable/**/*.py"]
for g in globs:
    for p in ROOT.glob(g):
        if "vendor" in p.parts: continue
        prod[str(p.relative_to(ROOT))]=p.read_text(errors="ignore").count("\n")+1
for p in ROOT.glob("jasper/web/*.py"):
    n=p.name
    if n.startswith("correction_") or n.startswith("balance_") or n=="active_speaker_flow.py":
        prod[str(p.relative_to(ROOT))]=p.read_text(errors="ignore").count("\n")+1
CLIS="active_speaker audition active_speaker_attempts_replay crossover_prescriber project_ring classify_features read_distortion round_views round_bank round angle_capture arm_walk active_speaker_emit_bench basic_profile seat_level delay_sweep forward_model gate_sweep close_reference measure null_door bass_extension_bench declare_geometry correction_bundle measurement_mic".split()
for c in CLIS:
    p=ROOT/f"jasper/cli/{c}.py"
    if p.exists(): prod[str(p.relative_to(ROOT))]=p.read_text().count("\n")+1
    d=ROOT/f"jasper/cli/{c}"
    if d.is_dir():
        for q in d.rglob("*.py"): prod[str(q.relative_to(ROOT))]=q.read_text().count("\n")+1
print("prod files",len(prod),"lines",sum(prod.values()))
def mod2path(m):
    q=m.replace(".","/")
    for cand in (q+".py", q+"/__init__.py"):
        if cand in prod: return cand
    return None
# attribute: each test file's lines split evenly across the distinct prod modules it imports
attr=collections.Counter(); who=collections.defaultdict(list)
for r in tbl:
    paths={mod2path(m) for m in r["imports"]}
    paths={p for p in paths if p}
    if not paths: continue
    for p in paths:
        attr[p]+=r["lines"]/len(paths)
        who[p].append(r["path"])
json.dump({"prod":prod,"attr":dict(attr),"who":dict(who)},open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/ratio.json","w"))
rat=[(p,prod[p],attr.get(p,0)) for p in prod]
big=[r for r in rat if r[1]>=150 and r[2]>0]
big.sort(key=lambda r:-(r[2]/r[1]))
print("\n== most over-tested (>=150 prod lines) ratio test/prod ==")
for p,pl,tl in big[:18]: print(f"  {tl/pl:6.2f}x  prod={pl:5d} test={tl:8.0f}  {p}")
untested=[(p,l) for p,l in prod.items() if attr.get(p,0)==0]
untested.sort(key=lambda r:-r[1])
print(f"\n== untested prod modules: {len(untested)} files / {sum(l for _,l in untested)} lines; top 20 ==")
for p,l in untested[:20]: print(f"  {l:5d}  {p}")
