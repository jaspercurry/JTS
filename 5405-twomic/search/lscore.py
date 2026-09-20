#!/usr/bin/env python3
"""Per-band scoring for one round, ungated and gated.

The 100-350 Hz mean is retired: the null only works at 85-125 Hz, so a single
mean lets one deep band pay for four flat ones. Here each third octave is read
on its own and the summary CAPS every band at -10 dB before averaging, so depth
beyond -10 in one band cannot buy the score.

Band edges, the 10 ms gate and the marker rule are taken verbatim from the
builder's ``diag.py``; the transfers, the fixed cross-correlation aligner and
its 1-4 kHz usability gate come from ``identlib``. Nothing is re-derived here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))        # identlib/ident live beside the rounds, not here

import identlib as il              # noqa: E402
from ident import groups, pose_angle  # noqa: E402
BANDS = [(89.1, 112.2), (111.4, 140.3), (142.5, 179.6), (178.2, 224.5),
         (222.7, 280.6), (280.6, 353.6)]
CENTRES = (100, 125, 160, 200, 250, 315)
GATED_FROM = 2                    # 160 Hz up: below this the 10 ms gate is too short
GATE_MS, PRE_MS = 10.0, 2.0
WOOFER_BAND = (100.0, 350.0)
CAP_DB = -10.0
MUTED = "0caaa048"
GRID = il.freqs()


def band_db(values, band):
    inside = (GRID >= band[0]) & (GRID < band[1])
    return 10.0 * np.log10(np.mean(np.abs(values[inside]) ** 2) + 1e-30)


def marker_of(muted):
    """The muted take's own 100-350 Hz first arrival, shared by every candidate."""
    mask = (GRID >= WOOFER_BAND[0]) & (GRID <= WOOFER_BAND[1])
    envelope = np.abs(np.fft.irfft(np.where(mask, muted, 0.0), n=il.N_FFT))
    top = float(np.max(envelope))
    loud = np.flatnonzero(envelope >= top * 10 ** (-6.0 / 20.0))
    return int(loud[0]) if loud.size else int(np.argmax(envelope))


def gated(transfer, marker):
    impulse = np.fft.irfft(transfer, n=il.N_FFT)
    taper = np.zeros(impulse.size)
    index = np.arange(marker - int(PRE_MS * 1e-3 * 48000),
                      marker + int(GATE_MS * 1e-3 * 48000)) % impulse.size
    shape = np.ones(index.size)
    rise = int(1.0e-3 * 48000)
    shape[:rise] = 0.5 - 0.5 * np.cos(np.pi * np.arange(rise) / rise)
    fall = max(1, index.size // 3)
    shape[-fall:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(fall) / fall)
    taper[index] = shape
    return np.fft.rfft(impulse * taper, n=il.N_FFT)


def summary(rows):
    """Mean of the six ungated band changes, each capped at CAP_DB first."""
    return float(np.mean([min(max(v, CAP_DB), 99.0) for v in rows]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-dir", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    labels = json.loads(args.labels.read_text())
    rows = il.take_transfers(
        args.round_dir,
        main_cal=SP / "calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt",
        side_cal=SP / "dayton-CMM31555.txt")
    grouped = groups(rows)
    out: dict = {}

    for mic, title in (("side", "BEHIND the box"), ("main", "IN FRONT")):
        for gate in (False, True):
            if mic == "main" and gate:
                continue
            kind = "GATED 10 ms" if gate else "UNGATED"
            centres = CENTRES[GATED_FROM:] if gate else CENTRES
            bands = BANDS[GATED_FROM:] if gate else BANDS
            print(f"\n=== {mic} ({title}) — {kind} change vs muted, dB")
            print(f"  {'angle':>6s} {'tune':<11s}" + "".join(f"{c:>8d}" for c in centres)
                  + ("" if gate else f"{'capped mean':>13s}"))
            for (row_mic, pose), byc in sorted(grouped.items(), key=lambda kv: -pose_angle(kv[0][1])):
                if row_mic != mic or MUTED not in byc:
                    continue
                muted = byc[MUTED]["transfer"]
                marker = marker_of(muted)
                reference = gated(muted, marker) if gate else muted
                for fingerprint, row in sorted(
                        byc.items(), key=lambda kv: labels.get(kv[0], kv[0])):
                    if fingerprint == MUTED:
                        continue
                    fit = il.align(row["transfer"], muted)
                    name = labels.get(fingerprint, fingerprint)
                    if fit["residual_db"] > il.ALIGN_RESIDUAL_MAX_DB:
                        print(f"  {pose_angle(pose):+6.0f} {name:<11s}  REFUSED by the aligner "
                              f"(1-4 kHz residual {fit['residual_db']:+.1f} dB)")
                        continue
                    curve = gated(fit["aligned"], marker) if gate else fit["aligned"]
                    values = [band_db(curve, b) - band_db(reference, b) for b in bands]
                    line = (f"  {pose_angle(pose):+6.0f} {name:<11s}"
                            + "".join(f"{v:>+8.1f}" for v in values))
                    if not gate:
                        line += f"{summary(values):>+13.2f}"
                        out.setdefault(name, {})[f"{mic}{pose_angle(pose):+.0f}"] = {
                            "bands": values, "capped_mean": summary(values)}
                    print(line)
    if args.out:
        args.out.write_text(json.dumps(out, indent=1) + "\n")
        print(f"\nwrote {args.out}")
    print(f"\n  capped mean = mean of the six ungated bands with each capped at {CAP_DB:g} dB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
