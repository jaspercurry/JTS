# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Summed capture curves and persisted overlap-level reads."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

# Pure-data threshold profiles only (no numpy/scipy), so top-level import is safe.
from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.quality_model import DRIVER

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve

DEFAULT_DURATION_S = 6.0
# Level tone and ESS share one source peak; acoustic level is then governed by
# the locked main volume and the applied per-role baseline gain.
DEFAULT_AMPLITUDE_DBFS = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS

# Trusted window for a phone-mic + speaker sweep: below ~40 Hz and above
# ~18 kHz, room modes, mic roll-off, and sweep fade dominate.
ANALYSIS_LO_HZ = 40.0
ANALYSIS_HI_HZ = 18000.0
DEFAULT_SMOOTHING_FRACTION = 24

# Lead before the located sweep arrival at which the equal-length quiet
# reference begins. Sets the analyzer's real minimum ambient requirement:
# ambient_duration_s >= kernel sweep duration + this lead, which
# test_signal_plan.AMBIENT_DURATION_MARGIN_S must stay above.
AMBIENT_CONTROLLED_LEAD_S = 1.0

DEFAULT_NULL_THRESHOLD_DB = DRIVER.null_threshold_db  # deep crossover null = "present"

VERDICT_UNUSABLE_CAPTURE = "unusable_capture"
CAPTURE_GEOMETRIES = frozenset({"near_field", "reference_axis"})


class DriverAcousticsError(ValueError):
    """Raised for malformed capture or sweep inputs."""


