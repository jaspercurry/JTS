# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE driver's full-band shape correction, prescribed from outside.

The per-driver intake for the prescriber loop; :mod:`.blend_prescription`
owns the sibling door onto the SUMMED region — same lifecycle, exception
type, packet-fingerprint anchoring, spool. Every admission bar is SHAPE: the
classification vouch DISCLOSES, never refuses (#2863). The band is the
DRIVER's declared band (:func:`driver_passbands_from_safety_profile`), not
the crossover region — a cut past the handoff still spends no headroom (#2523).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

import numpy as np

# Leaf of the crossover_v2 DAG. Facts about the graph — the ONE biquad
# evaluator, the emitter's filter vocabulary, the shelf steepness it actually
# spells, the trim floor, the declared protection edges — are CONSUMED here,
# the opposite of the lockstep rule the policy bounds below follow: bounds
# with a source are RESTATED, not imported;
# tests/test_crossover_v2_driver_prescription.py pins each pair.
from jasper.active_speaker.branch_chain import (
    CHAIN_GRID_HZ,
    HEADROOM_MARGIN_DB,
    _evaluation_grid,
    chain_response,
)
from jasper.active_speaker.camilla_yaml import (
    LINEARIZATION_BIQUAD_TYPES,
    linearization_slot,
)
# The emitter drops a shelf entry's own ``q`` and spells this instead, so a
# gate evaluating a prescriber's number would read a filter that never plays.
from jasper.camilla_config_contract import SHELF_Q
from jasper.active_speaker.driver_protection import (
    declared_protection_highpass_floor_hz,
    declared_protection_lowpass_ceiling_hz,
)
# Three modules must agree on this key name or a hearing-relevant ceiling goes
# quiet.
from jasper.active_speaker.linearization_fit import MIC_TIER_FIELD
from jasper.active_speaker.level_trim import MAX_ATTENUATION_DB

from jasper.sound.profile import (
    EVALUABLE_Q_MAX,
    EVALUABLE_Q_MIN,
    RESPONSE_SAMPLE_RATE_HZ,
)

from .blend_prescription import (
    PACKET_FINGERPRINT_FIELD,
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

#: The role-keyed bands a proposal is checked against.
DriverPassbands = Mapping[str, tuple[float, float]]

__all__ = [
    "DECLARED_TILT_BOUND_DB_PER_OCTAVE",
    "DECLARED_TILT_FIELD",
    "DRIVER_MAX_BOOST_Q",
    "DRIVER_MAX_COMPOSED_BOOST_DB",
    "DRIVER_MAX_FILTERS_PER_ROLE",
    "DRIVER_MAX_FILTER_BOOST_DB",
    "DRIVER_MIN_BOOST_DB",
    "DRIVER_MIN_CUT_DB",
    "DRIVER_PRESCRIPTION_KIND",
    "DRIVER_PRESCRIPTION_MAX_BYTES",
    "DRIVER_PRESCRIPTION_REFUSAL_REASONS",
    "DRIVER_PRESCRIPTION_TOO_LARGE",
    "DRIVER_PRESCRIPTION_SCHEMA_VERSION",
    "EXPECTED_DELTA_BOUND_DB",
    "EXPECTED_DELTA_FIELD",
    "LINEARIZATION_CANDIDATE_FIELD",
    "MAX_SPL_SPEND_BOUND_DB",
    "ClassificationBasis",
    "check_driver_document_size",
    "DriverPassbands",
    "DriverPrescription",
    "driver_max_q_for_gain",
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

#: This class's own version, not a bump of the blend class's: a NEW kind is a
#: new contract, and versioning them together would make every change to either
#: invalidate the other's in-flight documents.
DRIVER_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator. Its distinctness from the blend class's is what
#: makes the shared spool safe: an older reader reaches this string, does not
#: recognise it, and refuses rather than parsing this as a blend document.
DRIVER_PRESCRIPTION_KIND = "jts_crossover_driver_prescription"

#: The byte ceiling on ONE per-driver document, applied once its class is known
#: — a CONTENT bound, unlike the family's
#: :data:`~.blend_prescription.PRESCRIPTION_MAX_BYTES` (64 KiB), which must run
#: before the document names its kind and so cannot belong to a class
#: vocabulary.
#:
#: 32 KiB is five times the largest legitimate document: 8 filters × 4 roles
#: plus a full provenance block and a 1,200-character rationale measures
#: 6,046 bytes pretty-printed at indent 2. The test re-derives that from the
#: schema's own constants rather than trusting this comment.
DRIVER_PRESCRIPTION_MAX_BYTES = 32 * 1024

#: How much of the free-text rationale is BANKED, in characters. It TRUNCATES
#: and discloses rather than refusing: no branch here or downstream reads the
#: text, so a refusal would cost a round to re-author prose no gate consults.
#: The loss is counted onto :attr:`DriverPrescription.rationale_dropped_chars`,
#: which keeps :data:`DRIVER_PRESCRIPTION_MAX_BYTES`'s measured largest
#: document true.
RATIONALE_MAX_CHARS = 1_200

#: The candidate field a per-driver prescription lands in: the role-keyed
#: ``MeasuredCrossoverCandidate.linearization`` the Layer-1a fit already
#: writes, re-validated by ``camilla_yaml._validated_linearization`` before any
#: of it reaches CamillaDSP.
LINEARIZATION_CANDIDATE_FIELD = "linearization"

#: The pre-registration pair, by key name. Held here rather than spelled at
#: each reader because the door writes them, the candidate carries them and
#: ``round_views`` echoes them, and three literals is how the three drift.
EXPECTED_DELTA_FIELD = "expected_delta_db"
DECLARED_TILT_FIELD = "declared_tilt_db_per_octave"

#: Widest ``expected_delta_db`` the door admits, dB. Not a bar on optimism but
#: a UNIT check — the metric predicted is a pooled RMS deviation, so a number
#: wider than the whole magnitude span a graded curve occupies is a percentage
#: or a frequency in the wrong slot.
EXPECTED_DELTA_BOUND_DB = 30.0

#: Widest ``declared_tilt_db_per_octave`` the door admits, dB/octave — the
#: bound above read as a slope over the ~10 graded octaves. A voicing tilt is a
#: small fraction of it (methodology §8).
DECLARED_TILT_BOUND_DB_PER_OCTAVE = 3.0


# --------------------------------------------------------------------------- #
# bounds — every one restored from the engine that already emits into this seam
# --------------------------------------------------------------------------- #

#: Widest Q one prescribed per-driver BOOST may use — ``linearization_fit.
#: _PEAKING_Q_MAX``, the fit engine's PEAKING ceiling. A boost keeps a width
#: ceiling because the caps that bound its maximum-SPL spend are read on a
#: SAMPLED grid, and no fixed resolution bounds an arbitrary Q; this is the
#: width at which the composed reading stays the upper bound
#: :data:`MAX_SPL_SPEND_BOUND_DB`'s proof needs. A CUT carries no policy
#: ceiling at all (ADR-0207) — only the instrument-fidelity one and the eight
#: slots :data:`DRIVER_MAX_FILTERS_PER_ROLE` allows.
DRIVER_MAX_BOOST_Q = 8.0

def driver_max_q_for_gain(gain_db: float) -> float:
    """The widest Q one prescribed filter may use, by the SIGN of its gain.

    A boost gets :data:`DRIVER_MAX_BOOST_Q`, a POLICY ceiling; everything else
    — ``0.0`` included — gets :data:`~jasper.sound.profile.EVALUABLE_Q_MAX`, an
    INSTRUMENT-fidelity one (past it the f64 biquad cascade stops evaluating
    the filter asked for: measured +6.99 dB realized from a requested Q 8e14 on
    an admitted -3.0 dB cut). Same shape as ``blend_prescription.
    max_q_for_gain`` and deliberately not a call to it — that class's boost arm
    is 2.0. Same predicate :func:`_check_bounds` derives
    :attr:`DriverPrescription.prescription_class` from, so a filter cannot be a
    cut for the receipt and a boost for its Q bound.
    """
    return DRIVER_MAX_BOOST_Q if gain_db > 0.0 else EVALUABLE_Q_MAX


#: The depth below which the FIT ENGINE calls a cut cosmetic —
#: ``linearization_fit._MIN_FILTER_GAIN_DB``: "inaudible, wastes a filter slot".
#: A DISCLOSURE here, not a bound: a filter under it spends no maximum SPL and
#: cannot clip, and what a refusal was really guarding is the SLOT, which
#: :data:`DRIVER_MAX_FILTERS_PER_ROLE` guards directly. The count rides the
#: receipt as :attr:`DriverPrescription.subaudible_filters`.
DRIVER_MIN_CUT_DB = 0.5

#: Highest ONE prescribed boost may go, dB — ``camilla_yaml.
#: MAX_LINEARIZATION_BOOST_DB`` / ``linearization_fit.PER_FILTER_BOOST_CAP_DB``,
#: the rail the fit engine emits up to, restated on this module's lockstep rule.
#: A prescription at this ceiling is therefore emittable rather than accepted
#: here and refused downstream. Equal to
#: :data:`DRIVER_MAX_COMPOSED_BOOST_DB` (ADR-0207), so two boost filters both
#: at this rail can never clear the composed cap — each alone reads 12.0 there
#: and skirt overlap only adds (two Q-8 boosts a third of an octave apart still
#: compose to 12.7802). The composed cap binds every multi-filter boost; this
#: one binds the single-filter case.
DRIVER_MAX_FILTER_BOOST_DB = 12.0

#: Ceiling on the COMPOSED boost's peak over one role's passband, dB. POLICY
#: (ADR-0207) — the one bound here restored from no neighbour, since the fit
#: engine leaves total boost deliberately unbounded.
#:
#: It sizes the whole class's cost, and carries that weight ONLY because
#: :func:`_composed_grid` reads the cascade on the same span the charge is
#: taken over. That span clause is load-bearing: a band-limited reading made
#: this sentence false once while every other word of it stayed true. Given it,
#: the remaining terms in the emitted branch (crossover sections, per-driver
#: trim) are non-positive everywhere, so a role's evaluated chain peak cannot
#: exceed this and ``branch_chain.headroom_charge_db`` cannot charge more than
#: :data:`MAX_SPL_SPEND_BOUND_DB`. Per ROLE: the emitter folds roles by worst
#: branch, not by sum, so a document's total spend is this bound and not a
#: multiple of it.
DRIVER_MAX_COMPOSED_BOOST_DB = 12.0

#: The magnitude below which the fit engine calls a boost cosmetic, dB. DEFINED
#: by :data:`DRIVER_MIN_CUT_DB` rather than restated beside it, because
#: "inaudible, wastes a filter slot" does not depend on the sign; it carries
#: its own name only so the contract a prescriber reads names a floor per sign.
DRIVER_MIN_BOOST_DB = DRIVER_MIN_CUT_DB

#: The most maximum SPL one accepted document can cost the household, dB.
#: DERIVED, not chosen, in four steps:
#:
#: 1. ``branch_chain.headroom_charge_db(peak) = peak + HEADROOM_MARGIN_DB`` for
#:    any peak above ``_PEAK_EPS_DB`` (0.01 dB) — the whole charge formula.
#: 2. :func:`_check_composed` refuses any role whose evaluated cascade peak
#:    exceeds :data:`DRIVER_MAX_COMPOSED_BOOST_DB` by more than
#:    :data:`_COMPOSED_BOOST_EVAL_TOL_DB`, so an accepted document reads
#:    ``peak <= 12.0 + 1e-9``.
#: 3. THE SPAN CLAUSE, which the whole proof rests on: that reading is taken on
#:    :func:`_composed_grid`, which is ``branch_chain._evaluation_grid``
#:    IMPORTED (the charge's own span) unioned with a dense sweep of the role's
#:    band, so gate and charge read the same domain. Without it the inference
#:    is unsound, not merely loose — a band-limited gate once passed a cascade
#:    at 3.58 dB that the emitter charged 10.75 dB for.
#: 4. The emitter's peak cannot exceed the gate's, for two reasons both needed:
#:    the remaining terms in the emitted branch (crossover sections, per-driver
#:    trim) are non-positive to within 1e-8 dB (worst measured +1.1654e-09 dB,
#:    an LR8 section near 20 Hz — floating-point residue from cascading eight
#:    biquads); and the emitter evaluates on a strict SUBSET of
#:    :func:`_composed_grid`, so its maximum cannot exceed the superset's.
#:
#: Therefore ``charge <= 12.0 + 1.0 = 13.0`` at published precision (carrying
#: both tolerances, 13.000000011 dB). The bound is ATTAINED, not approached:
#: one filter at :data:`DRIVER_MAX_FILTER_BOOST_DB` at any Q composes to
#: exactly 12.000000 here, so its charge is exactly 13.000000.
#:
#: Imported rather than restated because it is a CONSEQUENCE of the charge
#: formula, not a policy this gate re-validates: a margin that moved must move
#: this too.
#:
#: **What this bounds is the CHARGE, not the realized peak.** Above ~18 kHz the
#: sampling residue exceeds ``HEADROOM_MARGIN_DB`` (#2850, open), so up there
#: the -1.0 dB per-driver soft-clip limiters are the backstop rather than this
#: arithmetic.
MAX_SPL_SPEND_BOUND_DB = DRIVER_MAX_COMPOSED_BOOST_DB + HEADROOM_MARGIN_DB

#: Slack on the COMPOSED BOOST comparison alone, dB, so the evaluator's own
#: double-precision residue cannot decide a policy question. Needed only
#: because ruling R8 (ADR-0207) made :data:`DRIVER_MAX_FILTER_BOOST_DB` and
#: :data:`DRIVER_MAX_COMPOSED_BOOST_DB` the SAME number: a single filter at the
#: rail composes to it exactly in arithmetic, and the biquad evaluates it to a
#: residue whose SIGN depends on centre frequency and Q. Swept over 4,000
#: random (freq, Q) draws at the rail, the worst |residue| is 2.416e-13 dB, so
#: 1e-9 sits ~4 orders above it and ~7 below the 4-decimal precision every
#: charge is published at — it cannot absorb a real cascade. Boost side only;
#: the cut side has no composed bound to collide with (ADR-0207).
_COMPOSED_BOOST_EVAL_TOL_DB = 1e-9

#: How many filters one role may carry — ``linearization_fit.
#: MAX_FILTERS_PER_DRIVER``, which is also the emitter's own
#: ``camilla_yaml.MAX_LINEARIZATION_FILTERS_PER_DRIVER``, so a prescription
#: past it cannot be accepted here and refused at emission.
DRIVER_MAX_FILTERS_PER_ROLE = 8

#: The highest frequency the gate's biquad evaluator is defined for: half of
#: :data:`~jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`, imported rather than
#: restated. It binds because a driver's DECLARED band is a datasheet fact and
#: the evaluator's is an arithmetic one — a supertweeter published to 40 kHz is
#: honest, and a bound evaluated past Nyquist is aliased rather than
#: conservative. The declared upper edge is CLAMPED, never dropped, and the
#: packet publishes the clamped value so a prescriber is shown the band it is
#: judged against.
_EVALUABLE_MAX_HZ = RESPONSE_SAMPLE_RATE_HZ / 2.0


# --------------------------------------------------------------------------- #
# the refusal vocabulary — its own closed set, by slug, never by prose
# --------------------------------------------------------------------------- #

# A SECOND set beside the blend gate's: `filter_outside_region` and
# `filter_outside_passband` name different bands with different owners, so a
# prescriber told the first when the second applied would re-read the crossover
# region while its filter was fine. The exception TYPE is shared.

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
FILTER_BOOST_TOO_HIGH = "driver_filter_boost_too_high"
COMPOSED_BOOST_EXCEEDED = "driver_composed_boost_exceeded"
TRIM_PIN_MALFORMED = "driver_trim_pin_malformed"
DRIVER_EXPECTATION_MALFORMED = "driver_expectation_malformed"

# A refusal this door can no longer give is DELETED from this set rather than
# left registered-but-unreachable, because a prescriber reads it as
# `refusal_reasons`.

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
    FILTER_BOOST_TOO_HIGH,
    COMPOSED_BOOST_EXCEEDED,
    TRIM_PIN_MALFORMED,
    DRIVER_EXPECTATION_MALFORMED,
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
    "pinned_trim_db",
    EXPECTED_DELTA_FIELD,
    DECLARED_TILT_FIELD,
    "rationale",
    # Written BY the gate, accepted on the way back in so a durable block
    # round-trips through this parser. A request that supplies them is
    # harmless: the gate re-derives every one of them.
    "prescription_class",
    "passbands_hz",
    "classification_basis",
    "unvouched_filters",
    "subaudible_filters",
    "rationale_dropped_chars",
    "boosts_in_crossover_overlap",
    "composed_boost_db",
    "composed_boost_role",
    "max_spl_spend_bound_db",
    "displaced_filters",
    "displaced_boost_db",
    "displaced_boost_role",
    # FORWARD-compatible, not backward: an OLDER build handed a NEWER receipt
    # refuses it as an unknown field. An allowlist rather than a required list,
    # so an OLDER receipt read by a NEWER build takes each missing field's
    # dataclass default — `unvouched_filters=None`, the honest "nobody computed
    # this" rather than a substituted zero. A stored shape this contract can no
    # longer parse REFUSES: no tolerant reader, no legacy field list.
})

#: Fields ONE filter may carry. ``role`` is the addition that makes this class
#: what it is — and note that ``role_attenuations_db`` stays PROHIBITED. A
#: prescriber may PIN one role's trim, through :data:`_PRESCRIPTION_FIELDS`'
#: ``pinned_trim_db`` and nowhere else; what it may not do is write the solver's
#: own output field, or reach a level through a FILTER.
_FILTER_FIELDS = frozenset({"role", "biquad_type", "freq", "q", "gain"})


@dataclass(frozen=True)
class ClassificationBasis:
    """Why one prescribed filter was allowed to be aimed where it was.

    Banked so a receipt can name the verdict that admitted the filter, not
    merely that some verdict did.
    """

    #: The FILTER's own centre; the feature it matched carries its own in the
    #: verdict's ``hz``. They differ by up to
    #: :data:`~.feature_classification.VERDICT_MATCH_TOLERANCE_OCTAVES`, so both
    #: are stated.
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

    ``filters`` is a TOTAL for every role it names, not a delta. A role the
    document does not name is not mentioned and is not changed.

    Every ``int | None`` / ``float | None`` disclosure below separates "nobody
    computed this" (``None``) from "computed, and the answer is none" (``0``);
    a receipt that spelled them the same would claim a measurement it never
    made. The durable read-back reports ``None`` for all of them. The
    ``displaced_*`` three also report it after a full request-gate run whenever
    the evidence carried no incumbent — which is every take from the spool.
    """

    #: The prescribed biquads, in emission order, each naming its own role.
    filters: tuple[dict[str, Any], ...]
    #: ``"cut"`` or ``"boost"``, derived from the gains and never read from the
    #: document.
    prescription_class: str
    #: The packet fingerprint this answered.
    packet_fingerprint: str
    #: Who authored it. Both required.
    prescriber_model: str
    prescriber_operator: str
    #: The per-role bands the proposal was checked against, as
    #: ``((role, lo_hz, hi_hz), ...)`` sorted by role — a tuple rather than a
    #: mapping so the record stays hashable and ordered.
    passbands_hz: tuple[tuple[str, float, float], ...]
    #: The banked verdict that VOUCHES for each vouched filter, in filter
    #: order. Shorter than ``filters`` when some are unvouched.
    classification_basis: tuple[ClassificationBasis, ...] = ()
    #: How many filters no banked verdict vouches for — a disclosure, never a
    #: refusal. Carried rather than derived, because an empty
    #: ``classification_basis`` cannot tell ``None`` from ``0``.
    unvouched_filters: int | None = None
    #: How many filters sit below the fit engine's audibility floor
    #: (:data:`DRIVER_MIN_CUT_DB` / :data:`DRIVER_MIN_BOOST_DB`, magnitude,
    #: both signs, a zero-gain filter included) — see
    #: :func:`_subaudible_filters`.
    subaudible_filters: int | None = None
    #: How many characters of the submitted rationale were dropped to fit
    #: :data:`RATIONALE_MAX_CHARS`.
    rationale_dropped_chars: int | None = None
    #: How many boosting filters sit inside a crossover overlap — see
    #: :func:`_boosts_in_crossover_overlap`.
    boosts_in_crossover_overlap: int | None = None
    #: The worst per-role composed boost the gate evaluated, dB.
    composed_boost_db: float | None = None
    #: Which role carried it. The emitter folds by worst BRANCH, so the number
    #: alone cannot say where the spend went.
    composed_boost_role: str | None = None
    #: How many incumbent filters the named roles REPLACE — see
    #: :func:`_check_displaced`.
    displaced_filters: int | None = None
    #: The worst amount the prescribed cascade sits ABOVE the incumbent one,
    #: dB, and the role that carried it (first role wins a tie).
    displaced_boost_db: float | None = None
    displaced_boost_role: str | None = None
    #: Roles whose trim this document NAMES, as ``((role, db), ...)`` sorted by
    #: role. Empty is the ordinary case and leaves every trim to the solver.
    #:
    #: A named trim is CARRIED, not re-solved: :func:`~.planning.build_candidate`
    #: folds it over the round's own solved value, so a transplanted filter
    #: chain keeps the ABSOLUTE per-driver level it was shaped against. An
    #: absolute pin does NOT promise to preserve the inter-driver delta — the
    #: round's own common-mode reference still sets the other roles, and where
    #: the two disagree the pinned branch leaves the ceiling under-used, which
    #: is the safe direction and is charged honestly as headroom.
    #:
    #: Non-positive by the gate, the same bound ``MeasuredCrossoverCandidate``
    #: re-proves: the emitted graph refuses a positive per-driver Gain, and a
    #: pin is not a way past it.
    pinned_trim_db: tuple[tuple[str, float], ...] = ()
    #: The prescriber's own words. NEVER parsed for behaviour.
    rationale: str = ""
    #: The PRE-REGISTERED expectation: how far this document predicts
    #: ``jasper-round-views frozen``'s pooled per-role RMS deviation
    #: (``FrozenReferenceResult.frozen``, dB) will move between the round this
    #: is staged for and the round it is graded against — negative is flatter,
    #: that view's own sign. ``None`` is "nothing was pre-registered", never a
    #: predicted zero. Read by no gate: it moves no limit and no grade, and
    #: exists so the next round's receipt can subtract it (doctrine §1).
    expected_delta_db: float | None = None
    #: The voicing tilt the operator DECLARES, dB/octave, negative for a
    #: downward in-room tilt. Declared rather than measured, so that an applied
    #: tilt is not read as a defect on the next round's receipt (methodology
    #: §8). Also read by no gate.
    declared_tilt_db_per_octave: float | None = None

    @property
    def roles(self) -> tuple[str, ...]:
        """Every role this prescription names, in emission order, once each."""
        return tuple(dict.fromkeys(str(entry["role"]) for entry in self.filters))

    def filters_for(self, role: str) -> tuple[dict[str, Any], ...]:
        """This role's filters, in emission order, with ``role`` stripped.

        The emitter's per-branch shape is ``{biquad_type, freq, q, gain}`` and
        ``camilla_yaml._validated_biquad_entry`` refuses the extra field.
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
            "pinned_trim_db": {role: db for role, db in self.pinned_trim_db},
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
            "subaudible_filters": self.subaudible_filters,
            "rationale_dropped_chars": self.rationale_dropped_chars,
            "boosts_in_crossover_overlap": self.boosts_in_crossover_overlap,
            "composed_boost_db": self.composed_boost_db,
            "composed_boost_role": self.composed_boost_role,
            "max_spl_spend_bound_db": MAX_SPL_SPEND_BOUND_DB,
            "displaced_filters": self.displaced_filters,
            "displaced_boost_db": self.displaced_boost_db,
            "displaced_boost_role": self.displaced_boost_role,
            EXPECTED_DELTA_FIELD: self.expected_delta_db,
            DECLARED_TILT_FIELD: self.declared_tilt_db_per_octave,
            "rationale": self.rationale,
        }


# --------------------------------------------------------------------------- #
# the band — read from the driver's OWN declaration
# --------------------------------------------------------------------------- #


def driver_passbands_from_safety_profile(
    profile: Any,
) -> dict[str, tuple[float, float]]:
    """Each role's own declared band, from the confirmed driver-safety profile.

    ``measurement_band_hz`` (the driver's published response range) narrowed by
    the declared protective high-pass and low-pass, then clamped at
    :data:`_EVALUABLE_MAX_HZ`. Neither protection edge is INVENTED where none
    is declared — an undeclared floor leaves the published lower edge standing,
    on ``declared_protection_highpass_floor_hz``'s never-nanny rule. A target
    with no readable band, or whose composed edges cross, is OMITTED rather
    than guessed, and the gate then refuses that role by name.
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

    The second of two: both call sites have already applied a stat-or-length
    bound before parsing, and this is the one that can speak in this class's
    vocabulary. See :data:`DRIVER_PRESCRIPTION_MAX_BYTES`.
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

    ``bool`` is refused because it is an ``int`` and ``gain=True`` would read as
    a +1 dB boost; strings because ``float("1900")`` succeeds; and
    ``OverflowError`` is caught because ``10 ** 400`` is legal JSON, a legal
    Python ``int``, and raises rather than returning infinity.
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

    The shape is what a durable read-back must also re-check; the bounds are
    what only the request boundary applies. The per-role COUNT sits here
    because it needs no evidence to decide.
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
            # The EMITTER's set, consumed: what the graph can be built out of,
            # so a type outside it cannot be accepted here and raise at
            # emission.
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
            # A SHELF carries no steepness of its own: the emitter's shelf
            # `FilterSpec` has no `q` and the evaluator forces this number, so
            # a stated one is REPLACED rather than honoured. Written into the
            # record because `_validated_biquad_entry` requires a positive `q`.
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

    ``camilla_yaml`` accepts a shelf leading a role's chain, or a trailing
    ``Highshelf`` taper after a ``Lowshelf`` lead (#1668), and raises on any
    other placement; ``linearization_slot`` is that classifier, consumed rather
    than restated. Applied PER ROLE in document order, the order the merged
    candidate field carries into the emitter.
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

    Derived from the declared bands and deliberately not the preset's
    ``fc_hz``: the region of interest is where two drivers BOTH radiate.
    """
    items = sorted(passbands.items())
    out: list[tuple[float, float, str, str]] = []
    for index, (role_a, (lo_a, hi_a)) in enumerate(items):
        for role_b, (lo_b, hi_b) in items[index + 1:]:
            lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
            if lo < hi:
                out.append((lo, hi, role_a, role_b))
    return tuple(out)


def _boosts_in_crossover_overlap(
    filters: Sequence[Mapping[str, Any]], passbands: DriverPassbands
) -> int:
    """How many boosting filters sit where two declared bands overlap.

    **It refuses nothing.** Both drivers radiate in the overlap, so a
    per-driver boost there moves the summed response the crossover stage owns —
    worth telling a reader, not worth stopping a round for. Centres only: a
    filter whose SKIRT reaches into the overlap is not counted, and what bounds
    the SPEND is the composed and per-filter caps that run either way.
    """
    overlaps = _crossover_overlaps(passbands)
    if not overlaps:
        return 0
    return sum(
        1
        for entry in filters
        if float(entry["gain"]) > 0.0
        and any(lo <= float(entry["freq"]) <= hi for lo, hi, _a, _b in overlaps)
    )


def _subaudible_filters(filters: Sequence[Mapping[str, Any]]) -> int:
    """How many filters sit under the fit engine's own cosmetic floor.

    **It refuses nothing** (``docs/measurement-loop-doctrine.md`` §4-§5): a
    sub-floor filter spends no maximum SPL and cannot clip, and the scarce
    thing a refusal stood in for is the SLOT, which
    :data:`DRIVER_MAX_FILTERS_PER_ROLE` bounds directly. By MAGNITUDE, so one
    number answers for both signs, counting a zero-gain filter. STRICTLY
    below: a filter AT the floor is not counted.
    """
    return sum(
        1 for entry in filters if abs(float(entry["gain"])) < DRIVER_MIN_CUT_DB
    )


def _check_bounds(
    filters: tuple[dict[str, Any], ...], passbands: DriverPassbands
) -> str:
    """Every per-filter bound, and the class the gains add up to.

    Returns ``"boost"`` when any gain is positive, else ``"cut"`` — the one
    producer of the receipt's class field. A document may mix signs; the class
    names what it is capable of, and each filter is bounded by its OWN sign,
    magnitude ceiling and width. Neither sign has a magnitude FLOOR: a
    sub-threshold filter is counted, not refused, by :func:`_subaudible_filters`.
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
        # The evaluator's own floor: below EVALUABLE_Q_MIN, `_biquad_coeffs`
        # silently clamps eff_q and the emitter spells the filter "q: 0.0000" —
        # not a shape this system can realize, whatever the gain's sign.
        if q < EVALUABLE_Q_MIN:
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} q {q:g} is below {EVALUABLE_Q_MIN:g}: "
                "spelled 'q: 0.0000' by the emitter and clamped by the "
                "evaluator, not a shape this system can realize",
            )
        q_max = driver_max_q_for_gain(gain)
        if q > q_max:
            _refuse(
                FILTER_Q_OUT_OF_RANGE,
                f"filter {position} Q {q:g} is past {q_max:g} for a "
                f"{'boost' if gain > 0.0 else 'cut'}",
                q=q,
                q_max=q_max,
            )
        # 10**(gain/40) is exactly 0.0 below ~-12960 dB, and `_biquad_coeffs`
        # divides by it — an uncaught ZeroDivisionError at evaluation time.
        if 10.0 ** (gain / 40.0) == 0.0:
            _refuse(
                FILTER_MALFORMED,
                f"filter {position} gain {gain:g} dB underflows 64-bit "
                "arithmetic and cannot be evaluated or emitted",
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

    Both halves are needed, each closing one failure.

    DOMAIN. ``camilla_yaml.linearization_headroom_db`` charges the cascade's
    peak over the WHOLE spectrum, and a mixed-sign cascade's extremum can sit
    outside the hull of its own centres — six +3.0 dB Q-0.7 filters at 40 Hz
    with two -12.0 dB Q-2.0 filters at 48 Hz, all inside the woofer's
    40-3000 Hz band, peak at 29.5 Hz and are charged 10.75 dB for a document a
    band-limited reading passed at 3.58. So the span is
    ``branch_chain._evaluation_grid``'s own, IMPORTED rather than mirrored.

    RESOLUTION. That span samples 1/48 octave, coarser inside one driver's band
    than a Q-8 filter needs, so a dense per-band sweep is unioned in. The union
    can only read HIGHER than either half, the only direction a safety bound
    may move.
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
    """The composed BOOST cap, per role, on the EVALUATED cascade.

    Returns ``(worst composed BOOST across the document in dB, the role it
    belongs to)`` — ``(0.0, None)`` when nothing rises above unity. That is the
    number :data:`MAX_SPL_SPEND_BOUND_DB` bounds and the receipt reports; the
    ROLE rides with it because the emitter folds by worst BRANCH.

    Through ``chain_response``, the ONE biquad evaluator here, so this gate and
    the emitter's accounting cannot disagree about what CamillaDSP realizes,
    and on :func:`_composed_grid`, never on a supplied axis.

    Read WITHOUT the crossover sections and WITHOUT the branch trim, both of
    which are non-positive to within 1e-8 dB — the trim by construction
    (``intervention.anchor_trims`` normalizes it), the LR sections by
    measurement (worst +1.1654e-09 dB, floating-point residue at LR8 near a
    20 Hz corner). So this reading is an UPPER bound on the emitter's charge,
    the direction a gate's number must err — an inference that is only sound
    because the span is the charge's own.
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
        # The extremum can and does land outside the declared band (measured as
        # low as 1.92 Hz and as high as 21.5 kHz), so the refusal names the
        # FREQUENCY rather than an interval the number may not be inside.
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
        # `>` not `>=`: the FIRST role reaching the worst value keeps it, so a
        # tie is decided by sorted role order rather than evaluation order.
        # `peak_boost > 0.0` not `>= 0.0`: a document that puts nothing above
        # unity has no role that spent, and naming one would attribute 0.0 dB
        # to whichever role sorted first.
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
    incumbent. ``filters`` is a TOTAL for every role it names, so an incumbent
    filter a naming document does not repeat is deleted — a change
    :func:`_check_composed` cannot see, and one that measured a 6.065 dB tilt
    step on 2026-08-22 (#2863).

    It DISCLOSES and never refuses: dropping an incumbent cut adds nothing to
    the cascade the emitter charges, and a driver's protective corners are not
    in this map at all — they are declared as ``required_protection_filters``
    and enforced by ``graph_safety``, ``path_safety`` and
    ``excitation_safety_plan``, so no document routed here can delete one.

    Measured against the INCUMBENT, which at staging time is what the speaker
    is playing rather than the fit this document will displace: the same
    question asked one round apart. It is also a PER-BRANCH number — removing
    an incumbent BOOST additionally releases the pre-split attenuation it was
    charged, a whole-speaker step this does not report.
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
        # One grid over BOTH cascades, so neither extremum falls between the
        # other's sample points.
        grid = _composed_grid(role_filters + previous, lo, hi)
        delta = 20.0 * np.log10(
            np.maximum(np.abs(np.asarray(chain_response(role_filters, grid))), 1e-12)
            / np.maximum(np.abs(np.asarray(chain_response(previous, grid))), 1e-12)
        )
        peak = max(0.0, float(np.max(delta)))
        # `> 0.0` and first-role-wins, both read off `_check_composed`.
        if peak > 0.0 and (worst_role is None or peak > worst_boost):
            worst_boost, worst_role = peak, role
    return displaced, worst_boost, worst_role


def _check_classification(
    filters: tuple[dict[str, Any], ...],
    verdicts: Sequence[FeatureVerdict] | None,
) -> tuple[tuple[ClassificationBasis, ...], int]:
    """Which filters a banked verdict VOUCHES for, and how many it does not.

    Returns ``(the vouching basis in filter order, the count of filters with no
    vouching verdict)``. **It refuses nothing**
    (``docs/measurement-loop-doctrine.md`` §2-§5): a vouch is a prediction
    about whether a filter will help, and a prediction recommends while the
    measurement decides. Refusing on it cost a role its incumbent shelf,
    because nothing vouches for a filter the FIT ENGINE placed (#2863).

    A verdict vouches when it is the NEAREST banked one to the filter's centre
    and its classification matches the filter's sign. Vouched is not "will
    help" and unvouched is not "will not" — a defect verdict says only that EQ
    is not structurally the wrong tool.
    """
    if verdicts is None:
        # No banked classification at all: nothing is vouched. Distinct from
        # the ``None`` the caller carries for "nobody computed this".
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


def _rationale(raw: Any) -> tuple[str, int]:
    """The prescriber's own words, banked to the ceiling, and what was dropped.

    Returns ``(the banked text, how many characters were dropped)``. It
    TRUNCATES rather than refusing (:data:`RATIONALE_MAX_CHARS`) — a prose
    ceiling cannot be a safety bound because nothing reads the prose — and is
    still strictly TEXT.
    """
    if raw is None:
        return "", 0
    if not isinstance(raw, str):
        _refuse(
            DRIVER_PRESCRIPTION_MALFORMED,
            f"rationale must be text, got {type(raw).__name__}",
        )
    text = " ".join(raw.split())
    return text[:RATIONALE_MAX_CHARS], max(0, len(text) - RATIONALE_MAX_CHARS)


def _pre_registration(raw: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """The two declared numbers, or ``None`` each for "nothing was declared".

    Applied by BOTH doors for :func:`_parse_pinned_trim`'s reason: a banked
    value outside these bounds could never have been produced by the door that
    wrote it. REFUSED rather than dropped — a pre-registration that vanished on
    a typo reads on the next receipt as a round nobody predicted.
    """
    def declared(field: str, bound: float) -> float | None:
        value = raw.get(field)
        if value is None:
            return None
        number = _finite_or_none(value)
        if number is None or abs(number) > bound:
            _refuse(
                DRIVER_EXPECTATION_MALFORMED,
                f"{field} must be a finite number between {-bound:g} and "
                f"{bound:g}, got {value!r}",
                field=field, bound=bound,
            )
        return number

    return (
        declared(EXPECTED_DELTA_FIELD, EXPECTED_DELTA_BOUND_DB),
        declared(DECLARED_TILT_FIELD, DECLARED_TILT_BOUND_DB_PER_OCTAVE),
    )


def _parse_pinned_trim(
    raw: Any, filters: Sequence[Mapping[str, Any]]
) -> tuple[tuple[str, float], ...]:
    """The named trims, judged ONCE — shape, range and scope in one place.

    A pin rides beside the filters rather than replacing the solver: see
    :attr:`DriverPrescription.pinned_trim_db`. This is the only judgment it
    gets, which is why it sits in :func:`_parse_prescription` with the other
    shape checks rather than with the bounds — the durable read-back must
    re-apply it too, and a banked pin outside this range could never have been
    produced by the door that wrote it.

    **Scope: only a role this same document already names.** A pin on a role the
    filters say nothing about is a bare level command, which is exactly what
    ``role_attenuations_db`` stays prohibited for. The pin travels with the chain
    it protects or it does not travel.

    **Range: non-positive, floored at :data:`MAX_ATTENUATION_DB`.** Consumed
    from the solver's own constant so this door and
    ``MeasuredCrossoverCandidate.__post_init__`` cannot admit different depths.
    Refused HERE as well as there so a prescriber gets an actionable answer at
    the door instead of losing the round to a refusal raised mid-build.
    """
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        _refuse(
            TRIM_PIN_MALFORMED,
            f"pinned_trim_db must be an object keyed by driver role, got "
            f"{type(raw).__name__}",
        )
    named = {str(entry["role"]) for entry in filters}
    out: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            _refuse(TRIM_PIN_MALFORMED, "pinned_trim_db keys must name a driver role")
        role = key.strip()
        if role in out:
            # Two keys differing only in whitespace strip to one role; a silent
            # last-wins would ship whichever the dict iterated last.
            _refuse(
                TRIM_PIN_MALFORMED,
                f"pinned_trim_db names role {role!r} more than once",
                role=role,
            )
        if role not in named:
            _refuse(
                TRIM_PIN_MALFORMED,
                f"pinned_trim_db names role {role!r}, which this document "
                "prescribes no filters for: a trim is pinned to protect the "
                "chain beside it, never on its own",
                role=role,
                document_names=sorted(named),
            )
        db = _finite_or_none(value)
        if db is None:
            _refuse(
                TRIM_PIN_MALFORMED,
                f"pinned_trim_db[{role!r}] must be a finite number of dB, got "
                f"{value!r}",
                role=role,
            )
        if db > 0.0 or db < MAX_ATTENUATION_DB:
            _refuse(
                TRIM_PIN_MALFORMED,
                f"pinned_trim_db[{role!r}] must be between {MAX_ATTENUATION_DB} "
                "and 0 dB: a per-driver trim attenuates, and the emitted graph "
                "refuses a positive per-driver gain",
                role=role,
                pinned_trim_db=db,
                floor_db=MAX_ATTENUATION_DB,
            )
        out[role] = db
    return tuple(sorted(out.items()))


def _parse_prescription(
    raw: Mapping[str, Any],
) -> tuple[
    tuple[dict[str, Any], ...], tuple[tuple[str, float], ...], str, str, str, str, int
]:
    """Shape, identity and provenance — and none of the bounds.

    Shared whole between the request gate and the durable read-back, so the only
    thing that differs between those two is their gate policy.
    """
    if not isinstance(raw, Mapping):
        _refuse(
            DRIVER_PRESCRIPTION_MALFORMED,
            f"a prescription must be a mapping, got {type(raw).__name__}",
        )
    # BEFORE the unknown-field check: every prohibited key is also an unknown
    # one, so checking shape first would report a prescriber reaching for
    # `role_attenuations_db` as a typo.
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
    rationale, dropped = _rationale(raw.get("rationale"))
    filters = _parse_filters(raw.get("filters"))
    return (
        filters,
        _parse_pinned_trim(raw.get("pinned_trim_db"), filters),
        fingerprint.strip(),
        model,
        operator,
        rationale,
        dropped,
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

    The four keywords are the evidence packet's own answers, read out by
    :mod:`.evidence_packet`'s named readers. Taking VALUES rather than the
    packet keeps this module a leaf of the DAG. All four are required and
    undefaulted, so a caller cannot lose the evidence's own opinion silently:
    they are the only inputs a prescriber willing to lie cannot forge.
    ``incumbent_filters`` bounds nothing and ``None`` is a legitimate value for
    it, unlike the three above — it buys the one disclosure
    :func:`_check_displaced` makes.

    Order is deliberate — shape, identity, bands, per-filter bounds, composed
    cascade — because each stage sends a prescriber somewhere different. The
    DISCLOSURES run last, so a document the gate refuses never pays for them.
    The bounds are INCLUSIVE.
    """
    if raw is None:
        return None
    (
        filters, pinned_trim_db, fingerprint, model, operator, rationale,
        rationale_dropped,
    ) = _parse_prescription(raw)
    expected_delta_db, declared_tilt = _pre_registration(raw)

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
            f"({packet_fingerprint[:12]}...); the fingerprint depends on "
            "which evidence inputs (--drivers, --applied-profile, --state) "
            "were present when each packet was built, so two honest builds "
            "of the same round can disagree",
            prescription_answers=fingerprint,
            packet_is=packet_fingerprint,
            # Only THIS side's inputs are knowable — the other is a
            # fingerprint, not a build.
            packet_is_evidence_present={
                "drivers": bool(passbands_hz),
                "classification": bool(classifications),
                "incumbent_linearization": incumbent_filters is not None,
            },
        )

    if not passbands_hz:
        _refuse(
            PASSBAND_UNAVAILABLE,
            "this speaker's evidence declares no per-driver band, so there is "
            "nothing a per-driver prescription could be checked against. The "
            "bands come from --drivers <design draft JSON>'s confirmed "
            "driver-safety profile — its own measurement_band_hz and "
            "required_protection_filters",
        )
    passbands = dict(passbands_hz)

    prescription_class = _check_bounds(filters, passbands)
    composed_boost_db, composed_boost_role = _check_composed(filters, passbands)
    basis, unvouched_filters = _check_classification(filters, classifications)
    displaced_filters, displaced_boost_db, displaced_boost_role = _check_displaced(
        filters, incumbent_filters, passbands
    )

    prescription = DriverPrescription(
        filters=filters,
        pinned_trim_db=pinned_trim_db,
        prescription_class=prescription_class,
        packet_fingerprint=fingerprint,
        prescriber_model=model,
        prescriber_operator=operator,
        passbands_hz=tuple(
            (role, lo, hi) for role, (lo, hi) in sorted(passbands.items())
        ),
        classification_basis=basis,
        unvouched_filters=unvouched_filters,
        subaudible_filters=_subaudible_filters(filters),
        rationale_dropped_chars=rationale_dropped,
        boosts_in_crossover_overlap=_boosts_in_crossover_overlap(
            filters, passbands
        ),
        composed_boost_db=composed_boost_db,
        composed_boost_role=composed_boost_role,
        displaced_filters=displaced_filters,
        displaced_boost_db=displaced_boost_db,
        displaced_boost_role=displaced_boost_role,
        rationale=rationale,
        expected_delta_db=expected_delta_db,
        declared_tilt_db_per_octave=declared_tilt,
    )
    driver_prescription_route(prescription)
    return prescription


def driver_prescription_route(prescription: DriverPrescription) -> str:
    """Which candidate field this prescription lands in.

    :data:`LINEARIZATION_CANDIDATE_FIELD` is the role-keyed field the Layer-1a
    fit already writes, so a prescribed per-driver filter is byte-shaped like a
    fitted one and passes the same emitter gates. BOTH signs take it, and it
    carries no condition of its own — the spend a boost costs is bounded by
    :data:`MAX_SPL_SPEND_BOUND_DB`, which :func:`_check_composed` applies at
    the boundary and the emitter re-proves.
    """
    return LINEARIZATION_CANDIDATE_FIELD


def driver_prescription_to_candidate_fields(
    prescription: DriverPrescription | None,
    *,
    fitted: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The candidate fields a validated prescription contributes — MERGED BY ROLE.

    The value must enter at CANDIDATE-BUILD time:
    ``MeasuredCrossoverCandidate.fingerprint`` is ``field(init=False)``, so a
    value stamped on afterwards is refused as ``candidate_tampered``.

    MERGE BY ROLE: a document's named roles replace THOSE roles' filters,
    unnamed roles keep their fitted ones. Replacing wholesale would
    un-linearize a driver the document never mentioned; composing would double
    corrections and could exceed the eight-filter branch ceiling from two
    authors neither of whom sees the total. ``fitted`` is therefore REQUIRED
    and undefaulted — forgetting it costs a driver its linearization,
    invisibly, until somebody measures. Pass the candidate's own
    ``{role: LinearizationFit.to_dict()}``, or ``None`` when no fit ran.

    The per-role value carries ``filters`` and ``prescribed_by`` and nothing
    that would claim to be a FIT: a prescription has no fit band, residual or
    reason to report, and emitting those zeroed would bank a fit-quality claim
    nothing measured. The one exception is :data:`MIC_TIER_FIELD`, carried
    forward from the replaced entry — it names the MICROPHONE that measured the
    round, not this correction, and ``_mic_trust_ceiling_hz`` reads it to
    decide where the delta probe may grade at all (#2649).

    ``headroom_cost_db`` is omitted but NOT owned here: a charge is a property
    of the emitted chain (filters, crossover sections, committed trim) and this
    is a pure function over a document and a fitted map.
    :func:`~.planning.build_candidate` stamps it (#2759), so a caller that
    folds these entries on without charging them discloses 0.0 for a branch
    that genuinely spends maximum SPL.

    ``{}`` for a ``None`` prescription, whatever ``fitted`` holds.
    """
    if prescription is None:
        return {}
    field = driver_prescription_route(prescription)
    merged: dict[str, Any] = {
        str(role): value
        for role, value in (fitted or {}).items()
        if isinstance(role, str) and role.strip()
    }
    pinned_roles = {role for role, _db in prescription.pinned_trim_db}
    # Not ``field``: this function already binds that name to the route key.
    pre_registration = {
        key: value
        for key, value in (
            (EXPECTED_DELTA_FIELD, prescription.expected_delta_db),
            (DECLARED_TILT_FIELD, prescription.declared_tilt_db_per_octave),
        )
        if value is not None
    }
    for role in prescription.roles:
        entry: dict[str, Any] = {
            "filters": [dict(f) for f in prescription.filters_for(role)],
            "prescribed_by": {
                "model": prescription.prescriber_model,
                "operator": prescription.prescriber_operator,
                PACKET_FINGERPRINT_FIELD: prescription.packet_fingerprint,
                # The pre-registration rides the stamp that already says WHO
                # asked, so the round banks what was predicted beside what it
                # measured. Omitted when nothing was declared: a null here
                # would read as a predicted zero.
                **pre_registration,
            },
        }
        # The BIT, never the number: the pinned value is the candidate's own
        # ``role_attenuations_db`` entry once ``build_candidate`` folds it, and
        # two copies is how they come to disagree.
        if role in pinned_roles:
            entry["trim_pinned"] = True
        # Read off the entry being REPLACED, so a role the fit never reached
        # carries no tier and the ceiling's reader is told rather than left to
        # infer.
        previous = merged.get(role)
        if isinstance(previous, Mapping) and previous.get(MIC_TIER_FIELD):
            entry[MIC_TIER_FIELD] = str(previous[MIC_TIER_FIELD])
        merged[role] = entry
    return {field: merged}


def driver_prescription_from_mapping(raw: Any) -> DriverPrescription | None:
    """A prescription read back out of this repository's own durable state.

    Shape and provenance only — the bounds have one owner and it is the
    request gate; re-applying them here could only refuse a round that really
    ran — and ``None`` instead of a raise. It does NOT route and rebuilds no
    classification basis, which is why
    :func:`driver_prescription_to_candidate_fields` asks
    :func:`driver_prescription_route` itself.
    """
    if raw is None:
        return None
    try:
        # The dropped-character count is discarded: this reader holds the
        # already-truncated text and cannot know what was originally written.
        filters, pinned_trim_db, fingerprint, model, operator, rationale, _dropped = (
            _parse_prescription(raw)
        )
        expected_delta_db, declared_tilt = _pre_registration(raw)
    except BlendPrescriptionRefused:
        return None
    bands = _passbands_from_mapping(raw.get("passbands_hz") if isinstance(raw, Mapping) else None)
    if not bands:
        return None
    return DriverPrescription(
        filters=filters,
        pinned_trim_db=pinned_trim_db,
        prescription_class=(
            "boost" if any(float(f["gain"]) > 0.0 for f in filters) else "cut"
        ),
        packet_fingerprint=fingerprint,
        prescriber_model=model,
        prescriber_operator=operator,
        passbands_hz=bands,
        rationale=rationale,
        expected_delta_db=expected_delta_db,
        declared_tilt_db_per_octave=declared_tilt,
    )


def _passbands_from_mapping(raw: Any) -> tuple[tuple[str, float, float], ...]:
    """The banked bands, or ``()`` so the caller reads it as absent.

    No Nyquist bound, unlike the spool's band reader: this one EVALUATES
    nothing. The strict ordering keeps a degenerate band out of the record.
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

    One owner for the instructions a prescriber is given and the gate it is
    judged by, so the two cannot describe different shapes. It is a PURE
    CONSTANT — nothing banked, measured, or household-authored reaches it,
    which is what makes prompt injection through the packet structurally
    impossible. That is also why the per-role BANDS and the banked VERDICTS are
    not here: those are evidence and live in the packet's evidence blocks.
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
            "pinned_trim_db": (
                "{<role>: <dB, between "
                f"{MAX_ATTENUATION_DB} and 0>}} — pin that driver's LEVEL "
                "instead of letting this round re-solve it. Only for a role "
                "you also prescribe filters for. Use it when the chain you are "
                "prescribing was shaped against a level this round will not "
                "re-derive: the trim is re-solved every round from a "
                "level-match datum, so a chain carried over from another round "
                "otherwise rides a level it was not shaped against. A trim you "
                "name is CARRIED, never re-solved, and it is never a "
                "measurement of this round"
            ),
            EXPECTED_DELTA_FIELD: (
                "PRE-REGISTER your prediction: how far this document should "
                "move `jasper-round-views frozen`'s pooled per-role RMS "
                "deviation against the round it is graded against, dB, "
                "negative for flatter, magnitude at most "
                f"{EXPECTED_DELTA_BOUND_DB:g}. That view echoes it beside the "
                "measured move and their difference. It gates nothing; "
                "leaving it out pre-registers nothing"
            ),
            DECLARED_TILT_FIELD: (
                "the voicing tilt you are DECLARING, dB/octave, negative for a "
                "downward in-room tilt, magnitude at most "
                f"{DECLARED_TILT_BOUND_DB_PER_OCTAVE:g}. Declare one whenever "
                "you apply one: an undeclared tilt is indistinguishable from a "
                "defect on the next round's receipt. It gates nothing"
            ),
            "rationale": (
                f"free text. The first {RATIONALE_MAX_CHARS} characters are "
                "banked and anything past them is dropped — a long rationale is "
                "truncated and disclosed as prescription."
                "rationale_dropped_chars, never refused. It is stored for a "
                "human reader and is NEVER parsed for behaviour: no argument "
                "made here can widen a bound below."
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
            "q_max_boost": DRIVER_MAX_BOOST_Q,
            "cuts_are_free": (
                "a cut (gain <= 0) carries no depth ceiling and no composed "
                "ceiling: it only removes level and cannot clip at any "
                "depth, and the round's own measured verify with "
                "auto-restore is the net. Its Q must sit in "
                f"[{EVALUABLE_Q_MIN:g}, {EVALUABLE_Q_MAX:g}] (ADR-0207) — "
                "not a policy ceiling but the range this system's evaluator "
                "and emitter realize faithfully. What a cut spends is one "
                "of max_filters_per_role's slots"
            ),
            "max_filter_boost_db": DRIVER_MAX_FILTER_BOOST_DB,
            "max_composed_boost_db": DRIVER_MAX_COMPOSED_BOOST_DB,
            "subaudible_below_db": DRIVER_MIN_CUT_DB,
            "a_shallower_filter_discloses_and_is_admitted": (
                "there is no magnitude FLOOR on either sign. A filter under "
                "subaudible_below_db is ADMITTED and counted onto "
                "prescription.subaudible_filters, never refused: that number is "
                "the deterministic fit engine's own cosmetic floor, a heuristic "
                "about audibility rather than a measurement of this speaker, so "
                "prescribe a deliberate sub-floor probe if that is the "
                "experiment you want. What it costs is one of "
                "max_filters_per_role's slots, which is the scarce thing"
            ),
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
                "both signs are bounded by the same depth caps and disclosed by "
                "the same classification_bar. What differs is the COST: a cut "
                "spends a filter slot, a boost also spends maximum SPL, up to "
                "max_spl_spend_bound_db — which is also why a boost is the one "
                "sign that keeps a WIDTH ceiling (q_max_boost). Boost no deeper "
                "than the dip the verdict measured (its depth_db is in the "
                "packet) — nothing refuses a deeper one, and nothing makes it "
                "work either"
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
            "at_the_crossover_knee_it_discloses": (
                "a boost sitting where two drivers' declared bands OVERLAP is "
                "COUNTED, not refused: both radiate there, so a per-driver "
                "boost moves the summed response the crossover stage owns. The "
                "count comes back as prescription.boosts_in_crossover_overlap "
                "— weigh it, and correct the handoff if it matters. What the "
                "boost SPENDS is bounded by the caps above either way"
            ),
            "refusals": sorted({
                FILTER_BOOST_TOO_HIGH,
                COMPOSED_BOOST_EXCEEDED,
                FILTER_Q_OUT_OF_RANGE,
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
            # Per SIGN, and a pair rather than one key, so a reader walking the
            # keys cannot conclude a boost has no bar to satisfy.
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
