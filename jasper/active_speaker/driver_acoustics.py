# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mic-backed acoustic analysis for active-speaker driver checks.

Turns a phone-mic sweep capture into a per-driver verdict and the summed
capture's magnitude curve; does no audio I/O and holds no state. numpy/scipy
and the measurement kernel are imported lazily inside functions so the
socket-activated ``/sound/`` wizard stays light. Playback safety (level,
ramp, tweeter protection) stays owned by ``safe_playback`` /
``calibration_level`` / ``driver_protection``.
"""

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

# Same band and sample rate as room correction, so its deconvolution path is
# reused verbatim.
DEFAULT_F1_HZ = 20.0
DEFAULT_F2_HZ = 20000.0
DEFAULT_DURATION_S = 6.0
DEFAULT_SAMPLE_RATE = 48000
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

# Verdict thresholds are all differential, so the unknown absolute calibration
# of the deconvolved magnitude cancels out.
SILENT_PEAK_DBFS = DRIVER.silent_peak_dbfs  # at/below this the capture is silent
PRESENT_MIN_SEPARATION_DB = 0.0  # in-band must be at least as strong as out
OUT_OF_BAND_SEPARATION_DB = -3.0  # clearly more energy outside the band
DEFAULT_NULL_THRESHOLD_DB = DRIVER.null_threshold_db  # deep crossover null = "present"

# Confidence neighbourhood around each crossover Fc, geometrically centred:
# ``[Fc / OVERLAP_BAND_RATIO, Fc * OVERLAP_BAND_RATIO]``. Nothing is averaged
# across it — the level is read AT Fc; the band only has to hold enough bins,
# and is the window the SNR verdict spans.
OVERLAP_BAND_RATIO = 2.0 ** 0.5  # half-octave each side → one octave total
# Below this many FFT bins the reading is too sparsely resolved to interpolate
# through: marked unusable, and the trim math fails closed to the datasheet
# sensitivity trim.
OVERLAP_MIN_BINS = DRIVER.overlap_min_bins

DRIVER_ACOUSTIC_KIND = "jts_active_speaker_driver_acoustics"
SUMMED_ACOUSTIC_KIND = "jts_active_speaker_summed_acoustics"

VERDICT_PRESENT = "present"
VERDICT_OUT_OF_BAND = "out_of_band"
VERDICT_SILENT = "silent"
VERDICT_UNUSABLE_CAPTURE = "unusable_capture"
SUMMED_BLEND_OK = "blend_ok"
SUMMED_POLARITY_OR_DELAY_PROBLEM = "polarity_or_delay_problem"

DRIVER_VERDICTS = frozenset(
    {VERDICT_PRESENT, VERDICT_OUT_OF_BAND, VERDICT_SILENT, VERDICT_UNUSABLE_CAPTURE}
)
SUMMED_VERDICTS = frozenset(
    {SUMMED_BLEND_OK, SUMMED_POLARITY_OR_DELAY_PROBLEM, VERDICT_UNUSABLE_CAPTURE}
)

CAPTURE_GEOMETRIES = frozenset({"near_field", "reference_axis"})


class DriverAcousticsError(ValueError):
    """Raised for malformed inputs (bad channel index, unreadable sweep meta)."""


@dataclass(frozen=True)
class DriverSweep:
    """Describes a channel-targeted sweep WAV written to disk."""

    sample_rate: int
    channel_count: int
    target_channel: int
    sweep_meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "channel_count": self.channel_count,
            "target_channel": self.target_channel,
            "sweep_meta": self.sweep_meta,
        }


@dataclass(frozen=True)
class DriverAcousticResult:
    """Per-driver acoustic verdict computed from a phone-mic capture."""

    verdict: str  # present | out_of_band | silent | unusable_capture
    present: bool
    observed_mic_dbfs: float
    peak_dbfs: float
    in_band_db: float
    out_of_band_db: float
    band_separation_db: float
    passband_hz: tuple[float, float]
    mic_clipping: bool
    quality: dict[str, Any]
    # One ``{fc_hz, lo_hz, hi_hz, level_db, bins, usable}`` entry per crossover
    # Fc this driver participates in. ``usable`` is the gate the trim math fails
    # closed on; ``level_db`` can be finite on an unusable entry.
    overlap_levels: tuple[dict[str, Any], ...] = ()
    # True when a calibrated measurement mic's curve was applied to the magnitude.
    calibrated: bool = False
    # Magnitude-class SNR verdicts (audio_measurement.snr_policy); None when no
    # noise_band_report was supplied to analyze_driver_capture.
    snr: dict[str, Any] | None = None
    # The paired ambient transform that produced ``snr``.
    ambient: dict[str, Any] | None = None
    # IR-gating / low-frequency validity-floor block (audio_measurement.gating,
    # docs/active-crossover-information-design.md "Measurement validity").
    # ``None`` only when there was no IR to gate at all.
    gating: dict[str, Any] | None = None
    # Server-owned geometry derived from the verified placement policy.
    capture_geometry: str = "near_field"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": DRIVER_ACOUSTIC_KIND,
            "verdict": self.verdict,
            "present": self.present,
            "observed_mic_dbfs": self.observed_mic_dbfs,
            "peak_dbfs": self.peak_dbfs,
            "in_band_db": self.in_band_db,
            "out_of_band_db": self.out_of_band_db,
            "band_separation_db": self.band_separation_db,
            "passband_hz": list(self.passband_hz),
            "mic_clipping": self.mic_clipping,
            "quality": self.quality,
            "overlap_levels": [dict(entry) for entry in self.overlap_levels],
            "calibrated": self.calibrated,
            "snr": self.snr,
            "ambient": self.ambient,
            "gating": self.gating,
            "capture_geometry": self.capture_geometry,
        }


@dataclass(frozen=True)
class SummedAcousticResult:
    """Summed-crossover verdict: is there a cancellation null at the crossover?"""

    verdict: str  # blend_ok | polarity_or_delay_problem | unusable_capture
    null_depth_db: float
    crossover_fc_hz: float
    observed_mic_dbfs: float
    mic_clipping: bool
    quality: dict[str, Any]
    # A reverse-polarity capture (one driver inverted), for which a DEEP null is
    # the pass signal. ``null_depth_db`` is always the raw measured depth; the
    # verdict interprets it per ``expect_null``.
    expect_null: bool = False
    calibrated: bool = False
    # Alignment-class SNR verdicts over the overlap band [fc/2, fc*2]. None when
    # neither noise_band_report nor noise_floor_dbfs was supplied.
    snr: dict[str, Any] | None = None
    ambient: dict[str, Any] | None = None
    # True when null_depth_db was reduced from its raw measured value because
    # the overlap-band SNR could not prove a deeper null. The verdict above is
    # always decided from the UNCAPPED measured depth; only the reported
    # number is capped.
    null_depth_capped: bool = False
    # Whether Fc and its lower shoulder Fc/2 sit above the low-frequency validity
    # floor. True whenever gating was not applied or found no floor issue.
    above_validity_floor: bool | None = True
    near_validity_floor: bool = False
    # See DriverAcousticResult.gating.
    gating: dict[str, Any] | None = None
    capture_geometry: str = "near_field"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": SUMMED_ACOUSTIC_KIND,
            "verdict": self.verdict,
            "null_depth_db": self.null_depth_db,
            "crossover_fc_hz": self.crossover_fc_hz,
            "observed_mic_dbfs": self.observed_mic_dbfs,
            "mic_clipping": self.mic_clipping,
            "quality": self.quality,
            "expect_null": self.expect_null,
            "calibrated": self.calibrated,
            "snr": self.snr,
            "ambient": self.ambient,
            "null_depth_capped": self.null_depth_capped,
            "above_validity_floor": self.above_validity_floor,
            "near_validity_floor": self.near_validity_floor,
            "gating": self.gating,
            "capture_geometry": self.capture_geometry,
        }


def write_driver_sweep_wav(
    path: str | Path,
    *,
    target_channel: int,
    channel_count: int,
    f1_hz: float = DEFAULT_F1_HZ,
    f2_hz: float = DEFAULT_F2_HZ,
    duration_s: float = DEFAULT_DURATION_S,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    amplitude_dbfs: float = DEFAULT_AMPLITUDE_DBFS,
) -> DriverSweep:
    """Write a multichannel sweep WAV with the ESS on one channel, silence else.

    ``DriverSweep.sweep_meta`` carries the synchronization parameters the
    analysis side regenerates the reference sweep from; reloading the int16 WAV
    instead would add quantization error to the deconvolution reference.
    """
    if channel_count < 1:
        raise DriverAcousticsError(f"channel_count must be >= 1, got {channel_count}")
    if not 0 <= target_channel < channel_count:
        raise DriverAcousticsError(
            f"target_channel {target_channel} out of range for "
            f"{channel_count} channels"
        )

    import numpy as np
    from scipy.io import wavfile

    from jasper.audio_measurement import sweep as sweep_mod

    mono, meta = sweep_mod.synchronized_swept_sine(
        f1=f1_hz,
        f2=f2_hz,
        duration_approx_s=duration_s,
        sample_rate=sample_rate,
        amplitude_dbfs=amplitude_dbfs,
    )
    frame = np.zeros((meta.n_samples, channel_count), dtype=np.float32)
    frame[:, target_channel] = mono
    pcm = np.clip(frame, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    wavfile.write(str(path), meta.sample_rate, pcm16)
    return DriverSweep(
        sample_rate=meta.sample_rate,
        channel_count=channel_count,
        target_channel=target_channel,
        sweep_meta=meta.to_dict(),
    )


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

        # Locate across the full legal relay window at 16 kHz.  The largest
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
        # The relay path intentionally selects equal-length signal and quiet
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


def _capture_band_levels(captured_wav: str | Path) -> list[dict[str, Any]]:
    """Raw-domain per-band FFT levels of a captured WAV, for the SC-1 SNR gate.

    Raw dBFS, not the deconvolved magnitude: an SNR verdict subtracts a
    ``noise_band_report`` built the same way, band for band.

    #2010: the default Hann window biases a non-stationary sweep's band split by
    a capture-layout-dependent amount (>13 dB across a 0-20 s leading quiet,
    sign-changing per band). No production caller reaches this function — the
    raw-WAV ``POST /crossover/driver-capture`` route its one call site needs is
    retired and pinned at 404 — so the shipped SC-1 SNR gate does not carry that
    bias, and reviving such a route re-arms it. ``window="rectangular"`` tracks
    the dwell-time law to within 0.2 dB but adds a
    ``10*log10(sweep_len/capture_len)`` duty-cycle offset on a padded capture;
    characterise that against the noise side before switching.
    """
    import numpy as np

    from jasper.audio_measurement import deconv, snr_policy
    from jasper.audio_measurement import sweep as sweep_mod

    captured, sr = sweep_mod.read_wav_mono(captured_wav)
    captured = deconv.cap_capture_length(captured, sweep_len=0, sample_rate=sr)
    return snr_policy.band_levels_dbfs(
        captured.astype(np.float64), sr, snr_policy.CROSSOVER_SNR_BANDS_HZ
    )


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


def _band_mean_db(freqs, mag_db, lo_hz: float, hi_hz: float) -> float | None:
    import numpy as np

    mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not bool(np.any(mask)):
        return None
    return float(np.mean(mag_db[mask]))


def _overlap_band_levels(
    freqs,
    mag_db,
    overlap_fcs,
    *,
    capture_usable: bool,
    silent: bool,
    mic_clipping: bool,
    snr_bands: Sequence[Mapping[str, Any]] | None = None,
    validity_floor_hz: float | None = None,
    validity_floor_known: bool = True,
) -> tuple[dict[str, Any], ...]:
    """The deconvolved magnitude AT each crossover Fc, with its confidence band.

    A view of the one level fact, not a second definition of it (ruling S8 --
    see ADR-0228). ``level_db`` is a POINT: ``mag_db`` interpolated at ``fc``
    off the 1/24-octave-smoothed magnitude, never a mean
    over ``[lo_hz, hi_hz]`` — that span is the confidence neighbourhood which
    must hold ``OVERLAP_MIN_BINS`` bins, and the window the SNR verdict spans.

    Returns one entry per ``Fc`` (``{fc_hz, lo_hz, hi_hz, level_db, bins,
    usable, snr_verdict, above_validity_floor, near_validity_floor}``). An entry
    is ``usable`` only when the capture passed quality gating, was not silent,
    the mic did not clip, the band held enough bins, ``fc`` sits at/above the
    validity floor, and its SNR verdict is not ``"insufficient"``. ``usable`` is
    the only gate: ``level_db`` is NaN only when there was nothing to read at
    ``fc``, and is finite on a clipped or under-resolved entry, so a reader that
    skips the flag gets a number no measurement stands behind. A ``"reduced"``
    verdict does not force ``usable=False``.

    ``validity_floor_known=False`` (an ungateable reference-axis capture) makes
    every entry ``above_validity_floor=None`` and unusable. ``near_validity_floor``
    marks the advisory ``[floor, NEAR_FLOOR_RATIO * floor)`` band and does not
    affect ``usable``.
    """
    import numpy as np

    from jasper.audio_measurement import snr_policy
    from jasper.audio_measurement.gating import NEAR_FLOOR_RATIO

    floor = (
        validity_floor_hz
        if validity_floor_hz is not None and math.isfinite(validity_floor_hz)
        else None
    )

    entries: list[dict[str, Any]] = []
    for raw_fc in overlap_fcs:
        try:
            fc = float(raw_fc)
        except (TypeError, ValueError):
            continue
        if not (fc > 0) or not math.isfinite(fc):
            continue
        lo = max(fc / OVERLAP_BAND_RATIO, ANALYSIS_LO_HZ)
        hi = min(fc * OVERLAP_BAND_RATIO, ANALYSIS_HI_HZ)
        level_db = float("nan")
        bins = 0
        in_range = False
        if capture_usable and freqs is not None and lo < hi:
            mask = (freqs >= lo) & (freqs <= hi)
            bins = int(np.count_nonzero(mask))
            in_range = bool(freqs[0] <= fc <= freqs[-1])
            if in_range:
                level_db = float(np.interp(fc, freqs, mag_db))
        above = None if not validity_floor_known else floor is None or fc >= floor
        near = bool(
            validity_floor_known
            and floor is not None
            and floor <= fc < NEAR_FLOOR_RATIO * floor
        )
        usable = (
            capture_usable
            and not silent
            and not mic_clipping
            and in_range
            and bins >= OVERLAP_MIN_BINS
            and math.isfinite(level_db)
            and above is True
        )
        worst = snr_policy.worst_band_verdict(snr_bands, lo, hi) if snr_bands else None
        snr_verdict = worst["verdict"] if worst else "unknown"
        if snr_verdict == "insufficient":
            usable = False
        entries.append({
            "fc_hz": fc,
            "lo_hz": lo,
            "hi_hz": hi,
            "level_db": level_db,
            "bins": bins,
            "usable": usable,
            "snr_verdict": snr_verdict,
            "above_validity_floor": above,
            "near_validity_floor": near,
        })
    return tuple(entries)


def usable_overlap_level_db(
    overlap_levels: Sequence[Mapping[str, Any]],
    fc: float,
    *,
    tol_hz: float = 1.0,
) -> float | None:
    """The USABLE overlap-band level at ``fc`` in dB, or ``None`` (fail-closed).

    The one owner of that reading: a live :class:`DriverAcousticResult` and a
    persisted capture record must not disagree about whether a band is evidence.
    """
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


def analyze_driver_capture(
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    *,
    passband_hz: tuple[float, float],
    overlap_fcs: Sequence[float] = (),
    has_mic_calibration: bool = False,
    calibration: "CalibrationCurve | None" = None,
    noise_band_report: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    capture_geometry: str = "near_field",
    ambient_duration_s: float | None = None,
) -> DriverAcousticResult:
    """Classify whether a driver is producing sound in its expected band.

    ``overlap_fcs`` are the crossover frequencies this driver participates in
    (:func:`jasper.active_speaker.profile.crossover_edges_for_role`). Magnitude
    only — never used to authorise a phase or delay decision.

    ``noise_band_report`` accepts either a
    ``[{band_id, band_hz, level_dbfs}, ...]`` list or a domain-tagged
    ``{domain, method, bands}`` report; when supplied, ``snr`` is scoped to
    ``relevant_hz = passband_hz ∩ [ANALYSIS_LO_HZ, ANALYSIS_HI_HZ]``, else
    ``None``. When gating reports a low-frequency validity floor, every derived
    quantity is restricted to data at/above that floor, and a driver whose whole
    passband sits below it is ``unusable_capture``.
    """
    import numpy as np

    lo, hi = float(passband_hz[0]), float(passband_hz[1])
    if not (0 < lo < hi):
        raise DriverAcousticsError(f"invalid passband_hz: {passband_hz!r}")

    report, freqs, mag_db, gating_block, paired_ambient = _capture_to_magnitude(
        captured_wav, sweep_meta, has_mic_calibration=has_mic_calibration,
        calibration=calibration, capture_geometry=capture_geometry,
        ambient_duration_s=ambient_duration_s,
    )
    quality_dict = report.to_dict()
    mic_clipping = report.clipped_fraction >= 1e-4
    silent = report.peak_dbfs <= SILENT_PEAK_DBFS
    calibrated = calibration is not None

    if freqs is None:
        validity_known = capture_geometry == "near_field"
        return DriverAcousticResult(
            verdict=VERDICT_UNUSABLE_CAPTURE,
            present=False,
            observed_mic_dbfs=report.rms_dbfs,
            peak_dbfs=report.peak_dbfs,
            in_band_db=float("nan"),
            out_of_band_db=float("nan"),
            band_separation_db=float("nan"),
            passband_hz=(lo, hi),
            mic_clipping=mic_clipping,
            quality=quality_dict,
            overlap_levels=_overlap_band_levels(
                None, None, overlap_fcs,
                capture_usable=False, silent=silent, mic_clipping=mic_clipping,
                validity_floor_hz=None,
                validity_floor_known=validity_known,
            ),
            calibrated=calibrated,
            snr=None,
            gating=None,
            capture_geometry=capture_geometry,
        )

    validity_known, floor_hz = _validity_floor(capture_geometry, gating_block)
    eff_lo = max(ANALYSIS_LO_HZ, floor_hz) if floor_hz is not None else ANALYSIS_LO_HZ

    band_lo = max(lo, eff_lo)
    band_hi = min(hi, ANALYSIS_HI_HZ)

    if not validity_known or (floor_hz is not None and band_lo >= band_hi):
        # The validity floor sits at/above this driver's own passband ceiling,
        # so refuse rather than emit a magnitude from below-floor data.
        return DriverAcousticResult(
            verdict=VERDICT_UNUSABLE_CAPTURE,
            present=False,
            observed_mic_dbfs=report.rms_dbfs,
            peak_dbfs=report.peak_dbfs,
            in_band_db=float("nan"),
            out_of_band_db=float("nan"),
            band_separation_db=float("nan"),
            passband_hz=(lo, hi),
            mic_clipping=mic_clipping,
            quality=quality_dict,
            overlap_levels=_overlap_band_levels(
                freqs, mag_db, overlap_fcs,
                capture_usable=True, silent=silent, mic_clipping=mic_clipping,
                validity_floor_hz=floor_hz,
                validity_floor_known=validity_known,
            ),
            calibrated=calibrated,
            ambient=paired_ambient,
            gating=gating_block,
            capture_geometry=capture_geometry,
        )

    in_band = _band_mean_db(freqs, mag_db, band_lo, band_hi)

    # Out-of-band reference: trusted analysis window minus the passband. Its
    # lower edge is the validity floor when one applies (eff_lo), not the raw
    # ANALYSIS_LO_HZ.
    out_mask = ((freqs >= eff_lo) & (freqs <= ANALYSIS_HI_HZ)) & ~(
        (freqs >= band_lo) & (freqs <= band_hi)
    )
    out_of_band = (
        float(np.mean(mag_db[out_mask])) if bool(np.any(out_mask)) else None
    )

    if in_band is None:
        in_band = float("nan")
    if out_of_band is None:
        # Passband spans the whole trusted window (e.g. a full-range driver):
        # there is nothing to compare against, so separation is not meaningful.
        out_of_band = in_band
    separation = in_band - out_of_band

    if silent:
        verdict, present = VERDICT_SILENT, False
    elif separation < OUT_OF_BAND_SEPARATION_DB:
        verdict, present = VERDICT_OUT_OF_BAND, False
    elif separation >= PRESENT_MIN_SEPARATION_DB:
        verdict, present = VERDICT_PRESENT, True
    else:
        # Slightly negative separation but audible: weak/marginal, not clearly
        # wrong. Treat as present so a real-but-quiet driver isn't rejected.
        verdict, present = VERDICT_PRESENT, True

    snr_block = None
    snr_bands = None
    effective_noise_report = paired_ambient or noise_band_report
    if effective_noise_report:
        from jasper.audio_measurement import snr_policy

        noise_domain, noise_bands = snr_policy.unwrap_noise_report(
            effective_noise_report
        )
        if noise_domain == "deconvolved":
            capture_bands = snr_policy.magnitude_band_levels(freqs, mag_db)
            band_method = (
                str(effective_noise_report.get("method") or "")
                if isinstance(effective_noise_report, Mapping)
                else ""
            ) or "deconvolved_band_difference"
        else:
            capture_bands = _capture_band_levels(captured_wav)
            band_method = "fft_band_power_difference"
        snr_block = snr_policy.band_snr_verdicts(
            decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
            capture_bands=capture_bands,
            noise_bands=noise_bands,
            noise_floor_dbfs_scalar=None,
            relevant_hz=(band_lo, band_hi),
            model=DRIVER,
            band_method=band_method,
        )
        snr_bands = snr_block.get("bands")

    return DriverAcousticResult(
        verdict=verdict,
        present=present,
        observed_mic_dbfs=report.rms_dbfs,
        peak_dbfs=report.peak_dbfs,
        in_band_db=in_band,
        out_of_band_db=out_of_band,
        band_separation_db=separation,
        passband_hz=(lo, hi),
        mic_clipping=mic_clipping,
        quality=quality_dict,
        overlap_levels=_overlap_band_levels(
            freqs, mag_db, overlap_fcs,
            capture_usable=True, silent=silent, mic_clipping=mic_clipping,
            snr_bands=snr_bands,
            validity_floor_hz=floor_hz,
            validity_floor_known=validity_known,
        ),
        calibrated=calibrated,
        snr=snr_block,
        ambient=paired_ambient,
        gating=gating_block,
        capture_geometry=capture_geometry,
    )


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


def summed_capture_curve(
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    *,
    crossover_fc_hz: float,
    capture_geometry: str,
    has_mic_calibration: bool = False,
    calibration: "CalibrationCurve | None" = None,
    ambient_duration_s: float | None = None,
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
    if freqs is None or mag_db is None:
        return None
    validity_known, floor_hz = _validity_floor(capture_geometry, gating_block)
    if not validity_known:
        return None
    lower_shoulder_hz = crossover_fc_hz / 2.0
    near = False
    if floor_hz is not None:
        if crossover_fc_hz < floor_hz or lower_shoulder_hz < floor_hz:
            return None
        from jasper.audio_measurement.gating import NEAR_FLOOR_RATIO

        near = floor_hz <= lower_shoulder_hz < NEAR_FLOOR_RATIO * floor_hz
    return SummedCaptureCurve(
        freqs=freqs,
        magnitude_db=mag_db,
        gating=gating_block,
        above_validity_floor=True,
        near_validity_floor=near,
    )