def _capture_to_magnitude(
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    *,
    has_mic_calibration: bool,
    calibration: "CalibrationCurve | None" = None,
    capture_geometry: str = "near_field",
    ambient_duration_s: float | None = None,
):
    """Shared capture → (quality, freqs, smoothed_magnitude_db, gating) pipeline.

    Returns ``(quality, None, None, None)`` when the capture fails quality
    gating: deconvolving a clipped / too short / wrong-rate capture would
    fabricate a curve.

    ``capture_geometry`` selects IR gating (see
    :mod:`jasper.audio_measurement.gating` and
    docs/active-crossover-information-design.md "Measurement validity").
    ``"near_field"`` is exempt and uses the ungated IR; ``"reference_axis"``
    gates the IR. The returned ``gating`` dict is populated (exempt or applied)
    whenever an IR exists at all.

    The paired-ambient report is measured on
    :data:`~jasper.audio_measurement.snr_policy.CROSSOVER_SNR_BANDS_HZ` — the
    canonical acoustic bands, correct for the WIDE per-driver near-field sweep
    this path was built for. The caller MUST measure its signal side on that
    same table: the two are subtracted per ``band_id``. A sweep too narrow to
    cover a canonical band needs a derived table instead.
    """
    if capture_geometry not in CAPTURE_GEOMETRIES:
        raise DriverAcousticsError(
            f"unsupported capture_geometry: {capture_geometry!r}"
        )

    import numpy as np

    from jasper.audio_measurement import (
        analysis,
        calibration as calibration_mod,
        deconv,
        gating,
        quality,
    )
    from jasper.audio_measurement import sweep as sweep_mod

    has_cal = has_mic_calibration or calibration is not None
    sample_rate = int(sweep_meta["sample_rate"])
    n_samples = int(sweep_meta["n_samples"])

    raw_captured, sr = sweep_mod.read_wav_mono(captured_wav)
    reference, _ = sweep_mod.synchronized_swept_sine(
        f1=float(sweep_meta["f1"]),
        f2=float(sweep_meta["f2"]),
        duration_approx_s=float(sweep_meta["duration_s"]),
        sample_rate=sample_rate,
        amplitude_dbfs=float(sweep_meta["amplitude_dbfs"]),
    )
    raw_capture_samples = len(raw_captured)
    truncated_from_samples = None
    capture_crop_start = 0
    ambient_source = None
    robust_ambient_source = None
    alignment = None
    if ambient_duration_s is not None:
        from scipy.signal import resample_poly
        from jasper.audio_measurement.alignment import assert_alignment_confident

        # Locate across the full legal capture window at 16 kHz.  The largest
        # correlation is <=2**20, then only the final <=2**21 full-rate crop is
        # deconvolved on the 1 GB Pi.
        from jasper.active_speaker.test_signal_plan import (
            CROSSOVER_CAPTURE_LOCATOR_WINDOW_S,
        )

        locator_input, locator_crop_start = deconv.cap_capture_tail(
            raw_captured,
            sweep_len=len(reference),
            sample_rate=sr,
            max_capture_seconds=CROSSOVER_CAPTURE_LOCATOR_WINDOW_S,
        )
        down = max(1, int(round(sr / 16000)))
        located_capture = resample_poly(locator_input, 1, down)
        located_reference = resample_poly(reference, 1, down)
        alignment = assert_alignment_confident(
            located_capture,
            located_reference,
            sample_rate=int(round(sr / down)),
            max_capture_s=60.0,
        )
        arrival_sample = locator_crop_start + int(round(alignment.lag_samples * down))
        pre_guard = int(round(0.250 * sr))
        tail = int(round(0.500 * sr))
        signal_start = arrival_sample - pre_guard
        signal_end = arrival_sample + len(reference) + tail
        ambient_start = arrival_sample - len(reference) - int(
            round(AMBIENT_CONTROLLED_LEAD_S * sr)
        )
        ambient_end = arrival_sample - pre_guard
        controlled_start = arrival_sample - int(round(float(ambient_duration_s) * sr))
        if (
            signal_start < 0
            or signal_end > len(raw_captured)
            or ambient_start < max(0, controlled_start)
            or ambient_end <= ambient_start
            or signal_end - signal_start != ambient_end - ambient_start
        ):
            raise ValueError(
                "signal-located crossover capture lacks the complete controlled "
                "ambient, sweep, or tail window"
            )
        captured = raw_captured[signal_start:signal_end]
        ambient_source = raw_captured[ambient_start:ambient_end]
        robust_ambient_source = raw_captured[controlled_start:ambient_end]
        capture_crop_start = signal_start
    else:
        captured = deconv.cap_capture_length(
            raw_captured,
            sweep_len=n_samples,
            sample_rate=sr,
        )
        if len(captured) < raw_capture_samples:
            truncated_from_samples = raw_capture_samples
    report = quality.assess_capture(
        captured,
        sample_rate=sr,
        expected_sample_rate=sample_rate,
        sweep_n_samples=n_samples,
        has_mic_calibration=has_cal,
        # The capture path intentionally selects equal-length signal and quiet
        # evidence from a longer recording; that is not the memory-bound
        # truncation this quality issue describes.  Only report a truncation
        # when cap_capture_length actually discarded a tail.
        truncated_from_samples=truncated_from_samples,
        quality_model=DRIVER,
    )
    if report.failed:
        return report, None, None, None, None

    full_signal_ir = deconv.regularized_deconvolution_full(
        captured.astype(np.float64),
        reference.astype(np.float64),
        sample_rate=sr,
    )
    arrival_peak_idx = int(np.argmax(np.abs(full_signal_ir)))
    arrival_window = deconv.direct_arrival_window(
        full_signal_ir, sr, direct_peak_idx=arrival_peak_idx
    )
    ir = deconv.apply_arrival_window(full_signal_ir, arrival_window)
    noise_ir = None
    if ambient_source is not None:
        full_noise_ir = deconv.regularized_deconvolution_full(
            ambient_source.astype(np.float64),
            reference.astype(np.float64),
            sample_rate=sr,
        )
        noise_ir = deconv.apply_arrival_window(full_noise_ir, arrival_window)
    if capture_geometry == "reference_axis":
        gated_ir, fragment = gating.gate_impulse_response(ir, sr)
        gated_noise_ir = (
            gating.apply_gate_fragment(noise_ir, sr, fragment)
            if noise_ir is not None
            else None
        )
        applied = fragment["floor_source"] is not None
        gating_block = {
            "schema_version": fragment["schema_version"],
            "applied": applied,
            "exempt_reason": None,
            **{k: v for k, v in fragment.items() if k != "schema_version"},
        }
        ir_used = gated_ir
        noise_ir_used = gated_noise_ir
    else:
        gating_block = gating.exempt_gating_block(ir, sr, reason="near_field")
        ir_used = ir
        noise_ir_used = noise_ir
    freqs, mag_db = deconv.magnitude_response(ir_used, sr, normalize=False)
    smoothed = analysis.smooth_fractional_octave(
        freqs, mag_db, DEFAULT_SMOOTHING_FRACTION
    )
    if calibration is not None:
        smoothed = calibration_mod.apply_calibration_curve(freqs, smoothed, calibration)
    ambient_report = None
    if noise_ir_used is not None and ambient_source is not None:
        if (
            robust_ambient_source is None
            or ambient_duration_s is None
            or alignment is None
        ):
            raise RuntimeError("controlled ambient analysis context is incomplete")
        noise_freqs, noise_mag = deconv.magnitude_response(
            noise_ir_used, sr, normalize=False
        )
        noise_smoothed = analysis.smooth_fractional_octave(
            noise_freqs, noise_mag, DEFAULT_SMOOTHING_FRACTION
        )
        if calibration is not None:
            noise_smoothed = calibration_mod.apply_calibration_curve(
                noise_freqs, noise_smoothed, calibration
            )
        from jasper.audio_measurement import snr_policy

        # One band table for every term below. The signal side (measured by the
        # caller) must use this same table: the two are subtracted per band_id.
        bands = snr_policy.CROSSOVER_SNR_BANDS_HZ
        noise_bands = snr_policy.magnitude_band_levels(
            noise_freqs, noise_smoothed, bands
        )
        robust = snr_policy.framed_ambient_band_report(
            robust_ambient_source,
            sr,
            bands,
            percentile=95,
        )
        baseline = snr_policy.framed_ambient_band_report(
            ambient_source,
            sr,
            bands,
            percentile=50,
        )
        # A band the reference sweep never excited (or barely reaches, at its
        # fade edges) is not safe to read from the deconvolved domain — see
        # snr_policy.excitation_covered_bands. apply_noise_band_fallback
        # substitutes the raw (non-deconvolved) robust ambient reading for
        # those bands instead, since it does not depend on the reference
        # spectrum at all and is grounded truth for what the room actually
        # did.
        covered = snr_policy.excitation_covered_bands(
            bands,
            f1_hz=float(sweep_meta["f1"]),
            f2_hz=float(sweep_meta["f2"]),
        )
        adjusted = snr_policy.apply_noise_band_fallback(
            noise_bands,
            robust_bands=robust["bands"],
            baseline_bands=baseline["bands"],
            covered=covered,
        )
        ambient_report = {
            "schema_version": 2,
            "domain": "deconvolved",
            "method": "paired_signal_window_deconvolution",
            "ambient_duration_s": round(float(ambient_duration_s), 3),
            "selected_quiet_duration_s": round(len(ambient_source) / sr, 3),
            "bands": adjusted,
            "raw_robust": robust,
            "raw_baseline": baseline,
            "source": {
                "kind": "signal_bounded_pre_sweep_quiet",
                "start_sample": ambient_start,
                "end_sample": ambient_end,
                "start_s": round(ambient_start / sr, 6),
                "end_s": round(ambient_end / sr, 6),
                "analysis_crop_start_sample": capture_crop_start,
                "located_sweep_start_sample": arrival_sample,
                "direct_arrival_sample": capture_crop_start + arrival_peak_idx,
                "pre_arrival_guard_ms": 250.0,
                "locator_sample_rate_hz": int(round(sr / down)),
                "locator_crop_start_sample": locator_crop_start,
                "locator_confidence": round(alignment.confidence, 6),
                "locator_peak": round(alignment.peak, 6),
            },
            "operator": {
                "deconvolution": "regularized_fft_inverse",
                "arrival_window_source": "signal",
                "ambient_alignment_source": "signal_direct_arrival_minus_guard",
                "robust_delta": "one_second_p95_minus_one_second_p50",
                "reflection_gate_source": (
                    "signal" if capture_geometry == "reference_axis" else None
                ),
                "calibration_applied_to_signal_and_noise": calibration is not None,
            },
        }
    return report, freqs, smoothed, gating_block, ambient_report


