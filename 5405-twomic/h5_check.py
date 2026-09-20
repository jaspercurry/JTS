#!/usr/bin/env python3
"""What is extrapolation, how fragile is it, and do the documents re-read?

  1  per knob, the measured range on the SAME structure vs the solution's value
  2  one knob at a time, does the score or a guard move sharply
  3  the same documents scored under R from other round subsets and against
     another round's rig state
  4  both product readers again, from disk, plus the shared-zero check
"""
from __future__ import annotations

import json

import numpy as np

import g5lib as g
import h5_fam as f
import h5lib as h
from h5_solve import STEP, classify, describe, measured_table, rows_for
from rearpred import section_of

from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

KNOBS = {"F1": ("delay ms", "120 Hz dB", "lowpass Hz"), "F2": ("delay ms", "64 Hz dB")}
STEPS = {"F1": (0.05, 0.5, 25.0), "F2": (0.05, 0.5)}
SUBSETS = {"all": set(g.ALL_TAGS), "pre-USB": {"d1", "d2", "c1", "649a", "d3"},
           "post-USB": {"i1b", "i2", "l1", "g1m", "g2m", "h1", "h3", "h4"},
           "no-H4": set(g.ALL_TAGS) - {"h4"}}


def main() -> int:
    h.widen()
    store = g.collect()
    chain_of = g.chains()
    measured = measured_table(json.loads((g.SP / "search/fp-index.json").read_text()))
    solved = json.loads((g.SP / "cands6/summary.json").read_text())
    docs = {tag: json.loads((g.SP / f"cands6/{tag}.json").read_text())
            for tag in ("fb-1", "fb-2", "fb-3")}
    sections = {tag: section_of(doc) for tag, doc in docs.items()}

    print("=== 1. each knob against the measured range on the SAME structure")
    for tag, section in sections.items():
        family, vec = classify(section)
        same = {n: v for n, v in measured[family].items()
                if (v[-1] is None) == (vec[-1] is None)}
        print(f"  {tag} ({family}, {'with' if vec[-1] else 'no'} added Peaking; "
              f"{len(same)} measured tunes share this structure)")
        for i, knob in enumerate(KNOBS[family]):
            values = [v[i] for v in same.values()]
            inside = min(values) <= vec[i] <= max(values)
            print(f"    {knob:<11s} {vec[i]:+9.3f}   measured {min(values):+8.3f} .."
                  f"{max(values):+8.3f}   {'INSIDE' if inside else 'OUTSIDE'}")
        if vec[-1]:
            print(f"    added Peaking {vec[-1][0]:.0f} Hz {vec[-1][1]:+.2f} dB q{vec[-1][2]:.2f}")

    base = section_of(g.load_json(g.SP / "search/D2/doc-e-035.json"))
    ident_c = section_of(g.load_json(g.SP / "cands3/ident-C.json"))
    model = h.FB(store, chain_of, target="h4")
    objective = f.Objective(model, base, ident_c, chain_of[f.E035])

    print("\n=== 2. one knob at a time (score; '!' = a front guard fails)")
    for tag, name in (("fb-1", "F1a"), ("fb-2", "F2a"), ("fb-3", "F2a")):
        x = (solved["families"][name]["x"] if tag != "fb-3" else solved["fb3"]["x"])
        family = f.FAMILIES[name][0]
        print(f"  {tag}: {describe(x, name, objective)}")
        for i, knob in enumerate(KNOBS[family]):
            cells = []
            for sign in (-1, +1):
                moved = list(x)
                moved[i] += sign * STEPS[family][i]
                rows = rows_for(objective, objective.chain(moved, name))
                cells.append(f"{rows['score']:+.2f}{'!' if rows['guard_notes'] else ' '}")
            here = rows_for(objective, objective.chain(list(x), name))
            print(f"    {knob:<11s} +-{STEPS[family][i]:<6g} {cells[0]}  "
                  f"[{here['score']:+.2f}]  {cells[1]}")

    print("\n=== 3. the same documents under R from other rounds / another rig state")
    print(f"  {'doc':<8s}" + "".join(f"{k:>11s}" for k in SUBSETS) + f"{'g2m state':>11s}")
    for tag, section in list(sections.items()) + [("ident-C", ident_c), ("S1", None)]:
        cells = []
        for tags in SUBSETS.values():
            alt = h.FB(store, chain_of, target="h4", fit_tags=tags)
            chain = (chain_of["711b458f"] if tag == "S1"
                     else alt.chain_of_section(section))
            cells.append(f.score_of(h.gain(alt.front(chain), alt.behind(chain))))
        alt = h.FB(store, chain_of, target="g2m")
        chain = chain_of["711b458f"] if tag == "S1" else alt.chain_of_section(section)
        cells.append(f.score_of(h.gain(alt.front(chain), alt.behind(chain))))
        print(f"  {tag:<8s}" + "".join(f"{v:+11.2f}" for v in cells))

    print("\n=== 4. the documents, re-read from disk")
    nm = g.load_json(g.SP / "docs/doc-Nm.json")["sections"]["rear_calibration"]
    for tag, doc in docs.items():
        try:
            section = read_rear_calibration(doc["sections"]["rear_calibration"],
                                            sample_rate=48000)
            read_prescription_document(doc)
            verdict = "PASS"
        except Exception as exc:                   # noqa: BLE001
            verdict = f"FAIL {type(exc).__name__}: {exc}"
            section = doc["sections"]["rear_calibration"]
        notes = [key for key in ("front", "common_delay_ms", "sample_rate_hz",
                                 "valid_band_hz", "phase_convention")
                 if json.dumps(section.get(key), sort_keys=True)
                 != json.dumps(nm.get(key), sort_keys=True)]
        if json.dumps(section["rear"]["bass"], sort_keys=True) != json.dumps(
                nm["rear"]["bass"], sort_keys=True):
            notes.append("rear.bass")
        print(f"  {tag}.json  {verdict}  "
              + (f"DIFFERS FROM Nm: {notes}" if notes
                 else "shares Nm's front, bass, common delay, band and convention")
              + ("  no devices section" if "devices" not in doc.get("sections", {}) else
                 "  HAS A DEVICES SECTION"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
