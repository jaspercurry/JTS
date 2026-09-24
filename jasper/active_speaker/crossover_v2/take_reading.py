# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""One take's impulse, read the way a person reads it in Room EQ Wizard: the
impulse itself, its timing by frequency, and its decay. See ADR-0355 and ADR-0357.

By default a take is read through the span its own analysis gated at; a
caller may name another.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.audio_measurement.decay import (
    ENVELOPE_MS, FIGURE_RANGES_DB, FILTER_ORDER, NOISE_MARGIN_DB, NOISE_TAIL_FRACTION, octave_decays,
)
from jasper.audio_measurement.deconv import DEFAULT_POST_ARRIVAL_MS
from jasper.audio_measurement.excess_phase import GD_SPAN_OCT
from jasper.audio_measurement.gating import FLOOR_MEASURED, PHASE_GATE_LEAD_MS, gate_impulse_response
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.impulse_reading import (
    ETC_SPAN_FRACTION, NOISE_BEFORE_ONSET_MS, ONSET_BELOW_PEAK_DB, energy_time_db, impulse_shape,
    log_grid_hz, magnitude_db, step_response, timing_by_frequency, trusted_band_hz,
)
from jasper.audio_measurement.series_stats import band_change_db, curve_difference, deviation_summary
from jasper.audio_measurement.spatial_combine import octave_bands_hz

from .measurement_context import capture_basis, compare_capture_basis
from .round_captures import PoseCapture, RoundCapturesRefused, capture_row, select_capture

#: A take read through a window whose trusted band is too narrow to say anything.
REFUSE_TAKE_BAND_TOO_NARROW = "take_band_too_narrow"
#: Two sides of a comparison that share no trusted band, or no sample rate.
REFUSE_COMPARE_NO_COMMON_BAND = "compare_no_common_band"
REFUSE_COMPARE_RATES_DIFFER = "compare_sample_rates_differ"
#: A preview document that carries no magnitude prediction to compare.
REFUSE_PREVIEW_UNREADABLE = "compare_preview_unreadable"


@dataclass(frozen=True)
class TakeRead:
    capture: PoseCapture
    role: str

    @property
    def arrival_ms(self) -> float | None:
        """The direct peak on the take's own recording clock; ``None`` for a
        take whose impulse was rebuilt without that clock."""
        pre = self.capture.preprocessing.get("pre_guard_samples")
        if pre is None:
            return None
        shift = float(self.capture.preprocessing.get("clock_shift_samples") or 0.0)
        return 1000.0 * (self.capture.peak_idx - float(pre) - shift) / self.capture.sample_rate

    def parameters(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "impulse_source": self.capture.preprocessing.get("impulse_source", "rebuilt"),
            "time_reference": "the take's recording schedule" if self.arrival_ms is not None else "the direct peak",
            "calibration_applied": False,
        }

    def window(self, window_ms: float | None = None) -> tuple[float, str]:
        """The window to read through, and whose it is: ``argument``, ``take``
        (the analysis's own gate), ``ungated`` (a take the analysis did not
        gate) or ``retained`` (an ungated take whose impulse ends sooner)."""
        if window_ms is not None:
            return float(window_ms), "argument"
        gate = self.capture.curve.get("gate_window_ms")
        if isinstance(gate, (int, float)) and gate > 0:
            return float(gate), "take"
        held_ms = 1000.0 * (self.capture.ir.size - 1 - self.capture.peak_idx) / self.capture.sample_rate
        return (DEFAULT_POST_ARRIVAL_MS, "ungated") if held_ms >= DEFAULT_POST_ARRIVAL_MS else (held_ms, "retained")


def read_take(round_dir: Path, *, take_id: str, role: str) -> TakeRead:
    return TakeRead(select_capture(Path(round_dir), capture_id=take_id, role=role), role)


def _numbers(values: np.ndarray, digits: int) -> list[float | None]:
    """JSON-safe: a non-finite value is ``None``, never ``NaN``."""
    return [round(float(value), digits) if np.isfinite(value) else None for value in values]


def _number(value: float | None, digits: int) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


