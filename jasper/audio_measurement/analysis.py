# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Frequency-response smoothing and resampling for the magnitude
response display + filter design.

We use power-mean (RMS-of-amplitude) smoothing rather than dB-mean,
because dB-mean over-emphasizes deep nulls — Toole (Sound Reproduction
3rd ed., Ch. 4) and Welti are clear on this. REW and Acourate also
default to power-mean.

For the V1 scope (modal-range PEQ from a single-position measurement)
we don't need RT60 / Schroeder estimation — that's Phase 2+ when
multi-position averaging adds value. So this module is intentionally
small.
"""
from __future__ import annotations

from typing import Any

import numpy as np


def smooth_fractional_octave(
    freqs: np.ndarray,
    magnitude_db: np.ndarray,
    fraction: int = 48,
) -> np.ndarray:
    """1/N-octave magnitude smoothing in linear power.

    Args:
      freqs: linear-spaced frequency grid (e.g. from rfftfreq), in Hz.
      magnitude_db: matching magnitude in dB.
      fraction: 1/N-octave. 48 ≈ "psychoacoustic detail" (REW
        terminology). 3 = audiometric. Higher fractions are sharper.

    Returns:
      Smoothed magnitude in dB on the same grid.
    """
    if fraction <= 0:
        raise ValueError(f"fraction must be positive, got {fraction}")
    if len(freqs) != len(magnitude_db):
        raise ValueError(
            f"length mismatch: freqs={len(freqs)} magnitude={len(magnitude_db)}"
        )
    # dB → linear power for averaging (the right thing for
    # acoustic energy — dB-mean would over-emphasize deep nulls).
    power = 10.0 ** (magnitude_db / 10.0)
    factor = 2.0 ** (1.0 / (2.0 * fraction))

    # The straightforward implementation is O(N * window_size) which
    # at N=24k bins (rfft of 48k) and a half-octave window of ~50
    # bins is ~1.2M ops. That's fast enough (<10 ms in numpy) that
    # we don't bother with cumulative-sum tricks; clarity wins.
    smoothed = np.empty_like(power)
    n = len(freqs)
    for i in range(n):
        f = freqs[i]
        if f <= 0:
            smoothed[i] = power[i]
            continue
        lower = f / factor
        upper = f * factor
        # Linear bins in `freqs`, so use binary-search bounds.
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
    """Resample a frequency response onto a log-spaced grid.

    Browser uPlot / canvas charts want log-frequency display, and
    480 points across 20 Hz – 20 kHz is roughly 1/48-octave —
    enough detail for the modal range and tractable for a JSON
    payload to the iPhone.
    """
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
    """Power-mean averaging of multiple positions' magnitude responses.

    Per Toole / Welti / Olive: room responses average sensibly in
    LINEAR POWER (squared amplitude), not in dB. Power averaging
    correctly reflects how the ear integrates energy from
    decorrelated reflection paths across positions, while dB
    averaging would over-emphasize deep nulls (a single -30 dB null
    at one position would drag the whole region down even if the
    other four positions have flat response there).

    For Phase 2 simplicity we power-average across the WHOLE
    spectrum. The strict Schroeder split (vector-mean below, power-
    mean above) requires keeping complex H(f) per position rather
    than just the magnitude, which our pipeline doesn't currently
    do — we drop phase right after deconvolution. Power-mean
    everywhere is what HouseCurve and most simpler tools do, and
    Toole's published target curves were derived from power-averaged
    measurements. Strict Schroeder split is a Phase 3 refinement.

    Args:
      magnitudes_db: list of N dB arrays, each on the same frequency
        grid. Empty list raises ValueError; 1 element returns itself.

    Returns:
      Averaged magnitude in dB.
    """
    if not magnitudes_db:
        raise ValueError("need at least one magnitude array")
    if len(magnitudes_db) == 1:
        return magnitudes_db[0].astype(np.float64)
    stack = np.stack([m.astype(np.float64) for m in magnitudes_db], axis=0)
    # dB → linear power → mean → linear power → dB
    power = 10.0 ** (stack / 10.0)
    mean_power = power.mean(axis=0)
    return 10.0 * np.log10(np.maximum(mean_power, 1e-12))


def deviation_metrics(
    measured_db: np.ndarray,
    target_db: np.ndarray,
    freqs: np.ndarray,
    *,
    f_low: float = 50.0,
    f_high: float = 350.0,
) -> dict[str, float]:
    """Absolute deviation-from-target stats over a single band.

    Returns RMS deviation, max deviation, and the number of grid
    points in the band. This is an ABSOLUTE readout of one curve vs
    the target — it says nothing about before/after improvement on
    its own. The verify path calls this on both the pre-correction
    measured curve and the post-correction verify curve *over the
    same band* and takes the difference to get an honest measured
    delta (see `before_after_delta`); the browser renders that
    server-computed delta rather than deriving improvement itself.

    f_low default 50 Hz (not 20 Hz): the iPhone built-in mic has a
    steep ~24 dB/octave high-pass filter starting around 250 Hz
    (Apple hardware spec). Below ~50 Hz, what the mic actually
    captures is dominated by the mic's HPF + system noise floor,
    not the room. Including 20-50 Hz in the deviation summary
    produced absurd numbers (e.g. "max 56 dB deviation") that were
    iPhone-mic artifacts, not room reality, and scared users who'd
    otherwise have a perfectly fine correction. f_high stays at
    350 Hz — the same Schroeder-ish boundary the PEQ designer
    uses, above which we don't try to correct.
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
    f_high: float = 350.0,
) -> list[dict[str, Any]]:
    """Tag each contiguous in-band segment as improved or regressed.

    For the before/after visualization the browser fills the area
    between the pre-correction measured curve and the post-correction
    verify curve, coloured by whether the correction moved that region
    *toward* the target (improved) or *away* from it (regressed). This
    function computes that classification on the Pi so the browser
    only renders server-computed data.

    "Improved" means the post-correction curve is closer to the target
    than the pre-correction curve at that grid point:
    ``|after − target| < |before − target|``. Anything not strictly
    improved (moved further from target, or held at the same distance)
    is "regressed" — we do not claim improvement without evidence.

    Only the same `[f_low, f_high]` band `deviation_metrics` uses is
    tagged; the returned segments carry inclusive grid index ranges
    (`i_lo`/`i_hi`) plus their frequency bounds so the caller can slice
    the exact before/after arrays it already holds. Contiguous runs of
    the same tone are merged into one segment.

    All three curves must share the same frequency grid (they do in the
    verify path — every capture resamples onto the same log grid).

    Coupling note: tones are computed here from the RAW curves on the
    480-point log grid, but the browser draws the fill between its
    DISPLAY curves, which may be chart-smoothed. That is safe today
    because the chart's default smoothing is 'none' and its smoothing
    option preserves the grid (same length, same frequencies), so the
    `i_lo`/`i_hi` indices still address the right points. A future
    client-side change that RESAMPLES the display curves onto a
    different grid would silently break this index alignment — keep
    display transforms grid-preserving or re-map the indices.
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
    # Strict improvement only: ties and regressions both read "regressed".
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
        # A gap in the band index (shouldn't happen for a contiguous
        # mask, but be robust) or a tone flip closes the current run.
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
    f_high: float = 350.0,
) -> dict[str, Any]:
    """Honest MEASURED before/after readout over one consistent band.

    Both `before_metrics` and `after_metrics` are computed by
    `deviation_metrics` over the SAME `[f_low, f_high]` band, so the
    delta compares like with like — this is the single guard against
    the band-mismatch trap (verify used 50–350 Hz while the design's
    predicted "before" was over the strategy band, so a naive delta
    would subtract different bands). `delta.rms_db` / `delta.max_db`
    are positive when the correction reduced deviation.

    Returns the two metric dicts, the delta, and `fill_segments` for
    the before/after visualization — everything the browser needs to
    render, computed on the Pi.
    """
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
    """Normalize a magnitude response so its average dB level across
    [f_low, f_high] is 0.

    Why: a measured response has arbitrary absolute level (mic gain,
    speaker SPL, distance). What matters for filter design is the
    SHAPE relative to the target. We anchor at the 200–1000 Hz
    midband — where speaker directivity is well-controlled and the
    iPhone-mic compensation is most accurate.

    Returns the magnitude shifted so band-mean = 0 dB.
    """
    band = (freqs >= f_low) & (freqs <= f_high)
    if not band.any():
        # Fall back to the full-range mean. Shouldn't hit this in
        # practice — our resample_log range covers 20–20k.
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
    """RMS and max-absolute of ``measured - predicted``, mean-centered.

    Mean-centering makes the comparison level-offset-invariant: a uniform gain
    difference between measured and predicted (e.g. mic sensitivity, session
    volume) does not by itself read as a tracking error.
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
) -> tuple[float, float]:
    """Tracking error, excluding bins inside a deep PREDICTED notch.

    Same level-offset-invariant RMS/max as :func:`tracking_error_db`, but first
    drops any bin whose PREDICTED level sits more than ``notch_exclusion_db``
    below this band's own predicted median. Inside a deep predicted
    interference notch, the notch depth is hypersensitive to sub-dB/
    sub-degree branch differences, so depth agreement there is not a
    meaningful tracking signal — see the VERIFY comparator in
    ``jasper.active_speaker.crossover_v2_flow`` (W6.7 ruling 1, the run-7
    hardware bug where a 27.8 dB "max" tracking error was entirely a shifted
    predicted notch, not a broadband alignment problem).

    Falls back to the full band when every bin would be excluded (a
    degenerate all-notch band), so the comparator is never computed over an
    empty set.
    """
    frequencies = np.asarray(freqs, dtype=np.float64)
    measured = np.asarray(measured_db, dtype=np.float64)
    predicted = np.asarray(predicted_db, dtype=np.float64)
    if not (frequencies.ndim == measured.ndim == predicted.ndim == 1):
        raise ValueError("tracking arrays must be 1-D")
    if not (len(frequencies) == len(measured) == len(predicted)):
        raise ValueError("tracking arrays must have matching lengths")
    mask = _band_mask(frequencies, band)
    band_predicted = predicted[mask]
    band_measured = measured[mask]
    median_predicted = float(np.median(band_predicted))
    keep = band_predicted >= (median_predicted - notch_exclusion_db)
    if not np.any(keep):
        keep = np.ones_like(keep, dtype=bool)
    return _offset_invariant_rms_and_max(band_measured[keep], band_predicted[keep])
