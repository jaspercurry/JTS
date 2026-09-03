# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure analysis of a crossover excitation-program capture (design §5.6).

``analyze_program_capture(program, samples, sample_rate) -> ProgramAnalysis``
derives segment locations, per-segment integrity, in-capture clock drift,
per-driver gated responses, tweeter-vs-woofer alignment and the crossover
candidate from the ``(program, capture)`` pair alone. No I/O, no product
policy, and no ``jasper.correction`` / ``jasper.active_speaker`` import
(``tests/test_correction_boundary_ssot.py`` pins that boundary), so product
crossover transfers arrive as host-evaluated per-role callables on
:class:`MeasurementPriors`.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

import numpy as np

from jasper.audio_measurement import analysis as analysis_mod
from jasper.audio_measurement import calibration as calibration_mod
from jasper.audio_measurement import deconv, gate_disclosure, gating, snr_policy
from jasper.audio_measurement.frame_fit import FrameComparison, fit_frame
from jasper.audio_measurement.frame_ledger import FrameLedger, reconcile_capture_frames
from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
    KIND_PILOT,
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    PROGRAM_PHASE_CHECK,
    PROGRAM_PHASE_MEASURE,
    PROGRAM_PHASE_VERIFY,
    STIMULUS_KINDS,
    ExcitationProgram,
    ProgramSegment,
    segment_stimulus,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.timeline_slip import (
    GLITCH_INPUT_TIMELINE_SLIP,
    TimelineStepFit,
    fit_timeline_step,
    slip_rejects_capture,
)
from jasper.audio_measurement.alignment import (
    GCC_UPSAMPLE,
    _bandlimit,
    _gcc_local_peak_snap,
    cross_correlation_alignment,
    gcc_phat,
    parabolic_peak,
)
from jasper.audio_measurement.comparison_bands import (
    OVERLAP_OCTAVE_RATIO,
    branch_snr_band_hz,
    crossover_region_band_hz,
    overlap_band_hz,
    verify_tracking_band_hz,
)
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve

logger = logging.getLogger(__name__)

# --- locator / drift / alignment tuning ---
# Per-segment search half-window, seconds, around the drift-free scheduled
# offset: a few-hundred-ppm drift over a ~25 s program is ≈6 ms, plus acoustic
# delay.
SEGMENT_SEARCH_S = 0.030
# Capture bound, seconds: a stuck recording is truncated to program duration
# plus this margin before any full-rate FFT runs (1 GB Pi; mirrors
# deconv.cap_capture_length). It also bounds the global offset the locator sees.
CAPTURE_BOUND_MARGIN_S = 10.0
# Downsampled rate, Hz, for the global-offset locate, so the whole-capture
# correlation never allocates hundreds of MB; the arrival is refined at the full
# rate inside a tiny window afterwards.
LOCATOR_RATE_HZ = 16_000
# Clip run: consecutive samples at/above full scale. The at-full-scale threshold
# is owned by quality_model, not re-declared here.
CLIP_RUN_SAMPLES = 3
CLIP_ABS_THRESHOLD = DRIVER.clip_abs_threshold
DBFS_FLOOR = -120.0
ILL_CONDITIONED_PROTECTION_DEEMBEDDING = "ill_conditioned_protection_deembedding"
# Conditioning floor on the emitted protection `P`, dB: below it, dividing `P`
# out amplifies the capture's noise faster than it recovers signal, so
# `_compose_configured_path_ir` refuses the ratio rather than saturating it.
CONFIGURED_PATH_PROTECTION_FLOOR_DB: float = -12.0


class ConfiguredPathConditioningError(ValueError):
    slug = ILL_CONDITIONED_PROTECTION_DEEMBEDDING

    def __init__(self, detail: str, *, protection_floor: bool = False) -> None:
        # `abs(P) < floor` does not involve `C`, so Fc cannot clear it: the
        # household's lever differs, and the copy is routed on this flag.
        self.protection_floor = protection_floor
        super().__init__(f"{self.slug}: {detail}")


# Max disagreement between the drift baselines, samples-equivalent (design
# §5.6.3), against a `_subsample_separation` estimator whose measured floor over
# a 28-capture hardware corpus is 0.04-0.30 samples. A +4-sample insertion
# rejects (spread 2.0); +2 passes (spread 1.0).
GLITCH_RESIDUAL_SAMPLES = 1.5
MAX_DRIFT_PPM = 500.0

# Max captured-level gap, dB, between a MEASURE program's two bit-identical
# woofer sweeps; a larger gap is a gain-rider and REUSES the
# ``drift_baselines_disagree`` verdict rather than adding a reason code (design
# §5.2). Read as band-relative in-band RMS, not full-band peak: two hardware
# mics read two identical woofer sweeps 0.64 dB apart by peak but 0.06-0.24 dB
# apart by in-band RMS.
REPEAT_LEVEL_TOLERANCE_DB = 0.3

# Floor on `SegmentLocation.confidence` below which a located sweep is not
# evidence: at ~0.03 the timeline step fit reports a confident-looking
# multi-thousand-sample step fitted from noise. Deliberately duplicated rather
# than imported from `crossover_v2.capture_dispatch.SWEEP_LOCATE_CONFIDENCE_FLOOR`
# — the two judge different segment kinds through different gates, so bench work
# may settle them apart — and pinned equal meanwhile by
# tests/test_measurement_integrity_floor_contracts.py.
SWEEP_LOCATE_CONFIDENCE_FLOOR = 0.3

# How many TIMES more present the winning anchor hypothesis's witness must be
# than its runner-up's before `_resolve_anchor` may call the anchor RESOLVED. A
# ratio, not a difference: `_locate_in_window`'s `presence` is normalized by its
# own window's energy, so no absolute number separates a quiet room from a loud
# one. 50 is the round number nearest the geometric centre of the measured
# population gap (sqrt(12.4 x 197) = 49.4) between cannot-discriminate (garbage
# 1.07, twin 3.50-3.51, silent-driver 12.4) and resolved (197-11500 fixture
# ramp, 214.17/404.40 on the two real jts3 captures, 61857 on VERIFY).
# PROVISIONAL: the `program_analysis.anchor` event's `ambiguous=`, `presence=`,
# `runner_up_presence=` and `runner_up_anchor=` fields are what a field
# population would be counted from.
ANCHOR_DISCRIMINATION_RATIO = 50.0

# Max residual, ms, between a located VERIFY summed sweep and its SCHEDULED
# slot; beyond it the timeline was spliced, not drifted. Twin of
# `crossover_v2.capture_dispatch.SWEEP_SCHEDULE_RESIDUAL_CEILING_MS` (that
# module applies it to MEASURE's `KIND_SWEEP`, this one to VERIFY's single
# `KIND_SUMMED_SWEEP`), duplicated and pinned by the same contract test as the
# floor above. INHERITED, NOT RE-DERIVED: 5 ms comes from MEASURE evidence (a
# glitched capture at −25…−28 ms against a clean corpus at ≤1.5 ms); no
# VERIFY-corpus distribution has been measured.
SWEEP_SCHEDULE_RESIDUAL_CEILING_MS = 5.0

# Sentinel for "the located sweeps were not trustworthy enough to fit a step
# from at all", distinct from `0.0` ("confidently no step"). A `str`, not a
# `float`, so a consumer cannot mistake it for a vanishingly small step;
# `DriftEstimate.discontinuity_samples` and `analysis_diagnostic_summary` must
# handle the non-numeric case.
DISCONTINUITY_UNRESOLVED = "unresolved"

# --- VERIFY capture integrity ---------------------------------------------- #
#
# Three states, because VERIFY plays ONE mono summed sweep and so has no repeat
# pair to take an epsilon, a level agreement or a within-role residual from.
# ``not_evaluated`` is what a structurally-inapplicable check reports, and is
# never collapsed into a pass.
INTEGRITY_PASS = "pass"
INTEGRITY_FAIL = "fail"
INTEGRITY_NOT_EVALUATED = "not_evaluated"

# Frame accounting, asked BEFORE anything about the signal. Two checks rather
# than one because the losses are independently caused: a capture page can
# report render continuity while declaring no counts, and a skipped render
# quantum leaves every count agreeing while the recording is short. Both read
# :class:`~jasper.audio_measurement.frame_ledger.FrameLedger`.
INTEGRITY_CHECK_RENDER_GAP = "capture_render_gap"
INTEGRITY_CHECK_FRAME_LEDGER = "frame_ledger"
# The checks a single summed sweep CAN answer.
INTEGRITY_CHECK_SWEEP_HEARD = "summed_sweep_heard"
INTEGRITY_CHECK_SWEEP_SCHEDULE = "summed_sweep_schedule"
INTEGRITY_CHECK_CLIPPED_RUN = "clipped_run"
# The MEASURE-side checks it CANNOT, recorded by name rather than omitted.
# Their MEASURE counterparts are ``DriftEstimate.glitch_inputs``.
INTEGRITY_CHECK_REPEAT_EPSILON = "repeat_epsilon"
INTEGRITY_CHECK_REPEAT_LEVEL = "repeat_level_agreement"
INTEGRITY_CHECK_WITHIN_ROLE_DESYNC = "within_role_desync"
INTEGRITY_CHECK_DISCONTINUITY_STEP = "discontinuity_step"

# Why each unevaluated check could not run, stored on the check itself so the
# record explains itself without a lookup table.
_INTEGRITY_NO_REPEAT_PAIR = "verify plays one summed sweep: no repeat pair"
_INTEGRITY_STEP_NEEDS_MORE_SWEEPS = (
    "a step fit needs more located sweeps than a verify program has"
)
_INTEGRITY_NO_SUMMED_SWEEP = "no summed sweep located in this capture"
_INTEGRITY_NO_STIMULUS = "no stimulus segment located in this capture"
_INTEGRITY_NO_RENDER_REPORT = (
    "the capture page reported no render-block counters"
)
_INTEGRITY_NO_FRAME_COUNT = "the capture page declared no frame count"
_INTEGRITY_SWEEP_NOT_HEARD = (
    "the summed sweep was not confidently located, so its schedule residual "
    "is not evidence"
)

DEFAULT_ALIGN_SEARCH_MS = 2.0  # geometry prior bound on |relative delay|

# Gated local-peak snap radius, as a fraction of the crossover period at Fc
# (docs/historical/crossover-measurement-reproducibility-plan.md §10).
# period/6 = λ/6 is
# the GPS integer-ambiguity lobe-selection budget (Teunissen): a coarse anchor
# with error σ ≤ λ/6 picks the correct comb lobe with ≥99.7% probability. On the
# E0 corpus that is ~2× the largest legitimate observed snap (≈39 µs) and below
# the +166 µs stable-but-wrong feature. Declaration-driven: the µs radius
# derives from the priors' Fc (≈83.3 µs at Fc=2 kHz). PROVISIONAL.
GCC_SNAP_RADIUS_PERIODS = 1.0 / 6.0

# Alignment estimator status vocabulary.
ALIGNMENT_OK = "ok"
ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"

# --------------------------------------------------------------------------- #
# Joint (polarity, delay) alignment selection
# --------------------------------------------------------------------------- #
#
# One objective: the ripple of the predicted summed blend, with correlation as
# the seed and the tie-break. Polarity and delay are ONE decision — an inverted
# Linkwitz-Riley pair sums to a commanded NULL at Fc. Search span: ± one period
# at Fc, the ambiguity interval a comb lobe lives in, derived from the priors'
# Fc rather than a µs literal.
ALIGNMENT_FLATNESS_SPAN_PERIODS = 1.0
ALIGNMENT_FLATNESS_STEP_US = 10.0
# Point-count cap for bounded CPU on the speaker: a low Fc makes the
# period-derived span long, so the step widens to fit rather than the count
# scaling as 1/Fc. Inactive at Fc >= ~500 Hz.
ALIGNMENT_FLATNESS_MAX_STEPS = 200
# Flat-minimum regularization, dB: among every pair within this much of the
# global minimum, keep the SEED pair, so an applied alignment does not wander
# between re-measurements. Same shape and value as
# RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB on its own axis, deliberately a separate
# constant so neither silently retunes the other.
ALIGNMENT_FLAT_MINIMUM_EPSILON_DB = 0.25

#: What the candidate's (polarity, delay) pair IS — never merely why some other
#: answer was rejected.
ALIGNMENT_COMMITTED_FLAT_SUM = "flat_sum_committed"
ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR = "declared_committed_after_low_snr"
#: The declared polarity at the delay the applied graph already carries.
ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR = "applied_alignment_held_after_low_snr"
#: A graph IS applied but its inter-driver delay could not be read, so no delay
#: is committed — its own value, because "the design asks for none" would be an
#: unchecked claim about this speaker.
ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY = (
    "no_delay_committed_after_unreadable_apply"
)
#: The delay came from a host-validated explicit prescription
#: (:data:`MeasurementPriors.explicit_alignment_delay_us`), not this capture's
#: search; its POLARITY is still the flat-sum objective's answer.
ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION = "explicit_prescription_committed"
#: The same prescription on a capture the SNR verdict refused for alignment.
#: Deliberately not spelled as the value above plus a suffix: that would make
#: the plain value a strict PREFIX of this one.
ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR = (
    "explicit_prescription_held_after_low_snr"
)
ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND = "seed_committed_no_scoring_band"
ALIGNMENT_COMMITTED_SEED_ALIGNMENT_REFUSED = "seed_committed_alignment_refused"
ALIGNMENT_COMMITMENTS = frozenset({
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY,
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND,
    ALIGNMENT_COMMITTED_SEED_ALIGNMENT_REFUSED,
})
#: Membership rule: THE CAPTURE'S ALIGNMENT EVIDENCE WAS WHOLLY UNTRUSTED, so
#: neither its polarity nor its anchor may be spoken for. Two readers ride that
#: one question — the household wording, chosen in a browser module that cannot
#: import a Python constant (``tests/test_crossover_envelope_v2.py`` fails when
#: the two disagree), and ``_build_candidate``, which gates the anchor
#: withdrawal on membership here so these commitments ship a summed model
#: carrying NO residual.
ALIGNMENT_DECLARED_POLARITY_OBJECTIVES = frozenset({
    ALIGNMENT_COMMITTED_DECLARED_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_APPLIED_HELD_AFTER_LOW_SNR,
    ALIGNMENT_COMMITTED_NONE_AFTER_UNREADABLE_APPLY,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
})
#: The commitments an explicit prescription produced. Read by
#: ``crossover_v2.coordinator._round_measurements``, which banks membership here
#: on the round receipt as the prescription's ``committed`` bit.
ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES = frozenset({
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
    ALIGNMENT_COMMITTED_EXPLICIT_AFTER_LOW_SNR,
})
#: The commitments where the flat-sum objective chose the POLARITY — the only
#: ones that may answer ``polarity_agrees_with_sum``. Necessary, not sufficient:
#: a prescription may also PIN the polarity, so
#: :attr:`AlignmentPairSelection.polarity_agrees_with_sum` asks
#: :attr:`~AlignmentPairSelection.polarity_pinned` FIRST.
_FLAT_SUM_POLARITY_OBJECTIVES = frozenset({
    ALIGNMENT_COMMITTED_FLAT_SUM,
    ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION,
})
#: The commitments the selector itself made, as opposed to the two where the
#: seed simply stood and no flat sum was ever computed.
_SELECTOR_COMMITTED_OBJECTIVES = frozenset({
    *_FLAT_SUM_POLARITY_OBJECTIVES,
    *ALIGNMENT_DECLARED_POLARITY_OBJECTIVES,
})

#: The verdict at which a branch stops being evidence a polarity flip may rest
#: on, read off the ALIGNMENT decision class
#: (:data:`~jasper.audio_measurement.snr_policy.DECISION_CLASS_ALIGNMENT`, the
#: 35 dB ``DRIVER.alignment_snr_ok_db`` law with no ``reduced`` rung) and never
#: the magnitude class. ``unknown``/absent means never computed.
ALIGNMENT_SNR_REFUSAL_VERDICT = "insufficient"

#: Where :func:`_driver_snr_block` files its ALIGNMENT-class verdict inside the
#: per-driver SNR block; :func:`driver_alignment_snr_verdict` is the one reader.
DRIVER_SNR_ALIGNMENT_KEY = "alignment"

# Ripple-optimal trim POLISH: re-solve the tweeter trim for minimum
# summed-response ripple over the seed ± this window at this step. A jts3
# corpus (5 replayed runs) observed a 1.7-6.3 dB gap between the seed and the
# ripple optimum, so ±10 dB is never truncated at its own edge.
RIPPLE_TRIM_SEARCH_WINDOW_DB = 10.0
RIPPLE_TRIM_SEARCH_STEP_DB = 0.1

# Flat-minimum regularization, dB: among candidates within this much of the
# scan's GLOBAL minimum ripple, prefer whichever is CLOSEST TO THE SEED. Below
# an audible ripple difference and above the 0.1 dB grid step, so a sharp
# minimum is never widened into a plateau by quantization.
RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB = 0.25

# A trim is a passive level-match: never net gain, never beyond the shared
# -60 dB attenuation floor owned by
# jasper.active_speaker.level_trim.MAX_ATTENUATION_DB, mirrored locally because
# this module does not import jasper.active_speaker. solve_branch_trims holds
# the range implicitly; the ripple-optimal scan must enforce it explicitly.
RIPPLE_TRIM_MAX_DB = 0.0
RIPPLE_TRIM_MIN_DB = -60.0

# How far the two branches' realized levels — read on mirrored ±1-octave
# half-bands about Fc, NOT each driver's whole passband — may sit apart after
# the committed trim before the pair is REPORTED as mislevelled. A DISCLOSURE,
# not a gate; read by :func:`realized_branch_level_match` and imported by
# ``crossover_v2.intervention.LEVEL_ESTIMATOR_TOLERANCE_DB``, where a
# disagreement past it flags a capture as retriable. Floor: five archived JTS3
# cdhorn captures agree to 0.51-1.30 dB with a known +0.54 dB linear-bin
# systematic (:func:`solve_branch_trims`' N1 note). Ceiling: an inter-branch
# error D appears as a step across Fc and reaches ``flat_spec.SPEC_BANDS[1]``'s
# 2.0 dB tolerance at D ≈ 2.0, so 2-3 dB errors are spec failures this
# disclosure does not flag. For scale, a profile the owner heard as dark reads
# ~9 dB here.
REALIZED_LEVEL_MATCH_TOLERANCE_DB = 3.0

# Direct-arrival window, ms, isolating each driver's IR before deconvolution
# magnitude / alignment (mirrors deconv defaults; the pre guard catches the
# non-causal deconvolution shoulder).
IR_PRE_MS = 5.0
IR_POST_MS = 60.0

# Deconvolution window pre-guard, seconds, before the scheduled sweep position.
# Both drivers share this pre-guard and the global-offset anchor, so their IR
# direct peaks land at the pre-guard sample ± the relative delay — the aligner
# relies on that shared time base.
DECONV_PRE_GUARD_S = 0.25

# Gain solve: land the MEASURE capture peak in [-12, -9] dBFS with ≥6 dB guard.
# A CEILING rather than a target — see `_solve_role_gain`.
DEFAULT_TARGET_CAPTURE_DBFS = -10.5
GAIN_GUARD_DB = 6.0
GAIN_MAX_DIGITAL_PEAK_DBFS = -GAIN_GUARD_DB  # digital peak must sit ≤ this

# The MEASURE level solve's own margin, dB, on top of a band's SNR requirement.
# The ambient evidence is CHECK's 12 s window, measured up to a minute earlier
# and (per `program`'s module docstring) BEFORE the courtesy beeps, and `k`
# comes from 0.8 s pilots rather than the sweep. Deliberately not shared with
# the ramp-driven solver's equal margin: the two solvers are independent.
MEASURE_SNR_SOLVE_MARGIN_DB = 6.0

# Peak-to-RMS of the excitation itself, dB. Every stimulus segment here is
# rendered by `program.segment_stimulus` as a constant-amplitude synchronized
# swept sine, so its peak sits exactly 10*log10(2) dB above its own full-band
# RMS. Measured at 3.02-3.03 dB on both real MEASURE sweeps.
SWEEP_PEAK_TO_RMS_DB = 3.0103

# --------------------------------------------------------------------------- #
# The demand and the budget must be in the same units
# --------------------------------------------------------------------------- #


def sweep_band_crest_factor_db(
    sweep_hz: tuple[float, float], band_hz: tuple[float, float]
) -> float:
    """dB from a swept sine's PEAK down to its RMS inside ``band_hz``.

    The MEASURE level solve budgets a capture *peak*
    (``MeasurementPriors.target_capture_dbfs``, and ``k_db`` measured from
    pilot peaks) while its SNR demand is stated against an ambient *band RMS*;
    this is the conversion between the two.

    Two terms, both exact for the constant-amplitude exponential sweep
    ``program.segment_stimulus`` renders:

    * :data:`SWEEP_PEAK_TO_RMS_DB` — peak to full-band RMS.
    * ``10*log10( ln(f2/f1) / ln(hi/lo) )`` — band occupancy. An exponential
      sweep dwells in ``[lo, hi]`` for ``ln(hi/lo)/ln(f2/f1)`` of its
      duration, so its energy density per Hz falls as ``1/f`` and a
      sub-band holds only that fraction of the total. After deconvolution
      the matched filter gathers all of the band's signal energy — and all
      of the band's noise over the same observation window — so the
      dilution does NOT cancel: the per-band SNR of a swept-sine measurement
      is ``(sweep RMS**2 / ambient band power) * dwell_fraction``.

    ``band_hz`` is clipped to ``sweep_hz`` first: a sweep puts no energy
    where it does not go.

    Validated against the rendered stimulus to within 0.03 dB on both
    production MEASURE sweeps (150-2000 Hz woofer, 1500-23000 Hz tweeter);
    the widest disagreement in the set is 0.63 dB, on a 10 Hz-wide clipped
    sliver where FFT bin granularity, not the law, is the limit.

    Erring LOUD: the ambient rows this is applied to
    (``snr_policy.CROSSOVER_SNR_BANDS_HZ``) can be wider than the slice of them
    the sweep covers — a woofer swept from 150 Hz clips the 80-160 Hz ``bass``
    row to 10 Hz. The crest is then computed over the 10 Hz slice, where the
    sweep's energy is, while the ambient level is still the whole row's. That
    over-states the demand, which keeps MEASURE louder — the same direction
    :func:`_solve_role_gain`'s row-width coarseness errs, and the safe one.
    """
    lo = max(float(band_hz[0]), float(sweep_hz[0]))
    hi = min(float(band_hz[1]), float(sweep_hz[1]))
    f1, f2 = float(sweep_hz[0]), float(sweep_hz[1])
    if not (hi > lo > 0.0 and f2 > f1 > 0.0):
        # No overlap, or a degenerate band — no occupancy term to compute.
        # `_ambient_rows_in_band` only yields overlapping rows, so this is
        # defense against a malformed band, not a live path.
        return SWEEP_PEAK_TO_RMS_DB
    return SWEEP_PEAK_TO_RMS_DB + 10.0 * math.log10(
        math.log(f2 / f1) / math.log(hi / lo)
    )


# Vocabulary for `RoleGainSolve.bound_by` — which limit chose the level.
GAIN_BOUND_FLAT_TARGET = "flat_target"          # room too noisy to back off
GAIN_BOUND_ROOM_SNR = "room_snr"                # the fit's own SNR need
GAIN_BOUND_PILOT_SNR = "pilot_snr"              # the quiet pilot's SNR guard
GAIN_BOUND_CAPTURE_FLOOR = "capture_floor"      # `DRIVER.peak_too_low_dbfs`
GAIN_BOUND_NO_AMBIENT_EVIDENCE = "no_ambient_evidence"  # disclosed fallback
# The ambient report was credible-looking but degenerate: every SNR arm
# resolved BELOW `DRIVER.peak_too_low_dbfs`, i.e. the room read so quiet that
# the math proposed a sweep too faint to trust. A disclosed refusal to solve,
# never a shipped level — see `_solve_role_gain`.
GAIN_BOUND_DEGENERATE_AMBIENT = "degenerate_ambient"
GAIN_BOUNDS = frozenset({
    GAIN_BOUND_FLAT_TARGET,
    GAIN_BOUND_ROOM_SNR,
    GAIN_BOUND_PILOT_SNR,
    GAIN_BOUND_CAPTURE_FLOOR,
    GAIN_BOUND_NO_AMBIENT_EVIDENCE,
    GAIN_BOUND_DEGENERATE_AMBIENT,
})

# Behavioral linearity tolerance, dB (design §3.4): captured delta within this
# of the programmed delta. Measured band-relative + ambient-compensated (see
# `_pilot_observations`), never as a full-band PEAK: LF room rumble ~30 dB
# above the tweeter-band ambient otherwise inflates the quiet woofer pilot's
# peak and compresses the captured delta, tripping this tight a tolerance on a
# perfectly linear driver.
LINEARITY_TOLERANCE_DB = 0.5

# Pilot edge-fade trim, seconds: `sweep.synchronized_swept_sine` applies a
# fixed 5 ms fade-in/fade-out to every stimulus it generates, to avoid a click
# at a non-zero-crossing edge, so a located pilot segment ramps up/down over
# that span rather than playing at full level throughout. Trimming exactly that
# span from each edge before measuring level keeps the RMS estimate to the
# pilot's steady-state portion. Read from the generator, not guessed.
PILOT_FADE_TRIM_S = 0.005

# Low-SNR honest routing (see `_pilot_observations`): ambient power subtraction
# only removes the room's noise-floor BIAS when the quiet (lo) pilot's own
# in-band power clears the in-band ambient power by enough margin that residual
# bias from ambient NONSTATIONARITY — the room's true noise power during the
# ~0.8 s pilot window can differ from the value measured over the program's
# separate, earlier ambient window — stays a small fraction of
# `LINEARITY_TOLERANCE_DB`. Modeling that mismatch as a bounded multiplicative
# factor ``k = 10**(AMBIENT_NONSTATIONARITY_DB/10)`` on the ambient power
# estimate:
#
#   subtracted signal estimate      Ŝ = P_measured − N̂
#   bias if the room's ACTUAL noise power during the pilot window is k·N̂
#   instead of N̂:                   bias_power = (k − 1) · N̂
#   bias in dB at signal level S (small-signal slope 10/ln(10)/S):
#       bias_db ≈ (10 / ln(10)) · (k − 1) / (S / N̂)      [S/N̂ = linear SNR]
#
# Budgeting `LINEARITY_SNR_BIAS_BUDGET_FRACTION` of the tolerance for this bias
# (leaving the rest for ordinary estimator/measurement jitter) and solving for
# the linear SNR gives the minimum trustworthy in-band SNR: `PILOT_MIN_SNR_DB`
# works out to ≈12.4 dB with the constants below. Real jts3 hardware captures
# measure ≈26-30 dB of in-band SNR on the quiet woofer pilot when measured in
# its own band, so this floor exists for the genuinely marginal case (very
# quiet phone/room), not the common one.
AMBIENT_NONSTATIONARITY_DB = 3.0
LINEARITY_SNR_BIAS_BUDGET_FRACTION = 0.5
_pilot_snr_k = 10.0 ** (AMBIENT_NONSTATIONARITY_DB / 10.0)
_pilot_snr_linear_min = (10.0 / math.log(10.0)) * (_pilot_snr_k - 1.0) / (
    LINEARITY_TOLERANCE_DB * LINEARITY_SNR_BIAS_BUDGET_FRACTION
)
PILOT_MIN_SNR_DB = 10.0 * math.log10(_pilot_snr_linear_min)

# Channel-map discriminator TARGET rise, dB (see `_channel_map_ok`). An
# ABSOLUTE floor, deliberately: a driver whose declared band never rose over
# the room did not play, at any measurement level. Derived from a jts3 hardware
# table — woofer pilots showed +22-30 dB TARGET rise, tweeter pilots +27 dB.
CHANNEL_MAP_TARGET_RISE_DB = 12.0

# The CROSS test is a RATIO, not an additive bound. An additive `cross_rise >=
# 6.0` is tuned against one measurement frame's room floor, which MASKS the
# cross-band content an honest capture always carries; raise the session level,
# the mask lifts, and the SAME healthy speaker fails a `channel_map_mismatch`
# hard stop that tells its household to rewire it. One speaker, one basin-2
# config, byte-identical graph:
#
#   session ref   seat SPL   woofer target/cross   tweeter target/cross  verdict
#   -27.5           68.1 dB    53.4 / -0.79          (healthy)            pass
#    -9.77          73.3 dB    48.5 /  4.13          71.7 / 10.81         FAIL
#    -6.80          78.6 dB    51.4 /  7.27          73.1 / 15.23         FAIL
#
# The cross energy is not electrical crosstalk, and that was MEASURED: a
# two-level discriminator through the BASELINE graph held cross-band rise at
# <=3 dB at both the -16.8 and -6.8 faders while own-band rises tracked the
# fader exactly. Through the per-driver ROUTING graph (which strips no
# crossover filters) CHECK instead sees program-segment SKIRT content plus
# modest driver nonlinearity — content at a roughly FIXED RELATIVE level, which
# an additive bound cannot describe and a ratio can. `target_rise - cross_rise`
# on those rows reads 54.2 / 44.4 / 60.9 / 44.1 / 57.9 dB: flat across a 10.5 dB
# span of level.
#
# WHAT THIS HALF CATCHES, measured rather than assumed: NOT mis-wiring. Seven
# wiring shapes moved the cross rise by within +/-0.4 dB, because a wiring
# fault changes which DRIVER radiates, not which BAND carries the energy. The
# `TARGET` floor above is the mis-wire catcher. The CROSS half guards ABNORMAL
# CROSS-BAND ENERGY — bleed, skirt and nonlinearity classes, and the degenerate
# case of one signal reaching both bands at once — and fails closed on any of
# them.
#
# Why 12.0: >=32 dB of margin under the hardware table above, and the quietest
# capture the ratio is judged on still clears it. It refuses the degenerate
# both-bands case (~0 dB) and a heavy bleed (10 dB). A LARGER bound is not free
# — it raises the judged threshold below, shrinking the region where the cross
# half looks at all — so 12.0 also buys the widest honest coverage.
# PROVISIONAL, like its neighbours.
CHANNEL_MAP_MIN_ISOLATION_DB = 12.0

# The ratio is only JUDGED once the target cleared its own floor by at least
# the isolation the ratio demands; below that it is not a meaningful quantity.
#
# The CROSS test refuses when `target_rise - cross_rise < BOUND`, i.e. when
# `target_rise < BOUND + cross_rise`. So it does not merely coexist with the
# TARGET floor — it RAISES it, to `max(FLOOR, BOUND + cross_rise)`, eating the
# floor by `cross_rise` dB. Any positive cross rise therefore pushes some band
# of quiet-but-correct captures into a rewire hard stop, and a bound at or
# below `CHANNEL_MAP_TARGET_RISE_DB` does NOT prevent that (that argument holds
# only at cross_rise <= 0). Measured end-to-end: a capture at target 13.50 /
# cross 1.72 yields isolation 11.78, refusing as the NON-retriable
# `channel_map_mismatch` — a hard stop telling a household to open its speaker,
# on a capture whose only real problem was that it was quiet.
#
# The guard makes the refusal self-justifying: above this threshold, a CROSS
# refusal implies `cross_rise >= CHANNEL_MAP_TARGET_RISE_DB` — the WRONG band
# cleared the very bar we demand of a driver that played, which nothing quiet
# can manufacture.
#
# The residual, named rather than hidden: for `target_rise` in
# [FLOOR, FLOOR + BOUND) the cross half is not judged at all, so abnormal
# cross-band energy on a quiet capture goes unremarked. A capture that quiet
# still has `snr_floor` and the TARGET floor in front of it.
CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB = (
    CHANNEL_MAP_TARGET_RISE_DB + CHANNEL_MAP_MIN_ISOLATION_DB
)

# VERIFY tracking-error smoothing: 1/6-octave, the constant design §5.2 names
# for the pass/fail comparison.
VERIFY_TRACKING_SMOOTHING_FRACTION = 6

# VERIFY tracking MAX comparator: a bin is excluded from the max-tracking
# comparator when the PREDICTED sum sits more than this many dB below its own
# median level over the tracking band. Inside a predicted interference notch,
# depth agreement is hypersensitive to sub-dB/sub-degree branch differences and
# is not a meaningful tracking signal — one hardware failure at 3.05 dB rms /
# 27.83 dB max was entirely a shifted predicted notch, not a broadband
# divergence. RMS stays full-band — see `_analyze_verify`.
VERIFY_NOTCH_EXCLUSION_DB = 12.0

# Three flatness claims live in the system, and which owns what is fixed:
#
#   * "is the speaker flat?" -> `jasper.active_speaker.flat_spec`'s cloud gauge
#     (`evaluate_flat_spec` + `spec_flatness_gauge`), a spatial power mean over
#     the post-apply positions self-referenced to its own low-mid mean
#     (`flat_spec.REFERENCE_BAND_HZ`) and graded in `SPEC_BANDS`.
#   * "did THIS crossover track its prediction?" -> `verify_tracking` below.
#   * "did THIS crossover hand off as designed?" -> `verify_absolute` below,
#     and only that record.
#
# Three structural reasons the cloud gauge cannot own the last two, not
# preferences: it is assembled when a position GROUP CLOSES, after
# `_verify_verdict` has run, so it does not exist when VERIFY is graded;
# Express and the driver-only path have no post-apply cloud at all, so it never
# exists there; and its spatial mean partially fills a design-axis null in (an
# 8-position mean measured shallower than any single position). Its
# self-reference is also the wrong zero — a crossover dip competes with every
# other graded deviation for the worst-band pointer — where the candidate's own
# crossover transfer reads the region directly.
#
# `CrossoverCandidate.flatness_improvement_db` is an UNRELATED Layer-1b metric
# (what the (polarity, delay) objective bought over the correlation seed), not
# a spec claim, so nothing here is named bare "flatness".

