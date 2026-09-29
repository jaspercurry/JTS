# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bass evidence: the reading a summed take banks at capture, and the view over it (#5737 C4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from jasper.audio_measurement.band_ladders import BASS_BANDS_HZ as BASS_BANDS_HZ
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.deconv import HarmonicWindowOutOfRange
from jasper.audio_measurement.deconv import required_pre_guard_s
from jasper.audio_measurement.distortion import floor_limited_mask, read_segment_distortion
from jasper.audio_measurement.evidence_reasons import TAKE_BASS_NOT_BANKED, EvidenceUnavailable
from jasper.audio_measurement.program import ExcitationProgram, KIND_SUMMED_SWEEP, preceding_silence_s, segment_sweep_meta
from jasper.audio_measurement.program import AMBIENT_SEGMENT_ID, KIND_SILENCE
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.sweep_levels import sweep_band_levels
from jasper.audio_measurement.repeated_sweep import average_summed_capture, sweep_ambient_id

from .crossover_v2.record_index import measurement_documents, record_path
from .measurement_analysis import BankedMeasurement, analyzed_measurements

BASS_VIEW_SCHEMA = "jts_bass_view/2"
BASS_BAND_HZ = (BASS_BANDS_HZ[0][0], BASS_BANDS_HZ[-1][1])


def _finite(values: np.ndarray) -> list[float | None]:
    return [float(value) if np.isfinite(value) else None for value in values]


def _quiet(program: ExcitationProgram, locations: dict[str, int], size: int) -> list[int] | None:
    segments = program.segments
    pass_ambient = next((s for s in segments if s.segment_id == sweep_ambient_id("sweep_verify")), None)
    if pass_ambient is not None:
        start = locations[pass_ambient.segment_id]
        stop = start + pass_ambient.n_samples
        return [start, stop]
    ambient = next((s for s in segments if s.segment_id == AMBIENT_SEGMENT_ID), None)
    if ambient is None:
        return None
    first = segments.index(ambient)
    while first and segments[first - 1].kind == KIND_SILENCE:
        first -= 1
    # Leave one second after the courtesy tone for its acoustic tail.
    start = locations[segments[first].segment_id] + (program.sample_rate_hz if first else 0)
    stop = locations[ambient.segment_id] + ambient.n_samples
    if not 0 <= start < stop <= size:
        return None
    return [start, stop]


def band_snr(capture: np.ndarray, program: ExcitationProgram, segment_id: str, anchor: int,
             window: list[int] | None, ladder: tuple[tuple[float, float], ...]) -> dict[str, Any]:
    """``segment_id``'s received power in each band of ``ladder`` against the
    quiet ``window`` of ``capture``, as a take banks it. A reader that needs
    another ladder banks its own call beside the bass one."""
    quiet = capture[window[0]:window[1]] if window else capture[:0]
    bands = sweep_band_levels(capture, quiet, program.sample_rate_hz,
                              segment_sweep_meta(program.segment(segment_id)), anchor, ladder)
    return {"segment_id": segment_id, "quiet_samples": window, "bands": bands}


def bass_evidence(program: ExcitationProgram, analysis: Any, samples: np.ndarray,
                  calibration: CalibrationCurve | None) -> dict[str, Any] | None:
    """What the bass view reads of a summed sweep, from the averaged passes of
    the ``samples`` ``analysis`` read: band SNR on the bass ladder and the
    harmonic rows over the bass band. Any other program has none, as CHECK banks
    ``curves: []`` (ADR-0383 §1)."""
    if not any(s.segment_id == "sweep_verify" and s.kind == KIND_SUMMED_SWEEP for s in program.segments):
        return None
    locations = {loc.segment_id: loc.scheduled_start for loc in analysis.locations}
    alignment = analysis.capture_integrity.pass_alignment if analysis.capture_integrity else None
    capture = average_summed_capture(
        program, samples, analysis.locations[0].scheduled_start - program.segments[0].start_sample, alignment)
    anchor = locations["sweep_verify"]
    reading = band_snr(capture, program, "sweep_verify", anchor, _quiet(program, locations, capture.size), BASS_BANDS_HZ)
    try:
        harmonics = read_segment_distortion(
            program, capture, "sweep_verify", anchor, band_hz=BASS_BAND_HZ, calibration=calibration,
            epsilon=analysis.drift.epsilon_ppm / 1e6 if analysis.drift else 0.0,
        )
    except HarmonicWindowOutOfRange:
        return {**reading, "harmonics": {"available": False, "reason": "harmonic_window_out_of_range"}}
    return {**reading, "harmonics": {"available": True, "freqs_hz": harmonics.freqs_hz.tolist(), "orders": {
        str(order): {"relative_db": harmonics.relative_db[order].tolist(),
                     "floor_relative_db": harmonics.floor_relative_db[order].tolist()}
        for order in harmonics.orders}}}


