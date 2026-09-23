# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-driver gated response, tweeter/woofer alignment and the crossover candidate."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Mapping, TYPE_CHECKING

import numpy as np
from scipy.optimize import minimize_scalar

from jasper.audio_measurement import analysis, deconv, gate_disclosure, gating, snr_policy
from jasper.audio_measurement.comparison_bands import (
    branch_snr_band_hz,
    OVERLAP_OCTAVE_RATIO,
)
from jasper.audio_measurement.program import (
    DEFAULT_VERIFY_TAIL_S,
    ProgramSegment,
    segment_stimulus,
)
from jasper.audio_measurement.quality_model import DRIVER
from jasper.log_event import log_event
from .model import (
    ALIGNMENT_ESTIMATED_FLAT_SUM,
    ALIGNMENT_COMMITTED_SUMMED_FIT, ALIGNMENT_SAVED_TIMING, TIMING_NEEDS_MEASUREMENT,
    ALIGNMENT_FLAT_MINIMUM_EPSILON_DB,
    ALIGNMENT_FLATNESS_MAX_STEPS,
    ALIGNMENT_FLATNESS_SPAN_PERIODS,
    ALIGNMENT_FLATNESS_STEP_US,
    AppliedAlignment,
    DECONV_PRE_GUARD_S,
    DRIVER_SNR_ALIGNMENT_KEY,
    DriverResponse,
    RecordedImpulse,
    _FLAT_SUM_POLARITY_OBJECTIVES,
    IR_POST_MS,
    IR_PRE_MS,
    logger,
    SummedAlignmentReference,
    VERIFY_TRACKING_SMOOTHING_FRACTION,
    RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB,
    RIPPLE_TRIM_MAX_DB,
    RIPPLE_TRIM_MIN_DB,
    RIPPLE_TRIM_SEARCH_STEP_DB,
    RIPPLE_TRIM_SEARCH_WINDOW_DB,
)
from .signals import _band_average_db, _complex_tf

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve


def _deconvolve_window(
    capture: np.ndarray,
    segment: ProgramSegment,
    anchor: int,
    sample_rate: int,
    *,
    epsilon: float = 0.0,
    pre_guard_s: float = DECONV_PRE_GUARD_S,
    tail_s: float = DEFAULT_VERIFY_TAIL_S,
) -> tuple[np.ndarray, int]:
    """Deconvolve one sweep → ``(full_ir, pre_guard_samples)``.

    The window starts ``pre_guard_samples`` before ``anchor`` (the scheduled
    capture position ``global_offset + start``), so it fully contains the sweep
    even though the global offset folds in the first driver's small acoustic
    delay. With a shared anchor + pre-guard across drivers, each deconvolved IR's
    direct peak lands at ``pre_guard_samples`` ± the relative delay.

    ``epsilon`` divides the measured clock drift out (design §3.1): the captured
    sweep is stretched by ``(1+ε)``, so the reference is resampled to match
    before inversion — keeping the deconvolution sharp (and the delay estimate
    accurate) under drift instead of smearing the IR.
    """
    stim = segment_stimulus(segment)
    if epsilon != 0.0:
        from scipy.signal import resample

        stretched_len = int(round(stim.size * (1.0 + epsilon)))
        if stretched_len > 0:
            stim = resample(np.asarray(stim, dtype=np.float64), stretched_len)
    pre = int(round(pre_guard_s * sample_rate))
    tail = int(round(tail_s * sample_rate))
    window_start = anchor - pre
    lo = max(0, window_start)
    pre_effective = anchor - lo  # shrinks if the window clamps at the capture head
    hi = min(capture.size, anchor + segment.n_samples + tail)
    window = np.asarray(capture[lo:hi], dtype=np.float64)
    if window.size < stim.size:
        raise ValueError(f"deconvolution window for {segment.segment_id!r} too short")
    full_ir = deconv.regularized_deconvolution_full(
        window, np.asarray(stim, dtype=np.float64), sample_rate
    )
    return full_ir, pre_effective


def recorded_impulse(
    full_ir: np.ndarray,
    origin_index: int,
    segment: ProgramSegment,
    sample_rate: int,
    *,
    clock_shift_samples: float = 0.0,
) -> RecordedImpulse:
    """The part of one deconvolved sweep a later reader can use.

    Kept through :data:`DEFAULT_VERIFY_TAIL_S` past the direct peak: the
    recording ends that long after the sweep, so the highest frequencies hold
    no decay beyond it. Everything before the peak is kept, the deconvolution
    pre-guard included, as the noise a reader measures the peak against.
    """
    peak = int(np.argmax(np.abs(full_ir)))
    end = min(full_ir.size, peak + int(round(DEFAULT_VERIFY_TAIL_S * sample_rate)) + 1)
    return RecordedImpulse(
        samples=np.asarray(full_ir[:end], dtype=np.float32),
        sample_rate_hz=int(sample_rate),
        origin_index=int(origin_index),
        peak_index=peak,
        segment_id=segment.segment_id,
        clock_shift_samples=float(clock_shift_samples),
    )


