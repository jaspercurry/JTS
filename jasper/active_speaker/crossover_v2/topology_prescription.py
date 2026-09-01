# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE crossover corner and its order, PINNED for one round.

The third strict sibling of :mod:`.alignment_prescription` and
:mod:`.blend_prescription`: pure functions, no I/O, no session, and the strict
reader for one prescription the host hands the machinery.  Those two pin the
*timing* between the drivers and the *shape* of the blend region; this one pins
the crossover *topology* — where the branches split, and how steeply.

**Why a prescription exists at all.**  The automatic path crosses WHERE the
household declared and never at a shape it chose (#1894), and nothing anywhere
ranks topologies.  So a household that wants to hear one named corner at one
named order — a pre-registered Fc/slope tournament of candidates, graded
against each other — had no door at all.  Every existing knob declares a
corner without pinning one.  This is the door:
the round solves AT the named topology, and its trims, its linearization and
its delay re-solve underneath.

**A constraint on the SOLVE, not a stamp on the answer.**  The same sentence
:mod:`.alignment_prescription` makes about a pinned polarity basin, and it is
load-bearing in the same way.  A round handed a pin does not fit at the
incumbent corner and then relabel the result: it composes the candidate's
crossover at the pinned corner and order, and everything downstream — the §4.2
de-embedding's ``C_c``, the linearization fit's band, the trims, the predicted
sum, the emitted graph and VERIFY's design target — is that topology's.  A
candidate whose graph crosses at 4000 Hz and whose prediction models 1648.7 is
the exact incoherence the series-2 diagnosis found in the automatic path.

**Admissibility, not excursion.**  The delay prescription's bound is a bounded
excursion from a declared measured basis, because a delay HAS a measured basis.
A pinned corner does not: an excursion bound anchored on the incumbent would
refuse a tournament by construction (that is what a tournament is — candidates
far from the incumbent), and there is no measured ranking to anchor on instead.  So
every bound here is an ADMISSIBILITY bound — a declaration the household or the
manufacturer already made about what this hardware may be asked to do — and
each one is asked of the module that already owns it:

* ``order`` must be an order the graph can emit
  (:data:`~jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`);
* ``fc_hz`` must clear the two DECLARED hard-excitation bounds every corner
  clears, asked of the shared predicate that owns them
  (:func:`.fc_sweep._fc_rejection`);
* ``order * 6`` dB/octave must be at least the protected role's PUBLISHED
  slope condition, when the manufacturer printed one.

**The published slope refuses; the commissioning figure discloses.**  Those are
two different numbers and this gate fused them until the 2026-08-23 owner ruling
("Why was LR2 refused?! ... If it was in the safe overall envelope, it's safe to
test").  What it read was
``required_protection_filters[highpass].minimum_slope_db_per_octave``, which is
``max(published, driver_protection.PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE)`` — so a
B&C DE250 whose datasheet says "1.6 kHz — 12 dB/oct. or higher" had an order-2
pin at 2400 Hz refused "below the protected driver's declared minimum of 24
dB/octave", a sentence the manufacturer never wrote.  The 2026-08-22 ruling
quoted in ``driver_safety`` is what governs: declared values are the only
refusing authority; code figures may prefill, disclose, and fall back.  So the
bound is now the PUBLISHED condition
(``excitation_safety_plan.resolve_driver_protection_slope_db_per_octave``), a
driver with no published condition ON THE RECORD gets no slope refusal at all
— which covers both a datasheet that prints none AND a safety profile saved
before the field that carries one existed, so every speaker commissioned before
this change has no slope bound until its next ``/sound/`` save — and the
commissioning recommendation rides the record as
:attr:`TopologyPrescription.recommended_slope_db_per_octave` instead.

**Why the slope bound is here and not downstream.**  Because nothing downstream
enforces a slope ABOVE 12 dB/octave on a crossover — quoting each gate rather
than summarising it, because the summary "no slope term" is wrong about one of
them:

* ``graph_safety.output_highpass_protected`` (and ``unprotected_tweeter_outputs``
  over it) matches an ``LinkwitzRileyHighpass`` with ``freq >= min_corner_hz``
  and reads no ``order`` at all;
* ``graph_safety.tweeter_guard_present`` DOES read one, as ``order`` absent or
  ``>= 2.0`` — its own docstring's loose-tolerance rule.  Every order this
  system can emit satisfies it (:data:`~jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`
  starts at 2), so it refuses a sub-LR2 shape and never an order-2 crossover;
* crossover apply compares corner frequencies only
  (``driver_protection.protection_highpass_floor_satisfied``, the shared floor
  predicate, which has no slope term);
* the derived requirement IS proved by the commissioning admission path, but
  only against the protective filter this build emitted from that same derived
  number — never against a corner a household pinned.

There was a SECOND copy of exactly this refusal, and it was live rather than
dead: ``camilla_yaml._assert_tweeter_crossover_hp_satisfies_floor`` refused the
CROSSOVER high-pass at ``order * 6 < PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE``
(a hardcoded 24) on ``emit_active_speaker_program_config``'s
``protection_sections_by_role is None`` branch — which the VERIFY stage takes,
because ``correction_crossover_v2`` builds ``bind_production_play`` twice and
only the MEASURE one supplies that mapping.  An order-2 pin admitted here was
therefore measured, applied, and refused at VERIFY's emit.  That slope half is
now a WARNING log
(``result=tweeter_hp_slope_below_commissioning_floor``) and its CORNER half
— the one naming a damage mechanism — is still a refusal.

So a published condition that is not checked here is not checked anywhere,
which is why this is the one place it is applied.

**The beaming ceiling is disclosed, never enforced.**  #1675 defines it as
guidance to warn on rather than a fence, so no admissibility bound anywhere
reads it — :func:`.fc_sweep._fc_rejection` carries no beaming term for the
automatic path either.  A pinned corner is the configured corner of its own
round, so enforcing it here would be stricter than that path is about the very
same speaker.  It rides the record instead, so a receipt can say the candidate
was above it.

**No polarity, and that absence is a contract.**  Polarity is
:mod:`.alignment_prescription`'s field, pinned there and translated there into
the measurement frame.  A second door onto the same knob is how two halves of
one decision enter by two paths and disagree; :data:`_PRESCRIPTION_FIELDS`
refuses the word outright rather than ignoring it, so a prescriber that put it
here learns at the tap.

**Fail-closed, never clamped, never inherited — one rule, three edges.**  The
operator asked for a NAMED candidate, so a silently different candidate is worse
than none: its receipt would carry the name of one that did not run.  That single
sentence is the reason for all three of these, and they are stated together so
it is not restated three times.  An inadmissible pin raises rather than being
pulled to the nearest legal corner (every refusal below names its reason).  An
unfittable one refuses rather than degrading.  And ``None`` from
:func:`read_topology_prescription` is the automatic path with every byte of the
ordinary selection unchanged — never a value inherited from a lapsed session's
durable state the way the session tier deliberately is (#2639), because a
"measure again" that re-ran a candidate nobody asked for would put that
candidate's name on a round at a corner this speaker is not commissioned for.

**One parser, two gate policies.**  :func:`read_topology_prescription` is the
REQUEST gate — shape, provenance, and every bound — and it is the only place a
bound is ever applied.  :func:`topology_prescription_from_mapping` reads a
prescription back out of this repository's own durable state, where the question
is different: that prescription was validated when it was accepted, the round it
drove has already been measured, and re-judging it at grading time could only
throw away the evidence of a round that really ran.  The read-back re-checks the
shape (a hand-edited state file is still refused) and returns ``None`` rather
than raising.  The two share the field parsing outright.

**What the receipt must say about authority, and why it is a field.**  A pinned
corner has NO measured ranking behind it.  No shipped path scores one topology
against another, or one corner against another — so a pin is an operator's
choice from an offline argument, not a measurement that beat the alternatives.
:data:`TOPOLOGY_AUTHORITY_OPERATOR_PINNED` is stamped on every accepted
prescription so a receipt read six weeks later cannot be mistaken for a ranked
verdict, exactly as the delay prescription prints the basis it was derived from
rather than leaving provenance to a doc.  It has one value today
because that is the honest state of the world; it is a field rather than a
sentence in a doc because the receipt is what outlives the session.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from typing import Any, Mapping

from jasper.log_event import log_event

from ..driver_protection import PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE
from ..profile import SUPPORTED_LR_ORDERS
from .fc_sweep import (
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    FC_REJECT_BELOW_DECLARED_FLOOR,
    _fc_rejection,
    recornered_preset,
)

__all__ = [
    "TOPOLOGY_AUTHORITY_OPERATOR_PINNED",
    "TOPOLOGY_PRESCRIPTION_KEY",
    "TOPOLOGY_PRESCRIPTION_KIND",
    "TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS",
    "TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED",
    "TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION",
    "TopologyPrescription",
    "TopologyPrescriptionRefused",
    "apply_topology_pin",
    "candidate_topology",
    "read_topology_prescription",
    "topology_prescription_from_mapping",
    "topology_prescription_response_format",
]

logger = logging.getLogger(__name__)

#: Said when a durable read-back cannot be parsed — the one line this module
#: emits, and only on the fail-soft path, where a silent ``None`` would leave a
#: receipt honestly empty with no way to tell that from "no pin was made".
PRESCRIPTION_UNREADABLE_EVENT = "correction.crossover_v2_topology_prescription_unreadable"

#: The request-body key a prescription arrives under.  Named here rather than
#: spelled at the web boundary for the reason every other vocabulary constant in
#: this package is: the reader that owns the shape owns the name of the shape.
TOPOLOGY_PRESCRIPTION_KEY = "topology_prescription"

#: The document version a prescriber of THIS class answers, mirroring
#: :data:`~.driver_prescription.DRIVER_PRESCRIPTION_SCHEMA_VERSION`'s envelope —
#: a pin naming a version this build does not speak is refused rather than
#: best-effort parsed.
TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, mirroring
#: :data:`~.driver_prescription.DRIVER_PRESCRIPTION_KIND`: distinct from every
#: sibling class's own string, so a reader handed the wrong document refuses by
#: name instead of parsing it as something it is not.
TOPOLOGY_PRESCRIPTION_KIND = "jts_crossover_topology_prescription"

#: What stands behind a pinned corner, stamped by the gate onto every accepted
#: prescription.  See this module's docstring: one value, because a topology pin
#: has no measured ranking authority today and a receipt that did not say so
#: would read like one that did.
TOPOLOGY_AUTHORITY_OPERATOR_PINNED = "operator_pinned_no_measured_ranking"

#: The closed vocabulary of refusals, so a caller can branch on a reason without
#: reading a message.  Same rule as :data:`~.contracts.PLAN_REFUSAL_REASONS`: by
#: type and code, never by prose.
TOPOLOGY_MALFORMED = "topology_malformed"
TOPOLOGY_FC_INVALID = "topology_fc_invalid"
TOPOLOGY_ORDER_INVALID = "topology_order_invalid"
#: An order the graph cannot emit.  Its own reason rather than
#: :data:`TOPOLOGY_ORDER_INVALID`, because "6" is a well-formed integer and a
#: perfectly ordinary thing for a prescriber walking a slope sweep to type — the
#: refusal has to send them to the supported set, not to the shape.
TOPOLOGY_ORDER_UNSUPPORTED = "topology_order_unsupported"
TOPOLOGY_PROVENANCE_MISSING = "topology_provenance_missing"
#: The published-slope bound.  The only slope entitled to refuse a corner, and
#: the code is kept across the 2026-08-23 ruling that narrowed what it fires on
#: — a receipt banked before that ruling names this reason, and a second
#: spelling would make the two rounds unsearchable together.  What changed is
#: WHICH number it compares against: the manufacturer's published condition,
#: never the commissioning figure derived from it.  See this module's docstring.
TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT = "topology_slope_below_declared_requirement"
#: A document naming a version this build does not speak. Mirrors
#: :data:`~.driver_prescription.DRIVER_PRESCRIPTION_SCHEMA_UNSUPPORTED`.
TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED = "topology_prescription_schema_unsupported"
TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    TOPOLOGY_MALFORMED,
    TOPOLOGY_FC_INVALID,
    TOPOLOGY_ORDER_INVALID,
    TOPOLOGY_ORDER_UNSUPPORTED,
    TOPOLOGY_PROVENANCE_MISSING,
    TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT,
    TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED,
    # The two declared frequency bounds are the AUTOMATIC path's own, reused
    # rather than re-spelled: a pin and a proposal are admissible on identical
    # terms, and a second vocabulary for one predicate is how the two drift into
    # disagreeing about the same speaker. See :func:`~.fc_sweep._fc_rejection`.
    FC_REJECT_BELOW_DECLARED_FLOOR,
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
})

#: The field names a prescription may carry.  Anything else is refused rather
#: than ignored, on :mod:`.alignment_prescription`'s rule: a misspelled
#: ``basis_artifact`` that silently dropped the provenance would leave a pinned
#: round claiming a basis nobody declared. ``polarity`` is refused BY THIS SET —
#: it is the sibling module's field, and this module's docstring says why.
_PRESCRIPTION_FIELDS = frozenset({
    "kind",
    "artifact_schema_version",
    "fc_hz",
    "order",
    "basis_artifacts",
    "basis_note",
    # Written BY the gate, not supplied to it — but accepted on the way back in
    # so a durable block round-trips through the same parser rather than needing
    # a second, laxer one. A request that supplies them is harmless: the gate
    # overwrites every one of them with what it actually checked.
    "authority",
    "checked_against_floor_hz",
    "checked_against_ceiling_hz",
    "checked_against_slope_db_per_octave",
    "beaming_ceiling_hz",
    "recommended_slope_db_per_octave",
    # Derived on the way out. Accepted on the way in for the same round-trip
    # reason and likewise never trusted — ``slope_db_per_octave`` is a property.
    "slope_db_per_octave",
})

#: Field names this build once WROTE onto a receipt and no longer speaks.
#: Dropped on the durable read-back only (:func:`topology_prescription_from_mapping`),
#: never at the request gate — the same split, and the same reason, as the
#: pre-envelope tolerance in :func:`_parse_prescription`: a banked receipt is a
#: document this repository already wrote and must stay readable across the
#: deploy that retired the field, while a freshly-authored pin naming a retired
#: field is a prescriber talking to a build that no longer exists and learns so
#: at the tap.  ``checked_against_search_band_hz`` recorded the crossover search
#: band the 2026-08-22 ruling deleted (#2870).
_RETIRED_PRESCRIPTION_FIELDS = frozenset({
    "checked_against_search_band_hz",
})


class TopologyPrescriptionRefused(ValueError):
    """One prescription this module would not accept, and why.

    Carries a ``reason`` from :data:`TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS`
    beside the human ``detail``, following
    :class:`~.contracts.CrossoverV2ContractError`'s rule: the classification
    travels with the raise, so a caller never has to re-derive it from wording
    no test owns.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class TopologyPrescription:
    """A validated crossover corner and order, and what proposed them.

    ``fc_hz`` is the corner BOTH branches are split at — the lower driver's
    low-pass and the upper driver's high-pass — matching
    :class:`~jasper.active_speaker.branch_chain.CrossoverSection`'s own frame,
    where one region produces one section per role at one corner.  ``order`` is
    the Linkwitz-Riley order the graph emits for both of them; there is no
    per-role order, because ``CrossoverSection`` has none and a crossover whose
    two halves had different orders would not sum.

    ``basis_artifacts`` names where the corner came from, and is required: a
    pinned candidate whose proposal nobody can find is a receipt with a number
    on it.
    ``basis_note`` is the human line beside it (what argument the corner came
    from, and what that argument weighed) and is optional — it is what a
    reader wants and not something a validator can meaningfully check, so
    requiring it would buy ceremony rather than trust.

    ``authority`` is stamped by the gate, never chosen: see this module's
    docstring for why a pinned corner must say out loud that no measurement
    ranked it.

    The three ``checked_against_*`` fields, ``beaming_ceiling_hz`` and
    ``recommended_slope_db_per_octave`` are the GATE's own record of what it
    compared this pin to, filled after the bounds pass, so a receipt states not
    only what was pinned but WHAT IT CLEARED — a reader who finds
    ``fc_hz: 2400`` on a receipt cannot otherwise tell whether that cleared a
    2500 Hz declared ceiling or a 4000 Hz one, and the declaration is not
    elsewhere in the block.  ``None`` on a record that has not been through the
    gate (a durable read-back of a hand-built block), and ``None`` for a bound
    that was not declared at all.

    **A ``None`` ``checked_against_slope_db_per_octave`` is NOT proof the maker
    prints no slope.**  It means no published condition was on the safety
    profile, and there are two ways to get there: the datasheet really carries
    no qualifier, or the profile predates the target field that holds one — in
    which case no driver on that speaker has a published slope until its next
    ``/sound/`` save, and this receipt says only that none was found.  A reader
    grading an old round must not read the empty slot as a datasheet fact.  See
    ``excitation_safety_plan.resolve_driver_protection_slope_db_per_octave``,
    which owns that read.
    """

    fc_hz: float
    order: int
    basis_artifacts: tuple[str, ...]
    basis_note: str = ""
    authority: str = ""
    checked_against_floor_hz: float | None = None
    checked_against_ceiling_hz: float | None = None
    checked_against_slope_db_per_octave: float | None = None
    #: The ka/beaming onset this corner was compared to, DISCLOSED and never
    #: enforced (#1675). Present and above ``fc_hz`` means the candidate is
    #: below the onset; present and below it means it is above and the receipt
    #: says so. ``None`` means the lower driver declared no diameter, which is
    #: an absent prior rather than a satisfied one.
    beaming_ceiling_hz: float | None = None
    #: This build's commissioning recommendation for a protective high-pass
    #: slope (``driver_protection.PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE``),
    #: DISCLOSED and never enforced — the beaming ceiling's sibling, read the
    #: same way: above ``slope_db_per_octave`` means this pin crosses shallower
    #: than the build recommends and the receipt says so. It is a code figure,
    #: which the 2026-08-22 ruling bars from refusing anything, and the design
    #: page's ``tweeter_slope_below_recommended_floor`` warning names the same
    #: number for the same reason. Stamped whatever the manufacturer published,
    #: because a pin admitted with no published condition at all is exactly the
    #: round whose receipt most needs to say what it was shallower than.
    #: ``None`` only on a record that has not been through the gate.
    recommended_slope_db_per_octave: float | None = None

    @property
    def slope_db_per_octave(self) -> float:
        """What this order attenuates at, in the units declarations use.

        The one place the order becomes the quantity a manufacturer's minimum
        is expressed in, mirroring
        :func:`~jasper.active_speaker.branch_chain.confirmed_protection_sections`
        on the way back (it turns the confirmed protective slope into the
        smallest supported order that meets it, by the same ``order * 6``
        relation).  Kept here
        rather than at the gate because the module that owns the order owns its
        translation.
        """
        return float(self.order) * 6.0

    def to_dict(self) -> dict[str, Any]:
        """The receipt's view: what was pinned, what justifies it, what it
        cleared, and what stands behind it."""
        return {
            "artifact_schema_version": TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION,
            "kind": TOPOLOGY_PRESCRIPTION_KIND,
            "fc_hz": self.fc_hz,
            "order": self.order,
            "slope_db_per_octave": self.slope_db_per_octave,
            "basis_artifacts": list(self.basis_artifacts),
            "basis_note": self.basis_note,
            "authority": self.authority,
            "checked_against_floor_hz": self.checked_against_floor_hz,
            "checked_against_ceiling_hz": self.checked_against_ceiling_hz,
            "checked_against_slope_db_per_octave": (
                self.checked_against_slope_db_per_octave
            ),
            "beaming_ceiling_hz": self.beaming_ceiling_hz,
            "recommended_slope_db_per_octave": self.recommended_slope_db_per_octave,
        }


def _finite_number(value: Any, *, reason: str, field: str) -> float:
    """One numeric field, strictly.

    ``bool`` is refused explicitly because it is an ``int`` in Python and
    ``float(True)`` is ``1.0`` — a corner of "true" hertz would otherwise
    validate.  Strings are refused for
    :func:`.blend_correction.blend_filters_from_mapping`'s reason:
    ``float("4000")`` succeeds, so accepting one would make the reader's
    strictness depend on the encoder's habits rather than on the contract.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TopologyPrescriptionRefused(
            reason, f"{field} must be a number, got {type(value).__name__}",
        )
    number = float(value)
    if not math.isfinite(number):
        raise TopologyPrescriptionRefused(
            reason, f"{field} must be finite, got {number!r}",
        )
    return number


