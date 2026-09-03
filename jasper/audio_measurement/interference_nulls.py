# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Interference-null identification — the orthogonal honesty gate.

``identify_interference_nulls(combined, band_hz=...) -> InterferenceNullReport`` answers one
question: **which dips in this speaker's measured response are interference nulls from a
delayed copy of its own sound, and therefore uncorrectable by EQ?**

It exists because the combiner's power-mean-vs-median screen
(:mod:`jasper.audio_measurement.spatial_combine`) flags bins where positions *disagree*, so it
is structurally blind to a null every position sees (on the S0 session that screen excluded 0
of 5462 bins in 8-16 kHz — a +1.27 dB gap against its own >2 dB trigger — while a source-fixed
comb sat inside that band cutting 5-7 dB nulls). Position-invariance says "this is real"; it
does not say "this is correctable". Consumers run both instruments plus ``geometry.locked`` and
take the union.

The method, and the two independent instruments it insists agree:

1. **Candidate arrival** — the cloud's per-position echo diagnostics, clustered by
   ``spatial_combine``'s own tolerances, needing at least ``MIN_CORROBORATING_POSITIONS``.
2. **Candidate nulls** — local minima of the combined 1/6-octave diagnostic curve, each with a
   depth against its own two flanking maxima (``NULL_DEPTH_STATISTIC``).
3. **Depth-ceiling acquittal** — a two-path sum with ratio ``r`` cannot cut a null deeper than
   ``20*log10((1+r)/(1-r))``; a dip deeper than the arrival's ceiling plus
   ``DEPTH_CEILING_MARGIN_DB`` is refused attribution *before* the ladder is fitted. Acquitted,
   not excluded — the dip is left alone.
4. **Ladder fit** — the best single-tau ladder ``f_n = (n + 1/2) / tau``, tau free, requiring
   at least ``MIN_LADDER_RUNGS`` *consecutive* rungs.
5. **Corroboration, both ways** — fitted ``tau`` within ``LADDER_ARRIVAL_TOLERANCE`` of the
   arrival's, and the ``r`` the null depths imply within ``R_AGREEMENT_TOLERANCE`` of the
   arrival's envelope — frequency- and time-domain estimators that never see each other's answer.
6. **Classification** — per rung, ``position_invariant`` at >= ``POSITION_PRESENCE_FRACTION`` of
   positions; the roll-up earns the word only when every rung did. Anything refused above is
   ``insufficient_evidence``.

**What ``position_invariant`` does and does not claim.** A threshold, not "every position" —
read ``positions_present``/``positions_total``. It cannot say WHERE the null comes from: an
origin that travels with the speaker and an unchanged room path are indistinguishable within
one session. **Detection only** — nothing here removes an echo; the output is a registry of
*reasons* carrying an identification's entire supporting arithmetic.

Pure computation: numpy plus :mod:`jasper.audio_measurement.spatial_combine`. No I/O, no
logging, no globals, no randomness, no product policy.

**Every threshold below is calibrated on one speaker** — the JTS3 cdhorn, one evening, three
geometries (the S0 corpus). Each constant states the population it was measured on and its
headroom; several have a positive population and no measured negative one, and say so.
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

# For a candidate minimum on the combined 1/6-octave diagnostic curve:
#
#     depth_db = (power mean of the DIAGNOSTIC curve at the two flanking
#                 maxima) - (the UNSMOOTHED power mean at the minimum's bin)
#
# **The null must be read unsmoothed**: 1/6-octave smoothing raises a synthetic comb's null
# bottom by 0.13-5.98 dB (a depth read smoothed at both ends reports 3.7 dB where the truth is
# 10.7 dB). **The flank may be read smoothed, and should be** — smoothing lowers a comb PEAK by
# only 0.027-1.044 dB, so reading the flanks smoothed costs ~1 dB of depth while removing a
# single unsmoothed bin's variance (on the S0 desk-edge leg, the difference between the genuine
# 11.6 kHz rung reading an impossible 1.02 dB above its ceiling and 0.27 dB above it).
#
# "Unsmoothed" means no fractional-octave smoothing, NOT the raw capture grid — the analysis
# grid cap costs this statistic only hundredths of a dB
# (test_the_analysis_grid_cap_costs_hundredths_of_a_db_end_to_end, the live authority).
#
# So the statistic is a LOWER BOUND on the true depth: ``r_freq`` derived from it is a lower
# bound on r, so the agreement gate can only refuse a true identification, never manufacture one.
NULL_DEPTH_STATISTIC = "flank(diagnostic) - null(unsmoothed)"

