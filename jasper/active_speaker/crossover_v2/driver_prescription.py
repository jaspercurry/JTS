# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE driver's full-band shape correction, prescribed from outside — cuts only.

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
  eligible."  Enforced here as :data:`FEATURE_NOT_CUTTABLE`, against banked
  verdicts, by :mod:`.feature_classification`.
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

**Cuts only, and the boost class is structurally unreachable rather than
merely refused.**  A cut removes level and can therefore never clip, whatever
its width or depth, which is why this class could open at all without a
headroom decision.  A per-driver BOOST is the opposite: every dB of it is
charged against ``camilla_yaml.linearization_headroom_db`` and absorbed by
``active_baseline_headroom``, so opening it spends the graph's finite budget
and is a gain-structure change reviewed as such.  That decision is the
owner's and has not been made, so :func:`driver_prescription_route` refuses
the class by name — and, one layer earlier, :func:`_check_bounds` refuses a
positive gain before a class receipt can even be derived from it.  Two
independent gates, on :mod:`.blend_prescription`'s rule: the seam's promise
should be a property of the FUNCTION, not of the current call graph.

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
===============================  ==================================  =========

They are RESTATED rather than imported, on ``camilla_yaml``'s own lockstep rule:
this gate is an independent re-validation of a document the fit engine did not
write, so inheriting the engine's policy constant would let a future change to
the engine silently move what an outside reader is allowed to propose.
``tests/test_crossover_v2_driver_prescription.py`` pins every pair
numerically, so the duplication is a pinned lockstep and not a drift.

**The band is the DRIVER's, and that is the point of the class.**  Not the
crossover region, not the radiating band — ``branch_chain.radiating_band_hz``
is the bound on a LIFT (#1809), and a cut past the handoff "is ordinary useful
work, because whatever leaks through still reaches the sum and removing it
spends no headroom" (``linearization_fit``'s own #2523 note).  What bounds a
cut is the driver's own declared band: its published response range, floored
by whatever protective high-pass it declares and capped by whatever protective
low-pass it declares.  See :func:`driver_passbands_from_safety_profile`.

**And a cut carries an evidence bar, which inverts the family's usual
posture.**  In the blend region, "cuts are bounded and free; boosts pay an
evidence bar" — a cut there is cheap because the region is small, the honesty
mask has already removed the interference-flagged bins, and the correction is
graded a few hundred Hz wide.  None of those three hold out here.  A full-band
per-driver cut reaches bins the merged mask never screened, at frequencies no
positional bar was ever computed for, and the 2026-08-19 night measured what
happens when a filter is aimed at the wrong KIND of feature: both prescribed
rounds were rolled back on skirt damage.  So this class pays the bar the blend
class does not, and it pays it in the currency stage P3 rule 1 names —
classification, not position.
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
from jasper.active_speaker.branch_chain import chain_response
from jasper.active_speaker.driver_protection import (
    declared_protection_highpass_floor_hz,
    declared_protection_lowpass_ceiling_hz,
)

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
    defect_cuttable_at,
)

#: The role-keyed bands a proposal is checked against, and the verdicts that
#: vouch for it. Aliased because these two travel together everywhere in this
#: module — a gate holding one without the other can answer only half of
#: "may this cut be made here".
DriverPassbands = Mapping[str, tuple[float, float]]

__all__ = [
    "DRIVER_MAX_COMPOSED_CUT_DB",
    "DRIVER_MAX_CUT_Q",
    "DRIVER_MAX_FILTERS_PER_ROLE",
    "DRIVER_MAX_FILTER_CUT_DB",
    "DRIVER_MIN_CUT_DB",
    "DRIVER_MIN_Q",
    "DRIVER_PRESCRIPTION_KIND",
    "DRIVER_PRESCRIPTION_MAX_BYTES",
    "DRIVER_PRESCRIPTION_REFUSAL_REASONS",
    "DRIVER_PRESCRIPTION_TOO_LARGE",
    "DRIVER_PRESCRIPTION_SCHEMA_VERSION",
    "LINEARIZATION_CANDIDATE_FIELD",
    "BOOST_ROUTE_UNAVAILABLE",
    "FEATURE_NOT_CLASSIFIED",
    "FEATURE_NOT_CUTTABLE",
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

#: No banked verdict covers the frequency this filter is aimed at.
#:
#: Deliberately distinct from :data:`FEATURE_NOT_CUTTABLE`, and the split is the
#: same one :data:`~.blend_prescription.INSUFFICIENT_POSITIONAL_EVIDENCE` draws
#: against ``boost_dip_not_stable``: this is "go and classify", an instruction a
#: prescriber can act on, not a verdict on the proposal.
FEATURE_NOT_CLASSIFIED = "driver_feature_not_classified"

#: A verdict covers the frequency and it does not admit a cut — the feature is
#: an interference null, a room arrival, a minimum-phase DIP (which a cut would
#: deepen), or one the classifier could not resolve. Stage P3 rule 1's refusal.
FEATURE_NOT_CUTTABLE = "driver_feature_not_cuttable"

#: A positive gain. Parked on the owner's headroom decision — see the module
#: docstring and :func:`driver_prescription_route`.
BOOST_ROUTE_UNAVAILABLE = "driver_boost_route_unavailable"

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
    FEATURE_NOT_CLASSIFIED,
    FEATURE_NOT_CUTTABLE,
    BOOST_ROUTE_UNAVAILABLE,
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
})