def _read_order(value: Any) -> int:
    """The Linkwitz-Riley order, strictly, and only one the graph can emit.

    Refused in two steps because they send a prescriber to two different
    places: a shape that is not an integer at all is a malformed request, and a
    well-formed integer outside
    :data:`~jasper.active_speaker.profile.SUPPORTED_LR_ORDERS` is a request for
    a filter this system does not build.  A float is refused rather than
    truncated for :func:`_finite_number`'s reason — ``int(4.7)`` is ``4``, and
    an order that quietly became a different order is exactly the silently
    different candidate this module exists to prevent.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TopologyPrescriptionRefused(
            TOPOLOGY_ORDER_INVALID,
            f"order must be an integer, got {type(value).__name__}",
        )
    if value not in SUPPORTED_LR_ORDERS:
        raise TopologyPrescriptionRefused(
            TOPOLOGY_ORDER_UNSUPPORTED,
            f"order must be one of {sorted(SUPPORTED_LR_ORDERS)}, got {value}",
        )
    return int(value)


def _read_artifacts(value: Any) -> tuple[str, ...]:
    """The named provenance, strictly and non-empty.

    A bare string is refused rather than wrapped: ``"a,b"`` and ``["a", "b"]``
    would otherwise be one artifact and two, decided by punctuation.
    """
    if value is None:
        raise TopologyPrescriptionRefused(
            TOPOLOGY_PROVENANCE_MISSING,
            "a prescription must name the basis_artifacts it was proposed from",
        )
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TopologyPrescriptionRefused(
            TOPOLOGY_PROVENANCE_MISSING,
            "basis_artifacts must be a list of names, got "
            f"{type(value).__name__}",
        )
    artifacts: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise TopologyPrescriptionRefused(
                TOPOLOGY_PROVENANCE_MISSING,
                "every basis_artifacts entry must be a non-blank name",
            )
        artifacts.append(entry.strip())
    if not artifacts:
        raise TopologyPrescriptionRefused(
            TOPOLOGY_PROVENANCE_MISSING,
            "a prescription must name at least one basis artifact",
        )
    return tuple(artifacts)


def _optional_number(value: Any) -> float | None:
    """A finite number, or ``None`` — never a raise.

    These fields are the GATE's own record of what it checked, not a
    requester's claim, so an unreadable one on the way back in is missing
    context rather than a malformed prescription.  Refusing here would let a
    truncated durable block cost a round its provenance entirely.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_prescription(
    raw: Mapping[str, Any], *, read_back: bool = False,
) -> TopologyPrescription:
    """The shape and the provenance, and NOT the bounds.

    Shared whole between the request gate and the durable read-back, so the
    only thing that differs between those two is their gate policy — which is
    the only thing that should.

    ``read_back`` is that one difference, and mirrors
    :func:`~.alignment_prescription._parse_prescription`'s own parameter for the
    identical reason. ``False`` (the request gate,
    :func:`read_topology_prescription`) judges a freshly-authored pin: a
    missing or wrong ``kind``/``artifact_schema_version`` is a malformed
    document and refuses at the tap. ``True`` (the durable read-back,
    :func:`topology_prescription_from_mapping`, and
    :func:`.durable_state.topology_prescription_prior_from_state`'s
    ``verify_priors`` rehydration) judges a document THIS repository already
    wrote — ``verify_priors.topology_prescription`` is carried unconditionally
    across deploys, and predates this envelope by three days (#2662/#2773
    landed before it). A record naming NEITHER field is exactly that
    pre-envelope shape and reads as this build's own kind and version 1,
    never a raise; a record naming EITHER field, even if the other is missing
    or wrong, is refused under both postures.

    It buys ONE more tolerance, on the same terms and for the same reason: a
    banked receipt may carry a field this build has since RETIRED
    (:data:`_RETIRED_PRESCRIPTION_FIELDS`), and the read-back drops it rather
    than refusing the record. Dropped, never read — a retired field's VALUE has
    no meaning to this build, and pretending otherwise is how a deleted concept
    keeps deciding things. The request gate still refuses one, so a prescriber
    authoring a retired field learns immediately.
    """
    if not isinstance(raw, Mapping):
        raise TopologyPrescriptionRefused(
            TOPOLOGY_MALFORMED,
            f"a prescription must be a mapping, got {type(raw).__name__}",
        )
    known = (
        _PRESCRIPTION_FIELDS | _RETIRED_PRESCRIPTION_FIELDS
        if read_back
        else _PRESCRIPTION_FIELDS
    )
    unknown = sorted(set(raw) - known)
    if unknown:
        raise TopologyPrescriptionRefused(
            TOPOLOGY_MALFORMED,
            f"unknown prescription field(s): {', '.join(unknown)}",
        )
    pre_envelope = (
        read_back and "kind" not in raw and "artifact_schema_version" not in raw
    )
    if not pre_envelope:
        if raw.get("kind") != TOPOLOGY_PRESCRIPTION_KIND:
            raise TopologyPrescriptionRefused(
                TOPOLOGY_MALFORMED,
                f"a prescription must name kind={TOPOLOGY_PRESCRIPTION_KIND!r}, "
                f"got {raw.get('kind')!r}",
            )
        version = raw.get("artifact_schema_version")
        if version != TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION:
            raise TopologyPrescriptionRefused(
                TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED,
                f"this build speaks topology-prescription schema "
                f"{TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION}, got {version!r}",
            )
    if "fc_hz" not in raw:
        raise TopologyPrescriptionRefused(
            TOPOLOGY_FC_INVALID, "a prescription must state fc_hz",
        )
    fc_hz = _finite_number(raw["fc_hz"], reason=TOPOLOGY_FC_INVALID, field="fc_hz")
    if fc_hz <= 0.0:
        raise TopologyPrescriptionRefused(
            TOPOLOGY_FC_INVALID, f"fc_hz must be above zero, got {fc_hz!r}",
        )
    if "order" not in raw:
        raise TopologyPrescriptionRefused(
            TOPOLOGY_ORDER_INVALID, "a prescription must state its order",
        )
    note = raw.get("basis_note", "")
    if not isinstance(note, str):
        raise TopologyPrescriptionRefused(
            TOPOLOGY_PROVENANCE_MISSING,
            f"basis_note must be text, got {type(note).__name__}",
        )
    authority = raw.get("authority", "")
    if not isinstance(authority, str):
        raise TopologyPrescriptionRefused(
            TOPOLOGY_MALFORMED,
            f"authority must be text, got {type(authority).__name__}",
        )
    return TopologyPrescription(
        fc_hz=fc_hz,
        order=_read_order(raw["order"]),
        basis_artifacts=_read_artifacts(raw.get("basis_artifacts")),
        basis_note=note,
        authority=authority,
        checked_against_floor_hz=_optional_number(raw.get("checked_against_floor_hz")),
        checked_against_ceiling_hz=_optional_number(
            raw.get("checked_against_ceiling_hz")
        ),
        checked_against_slope_db_per_octave=_optional_number(
            raw.get("checked_against_slope_db_per_octave")
        ),
        beaming_ceiling_hz=_optional_number(raw.get("beaming_ceiling_hz")),
        recommended_slope_db_per_octave=_optional_number(
            raw.get("recommended_slope_db_per_octave")
        ),
    )