ANALYSIS_KIND = "jts_program_analysis"


@dataclass(frozen=True)
class MeasurementGeometry:
    """Declared physical geometry the analysis corrects for.

    ``mic_distance_m`` is the prescribed on-axis mic distance (~1 m, design
    §5.2); ``driver_spacing_m`` is the declared woofer↔tweeter spacing. Their
    deterministic parallax ``(√(r²+d²)−r)/c`` is subtracted from the measured
    delay so what remains is the electrical branch delay to apply.
    """

    driver_spacing_m: float = 0.0
    mic_distance_m: float = 1.0
    speed_of_sound_m_s: float = DEFAULT_SOUND_SPEED_M_S

    def parallax_us(self) -> float:
        """The deterministic mic-parallax term, in µs.

        Aim assumption (design §5.2's placement prompt): the mic sits ON the
        reference axis — the tweeter's axis at tweeter height — at distance
        ``r = mic_distance_m``, so the tweeter path is exactly ``r`` and the
        woofer (``d = driver_spacing_m`` off-axis) carries the full geometric
        excess ``√(r²+d²) − r``. That excess inflates the measured
        woofer-minus-tweeter arrival difference; subtracting it leaves the
        electrical branch delay. A mic placed off the tweeter axis splits the
        excess between the drivers and this correction over-counts.
        """
        r = float(self.mic_distance_m)
        d = float(self.driver_spacing_m)
        c = float(self.speed_of_sound_m_s)
        if r <= 0 or c <= 0 or d <= 0:
            return 0.0
        extra_m = math.sqrt(r * r + d * d) - r
        return extra_m / c * 1e6


@dataclass(frozen=True)
class AppliedAlignment:
    """What the APPLIED graph says about its own inter-driver delay.

    ``delay_us`` is that delay in :class:`AlignmentEstimate`'s signed frame, or
    ``None`` when a graph is applied but its record does not say — a partial or
    hand-edited profile.

    A wrapper rather than a bare ``float | None`` because the question has
    THREE answers: absent (``MeasurementPriors.applied_alignment is None``) is
    "nothing is commissioned, so the design's own answer stands", while
    ``AppliedAlignment(None)`` is "something IS playing and we cannot say
    what". Collapsing them would let a persisted candidate claim the design
    asks for no delay on a speaker nobody checked.
    """

    delay_us: float | None


@dataclass(frozen=True)
class MeasurementPriors:
    """Per-analysis priors the program itself does not carry.

    ``crossover_fc_hz`` scopes the overlap band (trims / alignment / ripple)
    and the VERIFY window; ``align_search_ms`` bounds the delay search;
    ``target_capture_dbfs`` is the MEASURE capture-peak target the CHECK gain
    solve aims for. ``predicted_sum`` is the MEASURE-predicted summed magnitude
    ``(freqs_hz, magnitude_db)`` VERIFY compares against, built here from the
    RAW measured branches but OVERRIDDEN by the v2 session with a
    LINEARIZED-branch prediction whenever Layer-1a linearization was fitted:
    the emitted graph carries those correction filters, so a prediction that
    omits them reads a deterministic mismatch equal to their own in-band
    response (~1.7 dB measured on JTS3, against a ±1.5 dB tolerance).

    ``measure_excited_band_hz`` carries the band every MEASURE branch was
    actually swept over forward to VERIFY, whose tracking comparison must trust
    the SAME band ``predicted_sum`` was built in (see ``overlap_band_hz``); a
    wider band would compare real VERIFY capture data against sub-floor noise
    inherited from a branch MEASURE never drove there. ``None`` falls back to
    the unclamped band. ``alignment_delay_bounds_us`` is the
    unsigned, declaration-derived applied-delay magnitude range the flatness
    refinement may search, derived by the session from the crossover region's
    ``delay_range_ms``; the drift-corrected physical peak gap orients and
    centers one ±half-period signed lobe inside it, and ``None`` keeps GCC as
    the applied-delay estimate.

    Two priors are facts the host holds and hands down rather than ones this
    module could reach for, and each is read on exactly one path,
    :func:`_select_alignment_pair`:

    * ``applied_alignment`` (:class:`AppliedAlignment`) is what THIS SPEAKER
      ALREADY PLAYS, read off the applied Layer-A profile. It is committed only
      on the low-SNR refusal, in place of a fresh number from a capture the SNR
      verdict just called unusable for alignment; it is never a seed, a bound,
      or a prior on the scored path, because a capture good enough to score
      must not be pulled toward the answer the speaker already has. ``None``
      (every phase but MEASURE, and a speaker with nothing commissioned) means
      the refusal commits ``0.0``, which IS the declared design when nothing
      declares otherwise; an ``AppliedAlignment`` with no ``delay_us`` commits
      the same ``0.0`` under a different objective, that number then being a
      fallback rather than the design's answer.
    * ``explicit_alignment_delay_us`` is the host-validated inter-driver delay
      PRESCRIPTION for this session, in :class:`AlignmentEstimate`'s signed
      frame, computed from a named measured basis and bounded to ±half a period
      at Fc from it
      (:func:`jasper.active_speaker.crossover_v2.alignment_prescription.read_alignment_prescription`).
      It is committed as the delay, and the flat-sum objective keeps the
      polarity unless ``explicit_alignment_polarity_sign`` says otherwise.
      ``None`` leaves the automatic selection byte-identical. A bare ``float |
      None`` rather than ``AppliedAlignment``'s wrapper because this question
      has only TWO answers: an unreadable prescription is refused at the
      boundary with a named reason and never becomes a prior.

    ``explicit_alignment_polarity_sign`` is the OPTIONAL other half of that
    prescription: ``+1``/``-1`` in this module's own polarity frame
    (:func:`polarity_label`). It pins the BASIN a fit may solve in, because
    delay and polarity are degenerate on axis — invert plus half a period at Fc
    sums almost identically — so a re-fit at one physical configuration can
    otherwise hop basins between rounds and turn a one-variable round into two.
    It constrains the same selection at the same point, so the trims and the
    delay re-solve UNDER the pin rather than being edited after it. Never set
    without ``explicit_alignment_delay_us``.

    ``mic_tier`` is the correction-envelope trust tier
    (``jasper.active_speaker.linearization_envelope.MIC_TIERS`` — "reference" /
    "consumer" / "phone") the measurement mic resolved to. ``None`` (CHECK and
    VERIFY priors, which never set it) means "no tier known", which the v2
    session's Layer-1a linearization gate treats as ineligible, never a guess.
    """

    crossover_fc_hz: float | None = None
    align_search_ms: float = DEFAULT_ALIGN_SEARCH_MS
    target_capture_dbfs: float = DEFAULT_TARGET_CAPTURE_DBFS
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    ambient_report: Mapping[str, Any] | None = None
    measure_excited_band_hz: tuple[float, float] | None = None
    alignment_delay_bounds_us: tuple[float, float] | None = None
    applied_alignment: AppliedAlignment | None = None
    explicit_alignment_delay_us: float | None = None
    explicit_alignment_polarity_sign: int | None = None
    mic_tier: str | None = None
    # Host-evaluated transfers, NOT product objects: the kernel may not import
    # jasper.active_speaker, so it is handed `freqs -> complex response`.
    measurement_protection_response_by_role: Mapping[
        str, Callable[[np.ndarray], np.ndarray]
    ] | None = None
    configured_crossover_response_by_role: Mapping[
        str, Callable[[np.ndarray], np.ndarray]
    ] | None = None
    configured_polarity_sign_by_role: Mapping[str, int] | None = None
    # §4.2's candidate-required bins per role; see _measure_priors (host-owned).
    candidate_required_band_hz_by_role: Mapping[
        str, tuple[float, float]
    ] | None = None
    # Whether a resolved measurement-mic calibration curve was applied to this
    # capture. ``None`` (CHECK/VERIFY priors, which never set it) means "not
    # resolved either way" — never a guess that a calibration was applied. See
    # ``ProgramAnalysis.mic_calibrated`` for the one consumer.
    mic_calibrated: bool | None = None


@dataclass(frozen=True)
class SegmentLocation:
    """Where one program segment landed in the capture, and its integrity."""

    segment_id: str
    kind: str
    role: str | None
    scheduled_start: int
    located_start: int
    residual_samples: float
    confidence: float
    peak_dbfs: float
    clipped: bool


