#!/usr/bin/env python3
"""Gated identification of the rear acoustic path, and gated scoring.

The ungated model ``X_i = X_0 + R c_i`` is exact but the ROOM is inside both
``X_0`` and ``R``. Cutting both impulse responses with the SAME 10 ms window
after the muted take's own direct arrival leaves the direct sound only.

Two ways to carry the model into the gated domain, both implemented:

  A  identify in the gated domain: ``R_g = (X_i,g - X_0,g)/c_i``, predict
     ``X_g = X_0,g + R_g c``. Cheap, but wrong in principle: windowing is a
     convolution in frequency, so ``W{R c} != W{R} c``.
  B  identify ungated, predict ungated, window the PREDICTION:
     ``X_g = W{X_0 + R c}``. Exact if the ungated identification is good.

``validate.py`` measures which one actually predicts held-out rounds.
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np

import identlib as il
from ident import groups
from rearpred import section_of

from jasper.active_speaker.branch_chain import rear_stage_response

SP = Path(__file__).resolve().parent
ROUNDS = {"d1": "round-3138af96e104", "d2": "round-815ecfe40241",
          "c1": "round-dea67cbd648d", "649a": "round-649a313770cb",
          "i1b": "round-d929a1c333a8", "i2": "round-231b37be3851",
          "l1": "round-684797482a88", "g1m": "round-d35a332210c5",
          "g2m": "round-2b1eb4e370e9", "h1": "round-cf19d738eb19",
          "h3": "round-d313ffefa0dd", "h4": "round-8ae2ac84b867",
          "d3": "round-310c37cd0445", "f1": "round-1bab045aa4e2",
          "f2": "round-4e4bc654a331", "a1": "round-e5f73ee228bc",
          "a2": "round-433113c88326"}
#: D3's side rear-muted take is corrupt, so nothing behind can be referenced to
#: it. Its MAIN takes are sound, and they are the only measured sweep of the
#: cancellation low-pass corner (250/350/400/500 Hz) on the no-Peaking shape --
#: exactly the knob the front guard turns on. Main only, behind excluded.
SIDE_EXCLUDE = {"d3"}
ALL_TAGS = tuple(ROUNDS)
MUTED = "0caaa048"
LAB = {"0caaa048": "Nm", "27a565a9": "e-0.35", "c1cb20c0": "ident-B", "2fc52a19": "ident-C",
       "5e9afae3": "N1", "f299b04c": "d-0.15", "4b0374d1": "T1", "3c0c60db": "T2",
       "37419158": "T3", "711b458f": "S1", "ed2e86a5": "S2", "616bbfd9": "lp400",
       "88961484": "lp350", "a454643e": "w3", "654e057b": "d-0.12",
       "178e3a15": "d-0.25", "db96b13f": "d-0.25w45",
       "5bdbee3a": "fb-1", "76af80b5": "fb-2", "60f62a46": "fb-3",
       "bf03e5b6": "agg-1", "266e58cb": "agg-2"}

BANDS = [(89.1, 112.2), (111.4, 140.3), (142.5, 179.6), (178.2, 224.5),
         (222.7, 280.6), (280.6, 353.6)]
CENTRES = (100, 125, 160, 200, 250, 315)
GATED_FROM = 2                     # 160 Hz up; below that the 10 ms gate is too short
GATE_MS, PRE_MS = 10.0, 2.0
WOOFER_BAND = (100.0, 350.0)
CAP_DB = -10.0
#: Wider than identlib's 80-400 Hz: the 10 ms gate smears ~100 Hz, so the
#: prediction at 315 Hz needs R up to ~450 Hz, and the front 71-90 Hz guard
#: needs it below 80. The drive floor still throws out bins the chain cannot
#: excite, so widening costs nothing where the chain is quiet.
IDENT_BAND = (55.0, 650.0)
DRIVE_FLOOR_DB = 20.0
GRID = il.freqs()
CALS = {"main_cal": SP / "calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt",
        "side_cal": SP / "dayton-CMM31555.txt"}


def angle_of(pose: str) -> float:
    return float(pose.split("az")[1].split("_")[0])


def band_db(values, band) -> float:
    inside = (GRID >= band[0]) & (GRID < band[1])
    return 10.0 * np.log10(np.mean(np.abs(values[inside]) ** 2) + 1e-30)


def marker_of(muted) -> int:
    mask = (GRID >= WOOFER_BAND[0]) & (GRID <= WOOFER_BAND[1])
    envelope = np.abs(np.fft.irfft(np.where(mask, muted, 0.0), n=il.N_FFT))
    top = float(np.max(envelope))
    loud = np.flatnonzero(envelope >= top * 10 ** (-6.0 / 20.0))
    return int(loud[0]) if loud.size else int(np.argmax(envelope))


def taper_of(marker: int) -> np.ndarray:
    """diag.py's 10 ms window, as a vector so it can be applied repeatedly."""
    taper = np.zeros(il.N_FFT)
    index = np.arange(marker - int(PRE_MS * 1e-3 * 48000),
                      marker + int(GATE_MS * 1e-3 * 48000)) % il.N_FFT
    shape = np.ones(index.size)
    rise = int(1.0e-3 * 48000)
    shape[:rise] = 0.5 - 0.5 * np.cos(np.pi * np.arange(rise) / rise)
    fall = max(1, index.size // 3)
    shape[-fall:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(fall) / fall)
    taper[index] = shape
    return taper


def gate(transfer, taper) -> np.ndarray:
    return np.fft.rfft(np.fft.irfft(transfer, n=il.N_FFT) * taper, n=il.N_FFT)


def estimate_r(numerator, drive) -> np.ndarray:
    inside = (GRID >= IDENT_BAND[0]) & (GRID <= IDENT_BAND[1])
    level = np.abs(drive)
    floor = float(np.max(level[inside])) * 10.0 ** (-DRIVE_FLOOR_DB / 20.0)
    out = np.full(numerator.shape, np.nan + 0j)
    usable = inside & (level > floor)
    out[usable] = numerator[usable] / drive[usable]
    return out


def cache(tags=ALL_TAGS) -> dict:
    path = SP / "ident-cache.pkl"
    held = pickle.loads(path.read_bytes()) if path.exists() else {}
    for tag in tags:
        if tag in held:
            continue
        print(f"  decoding {tag} ({ROUNDS[tag]}) ...", flush=True)
        held[tag] = il.take_transfers(SP / ROUNDS[tag], **CALS)
        with path.open("wb") as handle:
            pickle.dump(held, handle)
    return held


def chains() -> dict[str, np.ndarray]:
    documents = il.load_documents(SP / "search" / "fp-index.json", SP)
    return {fp: rear_stage_response(section_of(doc), GRID)[0]
            for fp, doc in documents.items()}


#: One take is ONE playback heard by two microphones, so a playback-level
#: drift is common to both and cancels by itself in a front-minus-behind
#: figure. The aligner estimates its level trim per mic from 1-4 kHz content,
#: where the side mic is shadowed by the cabinet -- so the two trims disagree
#: (measured: sd 0.50 dB, worst 2.43 dB, every worst case at arm +0), and that
#: disagreement lands directly in F/B gain. With this set, the side take is
#: rescaled by the MAIN mic's trim magnitude, keeping its own timing.
SHARE_MAIN_TRIM = True
#: ``identlib.align`` fits ONE complex trim over 1-4 kHz onto the rear-muted
#: take, which is only right if the cancellation branch is silent up there. It
#: is not: measured over every round, the trim it charges rises with the
#: branch's level -- S1 +2.52 dB (n=12), lp400/d-0.12 +2.15..2.20, S2 +3.97,
#: against ~0.00 for N1, ident-C and every other quiet-branch tune. It repeats
#: per TUNE across rounds and poses, so it is the tune's own high-frequency
#: cancellation being mistaken for a playback-level drift. Applied, it makes
#: every FRONT-change figure for those tunes read ~2.5 dB too kind. F/B gain
#: is immune (a common trim cancels in front-minus-behind), the front is not.
#: "delay" keeps the aligner's timing and drops the magnitude trim entirely --
#: but that also throws away the correction for real playback-level drift
#: (sd ~1.2 dB), and the front model's error rose from 0.1 to ~1.0 dB. "hf"
#: keeps the trim and fits it over TRIM_BAND instead, where a 2nd-order
#: low-pass at any corner in this search is 37 dB or more down and the woofer
#: itself has rolled off, so no tune's branch can bias it.
#: SETTLED: the trim is NOT branch leakage. Fitting it over 4-10 kHz instead,
#: where a 2nd-order low-pass at any corner here is 37 dB or more down and the
#: woofer itself has rolled off, leaves the charge unchanged -- which rules
#: leakage out. What it matches, over 35 tunes, is the product's own
#: `rear_branch_sum_headroom_db`: correlation +0.999, slope 0.974, mean
#: difference 0.05 dB. That is a deliberate BROADBAND attenuation of the whole
#: stage so the rear branch sum cannot clip, and removing it is exactly right
#: when judging a tune's RESPONSE. It is still a real loss of maximum SPL, so
#: the headroom charge is added back into the EQ-back cost, where it belongs.
TRIM_MODE = "identlib"
TRIM_BAND = (4000.0, 10000.0)


def collect(tags=ALL_TAGS) -> dict:
    """``{(tag, mic, pose): {muted, taper, muted_g, cands: {fp: aligned}}}``."""
    held = cache(tags)
    store, trims = {}, {}
    for tag in tags:
        fits = {}
        for (mic, pose), byc in groups(held[tag]).items():
            if MUTED not in byc or (mic == "side" and tag in SIDE_EXCLUDE):
                continue
            muted = byc[MUTED]["transfer"]
            taper = taper_of(marker_of(muted))
            for fp, row in byc.items():
                if fp == MUTED:
                    continue
                fit = il.align(row["transfer"], muted)
                if fit["residual_db"] > il.ALIGN_RESIDUAL_MAX_DB:
                    continue
                if TRIM_MODE != "identlib":
                    shifted = row["transfer"] * np.exp(
                        -2j * np.pi * GRID * fit["delay_ms"] * 1e-3)
                    trim = 1.0 + 0j
                    if TRIM_MODE == "hf":
                        inside = (GRID >= TRIM_BAND[0]) & (GRID <= TRIM_BAND[1])
                        trim = (np.vdot(shifted[inside], muted[inside])
                                / max(float(np.vdot(shifted[inside],
                                                    shifted[inside]).real), 1e-30))
                    fit = {**fit, "trim_db": 20.0 * np.log10(abs(trim)),
                           "aligned": shifted * trim}
                fits[(mic, pose, fp)] = fit
                if mic == "main":
                    trims[(tag, pose, fp)] = fit["trim_db"]
            store[(tag, mic, pose)] = {"muted": muted, "taper": taper,
                                       "muted_g": gate(muted, taper), "cands": {}}
        for (mic, pose, fp), fit in fits.items():
            scale = 1.0
            if SHARE_MAIN_TRIM and mic == "side" and (tag, pose, fp) in trims:
                scale = 10.0 ** ((trims[(tag, pose, fp)] - fit["trim_db"]) / 20.0)
            store[(tag, mic, pose)]["cands"][fp] = fit["aligned"] * scale
    return store


def r_estimates(store, chain_of, tags, mic, angle, *, gated: bool) -> list[np.ndarray]:
    out = []
    for (tag, row_mic, pose), row in store.items():
        if tag not in tags or row_mic != mic or angle_of(pose) != angle:
            continue
        reference = row["muted_g"] if gated else row["muted"]
        for fp, aligned in row["cands"].items():
            if fp not in chain_of:
                continue
            candidate = gate(aligned, row["taper"]) if gated else aligned
            out.append(estimate_r(candidate - reference, chain_of[fp]))
    return out


def pooled(estimates) -> np.ndarray:
    return np.nan_to_num(il.robust_mean(estimates)) if estimates else None


def predict_bands(row, chain, r_full=None, r_gated=None) -> list[float]:
    """The six band changes vs the muted take: 100/125 ungated, 160-315 gated.

    ``r_full`` gives method B (window the prediction); ``r_gated`` method A.
    """
    ungated = row["muted"] + (r_full * chain if r_full is not None else 0.0)
    out = [band_db(ungated, b) - band_db(row["muted"], b) for b in BANDS[:GATED_FROM]]
    if r_gated is not None:
        gated_pred = row["muted_g"] + r_gated * chain
    else:
        gated_pred = gate(ungated, row["taper"])
    out += [band_db(gated_pred, b) - band_db(row["muted_g"], b) for b in BANDS[GATED_FROM:]]
    return out


def measured_bands(row, fp) -> list[float]:
    aligned = row["cands"][fp]
    out = [band_db(aligned, b) - band_db(row["muted"], b) for b in BANDS[:GATED_FROM]]
    gated_take = gate(aligned, row["taper"])
    out += [band_db(gated_take, b) - band_db(row["muted_g"], b) for b in BANDS[GATED_FROM:]]
    return out


def capped_mean(values) -> float:
    return float(np.mean([max(v, CAP_DB) for v in values]))


def label(fp: str) -> str:
    return LAB.get(fp, fp)


def load_json(path) -> dict:
    return json.loads(Path(path).read_text())