def read_topology_prescription(
    raw: Mapping[str, Any] | None,
    *,
    declared_floor_hz: float | None,
    lower_driver_ceiling_hz: float | None,
    minimum_slope_db_per_octave: float | None,
    beaming_ceiling_hz: float | None,
    way_count: int | None = None,
) -> TopologyPrescription | None:
    """THE request gate.  One point, and the one place a bound is applied.

    ``None`` when the request carries no prescription — the automatic path,
    untouched.  Otherwise a validated :class:`TopologyPrescription`, or
    :class:`TopologyPrescriptionRefused` naming which gate said no.

    **The web boundary short-circuits before it gets here**, so in production
    this function is only ever handed a real block.  That is not a redundancy
    to tidy away in either direction: the boundary skips the call because the
    four declarations below cost a context read each, and an ORDINARY round
    must not depend on declarations it is not using; this function still
    tolerates ``None`` because it is a public gate and a total one is cheaper
    to reason about than one whose contract is "ask my caller".  Absence is
    decided in exactly one place — at the request key — and both spellings
    agree about what it means.

    **Every keyword is required and undefaulted**, on
    :func:`~.alignment_prescription.read_alignment_prescription`'s rule and for
    the sharpest version of it: unlike the delay gate, NONE of these rest on a
    number the requester supplied — every one is a declaration the household or
    the manufacturer made about this hardware.  A caller that forgot one would
    lose the hardware's own opinion about a candidate it is about to play, and never
    know, which is exactly the failure a defaulted keyword hides.  ``None`` is
    still a legal VALUE for the two that admit it, and it means the same thing
    each time: that bound was not declared, so there is nothing to gate on —
    never a guessed default, on
    :func:`~jasper.active_speaker.driver_protection.declared_protection_highpass_floor_hz`'s
    never-nanny rule.

    ``declared_floor_hz`` and ``lower_driver_ceiling_hz`` are the two role
    bands a corner is admissible within — ``None`` only from a caller that has
    no second role to read them off, which is the ``way_count == 1`` refusal
    above and never reaches the bound check below — :func:`~.fc_sweep._fc_rejection`'s own
    ``hf_hard_floor_hz`` / ``lower_driver_hard_ceiling_hz``.  They are the WHOLE
    frequency gate: a corner both drivers' declared hard bands admit is
    admitted, and the invented crossover search band that used to narrow them
    further was deleted by the 2026-08-22 owner ruling (#2870).

    ``minimum_slope_db_per_octave`` is the PROTECTED (upper) role's PUBLISHED
    high-pass slope condition, and only that role's.  A two-way corner
    high-passes the upper driver at ``fc_hz``, so what the manufacturer printed
    beside its minimum crossover frequency is a claim about what that filter
    must do; the lower driver's own protection high-pass is a claim about the
    BOTTOM of its band and has nothing to say about the corner.  ``None`` means
    no published condition is on the safety profile — either the datasheet
    prints none, or the profile predates the field that carries one — and
    either way there is no slope bound to apply, never the commissioning figure
    standing in, which is the fusion the 2026-08-23 ruling struck (see this
    module's docstring, and :class:`TopologyPrescription` for what the resulting
    empty receipt slot may and may not be read as).

    **The bounds are inclusive.**  An order whose slope exactly meets the
    published minimum is legal, and a corner exactly at a declared edge is legal
    — the 2026-08-17 owner ruling on the declared floor ("if the manufacturer
    says 1600, we should be able to do it; no nannies") applied to every edge
    here, and the same ruling's reasoning: exactness is legal in this
    repository's gates, and a strict comparison would make the legality of a
    round depend on floating-point noise.
    """
    if raw is None:
        return None
    if way_count == 1:
        # A pin re-corners a crossover; a 1-way main declares none, so there is
        # no corner to move and the four bounds below name nothing. Refused
        # HERE, at the gate that owns "may this prescription run", rather than
        # at a host that would have to read two role bands to ask.
        raise TopologyPrescriptionRefused(
            TOPOLOGY_FC_INVALID,
            "this speaker declares no crossover region to re-corner",
        )
    if declared_floor_hz is None or lower_driver_ceiling_hz is None:
        # The only caller that omits them is one with no second role to read
        # them off, and the gate above already refused the shape that has none.
        # Reached otherwise, a pin would be admitted against bounds nobody
        # supplied — so it is refused, in this module's own vocabulary.
        raise TopologyPrescriptionRefused(
            TOPOLOGY_MALFORMED,
            "the role bands a corner must sit between were not declared",
        )
    prescription = _parse_prescription(raw)
    # Frequency before slope. The two send a prescriber to different places —
    # re-declare the band vs re-choose the order — and a corner that is outside
    # the hardware's declared range is the more fundamental of the two answers.
    #
    # ``fc_sweep._fc_rejection`` is the single owner of "is this corner
    # admissible for this speaker", including the hardest-first ordering, and is
    # imported by its private name deliberately: a pinned corner and a declared
    # one have to be admissible on *identical* terms — an operator who pins the
    # corner their declarations already permit must not be refused, and one who
    # pins a corner those declarations exclude must not be admitted — and a
    # second spelling of the comparisons is precisely how those two answers
    # drift apart on one speaker. ``tests/test_crossover_v2_topology_prescription.py``
    # pins the two to agree across a grid, so the call cannot quietly become a
    # copy.
    #
    # The predicate carries no beaming term, and that absence is #1675's ruling
    # rather than an omission: the ka onset is guidance to warn on, not a fence.
    # It rides ``TopologyPrescription.beaming_ceiling_hz`` as disclosure
    # instead, so a receipt can say a candidate was above it.
    reason = _fc_rejection(
        prescription.fc_hz,
        float(declared_floor_hz),
        float(lower_driver_ceiling_hz),
    )
    if reason == FC_REJECT_BELOW_DECLARED_FLOOR:
        raise TopologyPrescriptionRefused(
            reason,
            f"{prescription.fc_hz:.1f} Hz is below the upper driver's declared "
            f"floor of {float(declared_floor_hz):.1f} Hz",
        )
    if reason == FC_REJECT_ABOVE_LOWER_DRIVER_BAND:
        raise TopologyPrescriptionRefused(
            reason,
            f"{prescription.fc_hz:.1f} Hz is above the lower driver's declared "
            f"ceiling of {float(lower_driver_ceiling_hz):.1f} Hz",
        )
    if reason is not None:  # pragma: no cover - defensive
        # Unreachable while ``_fc_rejection``'s vocabulary is the two codes
        # ``fc_sweep`` declares, each of which the cases above already name.
        # Kept because the alternative to naming an unhandled code is admitting
        # a pin the shared predicate refused, which is the one outcome this
        # reuse exists to make impossible.
        raise TopologyPrescriptionRefused(TOPOLOGY_MALFORMED, reason)
    if minimum_slope_db_per_octave is not None:
        published_slope = float(minimum_slope_db_per_octave)
        if prescription.slope_db_per_octave < published_slope:
            raise TopologyPrescriptionRefused(
                TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT,
                f"order {prescription.order} crosses at "
                f"{prescription.slope_db_per_octave:.0f} dB/octave, below the "
                f"protected driver's published minimum of {published_slope:g} "
                "dB/octave",
            )
    # What the bounds were actually evaluated against, recorded on the record
    # the receipt banks, plus the authority caveat this class always carries.
    return replace(
        prescription,
        authority=TOPOLOGY_AUTHORITY_OPERATOR_PINNED,
        checked_against_floor_hz=float(declared_floor_hz),
        checked_against_ceiling_hz=float(lower_driver_ceiling_hz),
        checked_against_slope_db_per_octave=(
            None if minimum_slope_db_per_octave is None
            else float(minimum_slope_db_per_octave)
        ),
        beaming_ceiling_hz=(
            None if beaming_ceiling_hz is None else float(beaming_ceiling_hz)
        ),
        recommended_slope_db_per_octave=PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE,
    )


