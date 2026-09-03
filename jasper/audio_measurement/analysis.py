# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Frequency-response smoothing and resampling for the magnitude response display + filter
design. Power-mean (RMS-of-amplitude) smoothing, not dB-mean, which over-emphasizes deep nulls
(Toole, *Sound Reproduction* 3rd ed. Ch. 4; Welti; REW and Acourate default to it too).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from jasper.audio_measurement.room_boundary import ROOM_BOUNDARY_DEFAULT_HZ

#: The canonical shoulders, as multiples of Fc (one octave either side). THE
#: one statement of the span: consumers multiply Fc by these rather than
#: restating them, so moving the canon is this line.
CANONICAL_SHOULDER_RATIOS: tuple[float, float] = (0.5, 2.0)

#: Samples a shoulder read needs strictly either side of Fc. Two, because
#: :func:`numpy.interp` interpolates between two and repeats a lone one — with
#: one sample on a side the subtraction below is that sample minus itself.
MIN_SHOULDER_SAMPLES = 2


@dataclass(frozen=True)
class ShoulderSpan:
    """Where a null depth's shoulders were read, and how far in they were forced.

    A real 2-way overlaps by less than the canonical two octaves (the reference speaker's is
    1.32); the span is the canonical one clamped into that overlap. **A clamped depth is not
    the same quantity as a full-span one** — shoulders inside the crossover's own rolloff sit
    below the passband, so the subtraction reads smaller.
    """

    crossover_fc_hz: float
    overlap_hz: tuple[float, float]
    used_hz: tuple[float, float]
    samples_below_fc: int
    samples_above_fc: int

    @property
    def canonical_hz(self) -> tuple[float, float]:
        return (
            self.crossover_fc_hz * CANONICAL_SHOULDER_RATIOS[0],
            self.crossover_fc_hz * CANONICAL_SHOULDER_RATIOS[1],
        )

    @property
    def lower_clamped(self) -> bool:
        return self.used_hz[0] > self.canonical_hz[0]

    @property
    def upper_clamped(self) -> bool:
        return self.used_hz[1] < self.canonical_hz[1]

    @property
    def used_octaves(self) -> float:
        lower, upper = self.used_hz
        if not lower > 0.0 or not upper > lower:
            return 0.0
        return math.log2(upper / lower)

    @property
    def usable(self) -> bool:
        """Not a quality bar — narrowness is disclosed, not refused. The floor below which
        there is nothing to read."""
        return (
            self.used_hz[0] < self.crossover_fc_hz < self.used_hz[1]
            and self.samples_below_fc >= MIN_SHOULDER_SAMPLES
            and self.samples_above_fc >= MIN_SHOULDER_SAMPLES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "crossover_fc_hz": self.crossover_fc_hz,
            "overlap_hz": list(self.overlap_hz),
            "canonical_hz": list(self.canonical_hz),
            "used_hz": list(self.used_hz),
            "used_octaves": self.used_octaves,
            "lower_clamped": self.lower_clamped,
            "upper_clamped": self.upper_clamped,
            "samples_below_fc": self.samples_below_fc,
            "samples_above_fc": self.samples_above_fc,
        }


def shoulder_span(
    freqs, *, crossover_fc_hz: float, overlap_hz: tuple[float, float]
) -> ShoulderSpan:
    """``overlap_hz`` is the band both branches carry evidence over. One owner, beside the
    subtraction that consumes it, so a proposal and an acoustic confirm agree."""

    lower_ratio, upper_ratio = CANONICAL_SHOULDER_RATIOS
    grid = np.asarray(freqs, dtype=float)
    return ShoulderSpan(
        crossover_fc_hz=float(crossover_fc_hz),
        overlap_hz=(float(overlap_hz[0]), float(overlap_hz[1])),
        used_hz=(
            max(crossover_fc_hz * lower_ratio, float(overlap_hz[0])),
            min(crossover_fc_hz * upper_ratio, float(overlap_hz[1])),
        ),
        samples_below_fc=int(np.count_nonzero(grid < crossover_fc_hz)),
        samples_above_fc=int(np.count_nonzero(grid > crossover_fc_hz)),
    )


