"""Rear branch electrical level at 250-630 Hz, via the product's own evaluator."""
import json, sys
from pathlib import Path
import numpy as np
SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))
import identlib as il
from jasper.active_speaker.branch_chain import rear_stage_response
from rearpred import section_of

GRID = il.freqs()
FREQS = (250, 315, 400, 500, 630)
DOCS = (("S1", "cands5/gated-S1.json"), ("lp400", "search/H1/doc-S1-lp400.json"),
        ("lp350", "search/H1/doc-S1-lp350.json"), ("w3", "search/H1/doc-S1-w3.json"),
        ("ident-C", "cands3/ident-C.json"), ("T1", "search/L1/doc-T1.json"))


def level(curve, hz):
    return 20 * np.log10(abs(np.interp(hz, GRID, curve)) + 1e-30)


for title, mute_bass in (("REAR SUM", False), ("CANCELLATION branch alone", True)):
    print(f"\n{title} electrical level, dB")
    print("  %-9s" % "tune" + "".join("%9d" % f for f in FREQS))
    for name, path in DOCS:
        section = section_of(json.loads((SP / path).read_text()))
        if mute_bass:
            section = json.loads(json.dumps(section))
            section["rear"]["bass"]["muted"] = True
        curve = rear_stage_response(section, GRID)[0]
        print("  %-9s" % name + "".join("%+9.2f" % level(curve, f) for f in FREQS))
