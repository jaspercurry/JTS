#!/usr/bin/env python3
"""The forward model the solver optimises against, and the front guards.

Behind: prediction method B -- identify R ungated, predict ungated, window the
prediction. It validated at least as well as gated identification on both
held-out splits and is the only one that is right in principle.

In front: the same model on the MAIN mic, evaluated ungated, because all three
front guards are ungated band energies.
"""
from __future__ import annotations

from copy import deepcopy

import numpy as np

import g5lib as g

WEIGHTS = {20.0: 1.0, -20.0: 1.0, 0.0: 0.5}
GUARD_BANDS = (("100-350", (100.0, 350.0)), ("71-90", (71.0, 90.0)),
               ("350-5k", (350.0, 5000.0)))
GUARD_A_MIN_DB = -2.0
GUARD_B_SLACK_DB = 0.5
GUARD_C_MAX_DB = 0.4
PENALTY_WEIGHT = 5.0
E035 = "27a565a9"
CORNER = (300.0, 600.0)
DELAY = (-1.0, 1.0)
G120 = (0.0, 6.0)
PEAKING = ((140.0, 400.0), (-6.0, 6.0), (0.5, 3.0))
ALLPASS = ((120.0, 500.0), (0.5, 6.0))
STAGES = {"S1": (1, 0), "S2": (3, 1), "S3": (3, 2)}


def bounds_for(shape):
    n_pk, n_ap = shape
    return (CORNER, DELAY, G120, *(PEAKING * n_pk), *(ALLPASS * n_ap))


def cancellation(x, shape) -> dict:
    """The cancellation branch this parameter vector describes.

    LinkwitzRileyHighpass 80 Hz and the carried 190.14 Hz Peaking are fixed;
    no added filter sits below 140 Hz, and the 120 Hz Peaking keeps N1's freq
    and q so only its gain moves.
    """
    n_pk, n_ap = shape
    filters = [
        {"type": "BiquadCombo",
         "parameters": {"type": "LinkwitzRileyHighpass", "freq": 80.0, "order": 4}},
        {"type": "BiquadCombo",
         "parameters": {"type": "ButterworthLowpass", "freq": round(float(x[0]), 2), "order": 2}},
        {"type": "Biquad",
         "parameters": {"type": "Peaking", "freq": 190.14, "gain": -6.36, "q": 0.996}},
        {"type": "Biquad",
         "parameters": {"type": "Peaking", "freq": 120.0, "gain": round(float(x[2]), 3),
                        "q": 1.0}},
    ]
    cursor = 3
    for _ in range(n_pk):
        filters.append({"type": "Biquad", "parameters": {
            "type": "Peaking", "freq": round(float(x[cursor]), 2),
            "gain": round(float(x[cursor + 1]), 3), "q": round(float(x[cursor + 2]), 3)}})
        cursor += 3
    for _ in range(n_ap):
        filters.append({"type": "Biquad", "parameters": {
            "type": "Allpass", "freq": round(float(x[cursor]), 2),
            "q": round(float(x[cursor + 1]), 3)}})
        cursor += 2
    return {"delay_ms": round(float(x[1]), 4), "filters": filters,
            "gain_db": 0.0, "inverted": True, "muted": False}


def section_with(base, chain) -> dict:
    out = deepcopy(dict(base))
    out["rear"] = {**out["rear"], "cancellation": deepcopy(chain)}
    return out


class Model:
    """Everything the objective needs, bound to one measured rig state.

    ``sub`` is the only place the chain has to be evaluated: ``R`` is nan
    outside the identification band and zero after ``nan_to_num``, so
    ``R * c`` is identically zero there whatever the chain does. The product's
    evaluator is a per-bin Python loop, so this is a 35x saving, not a tidy-up.
    """

    def __init__(self, store, chain_of, *, score_round="l1", fit_tags=None):
        fit_tags = set(fit_tags or g.ALL_TAGS)
        self.behind = {}
        for angle in WEIGHTS:
            key = (score_round, "side",
                   next(p for (t, m, p) in store if t == score_round and m == "side"
                        and g.angle_of(p) == angle))
            row = store[key]
            model = g.pooled(g.r_estimates(store, chain_of, fit_tags, "side", angle,
                                           gated=False))
            self.behind[angle] = (row, model)
        self.front = {}
        front_r = g.pooled([e for angle in WEIGHTS
                            for e in g.r_estimates(store, chain_of, fit_tags, "main", angle,
                                                   gated=False)])
        for (tag, mic, pose), row in store.items():
            if tag != score_round or mic != "main":
                continue
            self.front[g.angle_of(pose)] = (row, front_r)
        live = np.zeros(g.GRID.shape, dtype=bool)
        for row, model in (*self.behind.values(), *self.front.values()):
            live |= np.abs(model) > 0
        self.sub = np.flatnonzero(live)
        self.grid_sub = g.GRID[self.sub]
        self.e035_guard_b = None
        self.e035_guard_b = min(self.front_guards(chain_of[E035])[1])

    def expand(self, chain_sub) -> np.ndarray:
        out = np.zeros(g.GRID.shape, dtype=complex)
        out[self.sub] = chain_sub
        return out

    def chain_of_x(self, x, shape, base_section) -> np.ndarray:
        from jasper.active_speaker.branch_chain import rear_stage_response
        section = section_with(base_section, cancellation(x, shape))
        return self.expand(rear_stage_response(section, self.grid_sub)[0])

    def bands_behind(self, chain) -> dict[float, list[float]]:
        return {angle: g.predict_bands(row, chain, r_full=model)
                for angle, (row, model) in self.behind.items()}

    def front_guards(self, chain) -> tuple[list[float], list[float], list[float]]:
        """Per main-mic pose: guard A, guard B and guard C changes, dB."""
        out = [[], [], []]
        for _angle, (row, model) in sorted(self.front.items()):
            predicted = row["muted"] + model * chain
            for i, (_name, band) in enumerate(GUARD_BANDS):
                out[i].append(g.band_db(predicted, band) - g.band_db(row["muted"], band))
        return tuple(out)

    def penalty(self, chain) -> float:
        a, b, c = self.front_guards(chain)
        cost = sum(max(0.0, GUARD_A_MIN_DB - v) for v in a)
        cost += sum(max(0.0, abs(v) - GUARD_C_MAX_DB) for v in c)
        if self.e035_guard_b is not None:
            cost += sum(max(0.0, self.e035_guard_b - GUARD_B_SLACK_DB - v) for v in b)
        return cost

    def score(self, chain, angles=None) -> tuple[float, dict[float, float]]:
        bands = self.bands_behind(chain)
        per = {angle: g.capped_mean(values) for angle, values in bands.items()
               if angles is None or angle in angles}
        weight = sum(WEIGHTS[a] for a in per)
        return float(sum(WEIGHTS[a] * v for a, v in per.items()) / weight), per

    def objective(self, x, shape, base_section, angles=None) -> float:
        chain = self.chain_of_x(x, shape, base_section)
        total, _per = self.score(chain, angles)
        return total + PENALTY_WEIGHT * self.penalty(chain)