def crossover_null_depth_db(
    freqs,
    mag_db,
    crossover_fc_hz: float,
    *,
    shoulders_hz: tuple[float, float] | None = None,
) -> float:
    """THE definition of null depth in this repo: the mean of the two shoulders minus the level
    at Fc, read off an already calibrated, already gated magnitude curve. Positive is a notch.

    Shoulders default to :data:`CANONICAL_SHOULDER_RATIOS` times Fc; a caller may narrow them
    with ``shoulders_hz`` (from :func:`shoulder_span`) rather than letting :func:`numpy.interp`
    clamp silently at the grid edge.

    Homed here, not beside its acoustic caller, because both consumers (``active_speaker.
    driver_acoustics``, ``crossover_v2.delay_landscape``) live ABOVE this package and
    ``jasper.audio_measurement`` may import neither (the boundary SSOT guard).
    """
    lower_hz, upper_hz = (
        (
            crossover_fc_hz * CANONICAL_SHOULDER_RATIOS[0],
            crossover_fc_hz * CANONICAL_SHOULDER_RATIOS[1],
        )
        if shoulders_hz is None
        else (float(shoulders_hz[0]), float(shoulders_hz[1]))
    )
    at_fc = float(np.interp(crossover_fc_hz, freqs, mag_db))
    lower_shoulder = float(np.interp(lower_hz, freqs, mag_db))
    upper_shoulder = float(np.interp(upper_hz, freqs, mag_db))
    return (lower_shoulder + upper_shoulder) / 2.0 - at_fc


def smooth_fractional_octave(
    freqs: np.ndarray,
    magnitude_db: np.ndarray,
    fraction: int = 48,
) -> np.ndarray:
    """1/N-octave magnitude smoothing in linear power. ``fraction`` 48 ~= "psychoacoustic
    detail" (REW terminology), 3 = audiometric; higher is sharper."""
    if fraction <= 0:
        raise ValueError(f"fraction must be positive, got {fraction}")
    if len(freqs) != len(magnitude_db):
        raise ValueError(
            f"length mismatch: freqs={len(freqs)} magnitude={len(magnitude_db)}"
        )
    # dB -> linear power for averaging; dB-mean would over-emphasize deep nulls.
    power = 10.0 ** (magnitude_db / 10.0)
    factor = 2.0 ** (1.0 / (2.0 * fraction))

    smoothed = np.empty_like(power)
    n = len(freqs)
    finite = np.all(np.isfinite(freqs)) and np.all(np.isfinite(power))
    positive = freqs > 0
    smoothed[~positive] = power[~positive]

    if finite and np.any(positive):
        positive_freqs = freqs[positive]
        lo_idx = np.searchsorted(freqs, positive_freqs / factor, side="left")
        hi_idx = np.searchsorted(freqs, positive_freqs * factor, side="right")
        lo_idx = np.maximum(0, lo_idx)
        hi_idx = np.maximum(lo_idx + 1, np.minimum(n, hi_idx))

        # Prefix sums reduce every power mean to two indexed reads and a subtraction.
        prefix_dtype = np.result_type(power.dtype, np.float64)
        prefix = np.empty(n + 1, dtype=prefix_dtype)
        prefix[0] = 0
        np.cumsum(power, dtype=prefix_dtype, out=prefix[1:])
        reverse_prefix = np.empty(n + 1, dtype=prefix_dtype)
        reverse_prefix[0] = 0
        np.cumsum(power[::-1], dtype=prefix_dtype, out=reverse_prefix[1:])
        finite = (
            np.all(np.isfinite(prefix))
            and np.all(np.isfinite(reverse_prefix))
            and np.all(hi_idx <= n)
        )
        if finite:
            forward_sum = prefix[hi_idx] - prefix[lo_idx]
            reverse_sum = (
                reverse_prefix[n - lo_idx] - reverse_prefix[n - hi_idx]
            )
            use_reverse = reverse_prefix[n - hi_idx] < prefix[lo_idx]
            window_sum = np.where(use_reverse, reverse_sum, forward_sum)
            subtraction_scale = np.where(
                use_reverse,
                reverse_prefix[n - lo_idx] + reverse_prefix[n - hi_idx],
                prefix[hi_idx] + prefix[lo_idx],
            )
            # If even the better-direction subtraction is ill-conditioned, fall back per-window.
            suspect = (subtraction_scale > 0) & (
                window_sum
                <= np.finfo(prefix_dtype).eps * 1e12 * subtraction_scale
            )
            smoothed[positive] = window_sum / (hi_idx - lo_idx)
            positive_idx = np.flatnonzero(positive)
            for position in np.flatnonzero(suspect):
                i = positive_idx[position]
                smoothed[i] = float(
                    np.mean(power[lo_idx[position]:hi_idx[position]])
                )

    if not finite:
        # Prefix subtraction can contaminate windows after NaN/+inf or overflow; scalar fallback.
        for i in range(n):
            f = freqs[i]
            if f <= 0:
                smoothed[i] = power[i]
                continue
            lower = f / factor
            upper = f * factor
            lo_idx = int(np.searchsorted(freqs, lower, side="left"))
            hi_idx = int(np.searchsorted(freqs, upper, side="right"))
            lo_idx = max(0, lo_idx)
            hi_idx = max(lo_idx + 1, min(n, hi_idx))
            smoothed[i] = float(np.mean(power[lo_idx:hi_idx]))

    # Clamp before log to avoid -inf for any all-zero windows.
    return 10.0 * np.log10(np.maximum(smoothed, 1e-12))


