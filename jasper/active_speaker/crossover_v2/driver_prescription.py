# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE driver's full-band shape correction, prescribed from outside.

The fourth member of the family, and the first that leaves the crossover
window:

===========================  =============================  ==================
quantity                     computed here                   prescribed here
===========================  =============================  ==================
inter-driver delay           ``program_analysis``'s aligner  ``.alignment_prescription``
blend-region shape           :mod:`.blend_correction`        :mod:`.blend_prescription`
ONE DRIVER's own shape       ``linearization_fit``           **this module**
===========================  =============================  ==================

**Why it exists.**  The owner's directive is that the *entire driver* be
linearized rather than only the band around the handoff.  The deterministic
half of that already ships — ``linearization_fit`` fits each driver over its
own passband and the filters land in that driver's own branch — but the
prescriber loop could only reach the SUMMED blend region.  So a reader holding
the round's evidence could see a driver defect at 5 kHz and had no vocabulary
to say so: the blend gate refuses it :data:`~.blend_prescription.FILTER_OUTSIDE_REGION`
at any Q, and the per-driver seam had no intake.  This module is that intake,
and it is the shipped instrument for two of stage P3's rules
(``docs/active-speaker-tuning-layers-design.md``):

* **rule 1, classify-first** — "only minimum-phase, speaker-own defects are
  eligible."  DISCLOSED here, against banked verdicts, by
  :func:`_check_classification` reading :mod:`.feature_classification`: every
  filter no verdict vouches for is counted onto the receipt and onto the
  propose/stage report, and none of them is refused.  Owner ruling, 2026-08-23
  — the vouch is a prediction about whether a filter will help, and predictions
  propose while measurements dispose.
* **rule 3, per-driver placement** — "a per-driver defect gets a per-driver
  filter in that branch — Layer 1a's existing per-role stage — and the shared
  stage is reserved for genuinely system-level shaping."  Enforced here as the
  route: a prescription of this class lands in
  :data:`LINEARIZATION_CANDIDATE_FIELD` and can land nowhere else.

**It rides the blend gate's lifecycle rather than forking it.**  Same posture
("the model proposes; the harness disposes"), same shared exception type
(:class:`~.blend_prescription.BlendPrescriptionRefused`), same packet
fingerprint anchoring, same one-owner response format, same fail-closed
never-clamped rule, same spool.  What is different is what a proposal may say
and what it is measured against, and that is the whole of what lives here.

**A document's named roles replace those roles; unnamed roles are untouched.**
The field this class lands in is role-keyed and has two producers — the
Layer-1a fit writes every eligible role, a document names the subset its author
had something to say about — so "replace or compose?" is a real question the
blend class never had to answer. The ruling is MERGE BY ROLE, and
:func:`driver_prescription_to_candidate_fields` both implements it and records
the two options it beat.

**Both signs, and a boost's whole cost is maximum SPL.**  Owner ruling,
2026-08-19.  A cut removes level and can never clip, at any width or depth.  A
boost is charged by ``camilla_yaml.linearization_headroom_db`` and absorbed by
``active_baseline_headroom``, which attenuates the program BEFORE the split by
what the worst branch's evaluated chain puts above unity — so a boosted graph
delivers no more absolute level at any frequency than an unboosted one at full
scale, and what the household actually spends is maximum SPL.  This gate
bounds that spend; the emitter's 12 dB rail, the per-driver soft-clip limiters
and the runtime contract's re-proof are unchanged and are not this class's to
weaken.

The admission bars are therefore the only new safety logic, and since the
2026-08-23 ruling every one of them is SHAPE.  A per-filter magnitude window,
the shared Q envelope, the declared band, the emitter's own filter vocabulary,
and :data:`DRIVER_MAX_COMPOSED_BOOST_DB` on the evaluated per-role cascade.
That last one is what sizes the class: it bounds the spend one document can
command at :data:`MAX_SPL_SPEND_BOUND_DB` = 13.0 dB.  What a boost owes over a
cut is no longer a bar but a NUMBER — that spend — which
:func:`_check_composed` measures at the boundary and the emitter re-proves.
The EVIDENCE that used to bar a filter is now
:func:`_check_classification`'s disclosure, counted onto the receipt so a
reader weighs it instead of the gate deciding for them.

**Where every bound comes from, and why not one of them is new.**  Restoring
rather than inventing is the #2730 precedent, and it is stronger here because
this seam's neighbour is a fit engine that has been emitting into the very
same branch for months:

===============================  ==================================  =========
bound                            restored from                       value
===============================  ==================================  =========
:data:`DRIVER_MAX_CUT_Q`         ``linearization_fit._PEAKING_Q_MAX``  8.0
:data:`DRIVER_MAX_FILTER_CUT_DB` ``linearization_fit.PER_FILTER_CUT_CAP_DB``  12.0
:data:`DRIVER_MAX_COMPOSED_CUT_DB` ``linearization_fit.MAX_NORMALIZATION_SPEND_DB``  18.0
:data:`DRIVER_MIN_CUT_DB`        ``linearization_fit._MIN_FILTER_GAIN_DB``  0.5
:data:`DRIVER_MAX_FILTERS_PER_ROLE` ``linearization_fit.MAX_FILTERS_PER_DRIVER``  8
:data:`DRIVER_MIN_Q`             ``blend_prescription.PRESCRIPTION_MIN_Q``  0.5
:data:`DRIVER_MAX_FILTER_BOOST_DB` ``linearization_fit.PER_FILTER_BOOST_CAP_DB``  12.0
:data:`DRIVER_MAX_COMPOSED_BOOST_DB` owner policy, ruling R8                   12.0
===============================  ==================================  =========

The per-filter BOOST ceiling is restored from the fit engine like the cut
ceilings above it.  **It was not always**: until 2026-08-22 both boost ceilings
came from the sibling ``blend_prescription`` class (3.0 and 4.0) on the owner's
2026-08-18 ruling that "a new permission should not open at the ceiling of an
old one", whose stated reason was that the fit's 12 dB rests on a closed-loop
delta probe and — as R8 quotes it — "a prescription has no such prediction".
Ruling **R8** of
``docs/tuning-master-plan.md`` overturns that on its own terms — under the
tournament every candidate banks a pre-registered expected delta before the
round measures it, which IS that closed-loop prediction — and the per-filter
ceiling moved to the emitter's own rail so a prescription at the ceiling is
emittable rather than accepted here and refused downstream.

The composed ceiling is the one bound here with no neighbour to restore from:
the fit engine has no composed cap (total boost is deliberately unbounded
there), so R8 sets it as policy at the same 12.0.  Its consequence is that two
boost filters both at the per-filter rail can never clear the composed cap, so
the composed cap binds every multi-filter boost.

Every bound with a source is RESTATED rather than imported, on ``camilla_yaml``'s
own lockstep rule: this gate is an independent re-validation of a document the
fit engine did not write, so inheriting the engine's policy constant would let a
future change to the engine silently move what an outside reader is allowed to
propose.  ``tests/test_crossover_v2_driver_prescription.py`` pins every such
pair numerically, so the duplication is a pinned lockstep and not a drift — and
it pins the composed ceiling, which has no pair, against its own literal.

