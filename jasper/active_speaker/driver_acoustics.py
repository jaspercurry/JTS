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

It reuses the room-correction sweep / deconvolution / analysis primitives in
[`jasper.correction`](../correction/__init__.py) rather than reinventing the
DSP — the only thing correction can't do is target one physical output, so
``write_driver_sweep_wav`` builds a channel-targeted multichannel WAV (sweep on
one channel, silence elsewhere). numpy/scipy and the correction modules are
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

if TYPE_CHECKING:
    from jasper.correction.calibration import CalibrationCurve

# Default swept-sine parameters. Shorter than room correction's 10 s — a single
# driver needs far less SNR than a multi-position room average — but the same
# band and sample rate so the correction deconvolution path is reused verbatim.
DEFAULT_F1_HZ = 20.0
DEFAULT_F2_HZ = 20000.0
DEFAULT_DURATION_S = 6.0
DEFAULT_SAMPLE_RATE = 48000
# Conservative digital amplitude; the caller may lower it. This is signal
# amplitude only — acoustic level is governed by the volume/DSP chain.
DEFAULT_AMPLITUDE_DBFS = -18.0

# Frequency window we trust for a phone-mic + speaker sweep. Below ~40 Hz and
# above ~18 kHz, room modes, mic roll-off, and sweep fade dominate.
ANALYSIS_LO_HZ = 40.0
ANALYSIS_HI_HZ = 18000.0
DEFAULT_SMOOTHING_FRACTION = 24

