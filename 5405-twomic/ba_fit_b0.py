"""Fit B0's front-chain compensation: measured (agg-1 - Nm) front, pose 0.

B0 is a rear-muted copy of agg-1 with extra filters on its FRONT chain. With
the rear muted the stage's headroom charge is the front chain's own peak, so
the split is exact: ``front.gain_db`` carries agg-1's broadband headroom cut
(0.6227 dB, the product's own charge) and the fitted bells carry the SHAPE.
"""
from __future__ import annotations

import copy
import json

import numpy as np
from scipy.optimize import least_squares

from ba_lib import POSE0, SP, curve, filter_response_db, load, peaking, smooth

AGG1, NM = "bf03e5b6", "0caaa048"
ROUNDS = ("e5f73ee228bc", "433113c88326")
# Below 40 Hz the two A1/A2 rounds disagree by up to 3.5 dB per third octave
# (mic/room noise under the woofer's own roll-off), so there is no target there.
FIT_LO, FIT_HI = 40.0, 5000.0
AGG1_FRONT_GAIN_DB = -0.51


def headroom(section):
    from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
    from jasper.active_speaker.rear_calibration import read_rear_calibration
    return rear_branch_sum_headroom_db(read_rear_calibration(section, sample_rate=48000))


def target(agg_section):
    diffs, freqs = [], None
    for name in ROUNDS:
        res = load(SP / "search" / "BA" / f"res-{name}.json")
        freqs, a = curve(res, "main", POSE0, AGG1)
        _, n = curve(res, "main", POSE0, NM)
        diffs.append(a - n)
    raw = np.mean(diffs, axis=0)
    # SHAPE = what the filters must do; the broadband cut rides in gain_db.
    shape = smooth(freqs, raw, 1 / 3.0) + headroom(agg_section)
    return freqs, shape, raw, [smooth(freqs, d, 1 / 3.0) for d in diffs]


def unpack(x, n):
    return [peaking(10 ** x[3 * i], x[3 * i + 1], x[3 * i + 2]) for i in range(n)]


def fit(freqs, tgt, seeds, sel):
    n = len(seeds)
    x0 = np.array([v for s in seeds for v in (np.log10(s[0]), s[1], s[2])])
    # Centres stay inside 60-800 Hz. Above 800 Hz agg-1 and its rear-muted copy
    # are electrically identical; below 60 Hz the target is inside the
    # round-to-round spread, so a bell either side would be fitting noise.
    lo = np.array([v for _ in seeds for v in (np.log10(60.0), -6.0, 0.3)])
    hi = np.array([v for _ in seeds for v in (np.log10(800.0), 6.0, 4.0)])
    out = least_squares(lambda x: (filter_response_db(unpack(x, n), freqs) - tgt)[sel],
                        x0, bounds=(lo, hi), max_nfev=30000)
    return unpack(out.x, n)


def main():
    agg = json.load(open(SP / "cands7" / "agg-1.json"))["sections"]["rear_calibration"]
    cut = headroom(agg)
    freqs, tgt, raw, per_round = target(agg)
    sel = (freqs >= FIT_LO) & (freqs <= FIT_HI)
    seeds = [(100, -3, 1.5), (250, 3, 2.0), (170, 1, 2.0), (500, -1, 1.5)]
    chosen = None
    for n in (4,):
        filters = fit(freqs, tgt, seeds[:n], sel)
        got = filter_response_db(filters, freqs)
        worst = float(np.max(np.abs(got - tgt)[sel]))
        rms = float(np.sqrt(np.mean(((got - tgt)[sel]) ** 2)))
        print(f"n={n} worst={worst:.2f} dB rms={rms:.2f} dB")
        chosen = (filters, worst, rms)
    filters, worst, rms = chosen

    b0 = copy.deepcopy(agg)
    b0["rear_muted"] = True
    b0["front"] = {**copy.deepcopy(agg["front"]),
                   "gain_db": round(AGG1_FRONT_GAIN_DB - cut, 4),
                   "filters": copy.deepcopy(agg["front"]["filters"]) + filters}
    hr_b0 = headroom(b0)
    print(f"agg-1 headroom cut {cut:.4f} dB -> B0 front.gain_db {b0['front']['gain_db']}")
    print(f"hr(B0) = {hr_b0:.4f} dB (must be 0 for the split to be exact)")
    for f in filters:
        p = f["parameters"]
        print(f"  Peaking {p['freq']:7.2f} Hz {p['gain']:+6.2f} dB q{p['q']:.3f}")
    got = filter_response_db(filters, freqs)
    print("  band | target(shape) | fit | modelled B0-A0")
    for c in (31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 1000, 4000):
        i = int(np.argmin(np.abs(freqs - c)))
        print(f"  {c:7.1f} {tgt[i]:+6.2f} {got[i]:+6.2f} {got[i] - tgt[i]:+6.2f}")
    (SP / "search" / "BA" / "b0-fit.json").write_text(json.dumps({
        "filters": filters, "front_gain_db": b0["front"]["gain_db"],
        "headroom_cut_db": cut, "hr_b0_db": hr_b0, "worst_db": worst, "rms_db": rms,
        "freqs_hz": [round(float(f), 4) for f in freqs],
        "shape_target_db": [round(float(v), 4) for v in tgt],
        "fit_db": [round(float(v), 4) for v in got],
        "raw_diff_db": [round(float(v), 4) for v in raw],
        "per_round_db": [[round(float(v), 4) for v in d] for d in per_round],
    }, indent=1) + "\n")
    (SP / "search" / "BA" / "b0-section.json").write_text(json.dumps(b0, indent=1) + "\n")
    print("wrote b0-fit.json and b0-section.json")


if __name__ == "__main__":
    main()
