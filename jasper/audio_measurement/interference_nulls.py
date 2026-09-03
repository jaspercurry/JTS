# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Interference-null identification — the orthogonal honesty gate.

``identify_interference_nulls(combined, band_hz=...) -> InterferenceNullReport``
answers one question about a spatial cloud: **which dips in this speaker's
measured response are interference nulls from a delayed copy of its own sound,
and therefore uncorrectable by EQ?**

It exists because the combiner's power-mean-vs-median screen
(:mod:`jasper.audio_measurement.spatial_combine`) flags bins where positions
*disagree*, so it is structurally blind to a null every position sees. On the
S0 session that screen excluded 0 of 5462 bins in 8-16 kHz — a +1.27 dB
power-vs-median gap against its own >2 dB trigger — while a source-fixed comb
sat inside that band cutting 5-7 dB nulls. Position-invariance says "this is
real"; it does not say "this is correctable". The two instruments are
deliberately independent: consumers run both, plus ``geometry.locked``, and
take the union.

The method, and the two independent instruments it insists agree:

1. **Candidate arrival** — the cloud's per-position echo diagnostics, admitted
   and clustered by ``spatial_combine``'s own tolerances. A candidate needs at
   least ``MIN_CORROBORATING_POSITIONS`` positions.
2. **Candidate nulls** — local minima of the combined 1/6-octave diagnostic
   curve inside the caller's band, each with a depth against its own two
   flanking maxima (``NULL_DEPTH_STATISTIC`` owns which curve is read where).
3. **Depth-ceiling acquittal** — a two-path sum with reflection ratio ``r``
   cannot cut a null deeper than ``20*log10((1+r)/(1-r))``, so a dip deeper
   than the candidate arrival's ceiling plus ``DEPTH_CEILING_MARGIN_DB``
   cannot be that arrival and is refused attribution *before* the ladder is
   fitted. Acquitted is not excluded: the dip is left alone.
4. **Ladder fit** — the best single-tau ladder ``f_n = (n + 1/2) / tau`` with
   **tau free**, requiring at least ``MIN_LADDER_RUNGS`` *consecutive* rungs.
5. **Corroboration, both ways** — the fitted ``tau`` within
   ``LADDER_ARRIVAL_TOLERANCE`` of the arrival's, and the ``r`` the null depths
   imply within ``R_AGREEMENT_TOLERANCE`` of the ``r`` the arrival's envelope
   measured. Frequency- and time-domain estimators that never see each other's
   answer.
6. **Classification** — per rung, ``position_invariant`` when that null is
   individually present at at least ``POSITION_PRESENCE_FRACTION`` of the
   cloud's positions; the report's roll-up earns the word only when every rung
   did. Anything refused above is ``insufficient_evidence`` with a reason.

**What ``position_invariant`` does and does not claim.** It is a threshold, not
"every position" — the exact counts ride every record
(``positions_present`` / ``positions_total``), and a sentence claiming "at every
position" must read those. And it cannot say where the null comes from:
within one session, an origin that travels with the speaker and a room path
that did not change while the session ran are indistinguishable. The vocabulary
here is about the *evidence*; nothing in this module knows what a horn is.

**Detection only.** Nothing here removes an echo or fills a null — the
guardrail is "no EQ of interference-flagged bins, ever; they are reported
instead", which is why the output is a registry of *reasons* carrying an
identification's entire supporting arithmetic.

Pure computation: numpy plus :mod:`jasper.audio_measurement.spatial_combine`.
No I/O, no logging, no globals, no randomness, no product policy.

**Every threshold below is calibrated on one speaker** — the JTS3 cdhorn, one
evening, three geometries (the S0 corpus). Each constant states the population
it was measured on and its headroom; several have a positive population and no
measured negative one, and say so. Read the constant, not this paragraph.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

import numpy as np

from jasper.audio_measurement.spatial_combine import (
    ECHO_CONFIDENCE_FLOOR,
    GEOMETRY_CLUSTER_TOLERANCE,
    CombinedResponse,
    EchoDiagnostic,
    merged_true_intervals,
    usable_echo_estimates,
)

# --------------------------------------------------------------------------- #
# Null-finding tuning
# --------------------------------------------------------------------------- #

# The depth statistic, and why it reads two different curves. For a candidate
# minimum located on the combined 1/6-octave diagnostic curve:
#
#     depth_db = (power mean of the DIAGNOSTIC curve at the two flanking
#                 maxima) - (the UNSMOOTHED power mean at the minimum's bin)
#
# **The null must be read unsmoothed.** A fractional-octave window whose width
# is a real fraction of the comb period fills the null it is measuring: on
# synthetic two-path combs, 1/6-octave smoothing raises the null bottom by
# 0.13-5.98 dB, so a depth read smoothed at both ends reports 3.7 dB where the
# truth is 10.7 dB.
#
# **The flank may be read smoothed, and should be.** The same sweep shows
# smoothing lowers a comb PEAK by only 0.027-1.044 dB — a null is a narrow
# notch, a peak a broad arch — so reading the flanking maxima smoothed costs at
# most ~1 dB of depth while removing a single unsmoothed bin's variance. On the
# S0 desk-edge leg that is the difference between the genuine 11.6 kHz rung
# reading an impossible 1.02 dB above its arrival's physical ceiling and
# reading 0.27 dB above it.
#
# "Unsmoothed" means no fractional-octave smoothing, NOT the raw capture grid:
# ``combine_positions`` block-averages down to ``MAX_ANALYSIS_BINS`` first
# (~1.465 Hz on the S0 corpus, three orders of magnitude under the 3347 Hz comb
# period being measured). Lifting that cap moves nothing this statistic feeds by
# more than hundredths of a dB —
# test_the_analysis_grid_cap_costs_hundredths_of_a_db_end_to_end asserts every
# move, and is the live authority.
#
# **So the statistic is a lower bound on the true depth**, understated by at
# most the peak-shave term. That direction composes: ``r_freq`` derived from it
# is a lower bound on r, so the agreement gate can only refuse a true
# identification, never manufacture one, and the ceiling test's margin below is
# calibrated with the bias in place.
NULL_DEPTH_STATISTIC = "flank(diagnostic) - null(unsmoothed)"

# How far a flanking-maximum search may run, in octaves either side of a
# candidate minimum. The flanking maxima define the "local envelope" a depth is
# measured against, and beyond about half an octave the response's own shape —
# baffle step, driver rolloff, the crossover region — dominates whatever comb
# structure is present. Also bounded by the neighbouring candidate minima, so a
# search can never step over one null to use the far side of another.
#
# **Consequence, because it biases the acquittal test.** A dip broader than
# about one octave has its flanks clipped and its depth understated: the S0
# 1.8 kHz lobing dip reads 10.08 dB here against 10.71 dB with hand-picked
# wider flanks, a 0.63 dB penalty. Understating depth is the safe direction for
# identification and the UNSAFE one for acquittal, which is why
# ``DEPTH_CEILING_MARGIN_DB`` is calibrated on readings taken WITH the clip in
# place.
FLANK_SEARCH_MAX_OCT = 0.50

# Minimum depth for a candidate minimum to enter the ladder fit at all, in dB.
#
# **A materiality cut, not a classifier.** Across the four S0 cloud groupings
# (re-derived by test_s0_ladder_calibration_populations_bracket_the_constants):
#
#   identified rungs                    12 records, 2.72 - 6.84 dB
#   material minima no ladder explains   3 records, 2.88 - 4.15 dB
#   minima under this floor             36 records, -1.86 - 2.44 dB
#
# The first two populations overlap almost completely: no threshold on depth
# alone tells a comb rung from an ordinary dip, and what identifies a null is
# the ladder fit plus the two corroborations. This floor's job is narrower —
# keeping numerically-trivial minima out of the candidate set, where a handful
# can be assembled into a spurious ladder. Removed entirely, the S0 main leg's
# four hand-width-low positions produced a 3-rung "ladder" at tau 290 us built
# on two 1.3-1.6 dB dips instead of the real 8-16 kHz family.
#
# **2.5 dB is the plan's own 8-16 kHz tolerance**, not a measured gap: excluding
# a band costs the correction real bandwidth permanently, so the floor sits at
# the scale on which the spec judges a deviation to matter at all. It is a
# parameter (``min_depth_db``) because that is a product judgement, and this
# module holds no product policy.
DEFAULT_MIN_NULL_DEPTH_DB = 2.5

# --------------------------------------------------------------------------- #
# Ladder-fit tuning
# --------------------------------------------------------------------------- #

