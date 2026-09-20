"""WALL2 tables: the six tunes, prediction check, repeatability against wall1c.

The builder's `seat_change` / `front_total` predictions are referenced to C0
(rear off, EMPTY front chain), so the prediction check uses C0. Everything the
brief asks for as a COST is referenced to B0, the fair off: B0 carries the same
front chain as A0, so a change against it is the rear stage alone.
"""
from __future__ import annotations

import json

import numpy as np

from ba_lib import POSE0, SP, curve, load, smooth
from wall_numbers import THIRDS, third_octave, trend_rms, wall_hole

LABELS = {"C0": "62a97fbc", "B0": "1f65d837", "A0": "1b2915e5",
          "seat-1": "1feb7466", "seat-2": "6f60360c", "wall170": "f4a56053"}
PRED_KEY = {"seat-1": "S-b", "seat-2": "seat-2"}


def bands(result, mic, fp8, ref8):
    f, a = curve(result, mic, POSE0, fp8)
    _, b = curve(result, mic, POSE0, ref8)
    sa, sb = smooth(f, a, 1 / 3.0), smooth(f, b, 1 / 3.0)
    return {str(c): round(third_octave(f, sa, c) - third_octave(f, sb, c), 2) for c in THIRDS}


def main():
    w2 = load(SP / "search" / "BA" / "res-wall2.json")
    w1 = load(SP / "search" / "BA" / "res-wall1c.json")
    pred = json.loads((SP / "cands9" / "summary.json").read_text())

    out = {"round": "dfe333aea6d3 (wall2)", "previous": "0d0abbb03574 (wall1c)",
           "normalisation": "0 dB = each curve's own mean over 500 Hz - 2 kHz",
           "cost_reference": "B0 (fair off: same front chain as A0)",
           "prediction_reference": "C0 (the builder's own reference)",
           "summary": {}, "vs_B0": {"seat": {}, "front": {}},
           "prediction_check": {}, "repeatability_wall1c_vs_wall2": {}}

    print(f"{'tune':8s} {'seat hole':>16s} {'seatRMS':>8s} {'front hole':>16s} {'frontRMS':>9s} {'EQ-back':>8s}")
    for tag, fp8 in LABELS.items():
        sf, sm = curve(w2, "side", POSE0, fp8)
        ff, fm = curve(w2, "main", POSE0, fp8)
        sh, shz = wall_hole(sf, sm)
        fh, fhz = wall_hole(ff, fm)
        front_vs_b0 = bands(w2, "main", fp8, LABELS["B0"])
        seat_vs_b0 = bands(w2, "side", fp8, LABELS["B0"])
        eq_back = round(-min(front_vs_b0.values()), 2)
        out["summary"][tag] = {
            "seat_hole_db": sh, "seat_hole_hz": shz, "seat_rms_db": trend_rms(sf, sm),
            "front_hole_db": fh, "front_hole_hz": fhz, "front_rms_db": trend_rms(ff, fm),
            "front_eq_back_vs_B0_db": eq_back}
        out["vs_B0"]["seat"][tag] = seat_vs_b0
        out["vs_B0"]["front"][tag] = front_vs_b0
        print(f"{tag:8s} {sh:+7.2f} @ {shz:6.1f} {trend_rms(sf, sm):8.2f} "
              f"{fh:+7.2f} @ {fhz:6.1f} {trend_rms(ff, fm):9.2f} {eq_back:8.2f}")

    print("\nprediction check (seat, vs C0 -- the builder's reference)")
    for tag, key in PRED_KEY.items():
        fig = pred["results"][key]["fig"]
        sf, sm = curve(w2, "side", POSE0, LABELS[tag])
        got_rms, (got_hole, got_hz) = trend_rms(sf, sm), wall_hole(sf, sm)
        got_bands = bands(w2, "side", LABELS[tag], LABELS["C0"])
        errs = {k: round(got_bands[k] - v, 2) for k, v in fig["seat_change"].items() if k in got_bands}
        worst = max(errs, key=lambda k: abs(errs[k]))
        out["prediction_check"][tag] = {
            "seat_rms": {"predicted": fig["seat"]["rms"], "measured": got_rms,
                         "error": round(got_rms - fig["seat"]["rms"], 2)},
            "seat_hole_db": {"predicted": fig["seat"]["hole_db"], "measured": got_hole,
                             "error": round(got_hole - fig["seat"]["hole_db"], 2)},
            "seat_change_vs_C0": {"predicted": fig["seat_change"], "measured": got_bands,
                                  "error": errs,
                                  "mean_abs": round(float(np.mean([abs(v) for v in errs.values()])), 2),
                                  "worst_band_hz": worst, "worst_db": errs[worst]},
        }
        row = out["prediction_check"][tag]
        print(f"  {tag}: RMS pred {fig['seat']['rms']:.2f} -> meas {got_rms:.2f} "
              f"({row['seat_rms']['error']:+.2f}); hole pred {fig['seat']['hole_db']:+.2f} -> "
              f"meas {got_hole:+.2f} ({row['seat_hole_db']['error']:+.2f}); "
              f"per-band err mean {row['seat_change_vs_C0']['mean_abs']:.2f} dB, "
              f"worst {errs[worst]:+.2f} at {worst} Hz")

    print("\nrepeatability, same tunes ~2 h apart (wall1c -> wall2)")
    for tag in ("C0", "B0", "A0"):
        row = {}
        for mic in ("side", "main"):
            f1, m1 = curve(w1, mic, POSE0, LABELS[tag])
            f2, m2 = curve(w2, mic, POSE0, LABELS[tag])
            h1, z1 = wall_hole(f1, m1)
            h2, z2 = wall_hole(f2, m2)
            row[mic] = {"rms": [trend_rms(f1, m1), trend_rms(f2, m2),
                                round(trend_rms(f2, m2) - trend_rms(f1, m1), 2)],
                        "hole_db": [h1, h2, round(h2 - h1, 2)], "hole_hz": [z1, z2]}
        out["repeatability_wall1c_vs_wall2"][tag] = row
        s, f = row["side"], row["main"]
        print(f"  {tag}: seat RMS {s['rms'][0]:.2f}->{s['rms'][1]:.2f} ({s['rms'][2]:+.2f})  "
              f"hole {s['hole_db'][0]:+.2f}->{s['hole_db'][1]:+.2f} ({s['hole_db'][2]:+.2f}) | "
              f"front RMS {f['rms'][0]:.2f}->{f['rms'][1]:.2f} ({f['rms'][2]:+.2f})  "
              f"hole {f['hole_db'][0]:+.2f}->{f['hole_db'][1]:+.2f} ({f['hole_db'][2]:+.2f})")

    (SP / "graphs" / "wall2-numbers.json").write_text(json.dumps(out, indent=1) + "\n")
    print("\nseat change vs B0 per third octave:")
    for tag in LABELS:
        print(f"  {tag:8s} " + " ".join(f"{k}:{v:+.1f}" for k, v in out['vs_B0']['seat'][tag].items()))
    print("front change vs B0 per third octave:")
    for tag in LABELS:
        print(f"  {tag:8s} " + " ".join(f"{k}:{v:+.1f}" for k, v in out['vs_B0']['front'][tag].items()))
    print(SP / "graphs" / "wall2-numbers.json")


if __name__ == "__main__":
    main()