# How far a flanking-maximum search may run, in octaves either side of a candidate minimum.
# Beyond about half an octave the response's own shape (baffle step, driver rolloff, crossover)
# dominates whatever comb structure is present. Also bounded by the neighbouring minima, so a
# search can never step over one null to use the far side of another.
#
# Biases the acquittal test: a dip broader than ~1 octave has its flanks clipped and depth
# understated (S0 1.8 kHz lobing dip: 10.08 dB here vs 10.71 dB with hand-picked wider flanks).
# Understating depth is safe for identification and UNSAFE for acquittal, so
# ``DEPTH_CEILING_MARGIN_DB`` is calibrated on readings taken WITH this clip in place.
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
# The first two overlap almost completely: no depth threshold alone tells a comb rung from an
# ordinary dip — the ladder fit plus two corroborations does that. This floor's job is narrower:
# keeping numerically-trivial minima out of the candidate set, where a handful can assemble into
# a spurious ladder (removed entirely, the S0 main leg's four hand-width-low positions produced
# a 3-rung "ladder" at tau 290 us built on two 1.3-1.6 dB dips instead of the real family).
#
# **2.5 dB is the plan's own 8-16 kHz tolerance**, not a measured gap — it is a parameter
# (``min_depth_db``) because that is a product judgement this module holds no policy on.
DEFAULT_MIN_NULL_DEPTH_DB = 2.5

# --------------------------------------------------------------------------- #
# Ladder-fit tuning
# --------------------------------------------------------------------------- #

# How far a measured minimum may sit from a predicted rung and still match, **in units of the
# ladder's own rung spacing** (1/tau), not percent of frequency: a percent-of-frequency
# tolerance is tight at low n and, by n ~ 0.5/tol, wide enough that every frequency matches some
# rung and the fit becomes vacuous. In spacing units the window is the same fraction everywhere.
#
# **0.15 spacings, against a measured worst case of 0.0933** on the four S0 cloud groupings,
# each fitted independently (re-derived by test_s0_ladder_calibration_populations_bracket_the_constants):
#
#   grouping                      rung errors (spacings)      worst
#   main leg, all 10 positions    0.0830 / 0.0264 / 0.0256    0.0830
#   main leg, tweeter height (6)  0.0818 / 0.0285 / 0.0232    0.0818
#   main leg, a hand-width low(4) 0.0933 / 0.0281 / 0.0299    0.0933
#   desk front edge (3)           0.0926 / 0.0350 / 0.0242    0.0926
#
# Worst reading is the n=2 rung on all four (0.0818-0.0933, a systematic offset this module does
# not model) against 0.0232-0.0350 for n=3/n=4. 0.15 clears it by 1.61x. Not centred in a gap:
# a looser tolerance admits MORE ladders rather than a wrong one, screened by the
# consecutive-rung requirement and the two corroborations instead.
RUNG_MATCH_TOLERANCE_SPACINGS = 0.15