# How far a measured minimum may sit from a predicted rung and still match,
# **in units of the ladder's own rung spacing** (1/tau), not in percent of
# frequency.
#
# Rung spacing is the natural unit because a ladder's rungs are equally spaced
# in Hz: a tolerance in percent-of-frequency is uniformly selective at no n at
# all — it is tight at low n and, by n ~ 0.5/tol, wide enough that every
# frequency matches some rung and the fit becomes vacuous. In spacing units
# the window is the same fraction of the gap between rungs everywhere, so the
# constant needs no companion bound on n.
#
# **0.15 spacings, against a measured worst case of 0.0933** on the four S0
# cloud groupings, each fitted independently (re-derived by
# test_s0_ladder_calibration_populations_bracket_the_constants):
#
#   grouping                      rung errors (spacings)      worst
#   main leg, all 10 positions    0.0830 / 0.0264 / 0.0256    0.0830
#   main leg, tweeter height (6)  0.0818 / 0.0285 / 0.0232    0.0818
#   main leg, a hand-width low(4) 0.0933 / 0.0281 / 0.0299    0.0933
#   desk front edge (3)           0.0926 / 0.0350 / 0.0242    0.0926
#
# The worst reading is the n=2 rung on all four, at 0.0818-0.0933 against
# 0.0232-0.0350 for n=3 and n=4 — a systematic offset this module does not
# model. 0.15 clears it by 1.61x. Not centred in a gap: there is no second
# population to bound it from above, and a looser tolerance admits MORE
# ladders rather than a wrong one, which the consecutive-rung requirement and
# the two corroborations are what actually screen.
RUNG_MATCH_TOLERANCE_SPACINGS = 0.15

# Consecutive matched rungs required before a set of minima is a ladder.
#
# **Consecutive is the load-bearing word.** Two rungs n and n+1 pin tau from
# their *spacing*, which is the ladder's actual signature. A non-adjacent pair
# pins nothing: any tau dividing the gap explains it, so an arbitrary pair of
# dips can always be called rungs of something.
#
# **The counterfactual**, on the S0 main leg (ten positions) over 1.2-19 kHz
# with only ``_longest_consecutive`` replaced by ``sorted`` (re-derived by
# test_contiguity_is_what_keeps_the_1_8_khz_dip_out_of_the_registry):
#
#   shipped        tau 298.75 us, rungs [2, 3, 4] at 8646 / 11627 / 14977 Hz,
#                  24.18 % of the band excluded; the 1846.4 Hz minimum refused
#                  ``outside_contiguous_run`` at 5.14 dB
#   gaps allowed   tau 298.55 us, rungs [0, 2, 3, 4] at 1846 / 8646 / 11627 /
#                  14977 Hz, 26.99 % excluded — n=1 skipped entirely
#
# The extra rung is the 1.8 kHz lobing dip, a DIFFERENT mechanism from the comb
# — uncorrelated with it across positions, and physically impossible for the
# ~320 us arrival to have cut. Without contiguity the gate excludes it from
# correction as a comb rung and passes both corroborations while doing it
# (the deepest rung sets r_freq, so a wrong extra rung does not move it).
#
# Two regimes where the counterfactual does NOT appear, so the one above is not
# over-read: at 5-19 kHz the two agree exactly (the dip is out of band), and on
# the S0 ground-plane leg the mutation changes nothing at any band, that cloud
# being refused at ``no_corroborating_arrivals`` before the fit runs.
#
# Two, not three, because two adjacent rungs is the minimum that carries the
# spacing information; three would refuse a ladder whose band only holds two.
MIN_LADDER_RUNGS = 2

# The fitted ladder's tau must land within this *relative* distance of the
# corroborating arrival's tau.
#
# **It is spatial_combine's own clustering tolerance, reused rather than
# reinvented** — two delays this close are already "the same delay" to
# :func:`~jasper.audio_measurement.spatial_combine.assess_geometry`, and a
# second number meaning the same thing is a second number to drift.
#
# **The band must admit a measured gap.** On the S0 corpus the ladder tau sits
# systematically BELOW the directly measured arrival tau: -7.071 % (main leg,
# all 10), -6.671 % (tweeter height, 6), -7.540 % (a hand-width low, 4),
# -7.058 % (desk front edge, 3) — fitted taus of 297.96-298.90 us against
# arrival medians of 320.27-322.26 us, a real rim wave not being an ideal
# single-delay reflector. A band of the 1/6-octave smoothing bandwidth alone
# (about ±6 %) would refuse every one of the four; 0.15 clears the worst by
# 1.99x. ``tests/test_interference_nulls.py``'s four-way calibration table
# hard-asserts these figures and is the live authority.
#
# **Symmetric, though the measurement is not.** All four readings are negative
# and consistently so, but that direction is an observation about this speaker,
# not a rule: a one-sided band would bake one rim wave's behaviour into a gate
# that has to work for waveguide edges, desk bounces and enclosure diffraction
# it has never seen.
LADDER_ARRIVAL_TOLERANCE = GEOMETRY_CLUSTER_TOLERANCE

# Positions whose echo estimates must cluster before there is an arrival to
# corroborate against. Below this the module reports
# ``insufficient_evidence``: a lone estimate sits within any tolerance of its
# own median, so "it clustered" would be vacuous — the same reasoning
# ``GEOMETRY_MIN_CONFIDENT`` is set by, and the same value, for the same
# reason. Stated separately because the two gates could legitimately diverge:
# this one is about *attributing a null*, that one about telling a household
# to move the mic.
MIN_CORROBORATING_POSITIONS = 2

# --------------------------------------------------------------------------- #
# Corroboration tuning
# --------------------------------------------------------------------------- #

# Maximum disagreement between the two independent estimates of the
# reflection ratio r before the identification is refused.
#
# The two instruments never see each other's answer. **Time domain:** the
# arrival's level relative to the direct sound, from the analytic envelope
# (``EchoDiagnostic.strength_db``), r = 10**(strength_db/20). **Frequency
# domain:** the deepest matched rung's depth, inverted through the two-path
# null-depth relation r = (x-1)/(x+1) with x = 10**(depth_db/20). The deepest
# rung is used because the relation is a *ceiling* — every measurement
# mechanism in the chain (spatial averaging over dispersed taus, the flank's
# peak-shave, directivity-weighted r) can only make a null shallower — so the
# deepest matched rung is the least-attenuated view and every other rung would
# understate r further.
#
# **That bound is on the depth statistic against the *true* depth, not against
# the time-domain number**, and the table below shows why the distinction
# matters: the desk-edge row runs the other way, r_freq 0.3746 *above* r_time
# 0.3559. ``strength_db`` is an estimate too, and on three positions it
# carries enough error to flip the sign of the difference. So do not read this
# gate as one-sided; it is an absolute disagreement for a reason.
#
# **0.10, calibrated one-sided against a measured positive population** — the
# four S0 cloud groupings (re-derived by
# test_s0_ladder_calibration_populations_bracket_the_constants):
#
#   grouping                      r_time    r_freq    agreement
#   main leg, all 10 positions    0.3765    0.3438    0.0327
#   main leg, tweeter height (6)  0.3748    0.3479    0.0269
#   main leg, a hand-width low(4) 0.3785    0.3374    0.0410
#   desk front edge (3)           0.3559    0.3746    0.0187
#
# Worst 0.0410; 0.10 clears it by 2.44x. **There is no measured negative
# population** and this constant does not pretend to bisect a gap: the S0
# session produced exactly one real arrival-and-ladder pair, read four ways.
# What it does bound is real — 0.10 around r = 0.37 is about 2 dB of
# disagreement about how deep a null the arrival can cut — and the hazard it
# guards is guarded from the other side, independently, by the depth ceiling.
# The number to watch on other hardware is the positive population's WORST
# reading, not this threshold.
R_AGREEMENT_TOLERANCE = 0.10

# How far above the candidate arrival's physical null-depth ceiling a dip must
# read before it is refused attribution, in dB.
#
# The physics is exact and one-directional: a direct sound summed with one
# delayed copy at ratio r cannot cut a null deeper than
# ``20*log10((1+r)/(1-r))`` anywhere. A dip deeper than that **cannot** be
# that arrival — no averaging, no smoothing and no directivity makes a null
# deeper than the arithmetic allows. The margin exists for estimation error in
# r and for the depth statistic's own bias, not for the physics.
#
# The ceiling is computed from the **largest** r in the corroborating cluster,
# not the median: the claim being made is "no position's arrival could have
# cut this", so the loudest reflection the cloud actually measured is the
# right bound, and it is the conservative one — a bigger r means a deeper
# permitted null means fewer acquittals.
#
# **1.25 dB, inside a measured 2.81 dB gap** on the S0 corpus at the shipped
# depth statistic (re-derived by test_s0_acquits_the_1_8_khz_dip_by_depth_ceiling
# and test_s0_ladder_calibration_populations_bracket_the_constants):
#
# * **Must not acquit — 12 genuine rungs**, the 8-16 kHz family across the four
#   cloud groupings, reading -4.05 to +0.27 dB relative to their own ceiling.
#   The desk-edge leg's 11.6 kHz rung genuinely reads 0.27 dB OVER its ceiling
#   — what a 3-position cloud's noise looks like against a bound this tight,
#   and why the margin cannot be zero.
# * **Must acquit — the S0 1.8 kHz lobing dip**, +3.08 dB over the ceiling on
#   the six tweeter-height positions (10.08 dB measured against a 7.01 dB
#   ceiling for r = 0.3829), physically impossible for the ~320 us arrival to
#   have caused.
#
# 1.25 sits 0.98 dB above the genuine population's ceiling and 1.83 dB below
# the acquittal case — deliberately NOT centred, since the binding side is the
# 0.98 dB: an acquittal is what REMOVES a null from the registry. It is a
# 2.81 dB gap between 12 readings and 1, from one speaker and one session; that
# is thin, and the acquittal population is the one to widen first.
#
# An acquitted dip is **left alone**: it is refused attribution, recorded with
# its reason, and *not* excluded by this gate. It may well be a real defect —
# the S0 one is — but it is not this instrument's to name, and the
# power-vs-median screen catches that particular one independently.
DEPTH_CEILING_MARGIN_DB = 1.25