@dataclass(frozen=True)
class IntegrityCheck:
    """One capture-integrity question, and what this capture said about it.

    ``status`` is one of :data:`INTEGRITY_PASS` / :data:`INTEGRITY_FAIL` /
    :data:`INTEGRITY_NOT_EVALUATED`. ``reason`` is filled ONLY on
    ``not_evaluated`` and says why the check could not run — the clause this
    type exists for, rather than a ``dict[str, bool]`` in which "did not run"
    and "ran and passed" are the same value.
    """

    name: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class CaptureIntegrity:
    """What a VERIFY capture's own timeline says about whether it is usable.

    The evidence half of the record (``locate_confidence_min``,
    ``schedule_residual_ms_worst``, ``clipped_segments``) is reported whether
    or not the check drawn from it ran: a summed sweep the locator could barely
    find still HAS a residual, and printing it beside a ``not_evaluated``
    schedule check is what stops a reader inferring a splice from a number that
    is really just noise.

    ``failed`` / ``not_evaluated`` / ``glitched`` are derived from ``checks``
    rather than stored, so the summary can never disagree with the checks it
    summarizes. ``checks`` is ordered most-fundamental-first, which is also the
    order a consumer should route on: a sweep nobody heard explains its own
    residual, so "not heard" outranks "off schedule".

    ``None`` where a :class:`ProgramAnalysis` carries no record at all means
    "no evidence" — the same convention ``linearity_ok`` / ``pilot_snr_ok``
    use — and never "clean".
    """

    checks: tuple[IntegrityCheck, ...] = ()
    locate_confidence_min: float | None = None
    # SIGNED (mirrors ``crossover_v2_flow._sweep_schedule_diag_fields``): the
    # direction the schedule broke in is half the forensic value — positive
    # means the sweep arrived LATE, the insertion shape.
    schedule_residual_ms_worst: float | None = None
    clipped_segments: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if c.status == INTEGRITY_FAIL)

    @property
    def not_evaluated(self) -> tuple[str, ...]:
        return tuple(
            c.name for c in self.checks if c.status == INTEGRITY_NOT_EVALUATED
        )

    @property
    def glitched(self) -> bool:
        """True when at least one EVALUATED check failed.

        A record of nothing but ``not_evaluated`` is not glitched — it is
        unexamined, and the ``not_evaluated`` list is how a reader tells the
        two apart.
        """
        return bool(self.failed)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe record for a verdict payload / durable evidence."""
        return {
            "glitched": self.glitched,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    # Present only where there IS one, so a consumer cannot
                    # read an empty string as a stated reason.
                    **({"reason": c.reason} if c.reason else {}),
                }
                for c in self.checks
            ],
            "locate_confidence_min": self.locate_confidence_min,
            "schedule_residual_ms_worst": self.schedule_residual_ms_worst,
            "clipped_segments": list(self.clipped_segments),
        }


@dataclass(frozen=True)
class DriftEstimate:
    """In-capture clock-drift estimate + the glitch verdict (design §5.6.3).

    ``repeat_level_delta_db`` is the woofer-repeat in-band-RMS level
    disagreement (see ``_estimate_drift``) — one of the three glitch inputs,
    carried on the record so a durable diagnostic has it on BOTH a passing and
    a failing capture, not only on the WARN line a glitch fires.

    ``per_role_epsilon_ppm`` is a first-vs-LAST epsilon estimate for EVERY role
    with ≥2 located sweep occurrences — diagnostic only, never gated (only the
    woofer pair decides ``glitch_detected``). Empty for a role with <2
    occurrences.

    ``glitch_inputs`` names WHICH of the four bounds tripped —
    ``epsilon_out_of_bound`` / ``residual_desync`` / ``repeat_level_disagree``
    / ``timeline_slip`` — in that fixed order, empty on a clean capture. The
    verdict itself stays one user-facing reason by design (§5.2).

    ``discontinuity_samples`` / ``discontinuity_after_segment`` describe a
    single discrete timeline step when one explains the located sweeps: its
    signed size in samples (positive ⇒ everything after it arrived LATE) and
    the segment id it landed AFTER. ``0.0`` / ``""`` when no step is resolved
    on a capture whose sweeps were confidently located, including on a clean
    capture. ``DISCONTINUITY_UNRESOLVED`` (a `str`) / ``""`` when one or more
    located sweeps fell below ``SWEEP_LOCATE_CONFIDENCE_FLOOR`` instead: a step
    fitted from an unlocated sweep is a fabrication, not a clean reading, so it
    gets a distinct sentinel rather than a silent ``0.0``.

    The two populate exactly when ``timeline_slip`` fires, so they are the
    slip's own record. Read the MAGNITUDE: the sign and the segment id are
    ambiguous when the step lands at an even schedule index, which is why the
    gate reads ``abs()`` alone.
    """

    epsilon_ppm: float
    max_residual_samples: float
    glitch_detected: bool
    repeat_level_delta_db: float = 0.0
    per_role_epsilon_ppm: Mapping[str, float] = field(default_factory=dict)
    glitch_inputs: tuple[str, ...] = ()
    discontinuity_samples: float | str = 0.0
    discontinuity_after_segment: str = ""


@dataclass(frozen=True)
class DriverResponse:
    """One driver's gated complex response, calibrated if a cal was supplied.

    ``repeat_responses`` holds this SAME driver's additional located sweep
    occurrences — each independently deconvolved/gated/transformed exactly like
    the primary — in occurrence order, ``repeat_index`` 1, 2, …. Populated only
    on a PRIMARY response (built from the driver's first/canonical sweep,
    ``sweep_w``/``sweep_t``); a repeat's own ``DriverResponse`` carries an empty
    ``repeat_responses`` and its own ``repeat_index``. Diagnostic evidence
    only: nothing here feeds the candidate/trim/alignment math, which stays
    anchored to the primary response.
    """

    role: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    complex_tf: np.ndarray
    gating: dict[str, Any]
    snr: dict[str, Any] | None
    validity_floor_hz: float | None
    repeat_responses: tuple["DriverResponse", ...] = ()
    repeat_index: int | None = None


@dataclass(frozen=True)
class AlignmentEstimate:
    """Tweeter-vs-woofer relative delay + polarity (design §5.6.5).

    Sign convention: ``delay_us`` is ``(D_woofer − D_tweeter)`` after parallax
    removal, so a POSITIVE ``delay_us`` means the tweeter's acoustic arrival is
    EARLIER and the tweeter branch must be delayed by that amount to time-align
    the crossover.

    GCC is the capture-quality seed: ``seed_delay_us`` records that corrected
    delay, while ``delay_us`` is whatever :func:`_select_alignment_pair`
    COMMITTED — the flattest-summing grid point on a trusted capture, or, when
    the capture's SNR was refused for alignment, a delay that capture did not
    supply at all. ``confidence`` is therefore labelled
    ``confidence_source='gcc_phat_seed'``: the confidence belongs to the seed,
    not to whatever was committed. The selection's own ripple/snap evidence is
    stored separately on :class:`CrossoverCandidate`. ``raw_delay_us`` is the
    selected delay in the pre-parallax coordinate, preserving ``delay_us ==
    raw_delay_us - parallax_us``.

    ``anchor_delay_us`` is the drift-corrected physical peak-gap anchor (raw
    full-IR argmax gap − inter-sweep drift + parallax) in the signed frame. The
    aligner computes it once and OWNS it, so ``_build_candidate`` derives the
    applied anchor and the objective reference gap from it rather than
    re-running the argmax. ``snapped_delay_us`` is the fine-stage result: that
    anchor snapped to the nearest local maximum of the SAME upsampled GCC-PHAT
    correlation within ±(period/6) at Fc (:data:`GCC_SNAP_RADIUS_PERIODS`). It
    is ``None`` when the radius held no local maximum (the candidate then keeps
    the bare anchor); both are ``None`` when the seed was refused. Direct
    ``_build_candidate`` callers set ``anchor_delay_us`` explicitly.

    ``status`` is :data:`ALIGNMENT_OK` for a trustworthy estimate. When the
    correlation peak lands at (or within one sample of) the ±search-window
    edge, the true delay likely exceeds the geometry prior and the windowed
    peak is a clamped artifact — ``status`` is
    :data:`ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW` and ``confidence`` is forced
    to 0.0; callers must not apply ``delay_us`` from such a result.

    Polarity follows the same seed-then-selection shape as the delay.
    ``polarity``/``polarity_sign`` on a FRESH estimate are the GCC
    correlation's own answer; on the estimate ``_measure_analysis`` publishes
    they are the pair :func:`_select_alignment_pair` committed.
    ``polarity_agrees_with_sum`` is the cross-check: ``True``/``False`` once
    the flat-sum selection has answered it, ``None`` on an estimate nothing has
    cross-checked yet (a fresh return from :func:`_estimate_alignment`, or a
    hand-built one). A disagreement is ordinary operation — the correlation
    losing to the objective that owns the question — so it is recorded rather
    than raised.
    """

    delay_us: float
    raw_delay_us: float
    parallax_us: float
    polarity: str  # "normal" | "inverted"
    polarity_sign: int  # +1 | -1
    confidence: float
    status: str = ALIGNMENT_OK
    seed_delay_us: float | None = None
    confidence_source: str = "gcc_phat"
    anchor_delay_us: float | None = None
    snapped_delay_us: float | None = None
    polarity_agrees_with_sum: bool | None = None


@dataclass(frozen=True)
class CrossoverCandidate:
    """The proposed measured candidate (design §5.6.6).

    The (polarity, delay) pair is chosen jointly, on predicted summed blend
    flatness: :func:`_select_alignment_pair` scores both polarities across a
    delay grid centred on the drift-corrected physical peak-gap anchor and
    commits the flattest pair, while correlation — the GCC-PHAT polarity sign
    and the gated local-peak snap — supplies the SEED pair and the tie-break
    and is otherwise reported evidence. ``alignment_objective`` names what was
    committed (:data:`ALIGNMENT_COMMITMENTS`) and ``seed_polarity_sign`` is the
    correlation's own polarity answer, so a disagreement between the two is
    readable off the persisted candidate. ``left_anchor_lobe`` records that the
    committed delay sits outside the comb lobe the anchor owns — on the
    CANDIDATE and not only in the journal because a wrong-lobe commitment is
    magnitude-flat and time-wrong, so an on-axis VERIFY cannot see it and the
    receipt has to.

    ``anchor_delay_us`` is the bare anchor in the signed candidate convention,
    ``snap_delta_us`` is ``committed − anchor``, and ``snap_found`` records
    whether a local correlation peak existed inside the snap radius — the
    seed's own provenance, not the committed delay's.
    ``alignment_seed_ripple_db`` is the summed ripple at the SEED pair and
    ``flatness_improvement_db`` is ``seed_ripple − committed_ripple``: what the
    objective bought over correlation alone. On the flat-sum path it is
    non-negative by construction, since the seed pair is always one of the
    scored candidates. On the low-SNR path it can be NEGATIVE, and that is the
    disclosure working: the branch that would have argued for the seed is the
    one the capture called unmeasurable, so the commitment is the declared
    design and this number says what declining a noise-derived flatness claim
    cost on paper. On that path ``snap_delta_us`` records the disagreement and
    is NOT the residual the model carries — the shipped model withdraws the
    refused anchor and stays in the independently-aligned frame, so a timing
    claim the capture withdrew cannot phase the curve that refuses candidates
    and fails rounds (:func:`summed_model_residual_delay_us`).

    ``predicted_ripple_db`` is measured on the INDEPENDENTLY ALIGNED
    (zero-residual) branch sum AT THE COMMITTED POLARITY — the one quantity
    here that deliberately does not sit on the committed-delay model
    ``ProgramAnalysis.predicted_sum`` uses. The two are not in contradiction:
    the delay is the axis a candidate could use to talk itself under the
    threshold, and holding the residual at zero closes that, while polarity is
    not a continuum a capture can shop along and scoring coherence at a
    polarity the candidate does not ship makes a fine capture read as an
    incoherent one (14.13 dB once, for a pair that sums to a fraction of a dB
    the right way round). It asks a capture-quality question — how coherently
    can these two branches sum at all — and is the sole input to
    ``crossover_v2_flow``'s ``MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB``, which
    DISCLOSES rather than refuses. The frame still binds: a disclosure a
    candidate's own alignment could talk its way out of would be as dishonest
    as a veto that could be; see ``_build_candidate``'s comment at the two
    calls for the measured evasion margin.

    ``trim_db`` is the APPLIED trim (ripple-optimal where the polish ran and
    the sanity guard trusts it, otherwise the band-average fallback);
    ``trim_band_average_db`` preserves ``solve_branch_trims``'s own level-match
    result — the SEED the ripple-optimal search started from — so
    replay/forensics can always see both, even when they coincide.
    ``_build_candidate`` always sets it. A persisted candidate is evidence, not
    config, and is never migrated across a frame change: a speaker commissioned
    under an older frame keeps its trim until re-commissioned.
    """

    trim_db: Mapping[str, float]
    polarity: str
    delay_us: float
    predicted_ripple_db: float
    confidence: float
    alignment_seed_ripple_db: float | None = None
    flatness_improvement_db: float | None = None
    anchor_delay_us: float | None = None
    snap_delta_us: float | None = None
    snap_found: bool = False
    trim_band_average_db: Mapping[str, float] | None = None
    alignment_objective: str = ""
    seed_polarity_sign: int | None = None
    left_anchor_lobe: bool = False
    #: Did correlation's polarity answer survive the objective that chose it?
    #: Carried from :attr:`AlignmentPairSelection.polarity_agrees_with_sum`
    #: rather than re-derived, so this repository holds ONE opinion about which
    #: commitments answer that question. ``None`` means no flat sum ever
    #: answered it.
    polarity_agrees_with_sum: bool | None = None
    #: Did the REQUEST hold the polarity axis, rather than anything measuring it?
    #: Carried from :attr:`AlignmentPairSelection.polarity_pinned` for the same
    #: reason, and it exists because the household row has to word an operator's
    #: instruction differently from a measurement. NOT derivable from the two
    #: facts already here: ``alignment_objective`` is the same
    #: ``explicit_prescription_committed`` pinned or not, and
    #: ``polarity_agrees_with_sum is None`` is also what a seed-committed arm
    #: reports — whose polarity IS a measurement. ``False`` on a selection-less
    #: arm is therefore correct rather than a fallback: the seed shipped, so
    #: nothing was pinned.
    polarity_pinned: bool = False
    #: The ripple polish's SIGNED trim excursion, in dB, when it was REJECTED;
    #: ``None`` when nothing was thrown away.
    #:
    #: On the candidate and not only in the journal, for the reason
    #: :attr:`left_anchor_lobe` is: a rejection commits the band-average seed,
    #: so ``trim_db[tweeter] == trim_band_average_db[tweeter]`` — identical to
    #: an admitted polish that moved nothing, and to the one-sided skip that
    #: never ran a scan. Three outcomes, one signature; this separates the one
    #: where a flatness answer was computed and discarded.
    ripple_polish_rejected_delta_db: float | None = None


@dataclass(frozen=True)
class PilotObservation:
    """One driver's CHECK pilot pair — level, linearity, channel-map sanity.

    ``level_lo_dbfs``/``level_hi_dbfs`` are band-relative and
    ambient-compensated when an ambient window is available (see
    `_pilot_observations`). They feed ONLY the linearity verdict —
    ``captured_delta_db`` is a relative delta, so the ambient-subtraction bias
    cancels between the two levels — and must never feed an ABSOLUTE-level
    consumer like the MEASURE gain solve: ambient subtraction shifts the
    absolute value by however much ambient power was removed, which moved
    `_solve_gain_plan`'s captured-peak target 13-17 dB hotter on two real
    captures when it was tried. ``peak_lo_dbfs``/``peak_hi_dbfs`` are the
    dedicated NON-ambient-subtracted full-band levels `_solve_gain_plan` reads
    instead, preserving ``MeasurementPriors.target_capture_dbfs``'s capture-PEAK
    semantics.

    ``snr_valid`` is True when the quiet (lo) pilot's in-band SNR clears
    `PILOT_MIN_SNR_DB`, i.e. the ambient-subtracted estimate (and therefore
    ``linearity_ok``) is trustworthy; when False, ``linearity_ok`` is ``None``
    — UNKNOWN. An untrustworthy estimate must register neither as a linearity
    FAILURE (the caller routes on ``snr_valid`` instead, attributing the
    room/positioning cause rather than the phone's AGC) nor as a PASS: forced
    ``True`` is how a capture with a -60.9 dB captured delta against a
    programmed 10.0 dB once published ``linearity_ok=true``. It defaults to
    True so a caller constructing one directly without an opinion on SNR gets
    the "trust the delta" behavior.

    ``snr_db`` is the quiet-pilot in-band SNR estimate ``snr_valid`` is
    thresholded from (`_pilot_in_band_snr_db`), kept as a number so a
    diagnostic consumer can see how close a borderline capture ran. ``+inf``
    when there is no ambient window to validate against — nothing to distrust —
    matching ``snr_valid``'s default-True stance.

    ``channel_map_ok`` is tri-state for the reason ``linearity_ok`` is:
    ``None`` means the evidence to judge this role's map was not there, never
    that it passed. The fallback path reaches it — see `_channel_map_ok`.

    ``channel_map_target_rise_db``/``channel_map_cross_rise_db`` are the two
    RAW rise numbers `_channel_map_ok` computed on the way to
    ``channel_map_ok`` (this driver's own band above ambient, and the
    worst/failing other band's rise above ITS ambient). ``None`` on the
    fallback total-energy-fraction path, which has no rise concept and is what
    v2 MEASURE/VERIFY take, or — for the cross figure — when there are no other
    roles to compare against. Published raw rather than pre-collapsed because
    the ISOLATION RATIO that DECIDES the verdict (`channel_map_isolation_db`)
    is derivable from them while the reverse is not: only the raw pair says
    which half moved.

    ``programmed_hi_gain_db`` is the HI segment's own declared ``gain_db``,
    published here so a caller downstream can compute ``level_hi_dbfs -
    programmed_hi_gain_db`` (the capture chain's own transfer) WITHOUT binding
    back to the ``ExcitationProgram`` instance that produced this analysis.
    ``None`` for a construction site that predates this field — a consumer must
    treat that as "nothing to compare", never as ``0.0``.
    """

    role: str
    level_lo_dbfs: float
    level_hi_dbfs: float
    programmed_delta_db: float
    captured_delta_db: float
    linearity_ok: bool | None
    channel_map_ok: bool | None
    snr_valid: bool = True
    peak_lo_dbfs: float = DBFS_FLOOR
    peak_hi_dbfs: float = DBFS_FLOOR
    snr_db: float = math.inf
    channel_map_target_rise_db: float | None = None
    channel_map_cross_rise_db: float | None = None
    programmed_hi_gain_db: float | None = None


@dataclass(frozen=True)
class RoleGainSolve:
    """One driver's MEASURE level solve, and the evidence it rests on.

    ``gain_db`` is the digital gain the MEASURE composer will actually schedule
    for this role; ``flat_target_gain_db`` is the level-only figure (land the
    capture peak on ``MeasurementPriors.target_capture_dbfs``, clamped by the
    ≥6 dB digital guard). The solve never exceeds that flat figure, so the pair
    is also the disclosure of how much quieter this session's MEASURE got.

    ``bound_by`` names which limit chose the number — one of the
    ``GAIN_BOUND_*`` constants. It is the honesty field: a
    ``GAIN_BOUND_NO_AMBIENT_EVIDENCE`` solve is the disclosed fallback to the
    flat target, never a silent guess, and ``GAIN_BOUND_FLAT_TARGET`` means the
    room was noisy enough that the SNR requirement wanted at least the flat
    level, so nothing moved.

    ``ambient_dbfs`` / ``required_snr_db`` / ``crest_factor_db`` /
    ``required_capture_dbfs`` are the ROOM-SNR demand and only that: the worst
    overlapping ambient band's level, the SNR this solve demanded above it, the
    stimulus crest factor that converts that band-RMS demand into the
    capture-PEAK units everything else here uses (see
    :func:`sweep_band_crest_factor_db`), and their sum. A coherent quadruple —
    the last is the first three added — and they stay the room demand even when
    a DIFFERENT arm won, so on ``GAIN_BOUND_PILOT_SNR`` or
    ``GAIN_BOUND_DEGENERATE_AMBIENT`` ``required_capture_dbfs`` is the room
    demand that arm overrode, not the capture peak aimed at. All four are
    ``None`` on the no-evidence fallback — a missing number is never a zero —
    and ``crest_factor_db`` is additionally ``None`` on a solve persisted
    before the demand carried a crest term at all.
    """

    role: str
    gain_db: float
    flat_target_gain_db: float
    bound_by: str
    band_hz: tuple[float, float] | None = None
    ambient_dbfs: float | None = None
    required_snr_db: float | None = None
    required_capture_dbfs: float | None = None
    crest_factor_db: float | None = None

    @property
    def reduction_db(self) -> float:
        """How much quieter than the flat target this solve is (≥ 0)."""
        return self.flat_target_gain_db - self.gain_db

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "gain_db": round(self.gain_db, 3),
            "flat_target_gain_db": round(self.flat_target_gain_db, 3),
            "reduction_db": round(self.reduction_db, 3),
            "bound_by": self.bound_by,
            "band_hz": (
                [round(self.band_hz[0], 1), round(self.band_hz[1], 1)]
                if self.band_hz is not None else None
            ),
            "ambient_dbfs": (
                round(self.ambient_dbfs, 2) if self.ambient_dbfs is not None else None
            ),
            "required_snr_db": (
                round(self.required_snr_db, 2)
                if self.required_snr_db is not None else None
            ),
            "required_capture_dbfs": (
                round(self.required_capture_dbfs, 2)
                if self.required_capture_dbfs is not None else None
            ),
            "crest_factor_db": (
                round(self.crest_factor_db, 2)
                if self.crest_factor_db is not None else None
            ),
        }


@dataclass(frozen=True)
class GainPlan:
    """Solved MEASURE digital gains (design §5.2).

    ``role_solves`` carries the per-role derivation behind ``gain_db`` — see
    :class:`RoleGainSolve`. Empty for a construction site that predates the
    field; a consumer must read that as "no derivation published", never as "no
    reduction happened".
    """

    gain_db: Mapping[str, float]
    predicted_peak_dbfs: float
    snr_floor_ok: bool
    role_solves: Mapping[str, RoleGainSolve] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgramAnalysis:
    """The deterministic result of one ``(program, capture)`` pair."""

    phase: str
    program_id: str
    locations: tuple[SegmentLocation, ...]
    drift: DriftEstimate | None = None
    driver_responses: tuple[DriverResponse, ...] = ()
    alignment: AlignmentEstimate | None = None
    candidate: CrossoverCandidate | None = None
    ambient_report: dict[str, Any] | None = None
    # Pure passthrough of MeasurementPriors.mic_tier, set only by
    # _analyze_measure; CHECK and VERIFY analyses never set it. See
    # MeasurementPriors.mic_tier for the trust-tier vocabulary and the "None
    # means unknown, never a guess" contract.
    mic_tier: str | None = None
    # True when §4.2's `M*C/P` composition ran, so these responses carry the
    # crossover shoulders the fitter's branch-input invariant assumes. Set only
    # by _analyze_measure; the fitter refuses an uncomposed protected-neutral
    # capture, which measured drivers that were never crossed.
    configured_path_composed: bool = False
    pilots: tuple[PilotObservation, ...] = ()
    linearity_ok: bool | None = None
    channel_map_ok: bool | None = None
    # Aggregate of ``PilotObservation.snr_valid`` across pilots (``all(...)``);
    # ``None`` when there are no pilots (same "no evidence" convention as
    # ``linearity_ok``). False means at least one pilot's quiet-side in-band
    # SNR was too low to trust the ambient-subtracted linearity estimate —
    # the session routes this to `REASON_SNR_FLOOR` (CHECK) or
    # `REASON_PILOT_LEVEL_COLLAPSE` (MEASURE / cloud / VERIFY), never to
    # `REASON_AGC_BEHAVIORAL_FAIL`.
    pilot_snr_ok: bool | None = None
    gain_plan: GainPlan | None = None
    summed_response: DriverResponse | None = None
    summed_ripple_db: float | None = None
    # How deep the notch at Fc sits below the shoulders either side of it, on
    # THIS capture's summed curve. The same subtraction whether or not a branch
    # rode the graph inverted; what it MEANS is the caller's polarity context,
    # never this field's.
    reverse_null_depth_db: float | None = None
    # Measured-vs-predicted scalars for one VERIFY capture, plus the ``"frame"``
    # the two curves were compared ACROSS and the tilt-removed twins of the two
    # numbers a gate or a screen reads. The raw scalars keep their meaning and
    # their value exactly; see :func:`_analyze_verify`'s frame-discipline block
    # for why the tilt is disclosed rather than corrected for.
    verify_tracking: dict[str, Any] | None = None
    # The ABSOLUTE crossover-region result for one VERIFY capture — see
    # ``_verify_absolute_result``. Its own field rather than a key inside
    # ``verify_tracking``: different reference, different presence condition
    # (tracking needs ``predicted_sum``, this needs the candidate's crossover
    # transfers), and folding them together is how a defect the MODEL also
    # predicts stays invisible. Always a dict on a VERIFY analysis — the
    # numbers, or ``{"not_evaluated": <reason>}``; ``None`` elsewhere.
    verify_absolute: dict[str, Any] | None = None
    # Why a MEASURE analysis carries no ``alignment`` and no ``candidate``;
    # ``None`` on every analysis that has them, so absence cannot read as pass.
    measure_pair_not_evaluated: str | None = None
    # The SMOOTHED ``(freqs_hz, measured_db, predicted_db)`` triple the tracking
    # scalars above were reduced from. A separate field rather than a key inside
    # ``verify_tracking`` because that dict travels to the phone in a
    # PhaseVerdict payload and these are full curves.
    #
    # It exists so the delta probe (:mod:`jasper.active_speaker.delta_probe`)
    # grades the SAME comparison the tracking gate does — one
    # measured-vs-predicted construction, two consumers reading it over
    # different bands — rather than re-deriving its own from the raw curves.
    #
    # ``None`` whenever ``verify_tracking`` is (no prediction prior), and the
    # probe reads that as "no evidence", never as a pass.
    verify_tracking_curve: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    # MEASURE-predicted summed magnitude ``(freqs_hz, magnitude_db)``: the
    # measured branches at the candidate's COMMITTED trim AND committed delay
    # (``_build_candidate``'s ``predicted_applied``), or the single branch
    # itself on a 1-way main. The v2 session hands this
    # to the VERIFY analysis as ``MeasurementPriors.predicted_sum`` so VERIFY's
    # PASS is |measured − predicted| ≤ ±1.5 dB (design §5.2), not merely the
    # summed ripple.
    #
    # It carries the delay, so the tracking comparison grades model FIDELITY —
    # did the emitted graph do what was modelled — against a target a
    # realizable delay actually produces, superseding design §5.6.6's
    # zero-residual intent. Quality is graded separately and elsewhere:
    # ``crossover_v2_flow.spec_report_for_predicted_sum`` against the flat
    # spec, and ``CrossoverCandidate.predicted_ripple_db`` (the
    # independently-aligned instrument) at the capture-quality threshold.
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    # Set by MEASURE from ``drift.glitch_detected`` and by VERIFY from
    # ``capture_integrity.glitched`` — in BOTH cases a one-bit projection of a
    # richer record that owns the fact, assigned at the single construction
    # site so the two can never disagree.
    glitch_detected: bool = False
    # The per-check VERIFY capture-integrity record ``glitch_detected``
    # summarizes — including the checks that could NOT run here and why. Set by
    # ``_analyze_verify`` on every VERIFY-phase analysis, which is also every
    # spatial-cloud position, since those replay the verify program. ``None`` on
    # CHECK/MEASURE: "no evidence", never "clean" — see :class:`CaptureIntegrity`.
    capture_integrity: CaptureIntegrity | None = None
    # End-to-end frame accounting for the capture that produced this analysis.
    # Set on EVERY phase by ``analyze_program_capture``, unlike
    # ``capture_integrity`` above: a hop's ±128-frame loss was found in a
    # MEASURE session, and a record that only existed on VERIFY would have been
    # absent exactly where the defect was. Only VERIFY turns it into graded
    # ``CaptureIntegrity`` checks, because MEASURE's ``glitch_detected`` has a
    # single owner (``DriftEstimate``) and a second writer would break the
    # one-bit-projection invariant that field's comment states. ``None`` only on
    # analyses built directly rather than through ``analyze_program_capture``.
    frame_ledger: FrameLedger | None = None
    # True when `_resolve_anchor` could not tell this capture's competing
    # timeline interpretations apart — every number below was computed at
    # windows the anchor may have placed a whole pilot spacing from where the
    # drivers actually played, so none of them attributes energy to a driver
    # reliably. Set on EVERY phase, for ``frame_ledger``'s reason: the fact
    # belongs to the capture, not to the phase that exposed it. Only CHECK reads
    # it, being the phase whose pilots re-locate onto EACH OTHER under the shift
    # and so produce a confident-looking wiring verdict instead of an honest
    # "not found" (MEASURE/VERIFY search a sweep that is not there and refuse as
    # ``locate_failed``). ``False`` — not ``None`` — is the default: a capture
    # nothing arbitrated (one candidate, or no witness) is unambiguous by
    # construction, not un-evidenced.
    anchor_ambiguous: bool = False
    # Pure passthrough of MeasurementPriors.mic_calibrated, mirroring
    # ``mic_tier`` above and set at the same site (_analyze_measure): whether a
    # resolved measurement-mic calibration curve was applied to this capture.
    # ``None`` (CHECK/VERIFY analyses, which never set it) means "not resolved
    # either way" — the v2 session's MEASURE disclosure treats only an explicit
    # ``False`` as reservation-worthy, never a guess.
    mic_calibrated: bool | None = None


# --------------------------------------------------------------------------- #
# low-level signal helpers
# --------------------------------------------------------------------------- #


def _peak_dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return DBFS_FLOOR
    peak = float(np.max(np.abs(x)))
    if peak <= 0 or not math.isfinite(peak):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * math.log10(peak))


def _has_clipped_run(
    x: np.ndarray, *, threshold: float = CLIP_ABS_THRESHOLD, run: int = CLIP_RUN_SAMPLES
) -> bool:
    """True if ``x`` has a run of ``run`` consecutive samples at ≥ full scale."""
    if x.size < run:
        return False
    at_fs = np.abs(x) >= threshold
    if not bool(np.any(at_fs)):
        return False
    # Longest run of True via reset-on-False cumulative counting.
    count = 0
    for flag in at_fs:
        if flag:
            count += 1
            if count >= run:
                return True
        else:
            count = 0
    return False


def _locate(
    capture: np.ndarray,
    stimulus: np.ndarray,
    *,
    sample_rate: int,
    max_capture_s: float,
):
    """Matched-filter ``stimulus`` in ``capture``; return the alignment result."""
    return cross_correlation_alignment(
        capture,
        stimulus,
        sample_rate=sample_rate,
        max_capture_s=max_capture_s,
    )


def _analytic_envelope(x: np.ndarray) -> np.ndarray:
    """Delegates to :func:`gating.analytic_envelope` — one implementation."""
    return gating.analytic_envelope(x)


def _subsample_separation(
    capture: np.ndarray,
    arrival_a: int,
    arrival_b: int,
    length: int,
) -> float:
    """Sub-sample separation ``arrival_b − arrival_a`` of two identical stimuli.

    Cross-correlates the two captured windows (same stimulus + same room IR, so
    the peak is sharp) and refines it on the upsampled analytic envelope —
    Gamper's repeat-ratio idea. Returns the refined ``(arrival_b − arrival_a)``.
    """
    from scipy.signal import correlate

    a = np.asarray(capture[arrival_a:arrival_a + length], dtype=np.float64)
    b = np.asarray(capture[arrival_b:arrival_b + length], dtype=np.float64)
    n = min(a.size, b.size)
    if n < 8:
        return float(arrival_b - arrival_a)
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    corr = correlate(b, a, mode="full", method="fft")
    env = _analytic_envelope(corr)
    peak = int(np.argmax(env))
    refined = parabolic_peak(env, peak)
    lag = refined - (n - 1)  # b ≈ a shifted right by lag
    return float((arrival_b - arrival_a) + lag)


def _complex_tf(
    ir: np.ndarray,
    sample_rate: int,
    *,
    n_fft: int,
    calibration: "CalibrationCurve | None",
):
    """Complex TF of an IR on a fixed grid, with the mic cal folded in (real)."""
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    H = np.fft.rfft(ir, n=n_fft)
    if calibration is not None:
        correction_db = calibration_mod.apply_calibration_curve(
            freqs, np.zeros_like(freqs), calibration
        )
        H = H * np.power(10.0, correction_db / 20.0)
    return freqs.astype(np.float64), H


def _band_average_db(freqs: np.ndarray, magnitude_db: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        raise ValueError("overlap band has no frequency bins")
    power = np.power(10.0, magnitude_db[mask] / 10.0)
    return 10.0 * math.log10(max(float(np.mean(power)), 1e-12))


# --------------------------------------------------------------------------- #
# locate + integrity (all phases)
# --------------------------------------------------------------------------- #


def _earliest_strong_peak(
    capture: np.ndarray,
    stimulus: np.ndarray,
    *,
    frac: float = 0.6,
    band_hz: tuple[float | None, float | None] | None = None,
    sample_rate: int | None = None,
) -> int:
    """Index of the EARLIEST normalized-correlation peak within ``frac`` of max.

    The first stimulus can be bit-identical to a later repeat (MEASURE's woofer
    pair) or share its SHAPE at a different level (CHECK's lo/hi pilot pair), so
    a plain global argmax — or a raw-amplitude threshold — can lock onto the
    wrong occurrence. This uses a locally energy-normalized matched filter
    (cosine similarity at each lag), so a quieter-but-identical first occurrence
    scores the same as a louder later one; taking the earliest lag within
    ``frac`` of the max then picks the true first occurrence, while an
    out-of-band interloper stays below the fraction.

    ``band_hz`` (with ``sample_rate``) restricts that similarity to the
    stimulus's OWN declared band — the ``(f1_hz, f2_hz)`` its
    :class:`~jasper.audio_measurement.program.ProgramSegment` carries, the same
    declaration `_channel_map_ok` and `_band_power` read. Without it the
    denominator is the capture's TOTAL local energy, so room noise the stimulus
    never occupied suppresses a quiet member's score: a quiet
    ``pilot_woofer_lo`` once scored 0.3932 against a 0.4176 gate while its
    IN-BAND SNR, 27.6 dB, was better than a round that had passed at 26.9 dB,
    latching the gate onto ``pilot_woofer_hi`` and sliding every analysis window
    one pilot spacing. A caller with no band to declare — or one whose band
    survives no FFT bin at this rate — keeps the full-band behavior exactly.
    """
    from scipy.signal import correlate

    cap = np.asarray(capture, dtype=np.float64)
    stim = np.asarray(stimulus, dtype=np.float64)
    cap = cap - cap.mean()
    stim = stim - stim.mean()
    L = stim.size
    if cap.size < L or L == 0:
        return 0
    if (
        band_hz is not None
        and sample_rate
        and band_hz[0] is not None
        and band_hz[1] is not None
    ):
        cap_b = _bandlimit(cap, sample_rate, band_hz[0], band_hz[1])
        stim_b = _bandlimit(stim, sample_rate, band_hz[0], band_hz[1])
        # A band that survives no bin at this rate (entirely above Nyquist at
        # the downsampled locate, or an inverted schedule) zeroes both sides;
        # fall back rather than correlate silence against silence.
        if float(np.linalg.norm(stim_b)) > 0.0 and float(np.linalg.norm(cap_b)) > 0.0:
            cap, stim = cap_b, stim_b
    stim_norm = float(np.linalg.norm(stim))
    if stim_norm <= 0.0:
        return 0
    num = correlate(cap, stim, mode="valid", method="fft")
    local_energy = correlate(cap * cap, np.ones(L), mode="valid", method="fft")
    local_norm = np.sqrt(np.maximum(local_energy, 0.0))
    # Floor the denominator so silent (near-zero-energy) windows don't blow the
    # ratio up; a floor at a small fraction of the loudest window is enough.
    floor = 1e-6 * float(local_norm.max()) + 1e-12
    ncc = np.abs(num) / (local_norm * stim_norm + floor)
    peak = float(ncc.max()) if ncc.size else 0.0
    if peak <= 0.0:
        return 0
    return int(np.argmax(ncc >= frac * peak))


def _stimulus_shape(segment: ProgramSegment) -> tuple[float | None, float | None, int]:
    """A stimulus segment's waveform identity — everything
    :func:`segment_stimulus` regenerates it from EXCEPT its level.

    Two segments sharing this triple regenerate to stimuli that differ by a
    single scalar amplitude, and :func:`_earliest_strong_peak`'s correlation is
    energy-normalized (scale-invariant) BY DESIGN, so its curve cannot
    distinguish them even in principle. That makes this the exact — and
    provable, not heuristic — ambiguity set :func:`_resolve_anchor` has to
    arbitrate.
    """
    return (segment.f1_hz, segment.f2_hz, segment.n_samples)


def _resolve_anchor(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    arrival: int,
    first: ProgramSegment,
    stimuli: dict[str, np.ndarray],
) -> tuple[ProgramSegment, int, bool]:
    """Decide WHICH shape-identical stimulus the located ``arrival`` really is,
    and say so when the evidence cannot decide.

    ``_earliest_strong_peak`` answers "where is a stimulus of this shape?" but
    not "which occurrence is it?", and it is level-blind by construction (see
    :func:`_stimulus_shape`). Its "earliest lag within ``frac`` of the max"
    tie-break is robust when the shape-siblings are EQUAL level, but the v2
    programs open with a deliberately UNEQUAL pilot pair whose lo member is the
    quietest thing in the program (VERIFY: −22 dBFS, 10 dB under
    ``pilot_summed_hi``). The quiet member's local SNR — not its waveform —
    then decides whether it clears ``frac``, which makes the anchor a knife
    edge: healthy captures have cleared it by +0.019…+0.185 NCC while equally
    pristine ones missed by −0.0048…−0.0490, snapping the anchor onto
    ``pilot_summed_hi`` and shifting the WHOLE timeline by one pilot spacing.
    Every segment is then searched only at ``scheduled ± SEGMENT_SEARCH_S``
    (±30 ms), so a 1.3 s anchor error guarantees "not found" on a recording in
    which the speaker is plainly audible.

    So rather than trust one level-blind gate, this enumerates the (few)
    interpretations the schedule permits and asks the capture which one the
    REST of the program agrees with: for each shape-sibling of ``first``,
    reinterpret ``arrival`` as that segment and score the resulting timeline by
    locating an independent WITNESS — the longest stimulus whose shape is NOT
    in the ambiguity set (VERIFY: the 6 s summed sweep) — through the very same
    :func:`_locate_in_window` the downstream locate uses, so the anchor
    committed to is by construction the anchor the segments actually locate
    under. Readings are ranked by ``presence``, never by the peakedness margin,
    which cannot say whether the witness is there at all.

    This CANNOT manufacture a passing capture: it only changes WHERE the
    analyzer looks, the confidence it reports is still the real measured
    correlation at the chosen place, and every downstream gate is untouched.
    Re-anchoring requires POSITIVE evidence — the winning candidate's witness
    locate must clear ``SWEEP_LOCATE_CONFIDENCE_FLOOR``, i.e. be a sharp lag
    rather than a wash — so a capture containing no locatable program declines
    to move at all. Degenerate shapes keep the unarbitrated behavior: a program
    whose first stimulus has no shape-sibling has nothing to arbitrate, and one
    with no non-sibling stimulus has no independent witness to arbitrate WITH.

    When the witness cannot tell the interpretations apart, this says so. The
    witness discriminates only while the rival timelines put its search window
    somewhere the witness's own shape does NOT recur — and on CHECK that holds
    in exactly one of the two shift directions (see the witness-ordering
    comment below). In the other, both hypotheses land the window on a real
    pilot and, correlation being scale-invariant, score alike on presence too:
    such a pair separates by ~3.5x where a reading that genuinely found the
    witness separates its own by 197x or more. An argmax over the near pair is
    a coin flip, and losing it slides every per-driver window one pilot
    spacing, so the analysis reports energy in the wrong driver's band. Below
    :data:`ANCHOR_DISCRIMINATION_RATIO` the third return value is True: the
    committed anchor is unchanged (these numbers are still the measured ones)
    but the capture is declared un-attributed, and the CHECK ladder refuses it
    as retriable rather than reading a wiring verdict off a coin flip.
    """
    shape = _stimulus_shape(first)
    candidates = [
        seg for seg in program.segments
        if seg.kind in STIMULUS_KINDS and _stimulus_shape(seg) == shape
    ]
    # Longest wins — correlation SNR grows with stimulus length — and `max`
    # holds its FIRST maximum, so an equal-length tie keeps the earliest such
    # segment in schedule order.
    #
    # That ordering is load-bearing, not incidental. A witness must not be
    # confusable with ITSELF under the shift being arbitrated: CHECK builds
    # both roles' pilot pairs with the same duration and gap, so reinterpreting
    # the anchor as `pilot_woofer_hi` moves the window for `pilot_tweeter_hi`
    # onto `pilot_tweeter_lo` — same shape, scale-invariant correlation, so
    # both hypotheses score within ~1e-3 of each other (a loudest-on-tie key
    # measured 0.968 vs 0.968 and 0.9927 vs 0.9926 on two CHECK fixtures): a
    # coin flip that shifts the timeline a full pilot spacing. Taking the
    # EARLIEST of a tied pair avoids THAT pair, because nothing of the same
    # shape sits one gap BEFORE a pair's `lo` member and
    # `_append_leading_pilot_pair` always appends lo-then-hi.
    #
    # It buys ONE of the two shift directions. When the coarse locate is itself
    # one spacing LATE, both hypotheses shift the other way and the chosen
    # witness `pilot_tweeter_lo` has its own twin `pilot_tweeter_hi` sitting
    # exactly one gap AFTER it. No witness choice fixes that, because the ONLY
    # non-sibling stimuli CHECK owns are the other role's pair; the near-tie
    # guard below is what covers it.
    witness = max(
        (seg for seg in program.segments
         if seg.kind in STIMULUS_KINDS and _stimulus_shape(seg) != shape),
        key=lambda seg: seg.n_samples,
        default=None,
    )
    if len(candidates) < 2 or witness is None:
        return first, arrival - first.start_sample, False

    witness_stim = stimuli.get(witness.segment_id)
    if witness_stim is None:
        witness_stim = segment_stimulus(witness)
        stimuli[witness.segment_id] = witness_stim
    scored: list[tuple[float, float, ProgramSegment, int]] = []
    for seg in candidates:
        offset = arrival - seg.start_sample
        _located, confidence, presence = _locate_in_window(
            capture, witness_stim, offset + witness.start_sample,
            witness.n_samples, sample_rate=sample_rate,
        )
        scored.append((presence, confidence, seg, offset))
    # Ranked on PRESENCE, not on the peakedness margin — see the docstring above
    # and `_locate_in_window`. The margin is still read, at `corroborated`
    # below, for the judgment it CAN make.
    #
    # `max` keeps the FIRST maximum, so an exact tie holds the structurally
    # first candidate rather than drifting. The runner-up is then the best of
    # what is LEFT (again first-maximum), which keeps the losing row's segment
    # and offset in hand for the event line below.
    best_index, (best_presence, best_confidence, best_seg, best_offset) = max(
        enumerate(scored), key=lambda item: item[1][0]
    )
    runner_up_presence, runner_up, runner_up_seg, runner_up_offset = max(
        (row for index, row in enumerate(scored) if index != best_index),
        key=lambda item: item[0],
    )
    # Re-anchoring requires POSITIVE evidence that the winning candidate's
    # witness locate is a LOCATE — a lag sharp against its own neighbourhood —
    # and not the shape a correlator makes out of room noise. When the witness
    # never played (a silent driver in CHECK, a capture with no program in it at
    # all) nothing in the window is sharp, and re-anchoring on the argmax of two
    # noise readings would shift the timeline for no reason. That is the module's
    # "was this even heard" judgment, on the quantity it was calibrated for
    # (:data:`SWEEP_LOCATE_CONFIDENCE_FLOOR` against the peakedness margin), and
    # it is NOT redundant with the presence ranking: on the garbage-capture
    # fixture that ranking prefers the LATER candidate and this floor is the
    # only thing that stops the move. It is equally NOT sufficient — a sharp lag
    # is not the witness.
    corroborated = best_confidence >= SWEEP_LOCATE_CONFIDENCE_FLOOR
    if not corroborated:
        best_seg, best_offset = first, arrival - first.start_sample
    # ...and when it IS corroborated, corroboration alone is not discrimination.
    # Two candidates both clearing that floor whose witness presence sits within
    # a factor of :data:`ANCHOR_DISCRIMINATION_RATIO` means the witness landed on
    # a real stimulus of its own shape under EITHER reading, and the argmax
    # between them carries no information (CHECK's witness has a same-shape twin
    # one gap later, so a one-spacing rival anchor lands the window on it). The
    # commitment below is left exactly as it was — a near-tie is not a reason to
    # pick the OTHER one — but the capture is flagged un-attributed so a
    # consuming phase refuses it as retriable instead of grading per-driver
    # windows the anchor may have slid a whole pilot apart.
    #
    # A ratio rather than a difference for the reason
    # :data:`ANCHOR_DISCRIMINATION_RATIO` measures out; written as a
    # multiplication so a runner-up presence of exactly zero (a witness window of
    # digital silence) is a resolved anchor rather than a division by zero.
    ambiguous = (
        corroborated
        and runner_up >= SWEEP_LOCATE_CONFIDENCE_FLOOR
        and best_presence < runner_up_presence * ANCHOR_DISCRIMINATION_RATIO
    )
    corrected = best_seg.segment_id != first.segment_id
    # One line per analyzed capture (this runs once per capture, never in a
    # loop): without it the anchor decision is the one unobservable step in the
    # chain, and a fabricated failure reads exactly like a real one.
    #
    # It names the LOSING interpretation too, because a reader triaging an
    # ambiguous anchor needs to know which timeline nearly won and how far away
    # it sat. Both shifts are measured from the SAME baseline
    # (`arrival - first.start_sample`), so their DIFFERENCE is the separation
    # between the two candidate timelines: one pilot spacing in the near-tie
    # shape. `presence=` is the term the choice is MADE on; `confidence=` is the
    # peakedness margin.
    runner_up_shift_ms = round(
        (runner_up_offset - (arrival - first.start_sample)) / sample_rate * 1000.0, 1
    )
    log_event(
        logger,
        "program_analysis.anchor",
        level=logging.WARNING if (corrected or ambiguous) else logging.INFO,
        phase=program.phase,
        program_id=program.program_id,
        anchor=best_seg.segment_id,
        witness=witness.segment_id,
        candidates=len(candidates),
        presence=round(best_presence, 6),
        runner_up_presence=round(runner_up_presence, 6),
        confidence=round(best_confidence, 4),
        runner_up=round(runner_up, 4),
        runner_up_anchor=runner_up_seg.segment_id,
        corroborated=corroborated,
        corrected=corrected,
        ambiguous=ambiguous,
        shift_ms=round(
            (best_offset - (arrival - first.start_sample)) / sample_rate * 1000.0, 1
        ),
        runner_up_shift_ms=runner_up_shift_ms,
    )
    return best_seg, best_offset, ambiguous


def _global_offset(
    program: ExcitationProgram, capture: np.ndarray, sample_rate: int
) -> tuple[int, ProgramSegment, dict[str, np.ndarray], bool]:
    """Locate the anchor stimulus → integer global offset G. Caches stimuli.

    The whole-capture matched filter runs at :data:`LOCATOR_RATE_HZ` so the
    largest correlation is over a 3× smaller array; the coarse arrival is then
    refined at the full rate inside a tiny window around it, so the returned
    offset is still full-rate-exact. Both passes score inside the anchor
    stimulus's OWN declared band (see :func:`_earliest_strong_peak`), each at
    the rate its own array is sampled at.

    That locate answers WHERE a first-stimulus-shaped waveform is, but not
    WHICH occurrence of it — :func:`_resolve_anchor` arbitrates that against
    the rest of the program and owns the returned segment, which is therefore
    the anchor the offset is pinned to (the program's first stimulus on every
    capture where that reading holds). The fourth return value is that
    arbitration's own honesty flag: True when the evidence could not tell the
    interpretations apart (see :data:`ANCHOR_DISCRIMINATION_RATIO`).
    """
    from scipy.signal import resample_poly

    stimuli: dict[str, np.ndarray] = {}
    first = None
    for seg in program.segments:
        if seg.kind in STIMULUS_KINDS:
            first = seg
            break
    if first is None:
        raise ValueError("program has no stimulus segment to locate against")
    stim = segment_stimulus(first)
    stimuli[first.segment_id] = stim
    band_hz = (first.f1_hz, first.f2_hz)

    down = max(1, int(round(sample_rate / LOCATOR_RATE_HZ)))
    if down > 1:
        capture_lo = resample_poly(capture, 1, down)
        stim_lo = resample_poly(np.asarray(stim, dtype=np.float64), 1, down)
    else:
        capture_lo = capture
        stim_lo = np.asarray(stim, dtype=np.float64)
    coarse = _earliest_strong_peak(
        capture_lo, stim_lo, band_hz=band_hz, sample_rate=sample_rate // down
    ) * down

    # Full-rate refinement in a ±4·down window around the coarse arrival —
    # bounded cost (one small correlate), full-rate precision.
    margin = 4 * down
    lo = max(0, coarse - margin)
    hi = min(capture.size, coarse + stim.size + margin)
    window = capture[lo:hi]
    if window.size >= stim.size:
        arrival = lo + _earliest_strong_peak(
            window, stim, band_hz=band_hz, sample_rate=sample_rate
        )
    else:
        arrival = coarse
    anchor, global_offset, ambiguous = _resolve_anchor(
        program, capture, sample_rate, arrival, first, stimuli
    )
    return global_offset, anchor, stimuli, ambiguous


def _locate_in_window(
    capture: np.ndarray,
    stim: np.ndarray,
    scheduled: int,
    n_samples: int,
    *,
    sample_rate: int,
) -> tuple[int, float, float]:
    """Matched-filter ``stim`` at ``scheduled`` ± :data:`SEGMENT_SEARCH_S`.

    The ONE place the per-segment search geometry lives: :func:`_locate_segments`
    locates every stimulus through it and :func:`_resolve_anchor` scores every
    candidate anchor through it, so "the anchor we chose" is by construction
    "the anchor these segments actually locate under". Two copies of this
    window would let the choice and the consequence drift apart silently.

    Returns BOTH of :class:`~jasper.audio_measurement.alignment.AlignmentResult`'s
    scores, because they answer different questions. ``confidence`` is its
    peakedness margin, ``(peak - secondary) / peak``: "is the winning lag sharp
    against its own neighbourhood", which is what
    :data:`SWEEP_LOCATE_CONFIDENCE_FLOOR` grades — and NOT whether ``stim`` is
    here at all, because this window spans only ~61 ms of lags and over that
    little of the lag axis the ratio of two ROOM-NOISE correlation values is an
    ordinary 0.6-0.8. ``presence`` is its ``peak``: the normalized correlation
    SIMILARITY at that lag, which does say. On real jts3 captures the window
    holding ``sweep_w`` read 0.5394 against 0.0025 for one holding only guard
    silence, while their peakedness sat 0.008 apart the WRONG way round.

    A window too short to hold ``stim`` yields ``(scheduled, 0.0, 0.0)`` — the
    schedule's own guess with zero of either score, never a located claim.
    """
    search = int(round(SEGMENT_SEARCH_S * sample_rate))
    lo = max(0, scheduled - search)
    hi = min(capture.size, scheduled + n_samples + search)
    window = capture[lo:hi]
    if window.size < stim.size:
        return scheduled, 0.0, 0.0
    res = _locate(
        window, stim, sample_rate=sample_rate,
        max_capture_s=window.size / sample_rate + 1.0,
    )
    return lo + int(res.lag_samples), float(res.confidence), float(res.peak)


def _locate_segments(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    stimuli: dict[str, np.ndarray],
) -> list[SegmentLocation]:
    """Locate every segment at scheduled offset ± window; record integrity."""
    out: list[SegmentLocation] = []
    for seg in program.segments:
        scheduled = global_offset + seg.start_sample
        if seg.kind in STIMULUS_KINDS:
            stim = stimuli.get(seg.segment_id)
            if stim is None:
                stim = segment_stimulus(seg)
                stimuli[seg.segment_id] = stim
            # `presence` is the anchor arbitration's term, not this one's: every
            # gate on `SegmentLocation.confidence` is calibrated on the
            # peakedness margin, so recording the other would move all of them.
            located, confidence, _presence = _locate_in_window(
                capture, stim, scheduled, seg.n_samples, sample_rate=sample_rate,
            )
            seg_samples = capture[located:located + seg.n_samples]
            out.append(SegmentLocation(
                segment_id=seg.segment_id,
                kind=seg.kind,
                role=seg.role,
                scheduled_start=scheduled,
                located_start=located,
                residual_samples=float(located - scheduled),
                confidence=confidence,
                peak_dbfs=_peak_dbfs(seg_samples),
                clipped=_has_clipped_run(seg_samples),
            ))
        else:
            seg_samples = capture[max(0, scheduled):scheduled + seg.n_samples]
            out.append(SegmentLocation(
                segment_id=seg.segment_id,
                kind=seg.kind,
                role=seg.role,
                scheduled_start=scheduled,
                located_start=scheduled,
                residual_samples=0.0,
                confidence=1.0,
                peak_dbfs=_peak_dbfs(seg_samples),
                clipped=_has_clipped_run(seg_samples),
            ))
    return out


# --------------------------------------------------------------------------- #
# drift (MEASURE)
# --------------------------------------------------------------------------- #

# A MEASURE sweep segment ID's occurrence suffix (build_measure_program's
# _occurrence_suffix): bare = first/primary, "_rep" = second, "_repN" = the
# (N+1)-th. Anchored at the END of the id so a driver token embedded earlier
# (today "w"/"t") never matters here.
_SWEEP_OCCURRENCE_SUFFIX_RE = re.compile(r"_rep(\d*)$")


def _sweep_occurrence_index(segment_id: str) -> int:
    """0-based occurrence index encoded in a MEASURE sweep segment ID's
    suffix (mirrors ``program.build_measure_program``'s ``_occurrence_suffix``):
    bare id ⇒ 0 (first/primary), ``_rep`` ⇒ 1, ``_rep{n}`` ⇒ n (n ≥ 2).
    """
    m = _SWEEP_OCCURRENCE_SUFFIX_RE.search(segment_id)
    if m is None:
        return 0
    digits = m.group(1)
    return 1 if digits == "" else int(digits)


def _sweep_occurrences_by_role(
    locations: Sequence[SegmentLocation],
) -> dict[str, list[SegmentLocation]]:
    """Group MEASURE ``KIND_SWEEP`` locations by driver role, each role's list
    ordered first→last by its ID-encoded occurrence index
    (:func:`_sweep_occurrence_index`) rather than physical schedule position:
    the N=3 layout (design §5.4) interleaves w1,t1,w2,t2,..., so schedule order
    is NOT occurrence order across roles. Tolerates ANY occurrence count ≥1 per
    role, including a role absent from ``locations`` or present only once.
    """
    by_role: dict[str, list[tuple[int, SegmentLocation]]] = {}
    for loc in locations:
        if loc.kind != KIND_SWEEP or not loc.role:
            continue
        by_role.setdefault(loc.role, []).append(
            (_sweep_occurrence_index(loc.segment_id), loc)
        )
    return {
        role: [loc for _idx, loc in sorted(pairs, key=lambda pair: pair[0])]
        for role, pairs in by_role.items()
    }


def _repeat_epsilon(
    capture: np.ndarray,
    program: ExcitationProgram,
    first: SegmentLocation,
    last: SegmentLocation,
) -> tuple[float, float] | None:
    """Sub-sample + integer-only clock-drift epsilon from a role's FIRST vs
    LAST located sweep occurrence (Gamper's repeat-ratio idea — the wider the
    baseline the more precise the estimate, design §3.1 / §5.6.3). ``None``
    when the two share a (degenerate) scheduled start.
    """
    seg_first = program.segment(first.segment_id)
    seg_last = program.segment(last.segment_id)
    scheduled_sep = seg_last.start_sample - seg_first.start_sample
    if scheduled_sep <= 0:
        return None
    measured_sep = _subsample_separation(
        capture, first.located_start, last.located_start, seg_first.n_samples
    )
    epsilon = measured_sep / scheduled_sep - 1.0
    eps_int = (last.located_start - first.located_start) / scheduled_sep - 1.0
    return epsilon, eps_int


def _locate_discontinuity(
    program: ExcitationProgram,
    capture: np.ndarray,
    stimulus_locs: Sequence[SegmentLocation],
) -> tuple[float | str, str, TimelineStepFit]:
    """Fit a single discrete timeline STEP across the located sweeps.

    Returns ``(step_samples, after_segment_id, fit)``. The first two are
    ``(0.0, "")`` when no step is resolved, including on a clean capture where
    that is the expected value, or ``(DISCONTINUITY_UNRESOLVED, "")`` when any
    of ``stimulus_locs`` falls below ``SWEEP_LOCATE_CONFIDENCE_FLOOR``, because
    a step fitted from a sweep the locator could barely find is a number
    invented from noise. The third is the fit itself, which ``_estimate_drift``
    puts through
    :func:`~jasper.audio_measurement.timeline_slip.slip_rejects_capture`.

    The model lives in :mod:`jasper.audio_measurement.timeline_slip`; what
    makes its output sharp enough to GATE on rather than merely annotate is the
    input. Each occurrence is placed against its role's first by
    :func:`_subsample_separation` (measured scatter 0.038-0.299 samples) rather
    than read off the integer ``located_start`` (2.00-3.13 samples on clean
    hardware). The module constant ``SLIP_GATE_SAMPLES`` owns the measured
    operating point and which slips remain out of reach.

    Placing each occurrence relatively also cancels the global offset and the
    driver's constant acoustic delay structurally; the per-role constants in
    the model absorb whatever is left.
    """
    ordered = sorted(
        stimulus_locs, key=lambda loc: program.segment(loc.segment_id).start_sample
    )
    # Gate BEFORE the fit, on the exact per-location confidence the fit is
    # about to trust implicitly.
    if any(loc.confidence < SWEEP_LOCATE_CONFIDENCE_FLOOR for loc in ordered):
        return DISCONTINUITY_UNRESOLVED, "", TimelineStepFit()

    # Sub-sample positions, per role, referenced to that role's first
    # occurrence. The reference's own integer start stays in the value; the
    # model's per-role constant absorbs it, so only WITHIN-role placement has
    # to be sharp — which is what `_subsample_separation` delivers. A role-less
    # sweep groups with the other role-less ones under "": no composable
    # MEASURE program can produce one (``RoleBand.__post_init__`` refuses a
    # role that is not a non-empty string), so this is scope discipline, not a
    # live case.
    # Keyed by POSITION in `ordered`, not by object identity: two locations can
    # legitimately compare equal, and an index needs no aliasing argument.
    by_role: dict[str, list[int]] = {}
    for index, loc in enumerate(ordered):
        by_role.setdefault(loc.role or "", []).append(index)
    placed: list[float] = [0.0] * len(ordered)
    for members in by_role.values():
        reference = ordered[members[0]]
        ref_n = program.segment(reference.segment_id).n_samples
        placed[members[0]] = float(reference.located_start)
        for index in members[1:]:
            placed[index] = float(reference.located_start) + _subsample_separation(
                capture,
                reference.located_start,
                ordered[index].located_start,
                ref_n,
            )

    fit = fit_timeline_step(
        [float(program.segment(loc.segment_id).start_sample) for loc in ordered],
        placed,
        [loc.role or "" for loc in ordered],
    )
    if not slip_rejects_capture(fit):
        return 0.0, "", fit
    return fit.step_samples, ordered[fit.cut_index - 1].segment_id, fit


def _estimate_drift(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
) -> DriftEstimate:
    occurrences_by_role = _sweep_occurrences_by_role(locations)
    # Only the SWEEP-kind stimuli anchor the drift baselines / residual guard.
    # A v2 MEASURE program may open with a leading pilot pair (linearity probe,
    # design §5.2) whose short/quiet windows are located more coarsely; folding
    # them into the residual guard would manufacture spurious desync. Pilots are
    # judged separately (their own linearity verdict), never as a drift baseline.
    stimulus_locs = [loc for loc in locations if loc.kind == KIND_SWEEP]

    # Primary gate: the WOOFER's first-vs-LAST located occurrence. A MEASURE
    # program is required to contain "sweep_w" with a role and this analysis
    # hard-depends on that invariant throughout, so it is the one literal
    # anchor kept — see `_sweep_occurrences_by_role` for why every OTHER
    # role/occurrence is discovered rather than hardcoded.
    woofer_role = program.segment("sweep_w").role
    assert woofer_role is not None, "a MEASURE sweep segment always carries a role"
    woofer_occurrences = occurrences_by_role.get(woofer_role, [])
    w1 = woofer_occurrences[0] if woofer_occurrences else None
    w2 = woofer_occurrences[-1] if len(woofer_occurrences) >= 2 else None

    epsilon = 0.0
    if w1 is not None and w2 is not None:
        result = _repeat_epsilon(capture, program, w1, w2)
        if result is not None:
            # Sub-sample separation of two identical woofer sweeps (τ cancels;
            # drift is the ratio). Design §3.1 / §5.6.3.
            epsilon = result[0]

    # Per-driver-demeaned schedule residual after applying ε. A driver's own
    # acoustic delay is a constant offset (removed by demeaning), so this does
    # NOT flag the real tweeter-vs-woofer delay; it catches a within-driver
    # desync (a dropped buffer between a driver's own repeated sweeps). A
    # mid-program dropped buffer between two same-role sweeps instead surfaces
    # as an out-of-band ε (the ppm bound below) whenever the woofer pair spans
    # it. NOTE: with one located sweep per role the demeaned residual is
    # identically zero, so this guard only ACTIVATES for a role with ≥2
    # located sweeps — under the N=3 interleaved MEASURE program that is BOTH
    # drivers (three occurrences each); an old-shaped 3-sweep program still
    # gets it for the woofer pair only, the single-sweep tweeter covered by
    # the ε ppm bound alone.
    #
    # Each occurrence is placed against its group's FIRST by
    # `_subsample_separation`, never read off `located_start` — the resolution
    # argument, and why 1.5 is the right number against this estimator, is
    # owned by `GLITCH_RESIDUAL_SAMPLES`. Placing them relatively cancels the
    # global offset and the driver's constant acoustic delay structurally; the
    # demeaning stays because the within-role SPREAD is the statistic.
    # Grouped here rather than through `_sweep_occurrences_by_role` on purpose:
    # that owner drops a role-less sweep, where this guard groups them
    # together. The two agree for every program that can reach here —
    # `RoleBand.__post_init__` refuses a role that is not a non-empty string —
    # so this is scope discipline, not drift.
    groups: dict[Any, list[SegmentLocation]] = {}
    for loc in stimulus_locs:
        groups.setdefault(loc.role, []).append(loc)
    max_residual = 0.0
    for members in groups.values():
        reference = members[0]
        ref_seg = program.segment(reference.segment_id)
        resids = [0.0]
        for loc in members[1:]:
            scheduled_sep = (
                program.segment(loc.segment_id).start_sample - ref_seg.start_sample
            )
            measured_sep = _subsample_separation(
                capture, reference.located_start, loc.located_start, ref_seg.n_samples,
            )
            resids.append(measured_sep - scheduled_sep * (1.0 + epsilon))
        mean = sum(resids) / len(resids)
        for r in resids:
            max_residual = max(max_residual, abs(r - mean))

    # Woofer-repeat LEVEL agreement (design §5.2): the woofer's first and LAST
    # sweeps are bit-identical stimuli, so a clean capture reproduces the same
    # captured level for both. Measured band-relative — in-band RMS over the
    # woofer's OWN declared band (`_band_power`, the same Hann+bandpass
    # mechanism `_pilot_observations` uses), after trimming the composer's
    # fixed edge fade (`_pilot_trim_fade`) — never full-band single-sample
    # PEAK: a low-frequency, room-mode-excited sweep's full-band peak is an
    # unstable estimator, and two hardware mics measured two genuinely-identical
    # woofer sweeps 0.64 dB apart by full-band peak (enough to trip this gate)
    # but only 0.06-0.24 dB apart by in-band RMS. Real AGC gain-riding, this
    # gate's actual purpose, still shows up in-band, since a uniform per-sweep
    # gain shift survives band-limiting. A larger delta REUSES the
    # drift-baselines-disagree glitch verdict, never a new user-facing code.
    #
    # Scope: this first-vs-last pairing only sees the woofer's TWO endpoint
    # occurrences — a level step confined to a middle repeat, or anywhere on
    # the tweeter, does not trip it. `per_role_epsilon_ppm` below is the timing
    # analogue exposed as diagnostic evidence.
    repeat_level_delta_db = 0.0
    repeat_level_disagrees = False
    if w1 is not None and w2 is not None:
        level_seg_w = program.segment("sweep_w")
        if level_seg_w.f1_hz is None or level_seg_w.f2_hz is None:
            raise ValueError("woofer sweep segment has no declared band")
        w1_samples = _pilot_trim_fade(
            capture[w1.located_start:w1.located_start + level_seg_w.n_samples], sample_rate,
        )
        w2_samples = _pilot_trim_fade(
            capture[w2.located_start:w2.located_start + level_seg_w.n_samples], sample_rate,
        )
        level_w1 = _band_rms_dbfs(w1_samples, sample_rate, level_seg_w.f1_hz, level_seg_w.f2_hz)
        level_w2 = _band_rms_dbfs(w2_samples, sample_rate, level_seg_w.f1_hz, level_seg_w.f2_hz)
        repeat_level_delta_db = abs(level_w1 - level_w2)
        repeat_level_disagrees = repeat_level_delta_db > REPEAT_LEVEL_TOLERANCE_DB

    # Per-role first-vs-last epsilon diagnostics (design §5). NEVER gates
    # `glitch_detected` — only the woofer pair above does that.
    per_role_epsilon_ppm: dict[str, float] = {}
    for role, occurrences in occurrences_by_role.items():
        if len(occurrences) < 2:
            continue
        result = _repeat_epsilon(capture, program, occurrences[0], occurrences[-1])
        if result is not None:
            per_role_epsilon_ppm[role] = result[0] * 1e6

    # Computed on EVERY capture, not just a failing one, so the clean corpus
    # carries the same field and a future bench pass can read its distribution
    # (the `repeat_level_delta_db` precedent). A clean capture resolves no step
    # and reports 0.0 / "".
    discontinuity_samples, discontinuity_after, slip_fit = _locate_discontinuity(
        program, capture, stimulus_locs
    )

    # WHICH bound tripped, in a fixed order — the verdict stays one reason code
    # (§5.2), this is telemetry's disambiguator. The slip gate is APPENDED
    # rather than folded into `residual_desync`: that name belongs to the
    # spread guard, and one name covering two instruments would leave a journal
    # reader unable to tell which of them fired.
    glitch_inputs = tuple(
        name
        for name, tripped in (
            ("epsilon_out_of_bound", abs(epsilon) * 1e6 > MAX_DRIFT_PPM),
            ("residual_desync", max_residual > GLITCH_RESIDUAL_SAMPLES),
            ("repeat_level_disagree", repeat_level_disagrees),
            (GLITCH_INPUT_TIMELINE_SLIP, slip_rejects_capture(slip_fit)),
        )
        if tripped
    )
    glitch = bool(glitch_inputs)

    if glitch:
        log_event(
            logger,
            "program_analysis.glitch",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            glitch_inputs=",".join(glitch_inputs),
            epsilon_ppm=round(epsilon * 1e6, 2),
            max_residual_samples=round(max_residual, 2),
            repeat_level_delta_db=round(repeat_level_delta_db, 3),
            # `discontinuity_samples` is `DISCONTINUITY_UNRESOLVED` (a `str`,
            # not a number) when the located sweeps weren't trustworthy enough
            # to fit a step from — `round()` would raise on that value.
            discontinuity_samples=(
                round(discontinuity_samples, 2)
                if isinstance(discontinuity_samples, (int, float))
                else discontinuity_samples
            ),
            discontinuity_after_segment=discontinuity_after,
        )
    return DriftEstimate(
        epsilon_ppm=epsilon * 1e6,
        max_residual_samples=max_residual,
        glitch_detected=glitch,
        repeat_level_delta_db=repeat_level_delta_db,
        per_role_epsilon_ppm=per_role_epsilon_ppm,
        glitch_inputs=glitch_inputs,
        discontinuity_samples=discontinuity_samples,
        discontinuity_after_segment=discontinuity_after,
    )


# --------------------------------------------------------------------------- #
# capture integrity (VERIFY)
# --------------------------------------------------------------------------- #


def _frame_accounting_checks(ledger: FrameLedger) -> list[IntegrityCheck]:
    """The two frame-accounting checks, most fundamental first.

    ``capture_render_gap`` asks whether the browser's audio render graph handed
    the recorder every quantum it should have; ``frame_ledger`` asks whether
    every frame the page says it recorded reached this host. See
    :mod:`jasper.audio_measurement.frame_ledger` for why those are two
    questions and which hops each one can and cannot see.

    A page that reported nothing leaves both unevaluated, not failed: the
    single-capture runner posts no report and some page builds declare no
    counts, and failing them would refuse captures over the age of the phone's
    bundle, which is not a fact about the recording.
    ``verification.evaluate_capture_validity`` treats a not-evaluated record as
    usable.
    """
    checks: list[IntegrityCheck] = []
    if not ledger.render_gap_evaluated:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_RENDER_GAP, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_RENDER_REPORT,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_RENDER_GAP,
            INTEGRITY_FAIL if ledger.render_gap_frames else INTEGRITY_PASS,
        ))
    if not ledger.balance_evaluated:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_FRAME_LEDGER, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_FRAME_COUNT,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_FRAME_LEDGER,
            INTEGRITY_PASS if ledger.balanced else INTEGRITY_FAIL,
        ))
    return checks


def _log_frame_ledger(program: ExcitationProgram, ledger: FrameLedger) -> None:
    """One structured line per analyzed capture — the self-report itself.

    Emitted on every phase and on a CLEAN capture too, at INFO: "no loss was
    reported for this capture" and "no capture was analysed" are different
    facts, and only a line on the clean path tells them apart. A short capture
    is the same line at WARNING.
    """
    lost = ledger.lost_at
    log_event(
        logger,
        "program_analysis.frame_ledger",
        level=logging.WARNING if lost else logging.INFO,
        phase=program.phase,
        program_id=program.program_id,
        received_frames=ledger.received_frames,
        declared_frames=ledger.declared_frames,
        encoded_frames=ledger.encoded_frames,
        render_gaps=ledger.render_gaps,
        render_gap_frames=ledger.render_gap_frames,
        lost_at=",".join(lost),
    )


def _verify_capture_integrity(
    program: ExcitationProgram,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    frame_ledger: FrameLedger,
) -> CaptureIntegrity:
    """Capture-integrity evidence for a ONE-summed-sweep program.

    ``_estimate_drift`` cannot run here and this is not a smaller version of
    it. Every one of its three glitch inputs compares a role's repeated sweeps
    against each other, and a VERIFY program plays one mono summed sweep — so
    the honest record is not "drift checks passed", it is "drift checks did not
    run, and here is what did".

    What runs, in routing order:

    0. **frame accounting** (:func:`_frame_accounting_checks`) — ahead of every
       signal question, because the checks below ask how good a measurement of
       the speaker this recording is, and these ask whether the recording is
       all there. A capture missing a render quantum can locate its sweep
       perfectly and still be a splice.
    1. **heard** — the summed sweep's own locate confidence against
       :data:`SWEEP_LOCATE_CONFIDENCE_FLOOR`. First because a sweep the
       correlator could barely find lands in the wrong place and then
       manufactures a large residual: report the cause, not the symptom.
    2. **schedule** — |residual| against
       :data:`SWEEP_SCHEDULE_RESIDUAL_CEILING_MS`, the xrun detector. Only when
       (1) passed; otherwise ``not_evaluated``, with the measured residual
       still disclosed as evidence.
    3. **clipped run** — any stimulus segment carrying a full-scale run
       (``SegmentLocation.clipped``, already computed by ``_locate_segments``).
       Independent of (1): a clip is a clip whether or not the locator was
       confident.

    Gate-window comparability is deliberately NOT here: it compares this
    capture's gate against the PREDICTION's, and only the session holds the
    MEASURE window. Pilot segments are excluded from (1) and (2) for the same
    reason ``_estimate_drift`` excludes them — short, quiet windows locate
    coarsely by design and would manufacture spurious fires — and included in
    (3), where window precision does not matter.

    What (2) cannot see, stated because a gate whose bound is unstated gets
    read as total:

    * A splice INSIDE the summed sweep. The residual is measured at the sweep's
      located START, so an insertion partway through corrupts the deconvolution
      while leaving the start where it belongs. This is the shape
      ``_locate_discontinuity`` names, and it needs more sweeps than a VERIFY
      program has. Check (0) closes the half of the class the BROWSER can see —
      a render quantum the audio graph never delivered — and none of the rest:
      a splice upstream of the worklet still reaches (2) with contiguous
      frames.
    * A splice BEFORE the first stimulus, which the global offset absorbs —
      correctly, since a uniformly shifted capture is not corrupt.
    * Anything at all on a pilot-less VERIFY program, where the summed sweep IS
      the global-offset anchor and its residual is therefore structurally ~0.
      Every session-composed VERIFY program carries the leading pilot pair, so
      the anchor is a pilot and the sweep's residual is a real measurement.
    """
    sweeps = [loc for loc in locations if loc.kind == KIND_SUMMED_SWEEP]
    stimuli = [loc for loc in locations if loc.kind in STIMULUS_KINDS]
    clipped_segments = tuple(loc.segment_id for loc in stimuli if loc.clipped)

    confidence_min: float | None = None
    residual_ms_worst: float | None = None
    if sweeps:
        confidence_min = min(float(loc.confidence) for loc in sweeps)
        worst = max(sweeps, key=lambda loc: abs(loc.residual_samples))
        residual_ms_worst = float(worst.residual_samples) / sample_rate * 1000.0

    checks: list[IntegrityCheck] = _frame_accounting_checks(frame_ledger)
    if confidence_min is None or residual_ms_worst is None:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_SUMMED_SWEEP,
        ))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_SUMMED_SWEEP,
        ))
    elif confidence_min < SWEEP_LOCATE_CONFIDENCE_FLOOR:
        checks.append(IntegrityCheck(INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_FAIL))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_SWEEP_NOT_HEARD,
        ))
    else:
        checks.append(IntegrityCheck(INTEGRITY_CHECK_SWEEP_HEARD, INTEGRITY_PASS))
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_SWEEP_SCHEDULE,
            INTEGRITY_FAIL
            if abs(residual_ms_worst) > SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
            else INTEGRITY_PASS,
        ))
    if not stimuli:
        # No stimulus segment to inspect is not "nothing was clipped" — it is
        # the same "nobody looked" a bare False would have been.
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_CLIPPED_RUN, INTEGRITY_NOT_EVALUATED,
            _INTEGRITY_NO_STIMULUS,
        ))
    else:
        checks.append(IntegrityCheck(
            INTEGRITY_CHECK_CLIPPED_RUN,
            INTEGRITY_FAIL if clipped_segments else INTEGRITY_PASS,
        ))
    for name in (
        INTEGRITY_CHECK_REPEAT_EPSILON,
        INTEGRITY_CHECK_REPEAT_LEVEL,
        INTEGRITY_CHECK_WITHIN_ROLE_DESYNC,
    ):
        checks.append(IntegrityCheck(
            name, INTEGRITY_NOT_EVALUATED, _INTEGRITY_NO_REPEAT_PAIR,
        ))
    checks.append(IntegrityCheck(
        INTEGRITY_CHECK_DISCONTINUITY_STEP, INTEGRITY_NOT_EVALUATED,
        _INTEGRITY_STEP_NEEDS_MORE_SWEEPS,
    ))

    integrity = CaptureIntegrity(
        checks=tuple(checks),
        locate_confidence_min=confidence_min,
        schedule_residual_ms_worst=residual_ms_worst,
        clipped_segments=clipped_segments,
    )
    if integrity.glitched:
        # The VERIFY twin of ``program_analysis.glitch``, at the same level
        # and for the same reason: the capture this fired on is about to be
        # refused, and the journal should say which measurement said so.
        log_event(
            logger,
            "program_analysis.capture_integrity",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            failed=",".join(integrity.failed),
            not_evaluated=",".join(integrity.not_evaluated),
            locate_confidence_min=(
                round(confidence_min, 4) if confidence_min is not None else None
            ),
            schedule_residual_ms_worst=(
                round(residual_ms_worst, 3) if residual_ms_worst is not None else None
            ),
            clipped_segments=",".join(clipped_segments),
        )
    return integrity


# --------------------------------------------------------------------------- #
# per-driver response + alignment + candidate (MEASURE)
# --------------------------------------------------------------------------- #


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

    Both sides of an SNR subtraction have to be the same quantity, so this
    picks the signal side to match whichever domain the noise report arrived
    in:

    * A ``"deconvolved"`` noise report (a deconvolved+windowed ambient IR run
      through :func:`~jasper.audio_measurement.snr_policy.magnitude_band_levels`)
      pairs with the deconvolved transfer-function levels.
    * A ``"raw"`` report — which is every report
      :func:`~jasper.audio_measurement.snr_policy.framed_ambient_band_report`
      produces, and so every ambient report a v2 CHECK hands forward — pairs
      with the RAW captured sweep's band levels.

    The deciding reason is the SOLVE's domain, not a general preference. This
    verdict checks whether the per-driver level solve delivered the SNR it
    aimed for, and :func:`_solve_role_gain` aims in the RAW domain: its room
    arm is ``ambient_band_level + required_snr_db + crest_factor_db``, where
    the ambient level is a row of the raw ``framed_ambient_band_report`` table.
    A raw-with-raw verdict therefore reads back the very quantity the solve
    targeted — ``_band_required_snr_db(...) + MEASURE_SNR_SOLVE_MARGIN_DB``,
    i.e. 41 dB where the alignment requirement applies. (The solve's
    ``crest_factor_db`` term converts its band-RMS demand into the capture PEAK
    ``k_db`` turns into a digital gain; it cancels out of an RMS-vs-RMS SNR.)

    A raw report cannot be subtracted from the transfer function. The
    deconvolution divides the capture by the reference sweep regenerated at
    that segment's own ``gain_db``, so the drive cancels exactly and ``mag_db``
    is invariant to how loud MEASURE played, while the room's dBFS floor is
    not. The difference is then a number that does not move when the
    measurement gets quieter: on synthetic two-way fixtures a MEASURE played
    20 dB quieter into an unchanged room reported the SAME worst-band SNR and
    the same ``ok`` verdict, while the same-domain reading fell the full 20 dB
    and the cross-domain one ran roughly +17 to +65 dB high — band-dependent
    and growing as the measurement quietens, so not a correctable offset.

    ``window="rectangular"`` because a sweep is non-stationary: a Hann window
    re-weights a swept sine's frequencies by WHEN they occur. The duty-cycle
    offset that makes rectangular unsafe on a PADDED capture (a band-independent
    ``10*log10(sweep_len/capture_len)`` term, 5.93 dB over a 0-to-20 s lead) is
    zero here by construction: :func:`_raw_sweep_segment` hands this function
    exactly ``segment.n_samples``, so the ratio is 1. It is the slice width,
    not the window, that buys this.

    ``radiated_band_hz`` scopes the verdict to the band this branch's stimulus
    actually drove — see :func:`~..comparison_bands.branch_snr_band_hz`, which
    owns that policy.
    BOTH decision classes below take the same window, because "a row the sweep
    never entered is not evidence" is a statement about the MEASUREMENT, not
    about which law reads it.

    Fails closed: a raw report with no captured segment to pair it against, or
    a branch declaring neither a corner nor a radiated band to scope the verdict
    by, produces no verdict at all rather than an unscoped one. A segment that
    is PRESENT but degenerate (a capture truncated before this sweep, so
    :func:`_raw_sweep_segment` clamps to fewer than 8 samples) instead makes
    ``band_levels_dbfs`` return no bands, so the block carries ``verdict:
    "unknown"`` and an empty band list. The two spellings are distinguishable
    on purpose: absent means "no evidence was offered", unknown means "evidence
    was offered and was unusable".
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
    # TWO decision classes off ONE set of measurements, because the repo's SNR
    # law asks two different questions of them. The magnitude verdict above
    # answers "did the level solve deliver the SNR it aimed for" and grades
    # `ok`/`reduced`/`insufficient` around 25/20 dB. A POLARITY/DELAY decision
    # is held to `DRIVER.alignment_snr_ok_db` (35 dB, ok-or-insufficient with
    # no reduced rung), which `_band_required_snr_db` declares and the MEASURE
    # level solve aims at; reading the magnitude verdict for the alignment
    # refusal leaves a 15 dB window in which a polarity read off a capture the
    # law calls unusable ships unrefused. Same bands, same noise, same
    # overlap-scoped window: only the law differs.
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

    :func:`deconv.direct_arrival_window` places each branch's direct peak at the
    same fixed offset (``IR_PRE_MS``) inside the window, so both branches share a
    common time reference (bulk delay removed) WITHOUT a circular roll — a roll
    followed by zero-padding to ``n_fft`` is not shift-invariant and would inject
    a spurious echo into the magnitude.

    The windowed IR is then run through the SAME adaptive reflection gate
    :func:`_driver_response` applies. Composing branches from the fixed
    ``IR_PRE_MS``/``IR_POST_MS`` window alone bakes a room reflection inside
    that 65 ms tail into the predicted sum, while VERIFY's measured sum has
    already gated it out — a hardware failure traced to a 15 cm desk-bounce
    producing a spurious ~1125 Hz null in the fixed-window prediction alone.
    Gating a window that already has the peak at a fixed local offset preserves
    that offset (the gate only shortens/tapers the TAIL), so the shared time
    base survives.
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
    # Both IRs were deconvolved from windows sharing the pre-guard + global
    # offset, so each direct peak sits at ``pre_samples`` ± the relative delay.
    # Slice the SAME [pre−H, pre+H] region from both (bounds the FFT and keeps
    # the shared time base), band-limit to the overlap, then GCC-PHAT: the peak
    # lag is τ + ε·Δstart (tweeter later ⇒ positive).
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
    # ε-correct: the tweeter's schedule offset is stretched by ε.
    delta_start = seg_t.start_sample - seg_w.start_sample
    tau_samples = lag_samples - epsilon * delta_start
    # delay_us = (D_woofer − D_tweeter) = −τ (τ = D_tweeter − D_woofer).
    raw_delay_us = -tau_samples / sample_rate * 1e6
    parallax_us = geometry.parallax_us()
    delay_us = raw_delay_us - parallax_us

    polarity = polarity_label(polarity_sign)

    status = ALIGNMENT_OK
    if at_edge:
        # A peak clamped at the search bound is not a measurement of the delay —
        # the true delay likely exceeds the geometry prior. Fail explicitly at
        # confidence 0 rather than returning a moderate-confidence wrong value.
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
    # anchor: the raw full-IR argmax gap (same time base as ``ir_t`` / ``ir_w``),
    # drift-corrected and parallax-corrected into the signed delay frame. Both
    # the fine-snap center (lag domain) and ``_build_candidate``'s applied
    # anchor derive from THIS single computation — the argmax is never
    # recomputed downstream, since a parallel argmax could silently
    # desynchronize the snap center from the reported anchor. The snap moves the
    # anchor to the nearest local maximum of the SAME correlation within
    # ±(period/6) at Fc; ``None`` (no local peak in radius, or an edge-refused
    # estimate) leaves ``_build_candidate`` on the bare anchor. This snaps the
    # applied delay only — GCC polarity/confidence is untouched.
    snapped_delay_us: float | None = None
    anchor_delay_us: float | None = None
    if status == ALIGNMENT_OK:
        anchor_lag_samples = float(
            int(np.argmax(np.abs(tweeter_full_ir)))
            - int(np.argmax(np.abs(woofer_full_ir)))
        )
        # Peak gap − inter-sweep drift, plus parallax, negated into the signed
        # frame.
        inter_sweep_drift_us = epsilon * delta_start / sample_rate * 1e6
        drift_corrected_peak_gap_us = (
            anchor_lag_samples / sample_rate * 1e6 - inter_sweep_drift_us
        )
        anchor_delay_us = -(drift_corrected_peak_gap_us + parallax_us)
        if fc_hz > 0.0:
            radius_samples = sample_rate / fc_hz * GCC_SNAP_RADIUS_PERIODS
            # Deliberate: `_gcc_local_peak_snap` recomputes the correlation via
            # the shared `_gcc_correlation` core rather than threading the seed's
            # array here. One extra small FFT once per MEASURE is the accepted
            # cost of not coupling the aligner's return to a big-array handoff.
            snapped_lag = _gcc_local_peak_snap(
                ir_t, ir_w, sample_rate=sample_rate, band_hz=(lo, hi),
                upsample=GCC_UPSAMPLE, anchor_lag_samples=anchor_lag_samples,
                radius_samples=radius_samples,
            )
            if snapped_lag is not None:
                snapped_tau = snapped_lag - epsilon * delta_start
                snapped_delay_us = -snapped_tau / sample_rate * 1e6 - parallax_us

    # The flat-sum cross-check belongs to the SELECTOR
    # (`_select_alignment_pair`), scored jointly with the delay in
    # `_build_candidate` where the branch transfer functions and trims already
    # exist. This estimate is therefore the correlation SEED — polarity included
    # — and `polarity_agrees_with_sum` stays None until the selection answers
    # it. A second, delay-blind flat-sum verdict here would be a second owner of
    # one question.
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
    physical applied delay must enter here only as the *residual* relative to
    that frame: ``(D_t - D_w) + applied_signed_delay``. Passing the full
    applied delay would count the measured peak gap twice.

    Public because the v2 session rebuilds its persisted VERIFY prediction from
    the LINEARIZED branch pair when Layer-1a linearization was fitted
    (``jasper.active_speaker.crossover_v2.intervention.plan_linearization``),
    reusing this SAME machinery rather than a second implementation.

    Callers do not compute the residual themselves: it has ONE owner,
    :func:`summed_model_residual_delay_us`.
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

    ``(D_t − D_w) + applied_signed_delay``, expressed in the two numbers the
    aligner owns. :class:`AlignmentEstimate`'s ``anchor_delay_us`` is the
    drift-corrected physical peak gap negated into the signed delay frame, so
    ``−anchor_delay_us`` IS ``(D_t − D_w)`` and the residual is simply
    ``applied − anchor``. At the bare anchor it is exactly ``0.0``, which is
    what makes the anchor the frame the argmax-referenced branch pair is
    already in.

    Why a residual and not the applied delay itself: :func:`_aligned_branch_tf`
    references each branch to its OWN direct peak, so the measured peak gap is
    already OUT of ``W``/``T``. Phasing by the full applied delay counts that
    gap twice, injecting a deep comb into the predicted sum on good
    measurements and failing VERIFY.

    ``anchor_delay_us`` is ``None`` exactly when the aligner refused the
    estimate (:data:`ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW`): there is then no
    trustworthy argmax-frame reference AND, by the same status,
    ``crossover_v2.planning.alignment_to_candidate_fields`` applies no delay at
    all. Both facts point the same way, so the model keeps the
    independently-aligned frame it is already in and returns ``0.0`` — a
    fabricated gap from an estimate the aligner itself refused would be worse
    than none.

    Callers pass ``None`` for a SECOND refusal, one level up. The low-SNR arm
    of :func:`_select_alignment_pair` commits a delay this capture did not
    supply, so ``committed − anchor`` there is not a fact about the speaker: it
    is the disagreement between an untrusted anchor and a trusted applied
    delay, and it can be most of a period. Phasing the shipped model by it
    fabricates a comb the emitted graph need not have — and that model is read
    by ``crossover_v2.accountability``'s prediction gate, which GRADES a
    candidate, and becomes VERIFY's tracking reference, which can FAIL a round.
    Same argument, same answer: the model keeps the independently-aligned
    frame, and the comparison it feeds grades the branches rather than a timing
    claim the capture already withdrew. Every MEASURED-vs-spec verdict stays
    live, including VERIFY's absolute claim — a speaker that really combs still
    fails honestly.
    """
    if anchor_delay_us is None:
        return 0.0
    return float(applied_delay_us) - float(anchor_delay_us)


def half_period_us(fc_hz: float) -> float:
    """Half of one period at ``fc_hz``, in microseconds. ONE spelling.

    The radius of the comb lobe a crossover corner owns, and therefore the
    delay-ambiguity budget: two delays more than this far apart put the
    summation on adjacent lobes, where a correlation peak — or a hand-written
    number — can land on the wrong one and look just as good locally. Two
    callers share it so the repository holds one opinion about where a lobe
    ends: ``_select_alignment_pair``'s ``left_anchor_lobe`` tripwire and
    :func:`jasper.active_speaker.crossover_v2.alignment_prescription.read_alignment_prescription`'s
    bound. One observes, one gates; the geometry is identical.

    ``fc_hz`` must be positive and finite — the quantity is undefined
    otherwise, and pretending would make a bound vacuous. Callers guard first:
    the tripwire returns ``False`` and the prescription reader refuses with a
    named reason.
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

    ``polarity_sign``/``delay_us`` are the committed pair; ``ripple_db`` is the
    summed blend ripple there. ``seed_*`` is the pair correlation alone would
    have shipped — the GCC polarity sign at the delay the anchor/snap path
    selected — scored on the SAME objective so the two are comparable, and
    ``objective`` names which commitment this is (:data:`ALIGNMENT_COMMITMENTS`).
    ``grid_points``/``grid_step_us`` describe the delay grid actually searched
    per polarity (``1``/``0.0`` when there was nothing to search: the low-SNR
    commitment, a capture with no trustworthy anchor to centre a grid on, or a
    declared bound that admitted no grid point at all).

    ``left_anchor_lobe`` is the compensating control for the search's width:
    True when the committed delay sits more than half a period at Fc from the
    anchor, i.e. outside the comb lobe the methodology asks the ANCHOR to own.
    Legitimate when the objective is that sure, and the exact shape a fooled
    objective would take — so it is carried on the candidate and raises the
    selection log to WARNING rather than living only in a delta a reader would
    have to compute.
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
    #: Was the POLARITY axis pinned by the request rather than searched? The
    #: objective string cannot carry this: a pinned round still commits the
    #: prescription, so it is still
    #: :data:`ALIGNMENT_COMMITTED_EXPLICIT_PRESCRIPTION` and must stay in
    #: :data:`ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES` for the receipt's
    #: ``committed`` bit to keep meaning what it means. What changes is only
    #: whether the flat sum answered the polarity question — which is exactly
    #: what :attr:`polarity_agrees_with_sum` reports, so it is recorded as its
    #: own bit and read there.
    polarity_pinned: bool = False

    @property
    def polarity_agrees_with_sum(self) -> bool | None:
        """Did correlation's polarity answer survive the flat-sum objective?

        ``None`` on any commitment the flat-sum objective did not make on the
        POLARITY axis — the low-SNR path, where the declared design is
        committed precisely because the evidence a flat sum would read is not
        trustworthy, and a PINNED round, where the axis held one value and
        nothing was compared to anything. Recording ``False`` there would report
        a comparison that never happened, which is the same dishonesty
        :data:`_SELECTOR_COMMITTED_OBJECTIVES` refuses for the seed fallbacks —
        and on the pinned path it would be actively misleading, since a pin that
        happened to match the seed would read as correlation being confirmed by
        a search that never ran.
        """
        if self.polarity_pinned:
            return None
        if self.objective not in _FLAT_SUM_POLARITY_OBJECTIVES:
            return None
        return self.polarity_sign == self.seed_polarity_sign

    @property
    def flatness_improvement_db(self) -> float:
        """``seed_ripple − committed_ripple``: what the objective bought."""
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

    ONE objective for both halves of one decision: the ripple of ``W + s·T``
    over ``[lo_hz, hi_hz]`` with the pair's own residual delay, scored across
    ``s ∈ {+1, −1}`` and a delay grid of :data:`ALIGNMENT_FLATNESS_STEP_US`
    steps spanning ±:data:`ALIGNMENT_FLATNESS_SPAN_PERIODS` period(s) at
    ``fc_hz`` around ``anchor_delay_us``. Polarity and delay trade against each
    other — a delay can shift and shallow the null an inverted pair commands —
    so scoring them separately can only compare one against a guess about the
    other.

    Correlation stays in the loop as the SEED (``seed_polarity_sign`` at
    ``seed_delay_us``) and as the tie-break: the seed pair is always one of the
    scored candidates, and among every pair within
    :data:`ALIGNMENT_FLAT_MINIMUM_EPSILON_DB` of the global minimum the search
    keeps the one closest to the seed, preferring the seed's own polarity
    first. On a capture where flatness cannot separate the two answers this is
    a no-op.

    ``trim_w_db``/``trim_t_db`` are the LEVEL-MATCH trims
    (``solve_branch_trims``'s band-average result), not the ripple-polished
    tweeter trim: the polish's own objective needs a polarity, so scoring the
    pair at the polished trim would be circular. The polish is a level nudge
    bounded by :data:`REALIZED_LEVEL_MATCH_TOLERANCE_DB` and does not move where
    a commanded null falls, so the pair it is applied to is the pair chosen
    here.

    ``delay_bounds_us`` is the preset's declared |delay| range (already
    margin-expanded by ``crossover_v2_flow.alignment_delay_search_bounds_us``).
    Grid points outside it are dropped rather than proposed: a delay past the
    declared bound is refused downstream by the plausibility screen, and a
    candidate that loses the whole capture to a rail is not an improvement on
    one that is merely not-flattest. The seed pair is exempt, since excluding
    it could only hide the comparison this function exists to make.

    ``branch_snr_insufficient`` is the refusal (see
    :data:`ALIGNMENT_SNR_REFUSAL_VERDICT`): the pair is not searched but
    COMMITTED to the declared design — relative polarity ``+1``, since the
    branch transfer functions are already in the configured-polarity frame — at
    a delay THIS CAPTURE DID NOT SUPPLY, with the objective string saying
    which. Neither a hard stop nor a silent estimate: the household still gets
    an alignment, and the seed pair is still scored so the record shows what
    was declined.

    That refusal never reads the anchor or the GCC seed, because both are this
    capture's own answer and the anchor is exactly the quantity a low-SNR
    capture gets wrong: across nine jts3 positions it read +59.6 µs on-axis
    (the correct value) against six clustered near −211 µs and two wild, and a
    wrong commit computes a −36 dB commanded null at 1885 Hz. The delay comes
    from ``applied_alignment`` — what the speaker ALREADY plays, per
    ``docs/measurement-loop-doctrine.md``'s best-available rule — or from
    ``0.0`` when there is none to hold. Three arms, three objectives, because
    they are three different facts: the applied alignment held, the declared
    design committed where nothing is applied, and no delay committed where
    something is applied but unreadable.

    ``left_anchor_lobe`` fires when the committed delay and this capture's
    anchor disagree by more than half a period at Fc, and raises the selection
    log to WARNING. It is a ONE-DIRECTIONAL tripwire, silent whenever the two
    happen to agree and silent by construction on the ``0.0`` arms whenever the
    anchor is small, so it is not this path's disclosure: that is ``objective``
    (one of :data:`ALIGNMENT_DECLARED_POLARITY_OBJECTIVES`) beside the
    ``applied_delay_us`` / ``anchor_delay_us`` fields on
    ``event=program_analysis.alignment_selection``.

    ``explicit_delay_us`` is the host-validated PRESCRIPTION: a delay computed
    from a named measured basis, bounded to ±half a period at Fc from it, and
    refused at the request boundary otherwise. It FIXES the delay axis — the
    grid is that one point, so the committed delay is exactly the prescribed
    number and never a nearby grid point that scored better — and outranks the
    low-SNR ladder, which answers "what do we commit when nothing better is
    known". Deliberately NOT an anchor substitute: re-centring the search on it
    would let the objective wander off the prescribed value, and the candidate
    measured would then not be the one asked for.

    ``explicit_polarity_sign`` pins the other axis the same way, so the
    objective chooses the trims and the residual UNDER the pinned basin instead
    of being handed a candidate someone flipped afterwards. It applies on BOTH
    prescription arms, including the low-SNR one, where an unpinned round
    commits declared ``+1`` because polarity is this capture's question and a
    pin is not this capture's answer either. The commitment records
    :attr:`~AlignmentPairSelection.polarity_pinned` so
    ``polarity_agrees_with_sum`` reports ``None`` rather than an agreement no
    search produced.

    Returns ``None`` when the objective cannot be evaluated at all — no
    frequency bin inside ``[lo_hz, hi_hz]``, or no candidate with a finite score
    — leaving the caller on the seed and saying which in a WARNING.
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

        The two Fc guards are not redundant and neither covers the other:
        ``not (fc_hz > 0.0)`` rejects zero, negatives AND NaN (every NaN
        comparison is False), while ``isfinite`` rejects ``+inf`` — which
        passes ``> 0.0`` and would make :func:`half_period_us` return ``0.0``,
        turning this tripwire into one that fires on every commitment.
        """
        if anchor_delay_us is None or not (fc_hz > 0.0) or not math.isfinite(fc_hz):
            return False
        return abs(delay_us - anchor_delay_us) > half_period_us(fc_hz)

    seed_ripple_db = _ripple_at(seed_polarity_sign, seed_delay_us)

    if explicit_delay_us is not None and branch_snr_insufficient:
        # The prescription stands (it did not come from this capture); an
        # UNPINNED polarity does not (it did), so it commits declared relative
        # polarity ``+1`` for the reason the arm below states. A PINNED polarity
        # did not come from this capture either, so the refusal has no more to
        # say about it than it does about the delay.
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
        # Neither `anchor_delay_us` nor `seed_delay_us` may be read here: both
        # are THIS capture's own answer, and this capture was refused.
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

    # The delay is only scorable against an anchor: with none,
    # `summed_model_residual_delay_us` is 0.0 for every candidate delay, so the
    # objective is blind to the axis. Search the polarity alone rather than
    # walking a grid of identical scores and calling the winner a selection.
    grid_step_us = 0.0
    delays = [seed_delay_us]
    # A prescription fixes the delay axis to exactly one point, and leaves the
    # polarity axis to the objective unless `explicit_polarity_sign` pins that
    # too (the `signs` line below). The seed is NOT appended here, though it is
    # everywhere else: appending it would let the search return the seed's delay
    # whenever the seed scored within the flat-minimum epsilon, and a candidate
    # that silently measures the estimator's answer instead of the prescribed
    # one is the failure this path exists to remove. `seed_ripple_db` above
    # already scored the seed, so the record still shows what was displaced.
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
        # A declared bound that admits no grid point at all leaves only the
        # exempt seed, which is the same "nothing was searched" state the
        # no-anchor path reports — so report it the same way (1 / 0.0) rather
        # than quoting the step of a grid that ended up empty.
        if grid:
            grid_step_us = step_us
        delays = [*grid, seed_delay_us]

    # The polarity axis, pinned the same way the delay axis is: one value, and
    # the objective solves everything else under it. The seed sign is NOT added
    # back for the reason the seed delay is not — a pinned round that scored the
    # seed's basin could commit it under the pin's name. `seed_ripple_db` above
    # already scored the seed pair, so the record still shows what was
    # displaced.
    signs = (
        (1, -1) if explicit_polarity_sign is None
        else (int(explicit_polarity_sign),)
    )
    pairs = [(sign, delay) for sign in signs for delay in delays]
    # Non-finite scores are not candidates. `_ripple_db` is finite for any
    # finite band (its 1e-12 magnitude clamp keeps the log bounded), so this
    # only fires on a branch TF that already carries NaN/inf — but then `min()`
    # below propagates the NaN, the epsilon comparison rejects every pair, and
    # `min()` of the empty result raises ValueError.
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
    # Flat-minimum regularization (see ALIGNMENT_FLAT_MINIMUM_EPSILON_DB):
    # among everything within epsilon of the global minimum, keep the seed's
    # polarity first and then the delay closest to the seed. A sharp minimum
    # has one member and this is plain argmin; a shallow basin resolves toward
    # what correlation already said.
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

    Each ``*_span_hz`` is that branch's OWN validity span: the band it was
    excited across, narrowed by any reflection-gate floor. Not the shared
    both-branches-excited overlap — a branch is read only where it played.
    The returned halves are ``[Fc/ρ, Fc]`` (woofer) and ``[Fc, Fc·ρ]``
    (tweeter) with the largest ``ρ ≤ OVERLAP_OCTAVE_RATIO`` that fits inside
    BOTH spans, so the branches are always sampled mirror-symmetrically about
    Fc and neither half ever reaches outside its own branch's excitation.

    Both inner edges are load-bearing, not just the outer ones: the halves meet
    AT Fc, so the woofer's span must reach UP to Fc and the tweeter's DOWN to
    it. Nothing in the system ties a declared driver band to the chosen Fc, so
    a tweeter swept from 2.5 kHz under a 2 kHz Fc is representable — and would
    put 250 Hz of never-excited deconvolution noise inside the tweeter's half,
    the exact failure :func:`solve_branch_trims` exists to avoid. That
    configuration has no level match in it at all and raises, through the
    catch-all seam in :mod:`jasper.web.correction_crossover_v2` that classifies
    an unanalysable capture as ``internal_error`` — never a guessed trim on the
    hardware.

    SSOT for the band pair: :func:`solve_branch_trims` computes the levels and
    ``_build_candidate`` discloses the bands, from this one derivation.
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
    # ``ratio > 1`` is guaranteed by the two checks above — they require
    # ``w_lo_bound < fc_hz`` and ``fc_hz < t_hi_bound``, so both quotients
    # exceed 1 — and a NaN bound fails those comparisons rather than reaching
    # here. No third guard: an unreachable raise is a claim no test can make.
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

    THIS IS THE LEVEL FACT. "Level-matched" means matched acoustic output
    through the HANDOVER REGION: after the target filters, the two driver
    traces are equal at Fc and each sits −6 dB against the summed target — the
    Linkwitz-Riley unity condition, Rane/Bohn's "amplitude response of each is
    −6 dB at crossover", McCarthy's equal-level acoustic-crossover definition.
    The linear-frequency power mean this function takes over the mirrored
    ±1-octave halves IS that consensus statistic.

    Passband-average sensitivity is NOT the level fact.
    :func:`~jasper.active_speaker.linearization_fit.driver_core_level_db` is the
    STARTING ESTIMATE that sizes a horn's fixed attenuation; on a horn with a
    sloped response the two conventions legitimately differ by many dB. That
    difference is disclosed, never reconciled — see
    :func:`~jasper.active_speaker.crossover_v2.intervention.compare_level_definitions`.

    Two constraints on the statistic. *Slope:* with LR4 the sensitivity to
    level error concentrates AT Fc, so the ±1-octave matching band is right and
    the null test carries the weight; a shallower crossover would widen it
    toward ±1.5 octaves. *Directivity (Toole):* where woofer beaming and horn
    directivity mismatch, the on-axis, listening-window and power-response
    ratios differ and there is no single correct level — which is why the axis
    these levels were read on is stated rather than assumed
    (``intervention.LEVEL_MATCH_AXIS``).

    Each ``*_span_hz`` is that branch's own validity span (default Fc ∓ 1
    octave); :func:`branch_level_bands_hz` turns the pair into the mirrored
    halves ``[Fc/ρ, Fc]`` (woofer) and ``[Fc, Fc·ρ]`` (tweeter). A branch is
    never read outside the band it was excited and gated in.

    Mirrored halves rather than one shared band, because this band-power-averages
    ``|W|`` and ``|T|`` and that is a level match only when each branch is
    weighted symmetrically about Fc. The SHARED both-branches-excited overlap
    band clamps its lower edge UP to the tweeter's sweep floor, so on a 2-way
    whose tweeter sweep starts AT Fc it leaves ``[Fc, 2Fc]`` — entirely on the
    side where the woofer is inside its crossover skirt and the tweeter is
    climbing into its passband, which measures skirt depth rather than level.
    On an ideal LR4 pair with two EQUAL-sensitivity drivers ``[Fc, 2Fc]``
    returns +10.59 dB instead of 0 (closed form), and on archived JTS3 captures
    it put the measured tweeter trim 10.9-13.1 dB below the same analysis's own
    per-driver ``target_level_db`` frame. Widening back to the nominal band is
    not the fix either: the tweeter was never EXCITED below Fc, so those bins
    are deconvolution noise that dilutes its power mean (+3.03 dB residual bias
    on the same ideal pair). Reading each branch only on its own side removes
    both problems — residual bias +0.54 dB at ρ=2, shrinking with ρ.

    That remaining +0.54 dB is a KNOWN systematic, not a limit of the method:
    the linear-frequency FFT bin grid weights the wider upper half harder, and
    the same estimator integrated in log-frequency is exactly 0.000 dB on an
    ideal pair (verified by quadrature). The linear average stands so a change
    of measure does not move every measured trim on top of the 10-13 dB this
    frame already moves, with no capture able to separate the two effects.

    Public as the level match's SSOT: the v2 session documents its own anchored
    give-back seed against this function and the contract tests import it. Its
    sibling :func:`overlap_band_hz` is public because the session CALLS it.
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
    """Each branch's REALIZED power-band level, on its own side of Fc, after the
    committed trim — and whether the two agree.

    The assertion nothing else in the chain makes: the other comparators
    compare the speaker to itself or to flat, and none compares the two
    drivers' realized handoff levels to each other. That is the gap a ~9 dB-dark
    tweeter walked through — every stage individually satisfied, because no
    stage was asked the one question whose answer was wrong.

    ``W``/``T`` are the branch transfer functions AS THEY WILL BE EMITTED — on
    the v2 path, the linearized pair ``resp.complex_tf * correction``, not the
    raw measurement. ``trim_*_db`` are the trims the graph will actually carry.
    This function applies them and re-reads the levels, so it grades the
    committed decision rather than re-litigating it.

    One estimator, not a second opinion: the levels come from
    :func:`solve_branch_trims` on the trimmed pair — the SAME power-band
    average over the SAME :func:`branch_level_bands_hz` halves that set the trim
    in the first place. A check with its own band or averaging rule would be a
    rival estimate, and a disagreement between two rivals says nothing about
    which is right. Reusing the estimator keeps this a strict closed-loop
    question and inherits its known +0.54 dB systematic rather than adding an
    unknown one.

    Each branch is read only on its own side of Fc, never the shared
    both-branches-excited overlap: reading a branch inside the other's
    crossover skirt measures skirt depth, not level.

    Raises ``ValueError`` (through :func:`branch_level_bands_hz`) when a span
    does not reach Fc, and (through :func:`_band_average_db`) when a half
    contains no bins — the same raise surface, into the same ``internal_error``
    seam, as the trim solve it wraps. Never a guessed verdict.
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

    The shared computation behind the trim-rejection telemetry PAIR, so that
    pair is commensurable by construction rather than by two call sites
    agreeing: :func:`solve_ripple_optimal_trim` evaluates every scanned
    candidate through it, and the linearization planner calls it once more at
    the ANCHORED trim, so the two ripples the rejection event logs differ in
    exactly one variable.

    It is not this module's only summed-ripple site:
    :func:`_select_alignment_pair` composes the same two functions over its own
    (polarity, delay) grid. That is a different question — which PAIR sums
    flatter, at a fixed trim — and it deliberately scores at the band-average
    trim rather than through this helper, because this helper's objective takes
    a polarity as input and the pair search would then be circular.

    Masking is internal, so full-length and already-band-sliced inputs give the
    same number; the scan pre-slices only to keep its per-candidate sums cheap.

    No delay term: this is the same zero-residual frame the scan optimizes in.
    A caller that wants a delay-carrying sum builds it with
    :func:`predicted_branch_sum` directly, which is what ``_build_candidate``'s
    own alignment evidence does with its residual.

    ``inf`` when the band holds no bins — :func:`_ripple_db`'s own answer,
    carried rather than converted into a fabricated number.

    Expects Python scalars for the trims and ``complex128``/``float64`` arrays
    for the branches, which is what every caller hands it. The explicit
    ``float()``/``int()`` casts below make the scalars weak under NEP 50, so a
    ``float32`` scalar or a ``complex64`` branch pair would promote differently
    here.
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
    """Ripple-minimizing tweeter trim, scanned around the band-average seed and
    regularized toward that seed on a flat minimum.

    Scans the tweeter trim and keeps whichever value minimizes the SUMMED
    branch response's ripple (max-min dB) over ``[lo_hz, hi_hz]``, evaluated by
    :func:`ripple_at_trim` rather than a second flatness metric. It is a
    flatness POLISH on an already-correct level — ``solve_branch_trims`` reads
    each branch on its own side of Fc, so the band bias this once compensated
    for is gone at its source — and ``_build_candidate`` runs it only where
    ``[lo_hz, hi_hz]`` straddles Fc. Its own contract is just "optimize ripple
    over whatever band it is handed".

    The woofer/reference branch's trim (``trim_w_db``) is held FIXED: ripple
    depends only on the RELATIVE gain between branches, so scanning one side
    alone still explores the full space of achievable relative gains. It
    defaults to 0.0, matching ``solve_branch_trims``'s convention that the
    quieter branch is left unattenuated; a caller whose band-average solve gave
    a nonzero woofer trim should pass that value so the scan is centered on the
    summed response that will actually be applied.

    Search window: ``seed_trim_db +/- window_db`` at ``step_db`` steps
    (defaults +/-10 dB / 0.1 dB), clamped to the physically valid attenuation
    range [:data:`RIPPLE_TRIM_MIN_DB`, :data:`RIPPLE_TRIM_MAX_DB`] — a trim is
    never net gain and never beyond the shared -60 dB floor, so the scan must
    not even EVALUATE an unphysical candidate.

    Selection is flat-minimum-regularized, not a bare argmin: among every
    scanned candidate whose ripple is within ``flat_minimum_epsilon_db`` of the
    GLOBAL minimum ripple found in this scan, the one CLOSEST TO THE SEED wins.
    A sharp, unique minimum degenerates to that single point; a shallow bowl
    instead prefers whichever near-optimal candidate drifts LEAST from the
    band-average trim, trading an inaudible amount of measured flatness for
    session-to-session repeatability, since the exact minimizer of a shallow
    bowl is sensitive to measurement noise. Exact ties are a special case of
    this rule and need no separate handling.

    Returns ``(trim_t_db, ripple_db, seed_trim_db)``: the selected trim, the
    summed-response ripple (dB, max-min) AT that trim, and the seed it was
    scanned around — echoed back so a caller building an evidence comparison
    need not thread the seed through separately.

    ``lo_hz``/``hi_hz`` default to Fc +/- 1 octave like ``solve_branch_trims``;
    every caller passes its own gating-clamped band explicitly.
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
        # The seed's own window holds no physically valid attenuation value at
        # all — clamp the seed itself into range rather than searching an empty
        # set.
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
        # Flat-minimum regularization: not just the argmin, but the
        # closest-to-seed candidate among everything within epsilon of the
        # GLOBAL minimum (see the docstring) — a sharp minimum has only one
        # such candidate, so this is a strict generalization of bare argmin.
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


# --------------------------------------------------------------------------- #
# CHECK helpers
# --------------------------------------------------------------------------- #


# How much of the scheduled ambient window must actually be present in the
# capture for it to count as evidence. A capture that started late clips the
# window's HEAD (never its tail — the pilots follow it), and RMS is
# length-independent, so a shortened window is still an honest floor estimate;
# what this rejects is the degenerate case where a couple of hundred samples
# survive and the estimate is noise about noise. Below the fraction the caller
# gets ``None`` and the analysis degrades to "no ambient evidence, trust the
# pilots" — never to a fabricated floor.
#
# ONE policy, both windows: CHECK's 12 s session-ambient window
# (`_ambient_from_capture`) and MEASURE/VERIFY's 1 s pilot-ambient window
# (`_pilot_ambient_samples`) ask the same question of the same kind of
# evidence, so they share this constant rather than each carrying a number that
# can drift from the other.
AMBIENT_MIN_USABLE_FRACTION = 0.5


def _ambient_from_capture(
    capture: np.ndarray, sample_rate: int, ambient_seg: ProgramSegment, global_offset: int
) -> tuple[np.ndarray | None, dict[str, Any]]:
    """CHECK's session-ambient window and its band-floor report.

    The window is CLIPPED to the capture, never SLID along it: ``end`` is
    computed from the window's own (possibly negative) schedule position, not
    from the clamped start, exactly as `_pilot_ambient_samples` does. Computing
    it from the clamped start walks a capture that began ``D`` seconds late
    forward onto whatever the schedule put AFTER the window — for the shipped
    CHECK program that is the courtesy prelude, 0.6 s of −18 dBFS beep at
    [12.0, 12.6) s butted directly against the 12 s window. On that geometry a
    0.6 s late start reads the room floor 39.5 dB hot (−70.00 → −30.52 dBFS
    window RMS; worst framed band −74.65 → −52.85 dBFS), which is not a floor
    at all — it is the beep. That number feeds BOTH `_snr_floor_ok` and
    `_solve_gain_plan`.

    Below :data:`AMBIENT_MIN_USABLE_FRACTION` of the window there is nothing
    left to measure, and this degrades the same honest way the pilot helper
    does: ``None`` samples plus an EMPTY band report, which
    `_snr_floor_ok` reads as False and `_solve_gain_plan` discloses as
    ``GAIN_BOUND_NO_AMBIENT_EVIDENCE``. Both degradations are fail-closed;
    neither fabricates a floor. The empty report is produced BY
    ``framed_ambient_band_report`` (from an empty array) rather than written
    out here, so that module stays the only owner of the report's shape.
    """
    begin = global_offset + ambient_seg.start_sample
    start = max(0, begin)
    end = min(capture.size, begin + ambient_seg.n_samples)
    if end - start < AMBIENT_MIN_USABLE_FRACTION * ambient_seg.n_samples:
        # Never a silent degrade: this is the difference between "the room was
        # quiet" and "we never heard the room", and it costs the household a
        # commissioning attempt. The LOG carries the surviving-audio truth
        # (how much window was left, how late the capture was) rather than the
        # report — the report keeps the one shape `snr_policy` owns, so
        # widening it here would make this function a second author of it.
        log_event(
            logger,
            "program_analysis.ambient_window_unusable",
            level=logging.WARNING,
            scheduled_samples=int(ambient_seg.n_samples),
            surviving_samples=int(max(0, end - start)),
            capture_late_samples=int(max(0, -begin)),
        )
        empty = np.empty(0, dtype=np.float64)
        return None, snr_policy.framed_ambient_band_report(empty, sample_rate, percentile=95)
    samples = capture[start:end]
    return samples, snr_policy.framed_ambient_band_report(samples, sample_rate, percentile=95)


def _pilot_ambient_samples(
    program: ExcitationProgram, capture: np.ndarray, global_offset: int,
) -> np.ndarray | None:
    """The program's own room-listening window, or ``None`` if it has none.

    MEASURE/VERIFY programs carry an
    :data:`~jasper.audio_measurement.program.AMBIENT_SEGMENT_ID` window ahead
    of their leading pilot pair (``program.PILOT_AMBIENT_WINDOW_S``) so
    `_pilot_observations`' in-band SNR guard has something to measure against
    on those phases too; without it the guard's input is ``+inf`` by
    construction and it can never fire.

    Located by SCHEDULE offset (like `_ambient_from_capture`), not by
    correlation — it is silence, so there is nothing to correlate. That makes
    it exact for a live capture and meaningless for a cross-schedule replay of
    an archived capture, where the window lands on whatever that schedule had
    at the position. The replay failure direction is the safe one: a too-loud
    "ambient" reads as low SNR, which resolves ``linearity_ok`` to ``None``
    (unknown) and can never manufacture a false AGC accusation.

    The window is CLIPPED to the capture, never SLID along it: ``end`` is
    computed from the window's own (possibly negative) schedule position, not
    from the clamped start, so a capture that began after the program did
    yields a shorter window rather than one that has walked forward onto the
    pilot it is supposed to measure the floor for. `_ambient_from_capture`
    (CHECK's 12 s window) has the same shape, and both share
    :data:`AMBIENT_MIN_USABLE_FRACTION`.
    """
    try:
        seg = program.segment(AMBIENT_SEGMENT_ID)
    except KeyError:
        return None
    begin = global_offset + seg.start_sample
    start = max(0, begin)
    end = min(capture.size, begin + seg.n_samples)
    if end - start < AMBIENT_MIN_USABLE_FRACTION * seg.n_samples:
        return None
    return capture[start:end]


def _band_power(samples: np.ndarray, sample_rate: int, f1_hz: float, f2_hz: float) -> float:
    """Mean-square (linear power) of ``samples`` restricted to ``[f1_hz, f2_hz]``.

    Hann-windowed before :func:`_bandlimit`'s zero-phase FFT bandpass: a raw
    slice (a pilot window, or the ambient window) almost never starts/ends at
    a zero crossing, so an un-windowed brick-wall FFT filter treats the slice
    as implicitly periodic and leaks broadband energy from that boundary
    discontinuity into every band — including a driver's OWN declared band
    read back out of its own capture. The Hann taper (a fixed, length-
    independent mean-square ratio) suppresses that leak; the constant
    windowing loss it introduces cancels out of every comparison that reads
    both sides through this same function (the channel-map TARGET/CROSS rise,
    and the pilot linearity delta), so it does not need correcting for here.

    Returned as LINEAR power (not dB) so a caller can SUBTRACT an ambient
    noise-power estimate before converting to dB — subtraction is only valid
    in the power domain, never on dB values directly.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 8:
        return 0.0
    filtered = _bandlimit(x * np.hanning(x.size), sample_rate, f1_hz, f2_hz)
    return float(np.mean(np.square(filtered)))


def _band_rms_dbfs(samples: np.ndarray, sample_rate: int, f1_hz: float, f2_hz: float) -> float:
    """RMS level (dBFS) of ``samples`` restricted to ``[f1_hz, f2_hz]``.

    Thin dB wrapper over :func:`_band_power` (``20·log10(rms) ==
    10·log10(power)``) — see that function's docstring for the windowing
    rationale.
    """
    power = _band_power(samples, sample_rate, f1_hz, f2_hz)
    if power <= 0 or not math.isfinite(power):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 10.0 * math.log10(power))


def _pilot_trim_fade(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Drop the composer's fixed edge fade from a located pilot segment.

    See `PILOT_FADE_TRIM_S`. Falls back to the untrimmed segment when
    trimming would leave nothing (a pathologically short/corrupt capture) —
    the level estimate then rides the fade too rather than raising here; the
    SNR/linearity gates downstream still catch a genuinely bad capture.
    """
    trim = int(round(PILOT_FADE_TRIM_S * sample_rate))
    if samples.size <= 2 * trim:
        return samples
    return samples[trim:-trim]


def _ambient_subtracted_dbfs(power: float, ambient_power: float) -> float:
    """dB of ``power`` after subtracting ``ambient_power`` (power domain).

    ``ambient_power`` is 0.0 when there is no ambient evidence (a program
    composed without a room-listening window, or one whose window fell outside
    the capture — see `_pilot_ambient_samples`); subtracting zero is a no-op,
    so this degrades to plain in-band RMS in that case.
    """
    signal_power = power - ambient_power if ambient_power > 0 else power
    if signal_power <= 0 or not math.isfinite(signal_power):
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 10.0 * math.log10(signal_power))


def _pilot_in_band_snr_db(power: float, ambient_power: float) -> float:
    """SNR (dB) of the ambient-subtracted estimate: ``(P − N) / N``.

    In the ``P = S + N`` model (measured power = signal + ambient noise
    power), this is exactly ``S / N`` — the linear SNR the
    `PILOT_MIN_SNR_DB` derivation is stated in terms of. Returns ``+inf``
    when there is no ambient evidence to contaminate the estimate (nothing to
    validate against, so nothing to distrust), and ``-inf`` when the pilot's
    measured power does not even exceed the ambient (the estimate is
    unusable).
    """
    if ambient_power <= 0 or not math.isfinite(ambient_power):
        return math.inf
    ratio = power / ambient_power - 1.0
    if ratio <= 0 or not math.isfinite(ratio):
        return -math.inf
    return 10.0 * math.log10(ratio)


def _band_exclusive_pieces(
    other_band: tuple[float, float], own_band: tuple[float, float]
) -> list[tuple[float, float]]:
    """The part(s) of ``other_band`` that fall OUTSIDE ``own_band``.

    Two drivers' declared bands legitimately overlap around the crossover point
    (design §5.2/§5.4), so a role's own pilot content routinely also falls
    inside the shared part of an adjacent role's declared band — and that
    shared part carries no map-discrimination signal. The CROSS test (see
    `_channel_map_ok`) only asks about the EXCLUSIVE remainder of the other
    role's band (interval subtraction; 0, 1, or 2 pieces), where a
    correctly-wired driver's own out-of-band rolloff makes energy absent.
    """
    o1, o2 = other_band
    a1, a2 = own_band
    pieces: list[tuple[float, float]] = []
    if o1 < a1:
        pieces.append((o1, min(o2, a1)))
    if o2 > a2:
        pieces.append((max(o1, a2), o2))
    return [(lo, hi) for lo, hi in pieces if hi > lo]


def _pilot_observations(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    *,
    ambient_samples: np.ndarray | None = None,
    channel_map_ambient_samples: np.ndarray | None = None,
) -> list[PilotObservation]:
    """Per-role pilot level/linearity/channel-map observations (design §3.4).

    Level is measured band-relative (each pilot's OWN declared band, via
    `_band_power` — the same Hann+bandpass mechanism `_channel_map_ok` uses,
    not a second filtering idiom) and, when an ambient window is available,
    ambient-power-subtracted before converting to dB. A full-band PEAK estimate
    instead lets LF room rumble inflate the quiet pilot's level and compress
    the captured delta (see `LINEARITY_TOLERANCE_DB`). With no window at all
    (``ambient_samples=None``) subtraction degrades to a no-op
    (`_ambient_subtracted_dbfs`) and SNR is trusted unconditionally, because
    there is nothing to validate against.

    Two ambient parameters, deliberately. ``ambient_samples`` feeds the
    level/SNR path; ``channel_map_ambient_samples`` feeds `_channel_map_ok`'s
    TARGET/CROSS rise test. CHECK passes the same 12 s window to both — the
    long, framed, percentile-based room-floor estimate
    `CHANNEL_MAP_TARGET_RISE_DB` was calibrated against. MEASURE/VERIFY pass
    only the first, because their pre-pilot window is a ~1 s spot estimate
    sized for the SNR guard rather than the duration-independent statistic the
    rise thresholds were derived from, and because ``analysis.channel_map_ok``
    is routed on at exactly ONE site — ``crossover_v2_flow._check_verdict``,
    which maps an explicit ``False`` to the hard-stop
    ``channel_map_mismatch``. Threading the window here would change no verdict
    while leaving a False flag on those analyses, ARMED for whoever adds a
    routing branch: a pilot pair a few dB over the floor would then hard-stop
    with copy blaming the speaker wiring, on evidence never calibrated for a
    1 s window. Their channel-map check therefore keeps the
    total-in-band-energy-fraction fallback, whose one-sided reporting is
    `_channel_map_ok`'s to state.

    The located segment's fixed composer fade (`_pilot_trim_fade`) is trimmed
    before measuring so the RMS estimate rides the steady-state portion, not
    the ramp.

    Low-SNR honest routing: the quiet (lo) pilot is the binding constraint
    (10 dB quieter than hi, same ambient), so its in-band SNR
    (`_pilot_in_band_snr_db`) gates trust. Below `PILOT_MIN_SNR_DB` the
    ambient-subtracted estimate is not reliable either way — ``linearity_ok``
    is ``None``, i.e. UNKNOWN, never a false FAILURE and never a false PASS —
    and ``snr_valid=False`` lets the caller route to the honest
    room/positioning reason instead of blaming the phone's AGC (see
    `ProgramAnalysis.pilot_snr_ok` and `crossover_v2_flow._consume_check`).

    ``peak_lo_dbfs``/``peak_hi_dbfs`` are a SEPARATE, non-ambient-subtracted
    measurement: the full-band `_peak_dbfs` of the located, untrimmed samples.
    They exist because `_solve_gain_plan` uses a pilot level ABSOLUTELY
    (``k = level - gain_db``, an estimate of the whole capture chain's dB
    gain), not as a delta, so feeding it the ambient-subtracted level would
    shift that absolute reference by however much ambient power was subtracted
    — measured at 13-17 dB across two real captures — retuning
    `MeasurementPriors.target_capture_dbfs`'s capture-PEAK target hotter than
    intended. A band-limited peak carries its own leakage bias (up to ~1.3 dB
    on a real capture) whether or not the slice is windowed first, so the
    full-band computation is what this consumer reads.
    """
    by_id = {loc.segment_id: loc for loc in locations}
    roles = sorted({seg.role for seg in program.segments if seg.kind == KIND_PILOT and seg.role})
    # Every role's declared band, so each role's channel-map check can ask
    # whether energy also rose in every OTHER role's band (the CROSS test).
    # A pilot (stimulus) segment always carries f1_hz/f2_hz — enforced by
    # ProgramSegment.__post_init__ — so a None here means a corrupt schedule.
    role_bands: dict[str, tuple[float, float]] = {}
    for role in roles:
        hi_seg = program.segment(f"pilot_{role}_hi")
        if hi_seg.f1_hz is None or hi_seg.f2_hz is None:
            raise ValueError(f"pilot segment for role {role!r} has no declared band")
        role_bands[role] = (hi_seg.f1_hz, hi_seg.f2_hz)

    ambient_arr = None if ambient_samples is None else np.asarray(ambient_samples)
    if ambient_arr is not None and ambient_arr.size < 8:
        ambient_arr = None
    has_ambient = ambient_arr is not None

    out: list[PilotObservation] = []
    for role in roles:
        lo_seg = program.segment(f"pilot_{role}_lo")
        hi_seg = program.segment(f"pilot_{role}_hi")
        lo_loc = by_id[f"pilot_{role}_lo"]
        hi_loc = by_id[f"pilot_{role}_hi"]
        lo_samples = capture[lo_loc.located_start:lo_loc.located_start + lo_seg.n_samples]
        hi_samples = capture[hi_loc.located_start:hi_loc.located_start + hi_seg.n_samples]

        own_f1, own_f2 = role_bands[role]
        lo_interior = _pilot_trim_fade(lo_samples, sample_rate)
        hi_interior = _pilot_trim_fade(hi_samples, sample_rate)
        lo_power = _band_power(lo_interior, sample_rate, own_f1, own_f2)
        hi_power = _band_power(hi_interior, sample_rate, own_f1, own_f2)
        ambient_power = (
            _band_power(ambient_arr, sample_rate, own_f1, own_f2)
            if ambient_arr is not None
            else 0.0
        )

        level_lo = _ambient_subtracted_dbfs(lo_power, ambient_power)
        level_hi = _ambient_subtracted_dbfs(hi_power, ambient_power)
        programmed_delta = hi_seg.gain_db - lo_seg.gain_db
        captured_delta = level_hi - level_lo

        lo_snr_db = _pilot_in_band_snr_db(lo_power, ambient_power) if has_ambient else math.inf
        snr_valid = lo_snr_db >= PILOT_MIN_SNR_DB
        # UNKNOWN below the SNR floor, never True: the captured delta is not
        # evidence down there in EITHER direction, and `True` is a claim — one
        # session published `linearity_ok=true` beside a captured delta of
        # -60.9 dB against a programmed 10.0 dB, which is the absence of a
        # linearity check rather than a passing one. `None` never registers as
        # a FAILURE either, and it stops reading as a PASS to a consumer that
        # does not also check `snr_valid`.
        linearity_ok = (
            None if not snr_valid
            else abs(captured_delta - programmed_delta) <= LINEARITY_TOLERANCE_DB
        )

        # Gain-solve reference: full-band peak (see the docstring above) —
        # deliberately NOT the ambient-subtracted level.
        peak_lo = _peak_dbfs(lo_samples)
        peak_hi = _peak_dbfs(hi_samples)

        own_band = role_bands[role]
        other_bands = tuple(
            piece
            for other_role, other_band in role_bands.items()
            if other_role != role
            for piece in _band_exclusive_pieces(other_band, own_band)
        )
        channel_ok, channel_target_rise_db, channel_cross_rise_db = _channel_map_ok(
            hi_samples, sample_rate, hi_seg,
            ambient_samples=channel_map_ambient_samples, other_bands=other_bands,
        )
        out.append(PilotObservation(
            role=role,
            level_lo_dbfs=level_lo,
            level_hi_dbfs=level_hi,
            programmed_delta_db=programmed_delta,
            captured_delta_db=captured_delta,
            linearity_ok=linearity_ok,
            channel_map_ok=channel_ok,
            snr_valid=snr_valid,
            peak_lo_dbfs=peak_lo,
            peak_hi_dbfs=peak_hi,
            snr_db=lo_snr_db,
            channel_map_target_rise_db=channel_target_rise_db,
            channel_map_cross_rise_db=channel_cross_rise_db,
            programmed_hi_gain_db=hi_seg.gain_db,
        ))
    return out


def _aggregate_tri_state_ok(
    verdicts: Sequence[bool | None],
) -> bool | None:
    """Reduce per-role tri-state verdicts to one, FAILURE-dominant.

    A FAILURE anywhere is the verdict; otherwise an UNKNOWN anywhere makes
    the whole verdict unknown, because "the roles we could read were fine"
    is not the same claim as "every role was fine". ``None`` for no roles at
    all — no evidence, the same value the caller published before any of
    these verdicts were tri-state.

    Written out rather than left as ``all(...)``: Python's ``all()`` folds
    ``None`` to False, so an unknown would leave here as a FAILURE. For
    ``linearity_ok`` that is the mic accusation the low-SNR routing exists to
    prevent; for ``channel_map_ok`` it is worse — a hard stop telling a
    household to open its speaker and rewire it, decided on evidence that was
    never there.

    One fold for both because that is one decision, not two — a second copy is
    a second place for "what does unknown mean here" to drift.
    """
    if not verdicts:
        return None
    if any(v is False for v in verdicts):
        return False
    if any(v is None for v in verdicts):
        return None
    return True


def _aggregate_linearity_ok(
    pilots: Sequence[PilotObservation],
) -> bool | None:
    """Per-pilot ``linearity_ok`` over the roles, through the shared fold."""
    return _aggregate_tri_state_ok([p.linearity_ok for p in pilots])


def _pilot_verdicts(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    *,
    global_offset: int,
) -> tuple[tuple[PilotObservation, ...], bool | None, bool | None, bool | None]:
    """Pilot observations + the aggregate linearity / channel-map / SNR verdicts.

    ``None`` verdicts when the program carries no pilots, so a caller can
    distinguish "no pilot evidence" from "pilot evidence, all clean". Shared by
    v2 MEASURE / VERIFY, whose leading pilot pair (design §5.2) carries
    per-capture linearity evidence CHECK-only verification cannot.

    Those programs also carry a short room-listening window immediately ahead
    of that pilot pair, so ``pilot_snr_ok`` is a REAL verdict here:
    `_pilot_ambient_samples` reads the window at its schedule offset and hands
    it to the level/SNR path. The channel-map check still uses
    `_channel_map_ok`'s total-in-band-energy-fraction fallback — see
    `_pilot_observations` for why that short window must not feed the rise
    test. That fallback is one-sided, so these phases publish ``None`` unless a
    role actually failed it.
    """
    pilots = _pilot_observations(
        program, capture, sample_rate, locations,
        ambient_samples=_pilot_ambient_samples(program, capture, global_offset),
    )
    linearity_ok = _aggregate_linearity_ok(pilots)
    channel_map_ok = _aggregate_tri_state_ok([p.channel_map_ok for p in pilots])
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    return tuple(pilots), linearity_ok, channel_map_ok, pilot_snr_ok


def _channel_map_ok(
    samples: np.ndarray,
    sample_rate: int,
    seg: ProgramSegment,
    *,
    ambient_samples: np.ndarray | None = None,
    other_bands: Sequence[tuple[float, float]] = (),
) -> tuple[bool | None, float | None, float | None]:
    """Band-relative channel-map sanity (design note above `CHANNEL_MAP_*`).

    Given a leading ambient (room-noise) window — CHECK's own 12 s ambient
    segment — this asks two independent questions per pilot, rather than the
    single "is most of the TOTAL energy in-band" fraction test a concurrent,
    unrelated room-noise band can veto even when the driver under test is
    behaving correctly:

    1. TARGET: did THIS driver's own declared band rise
       ``CHANNEL_MAP_TARGET_RISE_DB`` above that band's ambient level? (the
       driver actually played, above the room's floor in its own band.)
    2. CROSS: did every OTHER driver's band stay at least
       ``CHANNEL_MAP_MIN_ISOLATION_DB`` BELOW this driver's own rise — i.e. is
       the ISOLATION RATIO ``target_rise - cross_rise`` clear of that bound?
       This guards ABNORMAL CROSS-BAND ENERGY (bleed, skirt and nonlinearity
       classes, and one signal reaching both bands at once) and fails closed on
       it. It is **not** the mis-wire discriminator — a wiring fault changes
       which DRIVER radiates, not which BAND carries the energy, so rung 1 is
       what fires on one. A ratio rather than a fixed additive cross-rise bound
       because the cross energy an honest capture carries is skirt content at
       a roughly fixed RELATIVE level, so an additive bound is level-dependent
       and refuses healthy speakers at honest SNR — see the derivation and the
       hardware table above ``CHANNEL_MAP_MIN_ISOLATION_DB``.

       Rung 2 is only JUDGED above ``CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB``.
       The cross rises are measured and published either way, but below that
       threshold the TARGET floor governs alone, because the CROSS test RAISES
       the effective floor to ``max(FLOOR, BOUND + cross_rise)`` and judging it
       on a quiet capture turns a retriable ``snr_floor`` into a rewire hard
       stop. That constant's comment carries the measured case.

    Without an ambient window, falls back to the fraction test: energy inside
    the declared band must exceed half of the pilot window's TOTAL spectral
    energy. That is the path v2 MEASURE/VERIFY take — their pre-pilot window is
    deliberately NOT threaded here (see `_pilot_observations`'s two-ambient-
    parameters note) — and the path a program with no window at all takes.

    That fallback is ONE-SIDED evidence and is reported one-sided. A cleared
    fraction is ``None`` (UNKNOWN): broadband room noise clears it too, so it
    does not say the RIGHT driver put the energy there. A failed fraction keeps
    its ``False``: energy that is NOT in the band the pilot was scheduled in is
    a positive observation about this window, and it is the only channel-map
    evidence a capture with no ambient window still carries.

    Returns ``(ok, target_rise_db, cross_rise_db)`` — the two RAW rise numbers,
    published as a pair (surfaced on ``PilotObservation``) rather than
    collapsed into the ratio, so an operator can see WHICH half moved. The
    ratio is derived from them by `channel_map_isolation_db`, the one
    definition both this decision and every reporting surface read.
    ``cross_rise_db`` is the rise that failed the CROSS test when ``ok`` is
    False, or the worst (highest) rise observed across every other band when
    ``ok`` is True — and because ``target_rise_db`` is fixed across the loop,
    that worst rise IS the worst (lowest) isolation. Both rises are ``None`` in
    the no-ambient-window fallback path (no rise concept there);
    ``cross_rise_db`` is also ``None`` when ``other_bands`` is empty or the
    TARGET test alone already failed.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 8 or seg.f1_hz is None or seg.f2_hz is None:
        return False, None, None

    if ambient_samples is None or np.asarray(ambient_samples).size < 8:
        window = np.hanning(x.size)
        spectrum = np.abs(np.fft.rfft(x * window)) ** 2
        freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
        in_band = (freqs >= seg.f1_hz) & (freqs <= seg.f2_hz)
        total = float(np.sum(spectrum))
        if total <= 0:
            return False, None, None
        # One-sided: the fail is a finding, the pass is not evidence.
        if float(np.sum(spectrum[in_band])) / total > 0.5:
            return None, None, None
        return False, None, None

    target_rise = (
        _band_rms_dbfs(x, sample_rate, seg.f1_hz, seg.f2_hz)
        - _band_rms_dbfs(ambient_samples, sample_rate, seg.f1_hz, seg.f2_hz)
    )
    if target_rise < CHANNEL_MAP_TARGET_RISE_DB:
        return False, target_rise, None
    # The cross rises are always MEASURED (the diag publishes them either way);
    # they are only JUDGED once the target cleared its floor by the isolation
    # the ratio demands — see `CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB`, which
    # carries why judging them below that is the false-accusation bug.
    judge_cross = target_rise >= CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB
    worst_cross_rise: float | None = None
    for other_f1, other_f2 in other_bands:
        cross_rise = (
            _band_rms_dbfs(x, sample_rate, other_f1, other_f2)
            - _band_rms_dbfs(ambient_samples, sample_rate, other_f1, other_f2)
        )
        if worst_cross_rise is None or cross_rise > worst_cross_rise:
            worst_cross_rise = cross_rise
        if not judge_cross:
            continue
        isolation = channel_map_isolation_db(target_rise, cross_rise)
        # Both rises are real numbers on this path, so the ratio is always
        # defined; the ``None`` arm is fail-closed rather than dead — an
        # unjudgeable ratio must never read as a PASS on the rung that can
        # tell a household to rewire its speaker.
        if isolation is None or isolation < CHANNEL_MAP_MIN_ISOLATION_DB:
            return False, target_rise, cross_rise
    return True, target_rise, worst_cross_rise


def channel_map_isolation_db(
    target_rise_db: float | None, cross_rise_db: float | None
) -> float | None:
    """The channel-map ISOLATION RATIO: this driver's rise minus the cross rise.

    ONE definition, read by both `_channel_map_ok`'s pass/fail decision and
    every surface that reports the number (the CHECK diag event, the forensic
    analysis dump), so the ratio an operator reads beside a refusal is the
    same ratio that caused it rather than a second construction of it.

    ``None`` whenever either rise is absent, and a caller must treat that as
    "no evidence" rather than as a pass: the no-ambient-window fallback path
    computes neither rise, and a pilot with no OTHER band to compare against
    has no cross rise at all.
    """
    if target_rise_db is None or cross_rise_db is None:
        return None
    return target_rise_db - cross_rise_db


def _bands_overlap(
    lo_a: float, hi_a: float, lo_b: float, hi_b: float
) -> bool:
    return hi_a > lo_b and lo_a < hi_b


def _ambient_rows_in_band(
    band_hz: tuple[float, float],
    ambient_bands: Sequence[Any],
) -> list[tuple[float, float, float]]:
    """The ``(lo_hz, hi_hz, level_dbfs)`` ambient rows overlapping ``band_hz``.

    A row this cannot read is skipped rather than raised on. More than one
    producer writes ambient band rows (``snr_policy.framed_ambient_band_report``
    here, plus the legacy bare-band shape ``snr_policy.unwrap_noise_report``
    still normalizes), so an unreadable row must cost this solve that row's
    evidence — which the caller then discloses — never crash inside CHECK's
    accept path.
    """
    lo, hi = band_hz
    rows: list[tuple[float, float, float]] = []
    for entry in ambient_bands or ():
        if not isinstance(entry, Mapping):
            continue
        edges = entry.get("band_hz")
        if not (isinstance(edges, (list, tuple)) and len(edges) == 2):
            continue
        try:
            b_lo, b_hi, level = (
                float(edges[0]), float(edges[1]), float(entry["level_dbfs"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(b_lo) and math.isfinite(b_hi) and math.isfinite(level)):
            continue
        if _bands_overlap(lo, hi, b_lo, b_hi):
            rows.append((b_lo, b_hi, level))
    return rows


def _band_required_snr_db(
    lo_hz: float, hi_hz: float, overlap_hz: tuple[float, float] | None
) -> float:
    """The SNR the fit needs in one band, per the split SNR policy.

    ``jasper.audio_measurement.snr_policy`` splits SNR trust by what a number
    is used FOR: a magnitude/trim decision is usable at
    ``DRIVER.snr_ok_db``, while a null/alignment decision (MEASURE's GCC
    delay + polarity estimate, which reads the crossover overlap band) needs
    ``DRIVER.alignment_snr_ok_db``. A band inside the overlap window carries
    the alignment requirement; every other band carries the magnitude one.

    ``overlap_hz`` is ``None`` when no Fc prior reached this analysis. That
    resolves to the alignment requirement EVERYWHERE — the conservative
    direction, since a higher requirement means a LOUDER solve. An unknown Fc
    must never buy a quieter measurement.
    """
    if overlap_hz is None or _bands_overlap(
        lo_hz, hi_hz, overlap_hz[0], overlap_hz[1]
    ):
        return DRIVER.alignment_snr_ok_db
    return DRIVER.snr_ok_db


def _solve_role_gain(
    *,
    role: str,
    k_db: float,
    flat_target_gain_db: float,
    band_hz: tuple[float, float] | None,
    pilot_delta_db: float,
    ambient_bands: Sequence[Any],
    overlap_hz: tuple[float, float] | None,
) -> RoleGainSolve:
    """The quietest MEASURE gain for one driver that still serves the fit.

    ``k_db`` is this driver's measured chain gain (captured peak minus the
    digital gain that produced it), so a target capture peak ``C`` is reached
    at digital gain ``C - k_db``. Three floors compete for the number, and the
    LOUDEST of them wins because each is a genuine requirement.

    Every arm is peak-expressed. ``k_db`` and ``flat_target_gain_db`` are
    capture-PEAK quantities while an ambient band level and an SNR requirement
    are RMS quantities, so adding them directly under-drives MEASURE by the
    stimulus's own crest factor; each arm therefore carries
    :func:`sweep_band_crest_factor_db` (roughly 8-19 dB for the room arm's
    rows, exactly :data:`SWEEP_PEAK_TO_RMS_DB` for the pilot arm).

    * **room SNR** — the worst ``ambient + required_snr`` across the ambient
      bands overlapping this driver's own measurement band. Band-scoped on
      purpose: a room's noise is overwhelmingly low-frequency, so a tweeter
      measured from 2 kHz up genuinely needs less drive than a woofer measured
      from 150 Hz, and pinning both to one broadband figure makes MEASURE
      louder than it has to be.

      Two known coarsenesses in the ambient table
      (``snr_policy.CROSSOVER_SNR_BANDS_HZ``), both erring LOUD. (1) Its rows
      are wide, and overlap is overlap: a woofer swept from
      ``MEASURE_SWEEP_F_LO_HZ`` (150 Hz) clips the 80-160 Hz ``bass`` row by
      only 10 Hz yet inherits that row's full — LF-heavy, therefore loud —
      level, so woofers sit at ``GAIN_BOUND_FLAT_TARGET`` far more often than
      tweeters and the reduction this solve buys is mostly the tweeter's.
      (2) The table stops at 12 kHz, so a tweeter's top ~2/3 octave
      contributes no demand at all. Room noise up there is below every lower
      band in any real room, so an omitted row cannot be the one that would
      have won.
    * **pilot SNR** — MEASURE opens on a two-level pilot pair whose QUIET side
      sits ``pilot_delta_db`` below the sweep gain, and the pilot guard refuses
      the capture when that pilot's own in-band SNR falls under
      ``PILOT_MIN_SNR_DB``. Backing a driver's sweep off without carrying its
      pilots along would trade a loud measurement for a failing one, so the
      pilot floor is part of "the SNR the fit needs". Applied to every role
      rather than only the role that carries the leading pilots (the composer
      puts them on the woofer): it is a floor, so applying it more widely can
      only keep a level higher, and it stays correct if the composer ever moves
      the pair. The session's clip retry subtracts a further
      ``CLIP_RETRY_BACKOFF_DB`` (3 dB) from whatever this returns, which
      ``MEASURE_SNR_SOLVE_MARGIN_DB`` absorbs with room to spare.
    * **capture floor** — ``DRIVER.peak_too_low_dbfs``, the capture-quality
      model's "a capture peak below this is too low to trust". Guards the
      degenerate case where an ambient report reads near the dBFS floor and the
      SNR math alone would propose an inaudible sweep. It is a TRIPWIRE, not a
      shippable bound: if it wins, the other two arms have both resolved below
      a level that is by definition too faint to measure, so the solve is
      REFUSED and the role falls back to ``flat_target_gain_db`` with
      ``bound_by=GAIN_BOUND_DEGENERATE_AMBIENT`` and a WARNING.
      ``GAIN_BOUND_CAPTURE_FLOOR`` therefore never appears on a returned solve;
      it stays in ``GAIN_BOUNDS`` as the name of the losing arm and for reading
      back older persisted solves.

    The result is then clamped by ``flat_target_gain_db``: this solve can only
    make MEASURE quieter than the level-only figure, never louder.
    """
    rows = _ambient_rows_in_band(band_hz, ambient_bands) if band_hz else []
    if not rows:
        # Disclosed fallback (never a silent guess): with no ambient evidence
        # for this driver's band there is nothing to solve against, so keep
        # today's flat target and say so.
        return RoleGainSolve(
            role=role,
            gain_db=flat_target_gain_db,
            flat_target_gain_db=flat_target_gain_db,
            bound_by=GAIN_BOUND_NO_AMBIENT_EVIDENCE,
            band_hz=band_hz,
        )

    demands: list[tuple[float, float, float, float]] = []
    for lo, hi, level in rows:
        required_snr = (
            _band_required_snr_db(lo, hi, overlap_hz) + MEASURE_SNR_SOLVE_MARGIN_DB
        )
        # `level + required_snr` is a band-RMS demand, but what it is about to
        # be compared against — and what `k_db` converts into a digital gain —
        # is a capture PEAK. Carry the stimulus's own peak-to-band-RMS across
        # so both sides are peak-expressed.
        crest = sweep_band_crest_factor_db(band_hz, (lo, hi)) if band_hz else 0.0
        demands.append((level + required_snr + crest, level, required_snr, crest))
    required_capture_dbfs, ambient_dbfs, required_snr_db, crest_factor_db = max(
        demands, key=lambda item: item[0]
    )
    # Named residual, erring QUIET: a pilot's SNR is measured over its WHOLE
    # band, but this floor is built from the single worst overlapping ROW. A
    # row narrower than the pilot's band understates the noise the pilot
    # actually integrates, so this arm can sit lower than the pilot really
    # needs. It is not the binding arm anywhere it has been measured — on the
    # JTS3 field room the room arm wins by 16-19 dB.
    worst_ambient_dbfs = max(level for _lo, _hi, level in rows)
    pilot_floor_dbfs = (
        worst_ambient_dbfs + pilot_delta_db + PILOT_MIN_SNR_DB
        + MEASURE_SNR_SOLVE_MARGIN_DB
        # Same dimensional carry as the room arm. A pilot is a swept sine over
        # the role's WHOLE band (`program.segment_stimulus` renders every
        # stimulus kind the same way), and `PILOT_MIN_SNR_DB` is checked
        # against its in-band power ratio, so the occupancy term is 1 and only
        # the peak-to-RMS constant applies.
        + SWEEP_PEAK_TO_RMS_DB
    )
    capture_dbfs, bound_by = max(
        (
            (required_capture_dbfs, GAIN_BOUND_ROOM_SNR),
            (pilot_floor_dbfs, GAIN_BOUND_PILOT_SNR),
            (DRIVER.peak_too_low_dbfs, GAIN_BOUND_CAPTURE_FLOOR),
        ),
        key=lambda item: item[0],
    )
    if bound_by == GAIN_BOUND_CAPTURE_FLOOR:
        # The capture floor winning IS this function's degenerate-ambient
        # signal: an ambient report reading near the dBFS floor, where the SNR
        # math alone proposes an inaudible sweep. Shipping it as a level once
        # solved both roles to a -45 dBFS capture target, 34 dB below the flat
        # level, and every downstream guard that might have caught it (the
        # pilot SNR floor, the sweep locator) was computed from the same
        # collapsed ambient report and so could not bound it by construction. A
        # floor-bound solve is not a level, it is evidence that the ambient
        # report cannot be solved against — so refuse, keep the flat target,
        # and say which way the refusal went.
        log_event(
            logger,
            "program_analysis.measure_level_solve_refused",
            level=logging.WARNING,
            role=role,
            reason=GAIN_BOUND_DEGENERATE_AMBIENT,
            capture_floor_dbfs=round(DRIVER.peak_too_low_dbfs, 2),
            required_capture_dbfs=round(required_capture_dbfs, 2),
            pilot_floor_dbfs=round(pilot_floor_dbfs, 2),
            ambient_dbfs=round(ambient_dbfs, 2),
            fallback_gain_db=round(flat_target_gain_db, 3),
        )
        return RoleGainSolve(
            role=role,
            gain_db=flat_target_gain_db,
            flat_target_gain_db=flat_target_gain_db,
            bound_by=GAIN_BOUND_DEGENERATE_AMBIENT,
            band_hz=band_hz,
            # The evidence that was refused, retained deliberately: a reviewer
            # reading the disclosure event needs to see WHAT the ambient
            # report claimed, not just that it was rejected.
            ambient_dbfs=ambient_dbfs,
            required_snr_db=required_snr_db,
            required_capture_dbfs=required_capture_dbfs,
            crest_factor_db=crest_factor_db,
        )
    gain_db = capture_dbfs - k_db
    if gain_db >= flat_target_gain_db:
        gain_db, bound_by = flat_target_gain_db, GAIN_BOUND_FLAT_TARGET
    return RoleGainSolve(
        role=role,
        gain_db=gain_db,
        flat_target_gain_db=flat_target_gain_db,
        bound_by=bound_by,
        band_hz=band_hz,
        ambient_dbfs=ambient_dbfs,
        required_snr_db=required_snr_db,
        required_capture_dbfs=required_capture_dbfs,
        crest_factor_db=crest_factor_db,
    )


def _solve_gain_plan(
    program: ExcitationProgram,
    pilots: Sequence[PilotObservation],
    ambient_report: Mapping[str, Any],
    priors: MeasurementPriors,
) -> GainPlan:
    target = priors.target_capture_dbfs
    ambient_bands = (
        ambient_report.get("bands") if isinstance(ambient_report, Mapping) else None
    ) or ()
    # The nominal Fc ± 1 octave window, UNCLAMPED by the true sweep overlap:
    # `overlap_band_hz`'s clamps only narrow the band, and a narrower band
    # would demote bands to the (lower) magnitude requirement, i.e. buy a
    # quieter solve on a technicality. The wider window is the safe read here.
    overlap_hz = (
        overlap_band_hz(float(priors.crossover_fc_hz))
        if priors.crossover_fc_hz else None
    )
    gains: dict[str, float] = {}
    solves: dict[str, RoleGainSolve] = {}
    predicted_peaks: list[float] = []
    for pilot in pilots:
        lo_seg = program.segment(f"pilot_{pilot.role}_lo")
        hi_seg = program.segment(f"pilot_{pilot.role}_hi")
        # captured = digital_gain + K (unit slope). K from the two pilots.
        # Deliberately the PEAK-referenced levels (`peak_*_dbfs`), not
        # `level_*_dbfs` — the latter is ambient-subtracted for the
        # linearity verdict and would shift this ABSOLUTE reference (see
        # `PilotObservation`'s docstring); `target_capture_dbfs` is
        # documented as a capture-PEAK target and K must match that.
        k_lo = pilot.peak_lo_dbfs - lo_seg.gain_db
        k_hi = pilot.peak_hi_dbfs - hi_seg.gain_db
        k = (k_lo + k_hi) / 2.0
        # The level-only answer, and the CEILING of the solve below.
        flat_gain = min(target - k, GAIN_MAX_DIGITAL_PEAK_DBFS)  # ≥6 dB guard
        solve = _solve_role_gain(
            role=pilot.role,
            k_db=k,
            flat_target_gain_db=flat_gain,
            # A CHECK pilot's band IS the role's MEASURE sweep band: both
            # come from `_intersect_band(rb.band, MEASURE_SWEEP_F_LO_HZ,
            # MEASURE_SWEEP_F_HI_HZ)` in `program`'s composers, so the band
            # is read off the segment already in hand rather than plumbed in
            # a second time (and cannot drift from what MEASURE will sweep).
            band_hz=(
                (float(lo_seg.f1_hz), float(lo_seg.f2_hz))
                if lo_seg.f1_hz is not None and lo_seg.f2_hz is not None
                else None
            ),
            pilot_delta_db=abs(hi_seg.gain_db - lo_seg.gain_db),
            ambient_bands=ambient_bands,
            overlap_hz=overlap_hz,
        )
        gains[pilot.role] = solve.gain_db
        solves[pilot.role] = solve
        predicted_peaks.append(solve.gain_db)
    predicted_peak = max(predicted_peaks) if predicted_peaks else GAIN_MAX_DIGITAL_PEAK_DBFS

    # Deliberately judged at `target_capture_dbfs`, NOT at the solved level.
    # This is the room-quality gate ("is this room quiet enough to commission
    # in at all"), asked of the reference target against the whole ambient
    # report — including the sub-bass band no driver's sweep reaches. The solve
    # answers a different question (how much drive this fit needs, per driver,
    # in that driver's own band), and folding the two together would silently
    # change which sessions CHECK accepts.
    #
    # Passing requires the worst TRUE ambient band at or below
    # `target_capture_dbfs - DRIVER.snr_ok_db` (-35.5 dBFS at the shipped
    # target). Headroom is not tight where it has been measured: the JTS3 field
    # room's worst true band was -57.86 dBFS, ~22 dB inside the bound.
    snr_floor_ok = _snr_floor_ok(ambient_report, target)
    return GainPlan(
        gain_db=gains,
        predicted_peak_dbfs=predicted_peak,
        snr_floor_ok=snr_floor_ok,
        role_solves=solves,
    )


def _snr_floor_ok(ambient_report: Mapping[str, Any], target_capture_dbfs: float) -> bool:
    """False when the ambient report is missing, empty, or every row is
    unreadable — never raises on a malformed ``level_dbfs``.

    Mirrors ``_ambient_rows_in_band``'s defensive parse: more than one producer
    writes ambient band rows (a live in-process ``snr_policy`` report, plus
    replayed artifacts), so a row this cannot read must cost this gate that
    row's evidence rather than crash CHECK's accept path. Unreachable from the
    in-process producer alone, but an asymmetry between the two functions is
    what bites a replay path.
    """
    bands = ambient_report.get("bands") if isinstance(ambient_report, Mapping) else None
    if not bands:
        return False
    worst: float | None = None
    for b in bands:
        if not isinstance(b, Mapping):
            continue
        try:
            level = float(b["level_dbfs"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(level):
            continue
        if worst is None or level > worst:
            worst = level
    if worst is None:
        return False
    return (target_capture_dbfs - worst) >= DRIVER.snr_ok_db


# --------------------------------------------------------------------------- #
# phase dispatch
# --------------------------------------------------------------------------- #


def analyze_program_capture(
    program: ExcitationProgram,
    samples: np.ndarray,
    sample_rate: int,
    *,
    calibration: "CalibrationCurve | None" = None,
    geometry: MeasurementGeometry | None = None,
    priors: MeasurementPriors | None = None,
    capture_report: Mapping[str, Any] | None = None,
) -> ProgramAnalysis:
    """Analyze a program capture into a :class:`ProgramAnalysis` (design §5.6).

    ``capture_report`` is the capture page's own per-take account of the
    recording — ``CaptureResult.capture_integrity``, threaded in by the
    production analyze seam. It is reconciled against the frames actually handed
    to this function, and the resulting :class:`~.frame_ledger.FrameLedger`
    rides every returned analysis. ``None`` (the default, and every caller that
    has no phone report) leaves the ledger's page-side counts unreported, which
    grades as not-evaluated rather than as loss.

    The received count is taken from ``samples`` AS HANDED IN, which is the
    decoded WAV for every production caller: the rate check below forbids the
    one transform that could make it something else, since a resampled capture
    can only reach here by also claiming the program's rate. A caller that does
    resample upstream gets the disagreement reported rather than hidden.
    """
    if sample_rate != program.sample_rate_hz:
        raise ValueError(
            f"capture rate {sample_rate} != program rate {program.sample_rate_hz}"
        )
    capture = np.asarray(samples, dtype=np.float64).ravel()
    # BEFORE the truncation below, which is a legitimate transform and must
    # never read as loss (frame_ledger's hop G).
    frame_ledger = reconcile_capture_frames(
        capture_report, received_frames=int(capture.size),
    )
    _log_frame_ledger(program, frame_ledger)
    # Bound the capture BEFORE any full-rate FFT (1 GB Pi). A stuck recording
    # is truncated to the program duration plus CAPTURE_BOUND_MARGIN_S; a
    # program that genuinely starts beyond the margin fails downstream location
    # checks loudly instead of allocating hundreds of MB here.
    capture = deconv.cap_capture_length(
        capture,
        sweep_len=program.total_samples,
        sample_rate=sample_rate,
        max_capture_seconds=(
            program.total_samples / sample_rate + CAPTURE_BOUND_MARGIN_S
        ),
    )
    geometry = geometry or MeasurementGeometry()
    priors = priors or MeasurementPriors()

    global_offset, _first, stimuli, anchor_ambiguous = _global_offset(
        program, capture, sample_rate
    )
    locations = _locate_segments(program, capture, sample_rate, global_offset, stimuli)

    if program.phase == PROGRAM_PHASE_CHECK:
        analysis = _analyze_check(
            program, capture, sample_rate, global_offset, locations, priors,
        )
    elif program.phase == PROGRAM_PHASE_MEASURE:
        analysis = _analyze_measure(
            program, capture, sample_rate, global_offset, locations,
            calibration, geometry, priors,
        )
    elif program.phase == PROGRAM_PHASE_VERIFY:
        analysis = _analyze_verify(
            program, capture, sample_rate, global_offset, locations,
            calibration, priors, frame_ledger,
        )
    else:
        raise ValueError(f"unknown phase: {program.phase!r}")
    # Attached HERE rather than inside each phase analyzer so there is one
    # assignment site for one fact — the same discipline ``glitch_detected``
    # follows — and so a phase that grows its own analyzer later cannot ship
    # without it. Same argument for ``anchor_ambiguous``, which is a property of
    # the capture's timeline rather than of any one phase's reading of it.
    return replace(
        analysis, frame_ledger=frame_ledger, anchor_ambiguous=anchor_ambiguous
    )


def _analyze_check(
    program, capture, sample_rate, global_offset, locations, priors,
) -> ProgramAnalysis:
    ambient_seg = program.segment(AMBIENT_SEGMENT_ID)
    ambient_samples, ambient_report = _ambient_from_capture(
        capture, sample_rate, ambient_seg, global_offset
    )
    # CHECK's own 12 s session-ambient window feeds BOTH the level/SNR path
    # and the channel-map rise test — it is the long, framed estimate that
    # test was designed around (see `_pilot_observations`).
    pilots = _pilot_observations(
        program, capture, sample_rate, locations,
        ambient_samples=ambient_samples,
        channel_map_ambient_samples=ambient_samples,
    )
    linearity_ok = _aggregate_linearity_ok(pilots)
    channel_map_ok = _aggregate_tri_state_ok([p.channel_map_ok for p in pilots])
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    gain_plan = _solve_gain_plan(program, pilots, ambient_report, priors)
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        ambient_report=ambient_report,
        pilots=tuple(pilots),
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        gain_plan=gain_plan,
    )


def _compose_configured_path_ir(
    role: str,
    full_ir: np.ndarray,
    sample_rate: int,
    radiated_band_hz: tuple[float, float] | None,
    priors: MeasurementPriors,
) -> np.ndarray:
    """Replace exact emitted protection ``P`` with configured crossover ``C``.

    ``S = M*C/P`` (design §4.2) as ONE filter across the whole rfft support,
    never spliced into a sub-band. The conditioning policy binds ONLY on §4.2's
    candidate-required bins — the DRIVEN band (``radiated_band_hz``)
    intersected with ``priors.candidate_required_band_hz_by_role`` — with no
    clipping, interpolation or omission there; outside them the same exact
    ratio still applies, saturated at the policy's own +12 dB ceiling. A
    segment declaring no sweep bounds is missing evidence, not
    ill-conditioning. Numbers and the DC/Nyquist zero-ratio bins:
    ``docs/historical/crossover-measurement-v2-campaign-record.md``,
    "Composing the configured-Fc path".
    """
    maps = (
        priors.measurement_protection_response_by_role,
        priors.configured_crossover_response_by_role,
        priors.configured_polarity_sign_by_role,
    )
    if all(value is None for value in maps):
        return full_ir
    if any(value is None for value in maps):
        raise ConfiguredPathConditioningError("incomplete priors")
    if radiated_band_hz is None:
        # Missing evidence, not ill-conditioning: this segment declared no
        # sweep bounds, so there is no band to require. Same refusal shape the
        # kernel's other undeclared-band call sites raise.
        raise ValueError(f"{role} sweep segment has no declared band")
    protection_by_role, configured_by_role, polarity_by_role = maps
    try:
        protection_response = protection_by_role[role]  # type: ignore[index]
        configured_response = configured_by_role[role]  # type: ignore[index]
        polarity_sign = polarity_by_role[role]  # type: ignore[index]
    except KeyError as exc:
        raise ConfiguredPathConditioningError(f"missing role {role}") from exc
    if type(polarity_sign) is not int or polarity_sign not in (-1, 1):
        raise ConfiguredPathConditioningError(f"invalid polarity for {role}")
    samples = np.asarray(full_ir, dtype=np.float64)
    measured = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(samples.size, 1.0 / sample_rate)
    lo_hz, hi_hz = (float(value) for value in radiated_band_hz)
    band = (priors.candidate_required_band_hz_by_role or {}).get(role)
    if band is not None:
        lo_hz, hi_hz = max(lo_hz, float(band[0])), min(hi_hz, float(band[1]))
    required = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not np.any(required):
        raise ConfiguredPathConditioningError(f"no trusted bins for {role}")
    protection = np.asarray(protection_response(freqs), dtype=np.complex128)
    configured = np.asarray(configured_response(freqs), dtype=np.complex128)
    for label, response in (("P", protection), ("C", configured)):
        if not np.all(np.isfinite(response[required])):
            raise ConfiguredPathConditioningError(f"non-finite {label} for {role}")
    minimum = 10.0 ** (CONFIGURED_PATH_PROTECTION_FLOOR_DB / 20.0)
    if np.any(np.abs(protection[required]) < minimum):
        raise ConfiguredPathConditioningError(
            f"P below {CONFIGURED_PATH_PROTECTION_FLOOR_DB:g} dB for {role}",
            protection_floor=True,
        )
    ratio = np.divide(
        configured, protection,
        out=np.zeros_like(configured), where=protection != 0,
    )
    maximum = 10.0 ** (12.0 / 20.0)
    if not np.all(np.isfinite(ratio[required])) or np.any(
        np.abs(ratio[required]) > maximum
    ):
        raise ConfiguredPathConditioningError(f"C/P above +12 dB for {role}")
    saturated = ratio * (maximum / np.maximum(np.abs(ratio), maximum))
    spectrum = measured * polarity_sign * saturated
    if not np.all(np.isfinite(spectrum[required])):
        raise ConfiguredPathConditioningError(f"non-finite S for {role}")
    spectrum[~np.isfinite(spectrum)] = 0.0  # off-required, but rides the irfft
    return np.fft.irfft(spectrum, n=samples.size)


def _repeat_driver_responses(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    epsilon: float,
    occurrences: Sequence[SegmentLocation],
    *,
    role: str,
    calibration: "CalibrationCurve | None",
    ambient_report: Mapping[str, Any] | None,
    fc_hz: float | None,
    n_fft: int,
    priors: MeasurementPriors,
) -> tuple[DriverResponse, ...]:
    """Deconvolve + gate + TF every occurrence AFTER the first (design item 7).

    Per-repeat evidence, individually bounded exactly like the primary response
    (same ``n_fft``); no caching or parallelism, and it triples the
    deconvolution call count (2→6) on purpose. The PRIMARY response (the
    driver's canonical ``sweep_w``/``sweep_t`` occurrence) is built by the
    caller and untouched here; repeats never feed the candidate/trim/alignment
    math. ``role`` is the primary's own already-resolved role — every location
    in ``occurrences`` shares it by construction of the caller's per-role
    grouping — so it is threaded through rather than re-derived per segment.

    Consumed by
    ``jasper.active_speaker.linearization_envelope.compute_sigma_curve`` as the
    Layer-1a repeatability term.
    """
    out: list[DriverResponse] = []
    for repeat_index, loc in enumerate(occurrences[1:], start=1):
        seg = program.segment(loc.segment_id)
        full_ir, _pre = _deconvolve_window(
            capture, seg, global_offset + seg.start_sample, sample_rate,
            epsilon=epsilon,
        )
        full_ir = _compose_configured_path_ir(
            role, full_ir, sample_rate, _radiated_band_hz(seg), priors
        )
        resp = _driver_response(
            role, full_ir, sample_rate,
            calibration=calibration, ambient_report=ambient_report,
            fc_hz=fc_hz, n_fft=n_fft,
            radiated_band_hz=_radiated_band_hz(seg),
            capture_segment=_raw_sweep_segment(
                capture, seg, global_offset + seg.start_sample,
            ),
        )
        out.append(replace(resp, repeat_index=repeat_index))
    return tuple(out)


#: :attr:`ProgramAnalysis.measure_pair_not_evaluated`'s only value today: one
#: driver, so no pair to align or to build a candidate across.
MEASURE_PAIR_SINGLE_DRIVER = "single_driver_no_pair"


def _analyze_measure(
    program, capture, sample_rate, global_offset, locations,
    calibration, geometry, priors,
) -> ProgramAnalysis:
    # ``None`` is legal for exactly one shape: a 1-way passive main. On a
    # TWO-branch program it still raises below, where ``seg_t`` is known.
    fc_hz = None if priors.crossover_fc_hz is None else float(priors.crossover_fc_hz)
    drift = _estimate_drift(program, capture, sample_rate, locations)

    seg_w = program.segment("sweep_w")
    seg_t = next(
        (seg for seg in program.segments if seg.segment_id == "sweep_t"), None
    )
    if seg_t is not None and fc_hz is None:
        raise ValueError("MEASURE analysis requires priors.crossover_fc_hz")
    epsilon = drift.epsilon_ppm / 1e6
    # Deconvolve both sweeps anchored at their SCHEDULE window (with a shared
    # pre-guard) so relative timing survives (the aligner relies on this); the
    # measured ε is divided out of the reference so drift can't smear the IR.
    woofer_full_ir, pre_w = _deconvolve_window(
        capture, seg_w, global_offset + seg_w.start_sample, sample_rate,
        epsilon=epsilon,
    )
    woofer_full_ir = _compose_configured_path_ir(
        seg_w.role, woofer_full_ir, sample_rate, _radiated_band_hz(seg_w), priors
    )
    tweeter_full_ir = None
    pre_samples = pre_w
    if seg_t is not None:
        tweeter_full_ir, pre_t = _deconvolve_window(
            capture, seg_t, global_offset + seg_t.start_sample, sample_rate,
            epsilon=epsilon,
        )
        tweeter_full_ir = _compose_configured_path_ir(
            seg_t.role, tweeter_full_ir, sample_rate, _radiated_band_hz(seg_t), priors
        )
        pre_samples = min(pre_w, pre_t)
    n_fft = _n_fft_for(*[ir for ir in (woofer_full_ir, tweeter_full_ir) if ir is not None])

    # Primary responses are first-occurrence-derived, built from
    # woofer_full_ir/tweeter_full_ir. Repeats are additionally
    # deconvolved/gated/TF'd and attached as diagnostic-only
    # `repeat_responses` on the matching primary; they never change a primary's
    # own freqs_hz/magnitude_db/complex_tf/gating/snr/validity_floor.
    occurrences_by_role = _sweep_occurrences_by_role(locations)
    branches = [(seg_w, woofer_full_ir)]
    if seg_t is not None and tweeter_full_ir is not None:
        branches.append((seg_t, tweeter_full_ir))
    responses = tuple(
        replace(
            resp,
            repeat_responses=_repeat_driver_responses(
                program, capture, sample_rate, global_offset, epsilon,
                occurrences_by_role.get(resp.role, ()),
                role=resp.role,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
                priors=priors,
            ),
        )
        for resp in (
            _driver_response(
                seg.role, full_ir, sample_rate,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
                radiated_band_hz=_radiated_band_hz(seg),
                capture_segment=_raw_sweep_segment(
                    capture, seg, global_offset + seg.start_sample,
                ),
            )
            for seg, full_ir in branches
        )
    )

    # ONE branch: nothing to align across and nothing to blend, so both come
    # back absent WITH A REASON.
    alignment: AlignmentEstimate | None = None
    candidate: CrossoverCandidate | None = None
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    pair_not_evaluated: str | None = MEASURE_PAIR_SINGLE_DRIVER
    # ``fc_hz is not None`` is guaranteed by the raise above; restated to narrow.
    if seg_t is not None and tweeter_full_ir is not None and fc_hz is not None:
        pair_not_evaluated = None
        alignment = _estimate_alignment(
            capture, program, sample_rate, global_offset, drift.epsilon_ppm / 1e6,
            fc_hz, geometry, priors,
            woofer_full_ir=woofer_full_ir, tweeter_full_ir=tweeter_full_ir,
            pre_samples=pre_samples,
        )

        # The alignment reads BOTH branch impulse responses, so either one's
        # capture SNR can be the reason its (polarity, delay) answer is noise —
        # `_select_alignment_pair` refuses on the pair, not on a named driver. The
        # ALIGNMENT-class verdict, not the magnitude one every surface displays:
        # this decision is the one the 35 dB law was written for.
        alignment_roles = {seg_w.role, seg_t.role}
        branch_snr_insufficient = any(
            driver_alignment_snr_verdict(resp) == ALIGNMENT_SNR_REFUSAL_VERDICT
            for resp in responses
            if resp.role in alignment_roles
        )
        candidate, predicted_sum = _build_candidate(
            woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
            seg_w.role, seg_t.role, alignment, calibration,
            tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
            woofer_sweep_lo_hz=seg_w.f1_hz, tweeter_sweep_hi_hz=seg_t.f2_hz,
            alignment_delay_bounds_us=priors.alignment_delay_bounds_us,
            branch_snr_insufficient=branch_snr_insufficient,
            applied_alignment=priors.applied_alignment,
            explicit_alignment_delay_us=priors.explicit_alignment_delay_us,
            explicit_alignment_polarity_sign=priors.explicit_alignment_polarity_sign,
        )
        # `_build_candidate` owns the selection; the estimate published here is the
        # one every downstream consumer reads (`alignment_to_candidate_fields`
        # builds the APPLIED fields from it), so it must carry what was committed,
        # with the correlation's own answer preserved beside it as the seed.
        if candidate.alignment_objective in _SELECTOR_COMMITTED_OBJECTIVES:
            alignment = replace(
                alignment,
                polarity=candidate.polarity,
                polarity_sign=polarity_sign_of(candidate.polarity),
                # READ, not re-derived. The rule — only a commitment whose POLARITY
                # the flat sum actually chose may answer this — belongs to
                # :attr:`AlignmentPairSelection.polarity_agrees_with_sum`, and the
                # candidate carries its answer here. A second derivation at this
                # line drifts as soon as that rule widens.
                polarity_agrees_with_sum=candidate.polarity_agrees_with_sum,
            )
        # The delay half keeps its own condition: the anchor path is exactly where
        # the committed delay can differ from the estimate's GCC seed, and reading
        # the anchor's presence says so directly.
        if candidate.anchor_delay_us is not None:
            alignment = replace(
                alignment,
                delay_us=candidate.delay_us,
                raw_delay_us=candidate.delay_us + alignment.parallax_us,
                seed_delay_us=alignment.delay_us,
                confidence_source="gcc_phat_seed",
            )
    else:
        # One branch radiates the whole band, so the model of what this capture
        # sums to IS that branch, at the fixed zero trim such a round ships.
        predicted_sum = (responses[0].freqs_hz, responses[0].magnitude_db)
    # Per-capture behavioral-linearity evidence (design §5.2): a v2 MEASURE
    # program opens with a pre-pilot ambient window + a leading pilot pair; a
    # program carrying neither leaves the verdicts ``None``.
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations, global_offset=global_offset,
    )
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        drift=drift,
        driver_responses=responses,
        alignment=alignment,
        candidate=candidate,
        measure_pair_not_evaluated=pair_not_evaluated,
        mic_tier=priors.mic_tier,
        mic_calibrated=priors.mic_calibrated,
        # Exact: composition returns its input untouched iff every prior map
        # is None, and raises if only some are.
        configured_path_composed=(
            priors.configured_crossover_response_by_role is not None
        ),
        pilots=pilots,
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        predicted_sum=predicted_sum,
        glitch_detected=drift.glitch_detected,
    )


def _build_candidate(
    woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
    woofer_role, tweeter_role, alignment, calibration,
    *,
    tweeter_sweep_lo_hz: float | None = None,
    woofer_sweep_hi_hz: float | None = None,
    woofer_sweep_lo_hz: float | None = None,
    tweeter_sweep_hi_hz: float | None = None,
    alignment_delay_bounds_us: tuple[float, float] | None = None,
    branch_snr_insufficient: bool = False,
    applied_alignment: AppliedAlignment | None = None,
    explicit_alignment_delay_us: float | None = None,
    explicit_alignment_polarity_sign: int | None = None,
) -> tuple[CrossoverCandidate, tuple[np.ndarray, np.ndarray]]:
    freqs, W, gate_w = _aligned_branch_tf(woofer_full_ir, sample_rate, n_fft, calibration=calibration)
    _f2, T, gate_t = _aligned_branch_tf(tweeter_full_ir, sample_rate, n_fft, calibration=calibration)
    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=tweeter_sweep_lo_hz, woofer_sweep_hi_hz=woofer_sweep_hi_hz,
    )
    # Gating-consistent prediction: ``_aligned_branch_tf`` reflection-gates each
    # branch the same way ``_driver_response`` does, so a branch near a
    # reflective mic position can be valid only above a floor HIGHER than the
    # nominal Fc±1-oct band. Clamp every quantity derived from W/T — the trim
    # solve, the predicted sum's ripple — to the worse (higher) of the two
    # branches' floors, never silently trusting sub-floor bins. If the floor
    # consumes the whole band, `solve_branch_trims`/`_ripple_db` raise
    # ValueError on the now-empty mask, which the catch-all seam in
    # `jasper.web.correction_crossover_v2` classifies as `internal_error`.
    branch_floor_hz = max(
        (f for f in (_gate_floor_hz(gate_w), _gate_floor_hz(gate_t)) if f is not None),
        default=None,
    )
    lo_clamped = (
        max(lo, branch_floor_hz)
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz)
        else lo
    )
    # The LEVEL MATCH reads a different span from the ripple/prediction band
    # above: each branch on its own side of Fc, inside its OWN
    # excited-and-gated span, never the shared both-branches-excited overlap —
    # see solve_branch_trims. Each span is that branch's declared sweep band,
    # floored by the shared reflection floor (sub-floor bins stay untrusted
    # everywhere). A missing sweep bound falls back to the nominal Fc-octave
    # edge, exactly like overlap_band_hz's own None handling.
    def _span(lo_hz: float | None, hi_hz: float | None) -> tuple[float, float]:
        lo = float(lo_hz) if lo_hz is not None else fc_hz / OVERLAP_OCTAVE_RATIO
        hi = float(hi_hz) if hi_hz is not None else fc_hz * OVERLAP_OCTAVE_RATIO
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz):
            lo = max(lo, branch_floor_hz)
        return lo, hi

    woofer_span = _span(woofer_sweep_lo_hz, woofer_sweep_hi_hz)
    tweeter_span = _span(tweeter_sweep_lo_hz, tweeter_sweep_hi_hz)
    trim_w, trim_t_band_average, level_w, level_t = solve_branch_trims(
        freqs, W, T, fc_hz, woofer_span_hz=woofer_span, tweeter_span_hz=tweeter_span,
    )
    (w_band_lo, w_band_hi), (t_band_lo, t_band_hi) = branch_level_bands_hz(
        fc_hz, woofer_span_hz=woofer_span, tweeter_span_hz=tweeter_span,
    )
    # The level match's own inputs, on every MEASURE analysis. Read beside the
    # per-role `target_level_db` in
    # `correction.crossover_v2_linearization_giveback` (the fit frame for the
    # SAME capture) — a large disagreement between the two is the signature of
    # a level-frame defect.
    log_event(
        logger, "program_analysis.branch_level_match",
        woofer_role=woofer_role, tweeter_role=tweeter_role,
        fc_hz=round(float(fc_hz), 3),
        level_w_db=round(float(level_w), 3), level_t_db=round(float(level_t), 3),
        woofer_band_hz=(round(w_band_lo, 1), round(w_band_hi, 1)),
        tweeter_band_hz=(round(t_band_lo, 1), round(t_band_hi, 1)),
        trim_band_average_db=round(float(trim_t_band_average), 3),
    )
    # --- the (polarity, delay) pair, on one objective ------------------------
    #
    # Runs BEFORE the trim polish below, which takes the polarity as an input:
    # the pair is scored at the level-match trims, then the level is polished
    # at the committed polarity. Scoring at the polished trim instead would be
    # circular, and the polish is a bounded level nudge that cannot move where
    # a commanded null falls — see `_select_alignment_pair`.
    seed_delay_us = alignment.delay_us
    anchor_delay_us = None
    snap_found = False
    # THE FRAME, gated on the aligner's own status and nothing else — the same
    # gate the shipped model uses below (`residual_delay_us`). The aligner
    # single-sources the physical peak-gap anchor (raw argmax gap − inter-sweep
    # drift + parallax, in the signed frame; methodology §10); everything
    # downstream derives from it rather than re-running the argmax, which would
    # be a parallel computation of one load-bearing frame decision.
    #
    # Deliberately NOT also gated on declared bounds, which would conflate the
    # frame with the delay-ESTIMATOR question the comment at
    # `residual_delay_us` warns is different: a capture whose preset declares no
    # `delay_range_ms` still ships a model phased by `committed − anchor`, so a
    # selector scoring it at residual 0 chooses a pair on a curve nothing emits
    # — measured at a 20.37 dB penalty for the shipped model against the one the
    # objective graded, with the polarity chosen at a residual the speaker never
    # runs.
    if alignment.status == ALIGNMENT_OK and alignment.anchor_delay_us is not None:
        anchor_delay_us = float(alignment.anchor_delay_us)
    # THE SEED DELAY — a different question, and bounds-gated. The gated
    # local-peak snap is the seed's fine step, not the committed delay's: the
    # aligner snapped the anchor to the nearest local maximum of the SAME
    # GCC-PHAT correlation within ±(period/6) at Fc, or ruled one out. That pair
    # — correlation's polarity at correlation's refined delay — is what the
    # selector scores its own answer against and falls back to on a tie. With no
    # declared bounds the seed is the bare GCC estimate instead; the objective
    # scores it in the honest frame and may prefer a grid point over it.
    if anchor_delay_us is not None and alignment_delay_bounds_us is not None:
        if alignment.snapped_delay_us is not None:
            seed_delay_us = float(alignment.snapped_delay_us)
            snap_found = True
        else:
            seed_delay_us = anchor_delay_us
    # A refused estimate is not a seed to search around: nothing downstream
    # applies its delay OR its polarity (`alignment_to_candidate_fields` returns
    # the trims-only shape), so the objective would be grading a pair that can
    # never ship.
    selection = (
        _select_alignment_pair(
            freqs, W, T,
            fc_hz=fc_hz, lo_hz=lo_clamped, hi_hz=hi,
            trim_w_db=trim_w, trim_t_db=trim_t_band_average,
            anchor_delay_us=anchor_delay_us,
            seed_delay_us=seed_delay_us,
            seed_polarity_sign=alignment.polarity_sign,
            delay_bounds_us=alignment_delay_bounds_us,
            branch_snr_insufficient=branch_snr_insufficient,
            applied_alignment=applied_alignment,
            explicit_delay_us=explicit_alignment_delay_us,
            explicit_polarity_sign=explicit_alignment_polarity_sign,
        )
        if alignment.status == ALIGNMENT_OK
        else None
    )
    if selection is None:
        polarity_sign = alignment.polarity_sign
        delay_us = seed_delay_us
        seed_ripple_db = None
        flatness_improvement_db = None
        left_anchor_lobe = False
        alignment_objective = (
            ALIGNMENT_COMMITTED_SEED_ALIGNMENT_REFUSED
            if alignment.status != ALIGNMENT_OK
            else ALIGNMENT_COMMITTED_SEED_NO_SCORING_BAND
        )
    else:
        polarity_sign = selection.polarity_sign
        delay_us = selection.delay_us
        alignment_objective = selection.objective
        left_anchor_lobe = selection.left_anchor_lobe
        seed_ripple_db = flatness_improvement_db = None
        if math.isfinite(selection.seed_ripple_db) and math.isfinite(selection.ripple_db):
            seed_ripple_db = selection.seed_ripple_db
            flatness_improvement_db = selection.flatness_improvement_db
        log_event(
            logger, "program_analysis.alignment_selection",
            # Three shapes raise the level, and each is a thing a human should
            # read: correlation losing the polarity, any commitment the flat-sum
            # objective did not make, and a commitment that left the anchor's
            # comb lobe (the compensating control for the ±1-period span).
            level=(
                logging.INFO if (
                    selection.objective == ALIGNMENT_COMMITTED_FLAT_SUM
                    and selection.polarity_agrees_with_sum
                    and not selection.left_anchor_lobe
                ) else logging.WARNING
            ),
            woofer_role=woofer_role, tweeter_role=tweeter_role,
            objective=selection.objective,
            fc_hz=round(float(fc_hz), 3),
            band_hz=(round(float(lo_clamped), 1), round(float(hi), 1)),
            polarity=polarity_label(selection.polarity_sign),
            delay_us=round(float(selection.delay_us), 3),
            # BOTH ripples go through the None-safe rounder. The flat-sum path
            # scores only finite candidates, but the low-SNR path returns before
            # that filter — it commits the declared design without a search — so
            # a NaN branch there reaches `ripple_db` as well as
            # `seed_ripple_db`.
            ripple_db=_finite_or_none(selection.ripple_db, 4),
            seed_polarity=polarity_label(selection.seed_polarity_sign),
            seed_delay_us=round(float(selection.seed_delay_us), 3),
            seed_ripple_db=_finite_or_none(selection.seed_ripple_db, 4),
            polarity_agrees_with_sum=selection.polarity_agrees_with_sum,
            flatness_improvement_db=_finite_or_none(
                selection.flatness_improvement_db, 4,
            ),
            grid_points=selection.grid_points,
            grid_step_us=round(float(selection.grid_step_us), 3),
            branch_snr_insufficient=bool(branch_snr_insufficient),
            # What makes a refused capture's delay commitment readable rather
            # than inferable: the number that WAS held (the applied graph's own
            # delay, or None when there was nothing to hold) beside the anchor
            # that was DECLINED, and whether a graph was applied at all — which
            # separates "the design asks for none" from "we could not read what
            # this speaker plays". On every other objective these are context:
            # the anchor is the frame the grid was centred on, and the applied
            # alignment is not consulted.
            applied_delay_us=(
                None if applied_alignment is None or applied_alignment.delay_us is None
                else round(float(applied_alignment.delay_us), 3)
            ),
            applied_alignment_present=applied_alignment is not None,
            # The same disclosure for the same reason, one rung further out: a
            # delay this capture did not supply, and where it came from. `None`
            # on every ordinary session, which is what makes a non-None value
            # greppable as "this round's delay was prescribed, not searched".
            prescribed_delay_us=(
                None if explicit_alignment_delay_us is None
                else round(float(explicit_alignment_delay_us), 3)
            ),
            # The basin half of the same disclosure, and the deciding value on
            # this line: with it, `polarity` above is the round's INSTRUCTION
            # rather than its finding, and `polarity_agrees_with_sum` is None
            # because nothing was compared. Spelled in this module's own
            # polarity frame so all three polarity fields on one line —
            # `seed_polarity`, `prescribed_polarity`, `polarity` — read in one
            # vocabulary; the request's own words live on the receipt. `None`
            # when no prescription was made at all, matching its delay sibling;
            # "unpinned" when one was made and left the basin to the objective.
            prescribed_polarity=(
                None if explicit_alignment_delay_us is None
                else "unpinned" if explicit_alignment_polarity_sign is None
                else polarity_label(int(explicit_alignment_polarity_sign))
            ),
            anchor_delay_us=(
                None if anchor_delay_us is None
                else round(float(anchor_delay_us), 3)
            ),
            # The one thing the methodology asks the ANCHOR to own: which comb
            # lobe. Owned by the selection (which has the anchor and Fc) so the
            # candidate, the journal and the receipt read one derivation.
            left_anchor_lobe=selection.left_anchor_lobe,
        )
    # A prescription that never reached a commitment is the one failure a delay
    # sweep must not absorb quietly: the operator asked for a candidate and the
    # round would otherwise measure a fallback under that candidate's name. The
    # aligner's own refusals still stand — a railed delay search leaves the
    # branch pair in an unknown frame, and an unscorable band leaves the
    # estimator's seed standing — but the round SAYS SO here rather than leaving
    # a reader to infer it from an objective string that mentions a seed.
    #
    # Emitted AFTER the block above so it can name what WAS committed instead:
    # the selection is ``None`` on exactly the rails worth disclosing, so
    # reading its objective would print ``null`` in the one case an operator
    # most needs the answer. ``committed_delay_us`` rides beside it because the
    # gap between asked-for and committed is the whole finding.
    if (
        explicit_alignment_delay_us is not None
        and alignment_objective not in ALIGNMENT_EXPLICIT_PRESCRIPTION_OBJECTIVES
    ):
        log_event(
            logger, "program_analysis.alignment_prescription_not_committed",
            level=logging.WARNING,
            woofer_role=woofer_role, tweeter_role=tweeter_role,
            fc_hz=round(float(fc_hz), 3),
            prescribed_delay_us=round(float(explicit_alignment_delay_us), 3),
            committed_delay_us=round(float(delay_us), 3),
            # A pinned basin is lost on exactly the same rails the delay is, and
            # by the same silence, so it is disclosed beside it.
            prescribed_polarity=(
                "unpinned" if explicit_alignment_polarity_sign is None
                else polarity_label(int(explicit_alignment_polarity_sign))
            ),
            committed_polarity=polarity_label(polarity_sign),
            alignment_status=alignment.status,
            objective=alignment_objective,
        )
    snap_delta_us = None if anchor_delay_us is None else delay_us - anchor_delay_us
    # Polish the tweeter trim for minimum summed-response ripple, seeded by the
    # band-average level match. Guarded: a result further from the seed than
    # REALIZED_LEVEL_MATCH_TOLERANCE_DB is distrusted and discarded, never
    # applied as a wild trim.
    #
    # Run ONLY where summed ripple can express a level at all. The scan's
    # objective is the ripple of ``W + s·T`` over the SHARED
    # both-branches-excited band, and on tweeter-sweep-starts-at-Fc geometry
    # that band is one-sided: the woofer is 20+ dB down its skirt across it, so
    # the sum is the tweeter alone and its ripple barely responds to the
    # tweeter's own gain. Replayed on an archived JTS3 MEASURE capture the
    # objective moved the trim 7.9 dB down (-12.368 → -20.268) and only the
    # sanity guard stopped it. A selector that cannot see the woofer must not
    # set the woofer's handoff level, so it is skipped on that geometry rather
    # than guarded.
    ripple_band_straddles_fc = lo_clamped < fc_hz < hi
    # The rejected excursion; ``None`` covers BOTH the admitted case and the
    # skipped one (see the field's own docstring).
    ripple_polish_rejected_delta_db: float | None = None
    if ripple_band_straddles_fc:
        trim_t_ripple, _ripple_t_ripple, _seed = solve_ripple_optimal_trim(
            freqs, W, T, fc_hz,
            lo_hz=lo_clamped, hi_hz=hi,
            seed_trim_db=trim_t_band_average,
            trim_w_db=trim_w,
            sign=polarity_sign,
        )
        # Admitted only where the pair it produces can be GRADED as level
        # matched; the tolerance's own block above says why the two are one
        # number.
        polish_delta_db = trim_t_ripple - trim_t_band_average
        if abs(polish_delta_db) > REALIZED_LEVEL_MATCH_TOLERANCE_DB:
            ripple_polish_rejected_delta_db = float(polish_delta_db)
            log_event(
                logger, "program_analysis.ripple_trim_rejected",
                level=logging.WARNING,
                woofer_role=woofer_role, tweeter_role=tweeter_role,
                band_average_trim_db=round(trim_t_band_average, 3),
                ripple_optimal_trim_db=round(trim_t_ripple, 3),
                # The excursion, signed, beside the bound that rejected it.
                rejected_delta_db=round(polish_delta_db, 3),
                tolerance_db=REALIZED_LEVEL_MATCH_TOLERANCE_DB,
            )
            trim_t = trim_t_band_average
        else:
            trim_t = trim_t_ripple
    else:
        log_event(
            logger, "program_analysis.ripple_trim_skipped",
            woofer_role=woofer_role, tweeter_role=tweeter_role,
            reason="ripple_band_one_sided",
            fc_hz=round(float(fc_hz), 3),
            ripple_band_hz=(round(float(lo_clamped), 1), round(float(hi), 1)),
            band_average_trim_db=round(trim_t_band_average, 3),
        )
        trim_t = trim_t_band_average
    # TWO sums, two questions, two owners.
    #
    # `predicted_aligned` — the flattest-achievable, INDEPENDENTLY ALIGNED sum.
    # It answers "how coherently can this capture's two branches sum at all?",
    # a property of the measurement and not of the delay selection. It is the
    # ONLY input to `predicted_ripple_db`, and therefore to `crossover_v2_flow`'s
    # `MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB` threshold, which that constant
    # documents as calibrated against a fixed hardware corpus scored on THIS
    # metric — the zero-residual ripple, not the delay-carrying one. Crossing it
    # DISCLOSES rather than refuses.
    #
    # Why the disclosure keeps this frame: a candidate's own committed delay can
    # LOWER its ripple, so pointing it at a delay-carrying curve would let a
    # capture whose branches sum incoherently slip under the threshold on its
    # own alignment — and a reservation a capture can talk itself out of is
    # exactly as dishonest as a veto it could. Measured on a banked JTS3 capture
    # (`captures/r10b-alignment-20260801/ripple_vs_residual_sweep.py`): sweeping
    # the residual across the ±(period/6) snap radius, 32 of 84 sampled
    # residuals come in BELOW the zero-residual 14.8831 dB, bottoming at
    # 14.0744 dB — and that capture sits 0.12 dB under the 15.0 dB ceiling, so
    # the 0.81 dB an alignment could buy is not a hypothetical margin.
    predicted_aligned = predicted_branch_sum(
        W,
        T,
        trim_w,
        trim_t,
        polarity_sign,
    )
    ripple = _ripple_db(freqs, predicted_aligned, lo_clamped, hi)
    # `predicted_applied` — the same two branches under the delay this candidate
    # actually COMMITS. This is what gets persisted as
    # `ProgramAnalysis.predicted_sum` and becomes VERIFY's tracking reference,
    # so that comparison grades measured-vs-the-applied-model (model fidelity)
    # rather than measured against a target no realizable delay produces.
    #
    # The term is the RESIDUAL relative to the argmax-referenced frame, never
    # the applied delay itself — `summed_model_residual_delay_us` owns that
    # derivation and its docstring carries the double-count hazard. On the
    # anchor-primary path this is exactly `snap_delta_us`; at the bare anchor,
    # and on the SNR-refused path below, it is 0.0 and this call is
    # bit-identical to `predicted_aligned`.
    #
    # Read off `alignment` rather than the local `anchor_delay_us`, and gated on
    # the aligner's own status rather than the snap block's condition: a
    # trustworthy anchor is available whenever `status == ALIGNMENT_OK`, while
    # the snap block additionally needs the DECLARED plausibility bounds. Those
    # are different questions, and a preset that declares no `delay_range_ms`
    # still applies `alignment.delay_us`, so its model should carry that delay
    # too. The status gate keeps a direct caller's hand-built refused estimate —
    # which `crossover_v2.planning.alignment_to_candidate_fields` turns into a
    # trims-only, NO-delay apply — from being modelled as though a delay ran.
    #
    # The SNR refusal withdraws the anchor for the same reason. On that path the
    # committed delay came from the applied graph and the anchor came from a
    # capture the SNR policy called unusable FOR ALIGNMENT, so
    # `committed − anchor` measures their disagreement, not the speaker — and
    # this model is graded by `accountability.assess_accountability` and
    # persisted as VERIFY's tracking reference, which can FAIL the round.
    # Phasing it by that difference would let an untrusted number kill a
    # correctly-aligned speaker at VERIFY. Withheld here rather than in those
    # two gates: they must keep grading whatever model they are handed, and the
    # honest model of a round whose timing evidence was refused is the
    # independently-aligned one. `snap_delta_us` still RECORDS the disagreement
    # without phasing anything.
    _alignment_unmeasured = (
        alignment_objective in ALIGNMENT_DECLARED_POLARITY_OBJECTIVES
    )
    residual_delay_us = summed_model_residual_delay_us(
        alignment.anchor_delay_us
        if alignment.status == ALIGNMENT_OK and not _alignment_unmeasured
        else None,
        delay_us,
    )
    predicted_applied = predicted_branch_sum(
        W,
        T,
        trim_w,
        trim_t,
        polarity_sign,
        freqs_hz=freqs,
        residual_delay_us=residual_delay_us,
    )
    predicted_db = 20.0 * np.log10(np.maximum(np.abs(predicted_applied), 1e-12))
    candidate = CrossoverCandidate(
        trim_db={woofer_role: trim_w, tweeter_role: trim_t},
        polarity=polarity_label(polarity_sign),
        delay_us=delay_us,
        predicted_ripple_db=ripple,
        confidence=alignment.confidence,
        alignment_seed_ripple_db=seed_ripple_db,
        flatness_improvement_db=flatness_improvement_db,
        anchor_delay_us=anchor_delay_us,
        snap_delta_us=snap_delta_us,
        snap_found=snap_found,
        trim_band_average_db={woofer_role: trim_w, tweeter_role: trim_t_band_average},
        alignment_objective=alignment_objective,
        seed_polarity_sign=alignment.polarity_sign,
        left_anchor_lobe=left_anchor_lobe,
        # From the selection that MADE the comparison, never re-derived. A
        # selection-less arm (no scoring band, or a refused estimate) asked
        # nothing, and ``None`` is what that is.
        polarity_agrees_with_sum=(
            None if selection is None else selection.polarity_agrees_with_sum
        ),
        # Same rule, same reason: from the selection that applied the pin. No
        # selection means the seed shipped, and correlation's answer is a
        # measurement — so this is honestly ``False`` even on a round that ASKED
        # for a basin (the `alignment_prescription_not_committed` warning is
        # where that round learns its candidate did not run).
        polarity_pinned=bool(selection is not None and selection.polarity_pinned),
        ripple_polish_rejected_delta_db=ripple_polish_rejected_delta_db,
    )
    return candidate, (freqs, predicted_db)


#: ``verify_absolute["not_evaluated"]`` reasons. Named, because a screen and a
#: log both branch on them and "nobody graded this" must never be renderable as
#: "this passed".
ABSOLUTE_NO_FC = "no_crossover_fc"
ABSOLUTE_NO_TARGET = "no_candidate_crossover_target"
ABSOLUTE_NO_TRUSTED_BAND = "no_trusted_crossover_region"
#: The speaker HAS no crossover region — a 1-way main. Its own slug rather than
#: a fourth reader of the three above, which all say a round could not establish
#: a region its speaker does have and send an operator to re-measure (#3480).
ABSOLUTE_NO_CROSSOVER_TOPOLOGY = "no_crossover_topology"


def _verify_absolute_result(
    summed, segment, fc_hz, priors, measured_db=None,
) -> dict[str, Any]:
    """Measured summed response vs the CANDIDATE'S OWN crossover target across
    the crossover region — the absolute half of "did this crossover hand off as
    designed?".

    The target is ``20log10|Σ_role sign_role·C_role(f)|``: the coherent sum of
    the committed crossover transfers, carrying each region's configured
    polarity. That is what separates this from the tracking pair.
    ``priors.predicted_sum`` is built from the MEASURED branches and so
    reproduces whatever the real branch phases do, null included; this curve is
    built from the crossover alone and says what the candidate is SUPPOSED to
    sum to. No level or trim enters it — the fitter puts both branches on one
    shared level frame, and the grader below is offset-invariant, which it must
    be: the measured curve carries mic sensitivity, distance and session gain
    that no filter target has.

    Numbers only. The tolerance and the verdict belong to ``crossover_v2_flow``,
    exactly as ``VERIFY_TOLERANCE_DB`` does for tracking. ``worst_db`` is
    SIGNED and carries its frequency because a dip and a peak through the
    handoff are opposite defects and a magnitude hides which one shipped.
    Measured is smoothed and the target is not: the target is an analytic
    response with no noise to smooth, and smoothing would round the knee.
    """
    transfers = priors.configured_crossover_response_by_role
    if transfers is not None and not transfers:
        # An EMPTY map, never a missing one. Asked BEFORE the corner: "has no
        # crossover" and "nobody said where its crossover is" are two facts
        # with two remedies, and both arrive with ``fc_hz is None``.
        return {"not_evaluated": ABSOLUTE_NO_CROSSOVER_TOPOLOGY}
    if fc_hz is None:
        return {"not_evaluated": ABSOLUTE_NO_FC}
    if not transfers:
        return {"not_evaluated": ABSOLUTE_NO_TARGET}
    band = crossover_region_band_hz(
        fc_hz,
        validity_floor_hz=summed.validity_floor_hz,
        radiated_band_hz=_radiated_band_hz(segment),
    )
    if band is None:
        return {"not_evaluated": ABSOLUTE_NO_TRUSTED_BAND}
    mask = (summed.freqs_hz >= band[0]) & (summed.freqs_hz <= band[1])
    if not np.any(mask):
        return {"not_evaluated": ABSOLUTE_NO_TRUSTED_BAND}
    freqs = np.asarray(summed.freqs_hz, dtype=np.float64)
    total = np.zeros(freqs.shape, dtype=np.complex128)
    signs = priors.configured_polarity_sign_by_role or {}
    for role, response in transfers.items():
        sign = -1 if int(signs.get(role, 1)) < 0 else 1
        total = total + sign * np.asarray(response(freqs), dtype=np.complex128)
    target_db = 20.0 * np.log10(np.maximum(np.abs(total), 1e-12))
    if measured_db is None:
        measured_db = analysis_mod.smooth_fractional_octave(
            freqs, summed.magnitude_db, VERIFY_TRACKING_SMOOTHING_FRACTION,
        )
    # The SAME offset-invariant grader the tracking pair uses, so two numbers
    # on one screen mean the same kind of thing.
    rms_db, max_db = analysis_mod.tracking_error_db(
        freqs, measured_db, target_db, band,
    )
    deviation = measured_db[mask] - target_db[mask]
    deviation = deviation - float(np.mean(deviation))
    worst = int(np.argmax(np.abs(deviation)))
    return {
        "band_hz": [float(band[0]), float(band[1])],
        "rms_db": float(rms_db),
        "max_db": float(max_db),
        "worst_db": float(deviation[worst]),
        "worst_hz": float(freqs[mask][worst]),
        "n_bins": int(np.count_nonzero(mask)),
    }


def _analyze_verify(
    program, capture, sample_rate, global_offset, locations,
    calibration, priors, frame_ledger,
) -> ProgramAnalysis:
    fc_hz = float(priors.crossover_fc_hz) if priors.crossover_fc_hz else None
    seg = program.segment("sweep_verify")
    full_ir, _pre = _deconvolve_window(
        capture, seg, global_offset + seg.start_sample, sample_rate
    )
    n_fft = _n_fft_for(full_ir)
    summed = _driver_response(
        "summed", full_ir, sample_rate,
        calibration=calibration, ambient_report=None, fc_hz=fc_hz, n_fft=n_fft,
        radiated_band_hz=_radiated_band_hz(seg),
    )
    # The tracking comparator below — measured-vs-``priors.predicted_sum`` on
    # THIS capture's own grid — is deliberately NOT re-based onto the spatial
    # cloud's shared spec curve, because it answers a different question. "Did
    # apply do what the model predicted?" is a claim about one capture against
    # one prediction built from the SAME single design-axis position, and its
    # whole value is that both sides share that geometry: the predicted sum was
    # composed from the MEASURE branches captured there, so a divergence is the
    # applied graph misbehaving and nothing else. "Is the speaker flat?" is a
    # claim about the speaker, graded on the cloud. Feeding the cloud's
    # spatially-averaged curve into this comparator would compare a
    # multi-position average against a single-position prediction and read the
    # spatial variation the cloud exists to sample as a tracking error — a false
    # failure of the one gate in this flow that DOES gate (``_consume_verify``
    # → ``max_db_notch_excluded``).
    ripple = None
    reverse_null_depth = None
    tracking = None
    tracking_curve = None
    measured_db = None
    band = verify_tracking_band_hz(
        fc_hz,
        radiated_band_hz=_radiated_band_hz(seg),
        measure_excited_band_hz=priors.measure_excited_band_hz,
    )
    # The ripple and the reverse null are statements about a handoff, so they
    # stay behind the corner rather than behind the band.
    if fc_hz is not None and band is not None:
        lo, hi = band
        ripple = _ripple_db(summed.freqs_hz, summed.complex_tf, lo, hi)
        # The one null-depth definition in the tree. It lives in
        # `audio_measurement.analysis` rather than beside its consumers because
        # they sit above this package and this package may import neither.
        #
        # Guarded because the depth is read at Fc/2 and 2*Fc, `np.interp` CLAMPS outside
        # the data, and a curve that does not reach both shoulders — or reaches
        # them below the gate's validity floor — would return a number built
        # from edge values with nothing saying so. No number is the honest
        # answer there.
        from jasper.audio_measurement.analysis import crossover_null_depth_db

        lower_shoulder_hz = fc_hz / 2.0
        floor_hz = summed.validity_floor_hz
        spans_shoulders = (
            float(summed.freqs_hz[0]) <= lower_shoulder_hz
            and float(summed.freqs_hz[-1]) >= fc_hz * 2.0
        )
        above_floor = floor_hz is None or lower_shoulder_hz >= floor_hz
        if spans_shoulders and above_floor:
            reverse_null_depth = crossover_null_depth_db(
                summed.freqs_hz, summed.magnitude_db, fc_hz,
            )
    if band is not None and priors.predicted_sum is not None:
        lo, hi = band
        pred_freqs, pred_db = priors.predicted_sum
        measured_db = analysis_mod.smooth_fractional_octave(
            summed.freqs_hz, summed.magnitude_db, VERIFY_TRACKING_SMOOTHING_FRACTION
        )
        predicted_db_interp = np.interp(summed.freqs_hz, pred_freqs, pred_db)
        predicted_db = analysis_mod.smooth_fractional_octave(
            summed.freqs_hz,
            predicted_db_interp,
            VERIFY_TRACKING_SMOOTHING_FRACTION,
        )
        # Validity-floor clamp: this capture's OWN reflection gate
        # (`summed.validity_floor_hz`, from the same `_driver_response`
        # call above) can be tighter than the band above at a reflective
        # mic position — bins below that floor are an artifact
        # of a truncated gate window (gating.f_valid_floor_hz), not a
        # measurement, so they must not decide PASS/FAIL either way. This
        # generalizes the notch exclusion from "deep predicted notch" to
        # "below measurement validity": a fixed 65 ms prediction window can
        # bake a desk-bounce reflection into the predicted sum's sub-floor
        # region, invisible to notch exclusion because the false notch is
        # not always deep enough to trip it. Applies to BOTH rms and max,
        # and to the notch-exclusion bin set — the two exclusions compose
        # (clamp first, then still exclude a genuine deep predicted notch
        # above the floor).
        floor_hz = summed.validity_floor_hz
        lo_clamped = (
            max(lo, floor_hz) if floor_hz is not None and math.isfinite(floor_hz) else lo
        )
        tracking_band = (lo_clamped, hi)
        rms, max_abs = analysis_mod.tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, tracking_band,
        )
        # Notch-excluded: the actual gating comparator
        # (`crossover_v2_flow._consume_verify` reads ``max_db_notch_excluded``).
        rms_excl, max_excl = analysis_mod.notch_excluded_tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, tracking_band,
            notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
            notch_reference_db=predicted_db_interp,
        )
        # Raw full-band (pre-floor-clamp) numbers, kept as DIAGNOSTIC
        # fields only — never consumed by the gate.
        raw_rms, raw_max = analysis_mod.tracking_error_db(
            summed.freqs_hz, measured_db, predicted_db, (lo, hi),
        )
        # Hand the delta probe the very curves these scalars were reduced
        # from, so it grades one comparison rather than a second.
        tracking_curve = (summed.freqs_hz, measured_db, predicted_db)
        tracking = {
            "rms_db": rms,
            "max_db": max_abs,
            "rms_db_notch_excluded": rms_excl,
            "max_db_notch_excluded": max_excl,
            "tracking_band_hz": [tracking_band[0], tracking_band[1]],
            "rms_db_full_band": raw_rms,
            "max_db_full_band": raw_max,
        }
        # FRAME DISCIPLINE. The two curves above are not in the same frame:
        # ``predicted_db`` is an ON-AXIS two-branch model composed from the
        # MEASURE sitting, and ``measured_db`` is an IN-ROOM gated point
        # measurement from the VERIFY sitting. Differencing them raw cannot
        # tell instrument frame from model error — on one corpus a single
        # −0.79 dB/octave tilt between exactly these two frames accounted
        # for 84% of a "predictions are 2.02× optimistic" headline. So the
        # frame is fitted, the two terms are disclosed, and the residual is
        # reported BOTH ways.
        #
        # ``max_db_notch_excluded`` remains what gates
        # (``crossover_v2_flow._verify_verdict``) and every raw scalar keeps
        # its value: a measured tilt is EVIDENCE, not permission to
        # re-grade. Attributing it — directivity, mic, sitting — needs an
        # instrument this fit does not have, and until it is attributed the
        # raw number is the honest one to refuse on.
        #
        # FITTED OVER THE BINS THIS COMPARISON TRUSTS — the validity-floor
        # clamped band MINUS the deep-predicted-notch bins the gating
        # comparator already refuses to grade. Inside a modelled notch the
        # depth is hypersensitive to sub-dB branch differences, and a
        # straight line drawn through one lets the notch lever the slope: on
        # a 25 dB notch at a band edge an injected −0.800 dB/octave frame
        # recovers as +0.226, the wrong sign, and would then be "removed"
        # from the residual as instrument tilt. The mask comes from
        # ``notch_excluded_band_mask``, the only owner of that bin choice.
        #
        # This REDUCES the lever without removing it. The exclusion bounds a
        # notch's DEPTH (12 dB) and says nothing about its skirt WIDTH, so a
        # wide surviving skirt still biases the estimate: on this path, a
        # 1/6-octave 25 dB edge notch, the whole-band fit reads +5.72 and the
        # trusted-bin fit +0.31 against a −0.800 truth — 18× better and still
        # the wrong sign. A disclosed tilt is not trustworthy over a
        # notch-heavy prediction, and ``tilt_removed <= raw`` is not a
        # theorem.
        frame_mask = analysis_mod.notch_excluded_band_mask(
            summed.freqs_hz, predicted_db, tracking_band,
            notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
            notch_reference_db=predicted_db_interp,
        )
        frame = fit_frame(
            summed.freqs_hz[frame_mask],
            measured_db[frame_mask],
            predicted_db[frame_mask],
        )
        tilt_removed_rms_db: float | None = None
        tilt_removed_max_db: float | None = None
        if frame.fitted:
            # One frame, estimated once from the trusted bins, removed from
            # the measured curve — then each grade is re-taken by its OWN
            # grader over its OWN bins, so both keep their established
            # conventions and only the frame changed. Both graders
            # mean-centre their error, and mean-centring is invariant to
            # ANY additive constant, so the frame's OFFSET term cannot move
            # either number whatever bin set it was fitted on; only the
            # TILT can. That is why these are ``tilt_removed``.
            deframed_db = measured_db - frame.frame_db(summed.freqs_hz)
            # Twins for exactly the two numbers that reach a gate or a
            # screen — the notch-excluded max the tolerance reads, and the
            # RMS the expert disclosure prints. Not a twin for every raw
            # scalar in the record: a beside-number nobody reads is a
            # second thing to keep true.
            tilt_removed_rms_db = analysis_mod.tracking_error_db(
                summed.freqs_hz, deframed_db, predicted_db, tracking_band,
            )[0]
            tilt_removed_max_db = analysis_mod.notch_excluded_tracking_error_db(
                summed.freqs_hz, deframed_db, predicted_db, tracking_band,
                notch_exclusion_db=VERIFY_NOTCH_EXCLUSION_DB,
                notch_reference_db=predicted_db_interp,
            )[1]
        # ONE writer, ONE typed record. The raw pair below is assigned from
        # the very locals published above, not recomputed, so the
        # disclosure cannot state a different raw number than the gate
        # reads.
        #
        # ``raw_max_db`` is the NOTCH-EXCLUDED max, not ``max_abs``: on
        # every surface that renders a "level error" for this comparison
        # that is what the phrase means, because it is what the tolerance
        # gates on. A record pairing a tilt-removed notch-excluded max
        # against a raw non-excluded one would be two bin sets under one
        # label.
        tracking["frame"] = FrameComparison(
            fit=frame,
            raw_rms_db=rms,
            raw_max_db=max_excl,
            tilt_removed_rms_db=tilt_removed_rms_db,
            tilt_removed_max_db=tilt_removed_max_db,
        ).to_dict()
    # A v2 VERIFY program opens with a pre-pilot ambient window + a leading
    # pilot pair (design §5.2) so the post-apply capture carries its own
    # behavioral-linearity evidence AND the noise floor needed to trust it; a
    # program carrying neither leaves the verdicts ``None``.
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations, global_offset=global_offset,
    )
    # Capture integrity, computed on EVERY verify-shaped analysis: the tracking
    # comparison above is exactly the thing a spliced or clipped recording
    # invalidates.
    integrity = _verify_capture_integrity(
        program, sample_rate, locations, frame_ledger,
    )
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        summed_response=summed,
        summed_ripple_db=ripple,
        reverse_null_depth_db=reverse_null_depth,
        verify_tracking=tracking,
        verify_absolute=_verify_absolute_result(
            summed, seg, fc_hz, priors, measured_db=measured_db,
        ),
        verify_tracking_curve=tracking_curve,
        pilots=pilots,
        linearity_ok=linearity_ok,
        channel_map_ok=channel_map_ok,
        pilot_snr_ok=pilot_snr_ok,
        capture_integrity=integrity,
        glitch_detected=integrity.glitched,
    )


# --------------------------------------------------------------------------- #
# diagnostic summary (operator capture retention — jasper.web.correction_crossover_v2)
# --------------------------------------------------------------------------- #


def _gate_window_ms_of(response: "DriverResponse | None") -> float | None:
    if response is None:
        return None
    window = response.gating.get("window_ms") if response.gating else None
    return float(window) if isinstance(window, (int, float)) else None


def driver_snr_verdict(response: "DriverResponse | None") -> str | None:
    """This branch's worst relevant MAGNITUDE-class capture-SNR verdict.

    :mod:`jasper.audio_measurement.snr_policy`'s vocabulary
    (``ok``/``reduced``/``insufficient``/``unknown``), reduced across the bands
    the decision reads — ``snr_policy.worst_band_verdict``'s rule that one
    ``insufficient`` band vetoes its ``ok`` siblings is already baked into
    ``worst_relevant``. ``None`` when no verdict was computed at all, which is
    the ordinary state for a session whose CHECK carried no ambient window.

    This is the verdict every existing surface reads (the retention sidecar,
    ``measure_diag``, the dashboards). The alignment refusal reads the OTHER
    one — see :func:`driver_alignment_snr_verdict`.
    """
    return _snr_verdict_of(response.snr if response is not None else None)


def driver_alignment_snr_verdict(response: "DriverResponse | None") -> str | None:
    """This branch's worst relevant ALIGNMENT-class capture-SNR verdict.

    The law a polarity/delay decision is held to: 35 dB
    (``DRIVER.alignment_snr_ok_db``), ``ok`` or ``insufficient`` with no
    ``reduced`` rung, and per-band evidence required. ``None`` when the block
    predates this key or no verdict was computed — never a guessed verdict, so
    a capture with no ambient window keeps the flat-sum selector rather than
    being silently downgraded to the correlation answer.
    """
    snr = response.snr if response is not None else None
    if not snr:
        return None
    return _snr_verdict_of(snr.get(DRIVER_SNR_ALIGNMENT_KEY))


def _snr_verdict_of(block: Mapping[str, Any] | None) -> str | None:
    """``block["worst_relevant"]["verdict"]`` when it is a string, else None."""
    if not block:
        return None
    worst = block.get("worst_relevant") or {}
    verdict = worst.get("verdict")
    return verdict if isinstance(verdict, str) else None


def _gate_floor_source_of(response: "DriverResponse | None") -> str | None:
    """WHY this capture's gate window is what it is.

    ``window_ms`` alone cannot distinguish the two states the gate can end in,
    and they print identically: :data:`~jasper.audio_measurement.gating.FLOOR_MEASURED`
    means a reflection onset was found and the window stops at it, while
    :data:`~jasper.audio_measurement.gating.FLOOR_SEARCH_BOUND` means the
    search ran to :data:`~jasper.audio_measurement.gating.SEARCH_T_MAX_MS`
    without finding one and the window was CAPPED there. A 7 ms window means
    "reflections removed" in the first case and "no reflection found" in the
    second, and a whole hardware corpus has sat at the bound.

    ``None`` — an ungateable capture (silent/NaN, or no room after the direct
    peak to search), matching ``gating``'s own unknown-vs-value rule.
    """
    if response is None:
        return None
    source = response.gating.get("floor_source") if response.gating else None
    return str(source) if isinstance(source, str) else None


def _gate_disclosure_of(response: "DriverResponse | None") -> str | None:
    """The gate's provenance as a SENTENCE, for the retained sidecar.

    The enum beside it (:func:`_gate_floor_source_of`) is the machine answer;
    this is the one a person reading the dump gets, and it is rendered — never
    composed — here: the copy has exactly one writer,
    :func:`jasper.audio_measurement.gate_disclosure.describe_gate`.
    """
    if response is None or not response.gating:
        return None
    return gate_disclosure.describe_gate(response.gating)


def analysis_diagnostic_summary(analysis: Any) -> dict[str, Any]:
    """Flat, JSON-safe numeric diagnostics from one :class:`ProgramAnalysis`.

    The distortion replays attach this to a capture they re-analyse so the
    numbers can be compared against the ones a banked corpus already carries.
    Reads only fields ``ProgramAnalysis`` and its nested dataclasses already
    carry — nothing here is recomputed. Per-driver/per-pilot fields key off
    each entry's OWN ``role`` string rather than a hardcoded label, since this
    runs at the analyze seam, before the v2 session's role mapping exists.

    Deliberately duck-typed (``analysis: Any``) and defensive throughout: it
    must never raise even when a test double stands in for a real
    ``ProgramAnalysis``, so every field access goes through
    ``getattr(..., None)`` and a malformed or foreign ``analysis`` degrades to
    an emptier summary rather than raising past the caller's guard.
    """
    out: dict[str, Any] = {"phase": getattr(analysis, "phase", None)}

    drift = getattr(analysis, "drift", None)
    if drift is not None:
        out["epsilon_ppm"] = round(float(drift.epsilon_ppm), 3)
        out["max_residual_samples"] = round(float(drift.max_residual_samples), 3)
        out["repeat_level_delta_db"] = round(
            float(getattr(drift, "repeat_level_delta_db", 0.0)), 3
        )
        out["glitch_detected"] = bool(drift.glitch_detected)
        # WHICH bound tripped + the discrete step, when one is resolved — a
        # glitched capture's `epsilon_ppm` above is an artefact of that step,
        # not a drift measurement. See DriftEstimate's docstring.
        out["glitch_inputs"] = ",".join(getattr(drift, "glitch_inputs", ()) or ())
        # `discontinuity_samples` is `DISCONTINUITY_UNRESOLVED` (a `str`, not a
        # number) when the located sweeps weren't trustworthy enough to fit a
        # step from — `float()` would raise on that value, which this
        # duck-typed, must-never-raise summary cannot afford.
        discontinuity = getattr(drift, "discontinuity_samples", 0.0)
        out["discontinuity_samples"] = (
            round(float(discontinuity), 3)
            if isinstance(discontinuity, (int, float))
            else discontinuity
        )
        out["discontinuity_after_segment"] = getattr(
            drift, "discontinuity_after_segment", "",
        )
        # Diagnostic-only, never gated — see
        # DriftEstimate.per_role_epsilon_ppm's docstring.
        for role, eps in (getattr(drift, "per_role_epsilon_ppm", None) or {}).items():
            out[f"{role}_repeat_epsilon_ppm"] = round(float(eps), 3)

    alignment = getattr(analysis, "alignment", None)
    if alignment is not None:
        out["alignment_confidence"] = round(float(alignment.confidence), 4)
        out["alignment_confidence_source"] = getattr(
            alignment, "confidence_source", "gcc_phat",
        )
        out["alignment_status"] = alignment.status
        out["delay_us"] = round(float(alignment.delay_us), 3)
        seed_delay_us = getattr(alignment, "seed_delay_us", None)
        if seed_delay_us is not None:
            out["alignment_seed_delay_us"] = round(float(seed_delay_us), 3)
            out["alignment_refinement_delta_us"] = round(
                float(alignment.delay_us) - float(seed_delay_us), 3,
            )
        out["polarity"] = alignment.polarity
        # The polarity half of the same seed-vs-committed pair the two lines
        # above report for the delay. ``None`` on an estimate nothing
        # cross-checked; the sidecar carries the tri-state rather than
        # flattening "correlation agreed" and "nobody asked" together.
        out["polarity_agrees_with_sum"] = getattr(
            alignment, "polarity_agrees_with_sum", None,
        )

    candidate = getattr(analysis, "candidate", None)
    if candidate is not None:
        out["predicted_ripple_db"] = round(float(candidate.predicted_ripple_db), 4)
        out["alignment_objective"] = getattr(candidate, "alignment_objective", "")
        seed_polarity_sign = getattr(candidate, "seed_polarity_sign", None)
        out["seed_polarity"] = (
            None if seed_polarity_sign is None
            else polarity_label(int(seed_polarity_sign))
        )
        out["left_anchor_lobe"] = bool(getattr(candidate, "left_anchor_lobe", False))
        rejected_polish_db = getattr(
            candidate, "ripple_polish_rejected_delta_db", None
        )
        out["ripple_polish_rejected_delta_db"] = (
            None if rejected_polish_db is None else round(float(rejected_polish_db), 3)
        )
        anchor_delay_us = getattr(candidate, "anchor_delay_us", None)
        if anchor_delay_us is not None:
            out["anchor_delay_us"] = round(float(anchor_delay_us), 3)
            snap_delta_us = getattr(candidate, "snap_delta_us", None)
            out["snap_delta_us"] = (
                round(float(snap_delta_us), 3) if snap_delta_us is not None else None
            )
            out["snap_found"] = bool(getattr(candidate, "snap_found", False))
        if candidate.alignment_seed_ripple_db is not None:
            out["alignment_seed_ripple_db"] = round(
                float(candidate.alignment_seed_ripple_db), 4,
            )
            out["flatness_improvement_db"] = round(
                float(candidate.flatness_improvement_db), 4,
            )

    for resp in getattr(analysis, "driver_responses", None) or ():
        role = resp.role
        out[f"{role}_gate_window_ms"] = _gate_window_ms_of(resp)
        out[f"{role}_gate_floor_source"] = _gate_floor_source_of(resp)
        out[f"{role}_gate_disclosure"] = _gate_disclosure_of(resp)
        out[f"{role}_validity_floor_hz"] = resp.validity_floor_hz
        if resp.snr is not None:
            worst = resp.snr.get("worst_relevant") or {}
            out[f"{role}_snr_db"] = worst.get("estimated_snr_db")
            out[f"{role}_snr_verdict"] = driver_snr_verdict(resp)
            # WHICH band produced that pair — without it a retained clip
            # records a limiting SNR whose band has to be re-derived from the
            # crossover frequency and the declared driver bands. ``None`` when
            # the selected band carried no id: never a stand-in label.
            out[f"{role}_snr_band"] = worst.get("band_id")
            # The ALIGNMENT-class trio, beside the MAGNITUDE one. Published
            # because the number it carries DECIDES polarity and delay
            # (`_select_alignment_pair` refuses the pair when this verdict is
            # `insufficient`).
            #
            # A separate trio rather than a widened one because they answer
            # different questions under different laws: magnitude trusts 25 dB
            # and has a `reduced` rung, alignment demands 35 dB and has none
            # (`snr_policy._band_verdict`). Collapsing them would let a capture
            # that is fine for a trim read as fine for a null depth.
            #
            # `.get(...) or {}` twice over, deliberately: a block written
            # without this key carries no `alignment` at all, and this
            # function's contract is that a foreign or partial analysis
            # degrades to an emptier summary rather than raising past the
            # caller's best-effort guard.
            alignment = resp.snr.get(DRIVER_SNR_ALIGNMENT_KEY) or {}
            alignment_worst = alignment.get("worst_relevant") or {}
            out[f"{role}_alignment_snr_db"] = alignment_worst.get(
                "estimated_snr_db"
            )
            out[f"{role}_alignment_snr_verdict"] = driver_alignment_snr_verdict(
                resp
            )
            out[f"{role}_alignment_snr_band"] = alignment_worst.get("band_id")

    for pilot in getattr(analysis, "pilots", None) or ():
        role = pilot.role
        snr_db = getattr(pilot, "snr_db", math.inf)
        out[f"{role}_pilot_snr_db"] = round(snr_db, 2) if math.isfinite(snr_db) else None
        out[f"{role}_captured_delta_db"] = round(float(pilot.captured_delta_db), 3)
        out[f"{role}_programmed_delta_db"] = round(float(pilot.programmed_delta_db), 3)
        out[f"{role}_channel_map_target_rise_db"] = pilot.channel_map_target_rise_db
        out[f"{role}_channel_map_cross_rise_db"] = pilot.channel_map_cross_rise_db
        # Both raws AND the ratio: the raws say which half moved, the ratio is
        # what the CROSS verdict was decided on
        # (`CHANNEL_MAP_MIN_ISOLATION_DB`).
        out[f"{role}_channel_map_isolation_db"] = channel_map_isolation_db(
            pilot.channel_map_target_rise_db, pilot.channel_map_cross_rise_db
        )

    gain_plan = getattr(analysis, "gain_plan", None)
    if gain_plan is not None:
        out["gain_plan_snr_floor_ok"] = gain_plan.snr_floor_ok
        out["gain_plan_predicted_peak_dbfs"] = round(
            float(gain_plan.predicted_peak_dbfs), 3
        )
        # The per-role MEASURE level solve, flattened one field per role so the
        # forensic dump reads like every other per-role block above it.
        for role, solve in (getattr(gain_plan, "role_solves", None) or {}).items():
            out[f"{role}_measure_gain_db"] = round(float(solve.gain_db), 3)
            out[f"{role}_measure_gain_reduction_db"] = round(
                float(solve.reduction_db), 3
            )
            out[f"{role}_measure_gain_bound_by"] = solve.bound_by

    for flag in ("pilot_snr_ok", "linearity_ok", "channel_map_ok"):
        value = getattr(analysis, flag, None)
        if value is not None:
            out[flag] = value

    # VERIFY capture integrity, flattened one field per fact like every other
    # block here. Absent — not empty — on CHECK/MEASURE, whose glitch verdict
    # is the ``drift`` block above.
    integrity = getattr(analysis, "capture_integrity", None)
    if integrity is not None:
        out["integrity_failed"] = ",".join(getattr(integrity, "failed", ()) or ())
        out["integrity_not_evaluated"] = ",".join(
            getattr(integrity, "not_evaluated", ()) or ()
        )
        out["integrity_locate_confidence_min"] = getattr(
            integrity, "locate_confidence_min", None
        )
        out["integrity_schedule_residual_ms_worst"] = getattr(
            integrity, "schedule_residual_ms_worst", None
        )
        out["integrity_clipped_segments"] = ",".join(
            getattr(integrity, "clipped_segments", ()) or ()
        )

    # End-to-end frame accounting, flat like every other block. Present on
    # EVERY phase, unlike the integrity block above, so a MEASURE clip in the
    # forensic ring is self-describing about frame loss too.
    ledger = getattr(analysis, "frame_ledger", None)
    if ledger is not None:
        out["frames_received"] = ledger.received_frames
        out["frames_declared"] = ledger.declared_frames
        out["frames_encoded"] = ledger.encoded_frames
        out["frames_render_gaps"] = ledger.render_gaps
        out["frames_render_gap_frames"] = ledger.render_gap_frames
        out["frames_lost_at"] = ",".join(ledger.lost_at)

    summed_response = getattr(analysis, "summed_response", None)
    if summed_response is not None:
        out["verify_gate_window_ms"] = _gate_window_ms_of(summed_response)
        out["verify_gate_floor_source"] = _gate_floor_source_of(summed_response)
        out["verify_gate_disclosure"] = _gate_disclosure_of(summed_response)
        out["verify_validity_floor_hz"] = summed_response.validity_floor_hz

    tracking = getattr(analysis, "verify_tracking", None)
    if tracking:
        for key in ("rms_db", "max_db", "rms_db_notch_excluded", "max_db_notch_excluded"):
            if key in tracking:
                out[key] = tracking[key]
        band = tracking.get("tracking_band_hz")
        if isinstance(band, (list, tuple)) and len(band) == 2:
            out["tracking_band_lo_hz"] = band[0]
            out["tracking_band_hi_hz"] = band[1]
        # Frame discipline. A retained clip is the forensic record of ONE
        # comparison and the frame is half of what that comparison was, so the
        # frame's terms and the tilt-removed grades ride the sidecar BESIDE the
        # raw scalars above — never instead of them. Present with ``None`` terms
        # when the comparison ran but no frame could be fitted; absent only when
        # no tracking comparison happened at all.
        #
        # NAMING, deliberately not aligned with the household-facing record:
        # here the twin is ``max_db_notch_excluded_tilt_removed`` because it
        # sits beside ``max_db_notch_excluded`` and a forensic dump should pair
        # by name, while the durable ``verify.frame`` block calls the same
        # number ``max_db_tilt_removed`` because on that surface the gated max
        # is spelled ``max_db``. One number, written once, in
        # ``_analyze_verify``.
        frame = tracking.get("frame")
        if isinstance(frame, Mapping):
            out["frame_offset_db"] = frame.get("offset_db")
            out["frame_tilt_db_per_octave"] = frame.get("tilt_db_per_octave")
            out["frame_pivot_hz"] = frame.get("pivot_hz")
            out["frame_n_bins"] = frame.get("n_bins")
            tilt_removed = frame.get("tilt_removed")
            if isinstance(tilt_removed, Mapping):
                out["rms_db_tilt_removed"] = tilt_removed.get("rms_db")
                out["max_db_notch_excluded_tilt_removed"] = tilt_removed.get("max_db")

    return out
