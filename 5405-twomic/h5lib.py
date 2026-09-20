#!/usr/bin/env python3
"""Front-to-back gain: the figure a common EQ cannot move.

``F/B gain = front change - behind change``. Level behind alone was the wrong
target -- S1 buys its rear null partly by making the whole speaker quieter at
100 Hz, which any EQ could undo and which costs the owner 7 dB in front. The
difference of the two changes is what the cardioid really did.

Bands are the third octaves of the coordinator's own ``graphs/fb_table.py``:
centre c, edges c/2^(1/6) .. c*2^(1/6). Front is UNGATED and meaned over the
three arm poses. Behind is ungated at 63-125 Hz and 400-630 Hz and GATED 10 ms
at 160-315 Hz (ungated, the room refills those bands), meaned over +-20.
"""
from __future__ import annotations

import numpy as np

import g5lib as g

CENTRES = (63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630)
GATED = (160, 200, 250, 315)
SCORE = (100, 125, 160, 200, 250, 315)
BANDS = {c: (c / 2 ** (1 / 6), c * 2 ** (1 / 6)) for c in CENTRES}
BEHIND_ANGLES = (20.0, -20.0)
#: Wide enough that the 630 Hz band (up to 707 Hz) has an identified path.
IDENT_BAND = (50.0, 780.0)


def widen():
    g.IDENT_BAND = IDENT_BAND


def band_db(values, centre) -> float:
    low, high = BANDS[centre]
    inside = (g.GRID >= low) & (g.GRID < high)
    return 10.0 * np.log10(np.mean(np.abs(values[inside]) ** 2) + 1e-30)


def rows_of(store, tag, mic):
    return {g.angle_of(pose): row for (t, m, pose), row in store.items()
            if t == tag and m == mic}


class FB:
    """One round's rig state plus an R identified from a chosen set of rounds."""

    def __init__(self, store, chain_of, *, target, fit_tags=None):
        widen()
        fit_tags = set(fit_tags or g.ALL_TAGS)
        self.target = target
        self.front_rows = rows_of(store, target, "main")
        self.behind_rows = rows_of(store, target, "side")
        self.front_r = {a: g.pooled(g.r_estimates(store, chain_of, fit_tags, "main", a,
                                                  gated=False))
                        for a in self.front_rows}
        self.behind_r = {a: g.pooled(g.r_estimates(store, chain_of, fit_tags, "side", a,
                                                   gated=False))
                         for a in BEHIND_ANGLES if a in self.behind_rows}
        live = np.zeros(g.GRID.shape, dtype=bool)
        for model in (*self.front_r.values(), *self.behind_r.values()):
            if model is not None:
                live |= np.abs(model) > 0
        self.sub = np.flatnonzero(live)
        self.grid_sub = g.GRID[self.sub]

    def expand(self, chain_sub) -> np.ndarray:
        out = np.zeros(g.GRID.shape, dtype=complex)
        out[self.sub] = chain_sub
        return out

    def chain_of_section(self, section) -> np.ndarray:
        return self.expand(g.rear_stage_response(section, self.grid_sub)[0])

    def front(self, chain) -> dict[int, float]:
        cells = {c: [] for c in CENTRES}
        for angle, row in self.front_rows.items():
            model = self.front_r[angle]
            if model is None:
                continue
            predicted = row["muted"] + model * chain
            for c in CENTRES:
                cells[c].append(band_db(predicted, c) - band_db(row["muted"], c))
        return {c: float(np.mean(v)) for c, v in cells.items()}

    def behind(self, chain, angles=None) -> dict[int, float]:
        cells = {c: [] for c in CENTRES}
        for angle, model in self.behind_r.items():
            row = self.behind_rows[angle]
            if model is None or (angles is not None and angle not in angles):
                continue
            predicted = row["muted"] + model * chain
            gated = g.gate(predicted, row["taper"])
            for c in CENTRES:
                if c in GATED:
                    cells[c].append(band_db(gated, c) - band_db(row["muted_g"], c))
                else:
                    cells[c].append(band_db(predicted, c) - band_db(row["muted"], c))
        return {c: float(np.mean(v)) for c, v in cells.items()}

    def measured_front(self, fp) -> dict[int, float]:
        cells = {c: [] for c in CENTRES}
        for _angle, row in self.front_rows.items():
            if fp not in row["cands"]:
                continue
            take = row["cands"][fp]
            for c in CENTRES:
                cells[c].append(band_db(take, c) - band_db(row["muted"], c))
        return {c: float(np.mean(v)) if v else float("nan") for c, v in cells.items()}

    def measured_behind(self, fp) -> dict[int, float]:
        cells = {c: [] for c in CENTRES}
        for angle in BEHIND_ANGLES:
            row = self.behind_rows.get(angle)
            if row is None or fp not in row["cands"]:
                continue
            take = row["cands"][fp]
            gated = g.gate(take, row["taper"])
            for c in CENTRES:
                if c in GATED:
                    cells[c].append(band_db(gated, c) - band_db(row["muted_g"], c))
                else:
                    cells[c].append(band_db(take, c) - band_db(row["muted"], c))
        return {c: float(np.mean(v)) if v else float("nan") for c, v in cells.items()}

    def measured_behind_ungated(self, fp) -> dict[int, float]:
        cells = {c: [] for c in CENTRES}
        for angle in BEHIND_ANGLES:
            row = self.behind_rows.get(angle)
            if row is None or fp not in row["cands"]:
                continue
            for c in CENTRES:
                cells[c].append(band_db(row["cands"][fp], c) - band_db(row["muted"], c))
        return {c: float(np.mean(v)) if v else float("nan") for c, v in cells.items()}


def gain(front, behind) -> dict[int, float]:
    return {c: front[c] - behind[c] for c in CENTRES}


def line(values, fmt="{:+7.1f}") -> str:
    return "".join(fmt.format(values[c]) for c in CENTRES)