def _gate_floor_hz(fragment: Mapping[str, Any]) -> float | None:
    """Validity floor from a gate fragment, or ``None`` when ungateable.

    Shared by every caller that windows an IR through
    :func:`gating.gate_impulse_response`: ``floor_source is None`` means the
    IR was never gated (silent/NaN capture, no room to search), so the
    fragment's ``f_valid_floor_hz`` key is not a real floor even though it's
    present — mirrors :mod:`gating`'s own ``applied`` rule.
    """
    if fragment.get("floor_source") is None:
        return None
    floor = fragment.get("f_valid_floor_hz")
    return float(floor) if isinstance(floor, (int, float)) else None


def _radiated_band_hz(segment: Any) -> tuple[float, float] | None:
    """The band a sweep segment actually drove, for the gate's disclosure.

    A call-site seam, not policy: the band POLICY (intersecting this with
    the caller's gate floor) belongs to
    :func:`jasper.audio_measurement.gate_disclosure.evaluation_band_hz`.
    This only reads what the excitation program already declares, and
    returns ``None`` for a segment that declares no sweep bounds so the
    delta is omitted rather than computed over a made-up band.
    """
    lo, hi = getattr(segment, "f1_hz", None), getattr(segment, "f2_hz", None)
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return None
    lo, hi = float(lo), float(hi)
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo >= hi:
        return None
    return lo, hi


def _raw_sweep_segment(
    capture: np.ndarray, segment: ProgramSegment, anchor: int,
) -> np.ndarray:
    """The raw captured samples of one sweep segment, at the SAME schedule
    anchor :func:`_deconvolve_window` uses.

    Deliberately the scheduled window rather than the located one: the SNR
    verdict describes the response this anchor produced, so a level read
    somewhere else would be describing a different capture. Clamped to the
    capture, so a short recording yields a short (or empty) segment instead
    of raising — the SNR verdict is diagnostic, and the locator is what
    fails a truncated capture.

    The ``max(lo, ...)`` in the stop is load-bearing: without it, an ``anchor``
    far enough before the capture that ``anchor + n_samples`` lands in
    ``(-capture.size, 0)`` gives a NEGATIVE stop, which numpy reads as an
    offset from the END — so the function would return a non-empty slice of
    some other part of the recording and the SNR verdict would state a
    confident number about audio this sweep never played. No production caller
    reaches that (every anchor is ``global_offset + segment.start_sample``,
    both non-negative), so this guards the contract rather than a live path.
    """
    lo = max(0, anchor)
    hi = min(capture.size, max(lo, anchor + segment.n_samples))
    return np.asarray(capture[lo:hi], dtype=np.float64)


def _driver_snr_block(
    *,
    ambient_report: Mapping[str, Any] | None,
    fc_hz: float | None,
    freqs: np.ndarray,
    mag_db: np.ndarray,
    capture_segment: np.ndarray | None,
    sample_rate: int,
    radiated_band_hz: tuple[float, float] | None,
    alignment_band_hz: tuple[float, float] | None = None,
) -> dict[str, Any] | None:
    """The per-driver magnitude SNR verdict, read in ONE domain.

    Both sides of an SNR subtraction must be the same quantity: a
    ``"deconvolved"`` noise report pairs with deconvolved transfer-function
    levels; a ``"raw"`` report (every report a v2 CHECK hands forward)
    pairs with the RAW captured sweep's band levels. This matches
    :func:`_solve_role_gain`'s own RAW-domain aim, so a raw-with-raw verdict
    reads back the exact quantity the solve targeted. Mixing domains is
    unsafe: on synthetic fixtures a MEASURE played 20 dB quieter reported
    the SAME worst-band SNR in the cross-domain reading (deconvolution
    cancels drive level, the room's dBFS floor does not) while the
    same-domain reading fell the full 20 dB.

    ``window="rectangular"`` because a sweep is non-stationary (a Hann
    window re-weights by WHEN frequencies occur); the padded-capture
    duty-cycle offset that makes rectangular unsafe elsewhere is zero here
    since :func:`_raw_sweep_segment` hands exactly ``segment.n_samples``.

    ``radiated_band_hz`` scopes the verdict to the band this branch's
    stimulus actually drove (:func:`~..comparison_bands.branch_snr_band_hz`).
    Fails closed: a raw report with no captured segment, or a branch with
    no corner/radiated band, produces no verdict at all. A present-but-degenerate
    segment instead yields ``verdict: "unknown"`` with an empty band list —
    absent means "no evidence offered", unknown means "evidence was
    unusable".
    """
    if ambient_report is None:
        return None
    relevant_hz = branch_snr_band_hz(fc_hz, radiated_band_hz)
    if relevant_hz is None:
        return None
    noise_domain, noise_bands = snr_policy.unwrap_noise_report(ambient_report)
    if noise_domain == "deconvolved":
        capture_bands = snr_policy.magnitude_band_levels(freqs, mag_db)
        band_method = "deconvolved_band_difference"
    elif capture_segment is not None:
        capture_bands = snr_policy.band_levels_dbfs(
            capture_segment,
            sample_rate,
            snr_policy.CROSSOVER_SNR_BANDS_HZ,
            window="rectangular",
        )
        band_method = "fft_band_power_difference"
    else:
        return None
    block = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture_bands,
        noise_bands=noise_bands,
        noise_floor_dbfs_scalar=None,
        relevant_hz=relevant_hz,
        model=DRIVER,
        band_method=band_method,
        ladder="crossover_snr",
    )
    # TWO decision classes off ONE set of measurements: the magnitude
    # verdict above grades ok/reduced/insufficient around 25/20 dB; a
    # POLARITY/DELAY decision is held to the stricter 35 dB
    # `DRIVER.alignment_snr_ok_db` (no reduced rung) — reusing the magnitude
    # verdict would ship an unrefused polarity read off an unusable capture.
    block[DRIVER_SNR_ALIGNMENT_KEY] = snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_ALIGNMENT,
        capture_bands=capture_bands,
        noise_bands=noise_bands,
        noise_floor_dbfs_scalar=None,
        relevant_hz=alignment_band_hz or relevant_hz,
        model=DRIVER,
        band_method=band_method,
        ladder="crossover_snr",
    )
    return block