# --------------------------------------------------------------------------- #
# Classification tuning
# --------------------------------------------------------------------------- #

# Fraction of the cloud's positions at which an identified null must be
# individually present before it is called ``position_invariant``.
#
# Presence is measured with the *same* statistic and the *same* floor as the
# combined curve's, on that position's own diagnostic curve
# (``CombinedResponse.per_position_diag_db``) and its own unsmoothed one — one
# construction, so a per-position reading and a combined reading are
# comparable numbers rather than two conventions.
#
# **0.70, measured against 1.00 / 1.00 / 0.80** — the three identified rungs of
# the S0 main leg's ten-position cloud, re-derived by
# test_s0_main_leg_family_is_position_invariant. The 0.80 is the 15 kHz rung,
# located at all ten positions and missing at two only because its depth there
# (2.40 and 2.49 dB) falls a shade under the 2.5 dB materiality floor.
#
# **On a small cloud this fraction is coarsely quantised**: at three positions
# the only values are 0, 1/3, 2/3 and 1, so 0.70 means "all three" there. The
# regime it was chosen for is a 8-12 position cloud.
POSITION_PRESENCE_FRACTION = 0.70

# --------------------------------------------------------------------------- #
# Runaway-exclusion guard
# --------------------------------------------------------------------------- #

# The largest fraction of the analysis band's bins this gate may exclude
# before it refuses to identify anything at all.
#
# **What it guards.** Every identification costs the correction real
# bandwidth, permanently: PR-6 zeroes the fit's allowed depth on these bins
# and the spec evaluator drops them from grading. A gate that carved most of
# the band out of both would fail in a way that still *looks* like a clean
# report, so the failure needs a bound rather than a reviewer.
#
# **0.65, set above what a single-tau ladder can legitimately reach — a
# backstop, not a tuning knob.** Two populations:
#
# * **Real captures — 23.85 % to 30.74 %**: the S0 families across the four
#   cloud groupings, widest on the main leg over 5-19 kHz. Hard-asserted by
#   test_s0_exclusion_stays_far_below_the_runaway_cap, the live authority.
# * **Synthetic, deliberately pushed — ceiling 48.24 %**, over a committed grid
#   of two-path clouds (tau 208-625 us × r 0.15-0.80 × three analysis bands),
#   re-derived by test_the_runaway_exclusion_cap_holds_over_the_committed_grid.
#   The worst case is a dense ladder: six rungs of a 542 us comb in a 10 kHz
#   band.
#
# **That 48.24 % is not a runaway; it is the natural bound.** A null's
# half-depth width grows as its depth shrinks, approaching half a rung spacing
# either side as r falls, so a COMPLETE ladder's intervals approach the whole
# band from below — near 50 %. A guard at 0.50 would refuse a legitimate dense
# comb, and refusing means refusing EVERY identification in the report. So this
# constant fires on no input either population contains: it bounds a failure
# class not yet observed (a mis-fit, a widened interval, an overlap bug),
# chosen above what the physics allows rather than inside it.
#
# **When it binds, nothing is identified.** The report comes back empty with
# ``reason = REASON_EXCLUSION_CAP`` and the attempted fraction on
# ``excluded_fraction``, because dropping "the shallowest until it fits" would
# be an arbitrary ordering presented as a measurement. The other two honesty
# instruments are unaffected — they are separate gates and the consumer runs
# all three.
EXCLUSION_CAP_FRACTION = 0.65

# --------------------------------------------------------------------------- #
# Vocabulary
#
# Snake_case, self-identifying, stable — mirroring spatial_combine's
# ``REFUSAL_*`` and ``GEOMETRY_*`` slugs and linearization_envelope's
# ``ReasonCode``. Consumers gate on ``reason == ""`` / the classification
# constants, never on a specific refusal slug, so these can grow.
# --------------------------------------------------------------------------- #

CLASSIFICATION_POSITION_INVARIANT = "position_invariant"
CLASSIFICATION_POSITION_DEPENDENT = "position_dependent"
CLASSIFICATION_INSUFFICIENT_EVIDENCE = "insufficient_evidence"

# Report-level reasons — why nothing was identified. Listed in the order
# :func:`identify_interference_nulls` can emit them.
REASON_NO_PER_POSITION_CURVES = "no_per_position_curves"
REASON_NO_CORROBORATING_ARRIVALS = "no_corroborating_arrivals"
#: :func:`classify_dip_position_variance` only. A cross-position statistic
#: over one position is undefined — the same line
#: :func:`~jasper.audio_measurement.spatial_combine.combine_positions` draws
#: when it returns an empty ``band_spread`` below N=2.
REASON_TOO_FEW_POSITIONS = "too_few_positions"
REASON_NO_CANDIDATE_NULLS = "no_candidate_nulls"
REASON_NO_LADDER = "no_ladder"
REASON_LADDER_ARRIVAL_MISMATCH = "ladder_arrival_mismatch"
REASON_R_DISAGREEMENT = "r_disagreement"
REASON_EXCLUSION_CAP = "exclusion_cap_exceeded"

# Per-candidate reasons — why one measured minimum is not an identified null.
CANDIDATE_NOT_MEASURABLE = "no_flanking_maxima"
CANDIDATE_BELOW_MIN_DEPTH = "below_min_depth"
CANDIDATE_DEPTH_EXCEEDS_CEILING = "depth_exceeds_arrival_ceiling"
CANDIDATE_NO_MATCHING_RUNG = "no_matching_rung"
# One shape carries this slug loosely, and it is labelling only: a second
# candidate inside tolerance of an IN-RUN rung loses the tie in
# ``_assign_rungs`` and is reported here, where "another candidate was closer
# to the same rung" would describe it better. The refusal is right either way,
# and ``predicted_hz`` / ``rung_error_spacings`` keep the loss legible.
# Reaching it needs two candidates inside one 0.3-spacing rung-match window
# that both survived the 1/6-octave thinning, so ``(n + 0.65) / (n + 0.35)``
# must exceed ``2 ** (1/6)`` = 1.1225 — possible only at n <= 2. No corpus or
# synthetic case in this module's suite reaches it.
CANDIDATE_OUTSIDE_CONTIGUOUS_RUN = "outside_contiguous_run"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


def _frozen_evidence(record: object, evidence: Mapping[str, float]) -> None:
    """Replace ``record.evidence`` with an immutable copy, in ``__post_init__``.

    ``frozen=True`` stops the FIELD being rebound and says nothing about the
    mapping behind it, and these records are the audit trail. The ``dict()``
    copy is load-bearing: proxying the caller's mapping without copying leaves
    it mutable through THEIR reference.
    """
    object.__setattr__(record, "evidence", MappingProxyType(dict(evidence)))


@dataclass(frozen=True)
class IdentifiedNull:
    """One rung of an identified interference ladder.

    Every field is either a measurement or recomputable from ``evidence``, so a
    reader with the record in front of them can re-derive the verdict.

    Args:
      f_lo_hz: lower edge of the frequency interval this null excludes.
      f_hi_hz: upper edge. The interval is the contiguous run around the
        minimum where the diagnostic curve sits at or below half the null's
        *diagnostic-curve* depth beneath its flank baseline — the dip's
        half-depth width, bounded by its own two flanking maxima so it can
        never run away down a rolloff. Half-depth rather than the whole
        flank-to-flank span because the span includes the comb *peaks*, which
        are ordinary response the fit should still correct.
      f_center_hz: the located minimum, on the combined diagnostic curve.
      n: the rung index, ``f ~= (n + 1/2) / tau``.
      tau_us: the **fitted ladder** delay, in microseconds. The same value on
        every rung of one report — it is a property of the ladder, carried on
        each rung so a record stands alone.
      r_time: reflection ratio from the time domain, ``10**(strength_db/20)``
        of the corroborating cluster's median arrival strength.
      r_freq: reflection ratio from the frequency domain, inverted from the
        deepest matched rung's depth. Also a per-report value: it is the
        ladder's r, not this rung's, because shallower rungs understate it
        (see ``R_AGREEMENT_TOLERANCE``).
      agreement: ``abs(r_time - r_freq)``. Below ``R_AGREEMENT_TOLERANCE`` by
        construction — an identified null exists only if it was.
      depth_db: **this** rung's depth, by ``NULL_DEPTH_STATISTIC``. A lower
        bound on the true depth; see that constant.
      classification: ``CLASSIFICATION_POSITION_INVARIANT`` or
        ``CLASSIFICATION_POSITION_DEPENDENT``. Never
        ``CLASSIFICATION_INSUFFICIENT_EVIDENCE`` — that is a report-level
        verdict, and a report carrying it has no identified nulls at all.
      evidence: the supporting arithmetic, as plain floats, wrapped read-only
        in a ``MappingProxyType`` — a consumer serialising it wraps it in
        ``dict()`` first. Keys, all measurements: ``predicted_hz`` (the rung the
        fit put here), ``rung_error_spacings`` (how far the measurement sits
        from it, in rung spacings), ``flank_lo_hz`` / ``flank_hi_hz`` (where the
        local envelope was read), ``flank_baseline_db`` and ``null_level_db``
        (its two terms, so ``depth_db`` is recomputable), ``diag_depth_db`` (the
        same depth read entirely on the diagnostic curve — the smoothing-filled
        figure, which is what sets ``f_lo_hz``/``f_hi_hz``),
        ``depth_ceiling_db`` (what the arrival's r permits, so the acquittal
        that did NOT fire is auditable too), ``positions_present`` and
        ``positions_total``.
    """

    f_lo_hz: float
    f_hi_hz: float
    f_center_hz: float
    n: int
    tau_us: float
    r_time: float
    r_freq: float
    agreement: float
    depth_db: float
    classification: str
    evidence: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _frozen_evidence(self, self.evidence)


