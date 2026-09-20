#!/usr/bin/env python3
"""Shared machinery for the wall-round early/late figures.

The rig on 09-20: the speaker 0.2 m from a wall, ``main`` = UMIK-2 on the front
arm at 0.81 m (pose 0 only), ``side`` = the Dayton FIXED AT THE LISTENING SEAT
about 2 m away and ~20 deg off axis -- in FRONT of the speaker. Both mics see
direct sound, so unlike the mid-room rounds there is a real arrival at each.

Every take is put on B0's clock by ``identlib.align`` using 1-4 kHz only, which
no rear branch and no sub-630 Hz front bell can move, and the direct-arrival
marker is read once from B0 and shared. The product's own locate anchor fails at
the seat (confidence 0.08-0.28) and is not used for anything here.

BOTH repeat takes are kept even when the aligner's -6 dB residual gate fails.
That gate exists to prove two takes are the same playback, and at 2 m in a live
room the 1-4 kHz band is dense comb filtering that moves between passes; the
wall2 second pass fails it on five of six tunes. It does not follow that the
takes are bad here: an early-to-late RATIO is taken inside one take, so the
aligner's complex trim divides straight out of it, and the aligner's delay is
never more than 0.07 ms against a 20 ms window. ``w2gate.py`` measures the
take-to-take ratio agreement of exactly those gate-failing takes. LEVEL figures
do depend on the trim, so they carry the gate flag with them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))
sys.path.insert(0, str(SP / "graphs"))

import identlib as il                                             # noqa: E402
from h4core import (FS, GRID, N, band_limited_impulse, envelope_db,  # noqa: E402
                    hilbert, marker_of, rolled_impulse, slice_ms, window_energy)

REFERENCE = "1f65d837"            # B0, the FAIR off: rear off, front matched to A0
#: label, colour, linestyle, linewidth -- the owner's chosen scheme
TUNES = {"1f65d837": ("B0 fair off (rear off, front matched)", "#5b7fa6", "-", 2.0),
         "62a97fbc": ("C0 rear simply off", "#888888", "--", 1.8),
         "1b2915e5": ("A0 agg-1", "#d62728", "-", 1.7),
         "1feb7466": ("seat-1 (applied)", "#2ca02c", "-", 3.0)}
EXTRA = {"6f60360c": "seat-2", "f4a56053": "wall170-1",
         "8ceac668": "wall1c extra A", "8db6160f": "wall1c extra B"}
ORDER = ("1f65d837", "62a97fbc", "1b2915e5", "1feb7466")
MICS = (("side", "AT THE LISTENING SEAT (Dayton, ~2 m, ~20° off axis)"),
        ("main", "FRONT ARM (UMIK-2, 0.81 m, pose 0)"))

BAND_100_350 = (100.0, 350.0)
OCTAVES = (("80-160 Hz", (80.0, 160.0)), ("160-315 Hz", (160.0, 315.0)),
           ("315-630 Hz", (315.0, 630.0)), ("100-350 Hz", BAND_100_350))
RING_MS = {name: 1000.0 / (band[1] - band[0]) for name, band in OCTAVES}
THIRDS = tuple((c, (c / 2 ** (1 / 6), c * 2 ** (1 / 6)))
               for c in (63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630))
SPLITS = (20.0, 30.0)             # the early/late boundary, and its robustness twin
LATE_END_MS = 250.0
#: The decayed tail, used as the floor. The impulse lives on a CIRCULAR 682.7 ms
#: grid, so times run -341..+341 ms about the arrival and nothing beyond +341 ms
#: exists as a positive time -- it has wrapped onto the negative side. 260..340 ms
#: is therefore the latest honest floor window, and it still clears the +250 ms
#: end of the late window.
TAIL_MS = (260.0, 340.0)


def slots(rows):
    """``{(round, mic, tune): [take rows]}`` -- one entry per REPEAT slot.

    A take with ``attempt == 1`` opens a slot; a higher attempt is a RETAKE of
    the slot already open, so it replaces it rather than adding a repeat. Each
    slot keeps its LAST accepted take, so a refused capture is never mixed with
    the retake that replaced it.
    """
    held: dict = {}
    for tag, take_rows in rows.items():
        for row in take_rows:
            key = (tag, row["mic"], row["candidate"])
            held.setdefault(key, [])
            if row["attempt"] == 1:
                held[key].append([])
            if held[key]:
                held[key][-1].append(row)
    out: dict = {}
    for key, runs in held.items():
        kept = [[r for r in run if r["ok"]][-1] for run in runs if any(r["ok"] for r in run)]
        if kept:
            out[key] = kept
    return out


def cells(rows):
    """``{(round, mic): {tune: {'takes': [aligned transfers], 'marker': int, 'fits': ...}}}``."""
    held = slots(rows)
    out: dict = {}
    for (tag, mic, tune), takes in held.items():
        out.setdefault((tag, mic), {})[tune] = takes
    final: dict = {}
    for key, byc in out.items():
        if REFERENCE not in byc:
            continue
        anchor = byc[REFERENCE][0]["transfer"]
        node = {"marker": marker_of(anchor), "tunes": {}, "fits": {}}
        for tune, takes in byc.items():
            aligned, fits = [], []
            for take in takes:
                fit = il.align(take["transfer"], anchor)
                fits.append({"take_id": take["take_id"], "delay_ms": fit["delay_ms"],
                             "trim_db": fit["trim_db"], "residual_db": fit["residual_db"],
                             "own_marker_ms": marker_of(take["transfer"]) / 48.0,
                             "gate_pass": bool(fit["residual_db"] <= il.ALIGN_RESIDUAL_MAX_DB)})
                aligned.append(fit["aligned"])
            node["fits"][tune] = fits
            node["tunes"][tune] = aligned
        final[key] = node
    return final


def band_energies(node, tune, band, split):
    """Per repeat take: ``(early, late)`` energy in ``band``, windows from ``split``."""
    out = []
    for transfer in node["tunes"][tune]:
        ir = rolled_impulse(transfer, band, node["marker"])
        out.append((window_energy(ir, 0.0, split), window_energy(ir, split, LATE_END_MS)))
    return out


def ratio_db(node, tune, band, split):
    """Early-to-late ratio in dB, the repeat takes POWER-averaged first."""
    pairs = band_energies(node, tune, band, split)
    early = float(np.mean([p[0] for p in pairs]))
    late = float(np.mean([p[1] for p in pairs]))
    return 10.0 * np.log10(max(early, 1e-30) / max(late, 1e-30))


def level_db(node, tune, band, split, kind):
    """Power-averaged early or late LEVEL of one tune, dB (arbitrary reference)."""
    pairs = band_energies(node, tune, band, split)
    index = 0 if kind == "early" else 1
    return 10.0 * np.log10(max(float(np.mean([p[index] for p in pairs])), 1e-30))


def level_change_db(node, tune, band, split, kind):
    """``tune`` minus B0, early or late level, dB -- paired WITHIN each pass.

    The two passes of wall2 drift about 1.7 dB against each other at 100-350 Hz
    after the 1-4 kHz trim (``w2gate.py``), and every tune drifts together, so
    comparing pass 1 with pass 1 and pass 2 with pass 2 removes the common part.
    A ratio does not need this; a level does.
    """
    index = 0 if kind == "early" else 1
    mine = [p[index] for p in band_energies(node, tune, band, split)]
    theirs = [p[index] for p in band_energies(node, REFERENCE, band, split)]
    pairs = min(len(mine), len(theirs))
    return float(np.mean([10.0 * np.log10(max(mine[i], 1e-30) / max(theirs[i], 1e-30))
                          for i in range(pairs)]))


def label(tune: str) -> str:
    return TUNES[tune][0] if tune in TUNES else EXTRA.get(tune, tune)
