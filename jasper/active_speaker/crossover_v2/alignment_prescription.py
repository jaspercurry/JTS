# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE inter-driver delay — and optionally its polarity basin — prescribed from
a named measurement (#2662).

A sibling of :mod:`.blend_correction`: pure functions, no I/O, no session, and
the strict reader for one prescription the host hands the machinery.  That
module prescribes a *shape* for the blend region; this one prescribes the
*timing* between the two drivers.

**Why a prescription exists at all.**  The automatic aligner estimates the
inter-driver delay from the capture, and on the 2026-08-17 series it estimated
it with the wrong sign and hopped correlation lobes between captures of the
same speaker (the direct arrival gap was invariant at −405.7 ± 3.3 µs, n = 33,
while the estimator's own answers across those same captures ran +96, +62,
−231, −367, −280 µs).  A wrong estimator is not fixed by widening the automatic
search — PR #1649's snap radius is a deliberate ambiguity budget and its
negatives stand — it is answered by letting a bench measurement *say the
number*, and then making the ordinary machinery measure and grade that number
like any other candidate.

**Why the polarity may be pinned too.**  ``polarity`` is OPTIONAL and defaults
to absent, which is the automatic path: the flat-sum objective solves the
polarity as it always has.  It exists because delay and polarity are one
decision with two degenerate answers — invert plus half a period at Fc sums
almost identically on axis — so a fit re-run at one physical configuration can
land in a different basin each time.  On 2026-08-19 three successive stage-1
fits of one speaker solved three: (tweeter +34 µs, keep), (woofer +314 µs,
invert), and (woofer +314 µs, keep).  The second measured best off axis (2.37
vs 3.10 dB pooled, on axis unchanged) and was kept; the third is the anti-phase
notch the first two are not (3.86 on axis, auto-rolled back by the delta
probe).  A staged round that wants to measure ONE variable — a blend cut, an
Fc — cannot do so while the basin re-rolls underneath it, so pinning the basin
is what makes a round single-variable.  It is a constraint on the SOLVE and not
a stamp on the answer: the trims and the delay re-solve underneath the pinned
polarity, so the candidate is coherent rather than edited.

**One field, one owner.**  This module produces no second runtime knob.  A
prescription is a way of COMPUTING the delay that
:class:`~jasper.audio_measurement.program_analysis.AlignmentEstimate` already
owns: it is handed down as
``MeasurementPriors.explicit_alignment_delay_us``, committed by
``_select_alignment_pair`` as that estimate's ``delay_us``, and folded onto the
candidate by the same
:func:`~jasper.active_speaker.crossover_v2.planning.alignment_to_candidate_fields`
every automatic round goes through.  A pinned polarity travels the identical
route — down as ``MeasurementPriors.explicit_alignment_polarity_sign``,
committed by the same selection as that estimate's ``polarity_sign``, folded on
by the same function — for the same reason, and so that the two halves of one
alignment can never enter by two different doors.  Every consumer downstream of
that estimate
— the summed model's residual, the predicted sum the accountability gate
grades, the commanded delta, the headroom recompute, the emitted graph and its
``prove_static_delay_binding`` proof, VERIFY's tracking reference — reads the
prescribed number because it reads the one field.  That coherence is the whole
reason the prescription enters at the estimate rather than being stamped onto
the candidate afterwards: a candidate whose graph carries one delay and whose
prediction models another is the exact defect the series-2 diagnosis found in
the automatic path (`predicted_ripple_db` describing a speaker whose drivers
are time-coincident when they were 0.4 ms apart).

**Two gates, and they compose.**  Neither is sufficient alone:

* **Provenance** makes the basis auditable.  A prescription must name what it
  was measured from, so a human reading the round receipt can go and check.
* **The bound** makes the prescription a bounded excursion from that named
  basis: it may not leave the summation more than half a period at Fc away from
  the basis's own answer — the comb lobe, the same geometry the aligner's
  ``left_anchor_lobe`` tripwire watches, taken from
  :func:`~jasper.audio_measurement.program_analysis.half_period_us` so the two
  cannot drift apart.

**Where the bound is measured FROM, and why it is not the incumbent.**  From
the declared measured basis, not from the delay the speaker currently plays.
A step-size bound anchored on the incumbent would forbid exactly the correction
a lobe-hopped incumbent needs: on the series-2 speaker the incumbent sat
+501.7 µs from the physically-coincident answer — **1.65 half-period lobes**
(0.83 of a period at Fc = 1648.7 Hz, whose lobe is 303.27 µs) — so every
candidate that could fix it would be refused, the only admissible prescription would be
one that changed almost nothing, and the bound would be widened by hand at the
first bench session, which is how a fail-closed guard becomes decorative.  Anchoring on the basis asks the question that is actually
worth asking: *does this prescription leave the drivers within one lobe of
where the measurement says coincident is?*  The incumbent is not ignored; it is
simply not the reference — the aligner still evaluates ``left_anchor_lobe``
against the capture's own anchor and still raises its selection log when a
commitment leaves it.

**Fail-closed, never clamped.**  Every refusal below names its reason and
raises.  A prescription that is out of bounds is not pulled to the boundary and
run: the operator asked for a candidate, and a silently different candidate is
worse than none, because its receipt would carry the candidate's name.

**Absence is the automatic path, and it is never inherited.**  ``None`` from
:func:`read_alignment_prescription` means no prescription was made, and every
byte of the automatic selection is unchanged.  Unlike the session tier — which
a re-measure deliberately inherits from the lapsed session (#2639) — a
prescription is per-round and explicit.  Inheriting one would let an operator's
"measure again" silently re-run a candidate they did not ask for, which is the same
class of dishonesty as clamping.

**One parser, two gate policies.**  :func:`read_alignment_prescription` is the
REQUEST gate — shape, provenance, and the bound — and it is the only place the
bound is ever applied.  :func:`alignment_prescription_from_mapping` reads a
prescription back out of this repository's own durable state, where the
question is different: that prescription was validated when it was accepted,
the round it drove has already been measured, and re-judging it at grading time
could only ever throw away the evidence of a round that really happened.  So
the read-back re-checks the shape (a hand-edited state file is still refused)
and returns ``None`` rather than raising.  The two share the field parsing
outright, so the only thing that differs between them is the thing that should.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from typing import Any, Mapping

from jasper.active_speaker.crossover_alignment import (
    POLARITY_INVERT,
    POLARITY_KEEP,
)
from jasper.audio_measurement.program_analysis import half_period_us
from jasper.log_event import log_event

__all__ = [
    "ALIGNMENT_PRESCRIPTION_KEY",
    "ALIGNMENT_PRESCRIPTION_KIND",
    "ALIGNMENT_PRESCRIPTION_MALFORMED",
    "ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING",
    "ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS",
    "ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED",
    "ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION",
    "AlignmentPrescription",
    "AlignmentPrescriptionRefused",
    "alignment_prescription_from_mapping",
    "alignment_prescription_response_format",
    "read_alignment_prescription",
]

logger = logging.getLogger(__name__)

#: Said when a durable read-back cannot be parsed — the one line this module
#: emits, and only on the fail-soft path, where a silent ``None`` would leave a
#: receipt honestly empty with no way to tell that from "no prescription was
#: made".
PRESCRIPTION_UNREADABLE_EVENT = "correction.crossover_v2_alignment_prescription_unreadable"

#: The request-body key a prescription arrives under.  Named here rather than
#: spelled at the web boundary for the reason every other vocabulary constant in
#: this package is: the reader that owns the shape owns the name of the shape.
ALIGNMENT_PRESCRIPTION_KEY = "alignment_prescription"

#: The document version a prescriber of THIS class answers, mirroring
#: :data:`~.driver_prescription.DRIVER_PRESCRIPTION_SCHEMA_VERSION`'s envelope —
#: a proposal naming a version this build does not speak is refused rather than
#: best-effort parsed.
ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, mirroring
#: :data:`~.driver_prescription.DRIVER_PRESCRIPTION_KIND`: distinct from every
#: sibling class's own string, so a reader handed the wrong document refuses by
#: name instead of parsing it as something it is not.
ALIGNMENT_PRESCRIPTION_KIND = "jts_crossover_alignment_prescription"

#: The closed vocabulary of refusals, so a caller can branch on a reason without
#: reading a message.  Same rule as
#: :data:`~.contracts.PLAN_REFUSAL_REASONS`: by type and code, never by prose.
#:
#: ``ALIGNMENT_PRESCRIPTION_`` prefixed on the three names that would otherwise
#: collide with :mod:`.blend_prescription`'s own bare ``PRESCRIPTION_MALFORMED``
#: / ``PRESCRIPTION_PROVENANCE_MISSING`` / ``PRESCRIPTION_REFUSAL_REASONS`` —
#: two different closed vocabularies for two different seams, sharing one name.
#: Values are unchanged; only the Python identifiers moved, to
#: :data:`~.alignment_prescription.ALIGNMENT_PRESCRIPTION_KEY`'s own style.
ALIGNMENT_PRESCRIPTION_MALFORMED = "prescription_malformed"
PRESCRIPTION_DELAY_INVALID = "prescription_delay_invalid"
PRESCRIPTION_BASIS_INVALID = "prescription_basis_invalid"
ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING = "prescription_provenance_missing"
PRESCRIPTION_FC_UNKNOWN = "prescription_fc_unknown"
PRESCRIPTION_OUT_OF_LOBE = "prescription_out_of_lobe"
#: The preset's own declared delay window — the ONE bound in this gate that
#: does not depend on a number the operator supplied. It has always existed
#: (``crossover_v2_flow.alignment_delay_plausible``, the Fix-3 screen) but it
#: fired ten minutes into a session, at a MEASURE screen whose household copy
#: asks the user to move the microphone; on a prescribed candidate that copy is a lie
#: about a number the request could have been refused for at the tap. Its own
#: reason, never that screen's.
PRESCRIPTION_OUTSIDE_DECLARED_WINDOW = "prescription_outside_declared_window"
#: A ``polarity`` that is neither of the two words the candidate's own alignment
#: speaks. Its own reason rather than :data:`ALIGNMENT_PRESCRIPTION_MALFORMED`,
#: because a misspelled basin ("inverted", "flip", "-1") is the one shape an
#: operator walking a basin sweep will actually type, and the refusal has to
#: send them to the vocabulary rather than to the shape.
PRESCRIPTION_POLARITY_INVALID = "prescription_polarity_invalid"
#: A document naming a version this build does not speak. Mirrors
#: :data:`~.driver_prescription.DRIVER_PRESCRIPTION_SCHEMA_UNSUPPORTED`.
ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED = "alignment_prescription_schema_unsupported"
ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    ALIGNMENT_PRESCRIPTION_MALFORMED,
    PRESCRIPTION_DELAY_INVALID,
    PRESCRIPTION_BASIS_INVALID,
    ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
    PRESCRIPTION_FC_UNKNOWN,
    PRESCRIPTION_OUT_OF_LOBE,
    PRESCRIPTION_OUTSIDE_DECLARED_WINDOW,
    PRESCRIPTION_POLARITY_INVALID,
    ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED,
})