#: Fields ONE filter may carry. ``role`` is the addition that makes this class
#: what it is — and note that ``role_attenuations_db`` stays PROHIBITED. Naming
#: which driver a FILTER belongs to is this seam's whole subject; naming a
#: driver's LEVEL is the trim's fact and remains out of every prescriber's
#: reach.
_FILTER_FIELDS = frozenset({"role", "biquad_type", "freq", "q", "gain"})


@dataclass(frozen=True)
class ClassificationBasis:
    """Why one prescribed cut was allowed to be aimed where it was.

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
    #: ``"cut"`` always, today. The field exists rather than being implied so
    #: the receipt's attribution key has the same name and the same meaning as
    #: the blend class's, and so the day a boost class opens it does not have to
    #: be introduced into a record shape that never had one.
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
    #: The verdict that admitted each filter, in filter order.
    classification_basis: tuple[ClassificationBasis, ...] = ()
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
            "rationale": self.rationale,
        }


# --------------------------------------------------------------------------- #
# the band — read from the driver's OWN declaration
# --------------------------------------------------------------------------- #


def driver_passbands_from_safety_profile(
    profile: Any,
) -> dict[str, tuple[float, float]]:
    """Each role's own declared band, from the confirmed driver-safety profile.

    The band a per-driver cut must sit inside, composed from three declarations
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
        if entry.get("biquad_type") != "Peaking":
            # Peaking only. The fit engine also emits shelves, but a shelf is a
            # LEVEL move across a driver's whole top or bottom, and per-driver
            # level is the trim's fact and the fit's normalization ledger —
            # neither of which an intake may reach past. A prescriber that wants
            # a shelf is asking for a different quantity.
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} must be a Peaking biquad, got "
                f"{entry.get('biquad_type')!r}",
            )
        freq = _finite_number(entry.get("freq"), field=f"filter {position} freq")
        q = _finite_number(entry.get("q"), field=f"filter {position} q")
        gain = _finite_number(entry.get("gain"), field=f"filter {position} gain")
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
                "biquad_type": "Peaking",
                "freq": freq,
                "q": q,
                "gain": gain,
            }
        )
    return tuple(out)


def _check_bounds(
    filters: tuple[dict[str, Any], ...], passbands: DriverPassbands
) -> str:
    """Every per-filter bound, and the class the gains add up to.

    Returns ``"cut"``. It cannot return anything else today — a positive gain is
    refused here rather than classified — and it returns the string rather than
    a constant so the receipt's field has one producer, exactly as the blend
    gate's does.
    """
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
        # BEFORE the depth bounds, so a boost is told it is a boost rather than
        # being told it is too shallow a cut. `_check_bounds` is the first of
        # the two independent gates that keep the parked class unreachable; the
        # route is the second.
        if gain > 0.0:
            _refuse(
                BOOST_ROUTE_UNAVAILABLE,
                f"filter {position} boosts {gain:.2f} dB, and this seam carries "
                "cuts only: a per-driver boost is charged against the graph's "
                "finite headroom budget, so opening the class is a gain-"
                "structure decision rather than something an intake may route "
                "around. A cut only ever removes level and can never clip, "
                "which is why this class opened without one",
                role=role,
                gain_db=gain,
            )
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
    return "cut"