def topology_prescription_from_mapping(raw: Any) -> TopologyPrescription | None:
    """A prescription read back out of this repository's own durable state.

    The read-back half of the pair this module's docstring describes: the same
    shape and provenance checks, deliberately NOT the bounds, and ``None``
    instead of a raise.

    Why no bounds.  The only mappings that reach here were written by
    :func:`read_topology_prescription` accepting them, so re-applying a bound
    could not catch a prescription the boundary let through — it could only
    refuse one whose DECLARATIONS moved between the stage that measured the
    round and the stage that grades it, and refusing there would discard the
    evidence of a round that really ran.  The bounds have one owner, and it is
    the boundary.

    Why still strict about shape.  A hand-edited or truncated state file is a
    real input here, and a round graded at the wrong corner is worse than a
    round graded with no provenance.  Anything unreadable is ``None`` plus one
    WARNING, so an empty slot on a receipt is always distinguishable from a
    silently mangled one.

    ``read_back=True`` on the shared parser: a record naming neither ``kind``
    nor ``artifact_schema_version`` is the shape this build's own prior
    releases wrote, before this envelope existed, and reads as that build's
    own kind and version 1 rather than refusing — see
    :func:`_parse_prescription`'s docstring for why. A record naming either
    field, even if the other is missing or wrong, is still refused.
    """
    if raw is None:
        return None
    try:
        return _parse_prescription(raw, read_back=True)
    except TopologyPrescriptionRefused as exc:
        log_event(
            logger,
            PRESCRIPTION_UNREADABLE_EVENT,
            level=logging.WARNING,
            reason=exc.reason,
            detail=exc.detail,
        )
        return None