@dataclass(frozen=True)
class RefusedCandidate:
    """A measured minimum this gate declined to identify, and why.

    Refusals are output, not silence: the registry answers "why is this band
    excluded" AND "why is that dip not".

    Args:
      f_center_hz: the located minimum.
      depth_db: its depth by ``NULL_DEPTH_STATISTIC``, or 0.0 when the
        candidate was refused before a depth existed
        (``CANDIDATE_NOT_MEASURABLE``).
      reason: one of the ``CANDIDATE_*`` slugs.
      evidence: whatever the refusal turns on, so it is recomputable —
        ``depth_ceiling_db`` for an acquittal, ``min_depth_db`` for a
        materiality refusal, ``predicted_hz`` and ``rung_error_spacings`` when
        a rung was tried and missed.
    """

    f_center_hz: float
    depth_db: float
    reason: str
    evidence: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _frozen_evidence(self, self.evidence)


@dataclass(frozen=True)
class InterferenceNullReport:
    """The null registry — this gate's whole output.

    Read ``reason`` first. A non-empty ``reason`` means nothing was
    identified and ``classification`` is
    ``CLASSIFICATION_INSUFFICIENT_EVIDENCE``; ``nulls`` and
    ``excluded_bands_hz`` are then empty and every other field is
    diagnostic — populated as far as the method got, so a reader can see
    *where* it stopped rather than only that it did.

    Args:
      nulls: the identified rungs, ascending in frequency. Empty when
        ``reason`` is non-empty.
      excluded: per-bin mask on ``combined.freqs_hz``, True inside an
        identified null's interval. Full-length so it composes directly with
        ``CombinedResponse.excluded`` — the plan's honesty mask is the union
        of the two, consumed together with ``geometry.locked`` (the wiring
        contract, plan PR-4).
      excluded_bands_hz: ``excluded`` as merged ``(f_lo, f_hi)`` intervals,
        via :func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals`
        — the same owner the combiner's own intervals come from, so two
        exclusion lists a consumer unions are the same fact computed the same
        way.
      excluded_fraction: excluded bins as a fraction of the analysis band's
        bins. Reported whether or not the cap bound, for the reason
        ``lower_peak_ratio`` is reported on unrefused records in
        spatial_combine: the interesting reading is usually the one that did
        *not* trip.
      refusals: every candidate minimum this gate did not identify, in
        ascending frequency, each with its reason.
      reason: ``""`` when at least one null was identified; otherwise one of
        the ``REASON_*`` slugs.
      classification: the report-level verdict —
        ``CLASSIFICATION_INSUFFICIENT_EVIDENCE`` when ``reason`` is non-empty,
        otherwise ``position_invariant`` only when EVERY identified rung earned
        it, and ``position_dependent`` as soon as one did not. Conservative on
        purpose: over-claiming invariance is the defect class this program is
        about, and a consumer wanting per-rung nuance has it on every rung.
      band_hz: the analysis band actually applied — echoed back because a
        rung index, an exclusion fraction and a "no candidates" refusal are
        all only interpretable against the band they were computed in.
      tau_ladder_us: the fitted ladder delay, 0.0 when no ladder was fitted.
      arrival_tau_us: the corroborating cluster's median arrival delay, 0.0
        when the cluster was empty. On a ``no_corroborating_arrivals``
        refusal it still carries whatever the sub-minimum cluster held — a
        reader asking why the gate refused wants to see *what* was too thin,
        not only that something was.
      arrival_r_time: that cluster's median ``r``, under the same rule.
      arrival_r_max: its largest ``r`` — the one the depth ceiling is
        computed from — under the same rule.
      n_corroborating: how many positions were in the cluster.
      r_freq: the ladder's frequency-domain ``r``, 0.0 when no ladder.
      agreement: ``abs(arrival_r_time - r_freq)``, 0.0 when no ladder.
        Reported even when it is what caused the refusal — especially then.
      ladder_arrival_gap: ``tau_ladder / arrival_tau - 1``, signed. Negative
        means the ladder sits below the arrival, which is what all four S0
        groupings measured; see ``LADDER_ARRIVAL_TOLERANCE``. 0.0 when either
        term is missing.
      capped: True exactly when ``reason == REASON_EXCLUSION_CAP``. A
        separate bit because a consumer disclosing "this gate refused to
        report" wants to distinguish "found nothing" from "found too much",
        and they are very different messages.
      min_depth_db: the materiality floor actually applied.
      n_candidates: how many minima were located in band before any screen —
        the denominator for ``refusals``.
    """

    nulls: tuple[IdentifiedNull, ...]
    excluded: np.ndarray
    excluded_bands_hz: tuple[tuple[float, float], ...]
    excluded_fraction: float
    refusals: tuple[RefusedCandidate, ...]
    reason: str
    classification: str
    band_hz: tuple[float, float]
    tau_ladder_us: float
    arrival_tau_us: float
    arrival_r_time: float
    arrival_r_max: float
    n_corroborating: int
    r_freq: float
    agreement: float
    ladder_arrival_gap: float
    capped: bool
    min_depth_db: float
    n_candidates: int


# --------------------------------------------------------------------------- #
# Physics helpers
# --------------------------------------------------------------------------- #


def null_depth_ceiling_db(r: float) -> float:
    """Deepest null a direct sound plus one delayed copy at ratio ``r`` can cut.

    ``20*log10((1+r)/(1-r))`` — the peak-to-null range of ``|1 + r*e^(-jwt)|``.
    Exact, not empirical. ``r`` at or above 1.0 returns ``inf`` (a perfect
    cancellation has unbounded depth); ``r`` at or below 0 returns 0.0.

    The single owner of this relation, which appears three times in this
    module — the acquittal, the ``r_freq`` inversion below, and the
    ``depth_ceiling_db`` carried on every record.
    """
    if r <= 0.0:
        return 0.0
    if r >= 1.0:
        return float("inf")
    return float(20.0 * np.log10((1.0 + r) / (1.0 - r)))


def branch_gap_null_depth_ceiling_db(gap_db: float) -> float:
    """Deepest null two branches ``gap_db`` apart in level can cut, summed.

    ``-20*log10(1 - 10**(-gap/20))`` — the residual of ``|1 - 10**(-gap/20)|``
    when the quieter branch is inverted against the louder one. Distinct from
    :func:`null_depth_ceiling_db`, which is about one sound and a delayed COPY
    of it at ratio ``r``: this is about two SOURCES whose levels differ, and it
    is the bound a reverse-null confirmation is read against. A pair 10 dB apart
    cannot cancel deeper than about 3.3 dB however right the delay is, so a
    confirm run on unmatched branches measures its own level mismatch and calls
    it an alignment verdict.

    Disclosure, never a refusal: the number rides every null row beside the
    depth so a reader can tell a shallow null from a capped one. ``gap_db`` at
    or below 0 (a matched pair) returns ``inf`` — nothing bounds it — and a gap
    large enough that the quieter branch contributes nothing saturates at 0.0.
    """
    if gap_db <= 0.0:
        return float("inf")
    residual = 1.0 - 10.0 ** (-float(gap_db) / 20.0)
    if residual <= 0.0:
        return 0.0
    return float(-20.0 * np.log10(residual))


def reflection_ratio_from_depth(depth_db: float) -> float:
    """Invert :func:`null_depth_ceiling_db`: the ``r`` a null this deep implies.

    ``r = (x - 1) / (x + 1)`` with ``x = 10**(depth_db/20)``. Because the
    relation it inverts is a *ceiling*, the result is a **lower bound** on the
    true ``r`` whenever the measured depth is attenuated — which, by
    ``NULL_DEPTH_STATISTIC``, it always slightly is. A non-positive depth
    returns 0.0 rather than a negative ratio.
    """
    if depth_db <= 0.0:
        return 0.0
    x = 10.0 ** (depth_db / 20.0)
    return float((x - 1.0) / (x + 1.0))


def _power_mean_db(values: Sequence[float]) -> float:
    """dB level of the mean of the linear powers — this module's only
    averaging rule for levels, matching spatial_combine's estimator so a
    baseline computed here composes with a power mean computed there."""
    return float(10.0 * np.log10(np.mean([10.0 ** (v / 10.0) for v in values])))


# --------------------------------------------------------------------------- #
# Candidate location and depth
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Candidate:
    """One located minimum with its depth and interval. Internal."""

    f_hz: float
    index: int
    depth_db: float
    diag_depth_db: float
    baseline_db: float
    null_level_db: float
    flank_lo_hz: float
    flank_hi_hz: float
    lo_index: int
    hi_index: int
    f_lo_hz: float
    f_hi_hz: float