def resample_log(
    freqs: np.ndarray,
    magnitude_db: np.ndarray,
    *,
    f_min: float = 20.0,
    f_max: float = 20000.0,
    n_points: int = 480,
) -> tuple[np.ndarray, np.ndarray]:
    """480 points across 20 Hz-20 kHz is roughly 1/48-octave — enough detail for the modal
    range and tractable for a JSON payload to the iPhone."""
    if n_points < 2:
        raise ValueError(f"n_points must be ≥ 2, got {n_points}")
    if f_max <= f_min:
        raise ValueError(f"f_max ({f_max}) must be > f_min ({f_min})")

    log_freqs = np.geomspace(f_min, f_max, n_points)
    interp = np.interp(log_freqs, freqs, magnitude_db)
    return log_freqs.astype(np.float64), interp.astype(np.float64)


def spatial_average_db(
    magnitudes_db: list[np.ndarray],
) -> np.ndarray:
    """Per Toole/Welti/Olive: room responses average sensibly in LINEAR POWER, not dB — dB
    averaging over-emphasizes deep nulls (a single -30 dB null at one position would drag the
    whole region down even with flat response elsewhere). Power-averaged across the WHOLE
    spectrum (no vector-mean/power-mean Schroeder split — this pipeline drops phase after
    deconvolution). Empty list raises ``ValueError``; 1 element returns itself."""
    if not magnitudes_db:
        raise ValueError("need at least one magnitude array")
    if len(magnitudes_db) == 1:
        return magnitudes_db[0].astype(np.float64)
    stack = np.stack([m.astype(np.float64) for m in magnitudes_db], axis=0)
    # dB -> linear power -> mean -> dB
    power = 10.0 ** (stack / 10.0)
    mean_power = power.mean(axis=0)
    return 10.0 * np.log10(np.maximum(mean_power, 1e-12))