def _validity_floor(
    capture_geometry: str,
    gating_block: Mapping[str, Any] | None,
) -> tuple[bool, float | None]:
    """Return ``(known, floor_hz)`` for one analyzer-owned geometry.

    Near-field is explicitly exempt and therefore known with no floor. A
    reference-axis capture is known only when the IR gate produced a finite,
    positive floor. ``applied=False`` on that geometry means the IR was
    ungateable, not that the room suddenly became reflection-free.
    """

    if capture_geometry == "near_field":
        return True, None
    if (
        not isinstance(gating_block, Mapping)
        or gating_block.get("applied") is not True
    ):
        return False, None
    value = gating_block.get("f_valid_floor_hz")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        return False, None
    return True, float(value)


def usable_overlap_level_db(
    overlap_levels: Sequence[Mapping[str, Any]],
    fc: float,
    *,
    tol_hz: float = 1.0,
) -> float | None:
    """The usable persisted overlap-band level at ``fc`` in dB, or ``None``."""
    for entry in overlap_levels or ():
        if not isinstance(entry, Mapping) or not entry.get("usable"):
            continue
        raw_fc = entry.get("fc_hz")
        if isinstance(raw_fc, bool) or not isinstance(raw_fc, (int, float)):
            continue
        entry_fc = float(raw_fc)
        if not math.isfinite(entry_fc):
            continue
        if abs(entry_fc - fc) > max(tol_hz, fc * 0.01):
            continue
        level = entry.get("level_db")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            return None
        value = float(level)
        return value if math.isfinite(value) else None
    return None