**The band is the DRIVER's, and that is the point of the class.**  Not the
crossover region, not the radiating band — ``branch_chain.radiating_band_hz``
is the bound on a LIFT (#1809), and a cut past the handoff "is ordinary useful
work, because whatever leaks through still reaches the sum and removing it
spends no headroom" (``linearization_fit``'s own #2523 note).  What bounds a
cut is the driver's own declared band: its published response range, floored
by whatever protective high-pass it declares and capped by whatever protective
low-pass it declares.  See :func:`driver_passbands_from_safety_profile`.

**And BOTH signs carry an evidence DISCLOSURE here, where the blend class
carries a bar.**  In the blend region, "cuts are bounded and free; boosts pay
an evidence bar" — a cut there is cheap because the region is small, the
honesty mask has already removed the interference-flagged bins, and the
correction is graded a few hundred Hz wide.  None of those three hold out here.
A full-band per-driver cut reaches bins the merged mask never screened, at
frequencies no positional bar was ever computed for, and the 2026-08-19 night
measured what happens when a filter is aimed at the wrong KIND of feature: both
prescribed rounds were rolled back on skirt damage.  That is the reason the
classification is REPORTED on every filter, in the currency stage P3 rule 1
names — classification, not position.  It stopped being a refusal on
2026-08-23, when the owner ruled that a candidate inside the caps may be
tested: a filter aimed at the wrong kind of feature costs a rolled-back round,
which is what a round is FOR, and the 2026-08-22 evidence is that refusing on
it cost more than it saved — a role whose incumbent carried a Lowshelf could
not keep it, because nothing vouches for a filter the fit engine placed
(#2863).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

import numpy as np

# Leaf of the crossover_v2 DAG, exactly like the sibling it mirrors: the ONE
# biquad evaluator, the verdict register, the family's shared refusal type and
# blocklist, and the module that owns a driver's declared protection edges.
from jasper.active_speaker.branch_chain import (
    CHAIN_GRID_HZ,
    HEADROOM_MARGIN_DB,
    _evaluation_grid,
    chain_response,
)
# The EMITTER's own filter vocabulary and its own shelf-placement classifier,
# consumed rather than restated: "would the emitter accept this list" is the
# question this gate's shape bar asks, and it has one owner. That is the
# opposite of the lockstep rule the bounds below follow, and deliberately so —
# a POLICY number restated here is an independent re-validation, while the set
# of filters the graph can build is a fact about the graph.
from jasper.active_speaker.camilla_yaml import (
    LINEARIZATION_BIQUAD_TYPES,
    linearization_slot,
)
# The one Butterworth steepness every emitted linearization shelf carries. The
# emitter drops a shelf entry's own ``q`` and spells this instead, so a gate
# that evaluated a prescriber's number would be reading a filter the speaker
# will not play.
from jasper.camilla_config_contract import SHELF_Q
from jasper.active_speaker.driver_protection import (
    declared_protection_highpass_floor_hz,
    declared_protection_lowpass_ceiling_hz,
)
# ...and the owner of the persisted linearization entry this module merges INTO
# — imported for its one key name rather than restating the literal, because
# three modules have to agree on it or a hearing-relevant ceiling goes quiet.
from jasper.active_speaker.linearization_fit import MIC_TIER_FIELD

from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

from .blend_prescription import (
    PACKET_FINGERPRINT_FIELD,
    PRESCRIPTION_MIN_Q,
    PROHIBITED_PRESCRIPTION_KEYS,
    BlendPrescriptionRefused,
    find_prohibited_keys,
)
from .feature_classification import (
    DEFECT_BOOSTABLE,
    DEFECT_CUTTABLE,
    VERDICT_MATCH_TOLERANCE_OCTAVES,
    FeatureVerdict,
    defect_boostable_at,
    defect_cuttable_at,
)

#: The role-keyed bands a proposal is checked against, and the verdicts that
#: vouch for it. Aliased because these two travel together everywhere in this
#: module — a gate holding one without the other can answer only half of
#: "may this filter be aimed here".
DriverPassbands = Mapping[str, tuple[float, float]]

__all__ = [
    "DRIVER_MAX_COMPOSED_BOOST_DB",
    "DRIVER_MAX_COMPOSED_CUT_DB",
    "DRIVER_MAX_CUT_Q",
    "DRIVER_MAX_FILTERS_PER_ROLE",
    "DRIVER_MAX_FILTER_BOOST_DB",
    "DRIVER_MAX_FILTER_CUT_DB",
    "DRIVER_MIN_BOOST_DB",
    "DRIVER_MIN_CUT_DB",
    "DRIVER_MIN_Q",
    "DRIVER_PRESCRIPTION_KIND",
    "DRIVER_PRESCRIPTION_MAX_BYTES",
    "DRIVER_PRESCRIPTION_REFUSAL_REASONS",
    "DRIVER_PRESCRIPTION_TOO_LARGE",
    "DRIVER_PRESCRIPTION_SCHEMA_VERSION",
    "LINEARIZATION_CANDIDATE_FIELD",
    "MAX_SPL_SPEND_BOUND_DB",
    "BOOST_IN_CROSSOVER_OVERLAP",
    "ClassificationBasis",
    "check_driver_document_size",
    "DriverPassbands",
    "DriverPrescription",
    "driver_passbands_from_safety_profile",
    "driver_prescription_from_mapping",
    "driver_prescription_response_format",
    "driver_prescription_route",
    "driver_prescription_to_candidate_fields",
    "read_driver_prescription",
]


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

#: The document version a prescriber of THIS class answers.
#:
#: Its own number, starting at 1, rather than a bump of
#: :data:`~.blend_prescription.PRESCRIPTION_SCHEMA_VERSION`. That constant's own
#: rule is "bumped when the shape changes in a way an older prescriber's output
#: would no longer satisfy", and nothing about the blend shape changed here: a
#: document naming ``kind=jts_crossover_blend_prescription`` is parsed by the
#: same reader, against the same bounds, to the same answer, byte for byte. A
#: NEW kind is a new contract, not a new version of the old one — and versioning
#: them together would force every future change to either class to invalidate
#: the other's in-flight documents.
DRIVER_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator. Distinct from the blend class's, and that
#: distinctness is what makes the shared spool safe: an older reader handed a
#: newer envelope reaches this string, does not recognise it, and refuses —
#: rather than parsing a per-driver document as a blend one.
DRIVER_PRESCRIPTION_KIND = "jts_crossover_driver_prescription"

#: The byte ceiling on ONE per-driver document, applied once its class is known.
#:
#: **Two caps, one payload, and they are not the same job.** The family's
#: :data:`~.blend_prescription.PRESCRIPTION_MAX_BYTES` (64 KiB) stops a
#: pathological input being *parsed at all*, and it must be applied before the
#: document names its own kind — bytes too large to parse have no class yet, so
#: that refusal cannot belong to a class vocabulary. This one is a CONTENT bound
#: applied after the kind is read, and it is what makes
#: :data:`DRIVER_PRESCRIPTION_TOO_LARGE` a refusal this class can actually
#: raise rather than an advertised slug nothing reaches.
#:
#: 32 KiB, derived rather than picked, and the derivation is a MEASUREMENT
#: rather than the arithmetic that first stood here. The largest legitimate
#: document is one filter object per slot across every role a speaker plausibly
#: has — 8 filters × 4 roles — plus a full provenance block and a
#: 1,200-character rationale. Built and measured, pretty-printed at indent 2, it
#: is **6,046 bytes**; a first estimate of "under 4 KiB" was wrong by half and
#: the test that pins this caught it. 32 KiB is five times that, and half the
#: family ceiling. The test derives the figure from the schema's own constants
#: rather than trusting this comment, so a future ceiling that made documents
#: larger fails here instead of silently eating the margin.
DRIVER_PRESCRIPTION_MAX_BYTES = 32 * 1024

#: Ceiling on the free-text rationale, in characters. Same value and same
#: reason as the blend gate's: the text is stored and never parsed for
#: behaviour, so the cap bounds what gets banked rather than what is trusted.
RATIONALE_MAX_CHARS = 1_200

#: The candidate field a per-driver prescription lands in.
#:
#: ``MeasuredCrossoverCandidate.linearization`` — the role-keyed field the
#: Layer-1a fit already writes, reduced to the emitter's shape by
#: ``linearization_fit.linearization_filters_by_role`` and re-validated by
#: ``camilla_yaml._validated_linearization`` before any of it reaches
#: CamillaDSP. This is stage P3 rule 3's "Layer 1a's existing per-role stage",
#: named here so a caller folding a prescription onto a candidate does not
#: spell the field itself.
LINEARIZATION_CANDIDATE_FIELD = "linearization"


# --------------------------------------------------------------------------- #
# bounds — every one restored from the engine that already emits into this seam
# --------------------------------------------------------------------------- #

#: Widest Q one prescribed per-driver cut may use — ``linearization_fit.
#: _PEAKING_Q_MAX``, the fit engine's PEAKING ceiling, which is also the number
#: #2730 restored for the blend cut class on the 2026-08-19 ruling. The two
#: seams therefore agree about how narrow a cut may be, which is the right
#: relationship: a cut's width is a property of the feature it is aimed at, and
#: the feature does not care which stage the filter is emitted from.
#: **It bounds BOTH signs**, and this class deliberately does not call
#: ``blend_prescription.max_q_for_gain`` — whose boost arm is 2.0 — on the
#: owner's 2026-08-19 ruling that width is free ("filters can be whatever works
#: best to get flat"). What bounds a sharp boost here is what a sharp boost
#: costs: :data:`DRIVER_MAX_FILTER_BOOST_DB` per filter and
#: :data:`DRIVER_MAX_COMPOSED_BOOST_DB` on the evaluated cascade, which together
#: hold the maximum-SPL spend to :data:`MAX_SPL_SPEND_BOUND_DB` however narrow
#: the filter is. The classifier's opinion about the feature under it rides the
#: receipt as a disclosure (:func:`_check_classification`); since 2026-08-23 it
#: refuses nothing.
DRIVER_MAX_CUT_Q = 8.0

#: Narrowest Q, taken from the family's owner. Below this a Peaking filter is a
#: broadband tilt rather than a shape correction, and broadband per-driver level
#: is the trim's fact — a prescriber reaching for it is answering a question
#: this seam does not own.
#:
#: Deliberately the PRESCRIPTION floor (0.5) and not the fit engine's own
#: ``_PEAKING_Q_MIN`` (1.0). That constant is load-bearing for a different
#: property in a different module: it is the #1967 boost-exclusion DROP RADIUS,
#: and its docstring says so in as many words. Adopting a boost bound as a cut
#: bound would be borrowing a number for the one reason it was not chosen.
DRIVER_MIN_Q = PRESCRIPTION_MIN_Q

#: Deepest ONE prescribed cut may go, dB — ``linearization_fit.
#: PER_FILTER_CUT_CAP_DB``, the ceiling the fit engine re-proves as a hard
#: invariant with an explicit ``raise`` before it returns.
#:
#: Four times the blend class's 3.0 dB, and the asymmetry is not a loosening. The
#: blend ceiling is "what the blind zone was shown to hide, plus one model
#: error" over a few hundred Hz of SUMMED response; this one bounds a cut into
#: ONE branch, where the fit engine has been emitting up to it since Layer 1a
#: shipped. A prescriber allowed less than the engine that shares its seam would
#: be unable to express a defect the deterministic path can already correct.
DRIVER_MAX_FILTER_CUT_DB = 12.0

#: Ceiling on the COMPOSED cut's worst point over one role's passband, dB —
#: ``linearization_fit.MAX_NORMALIZATION_SPEND_DB``, the engine's own total
#: ledger for how far below its core-passband peak a driver may be taken.
#:
#: Enforced on the EVALUATED cascade rather than on a sum of gains, for
#: ``blend_prescription._check_composed``'s reason: two filters whose skirts
#: overlap deliver more than either alone. Per ROLE rather than across the
#: document, because the quantity being bounded is one branch's own spend and
#: two drivers do not share it.
DRIVER_MAX_COMPOSED_CUT_DB = 18.0

#: How shallow a cut may be before it is cosmetic —
#: ``linearization_fit._MIN_FILTER_GAIN_DB``, whose comment is the argument:
#: "below this magnitude a filter is cosmetic (inaudible, wastes a filter
#: slot)". A prescription is bounded to eight filters per role, so spending one
#: on an inaudible correction is spending a scarce thing on nothing.
DRIVER_MIN_CUT_DB = 0.5

#: Highest ONE prescribed boost may go, dB — ``camilla_yaml.
#: MAX_LINEARIZATION_BOOST_DB`` / ``linearization_fit.PER_FILTER_BOOST_CAP_DB``,
#: the rail the fit engine emits up to, restated on this module's lockstep rule
#: like every other bound here.
#:
#: **This overturns the owner's 2026-08-18 ruling** that stood on this constant
#: until 2026-08-22 — "a new permission should not open at the ceiling of an old
#: one", which opened this class at its sibling ``blend_prescription.
#: PRESCRIPTION_MAX_FILTER_BOOST_DB`` (3.0) instead. That ruling's stated reason
#: was that the fit's 12 dB rests on a closed-loop delta probe grading what the
#: fit predicted against what the speaker did, and — as R8 quotes it — "a
#: prescription has no such prediction". Ruling **R8** of
#: ``docs/tuning-master-plan.md`` (owner-ratified
#: 2026-08-21) overturns it ON ITS OWN TERMS: under the tournament every
#: candidate carries a **pre-registered expected delta**, banked before the round
#: measures it, which is exactly the closed-loop prediction whose absence was the
#: reason for opening low. The gate is now at the rail the emitter re-validates
#: against, so a prescription at this ceiling is emittable rather than accepted
#: here and refused downstream.
#:
#: Equal to :data:`DRIVER_MAX_COMPOSED_BOOST_DB` by R8, which has a consequence
#: worth naming: two boost filters BOTH at this rail can never clear the composed
#: cap, since each alone reads 12.0 there and any skirt overlap only adds (two
#: Q-8 boosts 0.30 octaves apart compose to 12.9526, and moving them a full third
#: of an octave apart still reads 12.7802). So the composed cap binds every
#: multi-filter boost, and this one binds only the single-filter case.
DRIVER_MAX_FILTER_BOOST_DB = 12.0

#: Ceiling on the COMPOSED boost's peak over one role's passband, dB.
#:
#: **POLICY, owner-ratified in ruling R8** (``docs/tuning-master-plan.md``,
#: 2026-08-21) — the one bound in this module that is not restored from a
#: neighbour, because there is no neighbour to restore it from: the fit engine
#: has no composed cap to inherit (``PER_FILTER_BOOST_CAP_DB`` is a per-filter
#: realization bound and total boost is deliberately unbounded there, by the
#: owner's 2026-07-27 ruling), and the sibling ``blend_prescription.
#: PRESCRIPTION_MAX_TOTAL_BOOST_DB`` (4.0) is the ceiling R8 moved off.
#:
#: This is the bound that sizes the whole class's cost, and it carries that
#: weight only because :func:`_composed_grid` reads the cascade on the same
#: span the charge is taken over. Given that, the remaining terms in the
#: emitted branch — the crossover sections and the per-driver trim — are
#: non-positive everywhere (measured and pinned), so a role's evaluated chain
#: peak cannot exceed this and ``branch_chain.headroom_charge_db`` cannot
#: charge more than :data:`MAX_SPL_SPEND_BOUND_DB`. **The span clause is
#: load-bearing**: a band-limited reading made this same sentence false while
#: every word of it about the other terms stayed true. Per ROLE for
#: :data:`DRIVER_MAX_COMPOSED_CUT_DB`'s reason, and the emitter folds the roles
#: by worst branch rather than by sum, so the document's total spend is that
#: bound and not a multiple of it.
DRIVER_MAX_COMPOSED_BOOST_DB = 12.0

#: How shallow a boost may be before it is cosmetic, dB. The same number as
#: :data:`DRIVER_MIN_CUT_DB` because it is the same argument — ``linearization_
#: fit._MIN_FILTER_GAIN_DB``'s "inaudible, wastes a filter slot", which does not
#: depend on the sign — and it is DEFINED by that constant rather than restated
#: beside it so the pair cannot drift. It carries its own name only so a boost's
#: refusal speaks in a prescriber's own vocabulary.
DRIVER_MIN_BOOST_DB = DRIVER_MIN_CUT_DB

#: The most maximum SPL one accepted document can cost the household, dB.
#:
#: DERIVED, not chosen, and **re-proved at 13.0 dB for ruling R8's widened caps**
#: (2026-08-22). The derivation, in four steps, each with its owner:
#:
#: 1. ``branch_chain.headroom_charge_db(peak) = peak + HEADROOM_MARGIN_DB`` for
#:    any peak above ``_PEAK_EPS_DB`` (0.01 dB). That is the whole charge
#:    formula — one addition, no other term.
#: 2. :func:`_check_composed` refuses any role whose evaluated cascade peak
#:    exceeds :data:`DRIVER_MAX_COMPOSED_BOOST_DB` by more than
#:    :data:`_COMPOSED_BOOST_EVAL_TOL_DB`, so an ACCEPTED document has
#:    ``peak <= 12.0 + 1e-9`` on the gate's own reading. That tolerance is
#:    carried rather than dropped; where it lands is stated after step 4,
#:    because step 4 contributes a second term to the same sum.
#: 3. **The span clause** — the step the whole proof rests on. That reading is
#:    taken on :func:`_composed_grid`, which is ``branch_chain._evaluation_grid``
#:    IMPORTED (the charge's own span) unioned with a dense sweep of the role's
#:    band. Gate and charge therefore read the same domain, and a union can only
#:    read HIGHER than either half. Without this clause the inference is
#:    unsound rather than merely loose: a band-limited gate once passed a
#:    cascade at 3.58 dB that the emitter charged 10.75 dB for, because the two
#:    were measuring different intervals. A premise can be true and the
#:    conclusion still false when they are about different domains.
#: 4. The emitter's peak cannot exceed the gate's, for TWO reasons that are both
#:    needed — the second was missing from an earlier revision of this list, and
#:    it turns out to be the larger term:
#:
#:    * the remaining terms in the emitted branch (the crossover sections and
#:      the per-driver trim) are non-positive to within 1e-8 dB, so
#:      ``cascade + sections <= cascade`` pointwise at any scale this system
#:      resolves. Measured and pinned: an LR section reaches a small POSITIVE
#:      maximum — floating-point residue from cascading up to eight biquads —
#:      that grows as the corner drops toward ~20 Hz. Worst **+8.4154e-10 dB**
#:      on ``CHAIN_GRID_HZ`` (the domain the emitter actually charges over) and
#:      **+1.1654e-09 dB** on the denser grid the pin stress-tests with, both at
#:      LR8 near 20 Hz. Search minima, so the pin's bar sits an order above
#:      them;
#:    * the emitter evaluates on ``_evaluation_grid(filters, CHAIN_GRID_HZ)``,
#:      which is a strict SUBSET of :func:`_composed_grid` (verified over 200
#:      random filter sets) — the gate adds a dense per-band sweep on top of the
#:      same base. A maximum over a subset cannot exceed the maximum over the
#:      superset.
#:
#: Therefore ``charge <= 12.0 + 1.0 = 13.0`` at published precision. Carrying
#: both tolerances instead of dropping them, the true ceiling is step 2's 1e-9
#: plus step 4's 1e-8 section allowance: ``13.000000011`` dB. Both are stated
#: here, after the step that contributes the second one, rather than in step 2
#: where the sum was not yet known.
#:
#: **The bound is ATTAINED, not approached**: one filter at
#: :data:`DRIVER_MAX_FILTER_BOOST_DB`, at any Q, composes to exactly 12.000000
#: on this gate's reading, so ``headroom_charge_db`` of it is exactly 13.000000.
#: So this is a tight maximum — the worst-case max-SPL spend one accepted
#: document can cost, which R8 records as moving 5 → 13 dB.
#:
#: Two independent checks. A random sweep over this gate's own rails, at its
#: pinned seed: of 2 000 sets of 1-4 Peaking filters, 1 538 are admitted and
#: their worst charge is 12.999377 dB, never crossing 13.0. That is EVIDENCE
#: over a sample, not a proof over the space — the proof is the four steps
#: above; the sweep is what would catch them being wrong. Its SHAPE is narrow
#: on purpose and the limits are worth knowing before leaning on it: Peaking
#: filters only, positive gains only, at most four of them, one band. Shelves,
#: mixed-sign cascades and the eight-filter ceiling are covered by the
#: dedicated refusal tests around it rather than by this draw. And the STRADDLE,
#: pinned end to end through the EMITTER in
#: ``tests/test_active_speaker_linearization_emission.py`` — two +9.0 dB Q-8
#: boosts 0.1233 octaves apart compose to 11.916 here and are charged 12.861 by
#: the emitter (admitted); move them to 0.1184 octaves and this gate refuses at
#: 12.088 composed, which would have been charged 13.067.
#:
#: Those two charge figures are the EMITTER's and run a little under
#: ``gate peak + 1.0`` (12.861 against 12.916). The gap is **0.0547 dB**, and it
#: splits the way step 4's two reasons predict — measured on that fixture, not
#: reasoned:
#:
#: * **0.0211 dB** is the crossover high-pass, which reads -0.021120 dB at the
#:   gate's peak (6752.603 Hz);
#: * **0.0336 dB** is the GRID, and it is the larger half: the emitter's coarser
#:   subset does not contain 6752.603 Hz at all and takes its maximum at
#:   6728.254 Hz instead.
#:
#: The peak does NOT relocate because the section is applied — checked on a
#: fixed grid, where adding the high-pass leaves the maximum at 6752.603 Hz.
#: The two frequencies differ because the two readings are on different grids,
#: which is a distinction worth keeping: one is a property of the filter, the
#: other of the sampling. One measured instance of step 4, not a demonstration
#: of the general claim.
#:
#: Imported rather than restated because it is a CONSEQUENCE of the charge
#: formula, not a policy this gate re-validates: a margin that moved must move
#: this too. Published on the refusal and on the round's own event so a reader
#: can see the ceiling beside the number that approached it.
#:
#: **What this bounds is the CHARGE, not the realized peak.** Above ~18 kHz the
#: sampling residue exceeds ``HEADROOM_MARGIN_DB`` (issue #2850, open at this
#: writing), so up there the -1.0 dB per-driver soft-clip limiters are the
#: backstop rather than this arithmetic — see the widened caps' entry in
#: ``branch_chain.HEADROOM_MARGIN_DB``'s own comment.
MAX_SPL_SPEND_BOUND_DB = DRIVER_MAX_COMPOSED_BOOST_DB + HEADROOM_MARGIN_DB

#: Slack allowed on the COMPOSED BOOST comparison alone, dB, so that the
#: evaluator's own double-precision residue cannot decide a policy question.
#:
#: Needed only because ruling R8 made :data:`DRIVER_MAX_FILTER_BOOST_DB` and
#: :data:`DRIVER_MAX_COMPOSED_BOOST_DB` the SAME number: a single filter at the
#: per-filter rail composes to that rail exactly in arithmetic, and the biquad
#: evaluates it to within a small residue whose SIGN depends on the centre
#: frequency and Q. Untolerated, +12.0 dB at Q 3 / 6245 Hz is admitted
#: (-5.3e-15) while +12.0 dB at Q 5 / 1600 Hz is refused (+7.8e-14) — the
#: published per-filter ceiling refusing at its own value, decided by low bits.
#:
#: **The residue's size is a swept figure, not the first one measured.** Those
#: two examples come from a hand-picked grid; over 4 000 random (freq, Q) draws
#: at the rail plus that grid, the worst |residue| is **2.416e-13 dB** at
#: 2015.4 Hz / Q 6.89 — about 3x the largest hand-picked value, and still
#: 4 139x under this tolerance. Quoting the hand-picked 7.8e-14 as if it bounded
#: the family would have been the narrow-fact-universal-tail mistake.
#:
#: 1e-9 dB is ~4 orders above that swept residue and ~7 below the 4-decimal
#: precision
#: every charge in this system is reported at, so it cannot absorb a real
#: cascade: :data:`MAX_SPL_SPEND_BOUND_DB` still bounds the charge to 13.0 dB at
#: every digit anything published carries.
#:
#: **Boost side only**, on the house rule against mirroring a fix into a sibling
#: path that is not broken. The cut comparison has the same shape but no such
#: collision — :data:`DRIVER_MAX_FILTER_CUT_DB` (12.0) sits well under
#: :data:`DRIVER_MAX_COMPOSED_CUT_DB` (18.0), so one filter at the per-filter cut
#: rail comes nowhere near the composed one.
_COMPOSED_BOOST_EVAL_TOL_DB = 1e-9

#: How many filters one role may carry — ``linearization_fit.
#: MAX_FILTERS_PER_DRIVER``, which is also ``camilla_yaml.
#: MAX_LINEARIZATION_FILTERS_PER_DRIVER``, the emitter's own re-validation. A
#: prescription past it would be accepted here and refused at emission, which is
#: the one failure shape a gate exists to prevent.
DRIVER_MAX_FILTERS_PER_ROLE = 8

#: The highest frequency the gate's biquad evaluator is defined for.
#:
#: Half of :data:`~jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`, imported from
#: the evaluator's own owner rather than written down here, on the rule
#: :mod:`.prescription_spool` follows for the same bound.
#:
#: It binds because a driver's DECLARED band is a datasheet fact and the
#: evaluator's is an arithmetic one, and they do not have to agree: a
#: supertweeter published to 40 kHz is an honest declaration, and a composed-cut
#: bound evaluated past Nyquist is an aliased number rather than a conservative
#: one. So the declared upper edge is CLAMPED here rather than the role being
#: dropped — a real tweeter must not lose its whole band to a datasheet's top
#: octave — and the packet publishes the clamped value, so the band a prescriber
#: is shown is the band it is judged against.
_EVALUABLE_MAX_HZ = RESPONSE_SAMPLE_RATE_HZ / 2.0


# --------------------------------------------------------------------------- #
# the refusal vocabulary — its own closed set, by slug, never by prose
# --------------------------------------------------------------------------- #

# A SECOND set beside the blend gate's, on `prescription_spool`'s rule: two
# sets because they answer two questions about two seams. `filter_outside_region`
# and `filter_outside_passband` are not the same refusal wearing two names —
# they name different bands with different owners, and a prescriber told the
# first when the second applied would go and re-read the crossover region while
# its filter was fine. The exception TYPE is shared, because every caller
# already handles that shape.

DRIVER_PRESCRIPTION_TOO_LARGE = "driver_prescription_too_large"
DRIVER_PRESCRIPTION_MALFORMED = "driver_prescription_malformed"
DRIVER_PRESCRIPTION_SCHEMA_UNSUPPORTED = "driver_prescription_schema_unsupported"
DRIVER_PRESCRIPTION_PACKET_MISMATCH = "driver_prescription_packet_mismatch"
DRIVER_PRESCRIPTION_PROVENANCE_MISSING = "driver_prescription_provenance_missing"
DRIVER_PRESCRIPTION_PROHIBITED_FIELD = "driver_prescription_prohibited_field"
FILTER_MALFORMED = "driver_filter_malformed"
FILTER_COUNT_EXCEEDED = "driver_filter_count_exceeded"
ROLE_UNKNOWN = "driver_role_unknown"
PASSBAND_UNAVAILABLE = "driver_passband_unavailable"
FILTER_OUTSIDE_PASSBAND = "driver_filter_outside_passband"
FILTER_Q_OUT_OF_RANGE = "driver_filter_q_out_of_range"
FILTER_CUT_TOO_DEEP = "driver_filter_cut_too_deep"
FILTER_CUT_TOO_SHALLOW = "driver_filter_cut_too_shallow"
COMPOSED_CUT_EXCEEDED = "driver_composed_cut_exceeded"
FILTER_BOOST_TOO_HIGH = "driver_filter_boost_too_high"
FILTER_BOOST_TOO_SHALLOW = "driver_filter_boost_too_shallow"
COMPOSED_BOOST_EXCEEDED = "driver_composed_boost_exceeded"

#: The boost's centre sits where two drivers' declared bands overlap — the
#: crossover knee. Owner ruling, 2026-08-19 (hearing lens), and the one
#: evidence-shaped bar on this door the 2026-08-23 ruling left standing: it is
#: a BAND bound, not a prediction about the feature there, and what it protects
#: is the crossover stage's own quantity.
#:
#: A per-driver boost there is charged nothing by the crossover stage and still
#: moves the SUMMED response, which is the crossover's own quantity to own.
#: ``linearization_fit`` bars its own engine from lifting in the radiating band
#: for #1809's reason; a prescriber may not reach past that from outside. CUTS
#: are unaffected: a cut past the handoff "is ordinary useful work, because
#: whatever leaks through still reaches the sum and removing it spends no
#: headroom", and no round has observed it failing.
#:
#: The bar is on the CENTRE only, which is the owner's never-nanny calibration
#: rather than an oversight: a boost centred just outside the overlap still
#: reaches into it on its skirt, and what adjudicates the summed response there
#: is the deciding-frame measurement, not a wider refusal here.
BOOST_IN_CROSSOVER_OVERLAP = "driver_boost_in_crossover_overlap"

# SIX slugs stood beside that one until 2026-08-23, all of them the
# classification bar's: `driver_feature_not_classified`,
# `driver_feature_not_cuttable`, `driver_feature_not_boostable`,
# `driver_feature_depth_unavailable`, `driver_boost_exceeds_feature_depth`, and
# `driver_boost_unvouched` (the same bar restated at the route). The owner's
# ruling made every one of them a DISCLOSURE — see `_check_classification` — so
# they are deleted rather than registered-but-unreachable: no reader maps a
# refusal slug back, and a vocabulary naming an answer this door can no longer
# give would mislead the prescriber reading `refusal_reasons`.

DRIVER_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    DRIVER_PRESCRIPTION_TOO_LARGE,
    DRIVER_PRESCRIPTION_MALFORMED,
    DRIVER_PRESCRIPTION_SCHEMA_UNSUPPORTED,
    DRIVER_PRESCRIPTION_PACKET_MISMATCH,
    DRIVER_PRESCRIPTION_PROVENANCE_MISSING,
    DRIVER_PRESCRIPTION_PROHIBITED_FIELD,
    FILTER_MALFORMED,
    FILTER_COUNT_EXCEEDED,
    ROLE_UNKNOWN,
    PASSBAND_UNAVAILABLE,
    FILTER_OUTSIDE_PASSBAND,
    FILTER_Q_OUT_OF_RANGE,
    FILTER_CUT_TOO_DEEP,
    FILTER_CUT_TOO_SHALLOW,
    COMPOSED_CUT_EXCEEDED,
    FILTER_BOOST_TOO_HIGH,
    FILTER_BOOST_TOO_SHALLOW,
    COMPOSED_BOOST_EXCEEDED,
    BOOST_IN_CROSSOVER_OVERLAP,
})