def apply_topology_pin(
    prescription: TopologyPrescription | None, *, preset: Any, fc_hz: float | None,
) -> tuple[Any, float | None]:
    """What a pin DOES to a session's topology: ``(preset, fc_hz)``.

    ``(preset, fc_hz)`` unchanged when there is no pin — the automatic path.
    ``fc_hz`` is ``None`` on a speaker that declares no corner, and a pin cannot
    reach one: the request boundary refuses a topology prescription for a
    speaker with no crossover to re-corner.
    Otherwise the same preset re-cornered at the pinned corner and order
    (:func:`~.fc_sweep.recornered_preset`) and the pinned corner itself.

    **Both stages call this, and that is the whole reason it exists here rather
    than at the boundary.**  Stage 1 opens the measuring session at the pin;
    stage 2 re-opens the GRADING session at the same pin, or it hands VERIFY
    the incumbent design target and grades an applied graph for not being the
    crossover it deliberately replaced.  Those are two call sites for one
    decision, and a decision spelled twice is one that drifts — the two would
    not have to disagree by much before a round was measured at one corner and
    graded at another.

    Returned as a pair rather than mutating a context because the caller's
    context is the web host's shape and this module may not know it.
    """
    if prescription is None:
        return preset, None if fc_hz is None else float(fc_hz)
    return (
        recornered_preset(
            preset, fc_hz=prescription.fc_hz, order=prescription.order,
        ),
        prescription.fc_hz,
    )


