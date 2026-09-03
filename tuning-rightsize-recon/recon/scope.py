import re, subprocess, pathlib, json, collections

ROOT = pathlib.Path("/home/user/JTS")
TUNING_PREFIXES = (
    "jasper.active_speaker", "jasper.audio_measurement", "jasper.correction",
    "jasper.attribution", "jasper.calibration_agent",
    "jasper.web.correction_", "jasper.web.active_speaker_flow", "jasper.web.balance_",
)
TUNING_CLIS = ["active_speaker","audition","active_speaker_attempts_replay","crossover_prescriber",
 "project_ring","classify_features","read_distortion","round_views","round_bank","round",
 "angle_capture","arm_walk","active_speaker_emit_bench","basic_profile","seat_level",
 "delay_sweep","forward_model","gate_sweep","close_reference","measure","null_door",
 "bass_extension_bench","declare_geometry","correction_bundle","measurement_mic"]
CLI_MODS = {f"jasper.cli.{c}" for c in TUNING_CLIS}

files = sorted(ROOT.glob("tests/**/*.py"))
rows = []
for f in files:
    try: txt = f.read_text()
    except Exception: continue
    mods = set(re.findall(r'(?:from|import)\s+((?:jasper|experiments)[\w\.]*)', txt))
    hit = set()
    for m in mods:
        if any(m.startswith(p) for p in TUNING_PREFIXES): hit.add(m)
        if m in CLI_MODS or any(m.startswith(c+".") for c in CLI_MODS): hit.add(m)
        if m.startswith("experiments"): hit.add(m)
    # also string-based module refs (importlib / monkeypatch targets)
    if not hit:
        for p in TUNING_PREFIXES:
            if p in txt: hit.add(p+"*(str)")
    if hit:
        n = txt.count("\n")+1
        rows.append((str(f.relative_to(ROOT)), n, sorted(hit)))
print(len(rows), sum(r[1] for r in rows))
json.dump(rows, open("/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/recon/scope.json","w"))