#: Top-level fields a proposal may carry. Anything else is refused rather than
#: ignored, on the family's rule: a misspelled ``filters`` that silently dropped
#: the whole prescription would leave the gate cheerfully accepting an empty one.
_PRESCRIPTION_FIELDS = frozenset({
    "artifact_schema_version",
    "kind",
    PACKET_FINGERPRINT_FIELD,
    "prescriber",
    "filters",
    "rationale",
    # Written BY the gate, accepted on the way back in so a durable block
    # round-trips through the SAME parser rather than needing a second, laxer
    # one. A request that supplies them is harmless: the class is re-derived
    # from the gains, the bands are taken from the packet rather than from the
    # document, and the classification basis is recomputed.
    "prescription_class",
    "passbands_hz",
    "classification_basis",
    "unvouched_filters",
    "composed_boost_db",
    "composed_boost_role",
    "max_spl_spend_bound_db",
    "displaced_filters",
    "displaced_boost_db",
    "displaced_boost_role",
    # FORWARD-compatible, not backward: this reader accepts everything
    # `to_dict` emits, but an OLDER build handed a NEWER receipt refuses it as
    # an unknown field — true of the boost class's fields, of #2863's
    # `displaced_*`, and of `unvouched_filters` above. Harmless today and recorded
    # rather than fixed, because no production caller reads a receipt back —
    # `driver_prescription_from_mapping` has test callers only, and the two
    # production readers are handed a prescriber's DOCUMENT, which never
    # carries these. A future field that a rollback path had to read would
    # need the tolerant reader this class has not yet had reason to build.
})

#: Fields ONE filter may carry. ``role`` is the addition that makes this class
#: what it is — and note that ``role_attenuations_db`` stays PROHIBITED. Naming
#: which driver a FILTER belongs to is this seam's whole subject; naming a
#: driver's LEVEL is the trim's fact and remains out of every prescriber's
#: reach.
_FILTER_FIELDS = frozenset({"role", "biquad_type", "freq", "q", "gain"})


