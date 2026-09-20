#!/usr/bin/env python3
"""The shared prediction core: what a rear document would do at a measured pose.

ONE implementation of the forward model, so ``predict_null.py`` (score a
document) and ``null_search.py`` (search for one) cannot drift apart. The model
is exactly ``crossover_v2.rear_preview._position``:

    (rear, front) = branch_chain.rear_stage_response(section, grid)
    muted     = H_front * front
    predicted = muted + H_rear * rear

``H_front``/``H_rear`` are the pair take's RAW woofer transfers: a pair batch
plays the applied tune with its rear calibration CLEARED, so no chain is
divided out of them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.crossover_v2.pose_curve import nearest_native_bins
from jasper.active_speaker.rear_calibration import read_rear_calibration
from jasper.audio_measurement.analysis import THIRD_OCTAVE_BASS_BANDS_HZ
from jasper.audio_measurement.rear_evidence import EARLY_WINDOW_MS, band_limited_impulse

SAMPLE_RATE_HZ = 48000
#: Where the null is wanted, and where the forward response must survive.
NULL_BAND_HZ = (100.0, 350.0)
FRONT_GUARD_BAND_HZ = (350.0, 5000.0)
THIRD_OCTAVE_REPORT_HZ = (80.0, 400.0)
#: A LOG grid, so every octave carries the same weight; the pair grid is
#: linear at 1.46 Hz, and a plain in-band mean would weight 250-350 Hz most.
#: 48 points/octave is one native bin at 100 Hz, so nothing is skipped there.
SCORE_POINTS_PER_OCTAVE = 48
GUARD_POINTS_PER_OCTAVE = 12


def log_bins(freqs: np.ndarray, band_hz, points_per_octave: int) -> np.ndarray:
    """The native bins nearest a log grid across ``band_hz``."""
    lo, hi = band_hz
    want = np.geomspace(lo, hi, int(round(np.log2(hi / lo) * points_per_octave)) + 1)
    return np.unique(nearest_native_bins(np.asarray(freqs, dtype=float), want))


def load_positions(out_dir: Path) -> dict[str, dict[str, Any]]:
    """``{"<mic>/<pose>": {freqs_hz, H_front, H_rear, H_both, mic, pose}}``."""
    summary = json.loads((out_dir / "summary.json").read_text())
    out: dict[str, dict[str, Any]] = {}
    for mic, poses in summary["mics"].items():
        for pose, row in sorted(poses.items()):
            with np.load(row["npz"]) as data:
                out[f"{mic}/{pose}"] = {
                    "mic": mic, "pose": pose, "freqs_hz": data["freqs_hz"],
                    **{name: data[name] for name in ("H_front", "H_rear", "H_both")},
                }
    return out


def section_of(document: Mapping[str, Any]) -> dict[str, Any]:
    """The validated ``rear_calibration`` section of a prescription document.

    Read by the PRODUCT's own reader at the installed rate, the way
    ``rear_preview.preview_rear_section`` reads one. A bare
    ``jts_rear_calibration`` is accepted too, so a fitter document needs no
    wrapper before it can be scored.
    """
    section = document
    if document.get("kind") == "jts_prescription":
        section = (document.get("sections") or {})["rear_calibration"]
    return read_rear_calibration(section, sample_rate=SAMPLE_RATE_HZ)


def predicted_pair(section: Mapping[str, Any], freqs: np.ndarray,
                   front_tf: np.ndarray, rear_tf: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(muted, predicted)`` -- ``rear_preview._position``'s own two curves."""
    rear_chain, front_chain = rear_stage_response(section, freqs)
    muted = np.asarray(front_tf) * front_chain
    return muted, muted + np.asarray(rear_tf) * rear_chain


def energy_change_db(muted: np.ndarray, predicted: np.ndarray) -> float:
    """``10*log10(mean |predicted|^2 / |muted|^2)`` over the bins handed in."""
    return 10.0 * np.log10(float(np.mean(np.abs(predicted) ** 2 / np.abs(muted) ** 2)))


def early_change_db(freqs: np.ndarray, muted: np.ndarray, predicted: np.ndarray,
                    band_hz=NULL_BAND_HZ) -> float:
    """Change in EARLY energy of the band-limited impulse, dB.

    ``band_limited_impulse`` is the product's, and the window is the product's
    ``EARLY_WINDOW_MS`` (0-10 ms; its partner ``LATE_WINDOW_MS`` is 10-40 ms).
    ``impulse_energy_figures`` is NOT called: it returns early/late as a RATIO
    rather than early alone, and it anchors on each impulse's OWN peak. Both
    windows here are anchored on the MUTED impulse's peak, because the question
    is how much the direct arrival at a FIXED position changed -- following the
    predicted impulse's own peak would let a deep null re-centre onto a later
    arrival and read as no change.
    """
    impulses = [band_limited_impulse(freqs, tf, band_hz) for tf in (muted, predicted)]
    peak = int(np.argmax(np.abs(impulses[0])))
    time_ms = (np.arange(impulses[0].size) - peak) * (1000.0 / SAMPLE_RATE_HZ)
    window = (time_ms >= EARLY_WINDOW_MS[0]) & (time_ms < EARLY_WINDOW_MS[1])
    energy = [max(float(np.sum(one[window] ** 2)), 1e-30) for one in impulses]
    return 10.0 * np.log10(energy[1] / energy[0])


def third_octave_changes(freqs: np.ndarray, muted: np.ndarray,
                         predicted: np.ndarray) -> list[dict[str, Any]]:
    """Per third octave inside :data:`THIRD_OCTAVE_REPORT_HZ`, the same energy
    mean :func:`energy_change_db` takes over the null band."""
    rows = []
    for lo, hi in THIRD_OCTAVE_BASS_BANDS_HZ:
        if lo < THIRD_OCTAVE_REPORT_HZ[0] or hi > THIRD_OCTAVE_REPORT_HZ[1]:
            continue
        inside = np.flatnonzero((freqs >= lo) & (freqs < hi))
        if inside.size < 3:
            continue
        rows.append({"band_hz": [lo, hi],
                     "change_db": energy_change_db(muted[inside], predicted[inside])})
    return rows


def score_position(section: Mapping[str, Any], row: Mapping[str, Any], *,
                   early: bool = False) -> dict[str, Any]:
    """One position's ungated (and optionally early) change under ``section``."""
    freqs = row["freqs_hz"]
    muted, predicted = predicted_pair(section, freqs, row["H_front"], row["H_rear"])
    bins = log_bins(freqs, NULL_BAND_HZ, SCORE_POINTS_PER_OCTAVE)
    guard = log_bins(freqs, FRONT_GUARD_BAND_HZ, GUARD_POINTS_PER_OCTAVE)
    out = {
        "null_band_db": energy_change_db(muted[bins], predicted[bins]),
        "guard_band_db": energy_change_db(muted[guard], predicted[guard]),
    }
    if early:
        out["early_db"] = early_change_db(freqs, muted, predicted)
        out["third_octaves"] = third_octave_changes(freqs, muted, predicted)
    return out
