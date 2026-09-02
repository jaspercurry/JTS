# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room or speaker: the same feature read through a ladder of gate windows.

A banked round's captures are deconvolved, gated at a ladder of window
lengths, and read pose by pose. What separates a room feature from a
loudspeaker one is **across-pose sigma that GROWS with window length**, not
sigma that is large: an azimuth-only pose cloud produces big, perfectly
window-invariant HF scatter that is pure directivity, and reading "high
sigma => room" mis-attributes it (#3495; the evidence is P1, at
``captures/recommission-day2-2026-09-01/p1-position-window/P1-REPORT.md``).

The published ``sigma_growth_ratio`` spans the feature's resolution-VALID
rungs only: below :data:`RESOLUTION_INVALID_CYCLES` the read is the window's,
not the feature's, and a ratio anchored there is set by its own tiny
denominator. Every other rung pair stays readable — ``sigma_map`` banks the
across-pose sigma on the whole analysis grid at every rung.

Which bin the ratio is about is the CALLER's choice, not an argmax: a band's
deepest median-detrended bin is not always its most window-divergent one.
``at_hz`` anchors the report on the frequency the spec verdict flagged; the
per-band worst bin is published beside it, unchanged.

Three measured hazards this module exists to not repeat (all P1):

* **The window's own bias is not small and never vanishes.** A raw long-rung
  delta conflates the window with the room, so the published delta is
  null-model corrected: the fitted notch is synthesized, injected into a real
  capture IR, and re-read through the same rungs, and its own change is
  subtracted.
* **A number without its frame does not reproduce.** One capture and one
  feature read a materially different depth under each defensible frame, so
  every result carries the frame descriptor that produced it.