@dataclass(frozen=True)
class ClassificationBasis:
    """Why one prescribed filter was allowed to be aimed where it was.

    Banked on the accepted prescription so a receipt six weeks later can name
    the verdict that admitted the filter, not merely that some verdict did. The
    blend class's :class:`~.blend_prescription.PositionalSupport` does the same
    job for its own bar, and for the same reason: a gate that recorded only
    "passed" leaves a reader unable to re-derive the decision.
    """

    #: The FILTER's own centre, and below it the classified feature it was
    #: matched to (whose own centre is the verdict's ``hz``). They differ by up
    #: to :data:`~.feature_classification.VERDICT_MATCH_TOLERANCE_OCTAVES`, and
    #: stating both is what lets a reader see a filter that only just cleared
    #: the match rather than one sitting on its target.
    filter_freq_hz: float
    role: str
    verdict: FeatureVerdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "filter_freq_hz": self.filter_freq_hz,
            "role": self.role,
            "match_tolerance_octaves": VERDICT_MATCH_TOLERANCE_OCTAVES,
            **self.verdict.to_dict(),
        }


@dataclass(frozen=True)
class DriverPrescription:
    """A validated per-driver correction and the evidence that justifies it.

    ``filters`` is a TOTAL for every role it names, not a delta — the whole
    per-driver correction that role should carry, exactly as
    :attr:`~.blend_prescription.BlendPrescription.filters` is for its region. A
    role the document does not name is not mentioned and is not changed.
    """

    #: The prescribed biquads, in emission order, each naming its own role.
    filters: tuple[dict[str, Any], ...]
    #: ``"cut"`` or ``"boost"``, derived from the gains and never read from the
    #: document. Same name and same meaning as the blend class's, so a receipt's
    #: attribution key reads the same across both.
    prescription_class: str
    #: The packet fingerprint this answered.
    packet_fingerprint: str
    #: Who authored it. Both required — a prescription with no author is a
    #: number with no way to ask about it later.
    prescriber_model: str
    prescriber_operator: str
    #: The per-role bands the proposal was checked against, echoed from the
    #: evidence, as ``((role, lo_hz, hi_hz), ...)`` sorted by role. A tuple
    #: rather than a mapping so the record stays hashable and ordered.
    passbands_hz: tuple[tuple[str, float, float], ...]
    #: The banked verdict that VOUCHES for each vouched filter, in filter
    #: order. Shorter than ``filters`` when some are unvouched — see
    #: :attr:`unvouched_filters` for how many, and why that is a disclosure
    #: rather than a refusal.
    classification_basis: tuple[ClassificationBasis, ...] = ()
    #: How many filters no banked verdict vouches for. ``None`` means nobody
    #: computed it (the durable read-back, which rebuilds no basis); ``0``
    #: means it was computed and every filter sits on a matching-sign defect
    #: verdict. The two are different facts, exactly as for
    #: ``displaced_filters`` below, and an empty ``classification_basis``
    #: cannot tell them apart — which is the whole reason this number is
    #: carried rather than derived.
    unvouched_filters: int | None = None
    #: The worst per-role composed boost the gate evaluated, dB, or ``None``
    #: when nothing evaluated it. ``0.0`` means "measured, and this document
    #: puts nothing above unity"; ``None`` means the durable read-back, which
    #: applies no bound and therefore computes no number. The two are different
    #: facts and a receipt that spelled them the same would claim a measurement
    #: it never made.
    composed_boost_db: float | None = None
    #: Which role carried that worst composed boost. The emitter folds by worst
    #: BRANCH, so the number alone cannot say where the spend went.
    composed_boost_role: str | None = None
    #: How many incumbent filters the named roles REPLACE — see
    #: :func:`_check_displaced`. ``None`` means no incumbent was available to
    #: compare against (the durable read-back, and any packet whose applied
    #: profile could not be read); ``0`` means one was, and it was empty.
    displaced_filters: int | None = None
    #: The worst amount the prescribed cascade sits ABOVE the incumbent one,
    #: dB, and the role that carried it. Same ``None``-versus-``0.0`` rule as
    #: ``composed_boost_db`` beside it, and the same first-role-wins tie rule.
    displaced_boost_db: float | None = None
    displaced_boost_role: str | None = None
    #: The prescriber's own words. **Never parsed for behaviour** — no branch in
    #: this module or any caller reads it, and it is excluded by construction
    #: from every instruction this harness renders.
    rationale: str = ""

    @property
    def roles(self) -> tuple[str, ...]:
        """Every role this prescription names, in emission order, once each."""
        return tuple(dict.fromkeys(str(entry["role"]) for entry in self.filters))

    def filters_for(self, role: str) -> tuple[dict[str, Any], ...]:
        """This role's filters, in emission order, with ``role`` stripped.

        The emitter's per-branch shape is ``{biquad_type, freq, q, gain}`` —
        the role is the KEY there, so carrying it inside the record too would
        put one fact in two places, and ``camilla_yaml._validated_biquad_entry``
        would refuse the extra field anyway.
        """
        return tuple(
            {key: value for key, value in entry.items() if key != "role"}
            for entry in self.filters
            if entry["role"] == role
        )

    def to_dict(self) -> dict[str, Any]:
        """The receipt's view: what was prescribed, and what justifies it."""
        return {
            "artifact_schema_version": DRIVER_PRESCRIPTION_SCHEMA_VERSION,
            "kind": DRIVER_PRESCRIPTION_KIND,
            "prescription_class": self.prescription_class,
            "filters": [dict(f) for f in self.filters],
            "passbands_hz": [
                [role, lo, hi] for role, lo, hi in self.passbands_hz
            ],
            PACKET_FINGERPRINT_FIELD: self.packet_fingerprint,
            "prescriber": {
                "model": self.prescriber_model,
                "operator": self.prescriber_operator,
            },
            "classification_basis": [
                basis.to_dict() for basis in self.classification_basis
            ],
            "unvouched_filters": self.unvouched_filters,
            "composed_boost_db": self.composed_boost_db,
            "composed_boost_role": self.composed_boost_role,
            "max_spl_spend_bound_db": MAX_SPL_SPEND_BOUND_DB,
            "displaced_filters": self.displaced_filters,
            "displaced_boost_db": self.displaced_boost_db,
            "displaced_boost_role": self.displaced_boost_role,
            "rationale": self.rationale,
        }


# --------------------------------------------------------------------------- #
# the band — read from the driver's OWN declaration
# --------------------------------------------------------------------------- #


def driver_passbands_from_safety_profile(
    profile: Any,
) -> dict[str, tuple[float, float]]:
    """Each role's own declared band, from the confirmed driver-safety profile.

    The band a per-driver filter must sit inside, composed from three declarations
    that already exist and are already gated elsewhere:

    * ``measurement_band_hz`` — "the driver's published frequency-response
      range" in ``driver_safety``'s own words to the research model. The base.
    * the declared protective HIGH-PASS, through
      :func:`~jasper.active_speaker.driver_protection.declared_protection_highpass_floor_hz`.
      This is #2736's floor — the number the apply transaction refuses a
      crossover below — and it is the right lower edge for a cut too: below its
      own protective corner a driver is not radiating program, so a filter there
      is aimed at nothing and can only spend a slot and shift phase.
    * the declared protective LOW-PASS, through the mirror reader. The woofer /
      mid analogue of the same fact.

    …and then clamped at :data:`_EVALUABLE_MAX_HZ`, which is the one edge that
    is not a declaration: a response above half the sample rate is undefined
    rather than merely out of policy, and a composed-cut bound evaluated there
    would be an aliased number wearing a safety bound's name.

    Neither protection edge is INVENTED where none is declared: an undeclared
    floor leaves ``measurement_band_hz``'s own lower edge standing, on
    ``declared_protection_highpass_floor_hz``'s never-nanny rule. Substituting
    the class-default policy corner would refuse honest proposals on a number
    the operator never declared.

    A target with no readable ``measurement_band_hz``, or one whose composed
    edges cross (a declared protection pair narrower than the published range
    admits), is OMITTED rather than given a guessed band. The gate then refuses
    that role by name, which is the honest answer: this speaker has not declared
    where that driver plays.
    """
    targets = profile.get("targets") if isinstance(profile, Mapping) else None
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        return {}
    out: dict[str, tuple[float, float]] = {}
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        role = target.get("role")
        if not isinstance(role, str) or not role.strip():
            continue
        band = target.get("measurement_band_hz")
        if not isinstance(band, Sequence) or isinstance(band, (str, bytes)):
            continue
        if len(band) != 2:
            continue
        lo, hi = _finite_or_none(band[0]), _finite_or_none(band[1])
        if lo is None or hi is None or not 0.0 < lo < hi:
            continue
        floor = declared_protection_highpass_floor_hz(target)
        if floor is not None and floor > lo:
            lo = floor
        ceiling = declared_protection_lowpass_ceiling_hz(target)
        if ceiling is not None and ceiling < hi:
            hi = ceiling
        hi = min(hi, _EVALUABLE_MAX_HZ)
        if not 0.0 < lo < hi:
            continue
        out[role.strip()] = (lo, hi)
    return out


# --------------------------------------------------------------------------- #
# the request gate
# --------------------------------------------------------------------------- #


def _refuse(reason: str, detail: str, **evidence: Any) -> NoReturn:
    raise BlendPrescriptionRefused(reason, detail, evidence=evidence or None)


def check_driver_document_size(payload: bytes) -> None:
    """This class's own size bound, applied once the document has named itself.

    Called by every reader that has established the document is a per-driver
    one — the CLI's gate and the spool's take — and by nothing else. See
    :data:`DRIVER_PRESCRIPTION_MAX_BYTES` for why two caps exist and which job
    each does.

    Both call sites have already been through a stat-or-length bound before the
    bytes were parsed, so this never runs on an unbounded read: it is the second
    of two, and the one that can speak in this class's vocabulary.
    """
    if len(payload) > DRIVER_PRESCRIPTION_MAX_BYTES:
        _refuse(
            DRIVER_PRESCRIPTION_TOO_LARGE,
            f"a per-driver prescription may be at most "
            f"{DRIVER_PRESCRIPTION_MAX_BYTES} bytes, got {len(payload)}",
            max_bytes=DRIVER_PRESCRIPTION_MAX_BYTES,
            got_bytes=len(payload),
        )


def _finite_or_none(value: Any) -> float | None:
    """One real number, or ``None`` — never a raise, never a coercion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _finite_number(value: Any, *, field: str) -> float:
    """One numeric field, strictly — no coercion, ever.

    ``bool`` is refused because it is an ``int`` in Python and ``gain=True``
    would read as a +1 dB boost. Strings are refused because ``float("1900")``
    succeeds, which would make this reader's strictness depend on the encoder's
    habits rather than on the contract. ``OverflowError`` is caught because
    ``10 ** 400`` is legal JSON, a legal Python ``int``, and raises rather than
    returning infinity — an escape that reached the blend CLI end to end before
    its own guard closed it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(
            FILTER_MALFORMED, f"{field} must be a number, got {type(value).__name__}"
        )
    try:
        number = float(value)
    except OverflowError:
        _refuse(FILTER_MALFORMED, f"{field} is too large to be a filter coefficient")
    if not math.isfinite(number):
        _refuse(FILTER_MALFORMED, f"{field} must be finite, got {number!r}")
    return number


def _parse_filters(raw: Any) -> tuple[dict[str, Any], ...]:
    """The filter list's SHAPE and its per-role count, and none of its bounds.

    Split from the bounds on the family's rule: the shape is what a durable
    read-back must also re-check, and the bounds are what only the request
    boundary applies. The per-role COUNT is here rather than with the bounds
    because it is a property of the document's shape — it does not need the
    evidence to decide.
    """
    if raw is None:
        _refuse(FILTER_MALFORMED, "a prescription must state a filters list")
    if isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        _refuse(FILTER_MALFORMED, f"filters must be a list, got {type(raw).__name__}")
    if not isinstance(raw, Sequence):
        _refuse(FILTER_MALFORMED, f"filters must be a list, got {type(raw).__name__}")
    out: list[dict[str, Any]] = []
    per_role: dict[str, int] = {}
    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} must be an object, got {type(entry).__name__}",
            )
        unknown = sorted(set(entry) - _FILTER_FIELDS)
        if unknown:
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} carries unknown field(s): {', '.join(unknown)}",
            )
        role = entry.get("role")
        if not isinstance(role, str) or not role.strip():
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} must name the driver role it belongs to",
            )
        role = role.strip()
        biquad_type = entry.get("biquad_type")
        if biquad_type not in LINEARIZATION_BIQUAD_TYPES:
            # The EMITTER's set, consumed. This bar is not a policy about what
            # is wise to prescribe; it is what the graph can be built out of, so
            # a type outside it would be accepted here and raise at emission.
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} must be one of "
                f"{sorted(LINEARIZATION_BIQUAD_TYPES)}, got {biquad_type!r}",
            )
        freq = _finite_number(entry.get("freq"), field=f"filter {position} freq")
        gain = _finite_number(entry.get("gain"), field=f"filter {position} gain")
        if biquad_type == "Peaking":
            q = _finite_number(entry.get("q"), field=f"filter {position} q")
        else:
            # A SHELF carries no steepness of its own, so the document does not
            # have to state one and a stated one is replaced rather than
            # honoured. Both halves of the loop already work this way: the
            # emitter's shelf `FilterSpec` carries no `q` at all
            # (`camilla_yaml._emit_driver_linearization_definitions`), and the
            # one biquad evaluator forces the same number
            # (`jasper.sound.profile._biquad_coeffs`'s `eff_q`). Writing it into
            # the record is what keeps the banked filter honest — the emitter's
            # own `_validated_biquad_entry` requires a positive `q`, and a
            # number nothing reads is worse than the number everything uses.
            q = SHELF_Q
        if freq <= 0.0:
            _refuse(FILTER_MALFORMED, f"filter {position} freq must be positive")
        per_role[role] = per_role.get(role, 0) + 1
        if per_role[role] > DRIVER_MAX_FILTERS_PER_ROLE:
            _refuse(
                FILTER_COUNT_EXCEEDED,
                f"role {role!r} carries {per_role[role]} filters; a driver's "
                f"branch may carry at most {DRIVER_MAX_FILTERS_PER_ROLE}",
                role=role,
                n_filters=per_role[role],
                max_filters=DRIVER_MAX_FILTERS_PER_ROLE,
            )
        out.append(
            {
                "role": role,
                "biquad_type": biquad_type,
                "freq": freq,
                "q": q,
                "gain": gain,
            }
        )
    _check_shelf_placement(out)
    return tuple(out)


