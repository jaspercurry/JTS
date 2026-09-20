#!/usr/bin/env python3
"""The two trust regions, as chain builders, plus the guards and the score.

A trust region, not a bounding box for its own sake: S2 was predicted -8.2 dB
and measured -0.6 because the model was asked about a shape nothing had ever
measured. Both families here stay on shapes the rig has actually played.
"""
from __future__ import annotations

from copy import deepcopy

import numpy as np

import h5lib as h

SCORE_CAP_DB = 12.0
FRONT_LOW = (80, 100, 125, 160, 200, 250, 315)
FRONT_HIGH = (400, 500, 630)
FRONT_MIN_DB, FRONT_MAX_DB = -3.0, 3.0
FRONT_HF_DB = 0.7
GUARD_B_SLACK_DB = 0.5
PENALTY = 5.0
E035 = "27a565a9"
IDENT_C = "2fc52a19"

#: F1 -- the N1 / e-0.35 shape. x = [delay, g120, corner] (+ freq, gain, q).
F1_CORE = ((-0.5, 0.2), (0.0, 6.0), (300.0, 472.0))
#: F2 -- ident-C, with only its delay and its 64 Hz Peaking gain free.
F2_CORE = ((-1.1188, -0.7188), (0.0, 3.9525))
EXTRA = ((200.0, 320.0), (0.0, 6.0), (1.5, 3.0))
FAMILIES = {"F1a": ("F1", 0), "F1b": ("F1", 1), "F2a": ("F2", 0), "F2b": ("F2", 1)}


def bounds_for(name):
    family, extra = FAMILIES[name]
    core = F1_CORE if family == "F1" else F2_CORE
    return (*core, *(EXTRA * extra))


def peaking(freq, gain, q) -> dict:
    return {"type": "Biquad", "parameters": {"type": "Peaking", "freq": round(float(freq), 2),
                                             "gain": round(float(gain), 3),
                                             "q": round(float(q), 3)}}


def cancellation(x, name, ident_c_chain) -> dict:
    family, extra = FAMILIES[name]
    if family == "F1":
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
        filters = []
        for one in ident_c_chain["filters"]:
            one = deepcopy(one)
            if one["parameters"].get("freq") == 64.3614:
                one["parameters"]["gain"] = round(float(x[1]), 4)
            filters.append(one)
        chain = {**deepcopy(ident_c_chain), "delay_ms": round(float(x[0]), 4),
                 "filters": filters}
        cursor = 2
    if extra:
        chain["filters"] = [*chain["filters"], peaking(x[cursor], x[cursor + 1], x[cursor + 2])]
    return chain


def section_with(base, chain) -> dict:
    out = deepcopy(dict(base))
    out["rear"] = {**out["rear"], "cancellation": deepcopy(chain)}
    return out


#: The optimiser is steered to this much INSIDE each limit, so a solution that
#: sits on the boundary is emitted strictly inside it. Reporting uses the
#: stated limit with a rounding tolerance, never the margin.
SOLVE_MARGIN_DB = 0.05
REPORT_TOL_DB = 0.005


def guard_violation(front, e035_80, *, margin=0.0, tol=REPORT_TOL_DB):
    cost, notes = 0.0, []

    def charge(amount, note):
        nonlocal cost
        if amount > 0.0:
            cost += amount
            if amount > margin + tol:
                notes.append(note)

    for c in FRONT_LOW:
        charge(FRONT_MIN_DB + margin - front[c],
               f"{c} Hz front {front[c]:+.2f} < {FRONT_MIN_DB:+.1f}")
        charge(front[c] - FRONT_MAX_DB + margin,
               f"{c} Hz front {front[c]:+.2f} > {FRONT_MAX_DB:+.1f}")
    for c in FRONT_HIGH:
        charge(abs(front[c]) - FRONT_HF_DB + margin,
               f"{c} Hz front {front[c]:+.2f} outside +-{FRONT_HF_DB}")
    charge(e035_80 - GUARD_B_SLACK_DB + margin - front[80],
           f"80 Hz front {front[80] - e035_80:+.2f} vs e-0.35")
    return cost, notes


def score_of(gain) -> float:
    """Mean over the scored bands of the F/B gain, each capped at +12 dB."""
    return float(np.mean([min(gain[c], SCORE_CAP_DB) for c in h.SCORE]))


class Objective:
    def __init__(self, model, base, ident_c_section, e035_chain):
        self.model = model
        self.base = {"F1": base, "F2": ident_c_section}
        self.ident_c_chain = ident_c_section["rear"]["cancellation"]
        self.e035_80 = model.front(e035_chain)[80]

    def section(self, x, name) -> dict:
        return section_with(self.base[FAMILIES[name][0]],
                            cancellation(x, name, self.ident_c_chain))

    def chain(self, x, name) -> np.ndarray:
        return self.model.chain_of_section(self.section(x, name))

    def evaluate(self, chain, angles=None) -> tuple[float, dict, dict, dict]:
        front = self.model.front(chain)
        behind = self.model.behind(chain, angles)
        gain = h.gain(front, behind)
        return score_of(gain), front, behind, gain

    def cost(self, x, name, angles=None) -> float:
        value, front, _behind, _gain = self.evaluate(self.chain(x, name), angles)
        return -value + PENALTY * guard_violation(
            front, self.e035_80, margin=SOLVE_MARGIN_DB)[0]