def deviation_metrics(
    measured_db: np.ndarray,
    target_db: np.ndarray,
    freqs: np.ndarray,
    *,
    f_low: float = 50.0,
    f_high: float = ROOM_BOUNDARY_DEFAULT_HZ,
) -> dict[str, float]:
    """ABSOLUTE deviation-from-target stats over one band — says nothing about before/after on
    its own; the verify path diffs two calls over the same band (see `before_after_delta`).

    ``f_low`` defaults to 50 Hz, not 20 Hz: the iPhone built-in mic's ~24 dB/octave HPF starts
    around 250 Hz (Apple hardware spec), so 20-50 Hz is dominated by the mic's own HPF + noise
    floor, not the room — including it produced absurd "max 56 dB deviation" readings that were
    mic artifacts.

    ``f_high`` defaults to :data:`jasper.audio_measurement.room_boundary.ROOM_BOUNDARY_DEFAULT_HZ`,
    routed through the boundary SSOT rather than re-declared (a hard-coded copy would silently
    cap acceptance/verify/envelope at 350 Hz once the ceiling goes per-room, #1787). The 50 Hz
    low edge is NOT that seam — it's the mic-physics floor above — so it stays a literal here.
    """
    band = (freqs >= f_low) & (freqs <= f_high)
    if not band.any():
        return {"rms_db": 0.0, "max_db": 0.0, "n_points": 0}
    delta = (measured_db - target_db)[band]
    rms = float(np.sqrt(np.mean(delta ** 2)))
    max_dev = float(np.max(np.abs(delta)))
    return {
        "rms_db": rms,
        "max_db": max_dev,
        "n_points": int(band.sum()),
    }


def before_after_fill_segments(
    freqs: np.ndarray,
    before_db: np.ndarray,
    after_db: np.ndarray,
    target_db: np.ndarray,
    *,
    f_low: float = 50.0,
    f_high: float = ROOM_BOUNDARY_DEFAULT_HZ,
) -> list[dict[str, Any]]:
    """Tag each contiguous in-band segment as improved or regressed, so the browser fills the
    before/after chart area from server-computed data rather than deriving it.

    "Improved" means ``|after - target| < |before - target|`` at that grid point; a tie or a
    move further from target reads "regressed" — improvement is never claimed without evidence.
    Returned segments carry inclusive grid index ranges (`i_lo`/`i_hi`) plus frequency bounds.

    Coupling note: tones are computed from the RAW 480-point log grid, but the browser draws
    the fill on its DISPLAY curves, which may be chart-smoothed. Safe today because the chart's
    smoothing preserves the grid; a future RESAMPLING display transform would break this index
    alignment.
    """
    if not (len(freqs) == len(before_db) == len(after_db) == len(target_db)):
        raise ValueError(
            "freqs/before/after/target length mismatch: "
            f"{len(freqs)}/{len(before_db)}/{len(after_db)}/{len(target_db)}"
        )
    band = (freqs >= f_low) & (freqs <= f_high)
    band_idx = np.nonzero(band)[0]
    if band_idx.size == 0:
        return []

    before_err = np.abs(before_db - target_db)
    after_err = np.abs(after_db - target_db)
    # Strict improvement only: ties read "regressed".
    improved = after_err < before_err

    segments: list[dict[str, Any]] = []
    run_start = int(band_idx[0])
    prev = int(band_idx[0])
    run_tone = bool(improved[run_start])

    def _emit(i_lo: int, i_hi: int, is_improved: bool) -> None:
        segments.append({
            "tone": "improved" if is_improved else "regressed",
            "i_lo": i_lo,
            "i_hi": i_hi,
            "f_lo_hz": float(freqs[i_lo]),
            "f_hi_hz": float(freqs[i_hi]),
        })

    for raw in band_idx[1:]:
        idx = int(raw)
        tone_here = bool(improved[idx])
        # A gap in the band index or a tone flip closes the current run.
        if idx != prev + 1 or tone_here != run_tone:
            _emit(run_start, prev, run_tone)
            run_start = idx
            run_tone = tone_here
        prev = idx
    _emit(run_start, prev, run_tone)
    return segments


