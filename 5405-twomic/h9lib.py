#!/usr/bin/env python3
"""WALL1C: identify the seat and front paths with the RIGHT zero.

The brief calls C0 "rear muted with N1's front chain". It is not: C0's front
chain is EMPTY (gain 0, no filters) while A0, N1c, ident-Cc and wall170-1c all
carry N1's front chain (gain -0.51 dB, Allpass 80, Peaking 190.14 -6.36). So

    X_i - X_C0  !=  R c_i

because the two differ by the front chain as well as by the rear branch. The
exact relation, with k the product's broadband headroom scaling:

    X_i   = k_i   [ H_front F_i   + H_rear c_i ]
    X_C0  = k_C0  [ H_front F_C0 ]

Matching each take to C0 over 1-4 kHz with a complex scalar t_i absorbs both
the headroom scaling and any playback drift; multiplying back by the KNOWN
scalar rho = F_i/F_C0 at those frequencies restores what the front chain
really does, and leaves

    R = ( X_i t_i rho - X_C0 F_i/F_C0 ) / c_i

on one common scale (k_C0 H_rear). Every figure downstream is normalised to
its own 500 Hz - 2 kHz level, so that common scale cancels.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import identlib as il
from ba_lib import POSE0, curve, load, smooth
from rearpred import section_of
from wall_numbers import THIRDS, trend_rms, wall_hole

from jasper.active_speaker.branch_chain import rear_stage_response

SP = Path(__file__).resolve().parent
ROUND = SP / "round-0d0abbb03574"
RESULT = SP / "search/BA/res-wall1c.json"
DOCS = {"C0": "search/BA/doc-C0.json", "A0": "search/BA/doc-A0.json",
        "N1c": "search/BA/doc-N1.json", "ident-Cc": "search/BA/doc-identC.json",
        "wall170-1c": "search/BA/doc-wall170.json"}
FP = {"C0": "62a97fbc", "A0": "1b2915e5", "N1c": "8ceac668", "ident-Cc": "8db6160f",
      "wall170-1c": "f4a56053"}
REAR_ON = ("A0", "N1c", "ident-Cc", "wall170-1c")
ZERO = "C0"
TRIM_BAND = (1000.0, 4000.0)
IDENT_BAND = (50.0, 780.0)
DRIVE_FLOOR_DB = 20.0
GRID = il.freqs()
CALS = {"main_cal": SP / "calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt",
        "side_cal": SP / "dayton-CMM31555.txt"}
MICS = ("side", "main")          # side = SEAT (2 m, in front); main = front arm 0.81 m


def sections() -> dict:
    return {name: section_of(json.loads((SP / path).read_text()))
            for name, path in DOCS.items()}


def chains(grid=GRID) -> tuple[dict, dict]:
    """(rear chain, front chain) per document, from the product's evaluator."""
    rear, front = {}, {}
    for name, section in sections().items():
        r, f = rear_stage_response(section, grid)
        rear[name], front[name] = r, f
    return rear, front


def take_rows() -> list:
    cache = SP / "wall1c-takes.pkl"
    if cache.exists():
        import pickle
        return pickle.loads(cache.read_bytes())
    rows = il.take_transfers(ROUND, **CALS)
    import pickle
    cache.write_bytes(pickle.dumps(rows))
    return rows


def averaged() -> tuple[dict, dict]:
    """``{(mic, name): complex transfer}`` with the repeats averaged, and refusals.

    Each take keeps the aligner's DELAY and is scaled by a complex trim fitted
    over 1-4 kHz onto the zero -- never identlib's own 1-4 kHz trim on top of
    that, which would be the same fit applied twice.
    """
    by_fp = {v: k for k, v in FP.items()}
    rows = [r for r in take_rows() if r["ok"] and r["candidate"] in by_fp]
    inside = (GRID >= TRIM_BAND[0]) & (GRID <= TRIM_BAND[1])
    anchors = {}
    for mic in MICS:
        zero = [r["transfer"] for r in rows
                if r["mic"] == mic and by_fp[r["candidate"]] == ZERO]
        anchors[mic] = zero[0]
    out, refused = {}, []
    for mic in MICS:
        for name in DOCS:
            kept = []
            for row in rows:
                if row["mic"] != mic or by_fp[row["candidate"]] != name:
                    continue
                fit = il.align(row["transfer"], anchors[mic])
                if fit["residual_db"] > il.ALIGN_RESIDUAL_MAX_DB:
                    refused.append((mic, name, row["take_id"][-9:], fit["residual_db"]))
                    continue
                shifted = row["transfer"] * np.exp(
                    -2j * np.pi * GRID * fit["delay_ms"] * 1e-3)
                trim = (np.vdot(shifted[inside], anchors[mic][inside])
                        / max(float(np.vdot(shifted[inside], shifted[inside]).real), 1e-30))
                kept.append(shifted * trim)
            if kept:
                out[(mic, name)] = np.mean(np.asarray(kept), axis=0)
            out[(mic, name, "n")] = len(kept)
    return out, refused