def _check_shelf_placement(filters: Sequence[dict[str, Any]]) -> None:
    """The other half of the emitter's filter vocabulary: WHERE a shelf may sit.

    ``camilla_yaml`` accepts a shelf in exactly two places in one role's chain —
    leading (index 0), or a trailing ``Highshelf`` taper after a ``Lowshelf``
    lead (#1668) — and raises on any other placement, because the position is
    what names the emitted filter and two shelves in "peak" slots would collide.
    :func:`~jasper.active_speaker.camilla_yaml.linearization_slot` is that
    classifier, consumed rather than restated for
    :func:`~.evidence_packet.packet_incumbent_linearization`'s reason: "would the
    emitter accept this list" has one owner.

    Applied PER ROLE and in document order, which is the order the merged
    candidate field carries into the emitter. Peaking anywhere is always fine,
    so a document that names no shelf never reaches the loop's body.
    """
    for role in dict.fromkeys(str(entry["role"]) for entry in filters):
        indexed = [
            (position, entry)
            for position, entry in enumerate(filters)
            if str(entry["role"]) == role
        ]
        role_filters = [entry for _, entry in indexed]
        count = len(role_filters)
        for index, (position, entry) in enumerate(indexed):
            biquad_type = str(entry["biquad_type"])
            if biquad_type == "Peaking":
                continue
            if linearization_slot(index, count, role_filters) != "peak":
                continue
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} is a {biquad_type} at position {index} of "
                f"the {role}'s {count} filter(s), which the emitter cannot "
                "build: a shelf may only lead a driver's chain, or (a Highshelf "
                "taper) end it after a Lowshelf lead. Put the shelf first",
                role=role,
                biquad_type=biquad_type,
                role_position=index,
                role_filters=count,
            )


def _crossover_overlaps(
    passbands: DriverPassbands,
) -> tuple[tuple[float, float, str, str], ...]:
    """Every pairwise overlap of the declared bands, as ``(lo, hi, role, role)``.

    The crossover knee, derived from the bands this gate already holds — no new
    input, and deliberately not the preset's ``fc_hz``: what a boost must not
    move is the region where two drivers BOTH radiate, and the declared bands
    are what say where that is. On the shipped two-way that is the tweeter's
    1600 Hz protective floor up to the woofer's 3000 Hz protective ceiling.
    """
    items = sorted(passbands.items())
    out: list[tuple[float, float, str, str]] = []
    for index, (role_a, (lo_a, hi_a)) in enumerate(items):
        for role_b, (lo_b, hi_b) in items[index + 1:]:
            lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
            if lo < hi:
                out.append((lo, hi, role_a, role_b))
    return tuple(out)


def _check_bounds(
    filters: tuple[dict[str, Any], ...], passbands: DriverPassbands
) -> str:
    """Every per-filter bound, and the class the gains add up to.

    Returns ``"boost"`` when any gain is positive, else ``"cut"`` — the one
    producer of the receipt's class field, exactly as the blend gate's is.

    A document may mix signs; the class names what it is capable of, not what
    every filter does. Each filter is bounded by its OWN sign here, and
    :func:`_check_classification` reads the evidence per filter by the same
    sign.
    """
    overlaps = _crossover_overlaps(passbands)
    for position, entry in enumerate(filters):
        role = str(entry["role"])
        freq = float(entry["freq"])
        q = float(entry["q"])
        gain = float(entry["gain"])
        band = passbands.get(role)
        if band is None:
            _refuse(
                ROLE_UNKNOWN,
                f"filter {position} names role {role!r}, which this speaker's "
                "evidence declares no band for; the roles it does declare are "
                f"{sorted(passbands) or 'none'}",
                role=role,
                declared_roles=sorted(passbands),
            )
        lo, hi = band
        if not lo <= freq <= hi:
            _refuse(
                FILTER_OUTSIDE_PASSBAND,
                f"filter {position} at {freq:.1f} Hz is outside the {role}'s own "
                f"declared band {lo:.1f}-{hi:.1f} Hz",
                role=role,
                freq_hz=freq,
                passband_hz=[lo, hi],
            )
        if not DRIVER_MIN_Q <= q <= DRIVER_MAX_CUT_Q:
            _refuse(
                FILTER_Q_OUT_OF_RANGE,
                f"filter {position} Q {q:g} is outside "
                f"{DRIVER_MIN_Q:g}-{DRIVER_MAX_CUT_Q:g}",
                q=q,
                q_min=DRIVER_MIN_Q,
                q_max=DRIVER_MAX_CUT_Q,
            )
        if gain > 0.0:
            for lo_o, hi_o, role_a, role_b in overlaps:
                if lo_o <= freq <= hi_o:
                    _refuse(
                        BOOST_IN_CROSSOVER_OVERLAP,
                        f"filter {position} boosts {freq:.1f} Hz, inside the "
                        f"{lo_o:.1f}-{hi_o:.1f} Hz region where the {role_a} "
                        f"and {role_b} declared bands overlap. Both drivers "
                        "radiate there, so a per-driver boost moves the SUMMED "
                        "response the crossover stage owns and is charged "
                        "nothing for it. Correct the handoff, or aim the boost "
                        "outside the overlap. A cut here is allowed",
                        role=role,
                        freq_hz=freq,
                        gain_db=gain,
                        overlap_hz=[lo_o, hi_o],
                        overlap_roles=[role_a, role_b],
                    )
            if gain < DRIVER_MIN_BOOST_DB:
                _refuse(
                    FILTER_BOOST_TOO_SHALLOW,
                    f"filter {position} boosts {gain:.2f} dB at {freq:.1f} Hz, "
                    f"under the {DRIVER_MIN_BOOST_DB:g} dB below which a filter "
                    "is inaudible and spends one of this branch's eight slots "
                    "on nothing",
                    role=role,
                    freq_hz=freq,
                    gain_db=gain,
                    min_boost_db=DRIVER_MIN_BOOST_DB,
                )
            if gain > DRIVER_MAX_FILTER_BOOST_DB:
                _refuse(
                    FILTER_BOOST_TOO_HIGH,
                    f"filter {position} boosts {gain:.2f} dB at {freq:.1f} Hz, "
                    f"past the {DRIVER_MAX_FILTER_BOOST_DB:g} dB per-filter "
                    "ceiling, which is the same rail the deterministic fit "
                    "engine emits up to and the emitter re-validates against",
                    role=role,
                    freq_hz=freq,
                    gain_db=gain,
                    max_boost_db=DRIVER_MAX_FILTER_BOOST_DB,
                )
            continue
        if gain > -DRIVER_MIN_CUT_DB:
            _refuse(
                FILTER_CUT_TOO_SHALLOW,
                f"filter {position} cuts {-gain:.2f} dB, under the "
                f"{DRIVER_MIN_CUT_DB:g} dB below which a filter is inaudible and "
                "spends one of this branch's eight slots on nothing",
                role=role,
                gain_db=gain,
                min_cut_db=DRIVER_MIN_CUT_DB,
            )
        if gain < -DRIVER_MAX_FILTER_CUT_DB:
            _refuse(
                FILTER_CUT_TOO_DEEP,
                f"filter {position} cuts {-gain:.2f} dB, past the "
                f"{DRIVER_MAX_FILTER_CUT_DB:g} dB per-filter ceiling",
                role=role,
                gain_db=gain,
                max_cut_db=DRIVER_MAX_FILTER_CUT_DB,
            )
    return "boost" if any(float(e["gain"]) > 0.0 for e in filters) else "cut"


#: How densely one role's band is sampled before the filter centres go in.
#: 2048 points over a 3.6-octave band is ~0.0018 octaves, two orders inside a
#: Q-8 filter's own half-power width; it is the BACKGROUND sampling, and
#: :func:`_composed_grid` is what makes a narrow filter's peak visible.
_COMPOSED_GRID_POINTS = 2048


def _composed_grid(
    role_filters: Sequence[Mapping[str, Any]], lo: float, hi: float
) -> np.ndarray:
    """The grid the composed caps are read on: the CHARGE's own span, unioned
    with a dense sweep of the role's declared band.

    **Two independent failures live here, and each half fixes one.**

    *Domain.* The bound this gate publishes is a claim about what the EMITTER
    will charge, and ``camilla_yaml.linearization_headroom_db`` charges the
    cascade's peak over the whole spectrum — not over the declared band. A
    MIXED-SIGN cascade's extremum can sit outside the hull of its own centres:
    six ``+3.0 dB`` Q-0.7 filters at 40 Hz with two ``-12.0 dB`` Q-2.0 filters
    at 48 Hz, all inside the shipped woofer's 40-3000 Hz band, peak at
    **29.5 Hz** — below the band, where that branch has no protective
    high-pass — and the emitter charges **10.75 dB** for a document a
    band-limited reading passed at 3.58. So the span is
    :func:`~jasper.active_speaker.branch_chain._evaluation_grid`'s own, IMPORTED
    rather than mirrored: the gate and the charge cannot disagree about where to
    look, because it is one construction.

    *Resolution.* That span's background sampling is 1/48 octave over the
    14.55 octaves from ``_GRID_EDGE_LO_HZ`` to ``_GRID_EDGE_HI_HZ``, which is
    coarser inside one driver's band than a Q-8 filter needs;
    a dense per-band sweep is unioned in for that. Both halves are needed —
    the span alone under-reads three cases this module pins (a two-bell cascade
    peaking between its centres), and the band alone is the domain hole above.
    The union can only ever read HIGHER than either, which is the only
    direction a safety bound may move.

    ``_evaluation_grid`` also unions each filter's own centre, each adjacent
    pair's geometric midpoint, and the two domain edges, for the hazard its own
    docstring names: no fixed resolution can bound an arbitrary Q.
    """
    return _evaluation_grid(
        role_filters,
        np.concatenate([
            CHAIN_GRID_HZ, np.geomspace(lo, hi, _COMPOSED_GRID_POINTS),
        ]),
    )