# Verdict thresholds (all differential, so the unknown absolute calibration of
# the deconvolved magnitude cancels out).
SILENT_PEAK_DBFS = -45.0  # at/below this the capture is effectively silent
PRESENT_MIN_SEPARATION_DB = 0.0  # in-band must be at least as strong as out
OUT_OF_BAND_SEPARATION_DB = -3.0  # clearly more energy outside the band
DEFAULT_NULL_THRESHOLD_DB = 6.0  # a crossover null this deep is "present"

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
# math fails closed to the datasheet sensitivity trim.
OVERLAP_MIN_BINS = 4

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

    ``jasper.correction.sweep.write_sweep_wav`` only emits mono; an active
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

    from jasper.correction import sweep as sweep_mod

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
):
    """Shared capture → (quality, freqs, smoothed_magnitude_db) pipeline.

    Returns ``(quality, None, None)`` when the capture fails quality gating —
    deconvolving an unsafe (clipped / too short / wrong rate) capture would
    fabricate a curve, so we stop and report the failure instead.

    When ``calibration`` is supplied (an L2 calibrated measurement mic), the
    mic-correction curve is applied to the magnitude via the SAME
    ``correction.calibration.apply_calibration_curve`` the room-correction path
    uses, so the surfaced FR is calibrated and the null-depth shoulders (taken at
    different frequencies) are corrected rather than relying on the additive cal
    cancelling.
    """
    import numpy as np

    from jasper.correction import analysis, calibration as calibration_mod, deconv, quality
    from jasper.correction import sweep as sweep_mod

    has_cal = has_mic_calibration or calibration is not None
    sample_rate = int(sweep_meta["sample_rate"])
    n_samples = int(sweep_meta["n_samples"])

    captured, sr = sweep_mod.read_wav_mono(captured_wav)
    # Bound the capture before assess + deconv (mirrors the /correction
    # session path) so quality and the IR describe the same signal and an
    # over-long capture can't drive the FFT to OOM on the 1 GB Pi.
    raw_capture_samples = len(captured)
    captured = deconv.cap_capture_length(
        captured, sweep_len=n_samples, sample_rate=sr,
    )
    report = quality.assess_capture(
        captured,
        sample_rate=sr,
        expected_sample_rate=sample_rate,
        sweep_n_samples=n_samples,
        has_mic_calibration=has_cal,
        truncated_from_samples=raw_capture_samples,
    )
    if report.failed:
        return report, None, None

    reference, _ = sweep_mod.synchronized_swept_sine(
        f1=float(sweep_meta["f1"]),
        f2=float(sweep_meta["f2"]),
        duration_approx_s=float(sweep_meta["duration_s"]),
        sample_rate=sample_rate,
        amplitude_dbfs=float(sweep_meta["amplitude_dbfs"]),
    )
    ir = deconv.deconvolve(
        captured.astype(np.float64),
        reference.astype(np.float64),
        sample_rate=sr,
    )
    freqs, mag_db = deconv.magnitude_response(ir, sr, normalize=False)
    smoothed = analysis.smooth_fractional_octave(
        freqs, mag_db, DEFAULT_SMOOTHING_FRACTION
    )
    if calibration is not None:
        smoothed = calibration_mod.apply_calibration_curve(freqs, smoothed, calibration)
    return report, freqs, smoothed


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
) -> tuple[dict[str, Any], ...]:
    """Mean deconvolved magnitude in a one-octave band centred on each Fc.

    Returns one entry per crossover ``Fc`` (``{fc_hz, lo_hz, hi_hz, level_db,
    bins, usable}``). An entry is ``usable`` only when the capture passed quality
    gating, was not silent, the mic did not clip, and the band held at least
    ``OVERLAP_MIN_BINS`` bins — otherwise ``level_db`` is NaN and ``usable`` is
    False so the level-match trim math can fail closed to the datasheet trim.
    """
    import numpy as np

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
        usable = (
            capture_usable
            and not silent
            and not mic_clipping
            and in_range
            and bins >= OVERLAP_MIN_BINS
            and math.isfinite(level_db)
        )
        entries.append({
            "fc_hz": fc,
            "lo_hz": lo,
            "hi_hz": hi,
            "level_db": level_db,
            "bins": bins,
            "usable": usable,
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
    """
    import numpy as np

    lo, hi = float(passband_hz[0]), float(passband_hz[1])
    if not (0 < lo < hi):
        raise DriverAcousticsError(f"invalid passband_hz: {passband_hz!r}")

    report, freqs, mag_db = _capture_to_magnitude(
        captured_wav, sweep_meta, has_mic_calibration=has_mic_calibration,
        calibration=calibration,
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
            ),
            fr_curve=None,
            calibrated=calibrated,
        )

    band_lo = max(lo, ANALYSIS_LO_HZ)
    band_hi = min(hi, ANALYSIS_HI_HZ)
    in_band = _band_mean_db(freqs, mag_db, band_lo, band_hi)

    # Out-of-band reference: trusted analysis window minus the passband.
    out_mask = ((freqs >= ANALYSIS_LO_HZ) & (freqs <= ANALYSIS_HI_HZ)) & ~(
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
        ),
        fr_curve=_downsample_curve(freqs, mag_db),
        calibrated=calibrated,
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
    """
    import numpy as np

    if crossover_fc_hz <= 0:
        raise DriverAcousticsError(
            f"crossover_fc_hz must be positive, got {crossover_fc_hz}"
        )

    report, freqs, mag_db = _capture_to_magnitude(
        captured_wav, sweep_meta, has_mic_calibration=has_mic_calibration,
        calibration=calibration,
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
        )

    at_fc = float(np.interp(crossover_fc_hz, freqs, mag_db))
    lower_shoulder = float(np.interp(crossover_fc_hz / 2.0, freqs, mag_db))
    upper_shoulder = float(np.interp(crossover_fc_hz * 2.0, freqs, mag_db))
    shoulder_mean = (lower_shoulder + upper_shoulder) / 2.0
    null_depth = shoulder_mean - at_fc

    deep = null_depth >= null_threshold_db
    if expect_null:
        # Reverse-polarity proof: the deep null is what we WANT.
        verdict = SUMMED_BLEND_OK if deep else SUMMED_POLARITY_OR_DELAY_PROBLEM
    else:
        verdict = SUMMED_POLARITY_OR_DELAY_PROBLEM if deep else SUMMED_BLEND_OK
    return SummedAcousticResult(
        verdict=verdict,
        null_depth_db=null_depth,
        crossover_fc_hz=crossover_fc_hz,
        observed_mic_dbfs=report.rms_dbfs,
        mic_clipping=mic_clipping,
        quality=quality_dict,
        expect_null=expect_null,
        fr_curve=_downsample_curve(freqs, mag_db),
        calibrated=calibrated,
    )