def _check_composed(
    filters: tuple[dict[str, Any], ...], passbands: DriverPassbands
) -> None:
    """The composed cap, per role, on the EVALUATED cascade.

    Through :func:`~jasper.active_speaker.branch_chain.chain_response` — the ONE
    biquad evaluator in this codebase — so this gate and the emitter's own
    accounting cannot disagree about what CamillaDSP will realize.

    Evaluated on a dense log sweep over the ROLE's own band rather than over a
    supplied axis. The blend gate takes the denser of its packet's grid and a
    fallback because its region is narrow and its packet carries a curve across
    it; here the band is the whole driver, no banked axis spans it per-role, and
    a coarse axis can step over a Q-8 filter's peak — which is a safety bound
    reading low because the evidence document was thin.

    **One under-read this grid cannot see, named rather than fixed.** The band
    is Nyquist-clamped by :func:`driver_passbands_from_safety_profile`, so a
    driver DECLARED past 24 kHz is evaluated only up to the clamp: a cascade
    whose composed extreme sits above it is read at the extreme's skirt instead
    of its peak. It is unreachable on any shipped speaker (the clamp binds only
    on a declaration above half the sample rate, and a filter can only be
    PLACED inside the clamped band anyway, so reaching it needs a Q low enough
    for the skirt to still dominate above 24 kHz — where nothing radiates). Left
    as a disclosure rather than a guard: the honest repair is a wider evaluation
    axis than the placement band, and that is a change to what "the composed cap
    is measured over" means, which wants its own reasoning rather than a line
    smuggled in here.
    """
    for role, band in sorted(passbands.items()):
        role_filters = [
            {key: value for key, value in entry.items() if key != "role"}
            for entry in filters
            if entry["role"] == role
        ]
        if not role_filters:
            continue
        lo, hi = band
        grid = np.geomspace(lo, hi, 2048)
        composed = 20.0 * np.log10(
            np.maximum(np.abs(np.asarray(chain_response(role_filters, grid))), 1e-12)
        )
        worst_cut = float(np.min(composed))
        if worst_cut < -DRIVER_MAX_COMPOSED_CUT_DB:
            _refuse(
                COMPOSED_CUT_EXCEEDED,
                f"the {role}'s composed cascade cuts {-worst_cut:.2f} dB at its "
                f"worst over its own band, past the "
                f"{DRIVER_MAX_COMPOSED_CUT_DB:g} dB ceiling",
                role=role,
                composed_cut_db=worst_cut,
                max_composed_cut_db=DRIVER_MAX_COMPOSED_CUT_DB,
            )