# Consecutive matched rungs required before a set of minima is a ladder.
#
# **Consecutive is the load-bearing word.** Two rungs n and n+1 pin tau from their *spacing*,
# the ladder's actual signature; a non-adjacent pair pins nothing (any tau dividing the gap
# explains it).
#
# **The counterfactual**, on the S0 main leg over 1.2-19 kHz with only ``_longest_consecutive``
# replaced by ``sorted`` (re-derived by
# test_contiguity_is_what_keeps_the_1_8_khz_dip_out_of_the_registry):
#
#   shipped        tau 298.75 us, rungs [2, 3, 4] at 8646/11627/14977 Hz, 24.18 % excluded
#   gaps allowed   tau 298.55 us, rungs [0, 2, 3, 4] at 1846/8646/11627/14977 Hz, 26.99 % excluded
#
# The extra rung is the 1.8 kHz lobing dip, a DIFFERENT mechanism, physically impossible for the
# ~320 us arrival to have cut. Without contiguity the gate excludes it as a comb rung and passes
# both corroborations (the deepest rung sets r_freq, so a wrong extra rung doesn't move it). No
# effect at 5-19 kHz (dip out of band) or on the S0 ground-plane leg (refused earlier).
#
# Two, not three: two adjacent rungs is the minimum carrying spacing information.
MIN_LADDER_RUNGS = 2

# The fitted ladder's tau must land within this *relative* distance of the corroborating
# arrival's tau. Reused from spatial_combine's own clustering tolerance rather than reinvented —
# two delays this close are already "the same delay" to
# :func:`~jasper.audio_measurement.spatial_combine.assess_geometry`.
#
# **The band must admit a measured gap.** On the S0 corpus the ladder tau sits systematically
# BELOW the arrival tau: -7.071/-6.671/-7.540/-7.058 % across the four groupings (fitted taus
# 297.96-298.90 us vs arrival medians 320.27-322.26 us — a real rim wave, not an ideal
# single-delay reflector). The 1/6-octave smoothing bandwidth alone (~+/-6 %) would refuse all
# four; 0.15 clears the worst by 1.99x (``tests/test_interference_nulls.py``'s calibration
# table is the live authority).
#
# **Symmetric, though the measurement is not**: all four readings are negative, but that is an
# observation about this speaker, not a rule — a one-sided band would bake one rim wave's
# behaviour into a gate that must also work for waveguide edges and desk bounces.
LADDER_ARRIVAL_TOLERANCE = GEOMETRY_CLUSTER_TOLERANCE

# Positions whose echo estimates must cluster before there is an arrival to corroborate
# against. Below this a lone estimate sits within any tolerance of its own median, so
# "it clustered" would be vacuous — same reasoning and value as ``GEOMETRY_MIN_CONFIDENT``,
# stated separately because the two gates (attributing a null vs. telling a household to move
# the mic) could legitimately diverge.
MIN_CORROBORATING_POSITIONS = 2

# --------------------------------------------------------------------------- #
# Corroboration tuning
# --------------------------------------------------------------------------- #

# Maximum disagreement between the two independent estimates of the reflection ratio r before
# the identification is refused. The two instruments never see each other's answer. **Time
# domain:** the arrival's envelope level, r = 10**(strength_db/20). **Frequency domain:** the
# deepest matched rung's depth, inverted through r = (x-1)/(x+1), x = 10**(depth_db/20) — the
# deepest rung is used because the relation is a *ceiling* and every mechanism in the chain can
# only make a null shallower, so it's the least-attenuated view.
#
# **Absolute, not one-sided**: the desk-edge row runs the other way (r_freq 0.3746 above r_time
# 0.3559) — ``strength_db`` is an estimate too and on three positions carries enough error to
# flip the sign.
#
# **0.10, calibrated one-sided against a measured positive population** — the four S0 cloud
# groupings (re-derived by test_s0_ladder_calibration_populations_bracket_the_constants):
#
#   grouping                      r_time    r_freq    agreement
#   main leg, all 10 positions    0.3765    0.3438    0.0327
#   main leg, tweeter height (6)  0.3748    0.3479    0.0269
#   main leg, a hand-width low(4) 0.3785    0.3374    0.0410
#   desk front edge (3)           0.3559    0.3746    0.0187
#
# Worst 0.0410; 0.10 clears it by 2.44x. No measured negative population — the S0 session
# produced one real arrival-and-ladder pair, read four ways; 0.10 around r=0.37 is ~2 dB of
# disagreement, guarded independently from the other side by the depth ceiling. Watch the
# positive population's WORST reading on other hardware, not this threshold.
R_AGREEMENT_TOLERANCE = 0.10