def _check_composed(
    filters: tuple[dict[str, Any], ...], passbands: DriverPassbands
) -> tuple[float, str | None]:
    """Both composed caps, per role, on the EVALUATED cascade.

    Returns ``(worst composed BOOST across the document in dB, the role it
    belongs to)`` — ``(0.0, None)`` when nothing rises above unity. That number
    is what :data:`MAX_SPL_SPEND_BOUND_DB` bounds and what the receipt and the
    round's event report, so it is returned rather than recomputed by a second
    reader; the ROLE rides with it because the emitter folds by worst BRANCH,
    so "which branch" is the half that says where the spend went.

    Through :func:`~jasper.active_speaker.branch_chain.chain_response` — the ONE
    biquad evaluator in this codebase — so this gate and the emitter's own
    accounting cannot disagree about what CamillaDSP will realize.

    Evaluated on :func:`_composed_grid`, never on a supplied axis: the CHARGE's
    own span unioned with a dense sweep of the role's band. Two shipped
    readings of this gate were wrong before that grid took its present shape,
    and both were found by review rather than by a round — a fixed band-limited
    sweep under-read a narrow filter near a WIDE band's top edge (0.17 dB for a
    stack truly reaching 24.00), and a band-limited DOMAIN missed a mixed-sign
    cascade whose extremum sat BELOW the band entirely (3.58 read against a
    10.75 dB charge). ``_composed_grid`` carries the argument for each half.

    **The boost extreme is read off the SAME grid as the cut extreme**, so the
    two bounds cannot disagree about what the cascade does — and each widening
    incidentally tightened the CUT side too (a band-edge −3.0 dB cut had been
    reading −1.23). That was harmless, because a cut cannot clip and a cut
    bound reading low only ever refuses less, and it is now consistent.

    Both are read WITHOUT the crossover sections and WITHOUT the branch trim,
    and BOTH of those terms are non-positive to within 1e-8 dB — the trim by
    construction (``intervention.anchor_trims`` normalizes it, pinned) and the
    LR sections by measurement. Not exactly zero, which the old wording
    ("maxes at +0.000000000 dB") implied by rounding: a section's true maximum
    is a small POSITIVE floating-point residue, worst measured +8.4154e-10 dB
    on the emitter's own grid and +1.1654e-09 dB on a denser one, both at LR8
    near a 20 Hz corner. 1e-8 dB is what the pin asserts and the honest ceiling
    to quote; it is a hundred-millionth of the 1.0 dB margin, so the inference
    below is unaffected. So this reading is an UPPER
    bound on what the emitter
    will charge, which is the direction a gate's number has to err.

    That inference is only sound because the span is now the charge's own. It
    was not sound before: the terms were non-positive then too, but the gate
    was measuring a different domain from the one being bounded, so "every
    other term only subtracts" licensed nothing. A premise can be true and the
    conclusion still false when they are about different intervals.
    """
    worst_boost = 0.0
    worst_role: str | None = None
    for role, band in sorted(passbands.items()):
        role_filters = [
            {key: value for key, value in entry.items() if key != "role"}
            for entry in filters
            if entry["role"] == role
        ]
        if not role_filters:
            continue
        lo, hi = band
        grid = _composed_grid(role_filters, lo, hi)
        composed = 20.0 * np.log10(
            np.maximum(np.abs(np.asarray(chain_response(role_filters, grid))), 1e-12)
        )
        # "over its own band" is what this grid is deliberately NOT limited to
        # — `_composed_grid` reads the CHARGE's whole span, so an extremum can
        # and does land outside the declared band (measured as low as 1.92 Hz
        # and as high as 21.5 kHz). Both refusals therefore name the FREQUENCY
        # rather than an interval the number may not be inside: a reader told
        # only "at its peak over its own band" goes looking for a filter there
        # and finds none.
        worst_cut_index = int(np.argmin(composed))
        worst_cut = float(composed[worst_cut_index])
        if worst_cut < -DRIVER_MAX_COMPOSED_CUT_DB:
            _refuse(
                COMPOSED_CUT_EXCEEDED,
                f"the {role}'s composed cascade cuts {-worst_cut:.2f} dB at its "
                f"worst ({grid[worst_cut_index]:.1f} Hz), past the "
                f"{DRIVER_MAX_COMPOSED_CUT_DB:g} dB ceiling",
                role=role,
                composed_cut_db=worst_cut,
                composed_cut_hz=float(grid[worst_cut_index]),
                max_composed_cut_db=DRIVER_MAX_COMPOSED_CUT_DB,
            )
        peak_index = int(np.argmax(composed))
        peak_boost = max(0.0, float(composed[peak_index]))
        if peak_boost > DRIVER_MAX_COMPOSED_BOOST_DB + _COMPOSED_BOOST_EVAL_TOL_DB:
            _refuse(
                COMPOSED_BOOST_EXCEEDED,
                f"the {role}'s composed cascade boosts {peak_boost:.2f} dB at "
                f"its peak ({grid[peak_index]:.1f} Hz), past the "
                f"{DRIVER_MAX_COMPOSED_BOOST_DB:g} dB ceiling. Two filters "
                "whose skirts overlap deliver more than either alone, and "
                "every dB above unity is charged against the household's "
                "maximum SPL",
                role=role,
                composed_boost_db=peak_boost,
                composed_boost_hz=float(grid[peak_index]),
                max_composed_boost_db=DRIVER_MAX_COMPOSED_BOOST_DB,
                max_spl_spend_bound_db=MAX_SPL_SPEND_BOUND_DB,
            )
        # `>` and not `>=`: the FIRST role reaching the worst value keeps it, so
        # a tie is decided by the sorted role order rather than by which role
        # happened to be evaluated last. A `max()` that forgot to compare would
        # report the last role's number as the document's, which is a silent
        # under-report into the receipt and the event.
        #
        # `peak_boost > 0.0` and not `>= 0.0`: a document that puts NOTHING
        # above unity has no role that spent, and naming one anyway would make
        # the receipt attribute a spend of 0.0 dB to whichever role sorted
        # first. That is this record's own rule about `composed_boost_db`,
        # applied to the field beside it — "not applicable" and "measured
        # nothing" are different facts and only one of them has a role.
        if peak_boost > 0.0 and (worst_role is None or peak_boost > worst_boost):
            worst_boost, worst_role = peak_boost, role
    return worst_boost, worst_role


def _check_displaced(
    filters: tuple[dict[str, Any], ...],
    incumbent: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    passbands: DriverPassbands,
) -> tuple[int | None, float | None, str | None]:
    """What this document DELETES, and what deleting it changes. Never refuses.

    Returns ``(incumbent filters the named roles replace, the worst amount the
    prescribed cascade sits ABOVE the incumbent one in dB, the role that
    carried it)``, or ``(None, None, None)`` when the evidence carries no
    incumbent to compare against. ``0`` filters with ``0.0`` dB is a measured
    "this document displaces nothing"; ``None`` is "nobody knows", and the two
    are different facts a receipt may not spell the same way.

    **Why the number exists.** ``filters`` is a total for every role it names,
    so a role's incumbent filters are deleted by a document that names the role
    and does not repeat them. :func:`_check_composed` cannot see that: it reads
    the document's own cascade, which is exactly right for the caps it enforces
    and says nothing about what the cascade replaced. On 2026-08-22 a
    five-filter tweeter document displaced a −6.037 dB Lowshelf at 5844.67 Hz,
    and the round measured a 6.065 dB tilt step for a change no gate had a
    number for (#2863).

    **It DISCLOSES and never refuses, and the mechanism test is why.** The
    composed caps guard one thing — maximum-SPL spend, charged by
    ``camilla_yaml.linearization_headroom_db`` over the cascade the emitter
    actually realizes — and dropping an incumbent cut adds nothing to that
    cascade, so it spends no headroom and cannot clip. A driver's protective
    corners are not in this map either: they are declared as
    ``required_protection_filters`` on the design draft's driver-safety profile
    and enforced somewhere else entirely — ``graph_safety``'s emit-gate
    high-pass proof, ``path_safety``'s per-driver protection floor, and
    ``excitation_safety_plan``'s permitted bands — so no document routed here
    can delete one. With no component-damage mechanism to name, a refusal here
    would fail ``docs/measurement-loop-doctrine.md`` §5's nanny test — deleting a
    shelf to find out whether it is still earning its place is precisely the
    reversible experiment that doctrine protects.

    **Measured against the INCUMBENT, which is not quite what the merge
    replaces.** At staging time the round has not measured, so the fit this
    document will actually displace does not exist yet; the incumbent is what
    the speaker is playing and what the evidence packet can name. The two are
    the same question asked one round apart.

    **It is a PER-BRANCH number, and one case sits outside it.** Removing an
    incumbent BOOST also releases the pre-split attenuation that boost was
    charged (``camilla_yaml.linearization_headroom_db``), which raises the
    whole speaker rather than one branch's band. This reads only the branch's
    own cascade, so it reports that removal as the negative per-branch delta it
    is and says nothing about the whole-speaker step beside it. Named rather
    than papered over: the number is honest about the band it covers.
    """
    if incumbent is None:
        return None, None, None
    displaced = 0
    worst_boost = 0.0
    worst_role: str | None = None
    for role in sorted({str(entry["role"]) for entry in filters}):
        previous = [dict(entry) for entry in incumbent.get(role) or ()]
        displaced += len(previous)
        if not previous:
            continue
        role_filters = [
            {key: value for key, value in entry.items() if key != "role"}
            for entry in filters
            if entry["role"] == role
        ]
        lo, hi = passbands[role]
        # One grid over BOTH cascades, so neither one's extremum can fall
        # between the other's sample points — `_composed_grid` unions each
        # filter's own centre, and a delta read on a grid built from half the
        # filters would miss the incumbent's corner exactly where it matters.
        grid = _composed_grid(role_filters + previous, lo, hi)
        delta = 20.0 * np.log10(
            np.maximum(np.abs(np.asarray(chain_response(role_filters, grid))), 1e-12)
            / np.maximum(np.abs(np.asarray(chain_response(previous, grid))), 1e-12)
        )
        peak = max(0.0, float(np.max(delta)))
        # `> 0.0` and the first-role-wins tie rule, both read off
        # `_check_composed`: a document that raises nothing has no role that
        # displaced upward, and naming one anyway would attribute 0.0 dB to
        # whichever role sorted first.
        if peak > 0.0 and (worst_role is None or peak > worst_boost):
            worst_boost, worst_role = peak, role
    return displaced, worst_boost, worst_role


def _check_classification(
    filters: tuple[dict[str, Any], ...],
    verdicts: Sequence[FeatureVerdict] | None,
) -> tuple[tuple[ClassificationBasis, ...], int]:
    """Which filters a banked verdict VOUCHES for, and how many it does not.

    Returns ``(the vouching basis in filter order, the count of filters with no
    vouching verdict)``. **It refuses nothing**, and that is ruling 2026-08-23:
    the vouch is a prediction about whether a filter will help, and
    ``docs/measurement-loop-doctrine.md`` §2 gives a prediction the power to
    propose and never to dispose. A filter inside this gate's caps and bands
    spends nothing the household can hear more of, so §4's closed list has no
    component-damage mechanism to name for it, §3 puts every defect outside that
    list under "discloses and recommends a next action; it never blocks", and
    §5's nanny test fails a refusal here — trying an unclassified feature and
    measuring the result is exactly the reversible experiment that doctrine
    protects. What it cost while it did refuse is on the record: a role whose
    incumbent carried a Lowshelf could not keep it, because no banked verdict
    vouches for a filter the fit engine placed, so naming the role deleted the
    shelf (#2863).

    A verdict vouches when it is the NEAREST banked one to the filter's centre
    and its classification matches the filter's sign — ``defect_cuttable_at``
    for a cut, ``defect_boostable_at`` for a boost, each applying its own
    nearest-verdict-decides rule. One question, asked the same way for both
    signs, so a receipt reads the same across them.

    **Vouched is not "will help", and unvouched is not "will not".** A defect
    verdict says EQ is not structurally the wrong tool; run-log §9.2 says in as
    many words that it does not say EQ helps, and every EQ candidate played on
    2026-08-19 measured worse against the frozen reference. The round that
    follows answers both, and it answers by measuring.
    """
    if verdicts is None:
        # No banked classification at all: nothing is vouched, and the count
        # says so. Distinct from the ``None`` the caller carries for "nobody
        # computed this", which is what a durable read-back reports.
        return (), len(filters)
    basis: list[ClassificationBasis] = []
    unvouched = 0
    for entry in filters:
        freq = float(entry["freq"])
        role = str(entry["role"])
        matching = (
            defect_boostable_at if float(entry["gain"]) > 0.0 else defect_cuttable_at
        )
        vouching, _nearest = matching(verdicts, freq)
        if vouching is None:
            unvouched += 1
            continue
        basis.append(
            ClassificationBasis(filter_freq_hz=freq, role=role, verdict=vouching)
        )
    return tuple(basis), unvouched