def _check_classification(
    filters: tuple[dict[str, Any], ...],
    verdicts: Sequence[FeatureVerdict] | None,
) -> tuple[ClassificationBasis, ...]:
    """Stage P3 rule 1, applied per filter: EQ only classified defects.

    Refuses with :data:`FEATURE_NOT_CLASSIFIED` when nothing was classified at
    the frequency — an instruction to go and measure, not a verdict on the
    proposal — and with :data:`FEATURE_NOT_CUTTABLE` when something was and it
    does not admit a cut.

    **What clearing this bar does and does not mean.** It means the feature is
    not one a cut is structurally the wrong tool for. It does NOT mean a cut
    will help: run-log §9.2 says so in as many words, and every EQ arm played
    that night measured worse against the frozen reference. The round that
    follows is what answers the second question, and it answers it by measuring.
    """
    if not filters:
        # A prescription that cuts nothing needs no evidence that its cuts are
        # aimed at defects. Ordering, not leniency: refusing an empty document
        # for want of a classification would report a missing-evidence problem
        # to an author who did not ask to correct anything, and the bar's whole
        # content is per-filter.
        return ()
    if verdicts is None:
        _refuse(
            FEATURE_NOT_CLASSIFIED,
            "this packet carries no banked feature classification, so no cut "
            "can be shown to be aimed at a minimum-phase driver defect rather "
            "than at an interference null or a room arrival. Bank a "
            "classification for this round and propose again.",
            match_tolerance_octaves=VERDICT_MATCH_TOLERANCE_OCTAVES,
        )
    basis: list[ClassificationBasis] = []
    for position, entry in enumerate(filters):
        freq = float(entry["freq"])
        role = str(entry["role"])
        vouching, nearest = defect_cuttable_at(verdicts, freq)
        if vouching is not None:
            basis.append(
                ClassificationBasis(filter_freq_hz=freq, role=role, verdict=vouching)
            )
            continue
        if nearest is None:
            _refuse(
                FEATURE_NOT_CLASSIFIED,
                f"filter {position} is aimed at {freq:.1f} Hz and no banked "
                "verdict covers that frequency (within "
                f"{VERDICT_MATCH_TOLERANCE_OCTAVES:.3g} octaves). Classify the "
                "feature and propose again — an unclassified feature may be a "
                "cancellation, which a cut makes worse rather than better.",
                role=role,
                freq_hz=freq,
                match_tolerance_octaves=VERDICT_MATCH_TOLERANCE_OCTAVES,
            )
        # The NEAREST verdict is the one quoted, because it is the one that
        # decided — see `defect_cuttable_at`. A cuttable peak sitting further
        # away inside the tolerance neither vouches nor appears here, which is
        # what stops a receipt citing a feature the filter is not on.
        why = (
            "cutting a minimum-phase DIP deepens it — aim a cut at a peak"
            if nearest.classification == DEFECT_BOOSTABLE
            else (
                "only a minimum-phase, speaker-own peak may be cut: a "
                "cancellation is lowered along with the direct sound, and a "
                "room arrival is not the speaker's to correct"
            )
        )
        _refuse(
            FEATURE_NOT_CUTTABLE,
            f"filter {position} is aimed at {freq:.1f} Hz, and the nearest "
            f"banked verdict ({nearest.freq_hz:.1f} Hz) is "
            f"{nearest.classification!r} "
            f"(excess group delay {nearest.egd_verdict or 'unreported'}, gate "
            f"{nearest.gate_verdict or 'unreported'}). {why}",
            role=role,
            freq_hz=freq,
            **nearest.to_dict(),
        )
    return tuple(basis)


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
) -> DriverPrescription | None:
    """THE request gate. One point, and the one place every bound is applied.

    ``None`` when there is no prescription — the deterministic path, untouched.
    Otherwise a validated :class:`DriverPrescription`, or
    :class:`~.blend_prescription.BlendPrescriptionRefused` naming which gate
    said no.

    The three keyword arguments are the evidence packet's own answers, read out
    of it by :mod:`.evidence_packet`'s named readers. Taking VALUES rather than
    the packet is what keeps this module a leaf of the DAG — the packet imports
    the response format from here — and it is exactly the shape
    :func:`~.blend_prescription.read_blend_prescription` already has.

    **All three are required and undefaulted**, on that function's rule: every
    other bound in this gate rests on a number the prescriber itself supplied,
    and these three are the only inputs a prescriber willing to lie cannot
    forge. A caller that forgot one would lose the evidence's own opinion and
    never know, which is precisely what a defaulted keyword hides.

    **Order is deliberate.** Shape, then identity, then the bands, then the
    per-filter bounds, then the composed cascade, then the classification bar,
    and last the route. Each stage sends a prescriber somewhere different, and
    reporting a later failure for an earlier cause would send it to re-derive a
    number that was fine. The classification bar sits AFTER the shape bounds on
    purpose: a filter that is out of band or too deep is wrong whatever the
    feature there turns out to be, and classifying is the expensive errand.

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
    _check_composed(filters, passbands)
    basis = _check_classification(filters, classifications)

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
        rationale=rationale,
    )
    driver_prescription_route(prescription)
    return prescription


def driver_prescription_route(prescription: DriverPrescription) -> str:
    """Which candidate field this prescription lands in, or a refusal.

    **A cut routes.** :data:`LINEARIZATION_CANDIDATE_FIELD` is the role-keyed
    field the Layer-1a fit already writes, so a prescribed per-driver cut is
    byte-shaped like a fitted one and passes the same emitter gates —
    ``camilla_yaml._validated_linearization`` re-validates every entry
    independently before any of it reaches CamillaDSP, and lands them in that
    role's own branch immediately after its crossover filters.

    **A boost does not route, and the reason is a decision rather than an
    oversight.** The seam itself can carry one:
    ``MeasuredCrossoverCandidate.linearization`` already accepts per-driver
    boosts to 12 dB, absorbed correctly by ``camilla_yaml.
    linearization_headroom_db``. What has not been decided is whether an OUTSIDE
    reader may spend that budget. Every dB of boost becomes a dB of pre-split
    attenuation in ``active_baseline_headroom``, so a prescribed boost trades
    the household's maximum SPL for a correction nobody has measured yet — and
    the fit engine's own permission to do that rests on the closed-loop delta
    probe, which grades what the FIT predicted against what the speaker did. A
    prescription has no such prediction to be graded against. That is the
    owner's call to make, not an intake's to route around.

    So the refusal is honest rather than conservative, and it is the SECOND of
    two gates rather than the only one: :func:`_check_bounds` has already
    refused a positive gain by the time a prescription can exist. This one makes
    the promise a property of the FUNCTION, so a :class:`DriverPrescription`
    built directly or read back by :func:`driver_prescription_from_mapping` —
    neither of which routes — cannot reach a candidate field either.
    """
    if prescription.prescription_class != "cut" or any(
        float(entry["gain"]) > 0.0 for entry in prescription.filters
    ):
        _refuse(
            BOOST_ROUTE_UNAVAILABLE,
            "a per-driver boost is charged against the graph's finite headroom "
            "budget and has no prediction the delta probe could grade, so no "
            "route carries one: opening the class is a gain-structure decision",
            blocked_by=["per_driver_boost_spends_headroom_budget"],
        )
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

    ``{}`` for a ``None`` prescription, whatever ``fitted`` holds: with no
    document there is nothing to merge, and the fit reaches the candidate by its
    own ordinary path. That keeps the no-prescription path byte-identical to
    today's.

    **It re-asks the route rather than trusting that the gate already did**, on
    the blend seam's rule: this function is the last thing between a
    prescription and a fingerprinted candidate field, and asking the one owner
    of the rule again costs a function call.
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
        merged[role] = {
            "filters": [dict(f) for f in prescription.filters_for(role)],
            "prescribed_by": {
                "model": prescription.prescriber_model,
                "operator": prescription.prescriber_operator,
                PACKET_FINGERPRINT_FIELD: prescription.packet_fingerprint,
            },
        }
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
                "0 or more objects, each exactly {role: <driver role>, "
                "biquad_type: 'Peaking', freq: <Hz>, q: <number>, "
                "gain: <negative dB>}"
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
            "biquad_type": "Peaking",
            "q_min": DRIVER_MIN_Q,
            "q_max": DRIVER_MAX_CUT_Q,
            "min_cut_db": DRIVER_MIN_CUT_DB,
            "max_filter_cut_db": DRIVER_MAX_FILTER_CUT_DB,
            "max_composed_cut_db": DRIVER_MAX_COMPOSED_CUT_DB,
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
                "measured Q. A filter wider than its target spends most of its "
                "action on the skirts, which the 2026-08-19 round measured as "
                "damage there"
            ),
        },
        "cuts_only": {
            "note": (
                "gain must be negative. A cut only ever removes level, so it "
                "cannot clip however narrow or deep it is. A per-driver BOOST "
                "is a different class: every dB of it is charged against the "
                "graph's finite headroom budget, so opening it is a "
                "gain-structure decision and no route carries one today"
            ),
            "refusal": BOOST_ROUTE_UNAVAILABLE,
        },
        "classification_bar": {
            "note": (
                "every cut must be aimed at a feature the packet's "
                "classification block typed as a minimum-phase, speaker-own "
                "PEAK. Interference nulls, room arrivals and unresolved "
                "features are barred: a filter aimed at a cancellation lowers "
                "the direct sound and the delayed copy together, and a room "
                "arrival is not the speaker's to correct"
            ),
            "eligible_classification": DEFECT_CUTTABLE,
            "dips_are_barred_too": (
                f"{DEFECT_BOOSTABLE!r} is a minimum-phase DIP. It is a "
                "speaker-own defect and it is still not cuttable: cutting a dip "
                "deepens it. Aim a cut at a PEAK. Classified features can sit "
                "closer together than the match tolerance below — on the "
                "2026-08-19 record two peak/dip pairs are 0.14 and 0.16 octaves "
                "apart — so the NEAREST verdict to your centre frequency is the "
                "one that decides, and a cuttable peak nearby cannot vouch for a "
                "filter sitting on a dip"
            ),
            "match_tolerance_octaves": VERDICT_MATCH_TOLERANCE_OCTAVES,
            "necessary_not_sufficient": (
                "a defect verdict says EQ is not structurally BARRED. It does "
                "not say EQ will help — every EQ arm played on 2026-08-19 "
                "measured worse against the frozen reference. The round that "
                "follows is what answers that, by measuring"
            ),
            "if_evidence_is_missing": (
                f"refused as '{FEATURE_NOT_CLASSIFIED}' rather than as a no — "
                "classify the feature and propose again"
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
                "candidate: the same admission, safety-envelope, headroom, "
                "variance and verify gates every automatic round faces, and "
                "the round's own adoption verdict decides keep or restore"
            ),
        },
    }
