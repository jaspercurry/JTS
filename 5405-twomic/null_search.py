#!/usr/bin/env python3
"""Minimise the sound BEHIND the speaker directly, by searching the
cancellation branch -- not by fitting a rear/front ratio.

    J(section) = mean over the 3 rear angles of
                 10*log10( mean over a log grid 100-350 Hz of |predicted|^2 / |muted|^2 )

``predicted`` and ``muted`` come from :mod:`rearpred`, which is
``rear_preview._position``'s own model. The FRONT chain and the BASS branch are
doc-N1's, verbatim, so doc-Nm (N1 with ``rear_muted``) stays the valid measured
zero; only the CANCELLATION branch moves.

``common_delay_ms`` does not enter J. Every term of both curves carries the
same ``exp(-j w (common + front.delay))`` factor, so the ratio cannot see it;
it exists only to keep each emitted branch delay at or above zero
(``contract.json`` ``emitted_delay_rule``), and is set once at the end.

Stages, each reported: (A) gain x delay with N1's filters, (B) + both corners
and polarity, (C) + two Peaking and one Allpass. B and C are also fitted on two
rear angles and scored on the held-out third.

``--objective early`` swaps the ratio for the EARLY energy of the same two
curves (``rearpred.early_change_db``'s window). The ungated objective measures
TOTAL energy in a 105 ms window at 0.61 m from a cabinet in a room, so it
counts the room tail the rear woofer fills as well as the direct sound it
cancels; the early objective counts only the direct arrival, which is what a
cardioid acts on.
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from scipy.optimize import differential_evolution, minimize

from jasper.active_speaker.branch_chain import rear_stage_chain_response
from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db

from jasper.audio_measurement.rear_evidence import EARLY_WINDOW_MS

from rearpred import (
    FRONT_GUARD_BAND_HZ, GUARD_POINTS_PER_OCTAVE, NULL_BAND_HZ, SCORE_POINTS_PER_OCTAVE,
    early_change_db, energy_change_db, load_positions, log_bins, predicted_pair,
    section_of, third_octave_changes,
)

SEED = 20260919
#: Penalty weight, dB of objective per dB of breach. Steep enough that the
#: search never buys a null with the forward response, gentle enough that the
#: optimiser can still walk out of an infeasible start.
PENALTY_WEIGHT = 5.0
#: The forward response's two guarantees: the front poses may not lose more
#: than this over the null band, and may not move more than this over the
#: guard band.
FRONT_NULL_FLOOR_DB = -2.0
FRONT_GUARD_TOLERANCE_DB = 0.4

#: Contract bounds (``docs/contract.json`` ``rear.bounds``). NOTE: the contract
#: caps q at 1.0 for Lowpass/Highpass/shelves and at 10.0 for Allpass, but
#: places NO cap on a Peaking's q -- only its +6 dB gain. The Peaking q here is
#: held to 1.0 anyway, which is the brief's "resonant Q <= 1".
PEAKING_GAIN_MAX_DB = 6.0
PEAKING_Q_MAX = 1.0
ALLPASS_Q_MAX = 10.0

BOUNDS_A = ((-30.0, 0.0), (-2.0, 3.0))                       # gain dB, delay ms
BOUNDS_B = (*BOUNDS_A, (60.0, 120.0), (250.0, 500.0))        # + high-pass, low-pass corner
BOUNDS_C = (*BOUNDS_B,
            (60.0, 500.0), (-12.0, PEAKING_GAIN_MAX_DB), (0.3, PEAKING_Q_MAX),
            (60.0, 500.0), (-12.0, PEAKING_GAIN_MAX_DB), (0.3, PEAKING_Q_MAX),
            (60.0, 500.0), (0.3, ALLPASS_Q_MAX))             # + 2 Peaking, 1 Allpass


def peaking(freq: float, gain: float, q: float) -> dict[str, Any]:
    return {"type": "Biquad", "parameters": {"type": "Peaking", "freq": round(float(freq), 4),
                                             "gain": round(float(gain), 4), "q": round(float(q), 4)}}


def cancellation_of(base: Mapping[str, Any], x: np.ndarray, *,
                    inverted: bool) -> dict[str, Any]:
    """The cancellation branch a parameter vector describes, on N1's structure.

    Stage A keeps every filter; B re-corners the two ``BiquadCombo`` passes the
    branch already has and may flip the polarity; C appends two Peaking and one
    Allpass after them.
    """
    chain = deepcopy(base)
    chain["gain_db"] = round(float(x[0]), 4)
    chain["delay_ms"] = round(float(x[1]), 4)
    chain["inverted"] = bool(inverted)
    if len(x) >= 4:
        corners = iter((x[2], x[3]))
        for item in chain["filters"]:
            if item["type"] == "BiquadCombo":
                item["parameters"]["freq"] = round(float(next(corners)), 4)
    if len(x) >= 12:
        chain["filters"] = [*chain["filters"],
                            peaking(x[4], x[5], x[6]), peaking(x[7], x[8], x[9]),
                            {"type": "Biquad", "parameters": {
                                "type": "Allpass", "freq": round(float(x[10]), 4),
                                "q": round(float(x[11]), 4)}}]
    return chain


class Model:
    """Everything that does not change during a search, precomputed once.

    The two curves are evaluated on ONE reduced grid -- the null band's log
    bins plus the guard band's -- because a full 16k-bin transform per
    candidate would make a 20,000-evaluation search hours long. Every position
    shares the pair grid, so one set of bin indices serves all six.
    """

    def __init__(self, positions: Mapping[str, Any], section: Mapping[str, Any],
                 *, objective: str = "ungated") -> None:
        self.mode = objective
        sample = next(iter(positions.values()))
        freqs = sample["freqs_hz"]
        score = log_bins(freqs, NULL_BAND_HZ, SCORE_POINTS_PER_OCTAVE)
        guard = log_bins(freqs, FRONT_GUARD_BAND_HZ, GUARD_POINTS_PER_OCTAVE)
        self.take = np.concatenate([score, guard])
        self.score = slice(0, score.size)
        self.guard = slice(score.size, self.take.size)
        self.grid = freqs[self.take]
        self.section = section
        # common_delay and front.delay cancel in the ratio (see the module
        # docstring), so both curves are built at zero shared delay here.
        front_chain = rear_stage_chain_response(section["front"], self.grid, delay_ms=0.0,
                                                extra_filters=section["boundary"]["front"])
        bass = rear_stage_chain_response(section["rear"]["bass"], self.grid,
                                         delay_ms=float(section["rear"]["bass"]["delay_ms"]))
        self.rear_keys = [key for key, row in positions.items() if row["mic"] == "side"]
        self.front_keys = [key for key, row in positions.items() if row["mic"] == "main"]
        self.muted, self.rear_tf, self.with_bass = {}, {}, {}
        for key, row in positions.items():
            self.muted[key] = row["H_front"][self.take] * front_chain
            self.rear_tf[key] = row["H_rear"][self.take]
            self.with_bass[key] = self.muted[key] + self.rear_tf[key] * bass
        # Early-energy machinery. Only the null band's bins survive
        # ``band_limited_impulse``'s mask, so the chain is evaluated on those
        # and the rest of the spectrum is left at zero: the transform is the
        # same, at a fraction of the cost.
        self.full_hz = freqs
        self.fft_n = 2 * (freqs.size - 1)
        self.band = np.flatnonzero((freqs >= NULL_BAND_HZ[0]) & (freqs <= NULL_BAND_HZ[1]))
        band_hz = freqs[self.band]
        self.band_front = rear_stage_chain_response(
            section["front"], band_hz, delay_ms=0.0, extra_filters=section["boundary"]["front"])
        self.band_bass = rear_stage_chain_response(
            section["rear"]["bass"], band_hz, delay_ms=float(section["rear"]["bass"]["delay_ms"]))
        self.band_hz = band_hz
        self.band_muted, self.band_rear, self.early_window, self.muted_early = {}, {}, {}, {}
        for key, row in positions.items():
            self.band_muted[key] = row["H_front"][self.band] * self.band_front
            self.band_rear[key] = row["H_rear"][self.band]
            impulse = self._impulse(self.band_muted[key])
            peak = int(np.argmax(np.abs(impulse)))
            time_ms = (np.arange(impulse.size) - peak) * (1000.0 / 48000.0)
            self.early_window[key] = (time_ms >= EARLY_WINDOW_MS[0]) & (time_ms < EARLY_WINDOW_MS[1])
            self.muted_early[key] = max(float(np.sum(impulse[self.early_window[key]] ** 2)), 1e-30)

    def _impulse(self, band_values: np.ndarray) -> np.ndarray:
        spectrum = np.zeros(self.full_hz.size, dtype=np.complex128)
        spectrum[self.band] = band_values
        return np.fft.irfft(spectrum, n=self.fft_n)

    def early_db(self, chain: Mapping[str, Any], key: str) -> float:
        cancellation = rear_stage_chain_response(chain, self.band_hz,
                                                 delay_ms=float(chain["delay_ms"]))
        predicted = (self.band_muted[key]
                     + self.band_rear[key] * (self.band_bass + cancellation))
        impulse = self._impulse(predicted)
        energy = max(float(np.sum(impulse[self.early_window[key]] ** 2)), 1e-30)
        return 10.0 * np.log10(energy / self.muted_early[key])

    def predicted(self, chain: Mapping[str, Any], key: str) -> np.ndarray:
        cancellation = rear_stage_chain_response(chain, self.grid,
                                                 delay_ms=float(chain["delay_ms"]))
        return self.with_bass[key] + self.rear_tf[key] * cancellation

    def rear_db(self, chain: Mapping[str, Any], keys=None) -> list[float]:
        wanted = self.rear_keys if keys is None else keys
        if self.mode == "early":
            return [self.early_db(chain, key) for key in wanted]
        return [energy_change_db(self.muted[key][self.score],
                                 self.predicted(chain, key)[self.score])
                for key in wanted]

    def penalty(self, chain: Mapping[str, Any]) -> float:
        total = 0.0
        for key in self.front_keys:
            predicted = self.predicted(chain, key)
            # The forward null-band floor follows the objective; the guard
            # band stays ungated, because 350 Hz-5 kHz is a magnitude promise
            # about the whole response, not about the first arrival.
            null = (self.early_db(chain, key) if self.mode == "early"
                    else energy_change_db(self.muted[key][self.score], predicted[self.score]))
            guard = energy_change_db(self.muted[key][self.guard], predicted[self.guard])
            total += max(0.0, FRONT_NULL_FLOOR_DB - null)
            total += max(0.0, abs(guard) - FRONT_GUARD_TOLERANCE_DB)
        return PENALTY_WEIGHT * total

    def objective(self, chain: Mapping[str, Any], keys=None) -> float:
        return float(np.mean(self.rear_db(chain, keys))) + self.penalty(chain)


def solve(model: Model, base: Mapping[str, Any], bounds, *, keys=None,
          inverted_options=(True,)) -> tuple[np.ndarray, bool, float]:
    """``(x, inverted, objective)``: differential evolution then Nelder-Mead.

    Polarity is a boolean, so it is not handed to a continuous optimiser --
    each option gets its own solve and the better one wins.
    """
    best = None
    for inverted in inverted_options:
        def cost(x: np.ndarray) -> float:
            return model.objective(cancellation_of(base, x, inverted=inverted), keys)

        # Budget, not taste: one objective evaluation costs ~3 ms (135 grid
        # bins x 6 positions), so a 12-parameter stage gets 180 x 40 = 7,200 of
        # them. ``latinhypercube`` rather than ``sobol`` because sobol rounds
        # the population up to a power of two and would treble that.
        found = differential_evolution(cost, bounds, seed=SEED, polish=True, tol=0.01,
                                       maxiter=40, popsize=15, init="latinhypercube")
        polished = minimize(cost, found.x, method="Nelder-Mead",
                            bounds=bounds, options={"xatol": 1e-4, "fatol": 1e-4})
        x, value = ((polished.x, polished.fun) if polished.fun < found.fun
                    else (found.x, found.fun))
        if best is None or value < best[2]:
            best = (np.asarray(x), inverted, float(value))
    return best


def prescription(section: Mapping[str, Any], chain: Mapping[str, Any], *,
                 stage: str, rationale: str) -> dict[str, Any]:
    """A complete ``jts_prescription`` document carrying this cancellation branch.

    ``common_delay_ms`` is raised here, and only here, so that every emitted
    branch delay (``common + front.delay + branch.delay``) lands at or above
    zero. It does not change what the document does -- see the module docstring.
    """
    out = deepcopy(dict(section))
    out["rear"] = {**out["rear"], "cancellation": deepcopy(dict(chain))}
    out["common_delay_ms"] = round(max(
        0.0, -float(out["rear"]["bass"]["delay_ms"]), -float(chain["delay_ms"])
    ) - float(out["front"]["delay_ms"]), 4)
    out["conditions"] = {
        "dataset": "jts3 round e4cd4da24c24 pair, UMIK-2 0.61 m front, "
                   "Dayton iMM-6C 0.61 m behind, arm 0/+-20",
        "fit_band_hz": [*NULL_BAND_HZ], "fit_tool": "null_search.py", "measured": True,
        "timing_reference": "front output of this stage",
    }
    out["assumptions"] = [
        "Searched directly for the least sound BEHIND: the objective is the measured "
        "energy ratio predicted/muted over 100-350 Hz, averaged over the three rear "
        "angles, not a fit to a rear/front ratio.",
        "The front chain and the bass branch are doc-N1's verbatim, so doc-N1 with "
        "rear_muted (fingerprint 0caaa048) stays the measured zero this was scored against.",
        "PREDICTION, NOT A TUNE: it comes from one pair round's superposition, whose own "
        "trust number (superposition_residual_db) is 3.0-5.8 dB at the rear microphone.",
        f"Search stage {stage}.",
    ]
    return {"kind": "jts_prescription", "schema": 1, "base": "saved",
            "rationale": rationale, "sections": {"rear_calibration": out}}


def report_stage(name: str, model: Model, positions, base, chain: Mapping[str, Any]) -> None:
    section = prescription(model.section, chain, stage=name, rationale="x")["sections"]["rear_calibration"]
    rear = model.rear_db(chain)
    print(f"  {name}: J {np.mean(rear):+.2f} dB  per rear angle "
          + " ".join(f"{value:+.2f}" for value in rear)
          + f"  penalty {model.penalty(chain):.2f}"
          f"  headroom charge {rear_branch_sum_headroom_db(section_of(prescription(model.section, chain, stage=name, rationale='x'))):.3f} dB")
    print(f"     cancellation: gain {chain['gain_db']:+.3f} dB  delay {chain['delay_ms']:+.3f} ms  "
          f"inverted {chain['inverted']}  filters "
          + " ".join(f"{item['parameters']['type']}@{item['parameters']['freq']:g}"
                     for item in chain["filters"]))
    for key in model.front_keys:
        predicted = model.predicted(chain, key)
        print(f"     {key}: null {energy_change_db(model.muted[key][model.score], predicted[model.score]):+.2f} dB "
              f"guard {energy_change_db(model.muted[key][model.guard], predicted[model.guard]):+.2f} dB")
    rear0 = next(key for key in model.rear_keys if positions[key]["pose"].startswith("az+0"))
    row = positions[rear0]
    muted, predicted = predicted_pair(section, row["freqs_hz"], row["H_front"], row["H_rear"])
    print("     rear 0 deg, per third octave 80-400 Hz: " + " ".join(
        f"{item['band_hz'][0]:.0f}:{item['change_db']:+.1f}"
        for item in third_octave_changes(row["freqs_hz"], muted, predicted))
        + f"   early {early_change_db(row['freqs_hz'], muted, predicted):+.2f} dB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True, help="doc-N1.json")
    parser.add_argument("--cands", type=Path, required=True)
    parser.add_argument("--objective", choices=("ungated", "early"), default="ungated")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    positions = load_positions(args.out_dir)
    section = section_of(json.loads(args.base.read_text()))
    model = Model(positions, section, objective=args.objective)
    base = section["rear"]["cancellation"]
    print(f"objective {args.objective.upper()} J = mean over the 3 rear angles of "
          f"10log10(mean |predicted|^2/|muted|^2) over {NULL_BAND_HZ[0]:g}-{NULL_BAND_HZ[1]:g} Hz"
          + (" -- EARLY variant: the same ratio taken on the 0-10 ms energy of the "
             "band-limited impulse" if args.objective == "early" else "")
          + "; lower is quieter behind")
    print(f"  incumbent doc-N1 cancellation: J {np.mean(model.rear_db(base)):+.2f} dB\n")

    print("=== stage A landscape: J + penalty, dB (rows = delay ms, columns = gain dB)")
    gains = np.arange(0.0, -13.0, -1.0)
    delays = np.arange(-1.5, 2.51, 0.25)
    print("      delay\\gain " + "".join(f"{gain:7.0f}" for gain in gains))
    grid_best = None
    for delay in delays:
        row = [model.objective(cancellation_of(base, np.array([gain, delay]), inverted=True))
               for gain in gains]
        print(f"      {delay:+6.2f}     " + "".join(f"{value:7.2f}" for value in row))
        for gain, value in zip(gains, row):
            if grid_best is None or value < grid_best[0]:
                grid_best = (value, gain, delay)
    print(f"      grid minimum {grid_best[0]:+.2f} dB at gain {grid_best[1]:+.0f} dB, "
          f"delay {grid_best[2]:+.2f} ms")

    print("\n=== stage results (full search)")
    stages = {}
    for name, bounds, options in (("A", BOUNDS_A, (True,)),
                                  ("B", BOUNDS_B, (True, False)),
                                  ("C", BOUNDS_C, (True, False))):
        x, inverted, value = solve(model, base, bounds, inverted_options=options)
        chain = cancellation_of(base, x, inverted=inverted)
        stages[name] = chain
        report_stage(name, model, positions, base, chain)

    print("\n=== held-out check: fit on 2 rear angles, score the third")
    print("  stage  held out            fitted J (2 angles)   held-out angle J")
    held: dict[str, list[float]] = {"B": [], "C": []}
    for name, bounds, options in (("B", BOUNDS_B, (True, False)), ("C", BOUNDS_C, (True, False))):
        for drop in model.rear_keys:
            keep = [key for key in model.rear_keys if key != drop]
            x, inverted, value = solve(model, base, bounds, keys=keep, inverted_options=options)
            chain = cancellation_of(base, x, inverted=inverted)
            out = model.rear_db(chain, [drop])[0]
            held[name].append(out)
            print(f"  {name}      {drop:24s} {np.mean(model.rear_db(chain, keep)):+8.2f} "
                  f"{out:+18.2f}")
    gain = float(np.mean(held["B"])) - float(np.mean(held["C"]))
    print(f"  mean held-out J: B {np.mean(held['B']):+.2f} dB, C {np.mean(held['C']):+.2f} dB "
          f"-- C buys {gain:+.2f} dB; {'prefer B (under 1 dB)' if gain < 1.0 else 'C earns its parameters'}")

    print("\n=== emitted candidates")
    args.cands.mkdir(parents=True, exist_ok=True)
    rationale = {
        "A": "solve-A: doc-N1 with the cancellation branch's gain and delay searched "
             "directly against the measured sound behind (round e4cd4da24c24, 3 rear angles).",
        "B": "solve-B: solve-A plus the cancellation branch's two band edges and its polarity.",
        "C": "solve-C: solve-B plus two Peaking and one Allpass on the cancellation branch.",
    }
    for name, chain in stages.items():
        document = prescription(model.section, chain, stage=name, rationale=rationale[name])
        path = args.cands / f"solve-{name}{args.tag}.json"
        path.write_text(json.dumps(document, indent=4) + "\n")
        try:
            section_of(document)
            verdict = "PASS"
        except Exception as exc:  # the product's reader is the judge
            verdict = f"FAIL {type(exc).__name__}: {exc}"
        print(f"  {path} -- product reader {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