def _qualified(freqs: np.ndarray, bands: list[dict[str, Any]]) -> np.ndarray:
    mask = np.zeros(freqs.shape, dtype=bool)
    for band in bands:
        lo, hi = band["band_hz"]
        mask |= (freqs >= lo) & (freqs < hi) & band["fundamental_qualified"]
    return mask


def bass_take(take: BankedMeasurement) -> dict[str, Any]:
    document = take.document()
    reading = (document.get("analysis") or {}).get("bass")
    if reading is None:
        raise EvidenceUnavailable(TAKE_BASS_NOT_BANKED, {"record": take.record_path, "field": "analysis.bass"})
    program = ExcitationProgram.from_dict(document["program"])
    segment = program.segment(reading["segment_id"])
    diagnostics = document["diagnostic"]
    alignment = diagnostics.get("pass_alignment")
    valid = not diagnostics.get("integrity_failed") and not document["analysis"]["glitch_detected"]
    bands = [dict(band) for band in reading["bands"]]
    for band in bands:
        snr = band["estimated_snr_db"]
        band["fundamental_qualified"] = valid and snr is not None and snr >= DRIVER.snr_warn_db
    orders = {}
    distortion = {key: value for key, value in reading["harmonics"].items() if key in {"available", "reason"}}
    if distortion["available"]:
        freqs = np.asarray(reading["harmonics"]["freqs_hz"], dtype=float)
        harmonic_qualified = _qualified(freqs, bands)
        silence = preceding_silence_s(program, segment)
        for order, row in reading["harmonics"]["orders"].items():
            relative, floor = (np.asarray(row[key], dtype=float) for key in ("relative_db", "floor_relative_db"))
            required = required_pre_guard_s(segment_sweep_meta(segment), (int(order),))
            timing_valid = silence >= required
            mask = harmonic_qualified & ~floor_limited_mask(relative, floor) & timing_valid
            orders[order] = {
                "freqs_hz": _finite(freqs),
                "relative_db": _finite(relative),
                "floor_relative_db": _finite(floor),
                "qualified": mask.tolist(), "timing_valid": timing_valid,
                "clearance_s": silence - required,
            }
    curve = next(curve for curve in document["curves"] if curve["role"] == "summed")
    frequencies = np.asarray(curve["freqs_hz"], dtype=float)
    bass = (frequencies >= BASS_BAND_HZ[0]) & (frequencies <= BASS_BAND_HZ[1])
    frequencies = frequencies[bass]
    segment_ids = {s.segment_id for s in program.segments}
    return {
        "record_path": take.record_path,
        "record": {key: value for key, value in take.record.items() if key not in {"analysis", "curves", "program"}},
        "stimulus_id": program.stimulus_id,
        "sweep_band_hz": [segment.f1_hz, segment.f2_hz],
        "sweep_duration_s": segment.n_samples / program.sample_rate_hz,
        "passes": [{"segment_id": s.segment_id, "start_sample": s.start_sample, "n_samples": s.n_samples,
                    "ambient_segment_id": sweep_ambient_id(s.segment_id),
                    **({"pass_alignment": alignment,
                        "offset_samples": diagnostics["pass_offsets_samples"][s.segment_id],
                        "correlation_peak": diagnostics["pass_correlation_peaks"][s.segment_id],
                        "correlation_peak_at_edge": s.segment_id in diagnostics["pass_correlation_edge_peaks"],
                        "residual_spread_samples": diagnostics["pass_alignment_residual_spread_samples"]} if alignment else {})}
                   for s in program.segments if sweep_ambient_id(s.segment_id) in segment_ids],
        "calibration": document["calibration"],
        "frequency_curve": curve,
        "diagnostics": diagnostics, "quiet_samples": reading["quiet_samples"], "ladder": "bass", "bands": bands,
        "freqs_hz": _finite(frequencies),
        "fundamental_db": _finite(np.asarray(curve["magnitude_db"], dtype=float)[bass]),
        "fundamental_qualified": _qualified(frequencies, bands).tolist(), "harmonics": orders,
        "distortion": distortion,
    }


def bass_view(bundle_dir: Path, *, take_ids: tuple[str, ...]) -> dict[str, Any]:
    paths = (record_path(row) for row, record in measurement_documents(bundle_dir) if record.get("take_id") in take_ids)
    takes = [bass_take(take) for take in analyzed_measurements(bundle_dir, paths=paths)]
    if not takes:
        raise ValueError("measurement_captures_missing")
    return {
        "schema": BASS_VIEW_SCHEMA, "bundle_dir": str(bundle_dir), "takes": takes,
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