def _locate_minima(diag: np.ndarray, band_idx: np.ndarray, min_sep_oct: float,
                   freqs: np.ndarray) -> list[int]:
    """Local minima of ``diag`` inside the band, thinned to one per
    ``min_sep_oct``, keeping the lowest.

    Thinning is by *level*, deliberately not by depth: depth needs flanks,
    flanks need the neighbouring minima, and choosing minima by depth would
    close that loop. The separation is one smoothing window
    (``1/diag_fraction`` octaves) because two minima closer than the window
    that produced them are not independently resolved — the curve cannot tell
    them apart, so neither should this.
    """
    y = diag[band_idx]
    if y.size < 3:
        return []
    interior = np.flatnonzero((y[1:-1] <= y[:-2]) & (y[1:-1] < y[2:])) + 1
    kept: list[int] = []
    for p in sorted(interior, key=lambda q: (float(y[q]), int(q))):
        f = freqs[band_idx[p]]
        if any(abs(np.log2(f / freqs[band_idx[q]])) < min_sep_oct for q in kept):
            continue
        kept.append(int(p))
    return sorted(kept)


def _measure_candidates(
    freqs: np.ndarray,
    diag: np.ndarray,
    raw: np.ndarray,
    band_idx: np.ndarray,
    min_sep_oct: float,
) -> tuple[list[_Candidate], list[float]]:
    """Locate the band's minima and measure each one's depth and interval.

    Returns the measurable candidates plus the frequencies of the ones with
    no flanking maximum available on one side, which the caller records as
    ``CANDIDATE_NOT_MEASURABLE``.

    **When that happens, precisely.** ``_locate_minima`` returns interior
    indices only, so a neighbouring bin always exists; what can be missing is a
    bin inside the ``FLANK_SEARCH_MAX_OCT`` window, on a grid coarse enough that
    half an octave falls inside one bin. Shipped grids (~1.5 Hz after the
    combiner's decimation) never reach it, and neither does a top-octave rolloff
    — a monotonic run has no local minimum. The branch is for a degenerate
    CAPTURE, and it refuses rather than reading a flank from whichever bin
    happened to be nearest.
    """
    positions = _locate_minima(diag, band_idx, min_sep_oct, freqs)
    y = diag[band_idx]
    out: list[_Candidate] = []
    unmeasurable: list[float] = []
    for j, p in enumerate(positions):
        f0 = float(freqs[band_idx[p]])
        # Bound the flank search by the neighbouring minima (so a search
        # cannot step over one null to reach past another) and by
        # FLANK_SEARCH_MAX_OCT (so the envelope stays local).
        lo_bound = positions[j - 1] if j > 0 else 0
        hi_bound = positions[j + 1] if j + 1 < len(positions) else int(band_idx.size - 1)
        lo_bound = max(
            lo_bound,
            int(np.searchsorted(freqs[band_idx], f0 * 2.0**-FLANK_SEARCH_MAX_OCT)),
        )
        hi_bound = min(
            hi_bound,
            int(np.searchsorted(freqs[band_idx], f0 * 2.0**FLANK_SEARCH_MAX_OCT)),
        )
        if lo_bound >= p or hi_bound <= p:
            unmeasurable.append(f0)
            continue
        pl = lo_bound + int(np.argmax(y[lo_bound:p]))
        pr = p + 1 + int(np.argmax(y[p + 1 : hi_bound + 1]))
        i, il, ir = int(band_idx[p]), int(band_idx[pl]), int(band_idx[pr])
        baseline = _power_mean_db((float(diag[il]), float(diag[ir])))
        depth = baseline - float(raw[i])
        diag_depth = baseline - float(diag[i])
        # Half-depth width, walked on the diagnostic curve and bounded by the
        # two flanking maxima. Always non-empty: the minimum itself is in it.
        half = baseline - 0.5 * diag_depth
        a = p
        while a > pl and diag[band_idx[a - 1]] <= half:
            a -= 1
        b = p
        while b < pr and diag[band_idx[b + 1]] <= half:
            b += 1
        out.append(
            _Candidate(
                f_hz=f0,
                index=i,
                depth_db=float(depth),
                diag_depth_db=float(diag_depth),
                baseline_db=float(baseline),
                null_level_db=float(raw[i]),
                flank_lo_hz=float(freqs[il]),
                flank_hi_hz=float(freqs[ir]),
                lo_index=int(band_idx[a]),
                hi_index=int(band_idx[b]),
                f_lo_hz=float(freqs[band_idx[a]]),
                f_hi_hz=float(freqs[band_idx[b]]),
            )
        )
    return out, unmeasurable


# --------------------------------------------------------------------------- #
# Ladder fit
# --------------------------------------------------------------------------- #


def _assign_rungs(freqs_hz: Sequence[float], tau_s: float, tolerance: float) -> dict[int, int]:
    """Nearest-rung assignment: ``{n: candidate index}``, one candidate per
    rung. Ties go to the candidate closest to the predicted frequency."""
    assigned: dict[int, int] = {}
    for k, f in enumerate(freqs_hz):
        n = int(round(f * tau_s - 0.5))
        if n < 0:
            continue
        predicted = (n + 0.5) / tau_s
        if abs(f - predicted) * tau_s > tolerance:
            continue
        if n in assigned and abs(freqs_hz[assigned[n]] - predicted) <= abs(f - predicted):
            continue
        assigned[n] = k
    return assigned


def _longest_consecutive(rungs: Sequence[int]) -> list[int]:
    """Longest run of consecutive integers, earliest on a tie."""
    ordered = sorted(rungs)
    best: list[int] = [ordered[0]]
    current: list[int] = [ordered[0]]
    for previous, this in zip(ordered, ordered[1:]):
        current = current + [this] if this == previous + 1 else [this]
        if len(current) > len(best):
            best = list(current)
    return best


def _refine_tau(rungs: Sequence[int], freqs_hz: Sequence[float], assigned: Mapping[int, int]) -> float:
    """Least-squares ``tau`` for a fixed rung assignment.

    Minimising ``sum((f_k - (n_k+1/2)/tau)**2)`` over ``tau`` gives
    ``tau = sum(m**2) / sum(f*m)`` with ``m = n + 1/2``. Closed form, so no
    optimiser and no starting-point policy.

    **Unweighted, on purpose.** Weighting by depth would make ``tau`` a
    function of the depth statistic, whose bias is frequency-dependent
    (``NULL_DEPTH_STATISTIC``), so a systematically shallower top rung would
    quietly pull the delay.
    """
    m = np.array([n + 0.5 for n in rungs], dtype=float)
    f = np.array([freqs_hz[assigned[n]] for n in rungs], dtype=float)
    return float(np.sum(m**2) / np.sum(f * m))


def _smoothing_bandwidth_hz(f_hz: float, fraction: int) -> float:
    """Width of the 1/``fraction``-octave window at ``f_hz``."""
    return float(f_hz * (2.0 ** (0.5 / fraction) - 2.0 ** (-0.5 / fraction)))


