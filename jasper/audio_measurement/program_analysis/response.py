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

from jasper.audio_measurement import deconv, gate_disclosure, gating, snr_policy
from jasper.audio_measurement.alignment import (
    _bandlimit,
    _gcc_local_peak_snap,
    gcc_phat,
    GCC_UPSAMPLE,
)
from jasper.audio_measurement.comparison_bands import (
    branch_snr_band_hz,
    overlap_band_hz,
    OVERLAP_OCTAVE_RATIO,
)
from jasper.audio_measurement.program import (
    ExcitationProgram,
    ProgramSegment,
    segment_stimulus,
)
from jasper.audio_measurement.quality_model import DRIVER
from jasper.log_event import log_event
from .model import (
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY,
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_FLAT_MINIMUM_EPSILON_DB,
    ALIGNMENT_FLATNESS_MAX_STEPS,
    ALIGNMENT_FLATNESS_SPAN_PERIODS,
    ALIGNMENT_FLATNESS_STEP_US,
    ALIGNMENT_OK,
    AlignmentEstimate,
    AppliedAlignment,
    DECONV_PRE_GUARD_S,
    DRIVER_SNR_ALIGNMENT_KEY,
    DriverResponse,
    _FLAT_SUM_POLARITY_OBJECTIVES,
    GCC_SNAP_RADIUS_PERIODS,
    IR_POST_MS,
    IR_PRE_MS,
    logger,
    MeasurementGeometry,
    MeasurementPriors,
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
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
    tail_s: float = 0.5,
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
        relevant_hz=relevant_hz,
        model=DRIVER,
        band_method=band_method,
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
) -> DriverResponse:
    """One role's gated, calibrated response plus the gate's own disclosure.

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
        pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS,
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    gated_ir, fragment = gating.gate_impulse_response(ir, sample_rate)
    applied = fragment["floor_source"] is not None
    delta = gate_disclosure.pre_post_gate_delta(
        ir, gated_ir, sample_rate,
        trusted_floor_hz=fragment["f_trusted_hz"],
        radiated_band_hz=radiated_band_hz,
    )
    gating_block = {
        "applied": applied,
        "exempt_reason": None,
        **fragment,
        "pre_post_gate_delta": delta,
    }
    validity_floor_hz = _gate_floor_hz(fragment)

    freqs, H = _complex_tf(gated_ir, sample_rate, n_fft=n_fft, calibration=calibration)
    mag_db = 20.0 * np.log10(np.maximum(np.abs(H), 1e-12))

    snr_block = _driver_snr_block(
        ambient_report=ambient_report,
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


def _estimate_alignment(
    capture: np.ndarray,
    program: ExcitationProgram,
    sample_rate: int,
    global_offset: int,
    epsilon: float,
    fc_hz: float,
    geometry: MeasurementGeometry,
    priors: MeasurementPriors,
    *,
    woofer_full_ir: np.ndarray,
    tweeter_full_ir: np.ndarray,
    pre_samples: int,
) -> AlignmentEstimate:
    seg_w = program.segment("sweep_w")
    seg_t = program.segment("sweep_t")
    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
    )

    max_lag = priors.align_search_ms * 1e-3 * sample_rate
    # Both IRs share the pre-guard + global offset time base, so each direct
    # peak sits at pre_samples +/- the relative delay. Slice the same
    # [pre-H, pre+H] region from both, band-limit to the overlap, GCC-PHAT.
    half = int(round(0.010 * sample_rate)) + int(math.ceil(max_lag)) + 1
    a = max(0, pre_samples - half)
    b_w = min(woofer_full_ir.size, pre_samples + half)
    b_t = min(tweeter_full_ir.size, pre_samples + half)
    b = min(b_w, b_t)
    ir_w = _bandlimit(np.asarray(woofer_full_ir[a:b], dtype=np.float64), sample_rate, lo, hi)
    ir_t = _bandlimit(np.asarray(tweeter_full_ir[a:b], dtype=np.float64), sample_rate, lo, hi)
    length = min(ir_w.size, ir_t.size)
    ir_w, ir_t = ir_w[:length], ir_t[:length]

    lag_samples, polarity_sign, confidence, at_edge = gcc_phat(
        ir_t, ir_w, sample_rate=sample_rate, band_hz=(lo, hi),
        upsample=GCC_UPSAMPLE, max_lag_samples=max_lag,
    )
    # epsilon-correct: the tweeter's schedule offset is stretched by epsilon.
    delta_start = seg_t.start_sample - seg_w.start_sample
    tau_samples = lag_samples - epsilon * delta_start
    # delay_us = (D_woofer - D_tweeter) = -tau (tau = D_tweeter - D_woofer).
    raw_delay_us = -tau_samples / sample_rate * 1e6
    parallax_us = geometry.parallax_us()
    delay_us = raw_delay_us - parallax_us

    polarity = polarity_label(polarity_sign)

    status = ALIGNMENT_OK
    if at_edge:
        # A peak clamped at the search bound likely exceeds the geometry
        # prior; fail explicitly rather than return a wrong value.
        status = ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW
        confidence = 0.0
        log_event(
            logger,
            "program_analysis.alignment_edge",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            lag_samples=round(lag_samples, 3),
            search_window_ms=priors.align_search_ms,
        )

    # Fine stage (methodology §10). The aligner OWNS the physical peak-gap
    # anchor (raw full-IR argmax gap, drift+parallax-corrected); the argmax
    # is never recomputed downstream so the snap center and the reported
    # anchor cannot desync. The snap moves the anchor to the nearest local
    # maximum of the same correlation within +/-(period/6) at Fc; ``None``
    # leaves ``_build_candidate`` on the bare anchor. Snaps applied delay
    # only — GCC polarity/confidence is untouched.
    snapped_delay_us: float | None = None
    anchor_delay_us: float | None = None
    if status == ALIGNMENT_OK:
        anchor_lag_samples = float(
            int(np.argmax(np.abs(tweeter_full_ir)))
            - int(np.argmax(np.abs(woofer_full_ir)))
        )
        # Peak gap - inter-sweep drift, plus parallax, negated into the signed frame.
        inter_sweep_drift_us = epsilon * delta_start / sample_rate * 1e6
        drift_corrected_peak_gap_us = (
            anchor_lag_samples / sample_rate * 1e6 - inter_sweep_drift_us
        )
        anchor_delay_us = -(drift_corrected_peak_gap_us + parallax_us)
        if fc_hz > 0.0:
            radius_samples = sample_rate / fc_hz * GCC_SNAP_RADIUS_PERIODS
            # Recomputes the correlation rather than threading the seed's
            # array: one extra small FFT per MEASURE, no big-array coupling.
            snapped_lag = _gcc_local_peak_snap(
                ir_t, ir_w, sample_rate=sample_rate, band_hz=(lo, hi),
                upsample=GCC_UPSAMPLE, anchor_lag_samples=anchor_lag_samples,
                radius_samples=radius_samples,
            )
            if snapped_lag is not None:
                snapped_tau = snapped_lag - epsilon * delta_start
                snapped_delay_us = -snapped_tau / sample_rate * 1e6 - parallax_us

    # The flat-sum cross-check belongs to `_select_alignment_pair`; this
    # estimate is the correlation SEED, and `polarity_agrees_with_sum`
    # stays None until the selection answers it.
    return AlignmentEstimate(
        delay_us=delay_us,
        raw_delay_us=raw_delay_us,
        parallax_us=parallax_us,
        polarity=polarity,
        polarity_sign=polarity_sign,
        confidence=confidence,
        status=status,
        anchor_delay_us=anchor_delay_us,
        snapped_delay_us=snapped_delay_us,
    )


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


def _finite_or_none(value: float, ndigits: int) -> float | None:
    """``round(value, ndigits)``, or ``None`` when it is not a finite number.

    For journal fields that quote a measured quantity which CAN be non-finite:
    ``None`` reads as "no number", where a bare NaN reads as a number and
    survives into whatever parses the line.
    """
    return round(float(value), ndigits) if math.isfinite(value) else None


def polarity_label(polarity_sign: int) -> str:
    """``+1 -> "normal"``, ``-1 -> "inverted"``. The ONE spelling of the map."""
    return "normal" if polarity_sign >= 0 else "inverted"


def polarity_sign_of(polarity: str) -> int:
    """Inverse of :func:`polarity_label`; anything but ``"inverted"`` is ``+1``."""
    return -1 if polarity == "inverted" else 1


@dataclass(frozen=True)
class AlignmentPairSelection:
    """What :func:`_select_alignment_pair` committed, and the evidence for it.

    ``polarity_sign``/``delay_us`` are the committed pair; ``ripple_db`` is
    the summed blend ripple there. ``seed_*`` is the pair correlation alone
    would have shipped, scored on the SAME objective; ``objective`` names
    which commitment this is (:data:`ALIGNMENT_COMMITMENTS`).
    ``grid_points``/``grid_step_us`` describe the delay grid searched per
    polarity (``1``/``0.0`` when nothing was searched).

    ``left_anchor_lobe`` is True when the committed delay sits more than
    half a period at Fc from the anchor — legitimate when the objective is
    that sure, but also the shape a fooled objective would take, so it
    raises the selection log to WARNING.
    """

    polarity_sign: int
    delay_us: float
    ripple_db: float
    seed_polarity_sign: int
    seed_delay_us: float
    seed_ripple_db: float
    objective: str
    grid_points: int
    grid_step_us: float
    left_anchor_lobe: bool = False
    #: Was the POLARITY axis pinned by the request rather than searched?
    #: The objective string can't carry this (a pinned round still commits
    #: ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION); read via
    #: :attr:`polarity_agrees_with_sum` instead.
    polarity_pinned: bool = False

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
    def flatness_improvement_db(self) -> float:
        """``seed_ripple - committed_ripple``: what the objective bought."""
        return self.seed_ripple_db - self.ripple_db


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
    applied_alignment: AppliedAlignment | None = None,
    explicit_delay_us: float | None = None,
    explicit_polarity_sign: int | None = None,
) -> AlignmentPairSelection | None:
    """Commit the (polarity, delay) pair whose predicted blend sums flattest.

    ONE objective for both halves of one decision: the ripple of ``W + s*T``
    over ``[lo_hz, hi_hz]``, scored across ``s in {+1, -1}`` and a delay
    grid of :data:`ALIGNMENT_FLATNESS_STEP_US` steps spanning
    +/-:data:`ALIGNMENT_FLATNESS_SPAN_PERIODS` period(s) at ``fc_hz`` around
    ``anchor_delay_us`` — polarity and delay trade against each other, so
    scoring them separately would compare one against a guess about the
    other. Correlation stays in the loop as the SEED and tie-break: within
    :data:`ALIGNMENT_FLAT_MINIMUM_EPSILON_DB` of the global minimum, the
    search keeps whichever is closest to the seed.

    ``trim_w_db``/``trim_t_db`` are the LEVEL-MATCH trims (band-average),
    not the ripple-polished tweeter trim — the polish needs a polarity, so
    scoring at the polished trim would be circular.

    ``delay_bounds_us`` is the preset's declared |delay| range; grid points
    outside it are dropped (the seed pair is exempt).

    ``branch_snr_insufficient`` is the refusal
    (:data:`ALIGNMENT_SNR_REFUSAL_VERDICT`): the pair is not searched but
    COMMITTED to the declared design (relative polarity ``+1``) at a delay
    this capture did not supply — never the anchor or GCC seed, which are
    this capture's own answer and exactly what a low-SNR capture gets
    wrong (across nine jts3 positions the anchor read +59.6 us on-axis
    against six clustered near -211 us and two wild). The delay comes from
    ``applied_alignment`` (what the speaker already plays) or ``0.0``; three
    arms/objectives distinguish held/declared/unreadable-apply.

    ``left_anchor_lobe`` fires when the committed delay and this capture's
    anchor disagree by more than half a period at Fc, raising the selection
    log to WARNING — legitimate when the objective is that sure, but also
    the shape a fooled objective would take.

    ``explicit_delay_us`` is a host-validated PRESCRIPTION (delay from a
    named measured basis, bounded to +/-half a period at Fc); it fixes the
    delay axis to exactly that point and outranks the low-SNR ladder.
    Deliberately NOT an anchor substitute — re-centring the search on it
    would let the objective wander off the prescribed value.
    ``explicit_polarity_sign`` pins the other axis the same way (on both
    prescription arms), recording :attr:`~AlignmentPairSelection.polarity_pinned`
    so ``polarity_agrees_with_sum`` reports ``None`` rather than a search
    result that never ran.

    Returns ``None`` when the objective cannot be evaluated at all (no
    frequency bin in range, or no finite-score candidate), leaving the
    caller on the seed with a WARNING.
    """
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

    if explicit_delay_us is not None and branch_snr_insufficient:
        # The prescription stands (it did not come from this capture); an
        # unpinned polarity commits declared relative polarity +1 instead.
        prescribed_us = float(explicit_delay_us)
        prescribed_sign = (
            1 if explicit_polarity_sign is None else int(explicit_polarity_sign)
        )
        return AlignmentPairSelection(
            polarity_sign=prescribed_sign,
            delay_us=prescribed_us,
            ripple_db=_ripple_at(prescribed_sign, prescribed_us),
            seed_polarity_sign=seed_polarity_sign,
            seed_delay_us=seed_delay_us,
            seed_ripple_db=seed_ripple_db,
            objective=ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
            grid_points=1,
            grid_step_us=0.0,
            left_anchor_lobe=_left_anchor_lobe(prescribed_us),
            polarity_pinned=explicit_polarity_sign is not None,
        )

    if branch_snr_insufficient:
        # Neither `anchor_delay_us` nor `seed_delay_us` may be read here:
        # both are this capture's own answer, and this capture was refused.
        held_delay_us = (
            None if applied_alignment is None else applied_alignment.delay_us
        )
        committed_delay_us = 0.0 if held_delay_us is None else float(held_delay_us)
        if held_delay_us is not None:
            objective = ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR
        elif applied_alignment is None:
            objective = ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR
        else:
            objective = ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY
        return AlignmentPairSelection(
            polarity_sign=1,
            delay_us=committed_delay_us,
            ripple_db=_ripple_at(1, committed_delay_us),
            seed_polarity_sign=seed_polarity_sign,
            seed_delay_us=seed_delay_us,
            seed_ripple_db=seed_ripple_db,
            objective=objective,
            grid_points=1,
            grid_step_us=0.0,
            left_anchor_lobe=_left_anchor_lobe(committed_delay_us),
        )

    # The delay is only scorable against an anchor: with none, the residual
    # is 0.0 for every candidate, so search the polarity alone.
    grid_step_us = 0.0
    delays = [seed_delay_us]
    # A prescription fixes the delay axis to exactly one point. The seed is
    # NOT appended here (unlike elsewhere): that would let the search
    # silently return the seed's delay instead of the prescribed one.
    if explicit_delay_us is not None:
        delays = [float(explicit_delay_us)]
    elif anchor_delay_us is not None and fc_hz > 0.0:
        span_us = ALIGNMENT_FLATNESS_SPAN_PERIODS * 1e6 / fc_hz
        n_steps = int(round(span_us / ALIGNMENT_FLATNESS_STEP_US))
        n_steps = max(1, min(n_steps, ALIGNMENT_FLATNESS_MAX_STEPS))
        step_us = span_us / n_steps
        grid = [anchor_delay_us + i * step_us for i in range(-n_steps, n_steps + 1)]
        if delay_bounds_us is not None:
            lo_us, hi_us = (abs(float(b)) for b in delay_bounds_us)
            lo_us, hi_us = min(lo_us, hi_us), max(lo_us, hi_us)
            grid = [d for d in grid if lo_us <= abs(d) <= hi_us]
        # An empty grid (bound admits no point) reports like no-anchor: (1, 0.0).
        if grid:
            grid_step_us = step_us
        delays = [*grid, seed_delay_us]

    # The polarity axis, pinned the same way: the seed sign is not added
    # back, for the same reason the seed delay is not.
    signs = (
        (1, -1) if explicit_polarity_sign is None
        else (int(explicit_polarity_sign),)
    )
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
        objective=(
            ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION
            if explicit_delay_us is not None
            else ALIGNMENT_COMMITTED_FLAT_SUM
        ),
        grid_points=len(delays),
        grid_step_us=grid_step_us,
        left_anchor_lobe=_left_anchor_lobe(committed_delay_us),
        polarity_pinned=explicit_polarity_sign is not None,
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
    NOT passband-average sensitivity
    (:func:`~jasper.active_speaker.linearization_fit.driver_core_level_db`
    is a different, legitimately-differing starting estimate for horn
    attenuation, disclosed rather than reconciled).

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


@dataclass(frozen=True)
class RealizedLevelMatch:
    """What the two branches ACTUALLY hand off at, once the trim is applied.

    ``difference_db`` is ``level_t_db - level_w_db``: the signed inter-driver
    level error, whose design intent is **zero**. Sign is kept because "the
    tweeter is 9 dB down" and "the tweeter is 9 dB up" are opposite defects and
    a bare magnitude hides which one shipped.

    ``matched`` is ``abs(difference_db) <= tolerance_db``. It is the verdict; it
    is not advice.
    """

    level_w_db: float
    level_t_db: float
    difference_db: float
    tolerance_db: float
    matched: bool
    woofer_band_hz: tuple[float, float]
    tweeter_band_hz: tuple[float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level_w_db": self.level_w_db,
            "level_t_db": self.level_t_db,
            "difference_db": self.difference_db,
            "tolerance_db": self.tolerance_db,
            "matched": self.matched,
            "woofer_band_hz": list(self.woofer_band_hz),
            "tweeter_band_hz": list(self.tweeter_band_hz),
        }


def realized_branch_level_match(
    freqs: np.ndarray,
    W: np.ndarray,
    T: np.ndarray,
    fc_hz: float,
    *,
    trim_w_db: float,
    trim_t_db: float,
    woofer_span_hz: tuple[float, float] | None = None,
    tweeter_span_hz: tuple[float, float] | None = None,
    tolerance_db: float = REALIZED_LEVEL_MATCH_TOLERANCE_DB,
) -> RealizedLevelMatch:
    """Each branch's REALIZED power-band level, on its own side of Fc, after
    the committed trim — and whether the two agree.

    The assertion nothing else in the chain makes: no other comparator
    compares the two drivers' realized handoff levels to each other (the
    gap a ~9 dB-dark tweeter walked through). ``W``/``T`` are the branch
    transfer functions AS EMITTED (linearized on the v2 path);
    ``trim_*_db`` are applied here and re-read, grading the committed
    decision rather than re-litigating it.

    Reuses :func:`solve_branch_trims`'s own estimator (same bands, same
    average) rather than a rival check, inheriting its known +0.54 dB
    systematic rather than adding an unknown one. Raises ``ValueError``
    through the same surface as the trim solve it wraps — never a guessed
    verdict.
    """
    g_w = 10.0 ** (float(trim_w_db) / 20.0)
    g_t = 10.0 ** (float(trim_t_db) / 20.0)
    _residual_w, _residual_t, level_w, level_t = solve_branch_trims(
        freqs, W * g_w, T * g_t, fc_hz,
        woofer_span_hz=woofer_span_hz, tweeter_span_hz=tweeter_span_hz,
    )
    woofer_band, tweeter_band = branch_level_bands_hz(
        fc_hz, woofer_span_hz=woofer_span_hz, tweeter_span_hz=tweeter_span_hz,
    )
    difference = float(level_t - level_w)
    return RealizedLevelMatch(
        level_w_db=float(level_w),
        level_t_db=float(level_t),
        difference_db=difference,
        tolerance_db=float(tolerance_db),
        matched=abs(difference) <= float(tolerance_db),
        woofer_band_hz=woofer_band,
        tweeter_band_hz=tweeter_band,
    )


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