#: The field names a prescription may carry.  Anything else is refused rather
#: than ignored: a misspelled ``basis_artifact`` that silently dropped the
#: provenance would leave the bound checking a prescription against a basis
#: nobody declared, which is the one failure this reader exists to prevent.
_PRESCRIPTION_FIELDS = frozenset({
    "kind",
    "artifact_schema_version",
    "delay_us",
    "basis_delay_us",
    "basis_artifacts",
    "basis_note",
    # Optional, and the one field here that pins something OTHER than the
    # timing. Absent is the automatic path.
    "polarity",
    # Written BY the gate, not supplied to it — but accepted on the way back in
    # so a durable block round-trips through the same parser rather than
    # needing a second, laxer one. A request that supplies them is harmless:
    # the gate overwrites both with what it actually checked.
    "checked_at_fc_hz",
    "lobe_us",
    # Derived on the way out. Accepted on the way in for the same round-trip
    # reason and likewise never trusted — ``residual_us`` is a property.
    "residual_us",
})


class AlignmentPrescriptionRefused(ValueError):
    """One prescription this module would not accept, and why.

    Carries a ``reason`` from :data:`ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS`
    beside the human ``detail``, following
    :class:`~.contracts.CrossoverV2ContractError`'s
    rule: the classification travels with the raise, so a caller never has to
    re-derive it from wording no test owns.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class AlignmentPrescription:
    """A validated inter-driver delay, and the measurement that justifies it.

    ``delay_us`` and ``basis_delay_us`` are both in
    :class:`~jasper.audio_measurement.program_analysis.AlignmentEstimate`'s
    signed frame — ``(D_woofer − D_tweeter)``, so a POSITIVE value delays the
    tweeter and a negative one delays the woofer.  One frame for both is what
    makes :attr:`residual_us` a physical quantity rather than a subtraction of
    two conventions.

    ``basis_delay_us`` is the delay the named measurement says would leave the
    drivers coincident.  ``basis_artifacts`` names where that came from, and is
    required: a bound checked against an undeclared basis is arithmetic, not
    provenance.  ``basis_note`` is the human line beside it (a spread, a sample
    count, a method) and is optional — it is what a reader wants and not
    something a validator can meaningfully check, so requiring it would buy
    ceremony rather than trust.

    ``polarity`` is the OPTIONAL basin pin, in the candidate's own vocabulary
    (:data:`~jasper.active_speaker.crossover_alignment.POLARITY_KEEP` /
    ``POLARITY_INVERT``) rather than the measurement frame's ``normal`` /
    ``inverted``, because a prescriber writes what the speaker should DO with
    the region's persisted polarity — the same thing
    :class:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverAlignment`
    means by the word.  Both admit exactly the two ACTIONS and not
    ``POLARITY_REVIEW`` — "surface it, do not auto-decide" is not a basin a round
    can be pinned to — and both read the two names from the one module that
    declares them, with ``tests/test_crossover_v2_alignment_prescription.py``
    pinning the two sets equal.  ``None`` leaves the polarity to the objective
    that owns it.
    """

    delay_us: float
    basis_delay_us: float
    basis_artifacts: tuple[str, ...]
    basis_note: str = ""
    polarity: str | None = None
    #: The corner the bound was evaluated at, and the lobe it produced.  Filled
    #: by :func:`read_alignment_prescription` after the bound passes, so a
    #: receipt states not only the residual but WHAT IT WAS COMPARED AGAINST —
    #: a reader who finds a residual of 155.7 µs on a receipt cannot otherwise
    #: tell whether that cleared a 303 µs lobe or a 270 µs one without knowing
    #: the corner, and the corner is not elsewhere in the block.  ``None`` on a
    #: record that has not been through the bound (the durable read-back of a
    #: pre-#2662 block, or a hand-built one).
    checked_at_fc_hz: float | None = None
    lobe_us: float | None = None

    @property
    def polarity_sign(self) -> int | None:
        """The pin in the MEASUREMENT frame, or ``None`` when unpinned.

        The one place the candidate's action word becomes the sign
        :func:`~jasper.audio_measurement.program_analysis._select_alignment_pair`
        searches over, mirroring
        :func:`~jasper.active_speaker.crossover_v2.planning.alignment_to_candidate_fields`
        on the way back out. Kept here rather than at the hand-down site because
        the module that owns the word owns its translation.
        """
        if self.polarity is None:
            return None
        return -1 if self.polarity == POLARITY_INVERT else 1

    @property
    def residual_us(self) -> float:
        """How far this prescription leaves the drivers from the basis's answer.

        The quantity the bound is expressed in, and the one the series-2
        diagnosis tabulates its candidates by: ``0.0`` prescribes exactly what the
        measurement says, and the sign says which driver is left early.
        """
        return self.delay_us - self.basis_delay_us

    def to_dict(self) -> dict[str, Any]:
        """The receipt's view: what was prescribed, and what justifies it."""
        return {
            "artifact_schema_version": ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
            "kind": ALIGNMENT_PRESCRIPTION_KIND,
            "delay_us": self.delay_us,
            "basis_delay_us": self.basis_delay_us,
            "residual_us": self.residual_us,
            "basis_artifacts": list(self.basis_artifacts),
            "basis_note": self.basis_note,
            "polarity": self.polarity,
            "checked_at_fc_hz": self.checked_at_fc_hz,
            "lobe_us": self.lobe_us,
        }


def _finite_number(value: Any, *, reason: str, field: str) -> float:
    """One numeric field, strictly.

    ``bool`` is refused explicitly because it is an ``int`` in Python and
    ``float(True)`` is ``1.0`` — a delay of "true" microseconds would otherwise
    validate.  Strings are refused for :func:`.blend_correction.
    blend_filters_from_mapping`'s reason: ``float("-450")`` succeeds, so
    accepting one would make the reader's strictness depend on the encoder's
    habits rather than on the contract.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AlignmentPrescriptionRefused(
            reason, f"{field} must be a number, got {type(value).__name__}",
        )
    number = float(value)
    if not math.isfinite(number):
        raise AlignmentPrescriptionRefused(
            reason, f"{field} must be finite, got {number!r}",
        )
    return number


def _parse_prescription(
    raw: Mapping[str, Any], *, read_back: bool = False,
) -> AlignmentPrescription:
    """The shape and the provenance, and NOT the bound.

    Shared whole between the request gate and the durable read-back, so the
    only thing that differs between those two is their gate policy — which is
    the only thing that should.

    ``read_back`` is that one difference for the envelope specifically.
    ``False`` (the request gate, :func:`read_alignment_prescription`) judges a
    freshly-authored document an operator or LLM is about to hand the
    machinery: a missing or wrong ``kind``/``artifact_schema_version`` is a
    malformed document and refuses at the tap, exactly like every other field
    here. ``True`` (the durable read-back,
    :func:`alignment_prescription_from_mapping`, and
    :func:`.durable_state.alignment_prescription_prior_from_state`'s
    ``verify_priors`` rehydration) judges a document THIS repository already
    wrote and persisted — and durable state on a live speaker predates this
    envelope: ``verify_priors.alignment_prescription`` has been carried
    unconditionally across deploys since #2662/#2773, three days before this
    envelope existed. A record naming NEITHER field is exactly the shape this
    build's own prior releases wrote, and refusing it would silently mis-grade
    a round already in flight on real hardware — so under ``read_back`` that
    one shape reads as this build's own kind and version 1, never as a raise.
    A record naming EITHER field, even if the other is missing or wrong, is
    NOT that legacy shape — it tried to speak the envelope and got it wrong —
    and is refused normally under both postures.
    """
    if not isinstance(raw, Mapping):
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_MALFORMED,
            f"a prescription must be a mapping, got {type(raw).__name__}",
        )
    unknown = sorted(set(raw) - _PRESCRIPTION_FIELDS)
    if unknown:
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_MALFORMED,
            f"unknown prescription field(s): {', '.join(unknown)}",
        )
    pre_envelope = (
        read_back and "kind" not in raw and "artifact_schema_version" not in raw
    )
    if not pre_envelope:
        if raw.get("kind") != ALIGNMENT_PRESCRIPTION_KIND:
            raise AlignmentPrescriptionRefused(
                ALIGNMENT_PRESCRIPTION_MALFORMED,
                f"a prescription must name kind={ALIGNMENT_PRESCRIPTION_KIND!r}, "
                f"got {raw.get('kind')!r}",
            )
        version = raw.get("artifact_schema_version")
        if version != ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION:
            raise AlignmentPrescriptionRefused(
                ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED,
                f"this build speaks alignment-prescription schema "
                f"{ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION}, got {version!r}",
            )
    if "delay_us" not in raw:
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_DELAY_INVALID, "a prescription must state delay_us",
        )
    delay_us = _finite_number(
        raw["delay_us"], reason=PRESCRIPTION_DELAY_INVALID, field="delay_us",
    )
    if "basis_delay_us" not in raw:
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_BASIS_INVALID,
            "a prescription must state the basis_delay_us it was derived from",
        )
    basis_delay_us = _finite_number(
        raw["basis_delay_us"],
        reason=PRESCRIPTION_BASIS_INVALID,
        field="basis_delay_us",
    )
    artifacts = _read_artifacts(raw.get("basis_artifacts"))
    note = raw.get("basis_note", "")
    if not isinstance(note, str):
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
            f"basis_note must be text, got {type(note).__name__}",
        )
    return AlignmentPrescription(
        delay_us=delay_us,
        basis_delay_us=basis_delay_us,
        basis_artifacts=artifacts,
        basis_note=note,
        polarity=_read_polarity(raw.get("polarity")),
        checked_at_fc_hz=_optional_number(raw.get("checked_at_fc_hz")),
        lobe_us=_optional_number(raw.get("lobe_us")),
    )


#: The basins a round may be pinned to: the candidate's two polarity ACTIONS.
#: Derived from the module that declares the words rather than restating them,
#: and deliberately without ``POLARITY_REVIEW`` — see
#: :class:`AlignmentPrescription`'s docstring.
_PINNABLE_POLARITIES = frozenset({POLARITY_KEEP, POLARITY_INVERT})


def _read_polarity(value: Any) -> str | None:
    """The optional basin pin, strictly, or ``None`` for the automatic path.

    ``None`` and absent are one answer here, unlike ``delay_us``: a prescription
    that omits the field is not making a claim about polarity, and an explicit
    ``null`` is the same non-claim spelled by a JSON encoder.
    """
    if value is None:
        return None
    # ``isinstance`` before membership, and not merely for tidiness: a JSON list
    # is unhashable, so ``value in frozenset`` would raise TypeError past every
    # refusal handler instead of naming the reason.
    if not isinstance(value, str) or value not in _PINNABLE_POLARITIES:
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_POLARITY_INVALID,
            f"polarity must be one of {sorted(_PINNABLE_POLARITIES)} or absent, "
            f"got {value!r}",
        )
    return value


def _optional_number(value: Any) -> float | None:
    """A finite number, or ``None`` — never a raise.

    These two fields are the GATE's own record of what it checked, not a
    requester's claim, so an unreadable one on the way back in is missing
    context rather than a malformed prescription.  Refusing here would let a
    truncated durable block cost a round its provenance entirely.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def read_alignment_prescription(
    raw: Mapping[str, Any] | None,
    *,
    fc_hz: float,
    declared_bounds_us: tuple[float, float] | None,
) -> AlignmentPrescription | None:
    """THE request gate.  One point, and the one derivation of the lobe bound.

    ``None`` when the request carries no prescription — the automatic path,
    untouched.  Otherwise a validated :class:`AlignmentPrescription`, or
    :class:`AlignmentPrescriptionRefused` naming which gate said no.

    ``declared_bounds_us`` is the PRESET's own unsigned delay-magnitude window,
    already margin-expanded — the caller derives it from the single existing
    owner, ``crossover_v2_flow.alignment_delay_search_bounds_us``, and hands it
    in, because this module may not import the flow.  ``None`` is "the preset
    declares no window", which is that helper's own answer for a preset with no
    ``delay_range_ms`` and means there is nothing to gate on.

    **Required and undefaulted**, unlike anything else here.  This is the only
    bound in the gate that does NOT rest on a number the requester supplied: the
    lobe bound checks a prescription against a basis the same request declared,
    so a prescriber willing to declare a wrong basis passes it.  A caller that
    forgot this argument would lose the hardware's own opinion and never know,
    which is the exact failure a defaulted keyword hides.

    ``fc_hz`` is the crossover corner the bound is a half-period of.  The
    caller passes the corner THIS round runs at — the one the speaker is
    commissioned at, or the pinned one when an operator pinned the topology,
    since ``topology_prescription.apply_topology_pin`` settles that before this
    gate is asked.  One corner per round: nothing re-scores the same
    prescription at a second corner since the corner hunt closed
    (``docs/tuning-master-plan.md`` ticket 2.3).

    Gating here and observing in the aligner is deliberate, and unchanged by
    that closure: an operator's declared basis fails closed at this boundary,
    while the aligner's own ``left_anchor_lobe`` tripwire keeps watching the
    same geometry on the delay actually committed and discloses a commitment
    that left its lobe.  The two answer different questions — may this request
    run, and did the result stay where the request said it would.

    **The bound is inclusive.**  A prescription exactly half a period from its
    basis is legal; one past it is refused.  Exactness is legal in this
    repository's gates, and a strict comparison here would make the legality of
    a round depend on floating-point noise in the sixth decimal of a corner
    frequency.
    """
    if raw is None:
        return None
    prescription = _parse_prescription(raw)
    # Checked after the shape and before the bound: an unusable corner is a
    # different problem from an out-of-lobe prescription, and reporting the
    # second would send an operator to re-derive a number that was fine.
    if not isinstance(fc_hz, (int, float)) or isinstance(fc_hz, bool):
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_FC_UNKNOWN,
            f"the crossover corner must be a number, got {type(fc_hz).__name__}",
        )
    corner = float(fc_hz)
    if not math.isfinite(corner) or corner <= 0.0:
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_FC_UNKNOWN,
            "a prescription's bound is a half-period at the crossover corner, "
            f"which is undefined at fc_hz={corner!r}",
        )
    # The HARDWARE's bound first, then the measurement's. A prescription the
    # preset could never emit is refused for that, not for a lobe it also
    # happens to miss: the two send an operator to different places (re-declare
    # the region vs re-derive the basis).
    if declared_bounds_us is not None:
        lo_us, hi_us = (abs(float(b)) for b in declared_bounds_us)
        lo_us, hi_us = min(lo_us, hi_us), max(lo_us, hi_us)
        magnitude_us = abs(prescription.delay_us)
        if not (lo_us <= magnitude_us <= hi_us):
            raise AlignmentPrescriptionRefused(
                PRESCRIPTION_OUTSIDE_DECLARED_WINDOW,
                f"{prescription.delay_us:.1f} us is {magnitude_us:.1f} us of "
                f"delay, outside the preset's declared window of "
                f"{lo_us:.1f}-{hi_us:.1f} us",
            )
    lobe_us = half_period_us(corner)
    if abs(prescription.residual_us) > lobe_us:
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_OUT_OF_LOBE,
            f"{prescription.delay_us:.1f} us leaves "
            f"{prescription.residual_us:.1f} us of residual against the "
            f"declared basis {prescription.basis_delay_us:.1f} us, outside the "
            f"+/-{lobe_us:.1f} us half-period lobe at {corner:.1f} Hz",
        )
    # What the bound was actually evaluated against, recorded on the record the
    # receipt banks. A residual alone does not say which lobe cleared it.
    return replace(prescription, checked_at_fc_hz=corner, lobe_us=lobe_us)


def alignment_prescription_from_mapping(
    raw: Any,
) -> AlignmentPrescription | None:
    """A prescription read back out of this repository's own durable state.

    The read-back half of the pair this module's docstring describes: the same
    shape and provenance checks, deliberately NOT the bound, and ``None``
    instead of a raise.

    Why no bound.  The only mappings that reach here were written by
    :func:`read_alignment_prescription` accepting them, so re-applying the
    bound could not catch a prescription the boundary let through — it could
    only refuse one whose corner moved between the stage that measured the
    round and the stage that grades it, and refusing there would discard the
    evidence of a round that really ran.  The bound has one owner, and it is
    the boundary.

    Why still strict about shape.  A hand-edited or truncated state file is a
    real input here, and a receipt that banked half a prescription would be
    claiming provenance it does not have.  Anything unreadable is ``None`` plus
    one WARNING, so an empty provenance slot on a receipt is always
    distinguishable from a silently mangled one.

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
    except AlignmentPrescriptionRefused as exc:
        log_event(
            logger,
            PRESCRIPTION_UNREADABLE_EVENT,
            level=logging.WARNING,
            reason=exc.reason,
            detail=exc.detail,
        )
        return None


def _read_artifacts(value: Any) -> tuple[str, ...]:
    """The named provenance, strictly and non-empty.

    A bare string is refused rather than wrapped: ``"a,b"`` and ``["a", "b"]``
    would otherwise be one artifact and two, decided by punctuation.
    """
    if value is None:
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
            "a prescription must name the basis_artifacts it was measured from",
        )
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
            "basis_artifacts must be a list of names, got "
            f"{type(value).__name__}",
        )
    artifacts: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise AlignmentPrescriptionRefused(
                ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
                "every basis_artifacts entry must be a non-blank name",
            )
        artifacts.append(entry.strip())
    if not artifacts:
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
            "a prescription must name at least one basis artifact",
        )
    return tuple(artifacts)


def alignment_prescription_response_format() -> dict[str, Any]:
    """What a prescriber must send to pin the inter-driver delay, and where to
    send it.

    The fourth of the evidence packet's contract blocks, beside
    :func:`~.blend_prescription.prescription_response_format`,
    :func:`~.driver_prescription.driver_prescription_response_format`, and
    :func:`~.topology_prescription.topology_prescription_response_format` —
    the same #2773 reason topology's own docstring gives: this and the
    topology pin enter as REQUEST-BODY KEYS on the session-open call rather
    than through the prescriber CLI's stage step, and a reader who found only
    the two staged contracts would never learn the request-time doors exist.
    """
    return {
        "key": ALIGNMENT_PRESCRIPTION_KEY,
        "entry": "request_body",
        "entry_detail": (
            "sent as the '" + ALIGNMENT_PRESCRIPTION_KEY + "' key on "
            "POST /crossover/v2/session, not staged through "
            "jasper-crossover-prescriber"
        ),
        "severity": (
            "a refused prescription refuses the whole session at the tap; it "
            "is never clamped to the nearest legal delay and never partially "
            "applied"
        ),
        "fields": {
            "kind": f"required, must be exactly {ALIGNMENT_PRESCRIPTION_KIND!r}",
            "artifact_schema_version": (
                "required, must be exactly "
                f"{ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION}"
            ),
            "delay_us": (
                "required number, signed (D_woofer - D_tweeter): positive "
                "delays the tweeter, negative delays the woofer"
            ),
            "basis_delay_us": (
                "required number, the delay the named measurement says would "
                "leave the drivers coincident"
            ),
            "basis_artifacts": (
                "required non-empty list of names — what this delay was "
                "measured from"
            ),
            "basis_note": "optional human line beside the artifacts",
            "polarity": (
                "optional, one of "
                + ", ".join(sorted(_PINNABLE_POLARITIES))
                + " — pins the basin the automatic objective would otherwise "
                "solve; absent leaves it to the objective"
            ),
        },
        "bound": (
            "delay_us may not leave the drivers more than one half-period at "
            "the crossover corner away from basis_delay_us — the comb lobe, "
            "checked at the tap against the corner this round is measured at"
        ),
        "refusals": sorted(ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS),
    }
