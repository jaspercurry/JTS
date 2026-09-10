# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bass evidence from the shared, immutable measurement reader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from jasper.audio_measurement.distortion import read_segment_distortion, required_pre_guard_s
from jasper.audio_measurement.program import AMBIENT_SEGMENT_ID, KIND_SILENCE
from jasper.audio_measurement.program_analysis import analysis_diagnostic_summary
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.sweep_levels import sweep_band_levels

from .measurement_analysis import AnalyzedMeasurement, analyzed_measurements

BASS_BANDS_HZ = tuple(zip((20., 30., 40., 50., 63., 80., 100., 125., 160.),
                         (30., 40., 50., 63., 80., 100., 125., 160., 200.)))


def _finite(values: np.ndarray) -> list[float | None]:
    return [float(value) if np.isfinite(value) else None for value in values]


def _quiet(take: AnalyzedMeasurement) -> tuple[np.ndarray, list[int] | None]:
    segments = take.program.segments
    ambient = next((s for s in segments if s.segment_id == AMBIENT_SEGMENT_ID), None)
    if ambient is None:
        return np.array([]), None
    first = segments.index(ambient)
    while first and segments[first - 1].kind == KIND_SILENCE:
        first -= 1
    locations = {loc.segment_id: loc.scheduled_start for loc in take.analysis.locations}
    # Leave one second after the courtesy tone for its acoustic tail.
    start = locations[segments[first].segment_id] + (take.sample_rate if first else 0)
    stop = locations[ambient.segment_id] + ambient.n_samples
    if not 0 <= start < stop <= take.samples.size:
        return np.array([]), None
    return take.samples[start:stop], [start, stop]


def _qualified(freqs: np.ndarray, bands: list[dict[str, Any]]) -> np.ndarray:
    mask = np.zeros(freqs.shape, dtype=bool)
    for band in bands:
        lo, hi = band["band_hz"]
        mask |= (freqs >= lo) & (freqs < hi) & band["fundamental_qualified"]
    return mask


def bass_take(take: AnalyzedMeasurement) -> dict[str, Any]:
    program, analysis = take.program, take.analysis
    document = take.document()
    segment = program.segment("sweep_verify")
    anchor = next(loc.scheduled_start for loc in analysis.locations if loc.segment_id == segment.segment_id)
    diagnostics = analysis_diagnostic_summary(analysis)
    valid = not diagnostics.get("integrity_failed") and not analysis.glitch_detected
    reading = read_segment_distortion(
        program, take.samples, segment.segment_id, anchor, band_hz=(20, 200),
        calibration=take.calibration.curve if take.calibration else None,
        epsilon=analysis.drift.epsilon_ppm / 1e6 if analysis.drift else 0.0,
    )
    quiet, quiet_samples = _quiet(take)
    bands = sweep_band_levels(take.samples, quiet, take.sample_rate, reading.sweep, anchor, BASS_BANDS_HZ)
    for band in bands:
        snr = band["estimated_snr_db"]
        band["fundamental_qualified"] = valid and snr is not None and snr >= DRIVER.snr_warn_db
    harmonic_qualified = _qualified(reading.freqs_hz, bands)
    orders = {}
    for order in reading.orders:
        required = required_pre_guard_s(reading.sweep, (order,))
        timing_valid = reading.preceding_silence_s >= required
        mask = harmonic_qualified & ~reading.floor_limited(order) & timing_valid
        orders[str(order)] = {
            "freqs_hz": _finite(reading.freqs_hz),
            "relative_db": _finite(reading.relative_db[order]),
            "floor_relative_db": _finite(reading.floor_relative_db[order]),
            "qualified": mask.tolist(), "timing_valid": timing_valid,
            "clearance_s": reading.preceding_silence_s - required,
            "received_db_spl": None,
        }
    curve = next(curve for curve in document["curves"] if curve["role"] == "summed")
    frequencies = np.asarray(curve["freqs_hz"])
    bass = (frequencies >= 20) & (frequencies <= 200)
    frequencies = frequencies[bass]
    return {
        "record_path": take.record_path,
        "record": {key: value for key, value in take.record.items() if key not in {"curves", "program"}},
        "program_id": program.program_id,
        "sweep_band_hz": [segment.f1_hz, segment.f2_hz],
        "sweep_duration_s": segment.n_samples / take.sample_rate,
        "calibration": document["calibration"],
        "frequency_curve": curve,
        "diagnostics": diagnostics, "quiet_samples": quiet_samples, "bands": bands,
        "freqs_hz": _finite(frequencies),
        "fundamental_db": _finite(np.asarray(curve["magnitude_db"])[bass]),
        "fundamental_qualified": _qualified(frequencies, bands).tolist(), "harmonics": orders,
        "actual_dsp_drive": None,
    }


def bass_view(bundle_dir: Path, *, calibration_root: Path | None = None) -> dict[str, Any]:
    takes = [bass_take(take) for take in analyzed_measurements(bundle_dir, calibration_root=calibration_root)]
    if not takes:
        raise ValueError("measurement_captures_missing")
    return {
        "schema": "jts_bass_view/1", "bundle_dir": str(bundle_dir), "takes": takes,
        "units": {"fundamental_db": "deconvolution magnitude; compare compatible takes only",
                  "relative_db": "received harmonic minus fundamental at the excitation frequency"},
        "limits": [
            "Received whole-chain harmonics are not isolated driver distortion or an excursion limit.",
            "Quiet-window SNR uses raw power in equal-duration sweep dwells; short windows and changing room noise limit precision.",
            "Unavailable harmonic coverage is unknown. Only bins qualified in both takes support a comparison.",
            "Absolute SPL needs recorded microphone sensitivity and capture gain; a response calibration alone is insufficient.",
            "Requested boost and Main level do not establish actual DSP drive or isolate compressor action.",
        ],
    }
