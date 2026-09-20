#!/usr/bin/env python3
"""Shared machinery for the H4 early/late figures.

One time base per (mic, pose): ``identlib.align`` puts every tune on the rear-
muted take's clock using 1-4 kHz only, which no rear candidate can move (the
rear chain is low-passed at 300 Hz). The direct-arrival marker is then read ONCE
from the muted take and shared by every tune of that cell, so a change in the
early/late split is a change in the sound, never a change in the window.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.signal import hilbert

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))
sys.path.insert(0, str(SP / "search"))

import identlib as il                                    # noqa: E402
from jasper.audio_measurement.rear_evidence import band_limited_impulse  # noqa: E402

FS = 48000
N = il.N_FFT
GRID = il.freqs()
MUTED = "0caaa048"
TUNES = {"0caaa048": ("rear muted (cardioid OFF)", "#777777", "--", 2.0),
         "5e9afae3": ("N1 (start of day)", "#1f77b4", "-", 1.6),
         "2fc52a19": ("ident-C (current best)", "#ff7f0e", "-", 2.4),
         "711b458f": ("S1", "#2ca02c", "-", 1.6),
         "654e057b": ("d-0.12", "#d62728", "-", 1.6)}
POSES = ("az+20.00_el+0.00_d+1.00", "az+0.00_el+0.00_d+1.00", "az-20.00_el+0.00_d+1.00")
BEHIND_LABEL = {20.0: "160 deg", 0.0: "180 deg", -20.0: "200 deg"}

MARKER_BAND_HZ = (1000.0, 4000.0)     # front: the direct arrival is the loudest point here
WOOFER_BAND_HZ = (100.0, 350.0)       # behind: no direct 1-4 kHz path, so read the woofer band
FIRST_ARRIVAL_FLOOR_DB = 6.0
BAND_100_350 = (100.0, 350.0)
OCTAVES = (("80-160 Hz", (80.0, 160.0)), ("160-315 Hz", (160.0, 315.0)),
           ("315-630 Hz", (315.0, 630.0)), ("100-350 Hz", BAND_100_350))
THIRDS = tuple((c, (c / 2 ** (1 / 6), c * 2 ** (1 / 6)))
               for c in (63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630))
#: A band-pass cannot place energy in time better than about 1/bandwidth.
RING_MS = {name: 1000.0 / (band[1] - band[0]) for name, band in OCTAVES}
EARLY_MS = (0.0, 20.0)
LATE_MS = (20.0, 200.0)
NOISE_MS = (-250.0, -50.0)            # circular pre-arrival region, same window for every tune
SMOOTH_MS = 3.0


def pose_angle(pose: str) -> float:
    return float(pose.split("az")[1].split("_")[0])


def cells(rows):
    """``{(mic, pose): {tune: transfer}}`` keeping the LAST accepted attempt.

    ``ok`` is the product's own verdict on the MAIN capture; the side cut
    inherits it, for the reason ``twomic_analyse`` documents.
    """
    out: dict = {}
    for row in rows:
        if not row["ok"]:
            continue
        out.setdefault((row["mic"], row["pose"]), {})[row["candidate"]] = row
    return out


def marker_of(muted: np.ndarray, rule: str = "direct") -> int:
    """The direct arrival of this cell, read from the rear-muted take only.

    ``direct`` is the peak of the 1-4 kHz Hilbert envelope. IN FRONT that is the
    direct arrival outright. BEHIND the cabinet it is used as well, and the
    measurement says it may be: the three behind poses read 5.15-5.29 ms against
    the front's 5.31 ms, so the diffracted 1-4 kHz arrival is still the earliest
    strong thing behind this (small) box.

    ``woofer_first`` is the folder's usual behind-the-box rule -- the first
    100-350 Hz envelope peak within 6 dB of the maximum. On THIS round it is
    unusable behind the cabinet: the woofer band there is room-dominated and
    never falls 6 dB below its own maximum anywhere in the 683 ms record, so the
    rule returns sample 0. It is kept only as a printed cross-check.
    """
    if rule == "direct":
        return int(np.argmax(np.abs(hilbert(
            band_limited_impulse(GRID, muted, MARKER_BAND_HZ)))))
    envelope = np.abs(hilbert(band_limited_impulse(GRID, muted, WOOFER_BAND_HZ)))
    floor = float(np.max(envelope)) * 10.0 ** (-FIRST_ARRIVAL_FLOOR_DB / 20.0)
    loud = np.flatnonzero(envelope >= floor)
    return int(loud[0]) if loud.size else int(np.argmax(envelope))


def aligned_transfers(rows):
    """``{(mic, pose): {'marker': int, 'tunes': {fp: aligned transfer}, 'fits': ...}}``."""
    out: dict = {}
    for (mic, pose), byc in cells(rows).items():
        if MUTED not in byc:
            continue
        muted = byc[MUTED]["transfer"]
        node = {"marker": marker_of(muted), "tunes": {MUTED: muted}, "fits": {}}
        for fingerprint, row in byc.items():
            if fingerprint == MUTED:
                continue
            fit = il.align(row["transfer"], muted)
            node["fits"][fingerprint] = fit
            if fit["residual_db"] <= il.ALIGN_RESIDUAL_MAX_DB:
                node["tunes"][fingerprint] = fit["aligned"]
        out[(mic, pose)] = node
    return out


def rolled_impulse(transfer: np.ndarray, band, marker: int) -> np.ndarray:
    """Zero-phase band-passed impulse, rolled so index 0 is the direct arrival.

    The response lives on a circular 682.7 ms grid, so a roll is exact and the
    samples now sitting at the array's end are the genuine PRE-arrival region.
    """
    return np.roll(band_limited_impulse(GRID, transfer, band), -marker)


def envelope_db(impulse: np.ndarray) -> np.ndarray:
    """Hilbert envelope ENERGY, smoothed over :data:`SMOOTH_MS`, in dB."""
    energy = np.abs(hilbert(impulse)) ** 2
    span = max(1, int(SMOOTH_MS * 1e-3 * FS))
    kernel = np.ones(span) / span
    return np.convolve(energy, kernel, mode="same")


def slice_ms(lo: float, hi: float) -> np.ndarray:
    """Index mask for a time window in ms about the direct arrival (index 0)."""
    time = (np.arange(N) + N // 2) % N - N // 2
    time = time * (1000.0 / FS)
    return (time >= lo) & (time < hi)


def window_energy(impulse: np.ndarray, lo: float, hi: float) -> float:
    return float(np.sum(impulse[slice_ms(lo, hi)] ** 2))


def noise_power(impulse: np.ndarray) -> float:
    """Mean power per sample in the pre-arrival region."""
    mask = slice_ms(*NOISE_MS)
    return float(np.mean(impulse[mask] ** 2))
