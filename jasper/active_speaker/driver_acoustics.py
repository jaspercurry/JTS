# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mic-backed acoustic analysis for active-speaker driver checks.

The driver-check step in the active-crossover flow has, until now, recorded
only operator confirmation plus a hand-passed ``observed_mic_dbfs`` number
([`measurement.py`](measurement.py) says it deliberately "does not ... infer
acoustic truth"). This module is the missing acoustic half: it turns a phone-
mic sweep capture into a real per-driver verdict (is this driver producing
sound, and in its expected band?) and a summed-crossover verdict (does the
crossover region sum without a cancellation null?).

It reuses the shared sweep / deconvolution / analysis primitives in
[`jasper.audio_measurement`](../audio_measurement/__init__.py) rather than
reinventing the DSP — the only thing that kernel can't do is target one physical
output, so ``write_driver_sweep_wav`` builds a channel-targeted multichannel WAV
(sweep on one channel, silence elsewhere). numpy/scipy and the kernel modules are
imported lazily inside functions so the socket-activated ``/sound/`` wizard
stays light until a measurement actually runs (mirrors
[`jasper/web/correction_setup.py`](../web/correction_setup.py)).

This module does no audio I/O and holds no state. The caller plays the WAV
through the active route under the existing safe-playback machinery, records
the phone mic with the shared browser recorder
([`measurement-audio.js`](../../deploy/assets/shared/js/measurement-audio.js)),
and hands the captured bytes here. The returned ``observed_mic_dbfs`` is what
[`measurement.record_driver_measurement`](measurement.py) already consumes;
the ``acoustic`` verdict block is new evidence for the same record.

Playback *safety* (level, ramp, tweeter protection) stays owned by
``safe_playback`` / ``calibration_level`` / ``driver_protection`` — this module
only chooses the digital sweep amplitude; the real SPL is governed by the
system volume and CamillaDSP at play time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

# quality_model holds only pure-data threshold profiles (no numpy/scipy), so it
# is safe to import at module top even though the rest of the measurement kernel
# stays lazily imported to keep the socket-activated /sound/ wizard light.
from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.quality_model import DRIVER

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve

# Default swept-sine parameters. Shorter than room correction's 10 s — a single
# driver needs far less SNR than a multi-position room average — but the same
# band and sample rate so the correction deconvolution path is reused verbatim.
DEFAULT_F1_HZ = 20.0
DEFAULT_F2_HZ = 20000.0
DEFAULT_DURATION_S = 6.0
DEFAULT_SAMPLE_RATE = 48000
# The level tone and ESS share one source peak. Acoustic level is then governed
# by the locked main volume and the applied per-role baseline gain.
DEFAULT_AMPLITUDE_DBFS = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS

# Frequency window we trust for a phone-mic + speaker sweep. Below ~40 Hz and
# above ~18 kHz, room modes, mic roll-off, and sweep fade dominate.
ANALYSIS_LO_HZ = 40.0
ANALYSIS_HI_HZ = 18000.0
DEFAULT_SMOOTHING_FRACTION = 24

# Verdict thresholds (all differential, so the unknown absolute calibration of
# the deconvolved magnitude cancels out). The driver-specific ones are aliased
# from the shared DRIVER QualityModel profile so the forked constant lives in
# data (jasper.audio_measurement.quality_model); values are unchanged.
SILENT_PEAK_DBFS = DRIVER.silent_peak_dbfs  # at/below this the capture is silent
PRESENT_MIN_SEPARATION_DB = 0.0  # in-band must be at least as strong as out
OUT_OF_BAND_SEPARATION_DB = -3.0  # clearly more energy outside the band
DEFAULT_NULL_THRESHOLD_DB = DRIVER.null_threshold_db  # deep crossover null = "present"

# Surfaced frequency-response curve (per-driver + summed), so the maintainer can
# eyeball Fc/slope by hand. Downsampled log-spaced so the JSON stays small. This
# module never auto-rewrites Fc/slope — it only surfaces the evidence; the
# polarity proposal lives in crossover_alignment.py.
FR_CURVE_MAX_POINTS = 72

# Overlap-band level (L1 phone level matching). For a per-driver near-field
# capture taken THROUGH the production crossover, the level each driver produces
# in a band centred on a shared crossover Fc is the physically-correct quantity
# to level-match: both adjacent drivers are rolling off symmetrically there
# (Linkwitz-Riley), so the matched −6 dB shoulder bias cancels in the
# driver-to-driver delta, leaving the relative driver sensitivity at Fc — exactly
# what makes the acoustic sum flat across the handoff. We average the deconvolved
# magnitude over one octave centred (geometrically) on Fc:
# ``[Fc / OVERLAP_BAND_RATIO, Fc * OVERLAP_BAND_RATIO]``.
OVERLAP_BAND_RATIO = 2.0 ** 0.5  # half-octave each side → one octave total
# A band needs at least this many FFT bins for a stable mean. Below this (a very
# low Fc on a short sweep) the overlap reading is marked unusable and the trim
# math fails closed to the datasheet sensitivity trim. Aliased from the shared
# DRIVER profile (value unchanged).
OVERLAP_MIN_BINS = DRIVER.overlap_min_bins

DRIVER_ACOUSTIC_KIND = "jts_active_speaker_driver_acoustics"
SUMMED_ACOUSTIC_KIND = "jts_active_speaker_summed_acoustics"

# The verdict vocabulary, named so the analyzers below and the frozensets are
# ONE source: a renamed or added verdict touches a single constant rather than
# drifting between the analyzer's literal and the declared set. Exported so
# callers that MAP verdicts (commissioning_capture's verdict->outcome maps) can
# guard-test that they cover the full set: a verdict missing from a map fails a
# test loudly instead of silently skipping a capture (`.get()` -> None -> not
# recorded).
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
    # Per-crossover overlap-band levels for L1 phone level matching. One entry
    # per crossover Fc this driver participates in, each
    # ``{fc_hz, lo_hz, hi_hz, level_db, bins, usable}``. ``usable`` is False when
    # the capture was silent/clipped/unusable or the band had too few bins, so
    # the trim math (jasper.active_speaker.baseline_profile) can fail closed.
    overlap_levels: tuple[dict[str, Any], ...] = ()
    # L2 calibrated-mic evidence. ``calibrated`` is True when a real measurement
    # mic's calibration curve was applied to the magnitude. ``fr_curve`` is the
    # downsampled (calibrated) magnitude response surfaced for the maintainer.
    fr_curve: dict[str, Any] | None = None
    calibrated: bool = False
    # SC-1 band-specific SNR verdict block (magnitude decision class), from
    # jasper.audio_measurement.snr_policy.band_snr_verdicts. None when no
    # noise_band_report was supplied to analyze_driver_capture — there is no
    # noise evidence to gate on, so there is nothing to report.
    snr: dict[str, Any] | None = None
    # The paired ambient transform that produced ``snr``. This preserves the
    # same deconvolved per-band noise evidence and signal-owned window/gate
    # provenance for later consumers (including the LF splice lane) instead
    # of forcing them back onto a scalar floor or a duplicate schema.
    ambient: dict[str, Any] | None = None
    # SC-2 IR-gating / low-frequency validity-floor block (see
    # jasper.audio_measurement.gating and docs/active-crossover-information-design.md
    # "Measurement validity"). ``None`` only when there was no IR to gate at all
    # (the capture failed quality gating before deconvolution); otherwise always
    # populated, exempt (near-field) or applied (reference-axis).
    gating: dict[str, Any] | None = None

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
            "fr_curve": self.fr_curve,
            "calibrated": self.calibrated,
            "snr": self.snr,
            "ambient": self.ambient,
            "gating": self.gating,
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
    # L2 phase-aware evidence. ``expect_null`` records whether this was a
    # reverse-polarity capture (one driver inverted) — for which a DEEP null is
    # the pass signal — versus a normal in-phase capture, where a deep null is
    # the polarity/delay problem. ``null_depth_db`` is always the raw measured
    # depth; the verdict interprets it per ``expect_null``.
    expect_null: bool = False
    fr_curve: dict[str, Any] | None = None
    calibrated: bool = False
    # SC-1 band-specific SNR verdict block (alignment decision class), from
    # jasper.audio_measurement.snr_policy.band_snr_verdicts over the overlap
    # band [fc/2, fc*2]. None when neither noise_band_report nor
    # noise_floor_dbfs was supplied to analyze_summed_crossover.
    snr: dict[str, Any] | None = None
    ambient: dict[str, Any] | None = None
    # True when null_depth_db was reduced from its raw measured value because
    # the overlap-band SNR could not prove a deeper null (see
    # jasper.audio_measurement.snr_policy.cap_null_depth_db). The verdict
    # above is always decided from the UNCAPPED measured depth; only the
    # reported number is capped.
    null_depth_capped: bool = False
    # Whether the crossover Fc (and its lower shoulder, Fc/2) sit above the
    # SC-2 low-frequency validity floor. True whenever gating was not applied
    # (near-field/exempt) or found no floor issue; False only when a
    # reference-axis capture's floor makes the null undecidable (paired with
    # verdict=unusable_capture — see analyze_summed_crossover).
    above_validity_floor: bool = True
    near_validity_floor: bool = False
    # SC-2 gating block — see DriverAcousticResult.gating.
    gating: dict[str, Any] | None = None

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
            "fr_curve": self.fr_curve,
            "calibrated": self.calibrated,
            "snr": self.snr,
            "ambient": self.ambient,
            "null_depth_capped": self.null_depth_capped,
            "above_validity_floor": self.above_validity_floor,
            "near_validity_floor": self.near_validity_floor,
            "gating": self.gating,
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

    ``jasper.audio_measurement.sweep.write_sweep_wav`` only emits mono; an active
    speaker needs the sweep routed to exactly one physical output so a single
    driver is excited. The returned ``DriverSweep.sweep_meta`` carries the exact
    synchronization parameters the analysis side must regenerate the reference
    sweep from (regenerating beats reloading the int16 WAV, which would add
    quantization error to the deconvolution reference).
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
    gating — deconvolving an unsafe (clipped / too short / wrong rate)
    capture would fabricate a curve, so we stop and report the failure
    instead.

    When ``calibration`` is supplied (an L2 calibrated measurement mic), the
    mic-correction curve is applied to the magnitude via the SAME
    ``jasper.audio_measurement.calibration.apply_calibration_curve`` the room-correction path
    uses, so the surfaced FR is calibrated and the null-depth shoulders (taken at
    different frequencies) are corrected rather than relying on the additive cal
    cancelling.

    ``capture_geometry`` selects whether the deconvolved impulse response is
    gated to its reflection-free span before the magnitude response is taken
    (see :mod:`jasper.audio_measurement.gating` and
    docs/active-crossover-information-design.md "Measurement validity: gating
    and the low-frequency floor"). ``"near_field"`` (today's shipped capture,
    taken a few centimetres from the driver) is exempt — the room cannot
    contaminate a capture that close — and the ungated IR is used, so a
    near-field caller's result is byte-identical to before this parameter
    existed. ``"reference_axis"`` (the ~1 m fixed-axis capture) gates the IR
    and returns the SC-2 gating block describing what was done; the returned
    ``gating`` dict is always populated (exempt or applied) whenever an IR
    exists at all.
    """
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
        from jasper.capture_relay.alignment import assert_alignment_confident

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
        ambient_start = arrival_sample - len(reference) - int(round(1.000 * sr))
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

        noise_bands = snr_policy.magnitude_band_levels(noise_freqs, noise_smoothed)
        robust = snr_policy.framed_ambient_band_report(
            robust_ambient_source,
            sr,
            percentile=95,
        )
        baseline = snr_policy.framed_ambient_band_report(
            ambient_source,
            sr,
            percentile=50,
        )
        robust_by_id = {item["band_id"]: item for item in robust["bands"]}
        baseline_by_id = {item["band_id"]: item for item in baseline["bands"]}
        adjusted = []
        for item in noise_bands:
            robust_item = robust_by_id.get(item["band_id"])
            baseline_item = baseline_by_id.get(item["band_id"])
            delta = (
                float(robust_item["level_dbfs"])
                - float(baseline_item["level_dbfs"])
                if robust_item is not None and baseline_item is not None
                else 0.0
            )
            adjusted.append({
                **item,
                "level_dbfs": round(float(item["level_dbfs"]) + delta, 2),
            })
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

    Deliberately mirrors how a ``noise_band_report`` is built — both sides via
    :func:`jasper.audio_measurement.snr_policy.band_levels_dbfs` on the raw
    signal — rather than the deconvolved (gain-corrected) magnitude response
    ``_capture_to_magnitude`` produces: an SNR verdict compares captured
    broadband energy against measured ambient noise in the same band, so both
    sides must be in the same (raw dBFS) units to be physically meaningful.
    """
    import numpy as np

    from jasper.audio_measurement import deconv, snr_policy
    from jasper.audio_measurement import sweep as sweep_mod

    captured, sr = sweep_mod.read_wav_mono(captured_wav)
    captured = deconv.cap_capture_length(captured, sweep_len=0, sample_rate=sr)
    return snr_policy.band_levels_dbfs(
        captured.astype(np.float64), sr, snr_policy.CROSSOVER_SNR_BANDS_HZ
    )


def _band_mean_db(freqs, mag_db, lo_hz: float, hi_hz: float) -> float | None:
    import numpy as np

    mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not bool(np.any(mask)):
        return None
    return float(np.mean(mag_db[mask]))


def _downsample_curve(
    freqs,
    mag_db,
    *,
    lo_hz: float = ANALYSIS_LO_HZ,
    hi_hz: float = ANALYSIS_HI_HZ,
    max_points: int = FR_CURVE_MAX_POINTS,
) -> dict[str, Any] | None:
    """Log-spaced downsample of a magnitude response, for the maintainer's plot.

    Restricted to the trusted analysis window and re-referenced to 0 dB at its
    peak — a RELATIVE shape (read it for Fc/slope, not absolute level; the
    cross-driver level relationship lives in ``overlap_levels`` / the alignment
    proposal). Keeps the surfaced JSON small. ``None`` for an empty window.
    """
    import numpy as np

    mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    f = freqs[mask]
    m = mag_db[mask]
    if f.size == 0:
        return None
    if f.size > max_points:
        targets = np.geomspace(f[0], f[-1], max_points)
        idx = np.unique(np.searchsorted(f, targets).clip(0, f.size - 1))
        f = f[idx]
        m = m[idx]
    m = m - float(np.max(m))
    return {
        "freqs_hz": [round(float(x), 2) for x in f],
        "mag_db": [round(float(x), 2) for x in m],
    }


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
) -> tuple[dict[str, Any], ...]:
    """Mean deconvolved magnitude in a one-octave band centred on each Fc.

    Returns one entry per crossover ``Fc`` (``{fc_hz, lo_hz, hi_hz, level_db,
    bins, usable, snr_verdict, above_validity_floor, near_validity_floor}``).
    An entry is ``usable`` only when the capture passed quality gating, was
    not silent, the mic did not clip, the band held at least
    ``OVERLAP_MIN_BINS`` bins, ``fc`` sits at/above the SC-2 low-frequency
    validity floor (when one applies), AND its SC-1 SNR verdict is not
    ``"insufficient"`` — otherwise ``level_db`` is NaN and ``usable`` is False
    so the level-match trim math can fail closed to the datasheet trim, same
    as a silent/clipped capture.

    ``snr_bands`` is the ``"bands"`` list from
    :func:`jasper.audio_measurement.snr_policy.band_snr_verdicts` (magnitude
    class) when the caller supplied noise evidence, else ``None``.
    ``snr_verdict`` is the worst verdict among the ``snr_bands`` entries
    covering ``[lo_hz, hi_hz]`` (:func:`~jasper.audio_measurement.snr_policy.worst_band_verdict`),
    or ``"unknown"`` when there is no evidence — which leaves ``usable``
    exactly as computed above (no regression for the shipped no-noise flow).
    A "reduced" verdict does NOT force ``usable=False``: it is a
    reduced-confidence result, not a refusal.

    ``validity_floor_hz`` is ``None`` when gating did not apply (near-field
    capture, or a reference-axis capture with no floor issue) — every entry
    is then ``above_validity_floor=True``. When a floor applies, an entry
    with ``fc < validity_floor_hz`` is marked ``above_validity_floor=False``
    (and thereby unusable); ``near_validity_floor`` is the advisory
    ``[floor, NEAR_FLOOR_RATIO * floor)`` reduced-confidence band and does
    NOT affect ``usable``.
    """
    import numpy as np

    from jasper.audio_measurement import snr_policy
    from jasper.audio_measurement.gating import NEAR_FLOOR_RATIO

    # A local `floor` narrowed to `float | None` (rather than branching on a
    # separate `has_floor` bool) lets every comparison below stay a plain
    # `floor is not None and ...` — mypy narrows `floor` from that guard.
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
                # Level AT fc from the 1/24-octave-smoothed magnitude. At the
                # crossover both adjacent drivers sit at their matched −6 dB
                # shoulder, so the driver-to-driver delta is exactly their
                # relative sensitivity. Taken as a point (not a linear-bin band
                # mean, which would skew a sloped response — the lower driver
                # rolls off while the upper rises); the smoothing is the
                # log-symmetric SNR averaging, and the [lo, hi] octave is the
                # confidence neighbourhood that must hold enough bins.
                level_db = float(np.interp(fc, freqs, mag_db))
        above = floor is None or fc >= floor
        near = floor is not None and floor <= fc < NEAR_FLOOR_RATIO * floor
        usable = (
            capture_usable
            and not silent
            and not mic_clipping
            and in_range
            and bins >= OVERLAP_MIN_BINS
            and math.isfinite(level_db)
            and above
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

    ``passband_hz`` is the driver's intended pass band (e.g. a woofer's
    ``(40, 400)``). A correct driver has more energy inside its band than
    outside it; a silent capture is flagged ``silent``; a driver whose energy
    sits clearly outside its band (mis-wired output, swapped driver) is flagged
    ``out_of_band``. ``observed_mic_dbfs`` is the capture RMS — the value
    ``measurement.record_driver_measurement`` already consumes.

    ``overlap_fcs`` are the crossover frequencies this driver participates in
    (from :func:`jasper.active_speaker.profile.crossover_edges_for_role`); each
    yields an overlap-band level entry (see :data:`OVERLAP_BAND_RATIO`) used to
    refine the datasheet sensitivity trim with a MEASURED level match. Magnitude
    only — never used to authorise a phase or delay decision.

    ``noise_band_report`` is an optional band-specific ambient-noise report:
    either the legacy correction-shape
    ``[{band_id, band_hz, level_dbfs}, ...]`` list, or a domain-tagged
    ``{domain, method, bands}`` report from the controlled pre-sweep quiet crop. When
    supplied, the SC-1 magnitude-class SNR verdict block
    (:func:`jasper.audio_measurement.snr_policy.band_snr_verdicts`) is
    computed and stored on the result's ``snr`` field, scoped to
    ``relevant_hz = passband_hz ∩ [ANALYSIS_LO_HZ, ANALYSIS_HI_HZ]``; when
    omitted, ``snr`` is ``None`` (no noise evidence to gate on).
    ``capture_geometry`` (``"near_field"`` default, or ``"reference_axis"``)
    selects IR gating — see :func:`_capture_to_magnitude`. When gating is
    applied and reports a low-frequency validity floor, every derived
    quantity here (in-band/out-of-band means, overlap-band levels) is
    restricted to data at/above that floor; a driver whose entire passband
    sits below the floor is reported ``unusable_capture`` rather than a
    magnitude computed from contaminated data (spec: "no proposal rests on
    data below the floor").
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
            ),
            fr_curve=None,
            calibrated=calibrated,
            snr=None,
            gating=None,
        )

    floor_hz = None
    if gating_block is not None and gating_block.get("applied"):
        floor_hz = gating_block.get("f_valid_floor_hz")
    eff_lo = max(ANALYSIS_LO_HZ, floor_hz) if floor_hz is not None else ANALYSIS_LO_HZ

    band_lo = max(lo, eff_lo)
    band_hi = min(hi, ANALYSIS_HI_HZ)

    if floor_hz is not None and band_lo >= band_hi:
        # The validity floor sits at/above this driver's own passband
        # ceiling: the reference-axis capture cannot decide anything about
        # this driver at all. Near-field splice (Slice 1) is the product fix
        # for this case; here we refuse rather than emit a magnitude
        # computed from below-floor data.
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
            ),
            fr_curve=_downsample_curve(freqs, mag_db),
            calibrated=calibrated,
            ambient=paired_ambient,
            gating=gating_block,
        )

    in_band = _band_mean_db(freqs, mag_db, band_lo, band_hi)

    # Out-of-band reference: trusted analysis window minus the passband. The
    # window's lower edge is the validity floor when one applies (eff_lo),
    # not the raw ANALYSIS_LO_HZ — near_field's eff_lo == ANALYSIS_LO_HZ
    # always, so this is byte-identical to before capture_geometry existed.
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
        ),
        fr_curve=_downsample_curve(freqs, mag_db),
        calibrated=calibrated,
        snr=snr_block,
        ambient=paired_ambient,
        gating=gating_block,
    )