# How far above the candidate arrival's physical null-depth ceiling a dip must read before it
# is refused attribution, in dB. The physics is exact and one-directional: a direct sound
# summed with one delayed copy at ratio r cannot cut a null deeper than
# ``20*log10((1+r)/(1-r))`` anywhere, so a deeper dip **cannot** be that arrival — the margin
# exists for estimation error in r and the depth statistic's own bias, not for the physics.
#
# The ceiling uses the **largest** r in the corroborating cluster, not the median — "no
# position's arrival could have cut this" needs the loudest measured reflection as the bound,
# which is also the conservative one (bigger r means deeper permitted null, fewer acquittals).
#
# **1.25 dB, inside a measured 2.81 dB gap** on the S0 corpus at the shipped depth statistic
# (re-derived by test_s0_acquits_the_1_8_khz_dip_by_depth_ceiling and
# test_s0_ladder_calibration_populations_bracket_the_constants):
#
# * **Must not acquit — 12 genuine rungs** (8-16 kHz family, four groupings): -4.05 to +0.27 dB
#   relative to their own ceiling. The desk-edge leg's 11.6 kHz rung genuinely reads 0.27 dB
#   OVER its ceiling — a 3-position cloud's noise against a bound this tight, why margin != 0.
# * **Must acquit — the S0 1.8 kHz lobing dip**: +3.08 dB over the ceiling on six tweeter-height
#   positions (10.08 dB vs 7.01 dB ceiling for r = 0.3829), physically impossible for the
#   ~320 us arrival to have caused.
#
# 1.25 sits 0.98 dB above the genuine population's ceiling and 1.83 dB below the acquittal
# case — NOT centred, since the binding side (an acquittal REMOVES a null from the registry) is
# the 0.98 dB. Only a 2.81 dB gap between 12 readings and 1, from one speaker and session; thin,
# and the acquittal population is the one to widen first.
#
# An acquitted dip is **left alone**: refused attribution, recorded with its reason, not
# excluded — it may be a real defect, but not this instrument's to name (the power-vs-median
# screen catches that one independently).
DEPTH_CEILING_MARGIN_DB = 1.25

# --------------------------------------------------------------------------- #
# Classification tuning
# --------------------------------------------------------------------------- #

# Fraction of the cloud's positions at which an identified null must be individually present
# before it is called ``position_invariant``. Presence uses the *same* statistic and floor as
# the combined curve's, on that position's own diagnostic curve
# (``CombinedResponse.per_position_diag_db``) — one construction, so per-position and combined
# readings are comparable numbers.
#
# **0.70, measured against 1.00/1.00/0.80** — the three identified rungs of the S0 main leg's
# ten-position cloud (test_s0_main_leg_family_is_position_invariant). The 0.80 is the 15 kHz
# rung, located at all ten positions and missing at two only because its depth there
# (2.40/2.49 dB) falls a shade under the 2.5 dB materiality floor.
#
# On a small cloud this fraction is coarsely quantised (three positions: 0, 1/3, 2/3, 1, so
# 0.70 means "all three"); chosen for the 8-12 position regime.
POSITION_PRESENCE_FRACTION = 0.70

# --------------------------------------------------------------------------- #
# Runaway-exclusion guard
# --------------------------------------------------------------------------- #