def _fit_ladder(
    candidates: Sequence[_Candidate],
    band_hz: tuple[float, float],
    diag_fraction: int,
    tolerance: float,
) -> tuple[float, dict[int, int]] | None:
    """Best single-tau ladder over ``candidates``, tau free.

    Enumerates every (candidate pair, rung gap) hypothesis — a pair of minima
    separated by ``dn`` rungs fixes ``tau = dn / (f_j - f_i)`` exactly, so the
    search is over integers rather than a tau grid, and it is complete rather
    than a sampling of one. ``dn`` is bounded by physics, not by a constant:
    the rungs between two minima cannot be closer together than the smoothing
    window that located them (below that the curve cannot show a comb at all),
    so ``dn <= (f_j - f_i) / bandwidth``, evaluated at the band's top where
    that window is widest.

    **That bound cannot hide a reportable ladder, and the argument is short.**
    A ladder is only ever *reported* when it has ``MIN_LADDER_RUNGS``
    consecutive matched rungs, so some pair of candidates is adjacent on it —
    and for an adjacent pair the true gap is ``dn = 1``, which is enumerated
    for every pair regardless of the bound (the range starts at 1). A wider
    pair's true ``dn`` may indeed be excluded, but only after the adjacent
    pair has already produced the same ``tau`` exactly.

    Each hypothesis is then assigned, refined and re-assigned to a fixed
    point — the refined tau can change which minima match, so a single pass
    would grade a fit against the assignment of a different one.

    Scored by **total matched depth**, tie-broken by RMS rung error. Depth
    because a ladder that leaves the band's deepest null unexplained is a bad
    hypothesis however tidily it fits the shallow ones: on the S0 main leg's six
    tweeter-height positions over 1.2-19 kHz, a rung-count score chose a
    168.9 us ladder over the real 298.9 us one — both 3 rungs, the wrong one
    tidier — by skipping the 6.31 dB null at 11.6 kHz. The band is part of the
    claim: over 5-19 kHz the two scores agree, the shallow low-frequency minima
    the count score preferred being out of band. No test pins that 6.31 dB.

    Returns ``(tau_s, {n: candidate index})`` for the longest consecutive run,
    or ``None`` when no hypothesis produced ``MIN_LADDER_RUNGS`` consecutive
    matched rungs.
    """
    if len(candidates) < MIN_LADDER_RUNGS:
        return None
    freqs_hz = [c.f_hz for c in candidates]
    depths = [c.depth_db for c in candidates]
    band_bandwidth = _smoothing_bandwidth_hz(band_hz[1], diag_fraction)
    best_score: tuple[float, float] | None = None
    best: tuple[float, dict[int, int]] | None = None
    for ia in range(len(candidates)):
        for ib in range(ia + 1, len(candidates)):
            span = freqs_hz[ib] - freqs_hz[ia]
            if span <= 0.0:
                continue
            for dn in range(1, max(1, int(np.floor(span / band_bandwidth))) + 1):
                tau = dn / span
                for _ in range(8):
                    assigned = _assign_rungs(freqs_hz, tau, tolerance)
                    if len(assigned) < MIN_LADDER_RUNGS:
                        break
                    run = _longest_consecutive(list(assigned))
                    if len(run) < MIN_LADDER_RUNGS:
                        break
                    refined = _refine_tau(run, freqs_hz, assigned)
                    if abs(refined - tau) <= 1e-12 * tau:
                        tau = refined
                        break
                    tau = refined
                assigned = _assign_rungs(freqs_hz, tau, tolerance)
                if len(assigned) < MIN_LADDER_RUNGS:
                    continue
                run = _longest_consecutive(list(assigned))
                if len(run) < MIN_LADDER_RUNGS:
                    continue
                matched = np.array([freqs_hz[assigned[n]] for n in run], dtype=float)
                predicted = np.array([(n + 0.5) / tau for n in run], dtype=float)
                errors = np.abs(matched - predicted) * tau
                if float(errors.max()) > tolerance:
                    continue
                # Resolvability: a ladder whose rungs sit closer together than
                # the window that located them was never visible on this
                # curve, so a "fit" to it is an artefact of the tolerance.
                # Checked at the highest matched rung, where the window is
                # widest.
                if 1.0 / tau <= _smoothing_bandwidth_hz(
                    float(matched.max()), diag_fraction
                ):
                    continue
                score = (
                    float(sum(depths[assigned[n]] for n in run)),
                    -float(np.sqrt(np.mean(errors**2))),
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best = (tau, {n: assigned[n] for n in run})
    return best


# --------------------------------------------------------------------------- #
# Per-position presence
# --------------------------------------------------------------------------- #


def _per_position_candidates(
    combined: CombinedResponse, band_idx: np.ndarray, min_sep_oct: float
) -> list[list[_Candidate]]:
    """Each position's own located-and-measured minima, in position order.

    Runs the *same* location-and-depth pass the combined curve gets, on each
    position's own pair of curves — so "present at this position" and
    "identified in the combined curve" are the same measurement rather than
    two conventions sharing one name.

    Computed once for the whole cloud rather than once per (rung, position):
    the pass is the expensive part and it does not depend on which rung is
    being classified.
    """
    return [
        _measure_candidates(combined.freqs_hz, row_diag, row_raw, band_idx, min_sep_oct)[0]
        for row_diag, row_raw in zip(
            combined.per_position_diag_db, combined.per_position_db, strict=True
        )
    ]


def _classify_presence(present: int, total: int) -> str:
    """The invariant/dependent line, in one place.

    Both :func:`identify_interference_nulls` and
    :func:`classify_dip_position_variance` reach this verdict, and a second
    copy of the comparison is a second thing to drift when
    :data:`POSITION_PRESENCE_FRACTION` is re-derived.
    """
    fraction = present / total if total else 0.0
    return (
        CLASSIFICATION_POSITION_INVARIANT
        if fraction >= POSITION_PRESENCE_FRACTION
        else CLASSIFICATION_POSITION_DEPENDENT
    )


def _present_at_positions(
    per_position: Sequence[Sequence[_Candidate]],
    f_hz: float,
    tau_s: float,
    tolerance: float,
    min_depth_db: float,
) -> int:
    """How many positions individually show a null at ``f_hz``.

    A position counts when it produced a candidate within the ladder's rung
    tolerance of ``f_hz`` whose depth clears the same materiality floor the
    combined curve's candidates were held to.
    """
    return sum(
        any(
            abs(c.f_hz - f_hz) * tau_s <= tolerance and c.depth_db >= min_depth_db
            for c in candidates
        )
        for candidates in per_position
    )


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def _validated_band(
    combined: CombinedResponse,
    band_hz: tuple[float, float],
    min_depth_db: float,
) -> tuple[float, float]:
    """The caller's analysis band, checked against this cloud's own grid.

    One rule, shared by :func:`identify_interference_nulls` and
    :func:`classify_dip_position_variance`, so the two cannot disagree about
    what a legal band is — a second copy is a second thing to drift.

    Raises:
      ValueError: on a band that is not a pair of finite numbers with
        ``0 < lo < hi``, on a band covering fewer than 3 bins of the
        combined grid, or on a non-positive ``min_depth_db``. All three are
        caller *configuration*, wrong for the whole cloud at once and
        unfixable by looking at the data.
    """
    try:
        lo_hz, hi_hz = (float(value) for value in band_hz)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"band_hz must be a pair of finite numbers with 0 < lo < hi, got {band_hz!r}"
        ) from exc
    if not (np.isfinite(lo_hz) and np.isfinite(hi_hz)) or not 0.0 < lo_hz < hi_hz:
        raise ValueError(
            f"band_hz must be finite and satisfy 0 < lo < hi, got {band_hz}"
        )
    if min_depth_db <= 0.0:
        raise ValueError(f"min_depth_db must be positive, got {min_depth_db}")
    band = (lo_hz, hi_hz)
    freqs = np.asarray(combined.freqs_hz, dtype=float)
    band_idx = np.flatnonzero((freqs >= lo_hz) & (freqs <= hi_hz))
    if band_idx.size < 3:
        raise ValueError(
            f"band_hz {band} covers {band_idx.size} bins of the combined grid "
            f"({float(freqs[0])}-{float(freqs[-1])} Hz); need at least 3"
        )
    return band


def _empty_report(
    combined: CombinedResponse,
    band_hz: tuple[float, float],
    reason: str,
    *,
    refusals: Sequence[RefusedCandidate] = (),
    n_candidates: int = 0,
    tau_ladder_us: float = 0.0,
    arrival_tau_us: float = 0.0,
    arrival_r_time: float = 0.0,
    arrival_r_max: float = 0.0,
    n_corroborating: int = 0,
    r_freq: float = 0.0,
    agreement: float = 0.0,
    ladder_arrival_gap: float = 0.0,
    excluded_fraction: float = 0.0,
    min_depth_db: float = DEFAULT_MIN_NULL_DEPTH_DB,
) -> InterferenceNullReport:
    """A report that identified nothing, carrying how far the method got."""
    mask = np.zeros(combined.freqs_hz.size, dtype=bool)
    mask.flags.writeable = False
    return InterferenceNullReport(
        nulls=(),
        excluded=mask,
        excluded_bands_hz=(),
        excluded_fraction=excluded_fraction,
        refusals=tuple(refusals),
        reason=reason,
        classification=CLASSIFICATION_INSUFFICIENT_EVIDENCE,
        band_hz=band_hz,
        tau_ladder_us=tau_ladder_us,
        arrival_tau_us=arrival_tau_us,
        arrival_r_time=arrival_r_time,
        arrival_r_max=arrival_r_max,
        n_corroborating=n_corroborating,
        r_freq=r_freq,
        agreement=agreement,
        ladder_arrival_gap=ladder_arrival_gap,
        capped=reason == REASON_EXCLUSION_CAP,
        min_depth_db=min_depth_db,
        n_candidates=n_candidates,
    )


def _arrival_cluster(
    echoes: Sequence[EchoDiagnostic | None], confidence_floor: float
) -> tuple[EchoDiagnostic, ...]:
    """The usable estimates that agree with their own median, by
    :func:`~jasper.audio_measurement.spatial_combine.assess_geometry`'s
    tolerance.

    Deliberately *not* the geometry verdict itself. ``geometry.locked`` asks
    whether enough of the cloud clusters to tell a household to spread the mic
    further; this asks which estimates describe one arrival well enough to
    attribute a null ladder to it. A dispersed cloud can still contain a tight
    sub-cluster, and that sub-cluster is a legitimate candidate — refusing it
    because the *whole* cloud is dispersed would confuse two questions.
    """
    usable = usable_echo_estimates(echoes, confidence_floor=confidence_floor)
    if not usable:
        return ()
    median_tau = float(np.median([e.tau_us for e in usable]))
    return tuple(
        e
        for e in usable
        if abs(e.tau_us - median_tau) <= GEOMETRY_CLUSTER_TOLERANCE * median_tau
    )