def impulse_report(read: TakeRead, *, span_ms: tuple[float, float] = (5.0, 100.0)) -> dict[str, Any]:
    """The impulse around its arrival, its decay, and what its gate found."""
    capture, rate = read.capture, read.capture.sample_rate
    shape = impulse_shape(capture.ir, rate, peak_index=capture.peak_idx)
    _, fragment = gate_impulse_response(capture.ir, rate, direct_peak_idx=capture.peak_idx)
    reflection = fragment.get("window_ms") if fragment.get("floor_source") == FLOOR_MEASURED else None
    start = max(0, shape.onset_index - round(span_ms[0] * rate / 1000))
    end = min(capture.ir.size, capture.peak_idx + round(span_ms[1] * rate / 1000) + 1)
    segment = capture.ir[start:end]
    summary = {
        "arrival_ms": _number(read.arrival_ms, 3),
        "onset_before_peak_ms": round(1000 * (capture.peak_idx - shape.onset_index) / rate, 3),
        "polarity": "normal" if shape.polarity > 0 else "inverted",
        "peak_to_noise_db": _number(shape.peak_to_noise_db, 1),
        "reflection_free_ms": _number(reflection, 2),
        "etc_db": [{"ms": ms, "db": _number(db, 1)} for ms, db in shape.etc_db],
    }
    return {
        "parameters": {
            **read.parameters(), "span_ms": list(span_ms), "onset_below_peak_db": ONSET_BELOW_PEAK_DB,
            "noise_before_onset_ms": list(NOISE_BEFORE_ONSET_MS), "etc_span_fraction": ETC_SPAN_FRACTION,
        },
        "capture": capture_row(capture),
        "summary": summary,
        "sample_rate_hz": rate,
        "start_ms": round(1000 * (start - capture.peak_idx) / rate, 4),
        "impulse": _numbers(segment, 8),
        "etc_db": _numbers(energy_time_db(capture.ir, peak_index=capture.peak_idx)[start:end], 2),
        "step": _numbers(step_response(segment), 5),
    }


def decay_report(read: TakeRead, *, step_ms: float = 1.0) -> dict[str, Any]:
    """How the take's sound decays from its onset, octave by octave (ISO 3382-1)."""
    capture, rate = read.capture, read.capture.sample_rate
    onset = impulse_shape(capture.ir, rate, peak_index=capture.peak_idx).onset_index
    bands = octave_decays(capture.ir, rate, start_index=onset, band_hz=capture.radiated_band_hz)
    step = max(1, round(step_ms * rate / 1000))
    return {
        "parameters": {
            **read.parameters(), "band_filter": f"octave; Butterworth order {FILTER_ORDER} edges; time-reversed",
            "figure_ranges_db": {name: list(levels) for name, levels in FIGURE_RANGES_DB.items()},
            "noise_margin_db": NOISE_MARGIN_DB, "envelope_ms": ENVELOPE_MS,
            "noise_tail_fraction": NOISE_TAIL_FRACTION,
        },
        "capture": capture_row(capture),
        "summary": {
            "arrival_ms": _number(read.arrival_ms, 3),
            "kept_after_onset_ms": round(1000 * (capture.ir.size - onset) / rate, 1),
            "bands": [{"hz": band.centre_hz, "edt_s": _number(band.edt_s, 3), "t20_s": _number(band.t20_s, 3),
                       "t30_s": _number(band.t30_s, 3), "decay_range_db": _number(band.decay_range_db, 1),
                       "noise_crossing_ms": _number(band.noise_crossing_ms, 1)} for band in bands],
        },
        "schroeder_step_ms": step_ms,
        "schroeder_db": [{"hz": band.centre_hz, "db": _numbers(band.schroeder_db[::step], 2)} for band in bands],
    }


def group_delay_report(
    read: TakeRead, *, window_ms: float | None = None, points_per_octave: int = 24,
) -> dict[str, Any]:
    """Phase, group delay and excess group delay by frequency, through one window."""
    capture = read.capture
    window, window_source = read.window(window_ms)
    band = trusted_band_hz(window, capture.radiated_band_hz, capture.sample_rate)
    if band is None:
        raise RoundCapturesRefused(REFUSE_TAKE_BAND_TOO_NARROW, {
            "capture_id": capture.capture_id, "role": read.role, "window_ms": window,
            "radiated_band_hz": list(capture.radiated_band_hz),
        })
    timing = timing_by_frequency(
        capture.ir, capture.sample_rate, peak_index=capture.peak_idx, window_ms=window,
        lead_ms=PHASE_GATE_LEAD_MS, band_hz=band, points_per_octave=points_per_octave,
    )
    grid = timing.freqs_hz
    excess = timing.excess_group_delay_ms

    def band_mean(values: np.ndarray | None, lo: float, hi: float) -> float | None:
        if values is None:
            return None
        inside = values[(grid >= lo) & (grid < hi)]
        inside = inside[np.isfinite(inside)]
        return _number(float(np.mean(inside)), 3) if inside.size else None

    bands = [
        {"hz": center, "group_delay_ms": band_mean(timing.group_delay_ms, lo, hi),
         "excess_group_delay_ms": band_mean(excess, lo, hi),
         "phase_deg": _number(float(np.interp(center, grid, timing.phase_deg)), 1)}
        for center, lo, hi in octave_bands_hz(*timing.band_hz)
    ]
    return {
        "parameters": {
            **read.parameters(), "window_ms": window, "window_source": window_source,
            "lead_ms": PHASE_GATE_LEAD_MS, "band_hz": [round(edge, 1) for edge in timing.band_hz],
            "points_per_octave": points_per_octave, "slope_span_octave": round(2 * GD_SPAN_OCT, 4),
        },
        "capture": capture_row(capture),
        "summary": {"arrival_ms": _number(read.arrival_ms, 3), "bands": bands},
        "freqs_hz": _numbers(grid, 2),
        "magnitude_db": _numbers(timing.magnitude_db, 2),
        "phase_deg": _numbers(timing.phase_deg, 1),
        "group_delay_ms": _numbers(timing.group_delay_ms, 4),
        "excess_group_delay_ms": None if excess is None else _numbers(excess, 4),
    }