def analyze_summed_crossover(
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    *,
    crossover_fc_hz: float,
    null_threshold_db: float = DEFAULT_NULL_THRESHOLD_DB,
    expect_null: bool = False,
    has_mic_calibration: bool = False,
    calibration: "CalibrationCurve | None" = None,
    noise_band_report: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    noise_floor_dbfs: float | None = None,
    capture_geometry: str = "near_field",
    ambient_duration_s: float | None = None,
) -> SummedAcousticResult:
    """Measure the cancellation null at the crossover in a summed-speaker capture.

    The null depth is the magnitude at ``crossover_fc_hz`` below the mean of the
    octave-away shoulders, which cancels the unknown absolute reference.

    Two capture kinds, selected by ``expect_null`` (the canonical reverse-polarity
    method in docs/HANDOFF-active-speaker-dsp.md). Both use the same
    ``null_threshold_db`` to decide whether a null is *present*; the per-capture
    verdict is a "did a null form?" signal, and the cap-independent polarity call
    (reverse-vs-in-phase margin) lives in ``crossover_alignment``:

    * ``expect_null=False`` (normal, in-phase): a correct crossover sums flat, so
      a deep null (``>= null_threshold_db``) is the polarity/delay PROBLEM.
    * ``expect_null=True`` (one adjacent driver inverted): a correct, time-aligned
      crossover now CANCELS, so a deep null (``>= null_threshold_db``) is the PASS
      signal and a shallow one flags delay/polarity/wiring/hardware.

    ``calibration`` (L2 calibrated mic) corrects the magnitude before the shoulder
    comparison; ``has_mic_calibration`` alone only relaxes the quality gate.

    ``noise_band_report`` (correction-shape band list) and/or
    ``noise_floor_dbfs`` (a single scalar) feed the SC-1 alignment-class SNR
    verdict (:func:`jasper.audio_measurement.snr_policy.band_snr_verdicts`)
    over the overlap band ``[fc/2, fc*2]``, stored on the result's ``snr``
    field. Per the split SNR policy, a scalar alone (or no evidence) reads as
    "unknown" for this decision class — it is not sufficient evidence for a
    null/alignment call. When real band evidence proves the overlap SNR, the
    REPORTED ``null_depth_db`` is capped at what that SNR can prove
    (:func:`~jasper.audio_measurement.snr_policy.cap_null_depth_db`,
    ``null_depth_capped=True``); the verdict below is always decided from the
    raw, uncapped measured depth — a capped-but-still-deep null is safely "at
    least that deep".
    ``capture_geometry`` — see :func:`_capture_to_magnitude`. When gating
    applies and reports a floor, and either ``crossover_fc_hz`` or its lower
    shoulder (``crossover_fc_hz / 2``, one of the two points the null depth is
    measured from) sits below it, the reference-axis capture cannot decide
    this crossover's null at all: reported ``unusable_capture`` rather than a
    null computed from contaminated data. ``above_validity_floor`` /
    ``near_validity_floor`` record the (non-excluding) advisory state for a
    usable result.
    """
    import numpy as np

    if crossover_fc_hz <= 0:
        raise DriverAcousticsError(
            f"crossover_fc_hz must be positive, got {crossover_fc_hz}"
        )

    report, freqs, mag_db, gating_block, paired_ambient = _capture_to_magnitude(
        captured_wav, sweep_meta, has_mic_calibration=has_mic_calibration,
        calibration=calibration, capture_geometry=capture_geometry,
        ambient_duration_s=ambient_duration_s,
    )
    quality_dict = report.to_dict()
    mic_clipping = report.clipped_fraction >= 1e-4
    calibrated = calibration is not None

    if freqs is None:
        return SummedAcousticResult(
            verdict=VERDICT_UNUSABLE_CAPTURE,
            null_depth_db=float("nan"),
            crossover_fc_hz=crossover_fc_hz,
            observed_mic_dbfs=report.rms_dbfs,
            mic_clipping=mic_clipping,
            quality=quality_dict,
            expect_null=expect_null,
            fr_curve=None,
            calibrated=calibrated,
            snr=None,
            null_depth_capped=False,
            gating=None,
            above_validity_floor=True,
            near_validity_floor=False,
        )

    floor_hz = None
    if gating_block is not None and gating_block.get("applied"):
        floor_hz = gating_block.get("f_valid_floor_hz")

    lower_shoulder_hz = crossover_fc_hz / 2.0
    if floor_hz is not None:
        if crossover_fc_hz < floor_hz or lower_shoulder_hz < floor_hz:
            # The room prevented a low-frequency decision here (spec:
            # "no proposal rests on data below the floor"). Never emits a
            # null computed from a below-floor shoulder.
            return SummedAcousticResult(
                verdict=VERDICT_UNUSABLE_CAPTURE,
                null_depth_db=float("nan"),
                crossover_fc_hz=crossover_fc_hz,
                observed_mic_dbfs=report.rms_dbfs,
                mic_clipping=mic_clipping,
                quality=quality_dict,
                expect_null=expect_null,
                fr_curve=None,
                calibrated=calibrated,
                ambient=paired_ambient,
                gating=gating_block,
                above_validity_floor=False,
                near_validity_floor=False,
            )
        from jasper.audio_measurement.gating import NEAR_FLOOR_RATIO

        above_validity_floor = True
        near_validity_floor = (
            floor_hz <= lower_shoulder_hz < NEAR_FLOOR_RATIO * floor_hz
        )
    else:
        above_validity_floor = True
        near_validity_floor = False

    at_fc = float(np.interp(crossover_fc_hz, freqs, mag_db))
    lower_shoulder = float(np.interp(lower_shoulder_hz, freqs, mag_db))
    upper_shoulder = float(np.interp(crossover_fc_hz * 2.0, freqs, mag_db))
    shoulder_mean = (lower_shoulder + upper_shoulder) / 2.0
    null_depth = shoulder_mean - at_fc

    deep = null_depth >= null_threshold_db
    if expect_null:
        # Reverse-polarity proof: the deep null is what we WANT.
        verdict = SUMMED_BLEND_OK if deep else SUMMED_POLARITY_OR_DELAY_PROBLEM
    else:
        verdict = SUMMED_POLARITY_OR_DELAY_PROBLEM if deep else SUMMED_BLEND_OK

    snr_block = None
    reported_null_depth = null_depth
    null_depth_capped = False
    effective_noise_report = paired_ambient or noise_band_report
    if effective_noise_report or noise_floor_dbfs is not None:
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
            decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
            capture_bands=capture_bands,
            noise_bands=noise_bands,
            noise_floor_dbfs_scalar=noise_floor_dbfs,
            relevant_hz=(
                max(crossover_fc_hz / 2.0, ANALYSIS_LO_HZ),
                min(crossover_fc_hz * 2.0, ANALYSIS_HI_HZ),
            ),
            model=DRIVER,
            band_method=band_method,
        )
        reported_null_depth, null_depth_capped = snr_policy.cap_null_depth_db(
            null_depth, snr_block.get("worst_relevant"), DRIVER.null_cap_margin_db,
        )

    return SummedAcousticResult(
        verdict=verdict,
        null_depth_db=reported_null_depth,
        crossover_fc_hz=crossover_fc_hz,
        observed_mic_dbfs=report.rms_dbfs,
        mic_clipping=mic_clipping,
        quality=quality_dict,
        expect_null=expect_null,
        fr_curve=_downsample_curve(freqs, mag_db),
        calibrated=calibrated,
        snr=snr_block,
        ambient=paired_ambient,
        null_depth_capped=null_depth_capped,
        gating=gating_block,
        above_validity_floor=above_validity_floor,
        near_validity_floor=near_validity_floor,
    )