# The largest fraction of the analysis band's bins this gate may exclude before it refuses to
# identify anything at all. **What it guards:** every identification costs the correction real
# bandwidth permanently (PR-6 zeroes the fit's allowed depth there; the spec evaluator drops
# those bins from grading), so a gate that carved most of the band out needs a bound, not a
# reviewer.
#
# **0.65, above what a single-tau ladder can legitimately reach — a backstop, not a tuning
# knob.** Real captures: 23.85-30.74 % across the four S0 groupings
# (test_s0_exclusion_stays_far_below_the_runaway_cap). Synthetic, deliberately pushed: ceiling
# 48.24 % over a committed grid (tau 208-625 us x r 0.15-0.80 x three bands,
# test_the_runaway_exclusion_cap_holds_over_the_committed_grid) — worst case a dense ladder,
# six rungs of a 542 us comb in a 10 kHz band.
#
# **48.24 % is not a runaway; it is the natural bound** — a null's half-depth width grows as
# depth shrinks, so a COMPLETE ladder's intervals approach the whole band (near 50 %) from
# below. A guard at 0.50 would refuse a legitimate dense comb and refuse EVERY identification
# in the report, so 0.65 is chosen above what the physics allows, to bound a failure class not
# yet observed (a mis-fit, a widened interval, an overlap bug).
#
# When it binds, the report comes back empty with ``reason = REASON_EXCLUSION_CAP`` and the
# attempted fraction on ``excluded_fraction`` — dropping "the shallowest until it fits" would
# be an arbitrary ordering presented as a measurement. The other two honesty instruments are
# unaffected; the consumer runs all three.
EXCLUSION_CAP_FRACTION = 0.65

# --------------------------------------------------------------------------- #
# Vocabulary — mirroring spatial_combine's ``REFUSAL_*``/``GEOMETRY_*`` slugs. Consumers gate
# on ``reason == ""``/the classification constants, never a specific refusal slug.
# --------------------------------------------------------------------------- #

CLASSIFICATION_POSITION_INVARIANT = "position_invariant"
CLASSIFICATION_POSITION_DEPENDENT = "position_dependent"
CLASSIFICATION_INSUFFICIENT_EVIDENCE = "insufficient_evidence"