* **The pose label is not the pose** (#3503) **and the phase label is not
  the program** (#3504). Poses are keyed on the full declared
  (azimuth, elevation, distance) triple, never on a seat index at an assumed
  common height; captures are bound to programs by content hash, never by
  the sidecar's declared stimulus phase, which is mislabelled on five of six
  captures of the round this instrument was built from.

One engine, two doors: :func:`sweep_round` reads a banked round directory,
:func:`sweep_features` reads captures a caller already holds. Both go through
the same ladder, so no second reader grows a window shape beside this one.

Pipeline stage: **diagnose**, offline and read-only. It plays nothing,
writes nothing but its own report, and decides nothing: the output is
evidence for an attribution argument, not a verdict, and EQ is not its
business.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.flat_spec import SPEC_BANDS
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.gating import (
    TAPER_FRACTION,
    TRUSTED_FLOOR_MULTIPLIER,
    build_gate_window,
)

from .feature_optics import (
    CENTRE_SEARCH_OCT,
    DETREND_FRACTION,
    MAGNITUDE_SMOOTH_FRACTION,
    PHASE_GATE_LEAD_MS,
    biquad_peaking,
    detrend,
    feature_q,
)
from .round_captures import PoseCapture, RoundCapturesRefused, discover_captures

SCHEMA_VERSION = 1
GENERATED_BY = "jasper.active_speaker.crossover_v2.gate_sweep"

#: The ladder, shortest first. It reaches 20 ms because the contested
#: sub-500 Hz features do not clear the cycles bars below ~12 ms at all, so a
#: (3, 5, 7) ladder cannot price the band it is being asked about (P1). Short
#: rungs test resolution validity; long rungs are a deliberate
#: room-admittance probe, and the two are never averaged.
DEFAULT_RUNGS_MS: tuple[float, ...] = (3.0, 4.0, 5.0, 7.0, 9.0, 12.0, 20.0)

#: Cycles-in-window bars. Below the invalid bar the read is not
#: resolution-valid — it is the gate's own trusted floor
#: (:data:`~jasper.audio_measurement.gating.TRUSTED_FLOOR_MULTIPLIER`) read as
#: cycles-in-window; below the grey bar it is merely doubtful. Flags, not
#: filters, on the published table — but the invalid bar does bound which
#: rungs a sensitivity may be computed across.
RESOLUTION_INVALID_CYCLES = TRUSTED_FLOOR_MULTIPLIER
RESOLUTION_GREY_CYCLES = 5.0

#: One normalisation constant per capture, from THIS band at THIS rung,
#: applied to every rung of that capture. Per-window normalisation would
#: poison exactly the cross-rung deltas this instrument publishes, by a
#: margin of the same order as the deltas themselves (P1). Deliberately NOT
#: the sibling :data:`.feature_classifier.NORMALISE_BAND_HZ` (400-8000 Hz,
#: median, per rung): 400-1200 Hz is the band that moves most with the rung,
#: and a reference must not drift with the thing it is referencing (P1).
REFERENCE_BAND_HZ = (2500.0, 8000.0)
REFERENCE_RUNG_MS = 7.0

#: Analysis grid. Deliberately NOT :func:`.feature_classifier.analysis_grid`,
#: whose floor is 300 Hz: the lowest spec band starts at 250 Hz and the
#: features under investigation sit at 358 and 441.6 Hz, so the grid has to
#: reach below the band edge it grades.
GRID_LO_HZ = 200.0
GRID_HI_HZ = 20000.0
GRID_FRACTION = 48

#: Fixed for every rung and pose, so the smoother sees the same bin density
#: at 3 ms as at 20 ms. Zero-padding is interpolation, not resolution — the
#: real resolution limit is the window, which the cycles flags carry.
N_FFT = 1 << 16

#: Length of the null model's host array, in seconds. Long enough for a
#: high-Q notch's own ringing to finish well inside it.
NULL_MODEL_HOST_S = 0.2

# --- refusals: every one names the input that was missing --------------------

REFUSE_REFERENCE_BAND_EMPTY = "gate_sweep_reference_band_empty"
REFUSE_SINGLE_POSE = "gate_sweep_single_pose"

# --- why a band has no sensitivity -------------------------------------------

NULL_INSUFFICIENT_VALID_RUNGS = "insufficient_valid_rungs"
NULL_BAND_NOT_RADIATED = "band_outside_radiated_band"
NULL_BAND_BELOW_GRID_RESOLUTION = "graded_band_narrower_than_grid"
NULL_DEGENERATE_SHORT_RUNG = "short_rung_sigma_is_zero"


def analysis_grid() -> np.ndarray:
    """Log grid at :data:`GRID_FRACTION` points per octave."""
    n = int(round(GRID_FRACTION * np.log2(GRID_HI_HZ / GRID_LO_HZ))) + 1
    return GRID_LO_HZ * 2.0 ** (np.arange(n) / GRID_FRACTION)


# --------------------------------------------------------------------------- #
# the window and the curve it produces
# --------------------------------------------------------------------------- #


def gated_segment(
    ir: np.ndarray,
    sample_rate: int,
    *,
    gate_ms: float,
    peak_idx: int,
    lead_ms: float = PHASE_GATE_LEAD_MS,
) -> tuple[np.ndarray, int]:
    """One rung's windowed segment, peak-aligned. Returns ``(segment, lead)``.

    The window is :func:`~jasper.audio_measurement.gating.build_gate_window`'s
    — the shipped gate's own shape at a forced span — with a 1.0 ms lead. The
    lead is load-bearing and measured: a zero-lead window truncates the direct
    arrival's own low-frequency pre-ringing and reads a sub-500 Hz feature
    many dB too deep (P1).
    """
    span = int(round(gate_ms * 1e-3 * sample_rate))
    lead = int(round(lead_ms * 1e-3 * sample_rate))
    start = max(0, peak_idx - lead)
    lead = peak_idx - start
    want = lead + span + 1
    segment = np.asarray(ir[start : start + want], dtype=np.float64)
    if segment.size < want:
        segment = np.pad(segment, (0, want - segment.size))
    window = build_gate_window(
        want, peak_idx=lead, span=span, taper_fraction=TAPER_FRACTION, lead=lead
    )
    return segment * window, lead


def gated_curve(
    ir: np.ndarray,
    sample_rate: int,
    *,
    gate_ms: float,
    peak_idx: int,
    grid: np.ndarray,
) -> np.ndarray:
    """A rung's smoothed magnitude, in dB on ``grid``. Not normalised."""
    segment, _ = gated_segment(ir, sample_rate, gate_ms=gate_ms, peak_idx=peak_idx)
    spectrum = np.fft.rfft(segment, n=N_FFT)
    freqs = np.fft.rfftfreq(N_FFT, d=1.0 / sample_rate)
    db = 20.0 * np.log10(np.maximum(np.abs(spectrum), 1e-15))
    keep = np.isfinite(db) & (freqs >= GRID_LO_HZ * 0.7) & (freqs <= GRID_HI_HZ * 1.3)
    smoothed = smooth_fractional_octave(
        freqs[keep], db[keep], MAGNITUDE_SMOOTH_FRACTION
    )
    return np.interp(grid, freqs[keep], smoothed)


def _band_mean_db(
    curve: np.ndarray, grid: np.ndarray, band: tuple[float, float]
) -> float:
    mask = (grid >= band[0]) & (grid <= band[1])
    return float(np.mean(curve[mask])) if mask.any() else float("nan")


def _intersect(
    a: tuple[float, float], b: tuple[float, float]
) -> tuple[float, float] | None:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo < hi else None


@dataclass(frozen=True)
class SweepCurves:
    """One capture read through the whole ladder — this sweep's own scratch.

    Held beside the capture rather than written onto it: the loader's
    :class:`~.round_captures.PoseCapture` is data, and two readers of one
    round must never see each other's derived curves.
    """

    capture: PoseCapture
    reference_const_db: float
    #: Normalised dB on the analysis grid, keyed by rung in ms.
    curves: dict[float, np.ndarray]
    #: The same curves with their one-octave broad tilt removed.
    detrended: dict[float, np.ndarray]


def _read_curves(
    captures: Sequence[PoseCapture], grid: np.ndarray, rungs_ms: Sequence[float]
) -> tuple[SweepCurves, ...]:
    """Every capture's normalised and detrended curves, in capture order."""
    reads: list[SweepCurves] = []
    for capture in captures:
        reference_band = _intersect(REFERENCE_BAND_HZ, capture.radiated_band_hz)
        if reference_band is None:
            raise RoundCapturesRefused(
                REFUSE_REFERENCE_BAND_EMPTY,
                {
                    "capture": capture.capture_id,
                    "radiated_band_hz": list(capture.radiated_band_hz),
                    "reference_band_hz": list(REFERENCE_BAND_HZ),
                    "note": "the reference band and the radiated band do not overlap",
                },
            )
        # The reference rung is computed whether or not it is a requested
        # rung: the constant has to be the same one on every ladder, or two
        # runs of this tool are not comparable.
        reference_curve = gated_curve(
            capture.ir,
            capture.sample_rate,
            gate_ms=REFERENCE_RUNG_MS,
            peak_idx=capture.peak_idx,
            grid=grid,
        )
        reference_const_db = _band_mean_db(reference_curve, grid, reference_band)
        curves: dict[float, np.ndarray] = {}
        detrended: dict[float, np.ndarray] = {}
        for rung in rungs_ms:
            raw = (
                reference_curve
                if rung == REFERENCE_RUNG_MS
                else gated_curve(
                    capture.ir,
                    capture.sample_rate,
                    gate_ms=rung,
                    peak_idx=capture.peak_idx,
                    grid=grid,
                )
            )
            curves[rung] = raw - reference_const_db
            detrended[rung] = detrend(curves[rung], grid)
        reads.append(
            SweepCurves(
                capture=capture,
                reference_const_db=reference_const_db,
                curves=curves,
                detrended=detrended,
            )
        )
    return tuple(reads)


# --------------------------------------------------------------------------- #
# the null model: what the WINDOW alone does to a feature of this shape
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NotchFit:
    """The band's worst feature, fitted across poses at the longest rung."""

    centre_hz: float
    depth_db: float
    q: float
    per_pose_centre_hz: tuple[float, ...]
    per_pose_depth_db: tuple[float, ...]
    per_pose_q: tuple[float, ...]


def fit_notch(
    reads: Sequence[SweepCurves],
    grid: np.ndarray,
    *,
    rung_ms: float,
    nominal_hz: float,
) -> NotchFit:
    """Centre, depth and Q of the feature at ``nominal_hz``, median of poses.

    The centre is searched over +/-:data:`.feature_optics.CENTRE_SEARCH_OCT`
    (1/6 octave), NOT the classifier's 1/3-octave neighbourhood: the wider
    span walked off onto a neighbouring feature on half the poses and fitted
    the null model to the wrong thing (P1).
    """
    lo = nominal_hz * 2.0**-CENTRE_SEARCH_OCT
    hi = nominal_hz * 2.0**CENTRE_SEARCH_OCT
    mask = (grid >= lo) & (grid <= hi)
    centres: list[float] = []
    depths: list[float] = []
    qs: list[float] = []
    for read in reads:
        curve = read.detrended[rung_ms]
        index = int(np.argmax(np.abs(curve[mask])))
        centre = float(grid[mask][index])
        centres.append(centre)
        depths.append(float(curve[mask][index]))
        qs.append(feature_q(curve, grid, centre))
    return NotchFit(
        centre_hz=float(np.median(centres)),
        depth_db=float(np.median(depths)),
        q=float(np.median(qs)),
        per_pose_centre_hz=tuple(centres),
        per_pose_depth_db=tuple(depths),
        per_pose_q=tuple(qs),
    )


def null_model_hosts(
    capture: PoseCapture, *, host_rung_ms: float
) -> dict[str, tuple[np.ndarray, int]]:
    """The two hosts a synthesized notch is injected into, as ``(ir, peak)``.

    ``real`` is the capture's own IR pre-gated to ``host_rung_ms`` — the
    host the published correction uses, because a real IR carries the
    reflections and the noise floor a window actually acts on.

    ``synthetic`` is a bare impulse, and it is disclosed beside the real one
    for a measured reason: injecting a feature into a host that ALREADY has
    one at that frequency stops being additive once the pair is deep, and
    the two hosts then disagree (P1 reports both for the same reason).
    A reader who sees them agree knows the correction is in its regime.
    """
    rate = capture.sample_rate
    segment, lead = gated_segment(
        capture.ir, rate, gate_ms=host_rung_ms, peak_idx=capture.peak_idx
    )
    n = max(int(NULL_MODEL_HOST_S * rate), segment.size + 1)
    real = np.zeros(n, dtype=np.float64)
    real[: segment.size] = segment
    synthetic = np.zeros(n, dtype=np.float64)
    synthetic[lead] = 1.0
    return {"real": (real, lead), "synthetic": (synthetic, lead)}


def window_bias_db(
    host: np.ndarray,
    sample_rate: int,
    *,
    peak_idx: int,
    grid: np.ndarray,
    fit: NotchFit,
    rungs_ms: Sequence[float],
) -> dict[float, float]:
    """How much of a feature of ``fit``'s shape each rung's WINDOW invents.

    The fitted notch is synthesized as a minimum-phase RBJ peaking section
    (:func:`.feature_optics.biquad_peaking`), injected into ``host``, and
    re-read at every rung. Each read is with-notch minus without-notch
    through identical windows, so the host's own structure cancels and what
    is left is the window's doing.
    """
    from scipy.signal import lfilter

    b, a = biquad_peaking(fit.centre_hz, fit.depth_db, fit.q, sample_rate)
    clean = np.asarray(host, dtype=np.float64)
    notched = np.asarray(lfilter(b, a, clean), dtype=np.float64)

    bias: dict[float, float] = {}
    for rung in rungs_ms:
        with_notch = gated_curve(
            notched, sample_rate, gate_ms=rung, peak_idx=peak_idx, grid=grid
        )
        without = gated_curve(
            clean, sample_rate, gate_ms=rung, peak_idx=peak_idx, grid=grid
        )
        read_with = float(np.interp(fit.centre_hz, grid, detrend(with_notch, grid)))
        read_without = float(np.interp(fit.centre_hz, grid, detrend(without, grid)))
        bias[rung] = read_with - read_without
    return bias


# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #


def _cycles(freq_hz: float, rung_ms: float) -> float:
    return freq_hz * rung_ms * 1e-3


def _resolution(freq_hz: float, rung_ms: float) -> str:
    cycles = _cycles(freq_hz, rung_ms)
    if cycles < RESOLUTION_INVALID_CYCLES:
        return "invalid"
    if cycles < RESOLUTION_GREY_CYCLES:
        return "grey"
    return "ok"


def _across_pose_sigma(
    reads: Sequence[SweepCurves], rungs_ms: Sequence[float]
) -> dict[float, np.ndarray]:
    """Across-pose standard deviation of the normalised curves, per rung.

    On the NORMALISED curve, not the detrended one: one constant per capture
    means the spread is the poses disagreeing, and nothing else.
    """
    return {
        rung: np.std(np.array([read.curves[rung] for read in reads]), axis=0, ddof=1)
        for rung in rungs_ms
    }


def _feature_result(
    reads: Sequence[SweepCurves],
    grid: np.ndarray,
    sigma: Mapping[float, np.ndarray],
    hz: float,
    *,
    rungs_ms: Sequence[float],
) -> dict[str, Any]:
    """One bin read through the whole ladder: table, poses, null model.

    ``hz`` is snapped to the nearest analysis-grid bin and the snapped
    ``bin_hz`` is what every number below is read at.
    """
    grid_index = int(np.argmin(np.abs(grid - float(hz))))
    bin_hz = float(grid[grid_index])
    valid = [rung for rung in rungs_ms if _resolution(bin_hz, rung) != "invalid"]
    bin_sigma = {rung: float(sigma[rung][grid_index]) for rung in rungs_ms}
    result: dict[str, Any] = {
        "bin_hz": bin_hz,
        "cycles_by_rung": {_key(r): _cycles(bin_hz, r) for r in rungs_ms},
        "resolution_by_rung": {_key(r): _resolution(bin_hz, r) for r in rungs_ms},
        "sigma_db_by_rung": {_key(r): bin_sigma[r] for r in rungs_ms},
        "n_valid_rungs": len(valid),
        "valid_rungs_ms": list(valid),
        "poses": [
            {
                "pose_key": read.capture.pose_key,
                "capture_id": read.capture.capture_id,
                "azimuth_deg": read.capture.azimuth_deg,
                "vertical_deg": read.capture.vertical_deg,
                "mark_distance_m": read.capture.mark_distance_m,
                "value_db_by_rung": {
                    _key(r): float(read.curves[r][grid_index]) for r in rungs_ms
                },
                "detrended_db_by_rung": {
                    _key(r): float(read.detrended[r][grid_index]) for r in rungs_ms
                },
            }
            for read in reads
        ],
    }
    if len(valid) < 2:
        result["sensitivity"] = None
        result["sensitivity_null_reason"] = NULL_INSUFFICIENT_VALID_RUNGS
        return result

    short, long_ = min(valid), max(valid)
    if bin_sigma[short] <= 0.0:
        result["sensitivity"] = None
        result["sensitivity_null_reason"] = NULL_DEGENERATE_SHORT_RUNG
        return result

    fit = fit_notch(reads, grid, rung_ms=long_, nominal_hz=bin_hz)
    host = reads[0].capture
    hosts = null_model_hosts(host, host_rung_ms=long_)
    bias_by_host = {
        name: window_bias_db(
            ir,
            host.sample_rate,
            peak_idx=peak,
            grid=grid,
            fit=fit,
            rungs_ms=(short, long_),
        )
        for name, (ir, peak) in hosts.items()
    }
    bias = bias_by_host["real"]
    raw_delta = float(
        np.median([read.detrended[long_][grid_index] for read in reads])
        - np.median([read.detrended[short][grid_index] for read in reads])
    )
    bias_delta = bias[long_] - bias[short]
    synthetic = bias_by_host["synthetic"]
    synthetic_delta = synthetic[long_] - synthetic[short]
    result["sensitivity"] = {
        "shortest_valid_rung_ms": short,
        "longest_valid_rung_ms": long_,
        "sigma_growth_ratio": bin_sigma[long_] / bin_sigma[short],
        "raw_delta_db": raw_delta,
        "bias_delta_db": bias_delta,
        "corrected_delta_db": raw_delta - bias_delta,
        # The same bias through a bare-impulse host. It corrects nothing —
        # it is the disclosure that says whether the real host was still
        # additive at this depth (see :func:`null_model_hosts`).
        "bias_delta_synthetic_host_db": synthetic_delta,
        "null_model": {
            "centre_hz": fit.centre_hz,
            "depth_db": fit.depth_db,
            "q": fit.q,
            "per_pose_centre_hz": list(fit.per_pose_centre_hz),
            "per_pose_depth_db": list(fit.per_pose_depth_db),
            "per_pose_q": list(fit.per_pose_q),
            "host_capture_id": host.capture_id,
            "read_db_by_rung": {_key(r): bias[r] for r in (short, long_)},
            "synthetic_host_read_db_by_rung": {
                _key(r): synthetic[r] for r in (short, long_)
            },
        },
    }
    result["sensitivity_null_reason"] = None
    return result


def _band_result(
    reads: Sequence[SweepCurves],
    grid: np.ndarray,
    sigma: Mapping[float, np.ndarray],
    band: tuple[float, float, float],
    *,
    rungs_ms: Sequence[float],
) -> dict[str, Any]:
    lo_hz, hi_hz, tolerance_db = band
    radiated = (
        max(read.capture.radiated_band_hz[0] for read in reads),
        min(read.capture.radiated_band_hz[1] for read in reads),
    )
    graded = _intersect((lo_hz, hi_hz), radiated)
    result: dict[str, Any] = {
        "band_hz": [lo_hz, hi_hz],
        "tolerance_db": tolerance_db,
        "graded_band_hz": list(graded) if graded else None,
        "radiated_band_hz": list(radiated),
    }
    if graded is None:
        result["sensitivity"] = None
        result["sensitivity_null_reason"] = NULL_BAND_NOT_RADIATED
        return result

    mask = (grid >= graded[0]) & (grid < graded[1])
    if not mask.any():
        # A graded band narrower than one grid step is non-empty as a span and
        # empty as a set of bins. There is nothing to be worst.
        result["sensitivity"] = None
        result["sensitivity_null_reason"] = NULL_BAND_BELOW_GRID_RESOLUTION
        return result

    longest = max(rungs_ms)
    median_detrended = np.median(
        np.array([read.detrended[longest] for read in reads]), axis=0
    )
    worst_index = int(np.argmax(np.abs(median_detrended[mask])))
    worst_hz = float(grid[mask][worst_index])

    feature = _feature_result(reads, grid, sigma, worst_hz, rungs_ms=rungs_ms)
    result["worst_bin_hz"] = feature.pop("bin_hz")
    # The band's deepest feature is not always its most window-divergent one
    # (P1), so the whole band's mean sigma is published beside the worst
    # bin's — and a caller that already knows which bin it is asking about
    # names it with ``at_hz`` instead. The mean pools every graded bin at
    # every rung, including bins below their own resolution floor at the
    # short rungs: that is P1's statistic, and the frame's resolution bars
    # price it. No ratio is published for it — a ratio of two sigmas over
    # hundreds of bins is set by its smallest denominator, not by the room.
    result["band_mean_sigma_db_by_rung"] = {
        _key(r): float(np.mean(sigma[r][mask])) for r in rungs_ms
    }
    result.update(feature)
    return result


def _key(rung_ms: float) -> str:
    return f"{rung_ms:g}"


def frame_descriptor(rungs_ms: Sequence[float], grid: np.ndarray) -> dict[str, Any]:
    """The frame every number in this report is stated in (#3495).

    Same capture, same feature, four defensible frames, four different depths
    (P1). A sensitivity without its frame is the frame's number, not the
    room's.
    """
    return {
        "window": {
            "owner": "jasper.audio_measurement.gating.build_gate_window",
            "kind": "rect_head_flat_plateau_half_hann_tail",
            "taper_fraction": TAPER_FRACTION,
            "lead_ms": PHASE_GATE_LEAD_MS,
            "lead_shape": "raised_cosine",
        },
        "rungs_ms": list(rungs_ms),
        "smoothing": {
            "magnitude_fraction": MAGNITUDE_SMOOTH_FRACTION,
            "detrend_fraction": DETREND_FRACTION,
            "kind": "power_mean_fractional_octave",
        },
        "grid": {
            "lo_hz": GRID_LO_HZ,
            "hi_hz": GRID_HI_HZ,
            "fraction": GRID_FRACTION,
            "points": int(grid.size),
        },
        "n_fft": N_FFT,
        "reference": {
            "policy": "one constant per capture, applied to every rung",
            "band_hz": list(REFERENCE_BAND_HZ),
            "rung_ms": REFERENCE_RUNG_MS,
            "statistic": "arithmetic mean of the smoothed dB curve",
            "intersected_with_radiated_band": True,
        },
        "deconvolution": "jasper.audio_measurement.deconv.regularized_deconvolution_full",
        "direct_peak": "integer argmax(|ir|)",
        "centre_search_oct": CENTRE_SEARCH_OCT,
        "resolution_bars_cycles": {
            "invalid_below": RESOLUTION_INVALID_CYCLES,
            "grey_below": RESOLUTION_GREY_CYCLES,
        },
    }


def _spec_band_of(hz: float) -> list[float] | None:
    """The spec band ``hz`` falls in, on the table's own edge rule."""
    for lo_hz, hi_hz, _tolerance in SPEC_BANDS:
        if lo_hz <= hz < hi_hz:
            return [lo_hz, hi_hz]
    return None


def _feature_at(
    reads: Sequence[SweepCurves],
    grid: np.ndarray,
    sigma: Mapping[float, np.ndarray],
    hz: float,
    *,
    rungs_ms: Sequence[float],
) -> dict[str, Any]:
    result = _feature_result(reads, grid, sigma, hz, rungs_ms=rungs_ms)
    return {
        "requested_hz": hz,
        "band_hz": _spec_band_of(result["bin_hz"]),
        **result,
    }


def _sigma_map(
    grid: np.ndarray, sigma: Mapping[float, np.ndarray], rungs_ms: Sequence[float]
) -> dict[str, Any]:
    """The whole across-pose sigma surface, so no reader has to re-run this.

    P1's artifact: any bin's growth across any pair of rungs — including
    pairs the published ``sensitivity`` refuses as resolution-invalid — is a
    subtraction away once this is banked.
    """
    return {
        "grid_hz": np.round(grid, 3).tolist(),
        "sigma_db_by_rung": {
            _key(rung): np.round(sigma[rung], 3).tolist() for rung in rungs_ms
        },
    }


def _validated(
    rungs_ms: Sequence[float], at_hz: Sequence[float]
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The ladder sorted and the requested bins bounds-checked, for any door."""
    rungs = tuple(sorted(float(rung) for rung in rungs_ms))
    if len(rungs) < 2:
        raise ValueError("a gate sweep needs at least two rungs")
    wanted = tuple(float(hz) for hz in at_hz)
    for hz in wanted:
        if not GRID_LO_HZ <= hz <= GRID_HI_HZ:
            raise ValueError(
                f"{hz:g} Hz is outside the analysis grid "
                f"({GRID_LO_HZ:g}-{GRID_HI_HZ:g} Hz)"
            )
    return rungs, wanted


def _prepare(
    captures: Sequence[PoseCapture], rungs_ms: Sequence[float]
) -> tuple[np.ndarray, tuple[SweepCurves, ...], dict[float, np.ndarray]]:
    """The grid, every capture's curves on it, and the across-pose sigma.

    The whole ladder, computed once. Both doors go through here so neither
    can grow a window shape, a grid or a normalisation the other does not
    have.
    """
    if len(captures) < 2:
        raise RoundCapturesRefused(
            REFUSE_SINGLE_POSE,
            {
                "captures": [cap.capture_id for cap in captures],
                "note": "across-pose sigma needs at least two poses",
            },
        )
    grid = analysis_grid()
    reads = _read_curves(captures, grid, rungs_ms)
    return grid, reads, _across_pose_sigma(reads, rungs_ms)


def sweep_features(
    captures: Sequence[PoseCapture],
    *,
    rungs_ms: Sequence[float] = DEFAULT_RUNGS_MS,
    at_hz: Sequence[float],
) -> list[dict[str, Any]]:
    """Named bins read through the ladder, from captures already in memory.

    :func:`sweep_round`'s ``features`` block, for a caller that holds its own
    deconvolved captures rather than a banked round directory. Same window,
    same grid, same normalisation, same null model — one engine, two doors.

    Computes from ``capture_id``, ``radiated_band_hz``, ``sample_rate``,
    ``ir`` and ``peak_idx`` alone, and echoes the declared pose into each
    ``poses`` row; ``wav``, ``program``, ``program_sha256`` and ``phase`` are
    never read, so a caller with none passes ``None``. Numbers banked from
    here need :func:`frame_descriptor`'s block beside them, exactly as the
    round door's do.

    Raises :class:`~.round_captures.RoundCapturesRefused` on fewer than two
    poses, :exc:`ValueError` on an unusable ladder or an off-grid bin.
    """
    rungs, wanted = _validated(rungs_ms, at_hz)
    grid, reads, sigma = _prepare(captures, rungs)
    return [_feature_at(reads, grid, sigma, hz, rungs_ms=rungs) for hz in wanted]


def sweep_round(
    round_dir: Path,
    *,
    rungs_ms: Sequence[float] = DEFAULT_RUNGS_MS,
    at_hz: Sequence[float] = (),
) -> dict[str, Any]:
    """Sweep one banked round's gate and report what moved with the window.

    ``at_hz`` names the bins the caller already cares about — the spec
    verdict's worst bin, typically, which is not in general the band's own
    deepest one. Each is read exactly as a band's worst bin is, null model
    included, and reported under ``features``.

    Raises :class:`RoundCapturesRefused` naming the missing input.
    """
    rungs, wanted = _validated(rungs_ms, at_hz)
    captures = discover_captures(Path(round_dir))
    grid, reads, sigma = _prepare(captures, rungs)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": GENERATED_BY,
        "round_dir": str(Path(round_dir)),
        "frame": frame_descriptor(rungs, grid),
        "poses": [
            {
                "pose_key": read.capture.pose_key,
                "capture_id": read.capture.capture_id,
                "phase": read.capture.phase,
                "azimuth_deg": read.capture.azimuth_deg,
                "vertical_deg": read.capture.vertical_deg,
                "mark_distance_m": read.capture.mark_distance_m,
                "capture_wav": read.capture.wav.name if read.capture.wav else None,
                "program_wav": (
                    read.capture.program.name if read.capture.program else None
                ),
                "program_sha256_12": read.capture.program_sha256[:12],
                "sample_rate_hz": read.capture.sample_rate,
                "direct_peak_ms": (
                    1000.0 * read.capture.peak_idx / read.capture.sample_rate
                ),
                "reference_const_db": read.reference_const_db,
                "radiated_band_hz": list(read.capture.radiated_band_hz),
            }
            for read in reads
        ],
        "bands": [
            _band_result(reads, grid, sigma, band, rungs_ms=rungs)
            for band in SPEC_BANDS
        ],
        "features": [
            _feature_at(reads, grid, sigma, hz, rungs_ms=rungs) for hz in wanted
        ],
        "sigma_map": _sigma_map(grid, sigma, rungs),
    }