def before_after_delta(
    freqs: np.ndarray,
    before_db: np.ndarray,
    after_db: np.ndarray,
    target_db: np.ndarray,
    *,
    f_low: float = 50.0,
    f_high: float = ROOM_BOUNDARY_DEFAULT_HZ,
) -> dict[str, Any]:
    """Honest MEASURED before/after readout: both metrics computed by `deviation_metrics` over
    the SAME band, guarding against a band-mismatch trap (verify used 50-350 Hz while a design's
    predicted "before" was over the strategy band). `delta.rms_db`/`delta.max_db` are positive
    when the correction reduced deviation."""
    before = deviation_metrics(
        before_db, target_db, freqs, f_low=f_low, f_high=f_high,
    )
    after = deviation_metrics(
        after_db, target_db, freqs, f_low=f_low, f_high=f_high,
    )
    return {
        "band_hz": [float(f_low), float(f_high)],
        "before": before,
        "after": after,
        "delta": {
            "rms_db": before["rms_db"] - after["rms_db"],
            "max_db": before["max_db"] - after["max_db"],
        },
        "fill_segments": before_after_fill_segments(
            freqs, before_db, after_db, target_db,
            f_low=f_low, f_high=f_high,
        ),
    }


def normalize_to_band(
    freqs: np.ndarray,
    magnitude_db: np.ndarray,
    *,
    f_low: float = 200.0,
    f_high: float = 1000.0,
) -> np.ndarray:
    """Shift so [f_low, f_high]'s average dB level is 0 — a measured response has arbitrary
    absolute level (mic gain, speaker SPL, distance), and filter design cares about SHAPE.
    Anchored at 200-1000 Hz, where speaker directivity is well-controlled and the iPhone-mic
    compensation is most accurate."""
    band = (freqs >= f_low) & (freqs <= f_high)
    if not band.any():
        # Fall back to the full-range mean; resample_log's range covers 20-20k so this is rare.
        ref = float(np.mean(magnitude_db))
    else:
        ref = float(np.mean(magnitude_db[band]))
    return (magnitude_db - ref).astype(np.float64)


from typing import Mapping, Sequence


_THIRD_OCTAVE_CENTERS_HZ = (20.0, 25.0, 31.5, 40.0, 50.0, 63.0,
                            80.0, 100.0, 125.0, 160.0, 200.0)
_THIRD_OCTAVE_EDGE_FACTOR = 2.0 ** (1.0 / 6.0)
THIRD_OCTAVE_BASS_BANDS_HZ: tuple[tuple[float, float], ...] = tuple(
    (center / _THIRD_OCTAVE_EDGE_FACTOR, center * _THIRD_OCTAVE_EDGE_FACTOR)
    for center in _THIRD_OCTAVE_CENTERS_HZ
)


def band_levels_from_magnitude(
    freqs,
    magnitude_db,
    bands,
) -> tuple[float, ...]:
    """Return the power-mean magnitude in each requested band."""

    frequencies = np.asarray(freqs, dtype=np.float64)
    magnitude = np.asarray(magnitude_db, dtype=np.float64)
    if frequencies.ndim != 1 or magnitude.ndim != 1 or len(frequencies) != len(magnitude):
        raise ValueError("frequency and magnitude arrays must be matched 1-D data")
    levels = []
    for low, high in bands:
        mask = (frequencies >= low) & (frequencies < high)
        if not np.any(mask):
            raise ValueError(f"band {low:g}-{high:g} Hz has no frequency bins")
        power = 10.0 ** (magnitude[mask] / 10.0)
        levels.append(10.0 * np.log10(max(float(np.mean(power)), 1e-12)))
    return tuple(levels)