def _driver_response(
    role: str,
    full_ir: np.ndarray,
    sample_rate: int,
    *,
    calibration: "CalibrationCurve | None",
    ambient_report: Mapping[str, Any] | None,
    fc_hz: float | None,
    n_fft: int,
    radiated_band_hz: tuple[float, float] | None = None,
    capture_segment: np.ndarray | None = None,
    gate_exempt_reason: str | None = None,
    preserve_timing: bool = False,
    alignment_band_hz: tuple[float, float] | None = None,
) -> DriverResponse:
    """One role's gated, calibrated response plus the gate's own disclosure.

    ``gate_exempt_reason`` keeps the room in: the response is the arrival
    window ungated, with :func:`gating.exempt_gating_block` saying why and no
    validity floor claimed (a seat take, ADR-0260).

    ``radiated_band_hz`` is the band this capture's excitation actually drove —
    the caller's segment sweep bounds. It is the ONLY input the pre/post-gate
    delta needs beyond the IR, and it is threaded from here rather than guessed
    downstream (see :mod:`jasper.audio_measurement.gate_disclosure`). Absent,
    the delta is simply not reported — never defaulted. It has a SECOND reader:
    :func:`_driver_snr_block` scopes the capture-SNR verdict to the same band,
    so a row this stimulus deliberately left empty cannot veto it
    (:func:`branch_snr_band_hz`). One declared fact, two consumers — neither
    re-derives the sweep's edges.

    ``capture_segment`` is the RAW captured samples of this role's sweep —
    the signal side of the SNR verdict whenever the noise report is a raw
    one. See :func:`_driver_snr_block` for why the verdict cannot be built
    from ``full_ir`` in that case.
    """
    peak_idx = int(np.argmax(np.abs(full_ir)))
    window = deconv.direct_arrival_window(
        full_ir, sample_rate, direct_peak_idx=peak_idx,
        pre_arrival_ms=IR_PRE_MS,
        post_arrival_ms=(1000 * DEFAULT_VERIFY_TAIL_S
                         if gate_exempt_reason == gating.SEAT_EXEMPT else IR_POST_MS),
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    if gate_exempt_reason is not None:
        gated_ir = ir
        gating_block = gating.exempt_gating_block(
            ir, sample_rate, reason=gate_exempt_reason
        )
        validity_floor_hz = None
    else:
        gated_ir, fragment = gating.gate_impulse_response(ir, sample_rate)
        delta = gate_disclosure.pre_post_gate_delta(
            ir, gated_ir, sample_rate,
            trusted_floor_hz=fragment["f_trusted_hz"],
            radiated_band_hz=radiated_band_hz,
        )
        gating_block = {
            "applied": fragment["floor_source"] is not None,
            "exempt_reason": None,
            **fragment,
            "pre_post_gate_delta": delta,
        }
        validity_floor_hz = _gate_floor_hz(fragment)

    freqs, H = _complex_tf(gated_ir, sample_rate, n_fft=n_fft, calibration=calibration)
    if preserve_timing:
        H = H * np.exp(-2j * np.pi * freqs * window[0] / sample_rate)
    mag_db = 20.0 * np.log10(np.maximum(np.abs(H), 1e-12))

    snr_block = _driver_snr_block(
        ambient_report=ambient_report, alignment_band_hz=alignment_band_hz,
        fc_hz=fc_hz,
        freqs=freqs,
        mag_db=mag_db,
        capture_segment=capture_segment,
        sample_rate=sample_rate,
        radiated_band_hz=radiated_band_hz,
    )
    return DriverResponse(
        role=role,
        freqs_hz=freqs,
        magnitude_db=mag_db,
        complex_tf=H,
        gating=gating_block,
        snr=snr_block,
        validity_floor_hz=validity_floor_hz,
    )


def _aligned_branch_tf(
    full_ir: np.ndarray,
    sample_rate: int,
    n_fft: int,
    *,
    calibration: "CalibrationCurve | None",
):
    """Delay-referenced, gating-consistent complex TF for the sum prediction.

    :func:`deconv.direct_arrival_window` places each branch's direct peak at
    the same fixed offset inside the window (bulk delay removed) without a
    circular roll, which would inject a spurious echo. The windowed IR then
    runs through the SAME adaptive reflection gate :func:`_driver_response`
    applies — a fixed window alone bakes a room reflection into the
    predicted sum that VERIFY's measured sum has already gated out (traced
    once to a 15 cm desk-bounce producing a spurious ~1125 Hz null).
    """
    peak_idx = int(np.argmax(np.abs(full_ir)))
    window = deconv.direct_arrival_window(
        full_ir, sample_rate, direct_peak_idx=peak_idx,
        pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS,
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    gated_ir, fragment = gating.gate_impulse_response(ir, sample_rate)
    freqs, H = _complex_tf(gated_ir, sample_rate, n_fft=n_fft, calibration=calibration)
    return freqs, H, fragment


def predicted_branch_sum(
    W: np.ndarray,
    T: np.ndarray,
    trim_w_db: float,
    trim_t_db: float,
    sign: int,
    *,
    freqs_hz: np.ndarray | None = None,
    residual_delay_us: float = 0.0,
) -> np.ndarray:
    """Return the complex branch sum in the argmax-referenced frame.

    ``_aligned_branch_tf`` independently references both direct peaks, so a
    physical applied delay must enter here only as the *residual* relative
    to that frame — passing the full applied delay would count the
    measured peak gap twice. Callers compute the residual via the ONE
    owner, :func:`summed_model_residual_delay_us`. Public because the v2
    session reuses this for its LINEARIZED-branch VERIFY prediction.
    """
    g_w = 10.0 ** (trim_w_db / 20.0)
    g_t = 10.0 ** (trim_t_db / 20.0)
    tweeter = T * g_t
    if freqs_hz is not None and residual_delay_us != 0.0:
        tweeter = tweeter * np.exp(
            -1j * 2.0 * np.pi * np.asarray(freqs_hz) * residual_delay_us * 1e-6
        )
    return W * g_w + sign * tweeter


def summed_model_residual_delay_us(
    anchor_delay_us: float | None, applied_delay_us: float
) -> float:
    """The ONE derivation of :func:`predicted_branch_sum`'s ``residual_delay_us``.

    ``(D_t - D_w) + applied_signed_delay``, expressed via the aligner's two
    numbers: ``-anchor_delay_us`` IS ``(D_t - D_w)``, so the residual is
    ``applied - anchor``, exactly ``0.0`` at the bare anchor.
    :func:`_aligned_branch_tf` already references each branch to its own
    direct peak, so phasing by the FULL applied delay would count the
    measured peak gap twice and inject a deep comb into the predicted sum.

    ``anchor_delay_us`` is ``None`` when the aligner refused the estimate
    (no trustworthy argmax-frame reference, and no delay is applied
    either), or when a capture's committed delay came from the low-SNR arm
    of :func:`_select_alignment_pair` (a disagreement between an untrusted
    anchor and a trusted applied delay, not a fact about the speaker). Both
    cases return ``0.0``: the model keeps the independently-aligned frame
    rather than fabricate a comb the emitted graph need not have — this
    feeds VERIFY's tracking reference and prediction gate, so a speaker
    that really combs still fails honestly on the absolute claim.
    """
    if anchor_delay_us is None:
        return 0.0
    return float(applied_delay_us) - float(anchor_delay_us)


def half_period_us(fc_hz: float) -> float:
    """Half of one period at ``fc_hz``, in microseconds. ONE spelling.

    The radius of the comb lobe a crossover corner owns, and therefore the
    delay-ambiguity budget: two delays further apart than this put the
    summation on adjacent lobes. Shared by ``_select_alignment_pair``'s
    ``left_anchor_lobe`` tripwire and
    :func:`jasper.active_speaker.crossover_v2.alignment_prescription.read_alignment_prescription`'s
    bound. ``fc_hz`` must be positive and finite; callers guard first.
    """
    return 0.5e6 / float(fc_hz)


def _ripple_db(freqs: np.ndarray, magnitude: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        return float("inf")
    band = magnitude[mask]
    band_db = 20.0 * np.log10(np.maximum(np.abs(band), 1e-12))
    return float(np.max(band_db) - np.min(band_db))


def polarity_label(polarity_sign: int) -> str:
    """``+1 -> "normal"``, ``-1 -> "inverted"``. The ONE spelling of the map."""
    return "normal" if polarity_sign >= 0 else "inverted"


def polarity_sign_of(polarity: str) -> int:
    """Inverse of :func:`polarity_label`; anything but ``"inverted"`` is ``+1``."""
    return -1 if polarity == "inverted" else 1


@dataclass(frozen=True)
class AlignmentPairSelection:
    """A timing read or driver-only estimate, with its evidence. See ADR-0319."""

    polarity_sign: int
    delay_us: float
    ripple_db: float | None
    seed_polarity_sign: int
    seed_delay_us: float
    seed_ripple_db: float | None
    objective: str
    left_anchor_lobe: bool = False
    #: Was the POLARITY axis pinned by the request rather than searched?
    #: The objective string can't carry this (a pinned round still commits
    #: ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION); read via
    #: :attr:`polarity_agrees_with_sum` instead.
    polarity_pinned: bool = False
    residual_rms_db: float | None = None
    margin_db: float | None = None
    repeat_spread_db: float | None = None
    repeat_spread_us: float | None = None
    repeat_count: int | None = None
    repeat_noise_db: float | None = None

    @property
    def polarity_agrees_with_sum(self) -> bool | None:
        """Did correlation's polarity answer survive the flat-sum objective?

        ``None`` on any commitment the flat-sum objective did not make on
        the polarity axis (low-SNR path, or a pinned round) — recording
        ``False`` there would report a comparison that never happened.
        """
        if self.polarity_pinned:
            return None
        if self.objective not in _FLAT_SUM_POLARITY_OBJECTIVES:
            return None
        return self.polarity_sign == self.seed_polarity_sign

    @property
    def flatness_improvement_db(self) -> float | None:
        """``seed_ripple - committed_ripple``: what the objective bought."""
        return self.seed_ripple_db - self.ripple_db if self.seed_ripple_db is not None and self.ripple_db is not None else None


def _alignment_delay_grid(fc_hz, anchor_delay_us, seed_delay_us, delay_bounds_us):
    span_us = ALIGNMENT_FLATNESS_SPAN_PERIODS * 1e6 / fc_hz
    n_steps = max(1, min(int(round(span_us / ALIGNMENT_FLATNESS_STEP_US)),
                         ALIGNMENT_FLATNESS_MAX_STEPS))
    step_us = span_us / n_steps
    grid = [anchor_delay_us + i * step_us for i in range(-n_steps, n_steps + 1)]
    if delay_bounds_us is not None:
        lo_us, hi_us = sorted(abs(float(b)) for b in delay_bounds_us)
        grid = [d for d in grid if lo_us <= abs(d) <= hi_us]
    return [*grid, seed_delay_us], step_us if grid else 0.0


def _summed_fit_comparator(freqs, W, T, reference, anchor_delay_us):
    # A 1/N-octave box has half-width 1/(2N) octaves. Retain its edge samples.
    radius = 2 ** (0.5 / VERIFY_TRACKING_SMOOTHING_FRACTION)
    lo, hi = reference.band_hz
    axis = reference.freqs_hz
    mask = (axis >= lo / radius) & (axis <= hi * radius)
    axis = axis[mask]
    measured = analysis.smooth_fractional_octave(axis, reference.magnitude_db[mask], VERIFY_TRACKING_SMOOTHING_FRACTION)
    start = max(0, np.searchsorted(freqs, axis[0]) - 1)
    stop = np.searchsorted(freqs, axis[-1], side="right") + 1
    freqs, W, T = freqs[start:stop], W[start:stop], T[start:stop]

    def score(sign, delay):
        predicted = predicted_branch_sum(
            W, T, 0.0, 0.0, sign, freqs_hz=freqs,
            residual_delay_us=summed_model_residual_delay_us(anchor_delay_us, delay),
        )
        db = 20 * np.log10(np.maximum(np.abs(predicted), 1e-12))
        smoothed = analysis.smooth_fractional_octave(
            axis, np.interp(axis, freqs, db), VERIFY_TRACKING_SMOOTHING_FRACTION,
        )
        rms, _ = analysis.tracking_error_db(axis, measured, smoothed, reference.band_hz)
        return float(rms)

    return score


def _select_summed_alignment_pair(
    freqs: np.ndarray, W: np.ndarray, T: np.ndarray, *,
    reference: SummedAlignmentReference, woofer_role: str, tweeter_role: str,
    fc_hz: float, anchor_delay_us: float, seed_delay_us: float,
    seed_polarity_sign: int, delay_bounds_us: tuple[float, float] | None,
    repeats: tuple[tuple[np.ndarray, np.ndarray], ...] = (),
    saved: AppliedAlignment | None = None,
) -> AlignmentPairSelection:
    """One confidence rule and one residual model for decision and verification."""
    scores = [_summed_fit_comparator(
        freqs, w * summed.response_by_role[woofer_role](freqs),
        t * summed.response_by_role[tweeter_role](freqs), summed, anchor_delay_us,
    ) for summed in (reference, *reference.repeat_responses) for w, t in ((W, T), *repeats)]

    def score(sign, delay):
        return float(np.sqrt(np.mean([evaluate(sign, delay) ** 2 for evaluate in scores])))

    def spread(values):
        return max(values) - min(values) if len(values) > 1 else None

    if saved is not None:
        sign, delay = polarity_sign_of(saved.polarity), saved.delay_us
        return AlignmentPairSelection(
            sign, delay, None, seed_polarity_sign, seed_delay_us, None,
            ALIGNMENT_SAVED_TIMING, residual_rms_db=score(sign, delay),
            repeat_noise_db=spread([evaluate(sign, delay) for evaluate in scores]),
            repeat_count=len(scores),
        )
    grid, step = _alignment_delay_grid(fc_hz, anchor_delay_us, seed_delay_us, delay_bounds_us)
    grid = sorted(set(grid))
    lower, upper = (0.0, math.inf) if delay_bounds_us is None else sorted(abs(float(b)) for b in delay_bounds_us)

    def fit(evaluate, sign):
        values = {d: evaluate(sign, d) for d in grid}
        center = min(values, key=values.__getitem__)
        radius = 1e6 / fc_hz
        extended = sorted(set([*grid, *np.arange(center - radius, center + radius + step / 2, step)])) if step else grid
        bracket = [d for d in extended if abs(d - center) <= radius and lower <= abs(d) <= upper]
        for d in bracket:
            if d not in values:
                values[d] = evaluate(sign, d)
        # Refine every local minimum; coarse quantization can reorder adjacent lobes.
        for i, d in enumerate(bracket):
            lo, hi = bracket[max(0, i - 1)], bracket[min(len(bracket) - 1, i + 1)]
            if (lo == hi or values[d] > min(values[lo], values[hi])
                    or values[d] == values[lo] == values[hi] or (lower > 0 and lo < 0 < hi)):
                continue
            optimum = minimize_scalar(lambda d: evaluate(sign, d), bounds=(lo, hi), method="bounded", options={"xatol": .1}).x
            for refined in (math.floor(optimum), math.ceil(optimum)):
                if lo <= refined <= hi:
                    values[refined] = evaluate(sign, refined)
        return min(values.items(), key=lambda item: item[1])

    minima = {sign: fit(score, sign) for sign in (1, -1)}
    sign = min(minima, key=lambda sign: minima[sign][1])
    delay, rms = minima[sign]
    margin = minima[-sign][1] - rms
    repeat_fits = [{polarity: fit(evaluate, polarity) for polarity in (1, -1)} for evaluate in scores]
    spread_db = spread([result[sign][1] for result in repeat_fits])
    spread_us = spread([result[sign][0] for result in repeat_fits])
    left_lobe = abs(delay - anchor_delay_us) > half_period_us(fc_hz)
    confident = spread_db is not None and margin > spread_db and not left_lobe
    return AlignmentPairSelection(
        sign, delay, None, seed_polarity_sign, seed_delay_us, None,
        ALIGNMENT_COMMITTED_SUMMED_FIT if confident else TIMING_NEEDS_MEASUREMENT,
        left_anchor_lobe=left_lobe, residual_rms_db=rms, margin_db=margin,
        repeat_spread_db=spread_db, repeat_spread_us=spread_us,
        repeat_count=len(scores),
    )


def _select_alignment_pair(
    freqs: np.ndarray,
    W: np.ndarray,
    T: np.ndarray,
    *,
    fc_hz: float,
    lo_hz: float,
    hi_hz: float,
    trim_w_db: float,
    trim_t_db: float,
    anchor_delay_us: float | None,
    seed_delay_us: float,
    seed_polarity_sign: int,
    delay_bounds_us: tuple[float, float] | None = None,
    branch_snr_insufficient: bool = False,
) -> AlignmentPairSelection | None:
    """Driver-only first estimate; never a measured timing decision."""
    band = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not np.any(band):
        return None
    freqs_band = freqs[band]
    W_band = W[band]
    T_band = T[band]

    def _ripple_at(polarity_sign: int, delay_us: float) -> float:
        summed = predicted_branch_sum(
            W_band, T_band, trim_w_db, trim_t_db, polarity_sign,
            freqs_hz=freqs_band,
            residual_delay_us=summed_model_residual_delay_us(
                anchor_delay_us, delay_us,
            ),
        )
        return _ripple_db(freqs_band, summed, lo_hz, hi_hz)

    def _left_anchor_lobe(delay_us: float) -> bool:
        """Did the commitment leave the comb lobe the anchor owns?

        Both Fc guards needed: ``not (fc_hz > 0.0)`` rejects zero/negative/NaN;
        ``isfinite`` also rejects ``+inf``, which would make
        :func:`half_period_us` return ``0.0`` and fire on every commitment.
        """
        if anchor_delay_us is None or not (fc_hz > 0.0) or not math.isfinite(fc_hz):
            return False
        return abs(delay_us - anchor_delay_us) > half_period_us(fc_hz)

    seed_ripple_db = _ripple_at(seed_polarity_sign, seed_delay_us)

    if branch_snr_insufficient:
        return None
    delays = [seed_delay_us]
    if anchor_delay_us is not None and fc_hz > 0.0:
        delays, _ = _alignment_delay_grid(fc_hz, anchor_delay_us, seed_delay_us, delay_bounds_us)
    signs = (1, -1)
    pairs = [(sign, delay) for sign in signs for delay in delays]
    # Non-finite scores are not candidates (only fires on a branch TF that
    # already carries NaN/inf).
    scored = [
        (sign, delay, ripple)
        for (sign, delay), ripple in (
            (pair, _ripple_at(*pair)) for pair in pairs
        )
        if math.isfinite(ripple)
    ]
    if not scored:
        log_event(
            logger, "program_analysis.alignment_not_scorable",
            level=logging.WARNING,
            reason="no_finite_ripple",
            fc_hz=round(float(fc_hz), 3),
            band_hz=(round(float(lo_hz), 1), round(float(hi_hz), 1)),
            candidates=len(pairs),
        )
        return None
    best_ripple = min(ripple for _s, _d, ripple in scored)
    # Flat-minimum regularization: within epsilon of the global minimum,
    # keep the seed's polarity first, then the delay closest to the seed.
    committed_sign, committed_delay_us, committed_ripple_db = min(
        (
            item for item in scored
            if item[2] <= best_ripple + ALIGNMENT_FLAT_MINIMUM_EPSILON_DB
        ),
        key=lambda item: (
            0 if item[0] == seed_polarity_sign else 1,
            abs(item[1] - seed_delay_us),
        ),
    )
    return AlignmentPairSelection(
        polarity_sign=committed_sign,
        delay_us=committed_delay_us,
        ripple_db=committed_ripple_db,
        seed_polarity_sign=seed_polarity_sign,
        seed_delay_us=seed_delay_us,
        seed_ripple_db=seed_ripple_db,
        objective=ALIGNMENT_ESTIMATED_FLAT_SUM,
        left_anchor_lobe=_left_anchor_lobe(committed_delay_us),
    )


def branch_level_bands_hz(
    fc_hz: float,
    *,
    woofer_span_hz: tuple[float, float] | None = None,
    tweeter_span_hz: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """``((woofer_lo, woofer_hi), (tweeter_lo, tweeter_hi))`` — the two
    log-symmetric half-bands the level match reads, one per branch.

    Each ``*_span_hz`` is that branch's OWN validity span (excited band,
    narrowed by any reflection-gate floor), never the shared overlap — a
    branch is read only where it played. Returns ``[Fc/rho, Fc]`` (woofer)
    and ``[Fc, Fc*rho]`` (tweeter) with the largest ``rho <=
    OVERLAP_OCTAVE_RATIO`` fitting inside BOTH spans. Both inner edges are
    load-bearing (halves meet AT Fc): a span that does not reach Fc raises,
    through the same ``internal_error`` seam as an unanalysable capture,
    never a guessed trim.

    SSOT for the band pair: :func:`solve_branch_trims` computes the levels
    and ``_build_candidate`` discloses the bands from this one derivation.
    """
    w_lo_bound, w_hi_bound = (
        woofer_span_hz
        if woofer_span_hz is not None
        else (fc_hz / OVERLAP_OCTAVE_RATIO, fc_hz)
    )
    t_lo_bound, t_hi_bound = (
        tweeter_span_hz
        if tweeter_span_hz is not None
        else (fc_hz, fc_hz * OVERLAP_OCTAVE_RATIO)
    )
    if not (w_lo_bound < fc_hz <= w_hi_bound):
        raise ValueError(
            f"woofer span [{w_lo_bound}, {w_hi_bound}] does not reach Fc={fc_hz}"
        )
    if not (t_lo_bound <= fc_hz < t_hi_bound):
        raise ValueError(
            f"tweeter span [{t_lo_bound}, {t_hi_bound}] does not reach Fc={fc_hz}"
        )
    # ratio > 1 is guaranteed by the two checks above (both quotients exceed 1).
    ratio = min(OVERLAP_OCTAVE_RATIO, fc_hz / w_lo_bound, t_hi_bound / fc_hz)
    return (fc_hz / ratio, fc_hz), (fc_hz, fc_hz * ratio)


def solve_branch_trims(
    freqs: np.ndarray,
    W: np.ndarray,
    T: np.ndarray,
    fc_hz: float,
    *,
    woofer_span_hz: tuple[float, float] | None = None,
    tweeter_span_hz: tuple[float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Level-match trims: each branch read on ITS OWN side of Fc.

    THIS IS THE LEVEL FACT: "level-matched" means matched acoustic output
    through the HANDOVER REGION — after the target filters, the two driver
    traces are equal at Fc and each sits -6 dB against the summed target
    (the Linkwitz-Riley unity condition). The linear-frequency power mean
    over the mirrored +/-1-octave halves IS that consensus statistic —
    NOT passband-average sensitivity.

    Two constraints on the statistic: with LR4 the level-error sensitivity
    concentrates AT Fc, so +/-1-octave is right; and where woofer/horn
    directivity mismatch, no single correct level exists, so the axis
    levels were read on is stated (``active_speaker.profile.LEVEL_MATCH_AXIS``).

    Each ``*_span_hz`` is that branch's own validity span (default Fc +/- 1
    octave), turned into mirrored halves by :func:`branch_level_bands_hz` —
    never the shared both-branches-excited overlap, which on a 2-way whose
    tweeter starts AT Fc measures skirt depth (an ideal LR4 pair reads
    +10.59 dB instead of 0) or dilutes the mean with never-excited noise if
    widened back to nominal. Reading each branch on its own side removes
    both problems, leaving a KNOWN +0.54 dB linear-vs-log-frequency-bin
    systematic at rho=2 (shrinking with rho), left uncorrected so it does
    not stack with the 10-13 dB this frame already moves.

    Public as the level match's SSOT — the v2 session and contract tests
    import it.
    """
    (w_lo, w_hi), (t_lo, t_hi) = branch_level_bands_hz(
        fc_hz, woofer_span_hz=woofer_span_hz, tweeter_span_hz=tweeter_span_hz,
    )
    level_w = _band_average_db(
        freqs, 20.0 * np.log10(np.maximum(np.abs(W), 1e-12)), w_lo, w_hi
    )
    level_t = _band_average_db(
        freqs, 20.0 * np.log10(np.maximum(np.abs(T), 1e-12)), t_lo, t_hi
    )
    target = min(level_w, level_t)  # attenuate the louder branch
    return target - level_w, target - level_t, level_w, level_t


def ripple_at_trim(
    freqs: np.ndarray,
    w_tf: np.ndarray,
    t_tf: np.ndarray,
    *,
    lo_hz: float,
    hi_hz: float,
    trim_w_db: float,
    trim_t_db: float,
    sign: int,
) -> float:
    """The summed pair's ripple (max-min dB) over ``[lo_hz, hi_hz]`` at ONE trim.

    The shared computation behind the trim-rejection telemetry PAIR:
    :func:`solve_ripple_optimal_trim` evaluates every scanned candidate
    through it, and the linearization planner calls it once more at the
    ANCHORED trim, so the two logged ripples differ in exactly one
    variable. Not this module's only summed-ripple site —
    :func:`_select_alignment_pair` asks a different question (which pair
    sums flatter at a fixed trim) and scores at the band-average trim to
    avoid circularity. No delay term: this is the zero-residual frame the
    scan optimizes in. ``inf`` when the band holds no bins.
    """
    return _ripple_db(
        freqs,
        predicted_branch_sum(
            w_tf, t_tf, float(trim_w_db), float(trim_t_db), int(sign),
        ),
        float(lo_hz),
        float(hi_hz),
    )


def solve_ripple_optimal_trim(
    freqs: np.ndarray,
    w_tf: np.ndarray,
    t_tf: np.ndarray,
    fc_hz: float,
    *,
    lo_hz: float | None = None,
    hi_hz: float | None = None,
    seed_trim_db: float,
    trim_w_db: float = 0.0,
    sign: int,
    window_db: float = RIPPLE_TRIM_SEARCH_WINDOW_DB,
    step_db: float = RIPPLE_TRIM_SEARCH_STEP_DB,
    flat_minimum_epsilon_db: float = RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB,
) -> tuple[float, float, float]:
    """Ripple-minimizing tweeter trim, scanned around the band-average seed
    and regularized toward that seed on a flat minimum.

    A flatness POLISH on an already-correct level (``_build_candidate``
    runs it only where the band straddles Fc). The woofer trim
    (``trim_w_db``) is held FIXED at 0.0 by default — ripple depends only
    on RELATIVE gain, so scanning one side explores the full space.

    Search window: ``seed_trim_db +/- window_db`` at ``step_db`` steps
    (default +/-10 dB / 0.1 dB), clamped to
    [:data:`RIPPLE_TRIM_MIN_DB`, :data:`RIPPLE_TRIM_MAX_DB`] so the scan
    never evaluates an unphysical candidate.

    Selection is flat-minimum-regularized: among candidates within
    ``flat_minimum_epsilon_db`` of the global minimum, the one closest to
    the seed wins — trading inaudible flatness for session-to-session
    repeatability on a shallow bowl.

    Returns ``(trim_t_db, ripple_db, seed_trim_db)``. ``lo_hz``/``hi_hz``
    default to Fc +/- 1 octave; every caller passes its own gating-clamped
    band explicitly.
    """
    lo = lo_hz if lo_hz is not None else fc_hz / OVERLAP_OCTAVE_RATIO
    hi = hi_hz if hi_hz is not None else fc_hz * OVERLAP_OCTAVE_RATIO
    band = (freqs >= lo) & (freqs <= hi)
    if not np.any(band):
        raise ValueError("overlap band has no frequency bins")
    freqs_band = freqs[band]
    w_band = w_tf[band]
    t_band = t_tf[band]

    n_steps = int(round(window_db / step_db))
    raw_candidates = [seed_trim_db + i * step_db for i in range(-n_steps, n_steps + 1)]
    candidate_trims = [
        trim for trim in raw_candidates if RIPPLE_TRIM_MIN_DB <= trim <= RIPPLE_TRIM_MAX_DB
    ]
    if not candidate_trims:
        # No physically valid value in the seed's own window: clamp the seed itself.
        candidate_trims = [min(max(seed_trim_db, RIPPLE_TRIM_MIN_DB), RIPPLE_TRIM_MAX_DB)]
    ripples_db = [
        ripple_at_trim(
            freqs_band,
            w_band,
            t_band,
            lo_hz=lo,
            hi_hz=hi,
            trim_w_db=trim_w_db,
            trim_t_db=candidate_trim,
            sign=sign,
        )
        for candidate_trim in candidate_trims
    ]

    min_ripple = min(ripples_db)
    best_trim = seed_trim_db
    best_ripple = min_ripple
    best_distance = math.inf
    for candidate_trim, ripple in zip(candidate_trims, ripples_db):
        # Flat-minimum regularization: closest-to-seed within epsilon of the
        # global minimum (see docstring); a sharp minimum has only one such candidate.
        if ripple > min_ripple + flat_minimum_epsilon_db:
            continue
        distance = abs(candidate_trim - seed_trim_db)
        if distance < best_distance:
            best_distance = distance
            best_trim = candidate_trim
            best_ripple = ripple
    return best_trim, best_ripple, seed_trim_db


def _n_fft_for(*irs: np.ndarray) -> int:
    longest = max(ir.size for ir in irs)
    return max(8192, 1 << (max(longest, 1) - 1).bit_length())