def candidate_topology(candidate: Any) -> dict[str, Any] | None:
    """WHERE one built candidate crosses, read off the candidate's OWN preset.

    Not off any session's ``fc_hz``, and the difference is the point: a
    candidate carries the preset it was realized with, so this reports the
    crossover the reviewed graph actually contains rather than the one a caller
    believes it asked for.  On a pinned round the two agree by construction; this
    is the reading that keeps saying so rather than assuming it.

    ``None`` when the candidate declares no crossover region, which
    :func:`~jasper.active_speaker.branch_chain.sections_by_role` already treats
    as "this role runs full range" — there is no corner to name, and inventing
    one would be the guess that function refuses to make.

    The slope is DERIVED rather than carried as a second number, on the
    ``order * 6`` relation :attr:`TopologyPrescription.slope_db_per_octave`
    already owns, so a household surface can render whichever of the two words
    reads better without a second source for the same fact.

    Duck-typed on ``source_preset.crossover_regions``: this module does not
    import the candidate type, exactly as it does not import the preset schema.
    """
    regions = getattr(
        getattr(candidate, "source_preset", None), "crossover_regions", (),
    )
    region = next(iter(regions or ()), None)
    if region is None:
        return None
    fc_hz = _optional_number(getattr(region, "fc_hz", None))
    order = getattr(region, "order", None)
    if fc_hz is None or isinstance(order, bool) or not isinstance(order, int):
        return None
    return {"fc_hz": fc_hz, "order": order, "slope_db_per_octave": order * 6.0}


