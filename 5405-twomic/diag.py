#!/usr/bin/env python3
"""Why 140-350 Hz does not null: is the rear wrong there, or is the room?

(b) Per band, three numbers decide it:
    |R c| / |X_0|   how much rear pressure the chain actually puts at the mic
    R agreement     whether the identified path is repeatable there
    N / |X_0|       how much of what is there is incoherent and uncancellable
(c) A GATED figure, 10 ms after the direct arrival, for 160-315 Hz: if the null
    is there gated but not ungated, the speaker works and the room hides it.
"""
from __future__ import annotations
import json, pickle, sys
from pathlib import Path
import numpy as np
import identlib as il
from ident import ERA
from rearpred import section_of

SP = Path(sys.argv[1])
BANDS = [(89.1, 112.2), (111.4, 140.3), (142.5, 179.6), (178.2, 224.5),
         (222.7, 280.6), (280.6, 353.6)]
LAB = {"0caaa048": "muted", "27a565a9": "e-0.35", "c1cb20c0": "ident-B",
       "2fc52a19": "ident-C", "5e9afae3": "N1", "f299b04c": "d-0.15"}
GATE_MS = 10.0
PRE_MS = 2.0
WOOFER_BAND = (100.0, 350.0)

held = pickle.load(open(SP / "ident-model2.pkl", "rb"))
models, chains, groups, aligned = (held[k] for k in ("models", "chains", "groups", "aligned"))
grid = il.freqs()


def angle_of(pose): return float(pose.split("az")[1].split("_")[0])


def band_db(values, band):
    inside = (grid >= band[0]) & (grid < band[1])
    return 10.0 * np.log10(np.mean(np.abs(values[inside]) ** 2) + 1e-30)


def change_db(candidate, muted, band):
    return band_db(candidate, band) - band_db(muted, band)


print("=== (a) MEASURED per-band change vs muted, side mic (behind), dB")
print("  round tune      angle " + "".join(f"{b[0]:>8.0f}" for b in BANDS) + "    mean")
for tag in ("c1", "i1b", "i2"):
    for (mic, pose), byc in sorted(groups[tag].items()):
        if mic != "side" or "0caaa048" not in byc:
            continue
        muted = byc["0caaa048"]["transfer"]
        for fp in sorted(byc, key=lambda f: LAB.get(f, f)):
            if fp == "0caaa048":
                continue
            fit = aligned[(tag, mic, pose, fp)]
            if not fit["usable"]:
                continue
            rows = [change_db(fit["aligned"], muted, b) for b in BANDS]
            print(f"  {tag:5s} {LAB.get(fp, fp):9s} {angle_of(pose):+5.0f} "
                  + "".join(f"{v:+8.1f}" for v in rows) + f"  {np.mean(rows):+7.1f}")

print("\n=== (b) why: rear pressure, R agreement, and the incoherent floor, per band")
print("  angle  quantity          " + "".join(f"{b[0]:>8.0f}" for b in BANDS))
for angle in (0.0, 20.0, -20.0):
    keys = [k for k in models if k[1] == "side" and angle_of(k[2]) == angle]
    if not keys:
        continue
    model = np.nan_to_num(il.robust_mean([models[k]["R"] for k in keys]))
    muted = models[keys[0]]["muted"]
    n1 = section_of(json.loads((SP / "docs/doc-N1.json").read_text()))
    from jasper.active_speaker.branch_chain import rear_stage_response
    drive = rear_stage_response(n1, grid)[0]
    ratio = [band_db(model * drive, b) - band_db(muted, b) for b in BANDS]
    # agreement: spread of the per-candidate R estimates
    spread = []
    for b in BANDS:
        inside = (grid >= b[0]) & (grid < b[1])
        cell = np.asarray([models[k]["R"][inside] for k in keys])
        with np.errstate(invalid="ignore"):
            level = 20.0 * np.log10(np.abs(cell))
            spread.append(float(np.nanmax(np.nanmean(level, axis=1))
                                - np.nanmin(np.nanmean(level, axis=1))))
    rows = []
    for tag, _mic, pose in keys:
        byc = groups[tag][("side", pose)]
        zero = byc["0caaa048"]["transfer"]
        for fp in byc:
            if fp == "0caaa048" or fp not in chains:
                continue
            fit = aligned[(tag, "side", pose, fp)]
            if fit["usable"]:
                rows.append(fit["aligned"] - zero - model * chains[fp])
    floor = il.residual_floor(rows, BANDS)
    noise = [10.0 * np.log10(max(floor[b], 1e-30)) - band_db(muted, b) for b in BANDS]
    print(f"  {angle:+5.0f}  |R.c_N1|/|X_0| dB " + "".join(f"{v:+8.1f}" for v in ratio))
    print(f"  {'':5s}  R spread dB       " + "".join(f"{v:8.1f}" for v in spread))
    print(f"  {'':5s}  N/|X_0| dB        " + "".join(f"{v:+8.1f}" for v in noise))

print("\n=== (c) GATED 10 ms after the direct arrival, 160-315 Hz, side mic")
print("  the marker is the muted take's own 100-350 Hz first arrival, shared by every candidate")
UPPER = [(142.5, 179.6), (178.2, 224.5), (222.7, 280.6), (280.6, 353.6)]


def gated(transfer, marker):
    impulse = np.fft.irfft(transfer, n=il.N_FFT)
    taper = np.zeros(impulse.size)
    start = marker - int(PRE_MS * 1e-3 * 48000)
    stop = marker + int(GATE_MS * 1e-3 * 48000)
    index = np.arange(start, stop) % impulse.size
    shape = np.ones(index.size)
    rise = int(1.0e-3 * 48000)
    shape[:rise] = 0.5 - 0.5 * np.cos(np.pi * np.arange(rise) / rise)
    fall = max(1, index.size // 3)
    shape[-fall:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(fall) / fall)
    taper[index] = shape
    return np.fft.rfft(impulse * taper, n=il.N_FFT)


print("  round tune      angle  t0 ms " + "".join(f"{b[0]:>8.0f}" for b in UPPER) + "   ungated mean")
for tag in ("c1", "i1b", "i2"):
    for (mic, pose), byc in sorted(groups[tag].items()):
        if mic != "side" or "0caaa048" not in byc:
            continue
        muted = byc["0caaa048"]["transfer"]
        mask = (grid >= WOOFER_BAND[0]) & (grid <= WOOFER_BAND[1])
        woofer = np.fft.irfft(np.where(mask, muted, 0.0), n=il.N_FFT)
        envelope = np.abs(woofer)
        top = float(np.max(envelope))
        loud = np.flatnonzero(envelope >= top * 10 ** (-6.0 / 20.0))
        marker = int(loud[0]) if loud.size else int(np.argmax(envelope))
        gm = gated(muted, marker)
        for fp in sorted(byc, key=lambda f: LAB.get(f, f)):
            if fp == "0caaa048":
                continue
            fit = aligned[(tag, mic, pose, fp)]
            if not fit["usable"]:
                continue
            gc = gated(fit["aligned"], marker)
            rows = [change_db(gc, gm, b) for b in UPPER]
            ung = np.mean([change_db(fit["aligned"], muted, b) for b in UPPER])
            print(f"  {tag:5s} {LAB.get(fp, fp):9s} {angle_of(pose):+5.0f} {marker / 48.0:6.2f} "
                  + "".join(f"{v:+8.1f}" for v in rows) + f"  {ung:+12.1f}")
