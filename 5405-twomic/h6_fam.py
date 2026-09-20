#!/usr/bin/env python3
"""Round 3 families and the aggressive front rule.

The hard front box is gone. What replaces it says what the owner actually
pays: any front change a COMMON EQ can undo costs woofer excursion, not
tonality, because the same EQ on both woofers leaves F/B gain untouched. So
the front is floored at -8 dB, charged 0.15 dB of score per dB below -3 so
the solver takes the cheaper of two equal answers, and the bill is reported
as the EQ-back curve rather than hidden in a pass/fail.
"""
from __future__ import annotations

from copy import deepcopy

import numpy as np

import h5lib as h

SCORE_CAP_DB = 12.0
FRONT_BANDS = (80, 100, 125, 160, 200, 250, 315)
HF_BANDS = (400, 500, 630)
FRONT_FLOOR_DB = -8.0
HF_LIMIT_DB = 1.0
SOFT_FROM_DB = -3.0
SOFT_RATE = 0.15
HARD_WEIGHT = 5.0
SOLVE_MARGIN_DB = 0.05
REPORT_TOL_DB = 0.005
#: The brief's cap for agg-3: no EQ-back boost above this anywhere in 63-630 Hz.
#: It never binds -- the best unconstrained solution already sits at 3.29 dB --
#: so agg-3 is solved at GENTLE_PICK_DB instead and the slack is reported.
BRIEF_BOOST_DB = 5.0
GENTLE_PICK_DB = 2.5
GENTLE_BOOST_DB = GENTLE_PICK_DB

IDENT_C_KEEP = (80.0, 300.0, 190.14, 120.0, 365.6582, 391.4937)
IDENT_C_287 = {"type": "Biquad", "parameters": {"type": "Peaking", "freq": 286.9708,
                                                "gain": 0.7095, "q": 0.4252}}
IDENT_C_GAIN_DB = -0.1313
IDENT_C_LOW = (64.3614, 3.9525, 0.6793)

G1_CORE = ((-0.5, 0.2), (0.0, 6.0), (300.0, 472.0))
G2_CORE = ((-1.0, 0.2), (50.0, 90.0), (-4.0, 4.0), (0.5, 3.0))
EXTRA = ((200.0, 320.0), (0.0, 6.0), (1.5, 3.0))
FAMILIES = {"G1a": ("G1", 0), "G1b": ("G1", 1), "G2a": ("G2", 0), "G2b": ("G2", 1)}
CORE = {"G1": G1_CORE, "G2": G2_CORE}


def bounds_for(name):
    family, extra = FAMILIES[name]
    return (*CORE[family], *(EXTRA * extra))


def peaking(freq, gain, q) -> dict:
    return {"type": "Biquad", "parameters": {"type": "Peaking", "freq": round(float(freq), 2),
                                             "gain": round(float(gain), 3),
                                             "q": round(float(q), 3)}}


def cancellation(x, name, ident_c_chain) -> dict:
    family, extra = FAMILIES[name]
    if family == "G1":
        filters = [
            {"type": "BiquadCombo",
             "parameters": {"type": "LinkwitzRileyHighpass", "freq": 80.0, "order": 4}},
            {"type": "BiquadCombo",
             "parameters": {"type": "ButterworthLowpass", "freq": round(float(x[2]), 2),
                            "order": 2}},
            {"type": "Biquad",
             "parameters": {"type": "Peaking", "freq": 190.14, "gain": -6.36, "q": 0.996}},
            peaking(120.0, x[1], 1.0),
        ]
        chain = {"delay_ms": round(float(x[0]), 4), "filters": filters,
                 "gain_db": 0.0, "inverted": True, "muted": False}
        cursor = 3
    else:
        kept = [deepcopy(one) for one in ident_c_chain["filters"]
                if one["parameters"]["freq"] in IDENT_C_KEEP]
        chain = {**deepcopy(ident_c_chain), "delay_ms": round(float(x[0]), 4),
                 "filters": [*kept, peaking(x[1], x[2], x[3]), deepcopy(IDENT_C_287)]}
        cursor = 4
    if extra:
        chain["filters"] = [*chain["filters"], peaking(x[cursor], x[cursor + 1], x[cursor + 2])]
    return chain