def identify_interference_nulls(
    combined: CombinedResponse,
    *,
    band_hz: tuple[float, float],
    min_depth_db: float = DEFAULT_MIN_NULL_DEPTH_DB,
    confidence_floor: float = ECHO_CONFIDENCE_FLOOR,
) -> InterferenceNullReport:
    """Identify the interference nulls in a spatial cloud's combined response.

    See the module docstring for the method and for what each verdict claims.
    The short version: a dip is identified only when it is a rung of a
    consecutive single-tau ladder **and** that ladder's delay and reflection
    ratio both agree with an arrival the cloud independently measured. Every
    other dip is refused, by name.

    Args:
      combined: the cloud, from
        :func:`~jasper.audio_measurement.spatial_combine.combine_positions`.
        Must carry per-position curves (it always does when it came from that
        function); a record without them is refused with
        ``REASON_NO_PER_POSITION_CURVES`` rather than silently skipping the
        classification step.
      band_hz: the analysis band, ``(lo, hi)``. **Caller-supplied and
        required** — the band a speaker's nulls are searched in is a property
        of its declared driver contract, which this module does not have and
        must not guess. It is echoed back on the report.
      min_depth_db: materiality floor, see ``DEFAULT_MIN_NULL_DEPTH_DB``.
      confidence_floor: echo confidence required for a position to
        corroborate, mirroring
        :func:`~jasper.audio_measurement.spatial_combine.assess_geometry`.

    Returns:
      An :class:`InterferenceNullReport`. Never raises on *data* — a cloud
      with nothing to find comes back with an empty registry and a reason.

    Raises:
      ValueError: on a malformed ``band_hz`` (not a pair of finite numbers
        with ``0 < lo < hi``, or a band that does not overlap the combined
        grid) or a non-positive ``min_depth_db``. Caller configuration, wrong
        for the whole cloud at once and unfixable by looking at the data, so
        it fails loudly — the same "malformed config raises, malformed data
        refuses" line
        :func:`~jasper.audio_measurement.spatial_combine.combine_positions`
        draws.
    """
    band = _validated_band(combined, band_hz, min_depth_db)
    lo_hz, hi_hz = band

    freqs = np.asarray(combined.freqs_hz, dtype=float)
    band_idx = np.flatnonzero((freqs >= lo_hz) & (freqs <= hi_hz))

    per_position_diag = np.asarray(combined.per_position_diag_db, dtype=float)
    per_position_raw = np.asarray(combined.per_position_db, dtype=float)
    if (
        per_position_diag.ndim != 2
        or per_position_diag.shape != (combined.n_positions, freqs.size)
        or per_position_raw.shape != per_position_diag.shape
    ):
        return _empty_report(
            combined, band, REASON_NO_PER_POSITION_CURVES, min_depth_db=min_depth_db
        )

    # --- 1. the arrival to corroborate against ---------------------------- #
    cluster = _arrival_cluster(combined.per_position_echo, confidence_floor)
    arrival_tau_us = float(np.median([e.tau_us for e in cluster])) if cluster else 0.0
    ratios = [10.0 ** (e.strength_db / 20.0) for e in cluster]
    arrival_r_time = float(np.median(ratios)) if ratios else 0.0
    arrival_r_max = float(max(ratios)) if ratios else 0.0
    if len(cluster) < MIN_CORROBORATING_POSITIONS:
        # Reported anyway: a reader asking why the gate refused wants to see
        # *what* was too thin, not only that something was.
        return _empty_report(
            combined,
            band,
            REASON_NO_CORROBORATING_ARRIVALS,
            n_corroborating=len(cluster),
            arrival_tau_us=arrival_tau_us,
            arrival_r_time=arrival_r_time,
            arrival_r_max=arrival_r_max,
            min_depth_db=min_depth_db,
        )
    ceiling_db = null_depth_ceiling_db(arrival_r_max)

    # --- 2. candidate minima ---------------------------------------------- #
    min_sep_oct = 1.0 / float(combined.diag_fraction)
    candidates, unmeasurable = _measure_candidates(
        freqs, np.asarray(combined.power_mean_diag_db, dtype=float),
        np.asarray(combined.power_mean_db, dtype=float), band_idx, min_sep_oct,
    )
    n_candidates = len(candidates) + len(unmeasurable)
    refusals: list[RefusedCandidate] = [
        RefusedCandidate(
            f_center_hz=f, depth_db=0.0, reason=CANDIDATE_NOT_MEASURABLE, evidence={}
        )
        for f in unmeasurable
    ]

    # --- 3. depth-ceiling acquittal, before the fit ------------------------ #
    admitted: list[_Candidate] = []
    for candidate in candidates:
        if candidate.depth_db > ceiling_db + DEPTH_CEILING_MARGIN_DB:
            refusals.append(
                RefusedCandidate(
                    f_center_hz=candidate.f_hz,
                    depth_db=candidate.depth_db,
                    reason=CANDIDATE_DEPTH_EXCEEDS_CEILING,
                    evidence={
                        "depth_ceiling_db": ceiling_db,
                        "ceiling_margin_db": DEPTH_CEILING_MARGIN_DB,
                        "arrival_r_max": arrival_r_max,
                    },
                )
            )
        elif candidate.depth_db < min_depth_db:
            refusals.append(
                RefusedCandidate(
                    f_center_hz=candidate.f_hz,
                    depth_db=candidate.depth_db,
                    reason=CANDIDATE_BELOW_MIN_DEPTH,
                    evidence={"min_depth_db": min_depth_db},
                )
            )
        else:
            admitted.append(candidate)

    def refuse(
        reason: str,
        *,
        tau_ladder_us: float = 0.0,
        r_freq: float = 0.0,
        agreement: float = 0.0,
        ladder_arrival_gap: float = 0.0,
        excluded_fraction: float = 0.0,
    ) -> InterferenceNullReport:
        """A refusal carrying everything measured up to the stage that fired."""
        return _empty_report(
            combined,
            band,
            reason,
            refusals=_sorted_refusals(refusals),
            n_candidates=n_candidates,
            arrival_tau_us=arrival_tau_us,
            arrival_r_time=arrival_r_time,
            arrival_r_max=arrival_r_max,
            n_corroborating=len(cluster),
            min_depth_db=min_depth_db,
            tau_ladder_us=tau_ladder_us,
            r_freq=r_freq,
            agreement=agreement,
            ladder_arrival_gap=ladder_arrival_gap,
            excluded_fraction=excluded_fraction,
        )

    if not admitted:
        return refuse(REASON_NO_CANDIDATE_NULLS)

    # --- 4. the ladder ----------------------------------------------------- #
    fitted = _fit_ladder(
        admitted, band, combined.diag_fraction, RUNG_MATCH_TOLERANCE_SPACINGS
    )
    if fitted is None:
        refusals.extend(
            RefusedCandidate(
                f_center_hz=c.f_hz, depth_db=c.depth_db,
                reason=CANDIDATE_NO_MATCHING_RUNG, evidence={},
            )
            for c in admitted
        )
        return refuse(REASON_NO_LADDER)
    tau_s, assigned = fitted
    tau_ladder_us = tau_s * 1e6
    gap = tau_ladder_us / arrival_tau_us - 1.0

    matched_indexes = set(assigned.values())
    for k, candidate in enumerate(admitted):
        if k in matched_indexes:
            continue
        n = int(round(candidate.f_hz * tau_s - 0.5))
        predicted = (n + 0.5) / tau_s if n >= 0 else 0.0
        error = abs(candidate.f_hz - predicted) * tau_s if predicted > 0.0 else float("inf")
        in_tolerance = error <= RUNG_MATCH_TOLERANCE_SPACINGS
        refusals.append(
            RefusedCandidate(
                f_center_hz=candidate.f_hz,
                depth_db=candidate.depth_db,
                reason=(
                    CANDIDATE_OUTSIDE_CONTIGUOUS_RUN
                    if in_tolerance
                    else CANDIDATE_NO_MATCHING_RUNG
                ),
                evidence={"predicted_hz": predicted, "rung_error_spacings": error},
            )
        )

    # --- 5. corroboration, both ways -------------------------------------- #
    r_freq = reflection_ratio_from_depth(
        max(admitted[k].depth_db for k in assigned.values())
    )
    agreement = abs(arrival_r_time - r_freq)
    fit_evidence = dict(
        tau_ladder_us=tau_ladder_us,
        r_freq=r_freq,
        agreement=agreement,
        ladder_arrival_gap=gap,
    )
    if abs(gap) > LADDER_ARRIVAL_TOLERANCE:
        return refuse(REASON_LADDER_ARRIVAL_MISMATCH, **fit_evidence)
    if agreement > R_AGREEMENT_TOLERANCE:
        return refuse(REASON_R_DISAGREEMENT, **fit_evidence)

    # --- 6. the runaway guard, before anything is claimed ------------------ #
    mask = np.zeros(freqs.size, dtype=bool)
    for k in assigned.values():
        mask[admitted[k].lo_index : admitted[k].hi_index + 1] = True
    excluded_fraction = float(np.count_nonzero(mask[band_idx]) / band_idx.size)
    if excluded_fraction > EXCLUSION_CAP_FRACTION:
        return refuse(
            REASON_EXCLUSION_CAP, excluded_fraction=excluded_fraction, **fit_evidence
        )

    # --- 7. classification and output -------------------------------------- #
    per_position = _per_position_candidates(combined, band_idx, min_sep_oct)
    nulls: list[IdentifiedNull] = []
    for n in sorted(assigned):
        candidate = admitted[assigned[n]]
        present = _present_at_positions(
            per_position, candidate.f_hz, tau_s,
            RUNG_MATCH_TOLERANCE_SPACINGS, min_depth_db,
        )
        predicted = (n + 0.5) / tau_s
        nulls.append(
            IdentifiedNull(
                f_lo_hz=candidate.f_lo_hz,
                f_hi_hz=candidate.f_hi_hz,
                f_center_hz=candidate.f_hz,
                n=n,
                tau_us=tau_ladder_us,
                r_time=arrival_r_time,
                r_freq=r_freq,
                agreement=agreement,
                depth_db=candidate.depth_db,
                classification=_classify_presence(present, combined.n_positions),
                evidence={
                    "predicted_hz": predicted,
                    "rung_error_spacings": abs(candidate.f_hz - predicted) * tau_s,
                    "flank_lo_hz": candidate.flank_lo_hz,
                    "flank_hi_hz": candidate.flank_hi_hz,
                    "flank_baseline_db": candidate.baseline_db,
                    "null_level_db": candidate.null_level_db,
                    "diag_depth_db": candidate.diag_depth_db,
                    "depth_ceiling_db": ceiling_db,
                    "positions_present": float(present),
                    "positions_total": float(combined.n_positions),
                },
            )
        )

    mask.flags.writeable = False
    return InterferenceNullReport(
        nulls=tuple(nulls),
        excluded=mask,
        excluded_bands_hz=merged_true_intervals(freqs, mask),
        excluded_fraction=excluded_fraction,
        refusals=_sorted_refusals(refusals),
        reason="",
        classification=(
            CLASSIFICATION_POSITION_INVARIANT
            if all(
                null.classification == CLASSIFICATION_POSITION_INVARIANT
                for null in nulls
            )
            else CLASSIFICATION_POSITION_DEPENDENT
        ),
        band_hz=band,
        tau_ladder_us=tau_ladder_us,
        arrival_tau_us=arrival_tau_us,
        arrival_r_time=arrival_r_time,
        arrival_r_max=arrival_r_max,
        n_corroborating=len(cluster),
        r_freq=r_freq,
        agreement=agreement,
        ladder_arrival_gap=gap,
        capped=False,
        min_depth_db=min_depth_db,
        n_candidates=n_candidates,
    )