@dataclass(frozen=True)
class PreviewSide:
    """A driver/blend forecast (``jts_capture_prediction``) read as one side of a comparison."""

    freqs_hz: np.ndarray
    predicted_db: np.ndarray
    band_hz: tuple[float, float]
    window_ms: float
    lead_ms: float
    candidate_id: str | None
    basis_capture_id: str | None


def read_preview(document: Any) -> PreviewSide:
    """The forecast inside ``jasper-crossover-prescriber judge --preview`` output, or the bare record."""
    preview = document.get("preview", document) if isinstance(document, dict) else None
    try:
        if not isinstance(preview, dict) or preview.get("kind") != "jts_capture_prediction":
            raise ValueError("not a jts_capture_prediction document")
        prediction, summary = preview["prediction"], preview["summary"]
        return PreviewSide(
            freqs_hz=np.asarray(prediction["freqs_hz"], dtype=float),
            predicted_db=np.asarray(prediction["predicted_db"], dtype=float),
            band_hz=(float(prediction["sum_band_hz"][0]), float(prediction["sum_band_hz"][1])),
            window_ms=float(summary["window"]["window_ms"]), lead_ms=float(summary["window"]["lead_ms"]),
            candidate_id=summary.get("candidate_id"), basis_capture_id=(summary.get("basis") or {}).get("capture_id"),
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise RoundCapturesRefused(REFUSE_PREVIEW_UNREADABLE, {"detail": str(exc)}) from exc


def _difference_report(
    grid: np.ndarray, a_db: np.ndarray, b_db: np.ndarray, band: tuple[float, float], *, remove_level: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``b - a``, summarised for the answer, and the curves for the artifact."""
    difference = curve_difference(grid, b_db, grid, a_db, band_hz=band, remove_level=remove_level)
    if difference is None:
        raise RoundCapturesRefused(REFUSE_COMPARE_NO_COMMON_BAND, {"band_hz": list(band)})
    delta = difference.delta_db
    b_side, a_side = difference.curve_db - difference.level_offset_db, difference.against_db


    summary = {key: _number(value, 2) if isinstance(value, float) else value
               for key, value in deviation_summary(difference.freqs_hz, delta).items()}
    return {
        **summary, "level_offset_db": _number(difference.level_offset_db, 2),
        "bands": [{"hz": center, "b_minus_a_db": _number(band_change_db(difference.freqs_hz, b_side, a_side, (lo, hi)), 2)}
                  for center, lo, hi in octave_bands_hz(*band)],
    }, {
        "freqs_hz": _numbers(difference.freqs_hz, 2), "a_db": _numbers(a_side, 3),
        "b_db": _numbers(b_side, 3), "b_minus_a_db": _numbers(delta, 3),
    }


def compare_report(
    a: TakeRead, b: TakeRead, *, window_ms: float | None = None, smoothing_fraction: int = 6,
    points_per_octave: int = 48, remove_level: bool = False,
) -> dict[str, Any]:
    """How ``b`` differs from ``a``: magnitude through one window and one smoothing.

    Across recordings only magnitude compares: each take's clock starts at its
    own anchor. Two roles of one recording share a clock, so their relative
    arrival is reported too.
    """
    rate = a.capture.sample_rate
    if b.capture.sample_rate != rate:
        raise RoundCapturesRefused(REFUSE_COMPARE_RATES_DIFFER, {"a": rate, "b": b.capture.sample_rate})
    (a_window, a_source), (b_window, b_source) = a.window(window_ms), b.window(window_ms)
    window = min(a_window, b_window)
    a_band, b_band = (trusted_band_hz(window, side.capture.radiated_band_hz, rate) for side in (a, b))
    band = None if a_band is None or b_band is None else (max(a_band[0], b_band[0]), min(a_band[1], b_band[1]))
    if band is None or band[1] <= band[0] * 1.5:
        raise RoundCapturesRefused(REFUSE_COMPARE_NO_COMMON_BAND, {
            "window_ms": window, "a_band_hz": list(a.capture.radiated_band_hz),
            "b_band_hz": list(b.capture.radiated_band_hz),
        })
    grid = log_grid_hz(band, points_per_octave)
    a_db, b_db = (magnitude_db(side.capture.ir, rate, peak_index=side.capture.peak_idx, window_ms=window,
                               lead_ms=PHASE_GATE_LEAD_MS, grid_hz=grid, smoothing_fraction=smoothing_fraction or None)
                  for side in (a, b))
    summary, curves = _difference_report(grid, a_db, b_db, band, remove_level=remove_level)
    same_recording = a.capture.capture_id == b.capture.capture_id
    a_arrival, b_arrival = a.arrival_ms, b.arrival_ms
    return {
        "parameters": {
            "roles": [a.role, b.role], "window_ms": window,
            "window_source": "argument" if window_ms is not None else f"shorter take window ({a_source}, {b_source})",
            "lead_ms": PHASE_GATE_LEAD_MS, "smoothing_fraction": smoothing_fraction or None,
            "points_per_octave": points_per_octave, "band_hz": [round(edge, 1) for edge in band],
            "level_removed": remove_level, "calibration_applied": False,
        },
        "a": capture_row(a.capture), "b": capture_row(b.capture),
        "summary": {
            **summary, "same_recording": same_recording,
            "relative_arrival_ms": (_number(b_arrival - a_arrival, 3)
                                    if same_recording and a_arrival is not None and b_arrival is not None else None),
            "basis": compare_capture_basis(capture_basis(b.capture.record_document),
                                           capture_basis(a.capture.record_document)),
        },
        **curves,
    }


def compare_preview_report(
    preview: PreviewSide, b: TakeRead, *, smoothing_fraction: int = 6, points_per_octave: int = 48,
) -> dict[str, Any]:
    """How a measured take differs from a forecast, read through the forecast's own window.

    The forecast carries no absolute level, so the level always comes off.
    """
    rate = b.capture.sample_rate
    trusted = trusted_band_hz(preview.window_ms, b.capture.radiated_band_hz, rate)
    band = None if trusted is None else (max(trusted[0], preview.band_hz[0], float(preview.freqs_hz[0])),
                                         min(trusted[1], preview.band_hz[1], float(preview.freqs_hz[-1])))
    if band is None or band[1] <= band[0] * 1.5:
        raise RoundCapturesRefused(REFUSE_COMPARE_NO_COMMON_BAND, {
            "window_ms": preview.window_ms, "preview_band_hz": list(preview.band_hz),
            "b_band_hz": list(b.capture.radiated_band_hz),
        })
    grid = log_grid_hz(band, points_per_octave)
    predicted = (smooth_fractional_octave(preview.freqs_hz, preview.predicted_db, smoothing_fraction)
                 if smoothing_fraction else preview.predicted_db)
    a_db = np.interp(grid, preview.freqs_hz, predicted)
    b_db = magnitude_db(b.capture.ir, rate, peak_index=b.capture.peak_idx, window_ms=preview.window_ms,
                        lead_ms=preview.lead_ms, grid_hz=grid, smoothing_fraction=smoothing_fraction or None)
    summary, curves = _difference_report(grid, a_db, b_db, band, remove_level=True)
    return {
        "parameters": {
            "roles": ["predicted", b.role], "window_ms": preview.window_ms, "window_source": "preview",
            "lead_ms": preview.lead_ms, "smoothing_fraction": smoothing_fraction or None,
            "points_per_octave": points_per_octave, "band_hz": [round(edge, 1) for edge in band],
            "level_removed": True, "calibration_applied": False,
        },
        "a": {"preview_candidate_id": preview.candidate_id, "basis_capture_id": preview.basis_capture_id},
        "b": capture_row(b.capture),
        "summary": {**summary, "same_recording": False, "relative_arrival_ms": None, "basis": None},
        **curves,
    }