def section_with(base, chain) -> dict:
    out = deepcopy(dict(base))
    out["rear"] = {**out["rear"], "cancellation": deepcopy(chain)}
    return out


def guards(front, *, margin=0.0, gentle=False):
    """(hard cost, soft charge, notes). Soft is in dB of score, hard is raw."""
    hard, notes = 0.0, []

    def charge(amount, note):
        nonlocal hard
        if amount > 0.0:
            hard += amount
            if amount > margin + REPORT_TOL_DB:
                notes.append(note)

    for c in FRONT_BANDS:
        charge(FRONT_FLOOR_DB + margin - front[c],
               f"{c} Hz front {front[c]:+.2f} < {FRONT_FLOOR_DB:+.1f}")
    for c in HF_BANDS:
        charge(abs(front[c]) - HF_LIMIT_DB + margin,
               f"{c} Hz front {front[c]:+.2f} outside +-{HF_LIMIT_DB}")
    if gentle:
        for c in h.CENTRES:
            charge(-front[c] - GENTLE_BOOST_DB + margin,
                   f"{c} Hz EQ-back boost {-front[c]:+.2f} > {GENTLE_BOOST_DB}")
    soft = SOFT_RATE * sum(max(0.0, SOFT_FROM_DB - front[c]) for c in FRONT_BANDS)
    return hard, soft, notes


def total_front(front, headroom_db) -> dict:
    """What the owner actually hears: the response change MINUS the cut."""
    return {c: v - headroom_db for c, v in front.items()}


def eq_back(front, headroom_db=0.0) -> tuple[dict[int, float], float]:
    """The common EQ that puts the front back where rear-muted had it.

    Same filter on both woofers, so it cannot move F/B gain -- it only costs
    excursion and headroom. TWO parts, and both are the owner's bill:
    ``headroom_db`` is the broadband cut the product already applies so the
    rear branch sum cannot clip (`rear_branch_sum_headroom_db`, which the
    measurement aligner removes before any response is read), and ``-front``
    is the SHAPE left after that. Their sum is what the volume knob and an EQ
    together have to put back, and its largest value is the price of the tune.
    """
    curve = {c: headroom_db - front[c] for c in h.CENTRES}
    return curve, max(0.0, max(curve.values()))


def score_of(gain) -> float:
    return float(np.mean([min(gain[c], SCORE_CAP_DB) for c in h.SCORE]))


class Objective:
    def __init__(self, model, bases, ident_c_chain):
        self.model = model
        self.bases = bases
        self.ident_c_chain = ident_c_chain

    def section(self, x, name) -> dict:
        return section_with(self.bases[FAMILIES[name][0]],
                            cancellation(x, name, self.ident_c_chain))

    def chain(self, x, name) -> np.ndarray:
        return self.model.chain_of_section(self.section(x, name))

    def headroom(self, section) -> float:
        """The product's own broadband cut for this section, dB."""
        from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
        from jasper.active_speaker.rear_calibration import read_rear_calibration
        return float(rear_branch_sum_headroom_db(
            read_rear_calibration(section, sample_rate=48000)))

    def evaluate(self, chain, angles=None):
        front = self.model.front(chain)
        gain = h.gain(front, self.model.behind(chain, angles))
        return score_of(gain), front, gain

    def cost(self, x, name, angles=None, *, gentle=False) -> float:
        section = self.section(x, name)
        chain = self.model.chain_of_section(section)
        value, front, _gain = self.evaluate(chain, angles)
        total = total_front(front, self.headroom(section))
        hard, soft, _notes = guards(total, margin=SOLVE_MARGIN_DB, gentle=gentle)
        return -(value - soft) + HARD_WEIGHT * hard