def thd_curve(
    fund_freqs,
    fund_db,
    harmonics: Mapping[int, tuple[np.ndarray, np.ndarray]],
    band=(20.0, 200.0),
    noise_floor: tuple[np.ndarray, np.ndarray] | None = None,
    min_fund_snr_db: float = 10.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return total-harmonic-distortion ratio on the fundamental grid."""

    frequencies = np.asarray(fund_freqs, dtype=np.float64)
    fundamental_db = np.asarray(fund_db, dtype=np.float64)
    if frequencies.ndim != 1 or len(frequencies) != len(fundamental_db):
        raise ValueError("fundamental frequency and magnitude arrays must match")
    mask = (frequencies >= band[0]) & (frequencies <= band[1])
    output_freqs = frequencies[mask]
    fundamental = 10.0 ** (fundamental_db[mask] / 20.0)
    harmonic_power = np.zeros_like(output_freqs)
    for order, (harmonic_freqs, harmonic_db) in harmonics.items():
        if type(order) is not int or order < 2:
            raise ValueError("harmonic orders must be integers of at least 2")
        source_freqs = np.asarray(harmonic_freqs, dtype=np.float64)
        source_db = np.asarray(harmonic_db, dtype=np.float64)
        if source_freqs.ndim != 1 or len(source_freqs) != len(source_db):
            raise ValueError("harmonic frequency and magnitude arrays must match")
        interpolated = np.interp(
            output_freqs,
            source_freqs,
            source_db,
            left=-6000.0,
            right=-6000.0,
        )
        harmonic_power += 10.0 ** (interpolated / 10.0)
    ratio = np.sqrt(harmonic_power) / np.maximum(fundamental, 1e-300)
    if noise_floor is not None:
        noise_freqs = np.asarray(noise_floor[0], dtype=np.float64)
        noise_db = np.asarray(noise_floor[1], dtype=np.float64)
        if noise_freqs.ndim != 1 or len(noise_freqs) != len(noise_db):
            raise ValueError("noise-floor frequency and magnitude arrays must match")
        interpolated_noise = np.interp(output_freqs, noise_freqs, noise_db)
        ratio[fundamental_db[mask] - interpolated_noise <= min_fund_snr_db] = np.nan
    return output_freqs, ratio


def compression_curve(
    rungs: Sequence[tuple[float, tuple[float, ...]]],
) -> tuple[tuple[float, ...], ...]:
    """Return measured-minus-linear-extrapolation compression per rung."""

    if not rungs:
        return ()
    first_command, first_levels = rungs[0]
    width = len(first_levels)
    if any(len(levels) != width for _, levels in rungs):
        raise ValueError("all compression rungs must have the same band count")
    if any(rungs[index][0] <= rungs[index - 1][0] for index in range(1, len(rungs))):
        raise ValueError("compression rungs must be in ascending commanded order")
    return tuple(
        tuple(
            float(measured) - (float(baseline) + command - first_command)
            for measured, baseline in zip(levels, first_levels)
        )
        for command, levels in rungs
    )


def _offset_invariant_rms_and_max(
    measured: np.ndarray, predicted: np.ndarray
) -> tuple[float, float]:
    """RMS and max-absolute of ``measured - predicted``, mean-centered so a uniform gain
    difference (mic sensitivity, session volume) does not by itself read as a tracking error.

    **Pass a REAL prediction.** A CONSTANT ``predicted`` array collapses this into plain
    flatness-vs-band-mean on ``measured`` alone — the retired per-capture construction PR-5
    removed. That reading is single-position, exclusion-blind, and NOT the speaker's flatness
    (``active_speaker.flat_spec`` owns that claim on the spatial cloud).
    """
    error = measured - predicted
    error -= float(np.mean(error))
    return float(np.sqrt(np.mean(error ** 2))), float(np.max(np.abs(error)))


def _band_mask(frequencies: np.ndarray, band: tuple[float, float]) -> np.ndarray:
    mask = (frequencies >= band[0]) & (frequencies <= band[1])
    if not np.any(mask):
        raise ValueError("tracking band has no frequency bins")
    return mask


def tracking_error_db(
    freqs,
    measured_db,
    predicted_db,
    band,
) -> tuple[float, float]:
    """Return level-offset-invariant RMS and max-absolute tracking error."""

    frequencies = np.asarray(freqs, dtype=np.float64)
    measured = np.asarray(measured_db, dtype=np.float64)
    predicted = np.asarray(predicted_db, dtype=np.float64)
    if not (frequencies.ndim == measured.ndim == predicted.ndim == 1):
        raise ValueError("tracking arrays must be 1-D")
    if not (len(frequencies) == len(measured) == len(predicted)):
        raise ValueError("tracking arrays must have matching lengths")
    mask = _band_mask(frequencies, band)
    return _offset_invariant_rms_and_max(measured[mask], predicted[mask])


def notch_excluded_tracking_error_db(
    freqs,
    measured_db,
    predicted_db,
    band,
    *,
    notch_exclusion_db: float,
    notch_reference_db=None,
) -> tuple[float, float]:
    """Same as :func:`tracking_error_db`, but first drops any bin whose PREDICTED level sits
    more than ``notch_exclusion_db`` below the band's own predicted median — inside a deep
    predicted notch, depth is hypersensitive to sub-dB branch differences and is not a
    meaningful tracking signal (W6.7 ruling 1: a 27.8 dB "max" tracking error was entirely a
    shifted predicted notch).

    Deliberately asymmetric: keys on the PREDICTED level only, never measured — a deep MEASURED
    notch where the prediction is flat is the wrong-polarity/wrong-alignment discriminant and
    must count in full (pinned by the case-A/case-B fixtures in
    ``tests/test_audio_measurement_harmonics.py``). ``notch_reference_db`` supplies the
    unsmoothed predicted curve when ``predicted_db`` has been smoothed.

    Falls back to the full band when every bin would be excluded. Bin set owned by
    :func:`notch_excluded_band_mask`.
    """
    frequencies = np.asarray(freqs, dtype=np.float64)
    measured = np.asarray(measured_db, dtype=np.float64)
    predicted = np.asarray(predicted_db, dtype=np.float64)
    if not (frequencies.ndim == measured.ndim == predicted.ndim == 1):
        raise ValueError("tracking arrays must be 1-D")
    if not (len(frequencies) == len(measured) == len(predicted)):
        raise ValueError("tracking arrays must have matching lengths")
    keep = notch_excluded_band_mask(
        frequencies, predicted, band,
        notch_exclusion_db=notch_exclusion_db,
        notch_reference_db=notch_reference_db,
    )
    return _offset_invariant_rms_and_max(measured[keep], predicted[keep])


def notch_excluded_band_mask(
    freqs,
    predicted_db,
    band,
    *,
    notch_exclusion_db: float,
    notch_reference_db=None,
) -> np.ndarray:
    """A **full-grid** boolean mask: in-band, minus any bin whose PREDICTED level sits more
    than ``notch_exclusion_db`` below the band's own predicted median (see
    :func:`notch_excluded_tracking_error_db`). Falls back to the whole band when every bin
    would be excluded.

    **The single owner of that bin choice** — a second consumer, rung P1's frame fit
    (:func:`jasper.audio_measurement.frame_fit.fit_frame`), must use the same bins: fitting a
    straight line through a deep predicted notch lets the notch's depth lever the slope
    (measured: a 25 dB notch flipped an injected -0.800 dB/octave frame to +0.226).
    """
    frequencies = np.asarray(freqs, dtype=np.float64)
    predicted = np.asarray(predicted_db, dtype=np.float64)
    notch_reference = (
        predicted
        if notch_reference_db is None
        else np.asarray(notch_reference_db, dtype=np.float64)
    )
    if not (frequencies.ndim == predicted.ndim == notch_reference.ndim == 1):
        raise ValueError("tracking arrays must be 1-D")
    if not (len(frequencies) == len(predicted) == len(notch_reference)):
        raise ValueError("tracking arrays must have matching lengths")
    in_band = _band_mask(frequencies, band)
    median_predicted = float(np.median(notch_reference[in_band]))
    keep = in_band & (notch_reference >= (median_predicted - notch_exclusion_db))
    return keep if bool(np.any(keep)) else in_band