@dataclass(frozen=True)
class SummedCaptureCurve:
    """One summed capture's calibrated magnitude, and whether it may be read.

    The capture half of a reverse-null measurement and only that half: the null
    depth is :func:`~jasper.audio_measurement.analysis.crossover_null_depth_db`,
    its shoulders :func:`~jasper.audio_measurement.analysis.shoulder_span`.
    """

    freqs: Any
    magnitude_db: Any
    gating: dict[str, Any] | None
    above_validity_floor: bool | None
    near_validity_floor: bool
    shoulders: Any = None


class SummedCaptureUnusable(DriverAcousticsError):
    def __init__(self, reason: str, diagnostics: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.diagnostics = {"reason": reason, **diagnostics}


def summed_capture_curve(
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    *,
    crossover_fc_hz: float,
    capture_geometry: str,
    has_mic_calibration: bool = False,
    calibration: "CalibrationCurve | None" = None,
    ambient_duration_s: float | None = None,
    raise_on_unusable: bool = False,
    overlap_hz: tuple[float, float] | None = None,
) -> SummedCaptureCurve | None:
    """A summed capture as a magnitude curve, or ``None`` when it cannot be read.

    ``None`` means the capture decides nothing: it either failed quality gating,
    or a ``reference_axis`` capture's validity floor sits above the lower
    shoulder ``crossover_fc_hz / 2``, so the room would supply the reference.

    A capture pipeline only — no verdict; grading the depth belongs elsewhere.
    ``capture_geometry`` is REQUIRED: the two geometries yield a different curve
    and a different floor, so no default is right.
    """
    if not (crossover_fc_hz > 0):
        raise DriverAcousticsError(
            f"crossover_fc_hz must be positive, got {crossover_fc_hz}"
        )
    report, freqs, mag_db, gating_block, _ambient = _capture_to_magnitude(
        captured_wav,
        sweep_meta,
        has_mic_calibration=has_mic_calibration,
        calibration=calibration,
        capture_geometry=capture_geometry,
        ambient_duration_s=ambient_duration_s,
    )
    lower_shoulder_hz = crossover_fc_hz / 2
    span = None
    if freqs is not None:
        from jasper.audio_measurement.analysis import shoulder_span

        band = overlap_hz or (float(freqs[0]), float(freqs[-1]))
        span = shoulder_span(freqs[(freqs >= band[0]) & (freqs <= band[1])],
                             crossover_fc_hz=crossover_fc_hz, overlap_hz=band)
        lower_shoulder_hz = span.used_hz[0]

    def unusable(reason: str) -> SummedCaptureCurve | None:
        if raise_on_unusable:
            raise SummedCaptureUnusable(reason, {
                "quality": report.to_dict(), "gating": gating_block,
                "required_lower_shoulder_hz": lower_shoulder_hz,
            })
        return None

    if freqs is None or mag_db is None:
        return unusable("capture_quality_failed")
    validity_known, floor_hz = _validity_floor(capture_geometry, gating_block)
    if not validity_known:
        return unusable("gate_floor_unknown")
    near = False
    if floor_hz is not None:
        if crossover_fc_hz < floor_hz or lower_shoulder_hz < floor_hz:
            return unusable("gate_excludes_lower_shoulder")
        from jasper.audio_measurement.gating import NEAR_FLOOR_RATIO

        near = floor_hz <= lower_shoulder_hz < NEAR_FLOOR_RATIO * floor_hz
    return SummedCaptureCurve(
        freqs=freqs,
        magnitude_db=mag_db,
        gating=gating_block,
        above_validity_floor=True,
        near_validity_floor=near,
        shoulders=span,
    )