# Report-level reasons — why nothing was identified. Listed in the order
# :func:`identify_interference_nulls` can emit them.
REASON_NO_PER_POSITION_CURVES = "no_per_position_curves"
REASON_NO_CORROBORATING_ARRIVALS = "no_corroborating_arrivals"
#: :func:`classify_dip_position_variance` only — a cross-position statistic over one position
#: is undefined, same line :func:`~jasper.audio_measurement.spatial_combine.combine_positions`
#: draws for an empty ``band_spread`` below N=2.
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
# Labelling only: a second candidate inside tolerance of an IN-RUN rung loses the tie in
# ``_assign_rungs`` and is reported here (``predicted_hz``/``rung_error_spacings`` keep the
# loss legible). Reaching it needs ``(n + 0.65) / (n + 0.35) > 2 ** (1/6)`` — possible only at
# n <= 2; no corpus or synthetic case in this module's suite reaches it.
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
    """One rung of an identified interference ladder. Every field is either a measurement or
    recomputable from ``evidence``, so a reader can re-derive the verdict.

    ``f_lo_hz``/``f_hi_hz`` bound the dip's half-depth width (bounded by its own two flanking
    maxima, so it never runs away down a rolloff) — half-depth rather than flank-to-flank
    because the full span includes the comb *peaks*, ordinary response the fit should still
    correct. ``tau_us``, ``r_freq`` and ``agreement`` are per-REPORT values (properties of the
    ladder, not this rung) carried on each rung so a record stands alone; ``r_freq`` uses the
    deepest matched rung's depth because shallower rungs understate it (``R_AGREEMENT_TOLERANCE``).
    ``classification`` is never ``CLASSIFICATION_INSUFFICIENT_EVIDENCE`` — that is report-level.
    ``evidence`` (read-only ``MappingProxyType``) carries the recomputation terms:
    ``predicted_hz``, ``rung_error_spacings``, ``flank_lo_hz``/``flank_hi_hz``,
    ``flank_baseline_db``/``null_level_db`` (``depth_db``'s two terms), ``diag_depth_db`` (the
    same depth on the diagnostic curve, which sets ``f_lo_hz``/``f_hi_hz``),
    ``depth_ceiling_db``, ``positions_present``/``positions_total``.
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
    """A measured minimum this gate declined to identify, and why. Refusals are output, not
    silence: the registry answers "why is this band excluded" AND "why is that dip not".
    ``depth_db`` is 0.0 when refused before a depth existed (``CANDIDATE_NOT_MEASURABLE``).
    ``evidence`` carries whatever the refusal turns on: ``depth_ceiling_db`` for an acquittal,
    ``min_depth_db`` for a materiality refusal, ``predicted_hz``/``rung_error_spacings`` for a
    missed rung.
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

    Read ``reason`` first: non-empty means nothing was identified, ``classification`` is
    ``CLASSIFICATION_INSUFFICIENT_EVIDENCE``, ``nulls``/``excluded_bands_hz`` are empty, and
    every other field is diagnostic — populated as far as the method got, so a reader can see
    *where* it stopped, not only that it did.

    ``excluded`` is a full-length per-bin mask (True inside an identified null's interval) that
    composes directly with ``CombinedResponse.excluded`` — the plan's honesty mask is the union
    of the two, consumed with ``geometry.locked`` (plan PR-4). ``excluded_bands_hz`` merges it
    via the same :func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals` the
    combiner's own intervals come from. ``excluded_fraction`` is reported whether or not the cap
    bound — the interesting reading is usually the one that did *not* trip.

    ``classification`` is ``position_invariant`` only when EVERY identified rung earned it, and
    ``position_dependent`` as soon as one did not — conservative on purpose, since over-claiming
    invariance is the defect class this program is about.

    ``arrival_tau_us``/``arrival_r_time``/``arrival_r_max`` are 0.0 when the cluster was empty
    but on a ``no_corroborating_arrivals`` refusal still carry whatever the sub-minimum cluster
    held, so a reader can see *what* was too thin. ``ladder_arrival_gap`` is
    ``tau_ladder/arrival_tau - 1``, signed — negative means the ladder sits below the arrival,
    what all four S0 groupings measured (``LADDER_ARRIVAL_TOLERANCE``). ``capped`` is True
    exactly when ``reason == REASON_EXCLUSION_CAP``, a separate bit distinguishing "found
    nothing" from "found too much". ``n_candidates`` is the denominator for ``refusals``.
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
    """``20*log10((1+r)/(1-r))`` — the peak-to-null range of ``|1 + r*e^(-jwt)|``. Exact, not
    empirical. ``r`` at or above 1.0 returns ``inf``; at or below 0 returns 0.0.

    The single owner of this relation, used by the acquittal, the ``r_freq`` inversion below,
    and ``depth_ceiling_db`` on every record.
    """
    if r <= 0.0:
        return 0.0
    if r >= 1.0:
        return float("inf")
    return float(20.0 * np.log10((1.0 + r) / (1.0 - r)))


def branch_gap_null_depth_ceiling_db(gap_db: float) -> float:
    """``-20*log10(1 - 10**(-gap/20))`` — the residual when the quieter branch is inverted
    against the louder one. Distinct from :func:`null_depth_ceiling_db` (one sound plus a
    delayed COPY at ratio ``r``): this is two SOURCES whose levels differ, the bound a
    reverse-null confirmation is read against — a pair 10 dB apart cannot cancel deeper than
    ~3.3 dB however right the delay is.

    Disclosure, never a refusal. ``gap_db`` at or below 0 returns ``inf``; large enough that the
    quieter branch contributes nothing saturates at 0.0.
    """
    if gap_db <= 0.0:
        return float("inf")
    residual = 1.0 - 10.0 ** (-float(gap_db) / 20.0)
    if residual <= 0.0:
        return 0.0
    return float(-20.0 * np.log10(residual))


def reflection_ratio_from_depth(depth_db: float) -> float:
    """``r = (x - 1) / (x + 1)`` with ``x = 10**(depth_db/20)``, inverting
    :func:`null_depth_ceiling_db`. A **lower bound** on the true ``r``, since the depth it
    inverts is always slightly attenuated (``NULL_DEPTH_STATISTIC``). Non-positive depth
    returns 0.0 rather than a negative ratio."""
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
    """Local minima of ``diag`` inside the band, thinned to one per ``min_sep_oct``, keeping
    the lowest. Thinning is by *level*, not depth: depth needs flanks, flanks need the
    neighbouring minima, so choosing by depth would close that loop. Separation is one
    smoothing window (``1/diag_fraction`` octaves) — closer minima are not independently
    resolved by the curve."""
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
    """Locate the band's minima and measure each one's depth and interval. Returns the
    measurable candidates plus the frequencies of ones with no flanking maximum on one side
    (``CANDIDATE_NOT_MEASURABLE``) — only reachable on a grid coarse enough that
    ``FLANK_SEARCH_MAX_OCT`` falls inside one bin; shipped grids (~1.5 Hz after decimation)
    never reach it. Refuses rather than reading a flank from whichever bin was nearest.
    """
    positions = _locate_minima(diag, band_idx, min_sep_oct, freqs)
    y = diag[band_idx]
    out: list[_Candidate] = []
    unmeasurable: list[float] = []
    for j, p in enumerate(positions):
        f0 = float(freqs[band_idx[p]])
        # Bound by the neighbouring minima (can't step over one null) and FLANK_SEARCH_MAX_OCT.
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
        # Half-depth width on the diagnostic curve, bounded by the two flanking maxima.
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
    """``{n: candidate index}``, one candidate per rung; ties go to the candidate closest to
    the predicted frequency."""
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
    """Minimising ``sum((f_k - (n_k+1/2)/tau)**2)`` gives ``tau = sum(m**2) / sum(f*m)`` with
    ``m = n + 1/2`` — closed form, no optimiser or starting point. **Unweighted, on purpose**:
    weighting by depth would make ``tau`` a function of the depth statistic's frequency-dependent
    bias (``NULL_DEPTH_STATISTIC``), letting a shallower top rung quietly pull the delay."""
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

    Enumerates every (candidate pair, rung gap) hypothesis — a pair separated by ``dn`` rungs
    fixes ``tau = dn / (f_j - f_i)`` exactly, so the search is over integers, complete rather
    than sampled. ``dn`` is bounded by physics: rungs cannot sit closer than the smoothing
    window that located them, so ``dn <= (f_j - f_i) / bandwidth`` at the band's top. That bound
    cannot hide a reportable ladder — a ladder is only ever reported with
    ``MIN_LADDER_RUNGS`` consecutive matched rungs, and for an adjacent pair ``dn = 1`` is
    always enumerated regardless of the bound.

    Each hypothesis is assigned, refined and re-assigned to a fixed point, since the refined
    tau can change which minima match.

    Scored by **total matched depth**, tie-broken by RMS rung error — depth, because a ladder
    leaving the band's deepest null unexplained is bad however tidily it fits the shallow ones
    (on the S0 main leg's six tweeter-height positions, a rung-count score chose a 168.9 us
    ladder over the real 298.9 us one by skipping the 6.31 dB null at 11.6 kHz).

    Returns ``(tau_s, {n: candidate index})`` for the longest consecutive run, or ``None``.
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
                # Resolvability: rungs closer than the locating window were never visible on
                # this curve. Checked at the highest matched rung, where the window is widest.
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
    """Each position's own located-and-measured minima, in position order — the *same*
    location-and-depth pass the combined curve gets, so "present at this position" and
    "identified in the combined curve" are one measurement, not two conventions. Computed once
    for the whole cloud since it does not depend on which rung is being classified."""
    return [
        _measure_candidates(combined.freqs_hz, row_diag, row_raw, band_idx, min_sep_oct)[0]
        for row_diag, row_raw in zip(
            combined.per_position_diag_db, combined.per_position_db, strict=True
        )
    ]


def _classify_presence(present: int, total: int) -> str:
    """The invariant/dependent line, in one place — both :func:`identify_interference_nulls`
    and :func:`classify_dip_position_variance` reach this verdict, so a second copy would drift
    when :data:`POSITION_PRESENCE_FRACTION` is re-derived."""
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
    """A position counts when it produced a candidate within the ladder's rung tolerance of
    ``f_hz`` whose depth clears the same materiality floor the combined curve's candidates
    were held to."""
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
    """One rule, shared by :func:`identify_interference_nulls` and
    :func:`classify_dip_position_variance`, so the two cannot disagree about a legal band.
    Raises ``ValueError`` on a malformed band, fewer than 3 covered bins, or a non-positive
    ``min_depth_db`` — caller *configuration*, unfixable by looking at the data."""
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
    :func:`~jasper.audio_measurement.spatial_combine.assess_geometry`'s tolerance.

    Deliberately *not* the geometry verdict itself: ``geometry.locked`` asks whether enough of
    the cloud clusters to tell a household to spread the mic; this asks which estimates
    describe one arrival well enough to attribute a null ladder. A dispersed cloud can still
    contain a legitimate tight sub-cluster.
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

    See the module docstring for the method. Short version: a dip is identified only when it
    is a rung of a consecutive single-tau ladder **and** that ladder's delay and reflection
    ratio both agree with an independently-measured arrival. Every other dip is refused, by
    name.

    ``band_hz`` is **caller-supplied and required** — the band a speaker's nulls are searched
    in is a property of its declared driver contract, which this module does not have and must
    not guess.

    Never raises on *data* — a cloud with nothing to find comes back with an empty registry and
    a reason. Raises ``ValueError`` on a malformed ``band_hz`` or non-positive ``min_depth_db``
    (caller configuration, the same "malformed config raises, malformed data refuses" line
    :func:`~jasper.audio_measurement.spatial_combine.combine_positions` draws).
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
        # Reported anyway: a reader wants to see *what* was too thin, not only that it was.
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
    """Ascending frequency — the order a reader scans a chart in, stable regardless of stage."""
    return tuple(sorted(refusals, key=lambda r: r.f_center_hz))


# --------------------------------------------------------------------------- #
# Position variance, without the ladder
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionVarianceDip:
    """One measured dip of the combined curve, with how many positions individually show it.

    ``f_lo_hz``/``f_hi_hz`` are the dip's half-depth interval — the SAME interval
    :class:`IdentifiedNull` carries, from the same :func:`_measure_candidates` pass, so a
    consumer touches exactly the bins the registry would have.
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
    """``position_dependent_bands_hz`` is the merged union of the ``position_dependent`` dips'
    intervals — the ONE field a consumer should act on, and deliberately the only one this
    module pre-merges."""

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

    :func:`identify_interference_nulls`'s classification step (stage 6) asks exactly this, but
    only reaches it for a dip that already cleared an arrival cluster, a ladder fit, and
    two-way corroboration — a dip that fails any of those comes back a
    :class:`RefusedCandidate` **with no position count at all**. This function runs that one
    stage on its own, for a dip the ladder never explained.

    **Not a second instrument**: same :func:`_measure_candidates` pass, same
    :func:`_per_position_candidates` pass, same :data:`POSITION_PRESENCE_FRACTION`. The only
    quantity introduced is the proximity below, *derived* rather than chosen: it can't reuse
    the ladder's own rung tolerance (:func:`_present_at_positions` needs a tau this function has
    no access to), so it uses the smoothing window's width (:func:`_smoothing_bandwidth_hz`) —
    two minima closer than that window are one feature on this curve.

    **What ``position_invariant`` means here**: individually measurable at
    :data:`POSITION_PRESENCE_FRACTION` of positions — NOT a finding that the dip is a driver
    property and NOT a licence to EQ it (:mod:`jasper.attribution.promotion` routes it to
    ``carve``, never gain). ``position_dependent`` is the direction that carries a decision —
    the speaker isn't radiating that dip, so correcting it corrects nothing a listener hears —
    which is why ``position_dependent_bands_hz`` is the only pre-merged field.

    Returns a report with ``reason`` set and no dips when the cloud cannot support the
    question: :data:`REASON_NO_PER_POSITION_CURVES`, :data:`REASON_TOO_FEW_POSITIONS` (N < 2),
    or :data:`REASON_NO_CANDIDATE_NULLS`. Never raises on *data*; raises ``ValueError`` on a
    malformed ``band_hz``/``min_depth_db`` (same rule as :func:`identify_interference_nulls`).
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