def topology_prescription_response_format() -> dict[str, Any]:
    """What a prescriber must send to pin a topology, and where to send it.

    The third of the evidence packet's contract blocks, beside
    :func:`~.blend_prescription.prescription_response_format` and
    :func:`~.driver_prescription.driver_prescription_response_format` — and the
    reason it is here at all is #2773: those two enter through the prescriber
    CLI's stage step, while this one and the alignment prescription enter as
    REQUEST-BODY KEYS on the session-open call, with a different severity.  A
    reader who found only the two staged contracts would never learn the
    request-time doors exist.  Stating all of them in one place is what makes
    the four classes discoverable as one family.
    """
    return {
        "key": TOPOLOGY_PRESCRIPTION_KEY,
        "entry": "request_body",
        "entry_detail": (
            "sent as the '" + TOPOLOGY_PRESCRIPTION_KEY + "' key on "
            "POST /crossover/v2/session, not staged through "
            "jasper-crossover-prescriber"
        ),
        "severity": (
            "a refused prescription refuses the whole session at the tap; it "
            "is never clamped to a legal corner and never partially applied"
        ),
        "authority": TOPOLOGY_AUTHORITY_OPERATOR_PINNED,
        "authority_detail": (
            "a pinned corner is an operator's choice from an offline argument, "
            "not a measured ranking: no shipped path scores one topology "
            "against another, so the round measures the candidate you asked for and "
            "says nothing about whether a different corner would be better"
        ),
        "fields": {
            "kind": (
                f"required, must be exactly {TOPOLOGY_PRESCRIPTION_KIND!r}"
            ),
            "artifact_schema_version": (
                "required, must be exactly "
                f"{TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION}"
            ),
            "fc_hz": "required number, the corner BOTH branches split at",
            "order": (
                "required integer, one of "
                + ", ".join(str(o) for o in sorted(SUPPORTED_LR_ORDERS))
                + "; its slope is order * 6 dB/octave. Refused only when the "
                "protected driver's MAKER published a steeper minimum — a "
                "shallower order than this build commissions at is admitted "
                "and disclosed on the record instead"
            ),
            "basis_artifacts": (
                "required non-empty list of names — what proposed this corner"
            ),
            "basis_note": "optional human line beside the artifacts",
        },
        "refusals": sorted(TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS),
        "not_accepted": {
            "polarity": (
                "pinned through the alignment_prescription request key, which "
                "owns the field; sending it here is refused as an unknown field"
            ),
        },
    }