def _sorted_refusals(refusals: Sequence[RefusedCandidate]) -> tuple[RefusedCandidate, ...]:
    """Refusals in ascending frequency — the order a reader scans a chart in,
    and stable regardless of which stage produced each one."""
    return tuple(sorted(refusals, key=lambda r: r.f_center_hz))


# --------------------------------------------------------------------------- #
# Position variance, without the ladder
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionVarianceDip:
    """One measured dip of the combined curve, with how many of the cloud's
    positions individually show it.

    ``f_lo_hz`` / ``f_hi_hz`` are the dip's own half-depth interval — the
    SAME interval :class:`IdentifiedNull` carries and the registry excludes
    on, from the same :func:`_measure_candidates` pass, so a consumer acting
    on this record touches exactly the bins the registry would have.
    """

    f_center_hz: float
    f_lo_hz: float
    f_hi_hz: float
    depth_db: float
    positions_present: int
    positions_total: int
    classification: str


@dataclass(frozen=True)
class PositionVarianceReport:
    """What :func:`classify_dip_position_variance` found.

    ``position_dependent_bands_hz`` is the merged union of the
    ``position_dependent`` dips' intervals — the ONE field a consumer should
    act on, and deliberately the only one this module pre-merges (see the
    function's docstring for why the invariant ones are not offered in the
    same shape).
    """

    band_hz: tuple[float, float]
    dips: tuple[PositionVarianceDip, ...]
    position_dependent_bands_hz: tuple[tuple[float, float], ...]
    n_positions: int
    min_depth_db: float
    reason: str


def classify_dip_position_variance(
    combined: CombinedResponse,
    *,
    band_hz: tuple[float, float],
    min_depth_db: float = DEFAULT_MIN_NULL_DEPTH_DB,
) -> PositionVarianceReport:
    """Which dips in ``band_hz`` do the cloud's positions *disagree* about?

    :func:`identify_interference_nulls`' classification step (module
    docstring, stage 6) asks exactly this question, but only ever reaches it
    for a dip that already cleared an arrival cluster, a consecutive
    single-tau ladder, and two-way tau/r corroboration. A dip that fails any
    of those comes back a :class:`RefusedCandidate` **with no position count
    at all**. This function is that one stage, run on its own, so the
    question can be asked about a dip the ladder never explained.

    **It is not a second instrument.** Candidates are located and measured by
    the same :func:`_measure_candidates` pass on the same two combined
    curves, per-position presence by the same
    :func:`_per_position_candidates` pass, and the invariant/dependent line
    by the same :data:`POSITION_PRESENCE_FRACTION`. Nothing here is
    calibrated separately; the only quantity this function introduces is the
    proximity below, and it is *derived* rather than chosen.

    **Why not reuse the ladder's own rung tolerance.**
    :func:`_present_at_positions` matches a position's minimum to a rung in
    *quefrency* (``|Δf| · τ``), which needs the τ this function has by
    construction no access to. The resolution-matched analogue is the width
    of the smoothing window the minima were located through:
    :func:`_smoothing_bandwidth_hz` at ``combined.diag_fraction``. Two
    minima closer together than that window are one feature on this curve —
    the same reasoning ``_fit_ladder``'s own resolvability check already
    applies to rung spacing.

    **What ``position_invariant`` means here.** The dip was individually
    measurable at at least :data:`POSITION_PRESENCE_FRACTION` of the positions.
    It is NOT a finding that the dip is a driver property and NOT a licence to
    EQ it: the module docstring's limit binds this function exactly as it binds
    the registry, and :mod:`jasper.attribution.promotion` routes
    ``position_invariant`` to ``carve``, never to gain. A consumer that grants
    an EQ BOOST because a dip is position-invariant has inverted that.

    ``position_dependent`` is the direction that carries a decision: the
    positions disagree, so the combined curve's dip is not a property of what
    the speaker radiates, and correcting it corrects nothing any listener
    hears. That asymmetry is why ``position_dependent_bands_hz`` is the only
    pre-merged field.

    Returns a report with ``reason`` set and no dips when the cloud cannot
    support the question: :data:`REASON_NO_PER_POSITION_CURVES` (a record
    that never retained them), :data:`REASON_TOO_FEW_POSITIONS` (N < 2), or
    :data:`REASON_NO_CANDIDATE_NULLS` (nothing in the band cleared
    ``min_depth_db``). Never raises on *data*.

    Raises:
      ValueError: on a malformed ``band_hz`` or a non-positive
        ``min_depth_db`` — caller configuration, the same "malformed config
        raises, malformed data refuses" line
        :func:`identify_interference_nulls` draws.
    """
    band = _validated_band(combined, band_hz, min_depth_db)
    freqs = np.asarray(combined.freqs_hz, dtype=float)
    band_idx = np.flatnonzero((freqs >= band[0]) & (freqs <= band[1]))

    n_positions = int(combined.n_positions)
    per_position_diag = np.asarray(combined.per_position_diag_db, dtype=float)
    per_position_raw = np.asarray(combined.per_position_db, dtype=float)
    if (
        per_position_diag.ndim != 2
        or per_position_diag.shape != (n_positions, freqs.size)
        or per_position_raw.shape != per_position_diag.shape
    ):
        return _empty_variance_report(band, n_positions, min_depth_db,
                                      REASON_NO_PER_POSITION_CURVES)
    if n_positions < 2:
        return _empty_variance_report(band, n_positions, min_depth_db,
                                      REASON_TOO_FEW_POSITIONS)

    min_sep_oct = 1.0 / float(combined.diag_fraction)
    candidates, _unmeasurable = _measure_candidates(
        freqs, np.asarray(combined.power_mean_diag_db, dtype=float),
        np.asarray(combined.power_mean_db, dtype=float), band_idx, min_sep_oct,
    )
    material = [c for c in candidates if c.depth_db >= min_depth_db]
    if not material:
        return _empty_variance_report(band, n_positions, min_depth_db,
                                      REASON_NO_CANDIDATE_NULLS)

    per_position = _per_position_candidates(combined, band_idx, min_sep_oct)
    dips: list[PositionVarianceDip] = []
    for cand in material:
        proximity_hz = _smoothing_bandwidth_hz(cand.f_hz, combined.diag_fraction)
        present = sum(
            any(
                abs(c.f_hz - cand.f_hz) <= proximity_hz and c.depth_db >= min_depth_db
                for c in position_candidates
            )
            for position_candidates in per_position
        )
        dips.append(
            PositionVarianceDip(
                f_center_hz=float(cand.f_hz),
                f_lo_hz=float(cand.f_lo_hz),
                f_hi_hz=float(cand.f_hi_hz),
                depth_db=float(cand.depth_db),
                positions_present=present,
                positions_total=n_positions,
                classification=_classify_presence(present, n_positions),
            )
        )

    dependent = np.zeros(freqs.size, dtype=bool)
    for dip in dips:
        if dip.classification != CLASSIFICATION_POSITION_DEPENDENT:
            continue
        dependent |= (freqs >= dip.f_lo_hz) & (freqs <= dip.f_hi_hz)
    return PositionVarianceReport(
        band_hz=band,
        dips=tuple(sorted(dips, key=lambda d: d.f_center_hz)),
        position_dependent_bands_hz=merged_true_intervals(freqs, dependent),
        n_positions=n_positions,
        min_depth_db=min_depth_db,
        reason="",
    )


def _empty_variance_report(
    band: tuple[float, float], n_positions: int, min_depth_db: float, reason: str,
) -> PositionVarianceReport:
    """A variance report that classified nothing, carrying why."""
    return PositionVarianceReport(
        band_hz=band,
        dips=(),
        position_dependent_bands_hz=(),
        n_positions=n_positions,
        min_depth_db=min_depth_db,
        reason=reason,
    )