def _prescriber(raw: Any) -> tuple[str, str]:
    """Who authored this, strictly and non-blank."""
    if not isinstance(raw, Mapping):
        _refuse(
            DRIVER_PRESCRIPTION_PROVENANCE_MISSING,
            "a prescription must carry a prescriber object naming its model "
            "and operator",
        )
    unknown = sorted(set(raw) - {"model", "operator"})
    if unknown:
        _refuse(
            DRIVER_PRESCRIPTION_PROVENANCE_MISSING,
            f"prescriber carries unknown field(s): {', '.join(unknown)}",
        )
    values: list[str] = []
    for field in ("model", "operator"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            _refuse(
                DRIVER_PRESCRIPTION_PROVENANCE_MISSING,
                f"prescriber.{field} must be a non-blank name",
            )
        values.append(" ".join(value.split()))
    return values[0], values[1]


def _rationale(raw: Any) -> str:
    """The prescriber's own words, bounded and never parsed."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        _refuse(
            DRIVER_PRESCRIPTION_MALFORMED,
            f"rationale must be text, got {type(raw).__name__}",
        )
    text = " ".join(raw.split())
    if len(text) > RATIONALE_MAX_CHARS:
        _refuse(
            DRIVER_PRESCRIPTION_MALFORMED,
            f"rationale must be at most {RATIONALE_MAX_CHARS} characters, got "
            f"{len(text)}",
        )
    return text


def _parse_prescription(
    raw: Mapping[str, Any],
) -> tuple[tuple[dict[str, Any], ...], str, str, str, str]:
    """Shape, identity and provenance — and none of the bounds.

    Shared whole between the request gate and the durable read-back, so the only
    thing that differs between those two is their gate policy.
    """
    if not isinstance(raw, Mapping):
        _refuse(
            DRIVER_PRESCRIPTION_MALFORMED,
            f"a prescription must be a mapping, got {type(raw).__name__}",
        )
    # The blocklist runs BEFORE the unknown-field check, and the order is the
    # point: every prohibited key is also an unknown one, so checking shape
    # first would report a prescriber reaching for `volume_db` or
    # `role_attenuations_db` as a typo. Those are different facts and only the
    # second is worth a distinct slug.
    prohibited = sorted(set(find_prohibited_keys(raw)))
    if prohibited:
        _refuse(
            DRIVER_PRESCRIPTION_PROHIBITED_FIELD,
            f"a prescription may not name {', '.join(prohibited)}: it supplies "
            "numbers into a fixed shape, never configuration, coefficients, or "
            "a per-role level",
            prohibited=prohibited,
        )
    unknown = sorted(set(raw) - _PRESCRIPTION_FIELDS)
    if unknown:
        _refuse(
            DRIVER_PRESCRIPTION_MALFORMED,
            f"unknown prescription field(s): {', '.join(unknown)}",
        )
    if raw.get("kind") != DRIVER_PRESCRIPTION_KIND:
        _refuse(
            DRIVER_PRESCRIPTION_MALFORMED,
            f"a prescription must name kind={DRIVER_PRESCRIPTION_KIND!r}, got "
            f"{raw.get('kind')!r}",
        )
    version = raw.get("artifact_schema_version")
    if version != DRIVER_PRESCRIPTION_SCHEMA_VERSION:
        _refuse(
            DRIVER_PRESCRIPTION_SCHEMA_UNSUPPORTED,
            f"this build speaks driver-prescription schema "
            f"{DRIVER_PRESCRIPTION_SCHEMA_VERSION}, got {version!r}",
            supported=DRIVER_PRESCRIPTION_SCHEMA_VERSION,
        )
    fingerprint = raw.get(PACKET_FINGERPRINT_FIELD)
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        _refuse(
            DRIVER_PRESCRIPTION_PROVENANCE_MISSING,
            f"a prescription must echo the packet's {PACKET_FINGERPRINT_FIELD}",
        )
    model, operator = _prescriber(raw.get("prescriber"))
    return (
        _parse_filters(raw.get("filters")),
        fingerprint.strip(),
        model,
        operator,
        _rationale(raw.get("rationale")),
    )


def read_driver_prescription(
    raw: Mapping[str, Any] | None,
    *,
    packet_fingerprint: Any,
    passbands_hz: DriverPassbands | None,
    classifications: Sequence[FeatureVerdict] | None,
    incumbent_filters: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> DriverPrescription | None:
    """THE request gate. One point, and the one place every bound is applied.

    ``None`` when there is no prescription — the deterministic path, untouched.
    Otherwise a validated :class:`DriverPrescription`, or
    :class:`~.blend_prescription.BlendPrescriptionRefused` naming which gate
    said no.

    The four keyword arguments are the evidence packet's own answers, read out
    of it by :mod:`.evidence_packet`'s named readers. Taking VALUES rather than
    the packet is what keeps this module a leaf of the DAG — the packet imports
    the response format from here — and it is exactly the shape
    :func:`~.blend_prescription.read_blend_prescription` already has.

    **All four are required and undefaulted**, on that function's rule: every
    other bound in this gate rests on a number the prescriber itself supplied,
    and these four are the only inputs a prescriber willing to lie cannot
    forge. A caller that forgot one would lose the evidence's own opinion and
    never know, which is precisely what a defaulted keyword hides —
    ``incumbent_filters`` most of all, because forgetting it costs no refusal
    and no error, only a silent ``None`` where a disclosure belonged.

    ``incumbent_filters`` bounds nothing. It is what the graph is already
    carrying, and it buys one disclosed number — see :func:`_check_displaced`
    for what that number is and why it is not a gate. ``None`` is a legitimate
    value ("this evidence does not say"), unlike the three above it.

    **Order is deliberate.** Shape, then identity, then the bands, then the
    per-filter bounds, then the composed cascade — every refusal, in the order
    that sends a prescriber somewhere different, because reporting a later
    failure for an earlier cause would send it to re-derive a number that was
    fine. The two DISCLOSURES run last, after the document is known to be
    admissible, so one the gate was going to refuse anyway never pays for their
    evaluation.

    **The bounds are inclusive.** A filter exactly at a ceiling is legal; one
    past it is refused.
    """
    if raw is None:
        return None
    filters, fingerprint, model, operator, rationale = _parse_prescription(raw)

    if not isinstance(packet_fingerprint, str) or not packet_fingerprint:
        _refuse(
            DRIVER_PRESCRIPTION_PACKET_MISMATCH,
            "the evidence packet carries no fingerprint to compare against",
        )
    if fingerprint != packet_fingerprint:
        _refuse(
            DRIVER_PRESCRIPTION_PACKET_MISMATCH,
            "this prescription answers a different evidence packet "
            f"({fingerprint[:12]}...) than the one supplied "
            f"({packet_fingerprint[:12]}...)",
            prescription_answers=fingerprint,
            packet_is=packet_fingerprint,
        )

    if not passbands_hz:
        _refuse(
            PASSBAND_UNAVAILABLE,
            "this speaker's evidence declares no per-driver band, so there is "
            "nothing a per-driver prescription could be checked against. The "
            "bands come from the confirmed driver-safety profile's own "
            "measurement_band_hz and required_protection_filters",
        )
    passbands = dict(passbands_hz)

    prescription_class = _check_bounds(filters, passbands)
    composed_boost_db, composed_boost_role = _check_composed(filters, passbands)
    # The last two refuse nothing, so they run after every bound: a document
    # the gate was going to reject anyway never pays for their evaluation.
    basis, unvouched_filters = _check_classification(filters, classifications)
    displaced_filters, displaced_boost_db, displaced_boost_role = _check_displaced(
        filters, incumbent_filters, passbands
    )

    prescription = DriverPrescription(
        filters=filters,
        prescription_class=prescription_class,
        packet_fingerprint=fingerprint,
        prescriber_model=model,
        prescriber_operator=operator,
        passbands_hz=tuple(
            (role, lo, hi) for role, (lo, hi) in sorted(passbands.items())
        ),
        classification_basis=basis,
        unvouched_filters=unvouched_filters,
        composed_boost_db=composed_boost_db,
        composed_boost_role=composed_boost_role,
        displaced_filters=displaced_filters,
        displaced_boost_db=displaced_boost_db,
        displaced_boost_role=displaced_boost_role,
        rationale=rationale,
    )
    driver_prescription_route(prescription)
    return prescription


def driver_prescription_route(prescription: DriverPrescription) -> str:
    """Which candidate field this prescription lands in.

    :data:`LINEARIZATION_CANDIDATE_FIELD` is the role-keyed field the Layer-1a
    fit already writes, so a prescribed per-driver filter is byte-shaped like a
    fitted one and passes the same emitter gates —
    ``camilla_yaml._validated_linearization`` re-validates every entry
    independently before any of it reaches CamillaDSP, and lands them in that
    role's own branch immediately after its crossover filters. Both signs take
    the same field: ``linearization`` accepts per-driver boosts to 12 dB and
    ``camilla_yaml.linearization_headroom_db`` absorbs them.

    It carries no condition of its own. It used to: until 2026-08-23 a boost
    reaching here without a vouching verdict refused ``driver_boost_unvouched``,
    which was :func:`_check_classification`'s bar restated as a property of the
    FUNCTION so the two ungated constructors could not step past it. That bar is
    now a disclosure by the same ruling, so restating it here would be the
    refusal the ruling removed, wearing a second name — and the spend a boost
    actually costs is bounded by :data:`MAX_SPL_SPEND_BOUND_DB`, which
    :func:`_check_composed` applies at the boundary and the emitter re-proves.
    """
    return LINEARIZATION_CANDIDATE_FIELD


def driver_prescription_to_candidate_fields(
    prescription: DriverPrescription | None,
    *,
    fitted: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The candidate fields a validated prescription contributes — MERGED BY ROLE.

    The sibling of :func:`~.blend_prescription.blend_prescription_to_candidate_fields`,
    and it exists for the same reason: a caller folding an outside value onto a
    candidate should not spell the field, and the value must enter **at
    candidate-build time** rather than be stamped on afterwards, because
    ``MeasuredCrossoverCandidate.fingerprint`` is ``field(init=False)`` — a
    content hash re-derived on read and refused as ``candidate_tampered``.

    **The compose-vs-replace ruling, and why this signature is what it is.**
    The blend class's answer does not carry over. There, a prescription and the
    solver produce ONE list for ONE region, so a prescribed total simply
    replaces a solved one. Here the field is role-keyed and two producers write
    into it — the Layer-1a fit writes every eligible role, a document names the
    subset its author had something to say about. Three options were on the
    table:

    * **replace wholesale** — the blend precedent, mechanically. A one-role
      document would silently discard the other driver's *fitted* filters, so a
      prescriber correcting the tweeter would un-linearize the woofer without
      saying so. Rejected.
    * **compose (append)** — prescribed filters added to the fitted ones for the
      same role. Doubles corrections at a frequency both aimed at, and can
      exceed the eight-filter branch ceiling from two authors neither of whom
      can see the total. Rejected.
    * **MERGE BY ROLE** — a document's named roles replace *those roles'*
      filters; unnamed roles keep their fitted ones. **Adopted (architect
      ruling, 2026-08-19.)** It is the only option under which the response
      format's own promise — "a role you do not name is not changed" — is true,
      and it keeps one author per branch so the branch ceiling has one owner.

    ``fitted`` is therefore **required and undefaulted**, on
    :func:`~.blend_prescription.read_blend_prescription`'s rule for exactly this
    hazard: it is the input a caller can forget while everything still appears
    to work, and the damage — a driver quietly losing its linearization — is
    invisible until somebody measures. Pass the candidate's own
    ``{role: LinearizationFit.to_dict()}`` mapping, or ``None`` when no fit ran.

    **Splatting the prescribed-only map is FORBIDDEN**, and there is no longer a
    way to obtain one from here. An earlier draft of this function returned only
    the prescribed roles and told callers they could "splat it unconditionally";
    those two sentences contradicted each other, and following the second would
    have produced exactly the dropped-role hazard the ruling above rejects.

    **The per-role value carries ``filters`` and nothing that would claim to be
    a fit.** ``linearization_filters_by_role`` reduces
    ``{role: LinearizationFit.to_dict()}`` by reading exactly one key, so this
    shape reduces correctly — but a prescription is not a fit and has no
    ``fit_band_hz``, no residual, and no ``reason_summary`` to report. Emitting
    those zeroed would bank a fit-quality claim nothing measured, which is the
    single thing this whole harness exists not to do. ``prescribed_by`` is
    carried instead, so a reader of the persisted candidate can tell a
    prescribed branch from a fitted one without joining it back to a receipt.

    **...with ONE exception, and the line is fit-QUALITY versus
    measurement-PROVENANCE.** :data:`MIC_TIER_FIELD` is carried forward from the
    role's own fitted entry when it had one. It is not a claim about this
    correction; it names the MICROPHONE that measured the round, and replacing a
    role's filters does not change which microphone that was. It has to survive
    because the candidate is the only carrier that crosses into the grading
    stage, and ``CrossoverV2Session._mic_trust_ceiling_hz`` reads the tier off
    this map to decide where the delta probe may grade at all (#2649). Dropping
    it on a document that names every role removed the ceiling silently and
    handed the probe untrusted HF — the exact defect #2649 closed.

    Nothing else crosses that line, and each omission was checked against its
    reader rather than assumed. ``reason_summary`` and
    ``fit_band_hz``/``residual_rms_db`` are read by the fit-reason disclosure
    and the attempt replay's comparability check, both of which SHOULD skip a
    prescribed branch; ``driver_class`` has no reader on this map at all (the
    browser surface reads it from the design draft), so carrying it would be
    the speculative flexibility the paragraph above trims.

    ``headroom_cost_db`` is the one omission this function does not OWN: the
    entries it returns carry no charge, and
    :func:`~.planning.build_candidate` stamps one onto every prescribed role
    before the map reaches a candidate (#2759). The split is that a charge is a
    property of the emitted CHAIN — filters, crossover sections, committed trim
    — and this is a pure function over a document and a fitted map, which holds
    the first of those three. Carrying the replaced fit's number instead would
    be worse than omitting it: it is that fit's charge for filters that are no
    longer emitted. So a caller that folds these entries onto a candidate
    WITHOUT charging them discloses 0.0 through ``worst_headroom_cost_db`` for
    a branch that genuinely spends maximum SPL — which is what a prescribed
    BOOST does once the bounded boost route is open (#2754).

    ``{}`` for a ``None`` prescription, whatever ``fitted`` holds: with no
    document there is nothing to merge, and the fit reaches the candidate by its
    own ordinary path. That keeps the no-prescription path byte-identical to
    today's.

    **It asks the route rather than spelling the field**, on the blend seam's
    rule: this function is the last thing between a prescription and a
    fingerprinted candidate field, and the one owner of "which field" answering
    again costs a function call. Since 2026-08-23 that answer carries no
    condition — see :func:`driver_prescription_route` — so this is a
    single-source-of-truth call and no longer a second gate.
    """
    if prescription is None:
        return {}
    field = driver_prescription_route(prescription)
    merged: dict[str, Any] = {
        str(role): value
        for role, value in (fitted or {}).items()
        if isinstance(role, str) and role.strip()
    }
    for role in prescription.roles:
        entry: dict[str, Any] = {
            "filters": [dict(f) for f in prescription.filters_for(role)],
            "prescribed_by": {
                "model": prescription.prescriber_model,
                "operator": prescription.prescriber_operator,
                PACKET_FINGERPRINT_FIELD: prescription.packet_fingerprint,
            },
        }
        # The one field that survives replacement — see the docstring. Read off
        # the entry being replaced, so a role the fit never reached carries none
        # and the ceiling's reader is told rather than left to infer.
        previous = merged.get(role)
        if isinstance(previous, Mapping) and previous.get(MIC_TIER_FIELD):
            entry[MIC_TIER_FIELD] = str(previous[MIC_TIER_FIELD])
        merged[role] = entry
    return {field: merged}


def driver_prescription_from_mapping(raw: Any) -> DriverPrescription | None:
    """A prescription read back out of this repository's own durable state.

    The read-back half of the pair, on the family's rule: the same shape and
    provenance checks, deliberately NOT the bounds, and ``None`` instead of a
    raise.

    Why no bounds. The only mappings that reach here were written by
    :func:`read_driver_prescription` accepting them, so re-applying a bound
    could not catch anything the boundary let through — it could only refuse a
    prescription whose declared band moved between the round that measured it
    and the stage that grades it, and refusing there would discard the evidence
    of a round that really ran. The bounds have one owner and it is the
    boundary.

    Why still strict about shape. A hand-edited state file is a real input, and
    a receipt that banked half a prescription would claim provenance it does not
    have.

    Note that this reader does NOT route and does NOT rebuild the
    classification basis: it re-derives ``prescription_class`` from the gains
    and applies no bound and no seam check, which is why
    :func:`driver_prescription_to_candidate_fields` asks
    :func:`driver_prescription_route` itself rather than assuming its input was
    gated.
    """
    if raw is None:
        return None
    try:
        filters, fingerprint, model, operator, rationale = _parse_prescription(raw)
    except BlendPrescriptionRefused:
        return None
    bands = _passbands_from_mapping(raw.get("passbands_hz") if isinstance(raw, Mapping) else None)
    if not bands:
        return None
    return DriverPrescription(
        filters=filters,
        prescription_class=(
            "boost" if any(float(f["gain"]) > 0.0 for f in filters) else "cut"
        ),
        packet_fingerprint=fingerprint,
        prescriber_model=model,
        prescriber_operator=operator,
        passbands_hz=bands,
        rationale=rationale,
    )


def _passbands_from_mapping(raw: Any) -> tuple[tuple[str, float, float], ...]:
    """The banked bands, or ``()`` so the caller reads it as absent.

    ``isfinite`` is the only numeric guard, and unlike the spool's band reader
    there is no Nyquist bound here because this reader EVALUATES nothing — the
    check would be a line no test could justify. The strict ordering is what
    keeps a degenerate band out of a record a later stage would print.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    out: list[tuple[str, float, float]] = []
    for entry in raw:
        if not isinstance(entry, Sequence) or isinstance(entry, (str, bytes)):
            return ()
        if len(entry) != 3 or not isinstance(entry[0], str) or not entry[0].strip():
            return ()
        lo, hi = _finite_or_none(entry[1]), _finite_or_none(entry[2])
        if lo is None or hi is None or not 0.0 < lo < hi:
            return ()
        out.append((entry[0].strip(), lo, hi))
    return tuple(out)


# --------------------------------------------------------------------------- #
# the response format — ONE owner, two consumers
# --------------------------------------------------------------------------- #


def driver_prescription_response_format() -> dict[str, Any]:
    """The contract a per-driver prescriber must satisfy, as data.

    Rendered into the evidence packet's ``response_format`` block beside the
    blend class's by :mod:`.evidence_packet`, and enforced by
    :func:`read_driver_prescription` here — one owner, so the instructions a
    prescriber is given and the gate it is judged by cannot describe different
    shapes.

    **It is a pure constant.** Nothing banked, measured, or household-authored
    reaches it, which is what makes prompt injection through the packet
    structurally impossible rather than merely filtered: a packet's instructions
    are these bytes whatever the round measured. That is also why the per-role
    BANDS and the banked VERDICTS are not in here — those are evidence, they
    vary per round, and they live in the packet's own evidence blocks. This
    block states that the bounds exist and where to read them; it never states
    their values for a particular speaker.
    """
    return {
        "artifact_schema_version": DRIVER_PRESCRIPTION_SCHEMA_VERSION,
        "kind": "jts_crossover_driver_prescription_contract",
        "the_other_class": (
            "system-level shaping inside the crossover region is a different "
            "class with different bounds and its own contract in this packet's "
            "'response_format' block"
        ),
        "what_this_class_is_for": (
            "one DRIVER's own full-band shape, corrected in that driver's own "
            "branch. Use it for a defect that belongs to one driver — a "
            "breakup mode, a cone or diaphragm resonance, a horn artefact — "
            "anywhere inside that driver's declared band, including well "
            "outside the crossover window. The blend class is the other "
            "instrument and it is for genuinely system-level shaping inside "
            "the crossover region; a shared filter is the wrong tool for a "
            "one-driver problem and charges both branches for it"
        ),
        "required_top_level": {
            "artifact_schema_version": DRIVER_PRESCRIPTION_SCHEMA_VERSION,
            "kind": DRIVER_PRESCRIPTION_KIND,
            PACKET_FINGERPRINT_FIELD: (
                "copy the packet's own fingerprint field verbatim; a "
                "prescription that names a different packet is refused"
            ),
            "prescriber": {
                "model": "the model that authored this, e.g. 'claude-opus-5'",
                "operator": "the person who ran it",
            },
            "filters": (
                "0 or more objects, each {role: <driver role>, biquad_type: "
                "<one of the biquad_types below>, freq: <Hz>, gain: <dB, "
                "negative to cut or positive to boost>}, plus q: <number> on a "
                "Peaking. A shelf takes no q: it is emitted at one fixed "
                "Butterworth steepness, so any q you send is replaced by it"
            ),
        },
        "optional_top_level": {
            "rationale": (
                f"free text, at most {RATIONALE_MAX_CHARS} characters. It is "
                "stored for a human reader and is NEVER parsed for behaviour: "
                "no argument made here can widen a bound below."
            ),
        },
        "filters_are_a_total": (
            "for every role you name, prescribe the WHOLE per-driver "
            "correction that branch should carry, not a delta. A role you do "
            "not name is not changed"
        ),
        "bounds": {
            "max_filters_per_role": DRIVER_MAX_FILTERS_PER_ROLE,
            # The EMITTER's set, so the contract a prescriber reads and the
            # graph it will be built into name the same filters.
            "biquad_types": sorted(LINEARIZATION_BIQUAD_TYPES),
            "where_a_shelf_may_sit": (
                "leading a role's chain, or — a Highshelf only — ending it "
                "after a Lowshelf lead. Anywhere else the emitter cannot name "
                "the filter and the document is refused. Peaking sits anywhere"
            ),
            "q_min": DRIVER_MIN_Q,
            "q_max": DRIVER_MAX_CUT_Q,
            "min_cut_db": DRIVER_MIN_CUT_DB,
            "max_filter_cut_db": DRIVER_MAX_FILTER_CUT_DB,
            "max_composed_cut_db": DRIVER_MAX_COMPOSED_CUT_DB,
            "min_boost_db": DRIVER_MIN_BOOST_DB,
            "max_filter_boost_db": DRIVER_MAX_FILTER_BOOST_DB,
            "max_composed_boost_db": DRIVER_MAX_COMPOSED_BOOST_DB,
            "freq_must_be_inside": (
                "the named role's own band in the packet's drivers block — the "
                "driver's published response range, floored by any protective "
                "high-pass it declares and capped by any protective low-pass"
            ),
            "composed_cap_is_evaluated": (
                "the composed cap is checked per role on the evaluated biquad "
                "cascade over that role's whole band, not on a sum of gains: "
                "two filters whose skirts overlap deliver more than either "
                "alone"
            ),
            "match_a_cut_to_its_feature": (
                "the packet's classification block reports each feature's own "
                "measured Q in feature_classification.verdicts[]. A filter "
                "wider than its target spends most of its action on the "
                "skirts, which the 2026-08-19 round measured as damage there"
            ),
        },
        "boosts": {
            "note": (
                "a positive gain is admitted, and it costs the household "
                "maximum SPL rather than safety: the graph attenuates the "
                "program before the split by what the worst branch puts above "
                "unity, so a boosted graph is never LOUDER at any frequency "
                "than an unboosted one at full scale — it reaches full scale "
                "at a lower volume setting. Boost only where the evidence says "
                "the driver has a real dip, and only as deep as the dip"
            ),
            "eligible_classification": DEFECT_BOOSTABLE,
            "a_boost_owes_nothing_a_cut_does_not": (
                "both signs are bounded by the same caps and disclosed by the "
                "same classification_bar. What differs is the COST: a cut "
                "spends a filter slot, a boost also spends maximum SPL, up to "
                "max_spl_spend_bound_db. Boost no deeper than the dip the "
                "verdict measured (its depth_db is in the packet) — nothing "
                "refuses a deeper one, and nothing makes it work either"
            ),
            "max_spl_spend_bound_db": MAX_SPL_SPEND_BOUND_DB,
            "spend_is_a_step_function": (
                "a branch that stays at or under unity is charged NOTHING. The "
                "first admissible boost in a band the branch already runs at "
                "full output costs about 1.5 dB of maximum SPL (its own "
                "magnitude plus a 1 dB margin), and each further dB costs about "
                "1 dB. A boost buried in that branch's own crossover stopband "
                "reaches nothing and is charged nothing"
            ),
            "not_at_the_crossover_knee": (
                "a boost may not sit where two drivers' declared bands OVERLAP. "
                "Both radiate there, so a per-driver boost moves the summed "
                "response the crossover stage owns and is charged nothing for "
                "it — correct the handoff instead. A cut in the overlap is "
                "allowed"
            ),
            "refusals": sorted({
                BOOST_IN_CROSSOVER_OVERLAP,
                FILTER_BOOST_TOO_HIGH,
                FILTER_BOOST_TOO_SHALLOW,
                COMPOSED_BOOST_EXCEEDED,
            }),
        },
        "classification_bar": {
            "it_discloses_and_never_refuses": (
                "this is EVIDENCE, not a gate. Every filter is checked against "
                "feature_classification.verdicts[] — never the lab_rows[] "
                "working beside it — and the count that no banked verdict "
                "vouches for comes back as prescription.unvouched_filters, on "
                "the propose/stage report and under --json. Nothing about it "
                "refuses: what a filter costs is bounded by the caps above, and "
                "whether it HELPS is what the next round measures"
            ),
            "what_a_vouch_means": (
                "the nearest banked verdict to your centre frequency types the "
                "feature as a minimum-phase, speaker-own defect of your "
                "filter's sign. Unvouched means the nearest verdict disagrees "
                "with your sign, or is an interference null / room arrival / "
                "unresolved feature, or there is no verdict there at all — a "
                "cut aimed at a cancellation lowers the direct sound and the "
                "delayed copy together, a boost aimed at one feeds it, and a "
                "room arrival is not the speaker's to correct. Those are the "
                "reasons an unvouched filter usually measures worse, which is "
                "why the count is on the report you read before you stage"
            ),
            # Per SIGN, and a pair rather than one key: a reader that walked
            # the keys used to find only the cut's eligible class and could
            # reasonably conclude a boost had no bar to satisfy.
            "eligible_classification_for_a_cut": DEFECT_CUTTABLE,
            "eligible_classification_for_a_boost": DEFECT_BOOSTABLE,
            "the_sign_must_match_the_feature": (
                f"{DEFECT_CUTTABLE!r} admits a CUT and {DEFECT_BOOSTABLE!r} "
                "admits a BOOST; neither admits the other, because cutting a "
                "dip deepens it and boosting a peak grows it. Classified "
                "features can sit closer together than the match tolerance "
                "below — on the 2026-08-19 record two peak/dip pairs are 0.14 "
                "and 0.16 octaves apart — so the NEAREST verdict to your centre "
                "frequency is the one that decides, and a peak nearby cannot "
                "vouch for a filter sitting on a dip or the reverse"
            ),
            "match_tolerance_octaves": VERDICT_MATCH_TOLERANCE_OCTAVES,
            "necessary_not_sufficient": (
                "a defect verdict says EQ is not structurally BARRED. It does "
                "not say EQ will help — every EQ candidate played on 2026-08-19 "
                "measured worse against the frozen reference. The round that "
                "follows is what answers that, by measuring"
            ),
            "if_evidence_is_missing": (
                "every filter comes back unvouched and the document is still "
                "admitted. Classify the features if you want the evidence; "
                "propose without it if you would rather let the round decide"
            ),
            "repeating_an_incumbent_filter_is_the_common_case": (
                "a document is a TOTAL for every role it names, so keeping "
                "what a branch already carries means listing it again. Those "
                "filters were placed by the fit engine and usually have no "
                "verdict of their own, so they come back unvouched — which is "
                "a disclosure, not an obstacle. Read the packet's "
                "incumbent.linearization block and repeat what you mean to keep"
            ),
        },
        "refusal_reasons": sorted(DRIVER_PRESCRIPTION_REFUSAL_REASONS),
        "prohibited_keys": sorted(PROHIBITED_PRESCRIPTION_KEYS),
        "execution_boundary": {
            "model_may_propose": True,
            "model_may_execute": False,
            "model_may_grade_itself": False,
            "jts_validates_and_measures": True,
            "note": (
                "an accepted prescription becomes an ordinary measured "
                "candidate: the same admission, safety-envelope, headroom and "
                "variance gates every automatic round faces, and the round's "
                "own adoption verdict decides keep or restore. On a round "
                "whose per-driver fit ran, the verify gates apply identically "
                "— the round models the graph your filters will actually "
                "produce. The pre-apply screen asks only that the prediction "
                "not be WORSE than the measurement it replaces; what settles "
                "whether a cut helped is the measured round, not a model"
            ),
        },
    }