def rho_of(front, name) -> complex:
    """F_name / F_C0 averaged over the trim band -- the scalar the trim removed."""
    inside = (GRID >= TRIM_BAND[0]) & (GRID <= TRIM_BAND[1])
    return complex(np.mean(front[name][inside] / front[ZERO][inside]))


def corrected(takes, front, mic, name) -> np.ndarray:
    """One tune's measurement put back on the common ``k_C0`` scale."""
    return takes[(mic, name)] * rho_of(front, name)


def zero_for(takes, front, mic, name) -> np.ndarray:
    """What the zero would have measured wearing THIS tune's front chain."""
    return takes[(mic, ZERO)] * (front[name] / front[ZERO])


def estimate_r(takes, rear, front, mic, name) -> np.ndarray:
    drive = rear[name]
    inside = (GRID >= IDENT_BAND[0]) & (GRID <= IDENT_BAND[1])
    floor = float(np.max(np.abs(drive[inside]))) * 10.0 ** (-DRIVE_FLOOR_DB / 20.0)
    usable = inside & (np.abs(drive) > floor)
    out = np.full(GRID.shape, np.nan + 0j)
    numerator = corrected(takes, front, mic, name) - zero_for(takes, front, mic, name)
    out[usable] = numerator[usable] / drive[usable]
    return out


def pooled(estimates) -> np.ndarray:
    return np.nan_to_num(il.robust_mean(list(estimates)))


def predict(takes, front, model, mic, rear_chain, front_chain) -> np.ndarray:
    """A candidate's transfer at ``mic``, on the common scale."""
    return takes[(mic, ZERO)] * (front_chain / front[ZERO]) + model * rear_chain


# ---- mapping a fine-grid change onto the result's own 121-point log grid ----

def result_grid() -> np.ndarray:
    result = load(RESULT)
    freqs, _ = curve(result, "main", POSE0, FP[ZERO])
    return freqs


def edges_of(freqs) -> tuple[np.ndarray, np.ndarray]:
    log = np.log(freqs)
    mid = np.concatenate([[log[0] - (log[1] - log[0]) / 2],
                          (log[1:] + log[:-1]) / 2,
                          [log[-1] + (log[-1] - log[-2]) / 2]])
    return np.exp(mid[:-1]), np.exp(mid[1:])


def to_result_grid(change_db, freqs) -> np.ndarray:
    """Energy-mean of a fine-grid dB change inside each result-grid cell."""
    low, high = edges_of(freqs)
    power = 10.0 ** (np.asarray(change_db) / 10.0)
    out = np.empty(freqs.size)
    for i, (a, b) in enumerate(zip(low, high)):
        sel = (GRID >= a) & (GRID < b)
        out[i] = 10.0 * np.log10(np.mean(power[sel])) if sel.any() else np.nan
    bad = ~np.isfinite(out)
    if bad.any():
        out[bad] = np.interp(np.log(freqs[bad]), np.log(freqs[~bad]), out[~bad])
    return out


def measured_curve(mic, name) -> tuple[np.ndarray, np.ndarray]:
    return curve(load(RESULT), mic, POSE0, FP[name])


def figures(freqs, mags) -> dict:
    depth, at_hz = wall_hole(freqs, mags)
    return {"rms": trend_rms(freqs, mags), "hole_db": depth, "hole_hz": at_hz}


def thirds_of(freqs, mags) -> dict:
    third = smooth(freqs, mags, 1 / 3.0)
    return {c: float(np.interp(c, freqs, third)) for c in THIRDS}
