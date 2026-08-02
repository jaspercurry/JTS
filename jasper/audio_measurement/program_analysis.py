# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure analysis of a crossover excitation-program capture.

``analyze_program_capture(program, samples, sample_rate) → ProgramAnalysis`` is
the single, deterministic, fixture-testable half of the conductor model (design
§5.6): every quantity — segment locations, per-segment integrity, in-capture
clock drift, per-driver gated responses, tweeter-vs-woofer alignment, and the
crossover candidate + predicted sum — derives from the ``(program, capture)``
pair with no side-channel state.

Pipeline (per phase):

1. **Locate** — matched-filter the first stimulus (one global offset), then each
   later segment at its scheduled offset ± a small window; record schedule
   residuals. Generalizes ``driver_acoustics._capture_to_magnitude``'s locator.
2. **Integrity** — per-segment peak dBFS + clipped-run detection (a run of ≥3
   consecutive samples at ≥0.999 full scale is a clip).
3. **Drift (MEASURE)** — ε from the woofer→woofer-repeat separation, cross-checked
   against the schedule-residual slope of all located segments; disagreement or
   |ε|>500 ppm ⇒ ``glitch_detected`` (callers must reject the capture).
   **VERIFY** plays one mono summed sweep, so none of those repeat-pair
   comparisons exists there; it gets its own shaped check instead
   (``_verify_capture_integrity`` → :class:`CaptureIntegrity`, issue #1971) —
   heard / on-schedule / unclipped, plus an explicit ``not_evaluated`` record
   naming the MEASURE-era checks that structurally cannot run. Both phases
   project their verdict onto the same ``glitch_detected`` bool.
4. **Per-driver response** — deconvolve → direct-arrival window + first-reflection
   gate → complex TF + magnitude (mic cal applied if given); band-SNR verdicts.
5. **Alignment (MEASURE)** — band-limited GCC-PHAT supplies an ×16-upsampled,
   ε/parallax-corrected seed, polarity, and capture confidence; the applied
   delay is selected from the drift-corrected physical peak-gap ANCHOR plus a
   ±(period/6) gated local-peak snap (methodology §10, 2026-07-22). Summed
   flatness has been evidence only, never the selector, since #1649.
6. **Candidate + prediction** — as-crossed branches (design §5.4) ⇒ trims level-
   match the branches through the crossover. Two sums come out of this, on
   purpose (rung P3 / R10b): ``predicted_sum``, the branches at the COMMITTED
   trim and delay — what the emitted graph will do, and what VERIFY's tracking
   comparison grades against — and the independently aligned
   ``W_xo·g_w + s·T_xo·g_t``, whose Fc±1-octave ripple is reported as
   ``predicted_ripple_db``, a capture-quality number the delay must not move.

CHECK additionally returns the ambient band floor, per-pilot captured levels +
the behavioral linearity verdict (§3.4), channel-map sanity, and the solved
``GainPlan`` for MEASURE. VERIFY returns the gated summed response + ripple vs a
supplied predicted sum. Every phase with a leading pilot pair also returns
``pilot_snr_ok`` — a real verdict on all of them since issue #1810 gave
MEASURE / VERIFY their own short pre-pilot room-listening window.

Reuses the measurement kernel (:mod:`~jasper.audio_measurement.sweep` /
``deconv`` / ``gating`` / ``snr_policy`` / ``analysis``) and mirrors
``jasper.capture_relay.alignment``'s confidence vocabulary. No I/O, no product
policy, no ``jasper.active_speaker`` import.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement import analysis as analysis_mod
from jasper.audio_measurement import calibration as calibration_mod
from jasper.audio_measurement import deconv, gate_disclosure, gating, snr_policy
from jasper.audio_measurement.frame_fit import FrameComparison, fit_frame
from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
    KIND_PILOT,
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_VERIFY,
    STIMULUS_KINDS,
    ExcitationProgram,
    ProgramSegment,
    segment_stimulus,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.quality_model import DRIVER
from jasper.capture_relay.alignment import cross_correlation_alignment
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.audio_measurement.calibration import CalibrationCurve

logger = logging.getLogger(__name__)

# --- locator / drift / alignment tuning ---
# Per-segment search half-window around the drift-free scheduled offset. Wide
# enough for a few-hundred-ppm drift over a ~25 s program (≈6 ms) plus acoustic
# delay, far tighter than the global first-stimulus search.
SEGMENT_SEARCH_S = 0.030
# Capture bound (kernel contract: defense at the FFT, 1 GB Pi — mirrors
# deconv.cap_capture_length's rationale). A legitimate conductor capture is the
# program plus a small phone-start lead; this margin bounds the global offset
# the locator can see. A stuck recording is truncated to
# program duration + this margin before any full-rate FFT runs.
CAPTURE_BOUND_MARGIN_S = 10.0
# Global-offset locate runs at this downsampled rate (mirrors
# driver_acoustics._capture_to_magnitude's 16 kHz locate) so the whole-capture
# correlation never allocates hundreds of MB; the arrival is then refined at
# the full rate inside a tiny window.
LOCATOR_RATE_HZ = 16_000
# Clip run: a run of at least this many consecutive samples at/above full scale.
# The at-full-scale threshold is the shared digital-full-scale fact owned by
# quality_model (same value every capture-quality layer reads), not a re-declared
# literal.
CLIP_RUN_SAMPLES = 3
CLIP_ABS_THRESHOLD = DRIVER.clip_abs_threshold
DBFS_FLOOR = -120.0

# A capture is rejected when the drift baselines disagree by more than this many
# samples-equivalent, or the primary drift exceeds the ppm bound (design §5.6.3).
GLITCH_RESIDUAL_SAMPLES = 1.5
MAX_DRIFT_PPM = 500.0

# The two woofer sweeps of a MEASURE program are bit-identical stimuli, so a
# clean capture reproduces the same captured level for both. A larger gap is a
# gain-rider (browser AGC nudging the level between the two sweeps) — a
# complement to the timing baselines (design §5.2). PROVISIONAL pending W6 bench
# distributions. A failure REUSES the ``drift_baselines_disagree`` glitch
# verdict — never a new user-facing reason code (design §5.2).
#
# Level is measured band-relative (in-band RMS over the woofer's own declared
# band, via `_band_power` — see `_estimate_drift`), not full-band single-sample
# PEAK — fixing the same 2026-07-20 bug class as `LINEARITY_TOLERANCE_DB`
# below (#1594, #1615): a low-frequency, room-mode-excited sweep's full-band
# peak is an unstable estimator, and two real hardware captures (Dayton
# iMM-6C AND UMIK-2) measured two genuinely-identical woofer sweeps 0.64 dB
# apart by full-band peak but only 0.06-0.24 dB apart by in-band RMS. In-band
# RMS is stable, so this tolerance stays tight.
REPEAT_LEVEL_TOLERANCE_DB = 0.3

# Timeline-discontinuity change-point (2026-07-27 forensics, issue #1765).
#
# Every glitch this system has actually produced was a DISCRETE timeline step,
# never clock drift: across the 2026-07-22..27 JTS3 journal (57 MEASURE
# captures) both `program_analysis.glitch` events fired on the residual guard
# with epsilon comfortably INSIDE MAX_DRIFT_PPM — 2026-07-23 at 30.53 ppm /
# 798.46 samples, 2026-07-27 at 94.12 ppm / 32.36 samples. The 500 ppm bound
# has never fired. The 2026-07-27 capture was recovered from JTS3's retained
# dump and cross-correlated (ncc 0.92-0.99) against the two captures that
# PASSED minutes later on the same program/rig: five of its six located sweeps
# agreed within 0.8 samples while the first woofer sweep sat 64.3 samples
# apart — a single +64-sample (1.333 ms) insertion between `sweep_w` and
# `sweep_t`. That one step predicts every reported number (woofer pair
# straddles it: 32.1 + 64/1_036_800 = 93.8 ppm vs 94.118 observed; tweeter
# pair sits entirely after it, so 32.097 ppm = the TRUE drift; demeaned
# residual 32.2 predicted vs 32.357 observed).
#
# So the reported `epsilon_ppm` on a glitched capture is an ARTEFACT of the
# step, not a drift measurement, and "the baselines disagree" says nothing
# about what actually broke. This estimator names it: fit a single-step
# timeline model across the located sweeps and report the step's size and
# where it landed. Diagnostic ONLY — like `per_role_epsilon_ppm` it never
# gates `glitch_detected`, so no capture's accept/reject changes.
#
# Thresholds: a clean capture's integer-located residuals sit well under a
# sample, so a fitted step this large cannot come from locate noise; the RSS
# ratio additionally requires the step model to EXPLAIN the capture rather
# than merely soak up one extra degree of freedom.
DISCONTINUITY_MIN_SAMPLES = 4.0
DISCONTINUITY_RSS_RATIO = 0.25

# #1839 addendum: the step-fit above trusts its OWN INPUT — every located
# sweep's `located_start` — implicitly. That trust is unearned once the
# correlator can barely find the sweep at all: session
# cap_-Us10xORVNlFa_dgi-sP7g's sweeps located at confidence 0.0298 (too
# quiet, #1838's field incident), and this fit still reported a
# confident-looking -2090.5-sample step fitted from what was actually noise.
# `jasper.active_speaker.crossover_v2_flow`'s `_sweep_locate_confidence_ok`
# makes the identical judgment against the identical `SegmentLocation.confidence`
# signal, at the same 0.3 floor — but that constant lives one layer up (the
# flow conductor depends on this module, not the reverse), so importing it
# here would invert that dependency. Duplicated deliberately: this module
# needs its own "was this sweep even heard" precondition before it fits a
# step, not merely a return value a caller might forget to cross-check.
#
# Named for the JUDGMENT, not for its first caller (#1971). It carried a
# `DISCONTINUITY_` prefix while the step fit was its only consumer;
# `_verify_capture_integrity` below is the second, and that prefix would have
# claimed the floor was about step fitting when what it actually decides is
# "was this sweep even heard".
SWEEP_LOCATE_CONFIDENCE_FLOOR = 0.3

# `crossover_v2_flow.SWEEP_SCHEDULE_RESIDUAL_CEILING_MS`'s twin (#1971) — the
# G2 xrun detector's ceiling, duplicated here for the same layering reason as
# the floor above and pinned against it by the same contract test. A located
# stimulus further than this off its SCHEDULED slot did not drift there; the
# timeline was spliced. The flow keeps applying it to MEASURE's `KIND_SWEEP`
# segments; this module applies it to VERIFY's single `KIND_SUMMED_SWEEP`,
# which no flow-side gate has ever filtered for (the whole of #1971).
#
# INHERITED, NOT RE-DERIVED. The 5 ms number comes from the 2026-07-22 MEASURE
# evidence the flow's copy documents (a glitched capture at −25…−28 ms against
# a clean corpus running ≤1.5 ms). No equivalent VERIFY-corpus distribution has
# been measured, so the margin on this path is an assumption carried over from
# a capture of the same programs by the same locator — not a measurement of
# this phase. Widening or tightening it wants a VERIFY corpus first.
SWEEP_SCHEDULE_RESIDUAL_CEILING_MS = 5.0

# Sentinel for "the located sweeps were not trustworthy enough to fit a step
# from at all" — distinct from `0.0`, which means "confidently no step" on a
# clean, well-located capture. Collapsing the two would hide exactly the
# ambiguity #1839 exists to remove: a future bench pass reading the
# discontinuity distribution could not otherwise tell "clean" from "never
# checked". A `str`, not a `float`, so a consumer cannot mistake it for a
# vanishingly small step; see `DriftEstimate.discontinuity_samples` and
# `analysis_diagnostic_summary` for the two places that must (and now do)
# handle the non-numeric case.
DISCONTINUITY_UNRESOLVED = "unresolved"

# --- VERIFY capture integrity (issue #1971) -------------------------------- #
#
# Three states, and the third is the point. ``_estimate_drift``'s glitch
# verdict is a bare ``bool``, which is honest on MEASURE (every one of its
# three inputs is computable from a MEASURE program) and a lie on VERIFY,
# where none of them is: VERIFY plays ONE mono summed sweep, so there is no
# repeat pair to take an epsilon, a level agreement, or a within-role
# residual from. Before #1971 that produced ``glitch_detected is False`` on
# every VERIFY analysis ever taken — a False that meant "nobody looked",
# indistinguishable from "looked and it was clean".
#
# So a VERIFY capture records a per-check STATUS. ``not_evaluated`` is what a
# structurally-inapplicable check reports, and it is never collapsed into a
# pass.
INTEGRITY_PASS = "pass"
INTEGRITY_FAIL = "fail"
INTEGRITY_NOT_EVALUATED = "not_evaluated"

# The checks a single summed sweep CAN answer. These are the substitutes the
# 2026-07-31 P0 repeat-floor bench had to assemble by hand
# (captures/repeat-floor-20260731/README.md) because no shipped gate covered
# the VERIFY path.
INTEGRITY_CHECK_SWEEP_HEARD = "summed_sweep_heard"
INTEGRITY_CHECK_SWEEP_SCHEDULE = "summed_sweep_schedule"
INTEGRITY_CHECK_CLIPPED_RUN = "clipped_run"
# The MEASURE-era checks a single summed sweep CANNOT answer. Recorded by name
# rather than omitted: a reader asking "did anything check for a dropped
# buffer here?" gets the answer, and a future VERIFY program that grows a
# repeat pair has the exact list of what would become evaluable. Their MEASURE
# counterparts are ``DriftEstimate.glitch_inputs``'s ``epsilon_out_of_bound``
# / ``repeat_level_disagree`` / ``residual_desync`` and the diagnostic
# ``_locate_discontinuity`` step fit.
INTEGRITY_CHECK_REPEAT_EPSILON = "repeat_epsilon"
INTEGRITY_CHECK_REPEAT_LEVEL = "repeat_level_agreement"
INTEGRITY_CHECK_WITHIN_ROLE_DESYNC = "within_role_desync"
INTEGRITY_CHECK_DISCONTINUITY_STEP = "discontinuity_step"

# Why each unevaluated check could not run — one short clause, stored on the
# check itself so the record explains itself without a lookup table.
_INTEGRITY_NO_REPEAT_PAIR = "verify plays one summed sweep: no repeat pair"
_INTEGRITY_STEP_NEEDS_MORE_SWEEPS = (
    "a step fit needs more located sweeps than a verify program has"
)
_INTEGRITY_NO_SUMMED_SWEEP = "no summed sweep located in this capture"
_INTEGRITY_NO_STIMULUS = "no stimulus segment located in this capture"
_INTEGRITY_SWEEP_NOT_HEARD = (
    "the summed sweep was not confidently located, so its schedule residual "
    "is not evidence"
)

# GCC-PHAT sub-sample refinement (design §5.6.5).
GCC_UPSAMPLE = 16
DEFAULT_ALIGN_SEARCH_MS = 2.0  # geometry prior bound on |relative delay|

# Gated local-peak snap radius, as a fraction of the crossover period at Fc
# (delay-selection methodology: docs/crossover-measurement-reproducibility-plan.md
# §10, 2026-07-22 bake-off + methodology entries). The drift-corrected physical
# peak-gap anchor owns comb-lobe selection; the fine stage snaps it to the
# nearest local maximum of the SAME upsampled GCC-PHAT correlation, but only
# within ±(period/6) of the anchor. period/6 = λ/6 is the GPS integer-ambiguity
# lobe-selection budget (Teunissen): a coarse anchor with error σ ≤ λ/6 selects
# the correct comb lobe with ≥99.7% probability. On the E0 hardware corpus this
# radius is ~2× the largest legitimate observed snap (≈39 µs) and structurally
# below the +166 µs stable-but-wrong correlation feature the bake-off ruled out.
# A snapped selection is bounded to the radius plus at most one upsampled bin of
# parabolic sub-sample refine (~1/GCC_UPSAMPLE sample), so it can never rail onto
# a neighbouring comb lobe. PROVISIONAL pending more crossover-measurement
# hardware runs; declaration-driven — the µs radius derives from the priors' Fc,
# never a hardcoded value (≈83.3 µs at Fc=2 kHz).
GCC_SNAP_RADIUS_PERIODS = 1.0 / 6.0

# Alignment estimator status vocabulary.
ALIGNMENT_OK = "ok"
ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW = "delay_exceeds_search_window"

# Overlap band for trims / alignment / ripple: Fc ± 1 octave.
OVERLAP_OCTAVE_RATIO = 2.0

# Ripple-optimal trim POLISH (#1667, scoped by PR-L3): re-solve the tweeter
# trim for minimum summed-response ripple, seeded by solve_branch_trims'
# band-average level match. #1667 introduced it to absorb that level match's
# one-sided-band bias; PR-L3 removed the bias at its source instead (see
# solve_branch_trims), so this is now a flatness polish on an already-correct
# level — and `_build_candidate` runs it only where its own evaluation band
# straddles Fc, since a band that cannot see the woofer cannot express the
# handoff level.
#
# Search window: the seed +/- this many dB, at this step. Issue #1667's own
# hardware corpus (jts3, 5 replayed runs) observed a 1.7-6.3 dB gap between
# the seed and the ripple optimum; +/-10 dB leaves headroom beyond that range
# on both sides so the scan is never truncated at its own edge for a real
# capture, while RIPPLE_TRIM_SANITY_MARGIN_DB below still catches a result
# that wanders implausibly far from a real level match.
RIPPLE_TRIM_SEARCH_WINDOW_DB = 10.0
RIPPLE_TRIM_SEARCH_STEP_DB = 0.1

# Flat-minimum regularization (#1667 follow-up, architect review): the exact
# minimizer of a SHALLOW ripple bowl (a wide, nearly-flat region straddling
# the true minimum — e.g. 0.31 dB of ripple spread across 2+ dB of trim, an
# observed shape on the real N=3 hardware capture) is sensitive to
# measurement noise and can wander session to session; an applied trim that
# shifts audibly between re-measurements is a worse product property than a
# fraction-of-a-dB of extra ripple. Among every candidate within this many
# dB of the scan's GLOBAL minimum ripple — a set that collapses to a single
# point for a sharp/unique minimum, or spans a wide plateau for a shallow
# one — the search prefers whichever is CLOSEST TO THE SEED (the
# band-average trim), trading a negligible, inaudible amount of measured
# flatness for session-to-session repeatability. This subsumes plain
# exact-tie breaking (an exact tie is trivially within epsilon too). 0.25 dB
# is below the threshold of an audible ripple difference and comfortably
# above the scan grid's own 0.1 dB step, so a genuinely sharp minimum's
# single best point is never accidentally widened into a multi-candidate
# plateau by grid quantization alone.
RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB = 0.25

# How far the ripple-optimal trim may move from the band-average seed before
# it is treated as untrustworthy and discarded in favor of the seed (with a
# WARNING — never a silent wild trim). Deliberately narrower than the search
# window above, so the guard has real teeth. Mirrors
# jasper.active_speaker.crossover_v2_flow.LINEARIZATION_TRIM_SANITY_MARGIN_DB
# — same reasoning, applied one layer earlier to the raw (pre-linearization)
# solve.
RIPPLE_TRIM_SANITY_MARGIN_DB = 6.0

# A trim is a passive level-match: never net gain (> 0 dB), and never beyond
# the shared -60 dB attenuation floor used across the active-speaker
# candidate/profile machinery
# (jasper.active_speaker.measured_crossover_candidate._MAX_ATTENUATION_DB /
# baseline_profile._MAX_ATTENUATION_DB). Mirrored locally rather than
# imported, the same way those two mirror each other — this module does not
# import jasper.active_speaker (see the module docstring). solve_branch_trims's
# own min()-based formula keeps its output in this range implicitly; the
# ripple-optimal scan must enforce it explicitly, since an unconstrained
# ripple minimum has no such guarantee (a flatter-but-physically-invalid
# "trim" is not a real answer).
RIPPLE_TRIM_MAX_DB = 0.0
RIPPLE_TRIM_MIN_DB = -60.0

# How far the two branches' realized levels — read on their own mirrored
# ±1-octave half-bands about Fc, NOT across each driver's whole passband — may
# sit apart after the committed trim before the pair is refused (linearization-integrity PR-L4
# item 1 — the assertion nothing in the chain made). The design intent is that
# they are EQUAL: a 2-way's summed response is flat only when each branch hands
# off at the same level, which is the whole purpose of a trim. This is the
# acceptance check on that intent, read by
# :func:`realized_branch_level_match`.
#
# Why 3.0 dB, from both directions:
#
# * FLOOR — the honest disagreement this estimator shows on real captures.
#   Across all five archived 2026-07-24/25 JTS3 cdhorn MEASURE captures the
#   level-match frame and the fit frame agree to 1.08-1.30 dB after PR-L3, and
#   the estimator carries a KNOWN +0.54 dB linear-bin systematic
#   (:func:`solve_branch_trims`' own N1 note). A tolerance near that floor would
#   turn a normal session into a refusal, which is the one failure mode a
#   safety assertion must not have.
#
#   That 1.08-1.30 dB is the PRE-#1929 measurement, taken with the fit's median
#   over each driver's whole declared capture span. #1929 moved that median to
#   the driver's radiating band and the range moved with it — archived run 5
#   goes 1.076 -> 0.510 dB. The floor argument is unaffected in DIRECTION (the
#   disagreement shrank, so 3.0 dB is if anything more generous than when it
#   was derived), which is why the constant did not move; but the number a
#   reader should quote today lives beside the importing gate, at
#   ``jasper.active_speaker.crossover_v2_flow.LEVEL_FRAME_AGREEMENT_TOLERANCE_DB``,
#   together with what #1929 did NOT close.
# * CEILING — the level error at which the flat spec must fail anyway. An
#   inter-branch level error of D dB appears in the summed response as a step
#   across Fc; the spec's reference is a power mean spanning BOTH sides
#   (250 Hz-8 kHz), so each side lands roughly D/2 off it. At D = 3.0 that is
#   1.5 dB per side — exactly ``flat_spec.SPEC_BANDS[0]``'s tolerance. Above
#   3 dB the speaker is out of spec on tonal balance alone, whatever else the
#   fit achieved.
#
# So the band between "measurable" and "already out of spec" is narrow, and
# 3.0 dB is the top of it: every level error the spec itself calls a failure is
# caught, with 2.3x margin over the worst honest frame disagreement measured.
# The 2026-07-27 profile the owner heard as dark would have refused here.
REALIZED_LEVEL_MATCH_TOLERANCE_DB = 3.0

# Direct-arrival window used to isolate each driver's IR before deconvolution
# magnitude / alignment (mirrors deconv defaults; the pre guard catches the
# non-causal deconvolution shoulder).
IR_PRE_MS = 5.0
IR_POST_MS = 60.0

# Deconvolution window pre-guard: how far BEFORE the scheduled sweep position the
# window starts, so the window fully contains the sweep even though the global
# offset folds in the first driver's (small) acoustic delay. Both drivers use
# the SAME pre-guard and global-offset anchor, so their deconvolved IR direct
# peaks land at the pre-guard sample ± the relative delay — the aligner relies
# on that shared time base.
DECONV_PRE_GUARD_S = 0.25

# Gain solve: land the MEASURE capture peak in [-12, -9] dBFS with ≥6 dB guard.
# Since #1825 this is the solve's CEILING rather than its target — see
# `_solve_role_gain`.
DEFAULT_TARGET_CAPTURE_DBFS = -10.5
GAIN_GUARD_DB = 6.0
GAIN_MAX_DIGITAL_PEAK_DBFS = -GAIN_GUARD_DB  # digital peak must sit ≤ this

# --- SNR-solved MEASURE level (issue #1825) --------------------------------- #
#
# MEASURE is the only phase in a v2 session whose level is solved: CHECK's
# pilots and every summed-sweep phase (VERIFY + both cloud groups) ride
# `program.BASE_STIMULUS_PEAK_DBFS` clamped by the driver cap, while MEASURE
# was driven until its capture peak hit `DEFAULT_TARGET_CAPTURE_DBFS`
# regardless of how quiet the room actually was. That is what the household
# hears as "measurement 2 is way louder than everything else" (owner,
# 2026-07-28). The room's noise floor — not the ADC's headroom — is what
# decides how much signal the fit needs, and CHECK already measures it.
#
# The solve's own insurance on top of a band's SNR requirement. The ambient
# evidence is CHECK's 12 s window, measured up to a minute before MEASURE
# plays and (per `program`'s module docstring) deliberately taken BEFORE the
# courtesy beeps ask the household to quiet down; `k` comes from 0.8 s pilots
# rather than from the sweep itself. 6 dB is the same figure
# `jasper.audio_measurement.level_solver.SOLVER_MARGIN_DB` carries for the
# same job in the ramp-driven solver. Deliberately NOT imported from there:
# the two solvers are independent, and sharing the symbol would let a tuning
# change to one silently retune the other.
MEASURE_SNR_SOLVE_MARGIN_DB = 6.0

# Peak-to-RMS of the excitation itself. Every stimulus segment this analysis
# reasons about — pilots included — is rendered by `program.segment_stimulus`
# as a constant-amplitude synchronized swept sine, so its peak sits exactly
# 10*log10(2) dB above its own full-band RMS. Measured at 3.02-3.03 dB on both
# real MEASURE sweeps (see `test_sweep_band_crest_factor_matches_the_rendered_sweep`).
SWEEP_PEAK_TO_RMS_DB = 3.0103

# --------------------------------------------------------------------------- #
# D6 (#1838): the demand and the budget must be in the same units
# --------------------------------------------------------------------------- #


def sweep_band_crest_factor_db(
    sweep_hz: tuple[float, float], band_hz: tuple[float, float]
) -> float:
    """dB from a swept sine's PEAK down to its RMS inside ``band_hz``.

    The MEASURE level solve budgets a capture *peak*
    (``MeasurementPriors.target_capture_dbfs``, and ``k_db`` measured from
    pilot peaks) but its SNR demand is stated against an ambient *band RMS*.
    Until issue #1838 the two were added directly, which under-drove the
    sweep by exactly this quantity: the capture peak landed where the demand
    asked, and the in-band signal RMS — the thing that actually has to clear
    the room — landed this far below it.

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

    **Named residual — row width, erring LOUD.** The ambient rows this is
    applied to (``snr_policy.CROSSOVER_SNR_BANDS_HZ``) can be wider than the
    slice of them the sweep covers: a woofer swept from 150 Hz clips the
    80-160 Hz ``bass`` row to 10 Hz. The crest is then computed over the
    10 Hz slice (correct — that is where the sweep's energy is) while the
    ambient level is still the whole row's (the room's noise across all
    80 Hz). That over-states the demand, i.e. keeps MEASURE LOUDER, which is
    the same direction the row-width coarseness already documented in
    :func:`_solve_role_gain` errs, and the safe one: this solve can only
    make MEASURE quieter than the level that has always shipped. Restricting
    the ambient level to the covered slice under a flat-noise assumption is
    the obvious refinement; it errs QUIET, so it wants bench data first.
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
# the math proposed a sweep too faint to trust. Issue #1838 — see
# `_solve_role_gain`. A disclosed refusal to solve, never a shipped level.
GAIN_BOUND_DEGENERATE_AMBIENT = "degenerate_ambient"
GAIN_BOUNDS = frozenset({
    GAIN_BOUND_FLAT_TARGET,
    GAIN_BOUND_ROOM_SNR,
    GAIN_BOUND_PILOT_SNR,
    GAIN_BOUND_CAPTURE_FLOOR,
    GAIN_BOUND_NO_AMBIENT_EVIDENCE,
    GAIN_BOUND_DEGENERATE_AMBIENT,
})

# Behavioral linearity tolerance (design §3.4): captured delta within this of
# the programmed delta. Measured band-relative + ambient-compensated (see
# `_pilot_observations`) since the 2026-07-20 fix — a full-band PEAK estimate
# (the pre-fix method) let LF room rumble ~30 dB above the tweeter-band
# ambient inflate the quiet woofer pilot's peak and compress the captured
# delta, tripping this tight a tolerance on a perfectly linear driver (the
# same bug class the channel-map discriminator was fixed for in #1594 —
# gotcha #6/#16 in docs/HANDOFF-crossover-measurement-v2.md).
LINEARITY_TOLERANCE_DB = 0.5

# Pilot edge-fade trim: `sweep.synchronized_swept_sine` applies a fixed 5 ms
# fade-in/fade-out to every stimulus it generates (its own "Light fade-in/out"
# comment) to avoid a click at a non-zero-crossing edge — a located pilot
# segment therefore ramps up/down over that fixed span rather than playing at
# full level throughout. Trimming exactly that span from each edge before
# measuring level keeps the RMS estimate to the pilot's steady-state portion.
# This is the composer's REAL fade length (read from the generator, not
# guessed), so no separate justification for "why 5 ms" is needed here.
PILOT_FADE_TRIM_S = 0.005

# Low-SNR honest routing (design note above `_pilot_observations`): ambient
# power subtraction only removes the room's noise-floor BIAS when the quiet
# (lo) pilot's own in-band power clears the in-band ambient power by enough
# margin that residual bias from ambient NONSTATIONARITY — the room's true
# noise power during the ~0.8 s pilot window can differ from the value
# measured over the program's separate, earlier ambient window — stays a small
# fraction of `LINEARITY_TOLERANCE_DB`. Modeling that mismatch as a bounded
# multiplicative factor ``k = 10**(AMBIENT_NONSTATIONARITY_DB/10)`` on the
# ambient power estimate:
#
#   subtracted signal estimate      Ŝ = P_measured − N̂
#   bias if the room's ACTUAL noise power during the pilot window is k·N̂
#   instead of N̂:                   bias_power = (k − 1) · N̂
#   bias in dB at signal level S (small-signal slope 10/ln(10)/S):
#       bias_db ≈ (10 / ln(10)) · (k − 1) / (S / N̂)      [S/N̂ = linear SNR]
#
# Budgeting `LINEARITY_SNR_BIAS_BUDGET_FRACTION` of the tolerance for this
# bias (leaving the rest for ordinary estimator/measurement jitter) and
# solving for the linear SNR gives the minimum trustworthy in-band SNR —
# `PILOT_MIN_SNR_DB` works out to ≈12.4 dB with the constants below. Real
# hardware captures that tripped this bug (2026-07-20, jts3) measured ≈26-30
# dB of in-band SNR on the quiet woofer pilot once measured in its own band
# (comfortably above this floor — routed as VALID, not `snr_floor`); this
# threshold exists for the genuinely marginal case (very quiet phone/room),
# not the common one.
AMBIENT_NONSTATIONARITY_DB = 3.0
LINEARITY_SNR_BIAS_BUDGET_FRACTION = 0.5
_pilot_snr_k = 10.0 ** (AMBIENT_NONSTATIONARITY_DB / 10.0)
_pilot_snr_linear_min = (10.0 / math.log(10.0)) * (_pilot_snr_k - 1.0) / (
    LINEARITY_TOLERANCE_DB * LINEARITY_SNR_BIAS_BUDGET_FRACTION
)
PILOT_MIN_SNR_DB = 10.0 * math.log10(_pilot_snr_linear_min)

# Channel-map discriminator (Fix 1, W6.4 — see `_channel_map_ok`). PROVISIONAL
# pending more W6 hardware runs. Derived from the run-5 hardware table
# (2026-07-18/19, jts3): woofer pilots showed +22-30 dB TARGET rise / +<=2 dB
# CROSS rise, tweeter pilots +27 dB TARGET rise / +1.9 dB CROSS rise — both
# comfortably clear of these thresholds even though concurrent room LF rumble
# had put the tweeter pilot's TOTAL in-band energy fraction (the pre-fix test)
# at a coin flip (-51.8 dBFS in-band vs -51.1 dBFS of simultaneous woofer-band
# room noise, against a -78.9 dBFS ambient floor — 27 dB of real, ignored SNR).
CHANNEL_MAP_TARGET_RISE_DB = 12.0
CHANNEL_MAP_CROSS_RISE_DB = 6.0

# VERIFY tracking-error smoothing: 1/6-octave, the constant design §5.2 names
# for the pass/fail comparison (previously 1/24-oct, a display-grade
# smoothing far finer than the design spec).
VERIFY_TRACKING_SMOOTHING_FRACTION = 6

# VERIFY tracking MAX comparator (W6.7 ruling 1): a bin is excluded from the
# max-tracking comparator when the PREDICTED sum sits more than this many dB
# below its own median level over the tracking band. Inside a predicted
# interference notch, depth agreement is hypersensitive to sub-dB/sub-degree
# branch differences and is not a meaningful tracking signal — the W6 run-7
# hardware failure (3.05 dB rms / 27.83 dB max) was entirely a shifted
# predicted notch, not a broadband divergence. RMS stays full-band (it
# already behaves sanely — see `_analyze_verify`). PROVISIONAL pending W6
# bench distributions on notch depth/shift variability.
VERIFY_NOTCH_EXCLUSION_DB = 12.0

# Flatness-verify (#1668 PR-D) lived HERE until the flat-linearization plan's
# PR-5 (the spec-curve SSOT). `_flatness_tracking`, `FLATNESS_VERIFY_HI_HZ`
# (16 kHz) and `FLATNESS_VERIFY_TOLERANCE_DB` (3.0, PROVISIONAL) are gone: they
# were a SECOND construction of "how flat is the speaker" — one VERIFY
# capture's own grid, graded against its own band mean, with no interference
# exclusion — sitting alongside the spatial cloud's spec evaluation and
# disagreeing with it by however much a single mic position differs from the
# cloud. That disagreement is the MEASURE-vs-VERIFY ledger-discrepancy class
# the plan's "S0 executed" § c documents, and PR-5 kills it by giving the
# claim exactly one owner: `jasper.active_speaker.flat_spec`
# (`evaluate_flat_spec` + `spec_flatness_gauge`), evaluated on
# `spatial_combine.combine_positions`' power-mean spec curve with the merged
# honesty mask, wired by
# `jasper.active_speaker.crossover_v2_flow.assemble_cloud_group_result`. The
# 16 kHz upper edge survives as `flat_spec.BEST_EFFORT_ABOVE_HZ`; the
# never-bench-derived 3.0 dB tolerance is replaced by the spec table's own
# per-band tolerances (`flat_spec.SPEC_BANDS`).
#
# Integration-verify (`verify_tracking`, below) is NOT affected and stays a
# distinct construction on purpose — see `_analyze_verify`'s own comment on
# interpretation call (B).
#
# Naming note kept from #1668 PR-D: do not name anything here bare "flatness"
# — `CrossoverCandidate.flatness_improvement_db` is an UNRELATED Layer-1b
# metric (anchor-vs-selected-delay ripple improvement), not a spec claim.

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
        OTHER driver (the woofer, ``d = driver_spacing_m`` off-axis) carries
        the full geometric excess ``√(r²+d²) − r``. That excess inflates the
        measured woofer-minus-tweeter arrival difference; subtracting it
        leaves the electrical branch delay. A mic placed off the tweeter axis
        splits the excess between the drivers and this correction over-counts
        — the placement screen owns keeping that assumption true.
        """
        r = float(self.mic_distance_m)
        d = float(self.driver_spacing_m)
        c = float(self.speed_of_sound_m_s)
        if r <= 0 or c <= 0 or d <= 0:
            return 0.0
        extra_m = math.sqrt(r * r + d * d) - r
        return extra_m / c * 1e6


@dataclass(frozen=True)
class MeasurementPriors:
    """Per-analysis priors the program itself does not carry.

    ``crossover_fc_hz`` scopes the overlap band (trims / alignment / ripple) and
    the VERIFY window; ``align_search_ms`` bounds the delay search;
    ``target_capture_dbfs`` is the MEASURE capture-peak target the CHECK gain
    solve aims for. ``predicted_sum`` is the MEASURE-predicted summed magnitude
    ``(freqs_hz, magnitude_db)`` VERIFY compares against — built from the RAW
    measured branches by this module's own ``_build_candidate``, but the v2
    conductor OVERRIDES it with a LINEARIZED-branch prediction whenever Layer-1a
    linearization was fitted (#1668 PR-D VERIFY-prediction coherence fix; see
    ``jasper.active_speaker.crossover_v2_flow.CrossoverV2Conductor._fit_linearization``)
    — the emitted graph carries the correction filters, so the persisted
    prediction must model them too, or VERIFY's tracking comparison reads a
    deterministic mismatch equal to the filters' own in-band response (measured
    live on JTS3: ~1.7 dB against the ±1.5 dB tolerance).

    ``measure_tweeter_sweep_lo_hz``/``measure_woofer_sweep_hi_hz`` carry the
    MEASURE program's actual per-driver sweep bounds forward to VERIFY (§5.6
    fix) — ``predicted_sum`` was itself built only inside that true overlap
    (see ``overlap_band_hz``), so VERIFY's tracking comparison must trust the
    SAME band; a wider nominal Fc±1-octave band would compare real VERIFY
    capture data against sub-floor noise inherited from an unexcited MEASURE
    branch. ``None`` (legacy callers) falls back to the unclamped nominal band.

    ``alignment_delay_bounds_us`` is the unsigned, declaration-derived
    applied-delay magnitude range the flatness refinement may search. The
    conductor derives it from the crossover region's ``delay_range_ms``; the
    drift-corrected physical peak gap orients and centers one ±half-period
    signed lobe inside it. GCC remains the confidence/polarity seed and the
    fallback estimate. ``None`` keeps GCC as the applied-delay estimate.

    ``mic_tier`` (#1668 PR-C) is the correction-envelope trust tier
    (``jasper.active_speaker.linearization_envelope.MIC_TIERS`` — "reference"
    / "consumer" / "phone") the measurement mic resolved to, threaded in by
    ``jasper.web.correction_crossover_v2.bind_production_analyze`` via
    ``jasper.audio_measurement.calibration.mic_tier_for_model``. ``None``
    (every construction site that predates this field, and CHECK/VERIFY
    priors, which never set it) means "no tier known" — the v2 conductor's
    Layer-1a linearization gate treats that as ineligible, never a guess.
    """

    crossover_fc_hz: float | None = None
    align_search_ms: float = DEFAULT_ALIGN_SEARCH_MS
    target_capture_dbfs: float = DEFAULT_TARGET_CAPTURE_DBFS
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    ambient_report: Mapping[str, Any] | None = None
    measure_tweeter_sweep_lo_hz: float | None = None
    measure_woofer_sweep_hi_hz: float | None = None
    alignment_delay_bounds_us: tuple[float, float] | None = None
    mic_tier: str | None = None


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
    ``not_evaluated`` and says why the check could not run — that clause is
    the whole reason this type exists rather than a ``dict[str, bool]``, in
    which "did not run" and "ran and passed" are the same value.
    """

    name: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class CaptureIntegrity:
    """What a VERIFY capture's own timeline says about whether it is usable.

    The evidence half of the record (``locate_confidence_min``,
    ``schedule_residual_ms_worst``, ``clipped_segments``) is reported whether
    or not the check drawn from it ran, because a measured figure is worth
    disclosing even where it is not worth a verdict: a summed sweep the
    locator could barely find still HAS a residual, and printing it beside a
    ``not_evaluated`` schedule check is what stops a reader inferring a splice
    from a number that is really just noise (the #1838 / D3 lesson, applied to
    VERIFY).

    ``failed`` / ``not_evaluated`` / ``glitched`` are derived from ``checks``
    rather than stored, so the summary can never disagree with the checks it
    summarizes. ``checks`` is ordered most-fundamental-first, which is also
    the order a consumer should route on: a sweep nobody heard explains its
    own residual, so "not heard" outranks "off schedule".

    ``None`` where a :class:`ProgramAnalysis` carries no record at all means
    "no evidence" — the same convention ``linearity_ok`` / ``pilot_snr_ok``
    already use — and never "clean". Every VERIFY analysis
    ``analyze_program_capture`` produces carries one (pinned by test).
    """

    checks: tuple[IntegrityCheck, ...] = ()
    locate_confidence_min: float | None = None
    # SIGNED (mirrors ``crossover_v2_flow._sweep_schedule_diag_fields``): the
    # direction the schedule broke in is half the forensic value — positive
    # means the sweep arrived LATE, the 2026-07-27 insertion shape.
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
    disagreement (see ``_estimate_drift``'s docstring) — one of the three
    glitch inputs, threaded through here (not just logged transiently at
    ``program_analysis.glitch``) so a caller building a durable diagnostic
    record (e.g. the crossover v2 conductor's per-capture diag event) has it
    on BOTH a passing and a failing capture, not only the WARN-level line a
    glitch fires. Defaults to ``0.0`` for legacy construction sites that
    predate this field.

    ``per_role_epsilon_ppm`` (sweep-composition PR-A, #1668) is a first-vs-LAST
    epsilon estimate for EVERY role with ≥2 located sweep occurrences —
    diagnostic only, never gated (only the woofer pair above decides
    ``glitch_detected``, unchanged). Free evidence for a future PR's deeper
    per-role drift hardening (G2). Empty for a role with <2 occurrences (an
    old-shaped program's un-repeated tweeter, or any degenerate single-sweep
    role) and for legacy construction sites that predate this field.

    ``glitch_inputs`` (issue #1765) names WHICH of the three bounds tripped —
    ``epsilon_out_of_bound`` / ``residual_desync`` / ``repeat_level_disagree``
    — in that fixed order, empty on a clean capture. The verdict is one
    user-facing reason by design (§5.2's "never a new user-facing code for a
    capture-glitch class"), but telemetry had no way to tell the three apart,
    and the drift-flavoured name misled readers into assuming the ppm bound
    fired when it never has (see DISCONTINUITY_MIN_SAMPLES).

    ``discontinuity_samples`` / ``discontinuity_after_segment`` describe a
    single discrete timeline step when one explains the located sweeps: its
    signed size in samples (positive ⇒ everything after it arrived LATE, the
    2026-07-27 shape) and the segment id it landed AFTER. ``0.0`` / ``""``
    when no step is resolved on a capture whose sweeps were confidently
    located — including on a clean capture, where this is the expected
    value. ``DISCONTINUITY_UNRESOLVED`` (a `str`) / ``""`` (#1839) when one
    or more located sweeps fell below ``SWEEP_LOCATE_CONFIDENCE_FLOOR``
    instead: a step fitted from an unlocated sweep is not a clean reading, it
    is a fabrication, so it is a distinct sentinel rather than silently
    ``0.0``. Diagnostic only; never gates ``glitch_detected``.
    """

    epsilon_ppm: float
    baselines_ppm: Mapping[str, float]
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

    ``repeat_responses`` (sweep-composition PR-A, #1668) holds this SAME
    driver's additional located sweep occurrences — each independently
    deconvolved/gated/transformed exactly like the primary — in occurrence
    order, ``repeat_index`` 1, 2, …. Only ever populated on a PRIMARY
    response (the tuple this dataclass's other fields describe, built from
    the driver's first/canonical sweep — ``sweep_w``/``sweep_t``); a repeat's
    own ``DriverResponse`` carries an empty ``repeat_responses`` and its own
    ``repeat_index``. Diagnostic evidence only — nothing here feeds the
    candidate/trim/alignment math, which stays anchored to the primary
    response exactly as before. Defaults keep every pre-existing
    construction site valid.
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

    Sign convention (pinned by test): ``delay_us`` is
    ``(D_woofer − D_tweeter)`` after parallax removal, so **positive delay_us ⇒
    the tweeter's acoustic arrival is EARLIER and the tweeter branch must be
    delayed by that amount** to time-align the crossover.

    T2 keeps GCC as the capture-quality seed: ``seed_delay_us`` records that
    corrected delay, while ``delay_us`` becomes the anchor-primary,
    gated-local-peak-snapped selection. ``confidence`` therefore remains
    explicitly ``confidence_source='gcc_phat_seed'``; the selection's own
    ripple/snap evidence is stored separately on :class:`CrossoverCandidate`.
    After refinement, ``raw_delay_us`` is the selected delay in the
    pre-parallax coordinate, preserving
    ``delay_us == raw_delay_us - parallax_us``; it is not the discarded GCC
    raw peak, whose corrected form remains available as ``seed_delay_us``.

    ``anchor_delay_us`` is the drift-corrected physical peak-gap anchor (raw
    full-IR argmax gap − inter-sweep drift + parallax) in the signed frame — the
    aligner computes it once and OWNS it, so ``_build_candidate`` derives the
    applied anchor and the objective reference gap from it rather than
    re-running the argmax (a parallel computation of one load-bearing decision).
    ``snapped_delay_us`` is the fine-stage result: that anchor snapped to the
    nearest local maximum of the SAME upsampled GCC-PHAT correlation within
    ±(period/6) at Fc (:data:`GCC_SNAP_RADIUS_PERIODS`). ``snapped_delay_us`` is
    ``None`` when the radius held no local maximum (the candidate then keeps the
    bare anchor); both are ``None`` when the seed was refused. They are plumbing
    from the aligner — which owns the correlation and the anchor — to
    :func:`_build_candidate`, which owns the final selection; direct
    ``_build_candidate`` callers set ``anchor_delay_us`` explicitly.

    ``status`` is :data:`ALIGNMENT_OK` for a trustworthy estimate. When the
    correlation peak lands at (or within one sample of) the ±search-window
    edge, the true delay likely exceeds the geometry prior and the windowed
    peak is a clamped artifact — ``status`` is
    :data:`ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW` and ``confidence`` is forced
    to 0.0; callers must not apply ``delay_us`` from such a result.
    """

    delay_us: float
    raw_delay_us: float
    parallax_us: float
    polarity: str  # "normal" | "inverted"
    polarity_sign: int  # +1 | -1
    polarity_agrees_with_sum: bool
    confidence: float
    status: str = ALIGNMENT_OK
    seed_delay_us: float | None = None
    confidence_source: str = "gcc_phat"
    anchor_delay_us: float | None = None
    snapped_delay_us: float | None = None


@dataclass(frozen=True)
class CrossoverCandidate:
    """The proposed measured candidate (design §5.6.6).

    Delay selection is anchor-primary (the drift-corrected physical peak gap)
    with a gated local-peak snap; summed-magnitude flatness is demoted to
    evidence and never chooses the applied delay (methodology:
    docs/crossover-measurement-reproducibility-plan.md §10, 2026-07-22).
    ``anchor_delay_us`` is the bare anchor in the signed candidate convention,
    ``snap_delta_us`` is ``selected − anchor`` (0.0 when the snap was not taken),
    and ``snap_found`` records whether a local correlation peak existed inside
    the snap radius. ``alignment_seed_ripple_db`` is the summed ripple evaluated
    AT the anchor and ``flatness_improvement_db`` is ``anchor_ripple −
    selected_ripple`` — evidence only, so a slightly negative value is honest
    (the snap is chosen for comb-lobe correctness, not ripple). Since
    ``snap_delta_us`` is ``selected − anchor``, it is also exactly the residual
    delay the model carries on this path
    (:func:`summed_model_residual_delay_us`) — one number, not two.

    ``predicted_ripple_db`` is measured on the INDEPENDENTLY ALIGNED
    (zero-residual) branch sum, and is the one quantity here that deliberately
    did NOT follow ``ProgramAnalysis.predicted_sum`` onto the committed-delay
    model at rung P3 / R10b. It asks a capture-quality question — how
    coherently can these two branches sum at all — which is a property of the
    measurement and not of the delay selection, and it is the sole input to
    ``crossover_v2_flow``'s G1 ``MEASURE_PREDICTED_RIPPLE_CEILING_DB``, whose
    threshold that constant documents as calibrated against a fixed hardware
    corpus scored on THIS metric. Moving it onto the delay-carrying curve would
    let a candidate's own alignment lower its own veto number — see
    ``_build_candidate``'s comment at the two calls for the measured evasion
    margin. The two ripples that DO carry the residual are the snap-evidence
    pair above, which is what makes them a comparison.

    ``trim_db`` is the APPLIED trim (#1667: ripple-optimal where the polish
    ran and the sanity guard trusts it, otherwise the band-average fallback);
    ``trim_band_average_db`` preserves ``solve_branch_trims``'s own
    level-match result — the SEED the ripple-optimal search started from — so
    replay/forensics can always see both, even when they coincide. ``None``
    only for a legacy/test construction site built before this field existed;
    ``_build_candidate`` always sets it.

    Frame note (PR-L3, 2026-07-27): both values changed meaning. Before, the
    level match band-averaged BOTH branches over the shared overlap band,
    which on a tweeter-swept-from-Fc speaker measured the woofer's crossover
    skirt and put the tweeter trim 10.9-13.1 dB too negative on the archived
    JTS3 captures. A candidate persisted before that fix carries the old
    frame; it is evidence, not config, and is not migrated — a speaker
    commissioned under it keeps its old trim until re-commissioned.
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


@dataclass(frozen=True)
class PilotObservation:
    """One driver's CHECK pilot pair — level, linearity, channel-map sanity.

    ``level_lo_dbfs``/``level_hi_dbfs`` are band-relative, ambient-compensated
    (when an ambient window is available) — see `_pilot_observations`. They
    feed ONLY the linearity verdict (``captured_delta_db`` is a relative
    delta, so the ambient-subtraction bias cancels between the two levels);
    they must never feed an ABSOLUTE-level consumer like the MEASURE gain
    solve (`_solve_gain_plan`) — ambient subtraction shifts the absolute
    value by however much ambient power was removed, which silently retunes
    a consumer that expects a true signal-peak reference (a review finding:
    threading these into the gain solve moved its captured-peak target
    13-17 dB hotter on the two real captures). ``peak_lo_dbfs``/
    ``peak_hi_dbfs`` are the dedicated, NON-ambient-subtracted levels
    `_solve_gain_plan` reads instead — the exact pre-existing full-band
    `_peak_dbfs`, kept verbatim (see `_pilot_observations`'s docstring for
    why an in-band variant was tried and rejected), preserving
    ``MeasurementPriors.target_capture_dbfs``'s documented capture-PEAK
    semantics exactly as before.

    ``snr_valid`` is True when the quiet (lo) pilot's in-band SNR clears
    `PILOT_MIN_SNR_DB`, i.e. the ambient-subtracted estimate (and therefore
    ``linearity_ok``) is trustworthy; when False, ``linearity_ok`` is
    ``None`` — UNKNOWN. An untrustworthy estimate must never register as a
    linearity FAILURE (the caller routes on ``snr_valid`` instead, honestly
    attributing the room/positioning cause rather than the phone's AGC), and
    since issue #1838 it must not register as a PASS either: it was forced
    ``True`` until then, which is how a capture with a -60.9 dB captured
    delta against a programmed 10.0 dB published ``linearity_ok=true``.
    ``snr_valid`` defaults to True so a caller constructing one directly
    (fixtures, legacy call sites) without an opinion on SNR gets the
    "trust the delta" behavior.

    ``snr_db`` is the actual quiet-pilot in-band SNR estimate ``snr_valid``
    is thresholded from (`_pilot_in_band_snr_db`) — kept as a number, not
    just the pass/fail bool, so a diagnostic consumer (the per-phase diag log
    events) can see how close a borderline capture ran. ``+inf`` when there
    is no ambient window to validate against (nothing to distrust — see
    `_pilot_in_band_snr_db`), matching ``snr_valid``'s default-True stance.
    Until issue #1810 (2026-07-28) that ``+inf`` was the ONLY value MEASURE
    and VERIFY could produce, because their programs carried no ambient
    window at all — so the guard was structurally dead on both phases.

    ``channel_map_target_rise_db``/``channel_map_cross_rise_db`` are the two
    rise numbers `_channel_map_ok` computed on the way to ``channel_map_ok``
    (this driver's own band above ambient, and the worst/failing other
    band's rise above ITS ambient) — diagnostic only, ``None`` on the
    fallback total-energy-fraction path (which has no rise concept, and is
    what v2 MEASURE/VERIFY take — see `_pilot_observations`) or, for the
    cross figure, when there are no other roles to compare against.

    ``programmed_hi_gain_db`` is the HI segment's own declared ``gain_db``
    (the digital gain the program composer scheduled it at) — published
    here so a caller downstream of this analysis (the v2 conductor's VERIFY
    inter-attempt pilot-level consistency gate, measurement-honesty gate G3,
    2026-07-22) can compute ``level_hi_dbfs - programmed_hi_gain_db`` (the
    capture chain's own transfer) WITHOUT binding back to the
    ``ExcitationProgram`` instance that produced this analysis — the SSOT
    stays "the analysis publishes the gain it measured against". ``None``
    for a legacy construction site that predates this field (fixtures,
    call sites built before this field existed) — a consumer must treat
    that as "nothing to compare", never as ``0.0``.
    """

    role: str
    level_lo_dbfs: float
    level_hi_dbfs: float
    programmed_delta_db: float
    captured_delta_db: float
    linearity_ok: bool | None
    channel_map_ok: bool
    snr_valid: bool = True
    peak_lo_dbfs: float = DBFS_FLOOR
    peak_hi_dbfs: float = DBFS_FLOOR
    snr_db: float = math.inf
    channel_map_target_rise_db: float | None = None
    channel_map_cross_rise_db: float | None = None
    programmed_hi_gain_db: float | None = None


@dataclass(frozen=True)
class RoleGainSolve:
    """One driver's MEASURE level solve, and the evidence it rests on (#1825).

    ``gain_db`` is the digital gain the MEASURE composer will actually
    schedule for this role; ``flat_target_gain_db`` is what the pre-#1825
    solve would have scheduled (land the capture peak on
    ``MeasurementPriors.target_capture_dbfs``, clamped by the ≥6 dB digital
    guard). The solve never exceeds that flat figure, so this pair is also
    the disclosure of how much quieter this session's MEASURE got and why.

    ``bound_by`` names which limit chose the number — one of the
    ``GAIN_BOUND_*`` constants. It is the honesty field: a
    ``GAIN_BOUND_NO_AMBIENT_EVIDENCE`` solve is the disclosed fallback to the
    flat target (never a silent guess), and ``GAIN_BOUND_FLAT_TARGET`` means
    the room was noisy enough that the SNR requirement wanted at least the
    flat level, so nothing moved.

    ``ambient_dbfs`` / ``required_snr_db`` / ``crest_factor_db`` /
    ``required_capture_dbfs`` are the ROOM-SNR demand, and only that: the
    worst overlapping ambient band's level, the SNR this solve demanded above
    it, the stimulus crest factor that converts that band-RMS demand into the
    capture-PEAK units everything else here is expressed in (D6, issue
    #1838 — see :func:`sweep_band_crest_factor_db`), and their sum. They are a
    coherent quadruple (the last is the first three added) and they stay the
    room demand even when a DIFFERENT arm won — read ``bound_by`` for which
    one did, and ``gain_db`` for the level actually scheduled. When
    ``bound_by`` is ``GAIN_BOUND_PILOT_SNR`` or
    ``GAIN_BOUND_DEGENERATE_AMBIENT``, ``required_capture_dbfs`` is therefore
    the room demand that arm overrode, not the capture peak aimed at. All four
    are ``None`` on the no-evidence fallback — a missing number is never a
    zero. ``crest_factor_db`` is additionally ``None`` on a solve persisted
    before #1838, where the demand carried no crest term at all.
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

    ``role_solves`` (#1825) carries the per-role derivation behind
    ``gain_db`` — see :class:`RoleGainSolve`. Empty for a construction site
    that predates the field (fixtures, legacy callers); a consumer must read
    that as "no derivation published", never as "no reduction happened".
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
    # Pure passthrough of MeasurementPriors.mic_tier (#1668 PR-C) — set only
    # by _analyze_measure (see that function's return statement); CHECK and
    # VERIFY analyses never set it (the v2 conductor's Layer-1a fit only
    # ever reads it off a MEASURE analysis). See MeasurementPriors.mic_tier's
    # docstring for the trust-tier vocabulary and "None means unknown, never
    # a guess" contract.
    mic_tier: str | None = None
    pilots: tuple[PilotObservation, ...] = ()
    linearity_ok: bool | None = None
    channel_map_ok: bool | None = None
    # Aggregate of ``PilotObservation.snr_valid`` across pilots (``all(...)``);
    # ``None`` when there are no pilots (same "no evidence" convention as
    # ``linearity_ok``). False means at least one pilot's quiet-side in-band
    # SNR was too low to trust the ambient-subtracted linearity estimate —
    # the conductor routes this to `REASON_SNR_FLOOR` (CHECK) or
    # `REASON_PILOT_LEVEL_COLLAPSE` (MEASURE / cloud / VERIFY), never to
    # `REASON_AGC_BEHAVIORAL_FAIL`. Live on every phase since issue #1810.
    pilot_snr_ok: bool | None = None
    gain_plan: GainPlan | None = None
    summed_response: DriverResponse | None = None
    summed_ripple_db: float | None = None
    # Measured-vs-predicted scalars for one VERIFY capture, plus — since rung
    # P1 — the ``"frame"`` the two curves were compared ACROSS and the
    # tilt-removed twins of the two numbers a gate or a screen reads. The raw
    # scalars keep their meaning and their value exactly; see
    # :func:`_analyze_verify`'s frame-discipline block for why the tilt is
    # disclosed rather than corrected for.
    verify_tracking: dict[str, Any] | None = None
    # The SMOOTHED ``(freqs_hz, measured_db, predicted_db)`` triple the
    # tracking scalars above were reduced from (linearization-integrity PR-L5).
    # A separate field rather than a key inside ``verify_tracking`` because
    # that dict travels to the phone in a PhaseVerdict payload and these are
    # full curves.
    #
    # It exists so the delta probe
    # (:mod:`jasper.active_speaker.delta_probe`) grades the SAME comparison the
    # tracking gate does — one measured-vs-predicted construction, two
    # consumers reading it over different bands — rather than re-deriving its
    # own from the raw curves. Two comparators of one quantity is the drift
    # shape the linearization-integrity ladder exists to remove.
    #
    # ``None`` whenever ``verify_tracking`` is (no prediction prior), and the
    # probe reads that as "no evidence", never as a pass.
    verify_tracking_curve: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    # (``flatness_tracking`` lived here until the flat-linearization plan's
    # PR-5 — see the retired-constants comment near ANALYSIS_KIND. A single
    # capture cannot answer "is the speaker flat"; the spatial cloud's spec
    # evaluation does, and it is the only owner of that claim now.)
    # MEASURE-predicted summed magnitude ``(freqs_hz, magnitude_db)`` — the two
    # measured branches at the candidate's COMMITTED trim AND committed delay
    # (rung P3 / R10b; ``_build_candidate``'s ``predicted_applied``). The v2
    # conductor hands this to the VERIFY analysis as
    # ``MeasurementPriors.predicted_sum`` so VERIFY's PASS is |measured −
    # predicted| ≤ ±1.5 dB (design §5.2), not merely the summed ripple.
    #
    # It carries the delay since R10b, so the tracking comparison grades model
    # FIDELITY — did the emitted graph do what was modelled — against a target
    # a realizable delay actually produces. Before that it was the
    # zero-residual "flattest-achievable, independently aligned" sum of design
    # §5.6.6, which no delay selection realizes; §5.6.6's intent is
    # deliberately superseded. Quality is graded separately and elsewhere:
    # ``crossover_v2_flow.spec_report_for_predicted_sum`` against the flat
    # spec, and ``CrossoverCandidate.predicted_ripple_db`` (still the
    # independently-aligned instrument) at the G1 capture-quality ceiling.
    predicted_sum: tuple[np.ndarray, np.ndarray] | None = None
    # Set by MEASURE from ``drift.glitch_detected`` and by VERIFY from
    # ``capture_integrity.glitched`` (issue #1971) — in BOTH cases a one-bit
    # projection of a richer record that is the owner of the fact, assigned at
    # the single construction site so the two can never disagree (pinned by
    # test). It was structurally ``False`` on every VERIFY analysis before
    # #1971, because the only thing that could set it ran on MEASURE alone.
    glitch_detected: bool = False
    # The per-check VERIFY capture-integrity record ``glitch_detected``
    # summarizes — including the checks that could NOT run here and why. Set
    # by ``_analyze_verify`` on every VERIFY-phase analysis (which is also
    # every spatial-cloud position, since those replay the verify program).
    # ``None`` on CHECK/MEASURE and on analyses built before this field
    # existed: "no evidence", never "clean" — see :class:`CaptureIntegrity`.
    capture_integrity: CaptureIntegrity | None = None


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
    """Delegates to :func:`gating.analytic_envelope` — one implementation.

    R9's gate needs the same ETC envelope this module's correlation
    refinement does. Rather than let a second copy exist, the lower-level
    module owns it and this stays a name local callers already use.
    """
    return gating.analytic_envelope(x)


def _parabolic_peak(values: np.ndarray, idx: int) -> float:
    """Sub-sample offset of a peak at integer ``idx`` via 3-point parabola.

    The refinement is clamped to ±1 bin: a true local maximum refines within
    ±0.5 bin, so a larger offset means the three points are near-degenerate
    (tiny ``denom``) and the parabola vertex is an extrapolation artifact —
    unclamped, a flat-topped correlation once "refined" a 96-bounded peak out
    to 128 samples. In that case the integer peak is the honest answer.
    """
    if idx <= 0 or idx >= values.size - 1:
        return float(idx)
    y0, y1, y2 = float(values[idx - 1]), float(values[idx]), float(values[idx + 1])
    denom = y0 - 2.0 * y1 + y2
    if denom == 0.0:
        return float(idx)
    offset = 0.5 * (y0 - y2) / denom
    if not -1.0 <= offset <= 1.0:
        return float(idx)
    return idx + offset


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
    refined = _parabolic_peak(env, peak)
    lag = refined - (n - 1)  # b ≈ a shifted right by lag
    return float((arrival_b - arrival_a) + lag)


def _bandlimit(ir: np.ndarray, sample_rate: int, lo_hz: float, hi_hz: float) -> np.ndarray:
    """Zero-phase band-pass an IR by masking its spectrum to ``[lo, hi]``."""
    n = ir.size
    spectrum = np.fft.rfft(ir)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    spectrum = spectrum * mask
    return np.fft.irfft(spectrum, n=n)


def _gcc_correlation(
    a: np.ndarray,
    b: np.ndarray,
    *,
    sample_rate: int,
    band_hz: tuple[float, float],
    upsample: int,
) -> tuple[np.ndarray, int]:
    """Band-limited GCC-PHAT cross-correlation of ``a`` vs ``b``, ×``upsample``
    FFT-interpolated.

    Returns ``(cc, m)``: ``cc`` is the length-``m`` upsampled real
    cross-correlation on the circular-lag axis (index ``i`` → lag ``i`` for
    ``i <= m/2`` else ``i - m``; native lag = index / ``upsample``). The
    cross-power is phase-transform weighted **only inside ``band_hz``**
    (whitening the near-zero out-of-band bins otherwise piles a spurious peak
    near zero lag). Shared core of :func:`_gcc_phat` (global-peak seed) and
    :func:`_gcc_local_peak_snap` (anchor-gated fine snap), so both read one
    correlation formula rather than two that could silently drift apart.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    L = max(a.size, b.size)
    n = 1
    while n < 2 * L:
        n *= 2
    A = np.fft.rfft(a, n=n)
    B = np.fft.rfft(b, n=n)
    R = A * np.conj(B)
    mag = np.abs(R)
    mag[mag < 1e-12] = 1e-12
    R_phat = R / mag
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    in_band = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    R_phat = R_phat * in_band
    m = n * upsample
    cc = np.fft.irfft(R_phat, n=m) * upsample
    return cc, m


def _gcc_phat(
    a: np.ndarray,
    b: np.ndarray,
    *,
    sample_rate: int,
    band_hz: tuple[float, float],
    upsample: int,
    max_lag_samples: float,
):
    """Band-limited GCC-PHAT of ``a`` vs ``b``; ``a ≈ b`` shifted right by the lag.

    Returns ``(lag_samples, polarity_sign, confidence, at_edge)``. The
    correlation (see :func:`_gcc_correlation`) is ×``upsample`` FFT-interpolated
    and parabolically refined. ``polarity_sign`` is the sign of the (signed)
    correlation at the peak, and ``confidence`` mirrors
    ``cross_correlation_alignment``'s primary-over-secondary margin.

    ``at_edge`` is True when the peak lands within one native sample of the
    ±``max_lag_samples`` search bound — the true peak is likely OUTSIDE the
    window and the returned lag is a clamped artifact the caller must refuse.
    """
    cc, m = _gcc_correlation(
        a, b, sample_rate=sample_rate, band_hz=band_hz, upsample=upsample,
    )
    # Circular-lag axis: index i → lag i for i<=m/2 else i-m; native = /upsample.
    max_lag_up = int(round(max_lag_samples * upsample))
    max_lag_up = max(1, min(max_lag_up, m // 2 - 1))
    idxs = np.concatenate(
        [np.arange(0, max_lag_up + 1), np.arange(m - max_lag_up, m)]
    )
    window = cc[idxs]
    peak_local = int(np.argmax(np.abs(window)))
    peak_idx = int(idxs[peak_local])
    # Parabolic refine on |cc| around the (unwrapped) peak.
    abs_cc = np.abs(cc)
    refined = _parabolic_peak(abs_cc, peak_idx)
    circ = refined if refined <= m / 2 else refined - m
    lag_samples = circ / upsample
    polarity_sign = 1 if cc[peak_idx] >= 0 else -1
    primary = float(abs_cc[peak_idx])
    # Secondary: strongest competitor outside the correlation main lobe. A
    # band-limited correlation's main lobe is ~1/bandwidth wide, so a fixed
    # 1-sample exclusion would sit on the main lobe and read a near-primary
    # "secondary" (spuriously low confidence). Exclude one main-lobe half-width.
    bandwidth = max(1.0, band_hz[1] - band_hz[0])
    exclude = max(upsample, int(round(sample_rate / bandwidth * upsample)))
    masked = abs_cc[idxs].copy()
    for j, gi in enumerate(idxs):
        if abs(gi - peak_idx) <= exclude or abs(gi - peak_idx) >= m - exclude:
            masked[j] = 0.0
    secondary = float(masked.max()) if masked.size else 0.0
    confidence = max(0.0, (primary - secondary) / primary) if primary > 0 else 0.0
    max_lag_native = max_lag_up / upsample
    at_edge = abs(lag_samples) >= max_lag_native - 1.0
    return lag_samples, polarity_sign, confidence, at_edge


def _gcc_local_peak_snap(
    a: np.ndarray,
    b: np.ndarray,
    *,
    sample_rate: int,
    band_hz: tuple[float, float],
    upsample: int,
    anchor_lag_samples: float,
    radius_samples: float,
) -> float | None:
    """Snap ``anchor_lag_samples`` to the nearest local maximum of the
    band-limited GCC-PHAT correlation of ``a`` vs ``b`` within ±``radius_samples``.

    Reuses the exact upsampled phase-transform machinery of :func:`_gcc_phat`
    (via the shared :func:`_gcc_correlation` core) and the same ±1-bin
    :func:`_parabolic_peak` sub-sample refine. Returns the refined native lag of
    the nearest genuine interior local maximum of the correlation MAGNITUDE — an
    upsampled bin strictly greater than both its neighbours — whose bin lies
    within the radius of the anchor (the parabolic refine may nudge the returned
    lag by up to one upsampled bin past it); ``None`` when the radius contains no
    such peak (the caller then keeps the bare anchor). "Nearest" = smallest
    ``|lag − anchor|``.

    Ianniello's gated correlator (docs/crossover-measurement-reproducibility-plan.md
    §10, 2026-07-22): the drift-corrected physical peak-gap anchor already owns
    comb-lobe selection, so this refines it inside one λ/6 lobe instead of
    trusting the global correlation peak, which can land on a neighbouring
    stable-but-wrong comb lobe.
    """
    cc, m = _gcc_correlation(
        a, b, sample_rate=sample_rate, band_hz=band_hz, upsample=upsample,
    )
    abs_cc = np.abs(cc)
    # Search the ±radius neighbourhood in UPSAMPLED-lag units around the anchor,
    # reading the circular array modularly (upsampled lag ℓ → index ℓ % m). A
    # local maximum is an upsampled bin strictly greater than both neighbours.
    anchor_up = anchor_lag_samples * upsample
    radius_up = abs(radius_samples) * upsample
    lo = int(math.floor(anchor_up - radius_up))
    hi = int(math.ceil(anchor_up + radius_up))
    best_ell: int | None = None
    best_dist = float("inf")
    for ell in range(lo, hi + 1):
        # The integer sweep brackets the fractional radius; keep only bins
        # genuinely inside it. (The parabolic refine below can nudge the RETURNED
        # lag by at most one upsampled bin past the radius — negligible against
        # the comb-lobe spacing, so no lobe jump.)
        if abs(ell - anchor_up) > radius_up:
            continue
        idx = ell % m
        if abs_cc[idx] <= abs_cc[(idx - 1) % m] or abs_cc[idx] <= abs_cc[(idx + 1) % m]:
            continue
        dist = abs(ell - anchor_up)
        if dist < best_dist:
            best_dist = dist
            best_ell = ell
    if best_ell is None:
        return None
    refined = _parabolic_peak(abs_cc, best_ell % m)
    circ = refined if refined <= m / 2 else refined - m
    return float(circ / upsample)


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
    capture: np.ndarray, stimulus: np.ndarray, *, frac: float = 0.6
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
    """
    from scipy.signal import correlate

    cap = np.asarray(capture, dtype=np.float64)
    stim = np.asarray(stimulus, dtype=np.float64)
    cap = cap - cap.mean()
    stim = stim - stim.mean()
    L = stim.size
    if cap.size < L or L == 0:
        return 0
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


def _global_offset(
    program: ExcitationProgram, capture: np.ndarray, sample_rate: int
) -> tuple[int, ProgramSegment, dict[str, np.ndarray]]:
    """Locate the first stimulus → integer global offset G. Caches stimuli.

    The whole-capture matched filter runs at :data:`LOCATOR_RATE_HZ` (mirrors
    ``driver_acoustics._capture_to_magnitude``'s 16 kHz downsampled locate) so
    the largest correlation is over a 3× smaller array; the coarse arrival is
    then refined at the full rate inside a tiny window around it, so the
    returned offset is still full-rate-exact.
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

    down = max(1, int(round(sample_rate / LOCATOR_RATE_HZ)))
    if down > 1:
        capture_lo = resample_poly(capture, 1, down)
        stim_lo = resample_poly(np.asarray(stim, dtype=np.float64), 1, down)
    else:
        capture_lo = capture
        stim_lo = np.asarray(stim, dtype=np.float64)
    coarse = _earliest_strong_peak(capture_lo, stim_lo) * down

    # Full-rate refinement in a ±4·down window around the coarse arrival —
    # bounded cost (one small correlate), full-rate precision.
    margin = 4 * down
    lo = max(0, coarse - margin)
    hi = min(capture.size, coarse + stim.size + margin)
    window = capture[lo:hi]
    if window.size >= stim.size:
        arrival = lo + _earliest_strong_peak(window, stim)
    else:
        arrival = coarse
    global_offset = arrival - first.start_sample
    return global_offset, first, stimuli


def _locate_segments(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    stimuli: dict[str, np.ndarray],
) -> list[SegmentLocation]:
    """Locate every segment at scheduled offset ± window; record integrity."""
    search = int(round(SEGMENT_SEARCH_S * sample_rate))
    out: list[SegmentLocation] = []
    for seg in program.segments:
        scheduled = global_offset + seg.start_sample
        if seg.kind in STIMULUS_KINDS:
            stim = stimuli.get(seg.segment_id)
            if stim is None:
                stim = segment_stimulus(seg)
                stimuli[seg.segment_id] = stim
            lo = max(0, scheduled - search)
            hi = min(capture.size, scheduled + seg.n_samples + search)
            window = capture[lo:hi]
            if window.size >= stim.size:
                res = _locate(
                    window, stim, sample_rate=sample_rate,
                    max_capture_s=window.size / sample_rate + 1.0,
                )
                located = lo + int(res.lag_samples)
                confidence = float(res.confidence)
            else:
                located = scheduled
                confidence = 0.0
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
    (:func:`_sweep_occurrence_index`) rather than physical schedule position
    (the N=3 interleaved layout — design §5.4, sweep-composition PR-A #1668 —
    physically interleaves w1,t1,w2,t2,... so schedule order is NOT occurrence
    order across roles). Tolerates ANY occurrence count ≥1 per role, including
    a role entirely absent from ``locations`` or present only once (era
    tolerance: an old-shaped program's un-repeated tweeter yields a
    one-element list, never a crash).
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
    stimulus_locs: Sequence[SegmentLocation],
) -> tuple[float | str, str]:
    """Fit a single discrete timeline STEP across the located sweeps (#1765).

    Returns ``(step_samples, after_segment_id)`` when a step is resolved;
    ``(0.0, "")`` when no step is resolved on a capture whose sweeps were
    confidently located — including a clean capture, where this is the
    expected value; or ``(DISCONTINUITY_UNRESOLVED, "")`` (#1839) when one or
    more of ``stimulus_locs`` falls below
    ``SWEEP_LOCATE_CONFIDENCE_FLOOR`` — a step fitted from a sweep the
    locator could barely find is not a "clean capture" reading, it is a
    number invented from noise (real incident: session
    cap_-Us10xORVNlFa_dgi-sP7g's sweeps located at confidence 0.0298, and this
    function reported a confident-looking -2090.5-sample step before this
    precondition existed). Diagnostic only — see ``DISCONTINUITY_MIN_SAMPLES``
    for the hardware forensics this exists to name, and ``DriftEstimate`` for
    the fields it feeds.

    Model: a sweep scheduled at ``start`` and located at ``located`` satisfies
    ``located = acoustic_delay[role] + start·(1+ε) + step·[start > cut]``. One
    constant per ROLE absorbs that role's own acoustic delay (the same reason
    ``_estimate_drift``'s residual guard demeans per role — a real
    tweeter-vs-woofer delay must not read as a glitch); ``ε`` is shared, and
    is fitted here independently of the woofer-pair baseline precisely
    BECAUSE a step corrupts that baseline. Every interior cut is tried and the
    best-fitting one wins; a step must clear both a physical-size floor and an
    explanatory-power ratio to be reported at all.

    Needs enough located sweeps to leave the step model ≥2 degrees of freedom,
    so an old-shaped 3-sweep program (2 roles ⇒ 4 parameters) resolves nothing
    and returns ``(0.0, "")`` rather than fitting noise.

    Limitation worth knowing before trusting a reported step: with only 6
    points and one extra parameter chosen as the best of 5 candidate cuts,
    a capture whose per-segment LOCATE noise is itself several samples can
    fit a spurious step. That is bounded — this value is diagnostic and gates
    nothing — and the same diag record carries
    ``sweep_locate_confidence_min`` / ``sweep_residual_ms_worst``, which is
    where a reader checks whether the locations were trustworthy in the first
    place. Read a step alongside those, not on its own.
    """
    ordered = sorted(
        stimulus_locs, key=lambda loc: program.segment(loc.segment_id).start_sample
    )
    roles = sorted({loc.role for loc in ordered}, key=str)
    # parameters = one per role + shared drift slope + the step itself
    if len(ordered) < len(roles) + 4:
        return 0.0, ""

    # #1839: a step fitted from a sweep the locator could barely find is not
    # a "clean, no step" reading (0.0) — it is a confident-looking
    # fabrication from noise. Gate BEFORE the least-squares fit, on the exact
    # per-location confidence the fit is about to trust implicitly.
    if any(loc.confidence < SWEEP_LOCATE_CONFIDENCE_FLOOR for loc in ordered):
        return DISCONTINUITY_UNRESOLVED, ""

    starts = np.array(
        [float(program.segment(loc.segment_id).start_sample) for loc in ordered]
    )
    located = np.array([float(loc.located_start) for loc in ordered])
    role_column = {role: idx for idx, role in enumerate(roles)}
    base = np.zeros((len(ordered), len(roles) + 1))
    for row, loc in enumerate(ordered):
        base[row, role_column[loc.role]] = 1.0
    base[:, -1] = starts

    def fit(design: np.ndarray) -> tuple[np.ndarray, float]:
        coef, *_ = np.linalg.lstsq(design, located, rcond=None)
        resid = located - design @ coef
        return coef, float(resid @ resid)

    _, no_step_rss = fit(base)
    best_rss = math.inf
    best_step = 0.0
    best_after = ""
    for cut in range(1, len(ordered)):
        step_column = np.zeros((len(ordered), 1))
        step_column[cut:, 0] = 1.0
        coef, rss = fit(np.hstack([base, step_column]))
        if rss < best_rss:
            best_rss, best_step, best_after = rss, float(coef[-1]), ordered[cut - 1].segment_id

    if abs(best_step) < DISCONTINUITY_MIN_SAMPLES:
        return 0.0, ""
    if best_rss > DISCONTINUITY_RSS_RATIO * no_step_rss:
        return 0.0, ""
    return best_step, best_after


def _estimate_drift(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    locations: Sequence[SegmentLocation],
) -> DriftEstimate:
    occurrences_by_role = _sweep_occurrences_by_role(locations)
    # Only the SWEEP-kind stimuli anchor the drift baselines / residual guard.
    # A v2 MEASURE program may open with a leading pilot pair (linearity probe,
    # design §5.2) whose short/quiet windows are located more coarsely; folding
    # them into the residual guard would manufacture spurious desync. Pilots are
    # judged separately (their own linearity verdict), never as a drift baseline.
    stimulus_locs = [loc for loc in locations if loc.kind == KIND_SWEEP]

    # Primary gate: the WOOFER's first-vs-LAST located occurrence — byte-
    # identical gating semantics to the pre-N=3 composer (which had exactly
    # one woofer repeat, so "first vs last" there IS "sweep_w" vs
    # "sweep_w_rep", today's exact pair). This IS the first dereference of
    # "sweep_w" in the analysis flow (`_estimate_drift` runs before
    # `_analyze_measure` resolves its own `seg_w`/`seg_t`) — but a MEASURE
    # program is required to contain "sweep_w" with a role, and this
    # analysis hard-depends on that invariant throughout, so it is the one
    # literal anchor kept — see `_sweep_occurrences_by_role` for why every
    # OTHER role/occurrence is discovered rather than hardcoded.
    woofer_role = program.segment("sweep_w").role
    assert woofer_role is not None, "a MEASURE sweep segment always carries a role"
    woofer_occurrences = occurrences_by_role.get(woofer_role, [])
    w1 = woofer_occurrences[0] if woofer_occurrences else None
    w2 = woofer_occurrences[-1] if len(woofer_occurrences) >= 2 else None

    baselines: dict[str, float] = {}
    epsilon = 0.0
    if w1 is not None and w2 is not None:
        result = _repeat_epsilon(capture, program, w1, w2)
        if result is not None:
            # Primary: sub-sample separation of two identical woofer sweeps
            # (τ cancels; drift is the ratio). Design §3.1 / §5.6.3.
            epsilon, eps_int = result
            baselines["woofer_repeat"] = epsilon * 1e6
            # Cross-check baseline: the integer-located separation ratio (no
            # sub-sample refinement) — a coarse independent view of the same span.
            baselines["woofer_repeat_integer"] = eps_int * 1e6

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
    groups: dict[Any, list[float]] = {}
    for loc in stimulus_locs:
        start = program.segment(loc.segment_id).start_sample
        residual = loc.located_start - (global_offset + start * (1.0 + epsilon))
        groups.setdefault(loc.role, []).append(residual)
    max_residual = 0.0
    for resids in groups.values():
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
    # unstable estimator (the loudest sample jumps between otherwise-identical
    # sweeps), the same bug class already fixed for the channel-map
    # discriminator (#1594) and the pilot linearity gate (#1615). Two real
    # hardware captures (Dayton iMM-6C AND UMIK-2, 2026-07-20) measured two
    # genuinely-identical woofer sweeps 0.64 dB apart by full-band peak —
    # enough to trip this gate — but only 0.06-0.24 dB apart by in-band RMS.
    # Real AGC gain-riding (this gate's actual purpose) still shows up in-band
    # (a uniform per-sweep gain shift survives band-limiting), so this keeps
    # the gate's teeth while dropping the false rejection. A larger delta
    # REUSES the drift-baselines-disagree glitch verdict — never a new
    # user-facing code. Scope note (sweep-composition PR-A, #1668): this
    # first-vs-last pairing only sees the woofer's TWO endpoint occurrences —
    # a level step confined to a middle repeat (or anywhere on the tweeter,
    # which this gate has never covered) does not trip it. Deferred to a
    # future PR's G2 hardening; `per_role_epsilon_ppm` below is the timing
    # analogue already exposed as diagnostic evidence.
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

    # Per-role first-vs-last epsilon diagnostics (design §5, sweep-composition
    # PR-A #1668): free evidence for a future PR's G2 hardening. NEVER gates
    # `glitch_detected` — only the woofer pair above does that, unchanged.
    per_role_epsilon_ppm: dict[str, float] = {}
    for role, occurrences in occurrences_by_role.items():
        if len(occurrences) < 2:
            continue
        result = _repeat_epsilon(capture, program, occurrences[0], occurrences[-1])
        if result is not None:
            per_role_epsilon_ppm[role] = result[0] * 1e6

    # WHICH bound tripped, in a fixed order — the verdict stays one reason
    # code (§5.2), this is telemetry's disambiguator (#1765).
    glitch_inputs = tuple(
        name
        for name, tripped in (
            ("epsilon_out_of_bound", abs(epsilon) * 1e6 > MAX_DRIFT_PPM),
            ("residual_desync", max_residual > GLITCH_RESIDUAL_SAMPLES),
            ("repeat_level_disagree", repeat_level_disagrees),
        )
        if tripped
    )
    glitch = bool(glitch_inputs)

    # Diagnostic only, never gated — and computed on EVERY capture, not just a
    # failing one, so the clean corpus carries the same field and a future
    # bench pass can read its distribution (the `repeat_level_delta_db`
    # precedent). A clean capture resolves no step and reports 0.0 / "".
    discontinuity_samples, discontinuity_after = _locate_discontinuity(
        program, stimulus_locs
    )

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
            # #1839: `discontinuity_samples` is `DISCONTINUITY_UNRESOLVED` (a
            # `str`, not a number) when the located sweeps weren't trustworthy
            # enough to fit a step from — `round()` would raise on that value.
            discontinuity_samples=(
                round(discontinuity_samples, 2)
                if isinstance(discontinuity_samples, (int, float))
                else discontinuity_samples
            ),
            discontinuity_after_segment=discontinuity_after,
        )
    return DriftEstimate(
        epsilon_ppm=epsilon * 1e6,
        baselines_ppm=baselines,
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


def _verify_capture_integrity(
    program: ExcitationProgram,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
) -> CaptureIntegrity:
    """Capture-integrity evidence for a ONE-summed-sweep program (issue #1971).

    ``_estimate_drift`` cannot run here and this is not a smaller version of
    it. Every one of its three glitch inputs compares a role's repeated
    sweeps against each other, and a VERIFY program plays one mono summed
    sweep — so the honest record is not "drift checks passed", it is "drift
    checks did not run, and here is what did".

    What runs, in routing order:

    1. **heard** — the summed sweep's own locate confidence against
       :data:`SWEEP_LOCATE_CONFIDENCE_FLOOR`. First because a sweep the
       correlator could barely find lands in the wrong place and then
       manufactures a large residual: report the cause, not the symptom
       (``crossover_v2_flow._sweep_locate_confidence_ok``'s D3 / #1838
       rationale, which VERIFY never inherited because that gate filters
       ``KIND_SWEEP``).
    2. **schedule** — |residual| against
       :data:`SWEEP_SCHEDULE_RESIDUAL_CEILING_MS`, the G2 xrun detector. Only
       when (1) passed; otherwise ``not_evaluated``, with the measured
       residual still disclosed as evidence.
    3. **clipped run** — any stimulus segment carrying a full-scale run
       (``SegmentLocation.clipped``, already computed by
       ``_locate_segments``). Independent of (1): a clip is a clip whether or
       not the locator was confident.

    The gate-window comparability substitute the P0 bench also used by hand is
    deliberately NOT here. It compares this capture's gate against the
    PREDICTION's, and only the conductor holds the MEASURE window — so it
    stays where it already lives (``crossover_v2_flow._verify_verdict``'s
    inconclusive rule) rather than being restated as a second owner.

    Pilot segments are excluded from (1) and (2) for the same reason
    ``_estimate_drift`` excludes them: their short, quiet windows locate
    coarsely by design and would manufacture spurious fires. They are
    included in (3), where window precision does not matter.

    **What (2) cannot see**, stated because a gate whose bound is unstated
    gets read as total:

    * A splice INSIDE the summed sweep. The residual is measured at the
      sweep's located START, so an insertion partway through corrupts the
      deconvolution while leaving the start where it belongs. MEASURE's G2 has
      the identical bound; this is the shape ``_locate_discontinuity`` exists
      to name, and it needs more sweeps than a VERIFY program has.
    * A splice BEFORE the first stimulus, which the global offset absorbs —
      correctly, since a uniformly shifted capture is not corrupt.
    * Anything at all on a LEGACY pilot-less VERIFY program, where the summed
      sweep IS the global-offset anchor and its residual is therefore
      structurally ~0. Every conductor-composed VERIFY program carries the
      leading pilot pair (``crossover_v2_flow._compose_verify_program`` always
      passes ``leading_pilot_gains_db``), so the anchor is a pilot and the
      sweep's residual is a real measurement; the pilot-less shape survives
      only in older fixtures.
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

    checks: list[IntegrityCheck] = []
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
    the gate's trusted floor) belongs to
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

    The ``max(lo, ...)`` in the stop is load-bearing, not defensive tidying:
    without it, an ``anchor`` far enough before the capture that
    ``anchor + n_samples`` lands in ``(-capture.size, 0)`` gives a NEGATIVE
    stop, which numpy reads as an offset from the END — so the function would
    return a non-empty slice of some other part of the recording and the SNR
    verdict would state a confident number about audio this sweep never
    played. No production caller can reach that today (every anchor is
    ``global_offset + segment.start_sample``, both non-negative), so this
    guards the contract rather than a live path.
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
) -> dict[str, Any] | None:
    """The per-driver SC-1 magnitude SNR verdict, read in ONE domain.

    Both sides of an SNR subtraction have to be the same quantity. This
    function's only real job is picking the signal side to match whichever
    domain the noise report arrived in — the identical branch
    :func:`jasper.active_speaker.driver_acoustics.analyze_driver_capture`
    has always made, and the rule #2024 established for the summed-crossover
    gate.

    * A ``"deconvolved"`` noise report (a deconvolved+windowed ambient IR run
      through :func:`~jasper.audio_measurement.snr_policy.magnitude_band_levels`)
      pairs with the deconvolved transfer-function levels.
    * A ``"raw"`` report — which is every report
      :func:`~jasper.audio_measurement.snr_policy.framed_ambient_band_report`
      produces, and so every ambient report a v2 CHECK hands forward — pairs
      with the RAW captured sweep's band levels.

    **The deciding reason is the SOLVE's domain, not a general preference.**
    This verdict exists to check whether #1829's per-driver level solve
    delivered the SNR it aimed for, and :func:`_solve_role_gain` aims in the
    RAW domain: its room arm is ``ambient_band_level + required_snr_db +
    crest_factor_db``, where the ambient level is a row of the raw
    :func:`~jasper.audio_measurement.snr_policy.framed_ambient_band_report`
    table. So a raw-with-raw verdict reads back the very quantity the solve
    targeted — ``_band_required_snr_db(...) + MEASURE_SNR_SOLVE_MARGIN_DB``,
    i.e. 41 dB where the alignment requirement applies. (The solve's
    ``crest_factor_db`` term converts its own band-RMS demand into the
    capture PEAK that ``k_db`` turns into a digital gain; it cancels out of
    an RMS-vs-RMS SNR and so does not appear in what the verdict reads back.)
    No other domain can perform that check: an instrument graded in units the
    solve never used cannot say whether the solve worked.

    **Why #2024 sent the summed-crossover gate the other way.** That gate has
    no level solve aimed at it — nothing sets the summed sweep's level from a
    band SNR target — so its only constraint is internal consistency, and the
    cheapest way to get both sides into one domain there was to keep the
    deconvolved side and narrow the band table until the raw fallback became
    unreachable. Same rule ("read one domain"), opposite resolution, because
    the two paths are anchored to different things.

    **Why the raw report cannot be subtracted from the transfer function.**
    The deconvolution divides the capture by the reference sweep regenerated
    at that segment's own ``gain_db``, so the drive cancels exactly and
    ``mag_db`` is invariant to how loud MEASURE played — a pinned contract
    (``test_measure_analysis_is_invariant_to_the_programmed_drive_gain``).
    The room's dBFS floor is not. Subtracting one from the other therefore
    yields a number that does not move when the measurement gets quieter,
    which is precisely the question issue #1830 exists to answer. Measured
    through this analyzer on synthetic two-way fixtures: a MEASURE played
    20 dB quieter into an unchanged room reported the SAME worst-band SNR and
    the same ``ok`` verdict, while the same-domain reading fell the full
    20 dB. Against that same-domain reading the old number ran **roughly +17
    to +65 dB high** — band-dependent, and growing as the measurement
    quietens, so not an offset anyone could have corrected for; in the quiet
    arm the honest per-band reading goes a few dB BELOW zero while the old
    one still said ``ok``. Stated as a bound on purpose: the figures come
    from fixtures at different ambient sigmas, and no single decimal
    reproduces across them. The load-bearing claim is the TREND, and that is
    pinned exactly by
    ``test_measure_snr_verdict_moves_with_the_measurement_level``.

    ``window="rectangular"`` because a sweep is non-stationary: a Hann
    window re-weights a swept sine's frequencies by WHEN they occur, which
    is issue #1847's measured defect and #2010's open charge against
    ``driver_acoustics._capture_band_levels``. That consumer keeps the Hann
    default only because nothing production reaches it; this path IS
    production, so it takes the documented fix.

    **The duty-cycle offset that makes rectangular unsafe elsewhere is zero
    here, by construction.** ``_capture_band_levels`` warns that rectangular
    is not a drop-in because on a PADDED capture it carries a band-independent
    ``10*log10(sweep_len/capture_len)`` term (5.93 dB across the 0-to-20 s
    lead sweep it measured). :func:`_raw_sweep_segment` hands this function
    exactly ``segment.n_samples`` — the sweep and nothing else — so that ratio
    is 1 and the term is 0 dB. It is the slice width, not the window, that
    buys this; widening the slice to include lead-in silence would re-arm the
    offset immediately. Pinned by
    ``test_raw_sweep_segment_returns_the_whole_scheduled_segment``.

    Fails closed: a raw report with no captured segment to pair it against
    produces no verdict at all rather than a cross-domain one. "Not
    measured" is honest; a number in the wrong units is not. A segment that
    is PRESENT but degenerate (a capture truncated before this sweep, so
    :func:`_raw_sweep_segment` clamps to fewer than 8 samples) reaches the
    same destination by the shipped route instead: ``band_levels_dbfs``
    returns no bands, so the block carries ``verdict: "unknown"`` and an
    empty band list. Both spellings of "not measured" are honest; they are
    distinguishable on purpose, because absent means "no evidence was
    offered" and unknown means "evidence was offered and was unusable".
    """
    if ambient_report is None or fc_hz is None:
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
    return snr_policy.band_snr_verdicts(
        decision_class=snr_policy.DECISION_CLASS_MAGNITUDE,
        capture_bands=capture_bands,
        noise_bands=noise_bands,
        noise_floor_dbfs_scalar=None,
        relevant_hz=(fc_hz / OVERLAP_OCTAVE_RATIO, fc_hz * OVERLAP_OCTAVE_RATIO),
        model=DRIVER,
        band_method=band_method,
    )


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

    ``radiated_band_hz`` is the band this capture's excitation actually
    drove — the caller's segment sweep bounds. It is the ONLY input the
    pre/post-gate delta needs beyond the IR, and it is threaded from here
    rather than guessed downstream because guessing it is precisely the
    over-report E5 measured (see
    :mod:`jasper.audio_measurement.gate_disclosure`). Absent, the delta is
    simply not reported — never defaulted.

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
    :func:`_driver_response` applies (W6.9 forensics, 2026-07-19): before this
    fix, the prediction composed branches from the fixed ``IR_PRE_MS``/
    ``IR_POST_MS`` window alone, so a room reflection within that 65 ms tail
    was baked into the predicted sum even though VERIFY's measured sum (via
    ``_driver_response``) already reflection-gated it out — a run-7/8 hardware
    failure traced to a 15 cm desk-bounce reflection producing a spurious
    ~1125 Hz null in the FIXED-window prediction that the adaptively-gated
    measured sum never had. Gating a window that already has the peak at a
    fixed local offset preserves that offset (the gate only shortens/tapers
    the TAIL), so the shared time base survives.
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


def overlap_band_hz(
    fc_hz: float,
    *,
    tweeter_sweep_lo_hz: float | None = None,
    woofer_sweep_hi_hz: float | None = None,
) -> tuple[float, float]:
    """SSOT overlap band for the GCC alignment, trim solve, ripple, and
    VERIFY-tracking comparisons: the nominal ``Fc ± 1 octave`` band, clamped to
    the TRUE driver-sweep overlap.

    The nominal ``[Fc/OVERLAP_OCTAVE_RATIO, Fc*OVERLAP_OCTAVE_RATIO]`` band
    silently assumes both drivers were excited across the whole span, but each
    driver's MEASURE sweep only covers its own declared band (design §5.4) —
    e.g. a tweeter sweep starting AT Fc means ``[Fc/2, Fc)`` is pure
    deconvolution noise for that branch (the driver was never excited there).
    That noise corrupted the GCC delay/confidence, the trim solve, the
    predicted ripple, and (via the MEASURE-predicted sum) VERIFY's tracking
    comparison — a real hardware run never cleared the alignment confidence
    floor because of it. Clamping ``lo`` UP to the tweeter's actual sweep
    floor and ``hi`` DOWN to the woofer's actual sweep ceiling keeps every one
    of those consumers inside frequencies BOTH branches actually have real
    excited energy. ``None`` bounds (legacy callers with no sweep-segment
    evidence) leave that side at the nominal Fc/octave edge — byte-identical
    to the pre-fix band.
    """
    lo = fc_hz / OVERLAP_OCTAVE_RATIO
    hi = fc_hz * OVERLAP_OCTAVE_RATIO
    if tweeter_sweep_lo_hz is not None:
        lo = max(lo, float(tweeter_sweep_lo_hz))
    if woofer_sweep_hi_hz is not None:
        hi = min(hi, float(woofer_sweep_hi_hz))
    return lo, hi


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

    lag_samples, polarity_sign, confidence, at_edge = _gcc_phat(
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

    polarity = "normal" if polarity_sign >= 0 else "inverted"

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

    # Fine stage (methodology §10, 2026-07-22). The aligner OWNS the physical
    # peak-gap anchor: the raw full-IR argmax gap (same time base as ``ir_t`` /
    # ``ir_w``), drift-corrected and parallax-corrected into the signed delay
    # frame. Both the fine-snap center (lag domain) and ``_build_candidate``'s
    # applied anchor derive from THIS single computation — the argmax is never
    # recomputed downstream (a parallel argmax could silently desynchronize the
    # snap center from the reported anchor in a subsystem that has died on frame
    # errors). The snap moves the anchor to the nearest local maximum of the
    # SAME correlation within ±(period/6) at Fc; ``None`` (no local peak in
    # radius, or an edge-refused estimate) leaves ``_build_candidate`` on the
    # bare anchor. GCC polarity/confidence machinery is unchanged — this snaps
    # the applied delay only.
    snapped_delay_us: float | None = None
    anchor_delay_us: float | None = None
    if status == ALIGNMENT_OK:
        anchor_lag_samples = float(
            int(np.argmax(np.abs(tweeter_full_ir)))
            - int(np.argmax(np.abs(woofer_full_ir)))
        )
        # Same step-by-step form the candidate used before this ownership move,
        # so the applied anchor is bit-identical: peak gap − inter-sweep drift,
        # plus parallax, negated into the signed frame.
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

    # Cross-check polarity against the flatter predicted sum.
    agrees = _flatter_sum_polarity(
        capture, program, sample_rate, global_offset, fc_hz, priors,
        woofer_full_ir=woofer_full_ir, tweeter_full_ir=tweeter_full_ir,
    )
    polarity_agrees = agrees == polarity_sign
    return AlignmentEstimate(
        delay_us=delay_us,
        raw_delay_us=raw_delay_us,
        parallax_us=parallax_us,
        polarity=polarity,
        polarity_sign=polarity_sign,
        polarity_agrees_with_sum=polarity_agrees,
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
    applied delay would count the measured peak gap twice (the reverted fix-2
    failure mode).

    Public (#1668 PR-D VERIFY-prediction coherence fix): the v2 conductor
    rebuilds its persisted VERIFY prediction from the LINEARIZED branch pair
    when Layer-1a linearization was fitted
    (``jasper.active_speaker.crossover_v2_flow``'s ``_fit_linearization``) —
    the exact model of what the emitted graph will do — reusing this SAME
    machinery rather than a second implementation. No logic changed in this
    rename.

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
    gap twice and — in the revert commit's own words — "injects a deep comb
    into the predicted sum on good measurements and fails VERIFY". That is the
    fix-2 failure mode, backed out in ``0b7ab5eb7`` (2026-07-21) BEFORE the
    #1647 branch it lived on merged, so it never reached a shipped build; the
    same commit deferred "the residual version" to hardware evidence, which is
    the shape rung P3 / R10b finally adopted.

    ``anchor_delay_us`` is ``None`` exactly when the aligner refused the
    estimate (:data:`ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW`): there is then no
    trustworthy argmax-frame reference AND, by the same status,
    ``crossover_v2_flow.alignment_to_candidate_fields`` applies no delay at
    all. Both facts point the same way, so the model keeps the
    independently-aligned frame it is already in and returns ``0.0`` — a
    fabricated gap from an estimate the aligner itself refused would be worse
    than none.
    """
    if anchor_delay_us is None:
        return 0.0
    return float(applied_delay_us) - float(anchor_delay_us)


def _ripple_db(freqs: np.ndarray, magnitude: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        return float("inf")
    band = magnitude[mask]
    band_db = 20.0 * np.log10(np.maximum(np.abs(band), 1e-12))
    return float(np.max(band_db) - np.min(band_db))


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

    Both inner edges are load-bearing, not just the outer ones: the halves
    meet AT Fc, so the woofer's span must reach UP to Fc and the tweeter's
    DOWN to it. Nothing in the system ties a declared driver band to the
    chosen Fc (see ``graph_safety``'s own note on that gap), so a tweeter
    swept from 2.5 kHz under a 2 kHz Fc is representable — and would put
    250 Hz of never-excited deconvolution noise inside the tweeter's half,
    the exact failure :func:`solve_branch_trims` exists to avoid. That
    configuration has no level match in it at all and raises, through the
    catch-all seam in :mod:`jasper.web.correction_crossover_v2` that already
    classifies an unanalysable capture as ``internal_error`` — never a
    guessed trim on the hardware.

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

    Each ``*_span_hz`` is that branch's own validity span (default Fc ∓ 1
    octave); :func:`branch_level_bands_hz` turns the pair into the mirrored
    halves ``[Fc/ρ, Fc]`` (woofer) and ``[Fc, Fc·ρ]`` (tweeter). A branch is
    never read outside the band it was excited and gated in.

    Why mirrored halves and not one shared band (PR-L3, 2026-07-27). This
    function band-power-averages ``|W|`` and ``|T|``, which is a level match
    only when each branch is weighted symmetrically about Fc. It was called
    with the SHARED both-branches-excited overlap band, whose lower edge
    :func:`overlap_band_hz` clamps UP to the tweeter's sweep floor. On a real
    2-way whose tweeter sweep starts AT Fc (JTS3: Fc 2 kHz, tweeter swept
    2-20 kHz) that clamp leaves ``[Fc, 2Fc]`` — entirely on the side where the
    woofer is inside its crossover skirt and the tweeter is climbing into its
    passband. The result is not a level match but a skirt-depth measurement.
    On an ideal LR4 pair with two EQUAL-sensitivity drivers, ``[Fc, 2Fc]``
    returns **+10.59 dB** instead of 0 (closed form, pinned by
    ``test_one_sided_overlap_band_biases_the_level_match``); on the archived
    JTS3 captures it put the measured tweeter trim 10.9 dB (2026-07-27) and
    13.1 dB (2026-07-25) below the same analysis's own per-driver
    ``target_level_db`` frame — the ideal-pair figure accounts for the
    observed error to within 0.27-2.47 dB across the two sessions, the
    remainder being each real driver's own rolloff riding on top of the
    filter's. That is where the speaker's ~10 dB-dark tweeter came from. Widening back to the nominal band is not the fix
    either: the tweeter was never EXCITED below Fc, so those bins are
    deconvolution noise that dilutes its power mean (+3.03 dB residual bias
    on the same ideal pair). Reading each branch only on its own side removes
    both problems — residual bias +0.54 dB at ρ=2, shrinking with ρ.

    That remaining +0.54 dB is a KNOWN, RECORDED systematic, not a limit of
    the method: it is the linear-frequency FFT bin grid weighting the wider
    upper half harder. The same estimator integrated in log-frequency is
    exactly 0.000 dB on an ideal pair (verified by quadrature during the
    PR-L3 review). Switching to a log-measure average is deliberately NOT
    done here: it would move every measured trim again mid-ladder, on top of
    the 10-13 dB this change already moves, with no capture to separate the
    two effects. Owner ruling 2026-07-27 — keep the linear average now,
    revisit it with PR-L4's measured-vs-datasheet cross-check, which supplies
    exactly the independent reference needed to tell a 0.5 dB estimator
    residual from a real acoustic difference.

    Public: kept module-public as the level match's SSOT — the v2 conductor
    documents its own anchored give-back seed against this function
    (`jasper.active_speaker.crossover_v2_flow`, "why not the old
    solve_branch_trims seed") and the contract tests import it. Its sibling
    :func:`overlap_band_hz` is public because the conductor CALLS it.
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

    **The assertion nothing in the chain made** (linearization-integrity PR-L4
    item 1). Of the comparators the 2026-07-27 forensics inventoried, three
    compare the speaker to itself, one compares it to flat, and *none* compared
    the two drivers' realized handoff levels to each other. That is the gap a
    ~9 dB-dark tweeter walked through: every stage was individually satisfied,
    because no stage was asked the one question whose answer was wrong.

    ``W``/``T`` are the branch transfer functions **as they will be emitted** —
    on the v2 path, the linearized pair ``resp.complex_tf * correction``, not
    the raw measurement. ``trim_*_db`` are the trims the graph will actually
    carry. This function applies them and re-reads the levels, so it grades the
    committed decision rather than re-litigating it.

    **One estimator, not a second opinion.** The levels come from
    :func:`solve_branch_trims` on the trimmed pair — the SAME power-band average
    over the SAME :func:`branch_level_bands_hz` halves that set the trim in the
    first place. That is deliberate: a check with its own band or its own
    averaging rule would be a rival estimate, and a disagreement between two
    rivals tells you nothing about which is right (the repo already carries that
    lesson as the measured-vs-datasheet gap this PR's item 3 closes). Reusing
    the estimator makes this a strict closed-loop question — *did the trim we
    are about to ship do what a trim is for?* — and it inherits the estimator's
    known +0.54 dB systematic rather than adding an unknown one.

    Each branch is read only on its own side of Fc, never the shared
    both-branches-excited overlap: reading a branch inside the other's crossover
    skirt measures skirt depth, not level, which is PR-L3's whole finding.

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
    """Ripple-minimizing tweeter trim, scanned around the band-average seed,
    regularized toward the seed on a flat minimum (#1667; flat-minimum
    regularization is a follow-up architect review).

    Instead of matching levels, scan the tweeter trim and keep whichever
    value minimizes the SUMMED branch response's ripple (max-min dB) over
    ``[lo_hz, hi_hz]`` — reusing :func:`predicted_branch_sum` and
    :func:`_ripple_db` exactly as ``predicted_ripple_db`` elsewhere on the
    candidate already does, rather than inventing a second flatness metric.

    #1667 introduced this as a fix for ``solve_branch_trims``' bias when the
    evaluation band sat inside one branch's own filter rolloff ("the fix is
    the OBJECTIVE, not the band"). PR-L3 found that insufficient — on that
    same one-sided geometry the summed ripple is the tweeter's own and
    barely responds to the tweeter's gain, so the scan recovered only a
    fraction of the error — and fixed the BAND at its source instead
    (``solve_branch_trims`` now reads each branch on its own side of Fc).
    This function is therefore a flatness POLISH on an already-correct
    level, and ``_build_candidate`` runs it only where ``[lo_hz, hi_hz]``
    straddles Fc. Its own contract is unchanged: it optimizes ripple over
    whatever band it is handed.

    The woofer/reference branch's trim (``trim_w_db``) is held FIXED —
    ripple depends only on the RELATIVE gain between branches, so scanning
    one side alone still explores the full space of achievable relative
    gains. ``trim_w_db`` defaults to 0.0, matching ``solve_branch_trims``'s
    own convention that the quieter branch is left unattenuated; a caller
    whose band-average solve gave a nonzero woofer trim should pass that
    value so the scan is centered on the summed response that will actually
    be applied.

    Search window: ``seed_trim_db +/- window_db`` at ``step_db`` steps
    (defaults: :data:`RIPPLE_TRIM_SEARCH_WINDOW_DB` /
    :data:`RIPPLE_TRIM_SEARCH_STEP_DB` — +/-10 dB / 0.1 dB), clamped to the
    physically valid attenuation range
    [:data:`RIPPLE_TRIM_MIN_DB`, :data:`RIPPLE_TRIM_MAX_DB`] — a trim is
    never net gain and never beyond the shared -60 dB floor, so the scan
    must not even EVALUATE an unphysical candidate (a flatter-but-invalid
    "trim" is not a real answer).

    Selection is flat-minimum-regularized, not a bare argmin: among every
    scanned candidate whose ripple is within ``flat_minimum_epsilon_db``
    (default :data:`RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB`, 0.25 dB) of the
    GLOBAL minimum ripple found in this scan, the one CLOSEST TO THE SEED
    wins — never merely the first/lowest-ripple candidate encountered. A
    sharp, unique minimum degenerates to that single point (bare argmin,
    unaffected); a shallow bowl (a wide, nearly-flat region straddling the
    true minimum — real hardware shape, not just a synthetic edge case)
    instead prefers whichever near-optimal candidate drifts LEAST from the
    conventional band-average trim, trading a negligible/inaudible amount
    of measured flatness for session-to-session repeatability — the exact
    minimizer of a shallow bowl is sensitive to measurement noise and would
    otherwise wander between re-measurements of the same speaker. Plain
    exact ties are a special case of this rule (trivially within epsilon of
    each other) and need no separate handling.

    Returns ``(trim_t_db, ripple_db, seed_trim_db)``: the selected trim, the
    summed-response ripple (dB, max-min) AT that trim, and the seed it was
    scanned around — echoed back so a caller building an evidence/sanity-
    guard comparison doesn't need to separately thread the seed through.

    ``lo_hz``/``hi_hz`` default to Fc +/- 1 octave like ``solve_branch_trims``;
    every current caller passes its own gating-clamped band explicitly — the
    #1667 fix changes the objective, never the band.
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
        # The seed's own window has no physically valid attenuation value at
        # all (shouldn't happen from solve_branch_trims's own <=0 output,
        # but stay defensive) — clamp the seed itself into range rather than
        # searching an empty set.
        candidate_trims = [min(max(seed_trim_db, RIPPLE_TRIM_MIN_DB), RIPPLE_TRIM_MAX_DB)]
    ripples_db = [
        _ripple_db(
            freqs_band,
            predicted_branch_sum(w_band, t_band, trim_w_db, candidate_trim, sign),
            lo, hi,
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


def _flatter_sum_polarity(
    capture, program, sample_rate, global_offset, fc_hz, priors,
    *, woofer_full_ir, tweeter_full_ir,
) -> int:
    n_fft = _n_fft_for(woofer_full_ir, tweeter_full_ir)
    freqs, W, _gate_w = _aligned_branch_tf(woofer_full_ir, sample_rate, n_fft, calibration=None)
    _f2, T, _gate_t = _aligned_branch_tf(tweeter_full_ir, sample_rate, n_fft, calibration=None)
    trim_w, trim_t, _lw, _lt = solve_branch_trims(freqs, W, T, fc_hz)
    # SSOT overlap band (fix 1) — clamps the nominal Fc±1-oct span to the real
    # driver-sweep overlap so this ripple check can't drift out of sync with
    # the alignment/trim/VERIFY bands that already use this helper.
    lo, hi = overlap_band_hz(
        fc_hz,
        tweeter_sweep_lo_hz=program.segment("sweep_t").f1_hz,
        woofer_sweep_hi_hz=program.segment("sweep_w").f2_hz,
    )
    ripple_pos = _ripple_db(freqs, predicted_branch_sum(W, T, trim_w, trim_t, +1), lo, hi)
    ripple_neg = _ripple_db(freqs, predicted_branch_sum(W, T, trim_w, trim_t, -1), lo, hi)
    return 1 if ripple_pos <= ripple_neg else -1


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
# gets ``None`` and the analysis degrades to the pre-#1810 "no ambient
# evidence, trust the pilots" behaviour — never to a fabricated floor.
#
# ONE policy, both windows (#1818): CHECK's 12 s session-ambient window
# (`_ambient_from_capture`) and MEASURE/VERIFY's 1 s pilot-ambient window
# (`_pilot_ambient_samples`) ask the same question of the same kind of
# evidence, so they share this constant rather than each carrying a number
# that can drift from the other.
AMBIENT_MIN_USABLE_FRACTION = 0.5


def _ambient_from_capture(
    capture: np.ndarray, sample_rate: int, ambient_seg: ProgramSegment, global_offset: int
) -> tuple[np.ndarray | None, dict[str, Any]]:
    """CHECK's session-ambient window and its band-floor report.

    The window is CLIPPED to the capture, never SLID along it (#1818): ``end``
    is computed from the window's own (possibly negative) schedule position,
    not from the clamped start, exactly as `_pilot_ambient_samples` does.
    Computing it from the clamped start walks a capture that began ``D``
    seconds late forward onto whatever the schedule put AFTER the window — for
    the shipped CHECK program that is the courtesy prelude, 0.6 s of −18 dBFS
    beep at [12.0, 12.6) s butted directly against the 12 s window. Measured on
    the shipped geometry: a 0.6 s late start read the room floor 39.5 dB hot
    (−70.00 → −30.52 dBFS window RMS; worst framed band −74.65 → −52.85 dBFS),
    which is not a floor at all — it is the beep. That number feeds BOTH
    `_snr_floor_ok` (the room-quality gate) and `_solve_gain_plan`.

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

    Issue #1810: MEASURE/VERIFY programs carry an
    :data:`~jasper.audio_measurement.program.AMBIENT_SEGMENT_ID` window ahead
    of their leading pilot pair (``program.PILOT_AMBIENT_WINDOW_S``) so
    `_pilot_observations`' in-band SNR guard has something to measure against
    on those phases too. Before that window existed the guard's input was
    ``+inf`` by construction and it could never fire — the structural defect
    that let a pilot pair drowned in room noise be reported as the phone's
    microphone misbehaving.

    Located by SCHEDULE offset (like `_ambient_from_capture`), not by
    correlation — it is silence, so there is nothing to correlate. That makes
    it exact for a live capture (the conductor plays the program it composed)
    and meaningless for a CROSS-ERA replay of an archived capture, where the
    window lands on whatever the older schedule had at that position. The
    replay failure direction is the safe one: a too-loud "ambient" reads as
    low SNR, which resolves ``linearity_ok`` to ``None`` (unknown — #1838;
    it was forced ``True`` before) and can never manufacture a false AGC
    accusation.

    The window is CLIPPED to the capture, never SLID along it: ``end`` is
    computed from the window's own (possibly negative) schedule position, not
    from the clamped start, so a capture that began after the program did
    yields a shorter window rather than one that has walked forward onto the
    pilot it is supposed to measure the floor for. `_ambient_from_capture`
    (CHECK's 12 s window) now has this same shape — it was fixed to match in
    #1818, and both share :data:`AMBIENT_MIN_USABLE_FRACTION`.
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

    ``ambient_power`` is 0.0 when there is no ambient evidence (a legacy
    program composed without a room-listening window, or one whose window
    fell outside the capture — see `_pilot_ambient_samples`); subtracting
    zero is a no-op, so this degrades to plain in-band RMS in that case.
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

    Two drivers' declared bands legitimately overlap around the crossover
    point (design §5.2/§5.4 — MEASURE needs response through the Fc overlap
    from both drivers), so a role's own pilot content routinely also falls
    inside the shared part of an adjacent role's declared band — that shared
    part carries no map-discrimination signal. The CROSS test (see
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
    `_band_power` — same Hann+bandpass mechanism `_channel_map_ok` uses, not a
    second filtering idiom) and, when an ambient window is available,
    ambient-power-subtracted before converting to dB —
    fixing the 2026-07-20 bug where a full-band PEAK estimate let LF room
    rumble inflate the quiet pilot's level and compress the captured delta
    (see `LINEARITY_TOLERANCE_DB`'s comment). With no window at all
    (``ambient_samples=None`` — a legacy program composed before #1810)
    subtraction degrades to a no-op (`_ambient_subtracted_dbfs`) and SNR is
    trusted unconditionally, because there is nothing to validate against.

    **Two ambient parameters, deliberately (issue #1810).**
    ``ambient_samples`` feeds the level/SNR path; ``channel_map_ambient_samples``
    feeds `_channel_map_ok`'s TARGET/CROSS rise test. CHECK passes the same
    12 s window to both — the long, framed, percentile-based room-floor
    estimate `CHANNEL_MAP_TARGET_RISE_DB` (12 dB) was calibrated against, on
    the run-5 hardware table. MEASURE/VERIFY pass only the first, for two
    reasons, neither of which is "it would break something today":

    1. **Calibration.** Their pre-pilot window is a ~1 s spot estimate sized
       for the SNR guard, not the duration-independent statistic the rise
       thresholds were derived from. Judging a 12 dB rise against it would be
       reading a threshold off an estimator it was never fitted to.
    2. **Pre-emption.** ``analysis.channel_map_ok`` is routed on at exactly
       ONE site today — ``crossover_v2_flow._check_verdict``, which maps it to
       the hard-stop ``channel_map_mismatch``. MEASURE, the cloud positions
       and VERIFY compute the flag and never branch on it, so threading the
       window here would currently change no verdict at all. It would instead
       leave a False flag sitting on those analyses, ARMED for the next
       maintainer who adds a routing branch: a pilot pair a few dB over the
       floor (exactly the #1810 shape) would then hard-stop with copy blaming
       the speaker wiring, on evidence never calibrated for a 1 s window.

    Their channel-map check therefore keeps the total-in-band-energy-fraction
    fallback it has always used.

    The located segment's fixed composer fade (`_pilot_trim_fade`) is trimmed
    before measuring so the RMS estimate rides the steady-state portion, not
    the ramp.

    Low-SNR honest routing: the quiet (lo) pilot is the binding constraint
    (10 dB quieter than hi, same ambient), so its in-band SNR
    (`_pilot_in_band_snr_db`) gates trust. Below `PILOT_MIN_SNR_DB` the
    ambient-subtracted estimate isn't reliable either way —
    ``linearity_ok`` is ``None``, i.e. UNKNOWN (never a false FAILURE, and
    since #1838 never a false PASS either) and
    ``snr_valid=False`` lets the caller route to the honest "room/
    positioning" reason instead of blaming the phone's AGC (see
    `ProgramAnalysis.pilot_snr_ok` and `crossover_v2_flow._consume_check`).

    ``peak_lo_dbfs``/``peak_hi_dbfs`` are a SEPARATE, non-ambient-subtracted
    measurement: the exact pre-fix `_peak_dbfs` (full-band peak of the
    located, untrimmed samples) `_solve_gain_plan` used before this function
    grew the band-relative/ambient-subtracted level. They exist because
    `_solve_gain_plan` uses a pilot level ABSOLUTELY (``k = level -
    gain_db``, an estimate of the whole capture chain's dB gain), not as a
    delta — feeding it the ambient-subtracted level would silently shift
    that absolute reference by however much ambient power was subtracted
    (measured 13-17 dB across both real captures), retuning
    `MeasurementPriors.target_capture_dbfs`'s documented capture-PEAK target
    hotter than intended. An in-band (band-limited) peak was evaluated as a
    more-robust replacement but empirically introduced its own bandlimiting-
    leakage bias (up to ~1.3 dB on a real capture — worse than "a few
    tenths") whether or not the slice was windowed first, so the exact
    pre-fix computation is kept verbatim for this consumer instead.
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
        # D7 (issue #1838): UNKNOWN below the SNR floor, not True. The
        # captured delta is not evidence down there in EITHER direction, and
        # `True` is a claim — session cap_-Us10xORVNlFa_dgi-sP7g published
        # `linearity_ok=true` beside a captured delta of -60.9 dB against a
        # programmed 10.0 dB, which is not a passing linearity check, it is
        # the absence of one. `None` still never registers as a FAILURE (the
        # reason this was forced True), and it stops reading as a PASS to a
        # consumer that does not also check `snr_valid`.
        linearity_ok = (
            None if not snr_valid
            else abs(captured_delta - programmed_delta) <= LINEARITY_TOLERANCE_DB
        )

        # Gain-solve reference: exact pre-fix full-band peak (see the
        # docstring above) — deliberately NOT the ambient-subtracted level.
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


def _aggregate_linearity_ok(
    pilots: Sequence[PilotObservation],
) -> bool | None:
    """Reduce per-pilot ``linearity_ok`` over the roles, tri-state aware.

    A FAILURE anywhere is the verdict; otherwise an UNKNOWN anywhere (a
    pilot whose SNR was too low to judge — D7, issue #1838) makes the whole
    verdict unknown, because "the roles we could read were fine" is not the
    same claim as "the pilots were linear". ``None`` for no pilots at all,
    unchanged. Written out rather than left as ``all(...)``: Python's
    ``all()`` folds ``None`` to False, which would have turned every
    unknown into a linearity FAILURE — precisely the mic accusation the
    low-SNR routing exists to prevent.
    """
    if not pilots:
        return None
    verdicts = [p.linearity_ok for p in pilots]
    if any(v is False for v in verdicts):
        return False
    if any(v is None for v in verdicts):
        return None
    return True


def _pilot_verdicts(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
    *,
    global_offset: int,
) -> tuple[tuple[PilotObservation, ...], bool | None, bool | None, bool | None]:
    """Pilot observations + the aggregate linearity / channel-map / SNR verdicts.

    ``None`` verdicts when the program carries no pilots (a legacy MEASURE /
    VERIFY program), so a caller can distinguish "no pilot evidence" from
    "pilot evidence, all clean". Shared by v2 MEASURE / VERIFY, whose leading
    pilot pair (design §5.2) carries per-capture linearity evidence CHECK-only
    verification cannot.

    Since issue #1810 (2026-07-28) those programs also carry a short
    room-listening window immediately ahead of that pilot pair, so
    ``pilot_snr_ok`` is a REAL verdict here rather than the unconditional
    ``True`` it used to be: `_pilot_ambient_samples` reads the window at its
    schedule offset and hands it to the level/SNR path. The channel-map check
    still uses `_channel_map_ok`'s total-in-band-energy-fraction fallback —
    see `_pilot_observations` for why that short window must not feed the
    rise test. A program without the window (legacy, or composed with no
    leading pilots) behaves exactly as before.
    """
    pilots = _pilot_observations(
        program, capture, sample_rate, locations,
        ambient_samples=_pilot_ambient_samples(program, capture, global_offset),
    )
    linearity_ok = _aggregate_linearity_ok(pilots)
    channel_map_ok = all(p.channel_map_ok for p in pilots) if pilots else None
    pilot_snr_ok = all(p.snr_valid for p in pilots) if pilots else None
    return tuple(pilots), linearity_ok, channel_map_ok, pilot_snr_ok


def _channel_map_ok(
    samples: np.ndarray,
    sample_rate: int,
    seg: ProgramSegment,
    *,
    ambient_samples: np.ndarray | None = None,
    other_bands: Sequence[tuple[float, float]] = (),
) -> tuple[bool, float | None, float | None]:
    """Band-relative channel-map sanity (design note above `CHANNEL_MAP_*`).

    Given a leading ambient (room-noise) window — CHECK's own 12 s ambient
    segment — this asks two independent questions per pilot instead of the
    old single "is most of the TOTAL energy in-band" fraction test (which a
    concurrent, unrelated room-noise band could veto even when the driver
    under test was behaving correctly — the run-5 hardware bug):

    1. TARGET: did THIS driver's own declared band rise
       ``CHANNEL_MAP_TARGET_RISE_DB`` above that band's ambient level? (the
       driver actually played, above the room's floor in its own band.)
    2. CROSS: did every OTHER driver's band stay BELOW
       ``CHANNEL_MAP_CROSS_RISE_DB`` above ITS ambient level during this same
       pilot window? (energy did not land in the wrong driver's band — the
       actual map-swap discriminator.)

    Without an ambient window, falls back to the original test: energy inside
    the declared band must exceed half of the pilot window's TOTAL spectral
    energy. That is the path v2 MEASURE/VERIFY still take — their pre-pilot
    window is deliberately NOT threaded here (see `_pilot_observations`'s
    "two ambient parameters" note) — and the path any legacy program with no
    window at all takes.

    Returns ``(ok, target_rise_db, cross_rise_db)`` — the two rise numbers are
    ADDITIVE diagnostic evidence for operator logging (surfaced on
    ``PilotObservation``); the pass/fail decision below is byte-identical to
    before this return shape grew. ``cross_rise_db`` is the rise that failed
    the CROSS test when ``ok`` is False, or the worst (highest) rise observed
    across every other band when ``ok`` is True. Both rises are ``None`` in
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
        return float(np.sum(spectrum[in_band])) / total > 0.5, None, None

    target_rise = (
        _band_rms_dbfs(x, sample_rate, seg.f1_hz, seg.f2_hz)
        - _band_rms_dbfs(ambient_samples, sample_rate, seg.f1_hz, seg.f2_hz)
    )
    if target_rise < CHANNEL_MAP_TARGET_RISE_DB:
        return False, target_rise, None
    worst_cross_rise: float | None = None
    for other_f1, other_f2 in other_bands:
        cross_rise = (
            _band_rms_dbfs(x, sample_rate, other_f1, other_f2)
            - _band_rms_dbfs(ambient_samples, sample_rate, other_f1, other_f2)
        )
        if worst_cross_rise is None or cross_rise > worst_cross_rise:
            worst_cross_rise = cross_rise
        if cross_rise >= CHANNEL_MAP_CROSS_RISE_DB:
            return False, target_rise, cross_rise
    return True, target_rise, worst_cross_rise


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
    direction, since a higher requirement means a LOUDER solve, i.e. closer
    to today's behavior. An unknown Fc must never buy a quieter measurement.
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

    **Every arm is peak-expressed** (D6, issue #1838). ``k_db`` and
    ``flat_target_gain_db`` are capture-PEAK quantities; an ambient band level
    and an SNR requirement are RMS quantities. Adding them directly — which
    this function did until #1838 — under-drove MEASURE by the stimulus's own
    crest factor, so each arm now carries
    :func:`sweep_band_crest_factor_db` (roughly 8-19 dB for the room arm's
    rows, exactly :data:`SWEEP_PEAK_TO_RMS_DB` for the pilot arm).

    * **room SNR** — the worst ``ambient + required_snr`` across the ambient
      bands overlapping this driver's own measurement band. Band-scoped on
      purpose: a room's noise is overwhelmingly low-frequency, so a tweeter
      measured from 2 kHz up genuinely needs less drive than a woofer
      measured from 150 Hz, and pinning both to one broadband figure is what
      made MEASURE louder than it had to be.

      Two known coarsenesses in the ambient table
      (``snr_policy.CROSSOVER_SNR_BANDS_HZ``), both erring LOUD, i.e. toward
      today's behavior — neither is worth a finer table until bench data says
      so. (1) Its rows are wide, and overlap is overlap: a woofer swept from
      ``MEASURE_SWEEP_F_LO_HZ`` (150 Hz) clips the 80-160 Hz ``bass`` row by
      only 10 Hz yet inherits that row's full — LF-heavy, therefore loud —
      level. Expect woofers to sit at ``GAIN_BOUND_FLAT_TARGET`` far more
      often than tweeters; the reduction this solve buys is mostly the
      tweeter's. (2) The table stops at 12 kHz, so a tweeter's top ~2/3
      octave contributes no demand at all. Room noise up there is below every
      lower band in any real room, so an omitted row cannot be the one that
      would have won — the worst-band max is unaffected.
    * **pilot SNR** — MEASURE opens on a two-level pilot pair whose QUIET
      side sits ``pilot_delta_db`` below the sweep gain, and issue #1816's
      guard refuses the capture when that pilot's own in-band SNR falls under
      ``PILOT_MIN_SNR_DB``. Backing a driver's sweep off without carrying its
      pilots along would trade a loud measurement for a failing one, so the
      pilot floor is part of "the SNR the fit needs". Applied to every role
      rather than only the role that actually carries the leading pilots
      (``crossover_v2_flow.CrossoverV2Conductor._compose_measure_program``
      puts them on the woofer today): it is a floor, so applying it more
      widely can only keep a level closer to today's, and it stays correct if
      the composer ever moves the pair. The conductor's clip retry
      (``_rearm_measure_after_transient``) subtracts a further
      ``CLIP_RETRY_BACKOFF_DB`` (3 dB) from whatever this returns, which
      ``MEASURE_SNR_SOLVE_MARGIN_DB`` absorbs with room to spare — and a
      clip is far less likely from a level solved down toward the floor
      than from one driven at the ADC's headroom.
    * **capture floor** — ``DRIVER.peak_too_low_dbfs``, the shipped
      capture-quality model's "a capture peak below this is too low to
      trust". Guards the degenerate case where an ambient report reads near
      the dBFS floor and the SNR math alone would propose an inaudible sweep.
      **It is a tripwire, not a shippable bound** (D2, issue #1838): if it
      wins, the other two arms have both resolved below a level that is by
      definition too faint to measure, which says the ambient evidence is not
      solvable — so the solve is REFUSED and the role falls back to
      ``flat_target_gain_db`` with ``bound_by=GAIN_BOUND_DEGENERATE_AMBIENT``
      and a WARNING. ``GAIN_BOUND_CAPTURE_FLOOR`` therefore no longer appears
      on a returned solve; it stays in ``GAIN_BOUNDS`` as the name of the
      losing arm and for reading back solves persisted before #1838.

    The result is then clamped by ``flat_target_gain_db``: this solve can only
    make MEASURE quieter than (or equal to) what it does today, never louder.
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
        # D6 (#1838): `level + required_snr` is a band-RMS demand, but what it
        # is about to be compared against — and what `k_db` converts into a
        # digital gain — is a capture PEAK. Carry the stimulus's own
        # peak-to-band-RMS across so both sides are peak-expressed.
        crest = sweep_band_crest_factor_db(band_hz, (lo, hi)) if band_hz else 0.0
        demands.append((level + required_snr + crest, level, required_snr, crest))
    required_capture_dbfs, ambient_dbfs, required_snr_db, crest_factor_db = max(
        demands, key=lambda item: item[0]
    )
    # NAMED RESIDUAL, erring QUIET (pre-existing, unchanged by #1838): a
    # pilot's SNR is measured over its WHOLE band, but this floor is built
    # from the single worst overlapping ROW. A row narrower than the pilot's
    # band understates the noise the pilot actually integrates, so this arm
    # can sit lower than the pilot really needs. Left alone because it is not
    # the binding arm anywhere it has been measured — on the JTS3 field room
    # the room arm wins by 16-19 dB — and because widening it is a tuning
    # change that wants bench data, not a correctness fix.
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
        # D2 (#1838): the capture floor winning is this function's OWN
        # documented degenerate-ambient signal — "an ambient report [reading]
        # near the dBFS floor [where] the SNR math alone would propose an
        # inaudible sweep". Until #1838 it was treated as a floor to ship,
        # and the field session that exposed the band-power bug shipped
        # exactly that: both roles solved to a -45 dBFS capture target, 34 dB
        # below the flat level, and every downstream guard that might have
        # caught it (the pilot SNR floor, the sweep locator) was computed from
        # the same collapsed ambient report and so could not bound it by
        # construction. A floor-bound solve is not a level, it is evidence
        # that the ambient report cannot be solved against — so refuse, keep
        # today's proven flat target, and say which way the refusal went.
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
        # The pre-#1825 answer, now the CEILING of the solve below.
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

    # Deliberately still judged at `target_capture_dbfs`, NOT at the solved
    # level. This is the room-quality gate ("is this room quiet enough to
    # commission in at all"), asked of the reference target against the whole
    # ambient report — including the sub-bass band no driver's sweep reaches.
    # The solve answers a different question (how much drive this fit needs,
    # per driver, in that driver's own band), and folding the two together
    # would silently change which sessions CHECK accepts.
    #
    # #1838 made this gate STRICTLY HARDER, and it is the only one that got
    # harder. Its arithmetic is unchanged, but its input stopped reading
    # 18-39 dB too quiet: passing now genuinely requires the worst TRUE
    # ambient band at or below `target_capture_dbfs - DRIVER.snr_ok_db`
    # (-35.5 dBFS at the shipped target). Rooms that used to sail through on
    # a collapsed reading can now be refused — which is the point. Headroom
    # is not tight where it has been measured: the JTS3 field room's worst
    # true band was -57.86 dBFS, ~22 dB inside the bound.
    snr_floor_ok = _snr_floor_ok(ambient_report, target)
    return GainPlan(
        gain_db=gains,
        predicted_peak_dbfs=predicted_peak,
        snr_floor_ok=snr_floor_ok,
        role_solves=solves,
    )


def _snr_floor_ok(ambient_report: Mapping[str, Any], target_capture_dbfs: float) -> bool:
    """False when the ambient report is missing, empty, or every row is
    unreadable (#1831) — never raises on a malformed ``level_dbfs``.

    Mirrors ``_ambient_rows_in_band``'s defensive parse, one function above:
    more than one producer writes ambient band rows (a live in-process
    ``snr_policy`` report, plus a replayed/legacy artifact), so a row this
    cannot read must cost this gate that row's evidence — never crash CHECK's
    accept path. Unreachable from today's in-process producer alone, but the
    asymmetry between the two functions is exactly what bites a replay path.
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
) -> ProgramAnalysis:
    """Analyze a program capture into a :class:`ProgramAnalysis` (design §5.6)."""
    if sample_rate != program.sample_rate_hz:
        raise ValueError(
            f"capture rate {sample_rate} != program rate {program.sample_rate_hz}"
        )
    capture = np.asarray(samples, dtype=np.float64).ravel()
    # Bound the capture BEFORE any full-rate FFT (kernel contract: defense at
    # the FFT, 1 GB Pi). A legitimate conductor capture is the program plus a
    # small phone-start lead; a stuck recording is truncated to the program
    # duration plus CAPTURE_BOUND_MARGIN_S. A program that genuinely starts
    # beyond the margin fails downstream location checks loudly instead of
    # allocating hundreds of MB here.
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

    global_offset, _first, stimuli = _global_offset(program, capture, sample_rate)
    locations = _locate_segments(program, capture, sample_rate, global_offset, stimuli)

    if program.phase == PHASE_CHECK:
        return _analyze_check(program, capture, sample_rate, global_offset, locations, priors)
    if program.phase == PHASE_MEASURE:
        return _analyze_measure(
            program, capture, sample_rate, global_offset, locations,
            calibration, geometry, priors,
        )
    if program.phase == PHASE_VERIFY:
        return _analyze_verify(
            program, capture, sample_rate, global_offset, locations,
            calibration, priors,
        )
    raise ValueError(f"unknown phase: {program.phase!r}")


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
    channel_map_ok = all(p.channel_map_ok for p in pilots) if pilots else None
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
    fc_hz: float,
    n_fft: int,
) -> tuple[DriverResponse, ...]:
    """Deconvolve + gate + TF every occurrence AFTER the first (design item 7,
    sweep-composition PR-A #1668): free per-repeat evidence for a future PR's
    deeper drift/G2 hardening. Individually bounded exactly like the primary
    response (same ``n_fft``); no caching or parallelism added — Pi
    measurement is not latency-critical here, and this triples the
    deconvolution call count (2→6) on purpose per the design. The PRIMARY
    response (the driver's canonical ``sweep_w``/``sweep_t`` occurrence) is
    built by the caller and untouched by this function; repeats never feed
    the candidate/trim/alignment math. ``role`` is the primary's own
    already-resolved role: every location in ``occurrences`` shares it by
    construction of the caller's per-role grouping
    (``_sweep_occurrences_by_role``), so it is threaded through explicitly
    rather than re-derived per segment as an ``Optional[str]``.

    Consumed by ``jasper.active_speaker.linearization_envelope.
    compute_sigma_curve`` — the Layer-1a repeatability term.
    """
    out: list[DriverResponse] = []
    for repeat_index, loc in enumerate(occurrences[1:], start=1):
        seg = program.segment(loc.segment_id)
        full_ir, _pre = _deconvolve_window(
            capture, seg, global_offset + seg.start_sample, sample_rate,
            epsilon=epsilon,
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


def _analyze_measure(
    program, capture, sample_rate, global_offset, locations,
    calibration, geometry, priors,
) -> ProgramAnalysis:
    if priors.crossover_fc_hz is None:
        raise ValueError("MEASURE analysis requires priors.crossover_fc_hz")
    fc_hz = float(priors.crossover_fc_hz)
    drift = _estimate_drift(program, capture, sample_rate, global_offset, locations)

    seg_w = program.segment("sweep_w")
    seg_t = program.segment("sweep_t")
    epsilon = drift.epsilon_ppm / 1e6
    # Deconvolve both sweeps anchored at their SCHEDULE window (with a shared
    # pre-guard) so relative timing survives (the aligner relies on this); the
    # measured ε is divided out of the reference so drift can't smear the IR.
    woofer_full_ir, pre_w = _deconvolve_window(
        capture, seg_w, global_offset + seg_w.start_sample, sample_rate,
        epsilon=epsilon,
    )
    tweeter_full_ir, pre_t = _deconvolve_window(
        capture, seg_t, global_offset + seg_t.start_sample, sample_rate,
        epsilon=epsilon,
    )
    pre_samples = min(pre_w, pre_t)
    n_fft = _n_fft_for(woofer_full_ir, tweeter_full_ir)

    # Primary responses stay EXACTLY first-occurrence-derived — today's
    # semantics, byte-identical (built from woofer_full_ir/tweeter_full_ir
    # exactly as before). Repeats (sweep-composition PR-A, #1668) are
    # additionally deconvolved/gated/TF'd and attached as diagnostic-only
    # `repeat_responses` on the matching primary; they never change a
    # primary's own freqs_hz/magnitude_db/complex_tf/gating/snr/validity_floor.
    occurrences_by_role = _sweep_occurrences_by_role(locations)
    responses = tuple(
        replace(
            resp,
            repeat_responses=_repeat_driver_responses(
                program, capture, sample_rate, global_offset, epsilon,
                occurrences_by_role.get(resp.role, ()),
                role=resp.role,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
            ),
        )
        for resp in (
            _driver_response(
                seg_w.role, woofer_full_ir, sample_rate,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
                radiated_band_hz=_radiated_band_hz(seg_w),
                capture_segment=_raw_sweep_segment(
                    capture, seg_w, global_offset + seg_w.start_sample,
                ),
            ),
            _driver_response(
                seg_t.role, tweeter_full_ir, sample_rate,
                calibration=calibration, ambient_report=priors.ambient_report,
                fc_hz=fc_hz, n_fft=n_fft,
                radiated_band_hz=_radiated_band_hz(seg_t),
                capture_segment=_raw_sweep_segment(
                    capture, seg_t, global_offset + seg_t.start_sample,
                ),
            ),
        )
    )

    alignment = _estimate_alignment(
        capture, program, sample_rate, global_offset, drift.epsilon_ppm / 1e6,
        fc_hz, geometry, priors,
        woofer_full_ir=woofer_full_ir, tweeter_full_ir=tweeter_full_ir,
        pre_samples=pre_samples,
    )

    candidate, predicted_sum = _build_candidate(
        woofer_full_ir, tweeter_full_ir, sample_rate, n_fft, fc_hz,
        seg_w.role, seg_t.role, alignment, calibration,
        tweeter_sweep_lo_hz=seg_t.f1_hz, woofer_sweep_hi_hz=seg_w.f2_hz,
        woofer_sweep_lo_hz=seg_w.f1_hz, tweeter_sweep_hi_hz=seg_t.f2_hz,
        alignment_delay_bounds_us=priors.alignment_delay_bounds_us,
    )
    if candidate.alignment_seed_ripple_db is not None:
        alignment = replace(
            alignment,
            delay_us=candidate.delay_us,
            raw_delay_us=candidate.delay_us + alignment.parallax_us,
            seed_delay_us=alignment.delay_us,
            confidence_source="gcc_phat_seed",
        )
    # Per-capture behavioral-linearity evidence (design §5.2): a v2 MEASURE
    # program opens with a pre-pilot ambient window + a leading pilot pair;
    # legacy programs carry neither, so the verdicts stay ``None``
    # (byte-identical to the pre-v2 analysis).
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
        mic_tier=priors.mic_tier,
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
) -> tuple[CrossoverCandidate, tuple[np.ndarray, np.ndarray]]:
    freqs, W, gate_w = _aligned_branch_tf(woofer_full_ir, sample_rate, n_fft, calibration=calibration)
    _f2, T, gate_t = _aligned_branch_tf(tweeter_full_ir, sample_rate, n_fft, calibration=calibration)
    lo, hi = overlap_band_hz(
        fc_hz, tweeter_sweep_lo_hz=tweeter_sweep_lo_hz, woofer_sweep_hi_hz=woofer_sweep_hi_hz,
    )
    # Gating-consistent prediction (W6.9 forensics): ``_aligned_branch_tf`` now
    # reflection-gates each branch the same way ``_driver_response`` does, so a
    # branch near a reflective mic position can be valid only above a floor
    # HIGHER than the nominal Fc±1-oct band. Clamp every quantity derived from
    # W/T — the trim solve, the predicted sum's ripple — to the worse (higher)
    # of the two branches' floors, never silently trusting sub-floor bins.
    # If the floor consumes the whole band, `solve_branch_trims`/`_ripple_db` raise
    # ValueError on the now-empty mask — the existing catch-all seam in
    # `jasper.web.correction_crossover_v2` already classifies that as
    # `internal_error` (see its comment: "analyze/emit raise ValueError"), so
    # this degrades through an existing signal rather than a new reason code.
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
    # above (PR-L3): each branch on its own side of Fc, inside its OWN
    # excited-and-gated span, never the shared both-branches-excited overlap
    # — see solve_branch_trims. Each span is that branch's declared sweep
    # band, floored by the shared reflection floor (sub-floor bins stay
    # untrusted everywhere). A missing sweep bound falls back to the nominal
    # Fc-octave edge, exactly like overlap_band_hz's own None handling.
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
    # The frame ledger the 2026-07-27 forensics asked for: the level match's
    # own inputs, on every MEASURE analysis. Read beside the per-role
    # `target_level_db` in `correction.crossover_v2_linearization_giveback`
    # (the fit frame for the SAME capture) — a large disagreement between the
    # two is the signature of a level-frame defect.
    log_event(
        logger, "program_analysis.branch_level_match",
        woofer_role=woofer_role, tweeter_role=tweeter_role,
        fc_hz=round(float(fc_hz), 3),
        level_w_db=round(float(level_w), 3), level_t_db=round(float(level_t), 3),
        woofer_band_hz=(round(w_band_lo, 1), round(w_band_hi, 1)),
        tweeter_band_hz=(round(t_band_lo, 1), round(t_band_hi, 1)),
        trim_band_average_db=round(float(trim_t_band_average), 3),
    )
    # #1667: re-solve the tweeter trim for minimum summed-response ripple
    # instead of trusting the band-average level match on its own — see
    # solve_ripple_optimal_trim's docstring for why band-average is biased.
    # Guarded: a result implausibly far from the band-average seed is
    # distrusted and discarded (never a wild applied trim).
    #
    # ...but ONLY where summed ripple can express a level at all (PR-L3).
    # The scan's objective is the ripple of ``W + s·T`` over the SHARED
    # both-branches-excited band, and #1667's own corpus was entirely
    # tweeter-sweep-starts-at-Fc geometry, where that band is one-sided: the
    # woofer is 20+ dB down its skirt across it, so the sum is the tweeter
    # alone and its ripple barely responds to the tweeter's own gain. What
    # the scan "recovered" there (1.7-6.3 dB per #1667's table) was a slice
    # of the band-average bias PR-L3 has now removed at the source, and with
    # an unbiased seed the same objective pulls the OTHER way: replayed on
    # the archived 2026-07-25 JTS3 run-5 MEASURE capture it moved the trim
    # 7.9 dB back down (-12.368 → -20.268) and only the sanity guard stopped
    # it. A selector that cannot see the woofer must not set the woofer's
    # handoff level, so it is skipped rather than guarded on that geometry.
    ripple_band_straddles_fc = lo_clamped < fc_hz < hi
    if ripple_band_straddles_fc:
        trim_t_ripple, _ripple_t_ripple, _seed = solve_ripple_optimal_trim(
            freqs, W, T, fc_hz,
            lo_hz=lo_clamped, hi_hz=hi,
            seed_trim_db=trim_t_band_average,
            trim_w_db=trim_w,
            sign=alignment.polarity_sign,
        )
        if abs(trim_t_ripple - trim_t_band_average) > RIPPLE_TRIM_SANITY_MARGIN_DB:
            log_event(
                logger, "program_analysis.ripple_trim_rejected",
                level=logging.WARNING,
                woofer_role=woofer_role, tweeter_role=tweeter_role,
                band_average_trim_db=round(trim_t_band_average, 3),
                ripple_optimal_trim_db=round(trim_t_ripple, 3),
                margin_db=RIPPLE_TRIM_SANITY_MARGIN_DB,
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
    delay_us = alignment.delay_us
    seed_ripple_db = None
    flatness_improvement_db = None
    anchor_delay_us = None
    snap_delta_us = None
    snap_found = False
    if (
        alignment.status == ALIGNMENT_OK
        and alignment_delay_bounds_us is not None
        and alignment.anchor_delay_us is not None
    ):
        # The aligner single-sourced the physical peak-gap anchor (raw argmax gap
        # − inter-sweep drift + parallax, in the signed frame; methodology §10,
        # 2026-07-22). Derive the applied anchor and the argmax-referenced
        # residual base FROM it — never recompute the argmax here, which would be
        # a parallel computation of one load-bearing frame decision.
        anchor_delay_us = float(alignment.anchor_delay_us)
        # Gated local-peak snap owns the fine step: the aligner already snapped
        # this anchor to the nearest local maximum of the SAME GCC-PHAT
        # correlation within ±(period/6) at Fc, or ruled one out. No local peak
        # in radius ⇒ keep the bare anchor; the snap is bounded closed-form, so
        # nothing can rail here. The declared `alignment_delay_bounds_us`
        # plausibility rail (Fix 3) still applies to the final `delay_us`
        # downstream in `crossover_v2_flow`.
        if alignment.snapped_delay_us is not None:
            delay_us = float(alignment.snapped_delay_us)
            snap_found = True
        else:
            delay_us = anchor_delay_us
        snap_delta_us = delay_us - anchor_delay_us
        # Flatness demoted to evidence (methodology §10): the summed ripple AT
        # the anchor and AT the snapped selection, in the argmax-referenced frame
        # (`summed_model_residual_delay_us`; the anchor residual is exactly 0).
        # Never a selector, so `flatness_improvement_db` can be
        # slightly negative — the snap is chosen for lobe-correctness, not ripple.
        band = (freqs >= lo_clamped) & (freqs <= hi)
        if np.any(band):
            freqs_band = freqs[band]
            W_band = W[band]
            T_band = T[band]

            def _ripple_at(candidate_delay_us: float) -> float:
                summed = predicted_branch_sum(
                    W_band, T_band, trim_w, trim_t, alignment.polarity_sign,
                    freqs_hz=freqs_band,
                    residual_delay_us=summed_model_residual_delay_us(
                        anchor_delay_us, candidate_delay_us,
                    ),
                )
                return _ripple_db(freqs_band, summed, lo_clamped, hi)

            anchor_ripple_db = _ripple_at(anchor_delay_us)
            selected_ripple_db = _ripple_at(delay_us)
            if math.isfinite(anchor_ripple_db) and math.isfinite(selected_ripple_db):
                seed_ripple_db = anchor_ripple_db
                flatness_improvement_db = anchor_ripple_db - selected_ripple_db
    # TWO sums, two questions, two owners. They were one call until rung P3
    # (R10b), which conflated a capture-quality measure with a model of the
    # speaker.
    #
    # `predicted_aligned` — the flattest-achievable, INDEPENDENTLY ALIGNED sum
    # (the old design §5.6.6 reference). It answers "how coherently can this
    # capture's two branches sum at all?", which is a property of the
    # measurement and not of the delay selection. It is the ONLY input to
    # `predicted_ripple_db`, and therefore to `crossover_v2_flow`'s G1
    # `MEASURE_PREDICTED_RIPPLE_CEILING_DB` gate, whose threshold that constant
    # documents as calibrated against a fixed 2026-07-22 hardware corpus scored
    # on THIS metric — the zero-residual ripple, not the delay-carrying one.
    #
    # Why the gate keeps this frame: a candidate's own committed delay can LOWER
    # its ripple, so pointing the veto at a delay-carrying curve would let a
    # capture whose branches sum incoherently be carried under the ceiling by
    # its own alignment. Measured on the banked 2026-07-30 JTS3 capture
    # (`captures/r10b-alignment-20260801/ripple_vs_residual_sweep.py`): sweeping
    # the residual across the ±(period/6) snap radius, 32 of 84 sampled
    # residuals come in BELOW the zero-residual 14.8831 dB, bottoming at
    # 14.0744 dB — and that capture sits 0.12 dB under the 15.0 dB ceiling, so
    # the 0.81 dB an alignment could buy is not a hypothetical margin. (An
    # earlier draft of this comment asserted the opposite — that a residual can
    # only ADD ripple, making the move merely "stricter". The sweep refutes it;
    # the real reason is evasion, not strictness.) Keeping the veto on a frame
    # no candidate parameter can move is what closes that path, and it is also
    # why this PR changes no adoption decision here.
    predicted_aligned = predicted_branch_sum(
        W,
        T,
        trim_w,
        trim_t,
        alignment.polarity_sign,
    )
    ripple = _ripple_db(freqs, predicted_aligned, lo_clamped, hi)
    # `predicted_applied` — the same two branches under the delay this
    # candidate actually COMMITS (rung P3: "make the summed model carry the
    # committed delay and trim"). This is what gets persisted as
    # `ProgramAnalysis.predicted_sum` and becomes VERIFY's tracking reference,
    # so that comparison finally grades measured-vs-the-applied-model — model
    # fidelity — instead of measured-against-a-target no realizable delay
    # produces. The trim was already carried; the delay was not.
    #
    # The term is the RESIDUAL relative to the argmax-referenced frame, never
    # the applied delay itself — `summed_model_residual_delay_us` owns that
    # derivation and its docstring carries the double-count hazard. On the
    # anchor-primary path this is exactly `snap_delta_us`; at the bare anchor
    # it is 0.0 and this call is bit-identical to `predicted_aligned`.
    #
    # Read off `alignment` rather than the local `anchor_delay_us`, and gated
    # on the aligner's own status rather than the snap block's condition: a
    # trustworthy anchor is available whenever `status == ALIGNMENT_OK`, while
    # the snap block additionally needs the DECLARED plausibility bounds. Those
    # are different questions, and a preset that declares no `delay_range_ms`
    # still applies `alignment.delay_us` (`MeasurementPriors.
    # alignment_delay_bounds_us`: "None keeps GCC as the applied-delay
    # estimate"), so its model should carry that delay too. The status gate is
    # what keeps a direct caller's hand-built refused estimate — which
    # `crossover_v2_flow.alignment_to_candidate_fields` turns into a
    # trims-only, NO-delay apply — from being modelled as though a delay ran.
    residual_delay_us = summed_model_residual_delay_us(
        alignment.anchor_delay_us if alignment.status == ALIGNMENT_OK else None,
        delay_us,
    )
    predicted_applied = predicted_branch_sum(
        W,
        T,
        trim_w,
        trim_t,
        alignment.polarity_sign,
        freqs_hz=freqs,
        residual_delay_us=residual_delay_us,
    )
    predicted_db = 20.0 * np.log10(np.maximum(np.abs(predicted_applied), 1e-12))
    candidate = CrossoverCandidate(
        trim_db={woofer_role: trim_w, tweeter_role: trim_t},
        polarity=alignment.polarity,
        delay_us=delay_us,
        predicted_ripple_db=ripple,
        confidence=alignment.confidence,
        alignment_seed_ripple_db=seed_ripple_db,
        flatness_improvement_db=flatness_improvement_db,
        anchor_delay_us=anchor_delay_us,
        snap_delta_us=snap_delta_us,
        snap_found=snap_found,
        trim_band_average_db={woofer_role: trim_w, tweeter_role: trim_t_band_average},
    )
    return candidate, (freqs, predicted_db)


def _analyze_verify(
    program, capture, sample_rate, global_offset, locations,
    calibration, priors,
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
    # INTERPRETATION CALL (B), flat-linearization productization plan: the
    # tracking comparator below — measured-vs-``priors.predicted_sum`` on THIS
    # capture's own grid — stays a construction of its own, deliberately NOT
    # re-based onto the spatial cloud's shared spec curve the way every
    # spec-facing gauge was in PR-5.
    #
    # It answers a different question. "Did apply do what the model
    # predicted?" is a claim about one capture against one prediction built
    # from the SAME single design-axis position, and its whole value is that
    # both sides share that geometry: the predicted sum was composed from the
    # MEASURE branches captured there, so a divergence is the applied graph
    # misbehaving and nothing else. "Is the speaker flat?" is a claim about
    # the speaker, which is why it is graded on the cloud (plan fundamental 1:
    # the cloud IS the measurement). Feeding the cloud's spatially-averaged
    # curve into this comparator would compare a multi-position average
    # against a single-position prediction and read the spatial variation the
    # cloud exists to sample as a tracking error — a false failure of the one
    # gate in this flow that DOES gate (``_consume_verify`` →
    # ``max_db_notch_excluded``). Collapsing the two conflates the questions;
    # keeping them apart is what lets the spec-facing SSOT land without
    # changing what gates today.
    ripple = None
    tracking = None
    tracking_curve = None
    if fc_hz is not None:
        lo, hi = overlap_band_hz(
            fc_hz,
            tweeter_sweep_lo_hz=priors.measure_tweeter_sweep_lo_hz,
            woofer_sweep_hi_hz=priors.measure_woofer_sweep_hi_hz,
        )
        ripple = _ripple_db(summed.freqs_hz, summed.complex_tf, lo, hi)
        if priors.predicted_sum is not None:
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
            # Validity-floor clamp (W6.9 forensics, 2026-07-19): this capture's
            # OWN reflection gate (`summed.validity_floor_hz`, from the same
            # `_driver_response` call above) can be tighter than the nominal
            # Fc±1-oct band at a reflective mic position — bins below that
            # floor are not a measurement, they're an artifact of a truncated
            # gate window (gating.f_valid_floor_hz), so they must not decide
            # PASS/FAIL either way. This generalizes the W6.7 notch exclusion
            # from "deep predicted notch" to "below measurement validity": the
            # W6 run-7/8 hardware failures were a fixed 65 ms prediction
            # window baking a desk-bounce reflection into the predicted sum's
            # sub-floor region, invisible to the notch-exclusion rule because
            # the false notch wasn't always deep enough to trip it. Applies to
            # BOTH rms and max, and to the notch-exclusion bin set — the two
            # exclusions compose (clamp first, then still exclude a genuine
            # deep predicted notch above the floor).
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
            # fields only — never consumed by the gate. What ``rms_db``/
            # ``max_db`` used to mean before the floor clamp landed.
            raw_rms, raw_max = analysis_mod.tracking_error_db(
                summed.freqs_hz, measured_db, predicted_db, (lo, hi),
            )
            # PR-L5: hand the delta probe the very curves these scalars
            # were reduced from, so it grades one comparison, not a second.
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
            # FRAME DISCIPLINE (rung P1). The two curves above are not in the
            # same frame and never were: ``predicted_db`` is an ON-AXIS
            # two-branch model composed from the MEASURE sitting, and
            # ``measured_db`` is an IN-ROOM gated point measurement from the
            # VERIFY sitting. Differencing them raw cannot tell instrument
            # frame from model error — on the 2026-07-29 corpus a single
            # −0.79 dB/octave tilt between exactly these two frames accounted
            # for 84 % of the "predictions are 2.02× optimistic" headline
            # (first-principles panel W1 / CC-1). So the frame is fitted, the
            # two terms are disclosed, and the residual is reported BOTH ways.
            #
            # Nothing above changes. ``max_db_notch_excluded`` is still what
            # gates (``crossover_v2_flow._verify_verdict``) and every raw
            # scalar is byte-identical to what it was before this block
            # existed: a measured tilt is EVIDENCE, not permission to re-grade.
            # Attributing it — directivity? mic? sitting? — needs an instrument
            # this fit does not have, and until it is attributed the raw number
            # is still the honest one to refuse on.
            #
            # FITTED OVER THE BINS THIS COMPARISON TRUSTS — the validity-floor
            # clamped band MINUS the deep-predicted-notch bins the gating
            # comparator already refuses to grade. Not a nicety: inside a
            # modelled notch the depth is hypersensitive to sub-dB branch
            # differences (the W6.7 finding the exclusion exists for), and a
            # straight line drawn through one lets the notch lever the slope.
            # On a 25 dB notch at a band edge an injected −0.800 dB/octave
            # frame was recovered as **+0.226** — the wrong sign — and would
            # then have been "removed" from the residual as instrument tilt.
            # The mask comes from ``notch_excluded_band_mask``, the same and
            # only owner of that bin choice, never re-derived here.
            #
            # This REDUCES the lever; it does not remove it. The exclusion
            # bounds a notch's DEPTH (12 dB) and says nothing about its skirt
            # WIDTH, so a wide surviving skirt still biases the estimate: on
            # this path, 1/6-octave 25 dB edge notch, the whole-band fit reads
            # +5.72 and the trusted-bin fit +0.31 against a −0.800 truth — 18×
            # better and still the wrong sign. Do NOT read a disclosed tilt as
            # trustworthy over a notch-heavy prediction, and do not add a
            # "tilt-removed ≤ raw" assertion anywhere: it is not a theorem.
            # Widening the fit-side margin is a threshold decision — issue
            # #1990 carries the numbers and the options.
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
            # the very locals published above, not recomputed — so the
            # disclosure cannot state a different raw number than the gate
            # reads (pinned by a test).
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
    # pilot pair (design §5.2, issue #1810) so the post-apply capture carries
    # its own behavioral-linearity evidence AND the noise floor needed to
    # trust it; legacy VERIFY programs carry neither and the verdicts stay
    # ``None``.
    pilots, linearity_ok, channel_map_ok, pilot_snr_ok = _pilot_verdicts(
        program, capture, sample_rate, locations, global_offset=global_offset,
    )
    # Capture integrity (issue #1971). Computed on EVERY verify-shaped
    # analysis — the tracking comparison above is exactly the thing a spliced
    # or clipped recording invalidates, and until this existed nothing on this
    # path ever looked.
    integrity = _verify_capture_integrity(program, sample_rate, locations)
    return ProgramAnalysis(
        phase=program.phase,
        program_id=program.program_id,
        locations=tuple(locations),
        summed_response=summed,
        summed_ripple_db=ripple,
        verify_tracking=tracking,
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


def _gate_floor_source_of(response: "DriverResponse | None") -> str | None:
    """WHY this capture's gate window is what it is (issue #1966).

    ``window_ms`` alone cannot distinguish the two states the gate can end
    in, and they print identically: :data:`~jasper.audio_measurement.gating.
    FLOOR_MEASURED` means a reflection onset was found and the window stops
    at it, while :data:`~jasper.audio_measurement.gating.FLOOR_SEARCH_BOUND`
    means the search ran to :data:`~jasper.audio_measurement.gating.
    SEARCH_T_MAX_MS` without finding one and the window was CAPPED there.
    A 7 ms window means "reflections removed" in the first case and "no
    reflection found" in the second; the whole 2026-07-30 corpus sat at the
    bound, and the record could not say so because this field was computed
    by the gate and dropped here.

    ``None`` — an ungateable capture (silent/NaN, or no room after the
    direct peak to search), matching ``gating``'s own unknown-vs-value rule.
    """
    if response is None:
        return None
    source = response.gating.get("floor_source") if response.gating else None
    return str(source) if isinstance(source, str) else None


def _gate_disclosure_of(response: "DriverResponse | None") -> str | None:
    """The gate's provenance as a SENTENCE, for the retained sidecar (#1966).

    The enum beside it (:func:`_gate_floor_source_of`) is the machine
    answer; this is the one a person reading the dump gets, and it is
    rendered — never composed — here: the copy has exactly one writer,
    :func:`jasper.audio_measurement.gate_disclosure.describe_gate`.
    """
    if response is None or not response.gating:
        return None
    return gate_disclosure.describe_gate(response.gating)


def analysis_diagnostic_summary(analysis: Any) -> dict[str, Any]:
    """Flat, JSON-safe numeric diagnostics from one :class:`ProgramAnalysis`.

    The operator capture-retention sidecar (``jasper.web.correction_crossover_v2``
    ``_maybe_retain_capture``) attaches this to every retained WAV so the clip
    is self-describing without replaying the analysis. Reads only fields
    ``ProgramAnalysis``/its nested dataclasses already carry — nothing here is
    recomputed. Per-driver/per-pilot fields key off each entry's OWN ``role``
    string (whatever the program declared — "woofer"/"tweeter" in production)
    rather than a hardcoded label, since this runs at the analyze seam, before
    the v2 conductor's role mapping exists.

    Deliberately duck-typed (``analysis: Any``) and defensive throughout: this
    is called from a best-effort retention path that must never raise even if
    a test double stands in for a real ``ProgramAnalysis`` (see
    ``bind_production_analyze``'s own tests, which monkeypatch
    ``analyze_program_capture`` to return a bare string) — every field access
    goes through ``getattr(..., None)`` so a malformed/foreign ``analysis``
    degrades to an emptier summary rather than raising past the caller's
    guard.
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
        # WHICH bound tripped + the discrete step, when one is resolved
        # (#1765) — a glitched capture's `epsilon_ppm` above is an artefact of
        # that step, not a drift measurement. See DriftEstimate's docstring.
        out["glitch_inputs"] = ",".join(getattr(drift, "glitch_inputs", ()) or ())
        # #1839: `discontinuity_samples` is `DISCONTINUITY_UNRESOLVED` (a
        # `str`, not a number) when the located sweeps weren't trustworthy
        # enough to fit a step from — `float()` would raise on that value,
        # which this duck-typed, must-never-raise summary cannot afford.
        discontinuity = getattr(drift, "discontinuity_samples", 0.0)
        out["discontinuity_samples"] = (
            round(float(discontinuity), 3)
            if isinstance(discontinuity, (int, float))
            else discontinuity
        )
        out["discontinuity_after_segment"] = getattr(
            drift, "discontinuity_after_segment", "",
        )
        # Diagnostic-only, never gated (sweep-composition PR-A, #1668) — see
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

    candidate = getattr(analysis, "candidate", None)
    if candidate is not None:
        out["predicted_ripple_db"] = round(float(candidate.predicted_ripple_db), 4)
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
            out[f"{role}_snr_verdict"] = worst.get("verdict")

    for pilot in getattr(analysis, "pilots", None) or ():
        role = pilot.role
        snr_db = getattr(pilot, "snr_db", math.inf)
        out[f"{role}_pilot_snr_db"] = round(snr_db, 2) if math.isfinite(snr_db) else None
        out[f"{role}_captured_delta_db"] = round(float(pilot.captured_delta_db), 3)
        out[f"{role}_programmed_delta_db"] = round(float(pilot.programmed_delta_db), 3)
        out[f"{role}_channel_map_target_rise_db"] = pilot.channel_map_target_rise_db
        out[f"{role}_channel_map_cross_rise_db"] = pilot.channel_map_cross_rise_db

    gain_plan = getattr(analysis, "gain_plan", None)
    if gain_plan is not None:
        out["gain_plan_snr_floor_ok"] = gain_plan.snr_floor_ok
        out["gain_plan_predicted_peak_dbfs"] = round(
            float(gain_plan.predicted_peak_dbfs), 3
        )
        # #1825: the per-role MEASURE level solve, flattened one field per
        # role so the forensic dump reads like every other per-role block
        # above it.
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

    # VERIFY capture integrity (#1971), flattened one field per fact like every
    # other block here. Absent — not empty — on CHECK/MEASURE, whose glitch
    # verdict is the ``drift`` block above; a retained VERIFY clip that carries
    # neither block is one taken before this record existed.
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
        # Frame discipline (rung P1). A retained clip is the forensic record of
        # ONE comparison, and the frame is half of what that comparison was, so
        # the frame's terms and the tilt-removed grades ride the sidecar BESIDE
        # the raw scalars above — never instead of them. Flattened one field
        # per term like every other per-subject block in this summary. Present
        # with ``None`` terms when the comparison ran but no frame could be
        # fitted; absent only when no tracking comparison happened at all.
        #
        # NAMING, deliberately not aligned with the household-facing record:
        # here the twin is ``max_db_notch_excluded_tilt_removed`` because it
        # sits beside ``max_db_notch_excluded`` and a forensic dump should pair
        # by name. The durable ``verify.frame`` block calls the same number
        # ``max_db_tilt_removed`` because on that surface the gated max is
        # already spelled ``max_db`` (``_verify_evidence_from_tracking``).
        # Each record uses its own neighbourhood's vocabulary; the number is
        # one number, written once, in ``_analyze_verify``.
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
