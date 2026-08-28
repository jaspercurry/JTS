# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Immutable domain contracts for the crossover-v2 intervention loop (#2291).

**What this module is for.** The v2 conductor owned measurement context,
candidate context, prescription policy, verification semantics, and lifecycle
in one mutable object.  On 2026-08-10 that produced a candidate whose
crossover sections said 1,648.7 Hz while its trim/level arithmetic still read
the session's configured 2,000 Hz, and a trim recorded as ``trim_rejected``
that was nevertheless committed.  Both are single-source-of-truth failures, and
both are addressed here by making the honest shape a *type* rather than a
convention.

**Dependency direction is one-way and load-bearing.** This module imports no
``jasper.web`` and nothing from
:mod:`jasper.active_speaker.crossover_v2_flow`; the flow imports these
contracts, never the reverse.  That is the ``docs/extensibility.md``
host-mediated-indirection invariant applied to a migration: the domain value
cannot reach back into the host that produced it.

**Not to be confused with**
:mod:`jasper.active_speaker.crossover_contract`, which owns a different
question — whether an *already applied* graph matches its declaration
(tuning ownership, snapshot readiness, apply-transaction preconditions).  This
module owns what a *proposed* intervention is.

**Fingerprint envelope.** Every fingerprinted value here follows the shape the
repository already uses in
:mod:`jasper.audio_measurement.evidence_identity` — a ``_core()`` payload
carrying ``schema_version`` and ``kind``, hashed by that module's canonical
``json_fingerprint``.  There is deliberately no second hashing implementation:
one canonicalizer, one digest domain.

Phase 1 of #2291 introduced these types and wired
:class:`InterventionProposal` alongside the planner; Phase 2b moved the
prescription policy itself out of the conductor into
:mod:`.intervention`, which builds a :class:`CandidateAcousticContext` for
every candidate.  These types were defined and validated ahead of their
producers — that is the point of contracts-first — and the phases that owed
them have landed: 3b for the verification/adoption vocabulary, 3c for the
round receipt.  Nothing here is inert.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from jasper.audio_measurement.evidence_identity import json_fingerprint

from ..branch_chain import CrossoverSection

__all__ = [
    "ADOPTION_ROWS",
    "ADOPTION_ROW_KEEP",
    "ADOPTION_ROW_KEEP_FOR_ITERATION",
    "ADOPTION_ROW_KEEP_ITERATING",
    "ADOPTION_ROW_KEEP_MISSED_EXHAUSTED",
    "ADOPTION_ROW_RESTORE_FAILED",
    "ADOPTION_ROW_RESTORE_REGRESSION",
    "ADOPTION_ROW_RESTORE_UNSAFE",
    "ADOPTION_ROW_RESTORE_UNTRUSTED",
    "ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED",
    "AdoptionDecision",
    "AdoptionOutcome",
    "BenefitStatus",
    "CandidateAcousticContext",
    "CandidateFcDisagreementError",
    "CaptureValidity",
    "CrossoverV2ContractError",
    "CrossoverV2FlowError",
    "DEFAULT_CLOUD_MEASURE_POSITIONS",
    "DESIGN_AXIS_DEG",
    "EvidenceTrust",
    "InterventionProposal",
    "IterationHeadroom",
    "MEASURE_KINDS",
    "MEASURE_KIND_BASELINE",
    "MEASURE_KIND_CANDIDATE",
    "MEASURE_KIND_VERIFY",
    "MEASURE_REGIMES",
    "NoCrossoverSectionsError",
    "PLAN_REFUSAL_REASONS",
    "POLARITIES",
    "POLARITY_INVERTED",
    "POLARITY_NORMAL",
    "POSITION_AXES",
    "POSITION_AXIS_HORIZONTAL",
    "POSITION_AXIS_VERTICAL",
    "PROPOSAL_FINGERPRINT_KINDS",
    "PlanRefusal",
    "QualityStatus",
    "REFERENCE_MARK_DESIGN_AXIS",
    "REGIME_NEAR_FIELD",
    "REGIME_REFERENCE_AXIS",
    "ROUND_RECEIPT_KIND",
    "RealizationStatus",
    "ResponseCurve",
    "RoundReceipt",
    "SafetyStatus",
    "SpecStatus",
    "TrimStrategy",
    "VERIFY_TOLERANCE_DB",
    "VerificationResult",
    "detached_json",
]

#: Bumped to 2 by decision 10 (#2600), which added the ``blend`` key to
#: ``RoundReceipt.round_measurements``. The bump is what the receipt key-set
#: guard's own remedy asks for: the version sat at 1 through three field
#: additions in one week, so a reader could not tell two shapes apart by it,
#: and the guard exists so that stops being true silently. A reader that
#: branches on this should treat 1 as "no blend record, ever" rather than as
#: "the blend record is absent for this round" — the two are different facts
#: and only the version can separate them.
SCHEMA_VERSION = 2

#: What a banked round receipt calls itself — the discriminator a store routes
#: on, so it is named here beside the type that emits it rather than spelled
#: once in :meth:`RoundReceipt._core` and again at every writer.
ROUND_RECEIPT_KIND = "jts_crossover_v2_round_receipt"


class CrossoverV2FlowError(RuntimeError):
    """The v2 session could not form a safe phase transition.

    Here rather than in the flow because two modules raise it and neither may
    import the other: :mod:`.capture_plan` refuses a plan that cannot fit, and
    the flow refuses a transition. ``angle_capture_spool.AngleRequestRefused``
    subclasses it, which is what lets one ``except`` clause cover both.
    """


class CrossoverV2ContractError(ValueError):
    """A crossover-v2 contract value is malformed, ambiguous, or inconsistent.

    Carries the :data:`PLAN_REFUSAL_REASONS` member a raise means, so a caller
    turning one into a :class:`PlanRefusal` reads an attribute set at the raise
    site instead of parsing the message. The reason is part of the contract;
    the message is for an operator and may be reworded freely.
    """

    #: Overridden by the subclasses below. The generic default is honest: an
    #: error that has not classified itself is exactly "some contract value is
    #: invalid".
    refusal_reason = "contract_invalid"


class NoCrossoverSectionsError(CrossoverV2ContractError):
    """A candidate context was asked for from no crossover sections at all."""

    refusal_reason = "no_crossover_sections"


class CandidateFcDisagreementError(CrossoverV2ContractError):
    """Sections in one candidate context name more than one crossover corner.

    The 2026-08-10 defect's shape, refused at construction.
    """

    refusal_reason = "candidate_fc_disagreement"


# --------------------------------------------------------------------------
# small validators
# --------------------------------------------------------------------------


def _finite(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CrossoverV2ContractError(f"{field_name} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise CrossoverV2ContractError(f"{field_name} must be finite")
    return number


def _positive(value: Any, *, field_name: str) -> float:
    number = _finite(value, field_name=field_name)
    if number <= 0.0:
        raise CrossoverV2ContractError(f"{field_name} must be positive")
    return number


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CrossoverV2ContractError(
            f"{field_name} must be a non-empty trimmed string"
        )
    return value


def detached_json(value: Any) -> Any:
    """One JSON-shaped value with no container shared with the caller.

    Recursive on purpose. A shallow ``dict(value)`` detaches only the top
    level, so a caller holding the *nested* dict it passed in could still
    mutate a "frozen" proposal after construction — and the fingerprint, taken
    once at construction, would no longer describe the object a reader sees.
    That divergence is the exact failure mode these contracts exist to prevent,
    one level down (#2307 gate note N1).

    Leaves are returned as they are: this normalizes *containers*, not values,
    so a numpy scalar or a small dataclass passes through untouched.

    A copied list stays a ``list`` rather than becoming a tuple, and that is a
    requirement rather than a preference: the shared fingerprinter's
    ``_freeze_json`` admits ``type(value) is list`` exactly and refuses a tuple
    as a non-JSON value. Detaching therefore changes no type the digest sees,
    so no existing fingerprint moves. The copy is fresh, which is the whole
    property — the caller holds no reference to any container inside the
    result, so nothing it does afterwards can reach in.
    """

    if isinstance(value, Mapping):
        return {key: detached_json(item) for key, item in value.items()}
    if isinstance(value, (str, bytes, bytearray)):
        return value
    if isinstance(value, Sequence):
        return [detached_json(item) for item in value]
    return value


def _json_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    """A defensive DEEP copy of one JSON-shaped mapping, or ``{}`` for ``None``.

    The copy matters, and its depth matters: a frozen dataclass holding a
    caller's live dict — at any level — is immutable in name only. See
    :func:`detached_json`.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CrossoverV2ContractError(f"{field_name} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise CrossoverV2ContractError(f"{field_name} keys must be strings")
    return {key: detached_json(item) for key, item in value.items()}


def _trim_map(value: Any, *, field_name: str) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CrossoverV2ContractError(f"{field_name} must be a mapping")
    return {
        _text(role, field_name=f"{field_name} role"): _finite(
            level, field_name=f"{field_name}[{role}]"
        )
        for role, level in value.items()
    }


# --------------------------------------------------------------------------
# response curves
# --------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class ResponseCurve:
    """One magnitude response as exact, finite, fingerprintable points.

    The planner's curves arrive as a ``(freqs_hz, db)`` pair of numpy arrays.
    They are normalized to plain float tuples here for three reasons: a numpy
    array is mutable (so a frozen dataclass holding one is not really frozen),
    it is not JSON-canonicalizable by the shared fingerprinter, and a non-finite
    bin must be refused rather than silently hashed.

    Values are stored exactly — no rounding.  Rounding would be a precision
    *policy*, and this module owns no policy; a fingerprint that changes when a
    committed number changes is the whole contract.
    """

    hz: tuple[float, ...]
    db: tuple[float, ...]

    def __init__(self, hz: Iterable[Any], db: Iterable[Any]) -> None:
        frequencies = tuple(_finite(value, field_name="curve hz") for value in hz)
        levels = tuple(_finite(value, field_name="curve db") for value in db)
        if not frequencies:
            raise CrossoverV2ContractError("a response curve must have points")
        if len(frequencies) != len(levels):
            raise CrossoverV2ContractError(
                "a response curve needs one level per frequency"
            )
        object.__setattr__(self, "hz", frequencies)
        object.__setattr__(self, "db", levels)

    @classmethod
    def from_pair(cls, pair: Any, *, field_name: str) -> "ResponseCurve | None":
        """Normalize the session's ``(freqs, db)`` pair; ``None`` passes through."""

        if pair is None:
            return None
        if isinstance(pair, ResponseCurve):
            return pair
        try:
            frequencies, levels = pair
        except (TypeError, ValueError) as exc:
            raise CrossoverV2ContractError(
                f"{field_name} must be a (freqs_hz, db) pair"
            ) from exc
        return cls(frequencies, levels)

    def to_json(self) -> dict[str, Any]:
        return {"hz": list(self.hz), "db": list(self.db)}


def _curve_json(curve: "ResponseCurve | None") -> Any:
    return None if curve is None else curve.to_json()


# --------------------------------------------------------------------------
# candidate acoustic context — the one Fc owner
# --------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class CandidateAcousticContext:
    """One candidate preset's crossover corner and the sections that realize it.

    **This type exists to make the 2026-08-10 dual-Fc defect impossible.** The
    planner used to receive candidate sections cornered at 1,648.7 Hz while
    continuing to read the session's configured ``self._fc_hz`` (2,000 Hz) for
    the overlap band, the straddle decision, the ripple trim solve, and the
    realized branch-level match.  A context owns the corner *and* the sections
    together, so there is nothing else to consult: a planner holding one of
    these cannot ask a second question about which crossover it is planning.

    Agreement is checked at construction and is **exact**, not toleranced.
    :data:`jasper.active_speaker._common.REGION_FC_MATCH_TOLERANCE_HZ` exists for
    a different question — comparing a corner that has round-tripped through
    persisted JSON against a declaration — and its own comment says it "must
    never bridge a real crossover setting change".  These sections are built
    in-process from a single float (``dataclasses.replace(section,
    fc_hz=float(fc_hz))``), so any inequality at all is a real disagreement.

    A role with no sections is legitimate and preserved: per
    :func:`jasper.active_speaker.branch_chain.sections_by_role`, a driver with
    no crossover region runs full range in the emitted graph.  The invariant is
    that every section which *exists* names this context's corner — not that
    every role has one.  At least one section must exist overall, because a
    context with no crossover anywhere describes no crossover.
    """

    fc_hz: float
    _sections: tuple[tuple[str, tuple[CrossoverSection, ...]], ...] = field(repr=False)
    fingerprint: str = field(init=False)

    def __init__(
        self,
        *,
        fc_hz: float,
        sections_by_role: Mapping[str, Sequence[CrossoverSection]],
    ) -> None:
        corner = _positive(fc_hz, field_name="fc_hz")
        if not isinstance(sections_by_role, Mapping):
            raise CrossoverV2ContractError("sections_by_role must be a mapping")
        normalized: list[tuple[str, tuple[CrossoverSection, ...]]] = []
        total = 0
        for role, sections in sections_by_role.items():
            name = _text(role, field_name="section role")
            if isinstance(sections, (str, bytes)) or not isinstance(
                sections, Sequence
            ):
                raise CrossoverV2ContractError(
                    f"sections for role {name!r} must be a sequence"
                )
            frozen: list[CrossoverSection] = []
            for section in sections:
                if not isinstance(section, CrossoverSection):
                    raise CrossoverV2ContractError(
                        f"sections for role {name!r} must be CrossoverSection values"
                    )
                # The invariant, in both directions: a section cornered
                # anywhere other than this context's Fc is the mixed-Fc defect,
                # and it fails closed rather than being silently re-cornered.
                if float(section.fc_hz) != corner:
                    raise CandidateFcDisagreementError(
                        "candidate section Fc "
                        f"{float(section.fc_hz)!r} Hz disagrees with candidate "
                        f"context Fc {corner!r} Hz for role {name!r}"
                    )
                frozen.append(section)
                total += 1
            normalized.append((name, tuple(frozen)))
        if not normalized:
            raise NoCrossoverSectionsError("sections_by_role must not be empty")
        if total == 0:
            raise NoCrossoverSectionsError(
                "a candidate acoustic context needs at least one crossover section"
            )
        object.__setattr__(self, "fc_hz", corner)
        object.__setattr__(self, "_sections", tuple(sorted(normalized)))
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    @classmethod
    def from_sections(
        cls, sections_by_role: Mapping[str, Sequence[CrossoverSection]]
    ) -> "CandidateAcousticContext":
        """Derive the corner from the sections themselves — the safest entry.

        A caller that already holds one candidate's sections has no reason to
        also carry an Fc, and carrying one is exactly how a session corner
        reaches candidate planning.  The sections must be unanimous; a split
        set has no single corner and fails closed.
        """

        corners = {
            float(section.fc_hz)
            for sections in (sections_by_role or {}).values()
            for section in sections
        }
        if not corners:
            raise NoCrossoverSectionsError(
                "a candidate acoustic context needs at least one crossover section"
            )
        if len(corners) != 1:
            raise CandidateFcDisagreementError(
                f"candidate sections name {len(corners)} different crossover "
                f"corners: {sorted(corners)!r}"
            )
        return cls(fc_hz=corners.pop(), sections_by_role=sections_by_role)

    @property
    def sections_by_role(self) -> dict[str, tuple[CrossoverSection, ...]]:
        """A fresh mapping each call — the stored value stays immutable."""

        return {role: sections for role, sections in self._sections}

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(role for role, _ in self._sections)

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_crossover_v2_candidate_acoustic_context",
            "fc_hz": self.fc_hz,
            "sections_by_role": {
                role: [
                    {
                        "fc_hz": float(section.fc_hz),
                        "order": int(section.order),
                        "highpass": bool(section.highpass),
                    }
                    for section in sections
                ]
                for role, sections in self._sections
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


# --------------------------------------------------------------------------
# trim strategy — a name may never say "rejected" while meaning "committed"
# --------------------------------------------------------------------------


class TrimStrategy(str, Enum):
    """Which inter-driver trim pair the planner committed, and why.

    **The vocabulary this replaces was actively misleading.** The retired
    fitter recorded ``linearization_outcome =
    "trim_rejected" if wild else "fitted"``, where ``wild`` meant only that the
    ripple scan drifted more than
    ``LINEARIZATION_TRIM_SANITY_MARGIN_DB`` (6 dB) from the level-preserving
    anchor.  Which pair was *committed* was decided separately and immediately
    afterwards, by whichever pair realized the better inter-driver level.  On
    2026-08-10 those two facts pointed opposite ways: the artifact said
    ``trim_rejected`` and the −13.013 dB resolved trim was committed anyway.
    The word "rejected" described a drift observation, not an outcome.

    Every member below therefore names **what was committed**.  Drift is a
    qualifier on a commitment, never a substitute for one.

    Reachability.  The planner returns the winning pair as data since #2291
    Phase 2b, so a LIVE plan carries one of the precise members;
    :attr:`RESOLVED_COMMITTED_AFTER_SANITY_DRIFT` is the one exception and is
    unreachable from the live path (see its own doc).

    :attr:`COMMITTED_PAIR_UNRECORDED` is the *artifact-derived* member: the
    proposal assembled at the commit seam
    (:func:`~.proposal.trim_strategy_for_outcome`, restored by #2392) reads the
    ``linearization_outcome`` string the build stamped onto the candidate,
    because that is the one trim fact the committing walk certainly holds — see
    that function for why it holds no other — and the string does not record
    which pair won the realized-level grading.  It states that evidence gap
    rather than guessing a precise member.

    It had a second, drift-qualified sibling until #2392 — the issue #2291
    Phase 5c-iii left the question to — and that member is deleted rather than
    restored, because the drift case turned out not to need one:
    :attr:`~.intervention.TrimDecision.outcome` is ``"trim_rejected"`` if and
    only if :attr:`~.intervention.TrimDecision.beyond_sanity_margin`, and
    :func:`~.intervention.decide_trim` commits the anchor on exactly that
    branch, so the string determines
    :attr:`ANCHORED_COMMITTED_AFTER_SANITY_DRIFT` precisely.  An "unrecorded"
    name for a fact the artifact does record would understate the evidence.
    ``test_the_unrecorded_drift_member_is_gone_and_referenced_nowhere`` keeps
    the name itself out of the tree, which is why it is described here rather
    than spelled.
    """

    NOT_FITTED = "not_fitted"
    """No linearization fit produced a trim pair (ineligible, or the fit failed)."""

    ANCHORED_COMMITTED = "anchored_committed"
    """The level-preserving anchored trim was committed; the scan stayed in margin."""

    RESOLVED_COMMITTED = "resolved_committed"
    """The ripple-scan trim was committed; the scan stayed in margin."""

    ANCHORED_COMMITTED_AFTER_SANITY_DRIFT = "anchored_committed_after_sanity_drift"
    """The scan drifted beyond the margin and the anchor was committed instead.

    This is the only genuine fallback, and the only case an older reader would
    have been right to call a rejection.
    """

    RESOLVED_COMMITTED_AFTER_SANITY_DRIFT = "resolved_committed_after_sanity_drift"
    """The scan drifted beyond the margin and was committed anyway.

    **Historical.** This is the 2026-08-10 jts3 case, and it was legal under
    the level-graded policy that shipped it — a drifted scan committed whenever
    it levelled better, while the candidate recorded
    ``linearization_outcome="trim_rejected"``.  #2291 Phase 2b deleted that
    policy: the planner commits the anchor beyond the margin, so nothing in the
    live path can produce this member any more.

    It is RETAINED, not removed, because it is the honest name for what
    already-persisted artifacts describe — a reader classifying the incident's
    own candidate needs a member that says what happened.  Whatever else is
    true of it, it must never be recorded under a name containing "rejected".
    """

    COMMITTED_PAIR_UNRECORDED = "committed_pair_unrecorded"
    """A trim pair was committed, in margin; the artifact does not say which."""


# --------------------------------------------------------------------------
# the proposal
# --------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class InterventionProposal:
    """One complete, fingerprinted prescription: everything committed, together.

    The field list is #2291's, and the fields that are empty today are empty
    *honestly* rather than absent — an intervention whose anchored/alternative
    trim evidence or pre-apply predicted spec cannot be stated is exactly the
    thing the incident review could not reconstruct, and a contract that
    quietly omits them would hide the same gap again.  Each such field names
    the phase that fills it.

    :attr:`fingerprint` covers every committed value below, so any change to
    the candidate, the crossover corner, a section, a trim, a filter, a
    predicted curve, a spec report, or an evidence identity produces a
    different digest.
    """

    candidate: Any
    """The complete ``MeasuredCrossoverCandidate`` this proposal would apply."""

    context: CandidateAcousticContext
    """The one Fc owner: candidate corner and sections, agreeing by construction."""

    evidence_identities: Mapping[str, Any]
    """Source evidence identities (session, program, candidate fingerprint, …)."""

    predicted_response_before: "ResponseCurve | None"
    predicted_response_after: "ResponseCurve | None"
    predicted_spec_before: Mapping[str, Any]
    """Pre-apply predicted spec.  Empty until #2291 Phase 3 adds ``entry_baseline``."""

    predicted_spec_after: Mapping[str, Any]
    commanded_delta: "ResponseCurve | None"
    trim_strategy: TrimStrategy
    trim_rationale: str
    anchored_trim_db: "Mapping[str, float] | None"
    """The level-preserving anchor.  ``None`` until Phase 2 returns it as data."""

    alternative_trim_db: "Mapping[str, float] | None"
    """The ripple-scan alternative.  ``None`` until Phase 2 returns it as data."""

    realized_branch_level: Mapping[str, Any]
    """Realized inter-driver level evidence for the committed pair, when known."""

    linearization_filters: Mapping[str, Any]
    excluded_regions: Mapping[str, Any]
    accountability: Mapping[str, Any]
    """Headroom / accountability result — the level-frame finding, when present."""

    diagnostic_findings: tuple[Mapping[str, Any], ...]
    fingerprint: str = field(init=False)

    def __init__(
        self,
        *,
        candidate: Any,
        context: CandidateAcousticContext,
        evidence_identities: Mapping[str, Any] | None = None,
        predicted_response_before: Any = None,
        predicted_response_after: Any = None,
        predicted_spec_before: Mapping[str, Any] | None = None,
        predicted_spec_after: Mapping[str, Any] | None = None,
        commanded_delta: Any = None,
        trim_strategy: TrimStrategy = TrimStrategy.NOT_FITTED,
        trim_rationale: str = "",
        anchored_trim_db: Mapping[str, float] | None = None,
        alternative_trim_db: Mapping[str, float] | None = None,
        realized_branch_level: Mapping[str, Any] | None = None,
        linearization_filters: Mapping[str, Any] | None = None,
        excluded_regions: Mapping[str, Any] | None = None,
        accountability: Mapping[str, Any] | None = None,
        diagnostic_findings: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        if candidate is None:
            raise CrossoverV2ContractError("a proposal needs a measured candidate")
        if not isinstance(context, CandidateAcousticContext):
            raise CrossoverV2ContractError(
                "context must be a CandidateAcousticContext"
            )
        if not isinstance(trim_strategy, TrimStrategy):
            raise CrossoverV2ContractError("trim_strategy must be a TrimStrategy")
        if not isinstance(trim_rationale, str):
            raise CrossoverV2ContractError("trim_rationale must be a string")
        findings = tuple(
            _json_mapping(item, field_name="diagnostic finding")
            for item in (diagnostic_findings or ())
        )
        object.__setattr__(self, "candidate", candidate)
        object.__setattr__(self, "context", context)
        object.__setattr__(
            self,
            "evidence_identities",
            _json_mapping(evidence_identities, field_name="evidence_identities"),
        )
        object.__setattr__(
            self,
            "predicted_response_before",
            ResponseCurve.from_pair(
                predicted_response_before, field_name="predicted_response_before"
            ),
        )
        object.__setattr__(
            self,
            "predicted_response_after",
            ResponseCurve.from_pair(
                predicted_response_after, field_name="predicted_response_after"
            ),
        )
        object.__setattr__(
            self,
            "predicted_spec_before",
            _json_mapping(predicted_spec_before, field_name="predicted_spec_before"),
        )
        object.__setattr__(
            self,
            "predicted_spec_after",
            _json_mapping(predicted_spec_after, field_name="predicted_spec_after"),
        )
        object.__setattr__(
            self,
            "commanded_delta",
            ResponseCurve.from_pair(commanded_delta, field_name="commanded_delta"),
        )
        object.__setattr__(self, "trim_strategy", trim_strategy)
        object.__setattr__(self, "trim_rationale", trim_rationale)
        object.__setattr__(
            self,
            "anchored_trim_db",
            _trim_map(anchored_trim_db, field_name="anchored_trim_db"),
        )
        object.__setattr__(
            self,
            "alternative_trim_db",
            _trim_map(alternative_trim_db, field_name="alternative_trim_db"),
        )
        object.__setattr__(
            self,
            "realized_branch_level",
            _json_mapping(realized_branch_level, field_name="realized_branch_level"),
        )
        object.__setattr__(
            self,
            "linearization_filters",
            _json_mapping(linearization_filters, field_name="linearization_filters"),
        )
        object.__setattr__(
            self,
            "excluded_regions",
            _json_mapping(excluded_regions, field_name="excluded_regions"),
        )
        object.__setattr__(
            self,
            "accountability",
            _json_mapping(accountability, field_name="accountability"),
        )
        object.__setattr__(self, "diagnostic_findings", findings)
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    @property
    def fc_hz(self) -> float:
        """The candidate corner — read from the context, never from a session."""

        return self.context.fc_hz

    @property
    def candidate_fingerprint(self) -> str:
        return str(getattr(self.candidate, "fingerprint", "") or "")

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_crossover_v2_intervention_proposal",
            "candidate_fingerprint": self.candidate_fingerprint,
            "context": self.context.to_dict(),
            "evidence_identities": dict(self.evidence_identities),
            "predicted_response_before": _curve_json(self.predicted_response_before),
            "predicted_response_after": _curve_json(self.predicted_response_after),
            "predicted_spec_before": dict(self.predicted_spec_before),
            "predicted_spec_after": dict(self.predicted_spec_after),
            "commanded_delta": _curve_json(self.commanded_delta),
            "trim_strategy": self.trim_strategy.value,
            "trim_rationale": self.trim_rationale,
            "anchored_trim_db": (
                None if self.anchored_trim_db is None else dict(self.anchored_trim_db)
            ),
            "alternative_trim_db": (
                None
                if self.alternative_trim_db is None
                else dict(self.alternative_trim_db)
            ),
            "realized_branch_level": dict(self.realized_branch_level),
            "linearization_filters": dict(self.linearization_filters),
            "excluded_regions": dict(self.excluded_regions),
            "accountability": dict(self.accountability),
            "diagnostic_findings": [dict(item) for item in self.diagnostic_findings],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


# --------------------------------------------------------------------------
# refusal
# --------------------------------------------------------------------------


PLAN_REFUSAL_REASONS = frozenset(
    {
        "no_candidate",
        "no_crossover_sections",
        "candidate_fc_disagreement",
        "contract_invalid",
    }
)


@dataclass(frozen=True, init=False)
class PlanRefusal:
    """The planner declined to produce a proposal, by a named reason.

    A refusal is a *result*, never an exception escaping into the caller's
    path, so the reason survives into logs and (from Phase 4) the household
    surface.  ``reason`` is drawn from the closed
    :data:`PLAN_REFUSAL_REASONS` set; ``detail`` is free text for an operator.
    """

    reason: str
    detail: str
    fc_hz: "float | None"

    def __init__(
        self, *, reason: str, detail: str = "", fc_hz: float | None = None
    ) -> None:
        name = _text(reason, field_name="refusal reason")
        if name not in PLAN_REFUSAL_REASONS:
            raise CrossoverV2ContractError(f"unknown plan refusal reason {name!r}")
        if not isinstance(detail, str):
            raise CrossoverV2ContractError("refusal detail must be a string")
        object.__setattr__(self, "reason", name)
        object.__setattr__(self, "detail", detail)
        object.__setattr__(
            self,
            "fc_hz",
            None if fc_hz is None else _positive(fc_hz, field_name="fc_hz"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_crossover_v2_plan_refusal",
            "reason": self.reason,
            "detail": self.detail,
            "fc_hz": self.fc_hz,
        }


# --------------------------------------------------------------------------
# verification and adoption — #2291 Phase 3 vocabulary, defined here first
# --------------------------------------------------------------------------


class CaptureValidity(str, Enum):
    """Was the post-apply capture usable at all?"""

    USABLE = "usable"
    UNUSABLE = "unusable"


class RealizationStatus(str, Enum):
    """Did the applied graph produce the change we commanded?"""

    MATCHED = "matched"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class BenefitStatus(str, Enum):
    """Did the measured speaker actually get better?"""

    IMPROVED = "improved"
    REGRESSED = "regressed"
    INDETERMINATE = "indeterminate"


class SpecStatus(str, Enum):
    """Is the resulting speaker inside the target envelope?"""

    PASSED = "passed"
    FAILED = "failed"
    UNEVALUABLE = "unevaluable"


@dataclass(frozen=True, init=False)
class VerificationResult:
    """Four independent verification answers, never collapsed into one verdict.

    Keeping these separate is the whole point (#1868): on 2026-08-10 VERIFY
    tracked the model to within 1.291 dB while the absolute crossover check
    failed by +5.456 dB, and the run still read as passed.  A realization pass
    must not be able to overwrite a benefit regression or a spec failure, and
    "we do not know" must have somewhere to live — hence
    :attr:`BenefitStatus.INDETERMINATE` and
    :attr:`SpecStatus.UNEVALUABLE` rather than a default of success.

    Consumed since #2291 Phase 3b, which computes the four statuses
    independently and feeds them to :class:`AdoptionDecision`; Phase 1 fixed
    the vocabulary and the invariants this rests on.
    """

    capture_validity: CaptureValidity
    realization: RealizationStatus
    benefit: BenefitStatus
    spec: SpecStatus
    reason: str

    def __init__(
        self,
        *,
        capture_validity: CaptureValidity,
        realization: RealizationStatus,
        benefit: BenefitStatus,
        spec: SpecStatus,
        reason: str = "",
    ) -> None:
        for name, value, kind in (
            ("capture_validity", capture_validity, CaptureValidity),
            ("realization", realization, RealizationStatus),
            ("benefit", benefit, BenefitStatus),
            ("spec", spec, SpecStatus),
        ):
            if not isinstance(value, kind):
                raise CrossoverV2ContractError(f"{name} must be a {kind.__name__}")
        if not isinstance(reason, str):
            raise CrossoverV2ContractError("reason must be a string")
        # An unusable capture cannot have produced a graded answer: claiming
        # one would be the "passed because the model matched" failure wearing a
        # different hat.
        if capture_validity is CaptureValidity.UNUSABLE:
            if realization is not RealizationStatus.UNAVAILABLE:
                raise CrossoverV2ContractError(
                    "an unusable capture cannot report a realization verdict"
                )
            if benefit is not BenefitStatus.INDETERMINATE:
                raise CrossoverV2ContractError(
                    "an unusable capture cannot report a benefit verdict"
                )
            if spec is not SpecStatus.UNEVALUABLE:
                raise CrossoverV2ContractError(
                    "an unusable capture cannot report a spec verdict"
                )
            if not reason:
                raise CrossoverV2ContractError(
                    "an unusable capture must state a reason"
                )
        object.__setattr__(self, "capture_validity", capture_validity)
        object.__setattr__(self, "realization", realization)
        object.__setattr__(self, "benefit", benefit)
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "reason", reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_crossover_v2_verification_result",
            "capture_validity": self.capture_validity.value,
            "realization": self.realization.value,
            "benefit": self.benefit.value,
            "spec": self.spec.value,
            "reason": self.reason,
        }


class EvidenceTrust(str, Enum):
    """Could this round measure the state it applied? (#2537)

    The first of the adoption table's four axes. Safety and quality are both
    read off measurements, so a round that could not measure has little for
    them to read — but this does NOT gate them: safety is evaluated first and
    checked first, precisely so a hazard visible in a bad capture is named as a
    hazard rather than as an absence.

    :attr:`UNTRUSTED` is the honest word for "no usable evidence", and it is
    what the owner's own ruling turns on — *an unmeasured applied state cannot
    be the least bad MEASURED tune*, so it comes off. It is deliberately not
    called "failed": nothing about the correction is being asserted.
    """

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class SafetyStatus(str, Enum):
    """Is the applied state safe to leave on a household's speaker? (#2537)

    The adoption table's hard-stop axis, and the ONLY one that can pull a
    measured graph off for something other than the absence of evidence.
    **Direction is the whole discriminator**: quieter than declared is a
    quality signal to learn from, louder than declared is a hazard.
    """

    SAFE = "safe"
    UNSAFE = "unsafe"


class QualityStatus(str, Enum):
    """How good is the measured result, once it is trusted and safe? (#2537)

    Three-valued, and the third value is why: :attr:`MISSED` and
    :attr:`REGRESSED` are not the same answer.

    * :attr:`MISSED` — the round did not hit its target, and no better MEASURED
      state is known. Keeping it is the least-bad measured tune, and the misses
      become the next round's targets.
    * :attr:`REGRESSED` — a better measured state IS known: the entry baseline
      measured flatter than the applied graph, past the margin. That is the one
      case where going back returns to a state this round itself measured, so
      the owner's "reverting to an unknown measured state seems dumb" does not
      cover it — the previous state is not unknown, it is the evidence.
    """

    PASSED = "passed"
    MISSED = "missed"
    REGRESSED = "regressed"


class IterationHeadroom(str, Enum):
    """Is a flatter, more level result still plausibly reachable? (#2602)

    The adoption table's FOURTH axis, and the owner's ruling it exists for:
    *in-tolerance is not done*. Before it, :attr:`QualityStatus.PASSED` was
    terminal — a round that realized its prediction and measured flatter ended
    the series, whatever was left on the table. The round-3 review of
    2026-08-16 is what that costs: a result inside every spec band, whose
    tweeter was "largely in range but still not flat", and whose 250-2000 Hz
    sat **2.37 dB above** 8000-16000 Hz — a tilt no reference choice moves.

    Two graded objectives, both read off the post-apply spec report and both
    frame-invariant, which is what lets them be compared across rounds at all:

    * **within-band flatness** — the worst
      :attr:`~jasper.active_speaker.flat_spec.BandResult.max_ripple_db`, each
      band's own deviation from its OWN level.
    * **between-band level alignment** — the largest step between two bands'
      levels, :func:`~jasper.active_speaker.flat_spec.spec_band_tilt`.

    Those two are the exact orthogonal decomposition of a band's total
    deviation (``max_deviation_db = level_deviation_db + max_ripple_db``, the
    identity :class:`~jasper.active_speaker.flat_spec.BandResult` states), so
    the pair covers the whole miss without either half double-counting the
    other.

    :attr:`EXHAUSTED` is the fail-closed answer, deliberately: an unreadable or
    ungradable report cannot show that anything is reachable, and the honest
    response to "we cannot tell" is to stop the series rather than to spend a
    household's evening on rounds nothing is steering.
    """

    REACHABLE = "reachable"
    EXHAUSTED = "exhausted"


class AdoptionOutcome(str, Enum):
    """What the round did with the intervention.

    Names mined from the R21 accept receipt's terminal vocabulary, which
    already learned this lesson: a status says what actually happened
    (``accepted_not_applied`` rather than ``applied``), and
    ``recovery_required`` always travels with a typed reason.

    :attr:`KEEP_FOR_ITERATION` replaced ``user_decision`` in #2537. The old
    name described a screen nobody rendered — ``_act_on_adoption`` treated it
    exactly like ``KEEP``, so a cell whose whole purpose was "do not claim
    success" claimed success by silence. The new name says what the round
    actually does with a trusted, safe, imperfect result: it keeps it, because
    it is the best MEASURED state known, and it records what to fix next.
    """

    KEEP = "keep"
    KEEP_FOR_ITERATION = "keep_for_iteration"
    RESTORE = "restore"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, init=False)
class AdoptionDecision:
    """Keep, keep-and-iterate, restore, or escalate — with a reason and a row.

    :attr:`AdoptionOutcome.RECOVERY_REQUIRED` is the one state that must never
    be reported as a restore: it means a restore was attempted and did not
    complete, so the speaker is in neither the entry graph nor the intended
    one.  Its reason is mandatory, mirroring the R21 receipt's
    ``recovery_reason``.

    ``row`` is the decision table's own stable identifier (#2537) — one of
    :data:`ADOPTION_ROWS`.  It exists because ``outcome`` and ``reason``
    together still cannot say *which rule fired*: two rows can share an
    outcome (three of the seven restore, and the four that keep the graph split
    two-and-two between ``keep`` and ``keep_for_iteration``) and a reason
    travels from whichever
    axis decided, so a driver chaining rounds mechanically would have to
    re-derive the rule from the reason string.  The row is the thing that does
    not move when a reason's wording does.

    Consumed since #2291 Phase 3b, which applies the issue's adoption table.
    """

    outcome: AdoptionOutcome
    reason: str
    row: str

    def __init__(
        self,
        *,
        outcome: AdoptionOutcome,
        reason: str = "",
        row: str = "",
    ) -> None:
        if not isinstance(outcome, AdoptionOutcome):
            raise CrossoverV2ContractError("outcome must be an AdoptionOutcome")
        if not isinstance(reason, str):
            raise CrossoverV2ContractError("reason must be a string")
        if not isinstance(row, str):
            raise CrossoverV2ContractError("row must be a string")
        if outcome in (
            AdoptionOutcome.RECOVERY_REQUIRED,
            AdoptionOutcome.RESTORE,
            AdoptionOutcome.KEEP_FOR_ITERATION,
        ) and not reason.strip():
            raise CrossoverV2ContractError(
                f"adoption outcome {outcome.value!r} must state a reason"
            )
        if row and row not in ADOPTION_ROWS:
            raise CrossoverV2ContractError(f"unknown adoption row {row!r}")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "row", row)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_crossover_v2_adoption_decision",
            "outcome": self.outcome.value,
            "reason": self.reason,
            "row": self.row,
        }


#: The adoption table's seven rows, as stable identifiers (#2537, #2602, #2656).
#:
#: Numbered after the owner's own ruling, which named four; row 5 is the fifth
#: the ruling's principle *requires* and did not enumerate — see
#: :class:`QualityStatus.REGRESSED` for why a measured regression is not a
#: "keep for iteration".  Rows 6 and 7 are the rule working exactly
#: as written below: a future row APPENDS, it never renumbers, so splitting the
#: passing cell left row 1 meaning what it always meant, and splitting the
#: missing one left row 2 meaning what it always meant.  The numbers are part
#: of the identifier so a reader can line a receipt up against the table
#: without a lookup.
#:
#: Eight identifiers for seven rows: :data:`ADOPTION_ROW_RESTORE_FAILED` is row
#: 0 and sits OUTSIDE the table, for the reason its own comment gives.
ADOPTION_ROW_KEEP = "row1_trusted_safe_passed"
ADOPTION_ROW_KEEP_FOR_ITERATION = "row2_trusted_safe_missed"
ADOPTION_ROW_RESTORE_UNSAFE = "row3_unsafe"
ADOPTION_ROW_RESTORE_UNTRUSTED = "row4_untrusted_evidence"
ADOPTION_ROW_RESTORE_REGRESSION = "row5_trusted_safe_regressed"
#: #2602's row, and the one that made the table four-axis: a round that PASSED
#: on quality, with a flatter result still reachable. Appended rather than
#: folded into :data:`ADOPTION_ROW_KEEP`, per the numbering rule above — row 1
#: still means what it meant, "this round passed and the series is over", and
#: the two now differ in the only way that matters to a household, which is
#: whether another round is coming.
ADOPTION_ROW_KEEP_ITERATING = "row6_trusted_safe_passed_reachable"
#: #2656's row, and the mirror of row 6: a round that MISSED on quality, on a
#: series whose round budget is spent. Row 2 means "missed, and another round
#: is coming"; this means "missed, and that was the last one". They differ in
#: the only way that matters to a household, which is the same way rows 1 and 6
#: differ — whether another round is coming.
#:
#: NOT folded into :data:`ADOPTION_ROW_KEEP`, whose identifier says *passed*
#: and this round did not. The OUTCOME is the same ``keep`` — the measured
#: graph stays live, exactly as it does on row 2, because it is still the
#: best measured state known — and this row is what stops that keep from
#: reading as a pass on a receipt, a journal line, or a screen.
ADOPTION_ROW_KEEP_MISSED_EXHAUSTED = "row7_trusted_safe_missed_exhausted"
#: Outside the table: a restore was attempted and did not complete, which no
#: row describes because it is not a decision about the evidence at all.
ADOPTION_ROW_RESTORE_FAILED = "row0_restore_failed"

ADOPTION_ROWS: frozenset[str] = frozenset({
    ADOPTION_ROW_KEEP,
    ADOPTION_ROW_KEEP_FOR_ITERATION,
    ADOPTION_ROW_RESTORE_UNSAFE,
    ADOPTION_ROW_RESTORE_UNTRUSTED,
    ADOPTION_ROW_RESTORE_REGRESSION,
    ADOPTION_ROW_KEEP_ITERATING,
    ADOPTION_ROW_KEEP_MISSED_EXHAUSTED,
    ADOPTION_ROW_RESTORE_FAILED,
})


#: What a round records when the host cannot name the graph a capture was
#: measured through — the honest filler for :attr:`RoundReceipt`'s two graph
#: fingerprints and for
#: :attr:`~jasper.active_speaker.crossover_v2.round_evidence.EntryBaseline.graph_fingerprint`.
#:
#: The three ways that happens are all honest and none is a defect: no
#: ``entry_graph_fingerprint`` seam is bound (every conductor unit test), the
#: seam raised, or the speaker has no applied Layer-A profile yet (its
#: first-ever round).  A capture that measured the speaker correctly must not be
#: rejected because its provenance could not be named — provenance is on the
#: record, never a gate.
#:
#: A NAMED sentinel rather than ``""`` because both that contract and this one
#: require a non-empty trimmed identity on the write and the read side: an empty
#: string would make ``from_dict`` refuse the whole record, so the round would
#: silently lose its baseline to a missing *fingerprint*.  This word survives the
#: round trip and says exactly what is true.
ENTRY_GRAPH_FINGERPRINT_UNKNOWN = "unknown"


#: What :attr:`RoundReceipt.proposal_fingerprint` identifies, as a closed word.
#:
#: **The field's meaning changed in #2392, and a durable record whose meaning
#: changed silently is a record a later reader will misattribute.**
#: Before #2392 the receipt was fed ``_tuning_attempt_id or
#: candidate.fingerprint`` — both of which are a *candidate* fingerprint. Since
#: #2392 it is fed :attr:`InterventionProposal.fingerprint`. The two cannot be
#: told apart by inspection: every one of them is a 64-character SHA-256 hex
#: digest from :func:`~jasper.audio_measurement.evidence_identity.json_fingerprint`,
#: so "the formats are disjoint" was never available as a migration story.
#:
#: So the receipt SAYS which it is, and the three states are total:
#:
#: * key **absent** from a banked ``round_receipt.json`` — written before
#:   #2392, therefore a candidate fingerprint.
#: * ``"candidate"`` — written after #2392, but proposal assembly refused
#:   (:func:`~.proposal.plan_intervention_proposal`) or the round was graded
#:   from a state that predates the proposal, so the candidate identity is what
#:   the round could honestly name.
#: * ``"intervention_proposal"`` — the fingerprint of the
#:   :class:`InterventionProposal` this round actually proposed.
#:
#: Closed rather than free text for :data:`PLAN_REFUSAL_REASONS`' reason: a
#: typo'd kind on a write-once artifact is a mislabelled durable record, and
#: failing closed at construction is the only place it can still be caught.
PROPOSAL_FINGERPRINT_KINDS = frozenset({"candidate", "intervention_proposal"})


@dataclass(frozen=True, init=False)
class RoundReceipt:
    """The immutable record one correction round leaves behind.

    #2291's receipt field list, bound together so a later round can treat the
    currently active profile as its new entry graph without re-deriving
    history.  ``restore_result`` is present only when a restore was attempted,
    and a :attr:`AdoptionOutcome.RECOVERY_REQUIRED` adoption must carry one —
    a recovery that cannot say what the restore did is not a receipt.

    Produced since #2291 Phase 3c, by
    :func:`~jasper.active_speaker.crossover_v2.round_evidence.build_round_receipt`,
    and persisted as a write-once evidence-bundle artifact at
    ``crossover_v2/<relay_session_id>/round_receipt.json``.

    :attr:`proposal_fingerprint_kind` is REQUIRED rather than defaulted, and
    that is the whole migration story of #2392: a receipt that cannot say what
    its proposal fingerprint identifies is a receipt a later session will
    misread, and a default would let a caller claim one regime by forgetting to
    state the other.  See :data:`PROPOSAL_FINGERPRINT_KINDS`.
    """

    round_id: str
    entry_graph_fingerprint: str
    rollback_anchor: Mapping[str, Any]
    entry_baseline: Mapping[str, Any]
    proposal_fingerprint: str
    proposal_fingerprint_kind: str
    applied_graph_fingerprint: str
    post_measurement: Mapping[str, Any]
    verification: VerificationResult
    adoption: AdoptionDecision
    #: The axes the adoption row was read off — trust, safety, quality, and
    #: since #2602 headroom — each as
    #: ``{"status": ..., "reason": ..., "evidence": {...}}`` (#2537, #2602).
    #: On the receipt rather than only in the journal because the receipt is
    #: what the NEXT round reads: "keep, and here is what to fix" is only
    #: actionable if the misses travel with it, and a journal line is not an
    #: artifact a chained driver can fetch.  ``{}`` on a round graded before
    #: this shipped, which is an absence and not "they all passed" — and a
    #: three-key mapping is a round graded before #2602, not a headroom axis
    #: that declined to answer.
    round_axes: Mapping[str, Any]
    restore_result: Mapping[str, Any]
    #: The round's own measured numbers that no verdict collapsed — the
    #: band-resolved realization the delta probe reported (#2649) and the
    #: per-position residual the post-apply cloud produced (§4.2). A THIRD
    #: mapping beside the two above rather than keys folded into either,
    #: because the three answer different questions and a reader has to be
    #: able to tell them apart: ``round_axes`` is what the axes DECIDED,
    #: ``evidence_identities`` is what the evidence WAS by name, and this is
    #: what the round MEASURED and nothing graded. It exists because the next
    #: bite commands from these numbers — a realization ratio per band says
    #: where the model and the hardware disagreed, and a role-labelled
    #: residual says whether a miss is the speaker's or the room's.
    #: ``{}`` on a round graded before this shipped, and on one whose
    #: instruments produced neither.
    round_measurements: Mapping[str, Any]
    evidence_identities: Mapping[str, Any]
    created_at: str
    fingerprint: str = field(init=False)

    def __init__(
        self,
        *,
        round_id: str,
        entry_graph_fingerprint: str,
        proposal_fingerprint: str,
        proposal_fingerprint_kind: str,
        verification: VerificationResult,
        adoption: AdoptionDecision,
        created_at: str,
        rollback_anchor: Mapping[str, Any] | None = None,
        entry_baseline: Mapping[str, Any] | None = None,
        applied_graph_fingerprint: str = "",
        post_measurement: Mapping[str, Any] | None = None,
        round_axes: Mapping[str, Any] | None = None,
        restore_result: Mapping[str, Any] | None = None,
        round_measurements: Mapping[str, Any] | None = None,
        evidence_identities: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(verification, VerificationResult):
            raise CrossoverV2ContractError(
                "verification must be a VerificationResult"
            )
        if not isinstance(adoption, AdoptionDecision):
            raise CrossoverV2ContractError("adoption must be an AdoptionDecision")
        if not isinstance(applied_graph_fingerprint, str):
            raise CrossoverV2ContractError(
                "applied_graph_fingerprint must be a string"
            )
        kind = _text(
            proposal_fingerprint_kind, field_name="proposal_fingerprint_kind"
        )
        if kind not in PROPOSAL_FINGERPRINT_KINDS:
            raise CrossoverV2ContractError(
                f"unknown proposal fingerprint kind {kind!r}"
            )
        restore = _json_mapping(restore_result, field_name="restore_result")
        if adoption.outcome is AdoptionOutcome.RECOVERY_REQUIRED and not restore:
            raise CrossoverV2ContractError(
                "a recovery_required round must record its restore result"
            )
        object.__setattr__(self, "round_id", _text(round_id, field_name="round_id"))
        object.__setattr__(
            self,
            "entry_graph_fingerprint",
            _text(entry_graph_fingerprint, field_name="entry_graph_fingerprint"),
        )
        object.__setattr__(
            self,
            "proposal_fingerprint",
            _text(proposal_fingerprint, field_name="proposal_fingerprint"),
        )
        object.__setattr__(self, "proposal_fingerprint_kind", kind)
        object.__setattr__(
            self, "created_at", _text(created_at, field_name="created_at")
        )
        object.__setattr__(
            self,
            "rollback_anchor",
            _json_mapping(rollback_anchor, field_name="rollback_anchor"),
        )
        object.__setattr__(
            self,
            "entry_baseline",
            _json_mapping(entry_baseline, field_name="entry_baseline"),
        )
        object.__setattr__(
            self, "applied_graph_fingerprint", applied_graph_fingerprint
        )
        object.__setattr__(
            self,
            "post_measurement",
            _json_mapping(post_measurement, field_name="post_measurement"),
        )
        object.__setattr__(self, "verification", verification)
        object.__setattr__(self, "adoption", adoption)
        object.__setattr__(
            self, "round_axes", _json_mapping(round_axes, field_name="round_axes")
        )
        object.__setattr__(self, "restore_result", restore)
        object.__setattr__(
            self,
            "round_measurements",
            _json_mapping(round_measurements, field_name="round_measurements"),
        )
        object.__setattr__(
            self,
            "evidence_identities",
            _json_mapping(evidence_identities, field_name="evidence_identities"),
        )
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": ROUND_RECEIPT_KIND,
            "round_id": self.round_id,
            "entry_graph_fingerprint": self.entry_graph_fingerprint,
            "rollback_anchor": dict(self.rollback_anchor),
            "entry_baseline": dict(self.entry_baseline),
            "proposal_fingerprint": self.proposal_fingerprint,
            "proposal_fingerprint_kind": self.proposal_fingerprint_kind,
            "applied_graph_fingerprint": self.applied_graph_fingerprint,
            "post_measurement": dict(self.post_measurement),
            "verification": self.verification.to_dict(),
            "adoption": self.adoption.to_dict(),
            "round_axes": dict(self.round_axes),
            "restore_result": dict(self.restore_result),
            "round_measurements": dict(self.round_measurements),
            "evidence_identities": dict(self.evidence_identities),
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


# --------------------------------------------------------------------------- #
# constants the flow used to own
# --------------------------------------------------------------------------- #

# Total MIC POSITIONS in the pre-apply cloud, MEASURE's design-axis anchor
# included — so the plan emits ``N − 1`` additional prompted positions after
# MEASURE.
#
# Read that literally: the cloud carries ``N − 1`` SUMMED CURVES, not N. The
# anchor is a per-driver MEASURE capture, so ``_analyze_measure`` produces no
# ``summed_response`` for it to contribute and only a modelled
# ``predicted_sum``. The same holds for the post-apply group below, where
# VERIFY's anchor DOES capture a summed sweep but is consumed by the tracking
# verdict rather than joined to the group.
#
# 9 is chosen so that ``N − 1`` = 8 CURVES, which is what
# docs/historical/linearization-campaign-2026-07.md fundamental 1's "N≈8–12 gated sweeps" floor
# actually asks for (adjudication 3a, 2026-07-26: the first draft shipped 8
# positions ⇒ 7 curves, meeting the floor in positions but not in the thing
# that gets combined). Beyond that floor it is a WALL-CLOCK choice, not a
# statistical optimum: S0's stability work (6-of-10 subsets,
# docs/historical/linearization-campaign-2026-07.md "S0 executed") says more positions is
# strictly better, and the session-length ceiling is what stops us at 9. Treat
# it as a constant, never as a promise about accuracy.
DEFAULT_CLOUD_MEASURE_POSITIONS = 9
# VERIFY PASS: |measured sum − predicted sum| ≤ this over [Fc/2, 2·Fc] (§5.2),
# measured against the notch-excluded max (W6.7 ruling 1 —
# `program_analysis.VERIFY_NOTCH_EXCLUSION_DB`) rather than the raw max.
VERIFY_TOLERANCE_DB = 1.5
# …and the key that number is compared against, which is why it lives beside
# it. The absolute VERIFY tracking error used by both the live attempts loop
# and the offline repeat-floor replay. Lower is better: zero is the model's
# prediction of perfect realization, while the analyzer's value is what the
# applied speaker actually realized.
ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED = "max_db_notch_excluded"

#: WHERE the two sides of #2291's before→after comparison were measured.
#:
#: The mark is the one spot CHECK asks the household to stand the microphone on
#: and MEASURE names ("this spot is the mark"), and both the entry baseline and
#: the post-apply VERIFY are taken there. ``program_id`` equality cannot see
#: position — a capture a metre away replays the identical program — so
#: :class:`~jasper.active_speaker.crossover_v2.verification.MeasurementComparand`
#: carries this second identity and
#: :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_benefit`
#: refuses a pair whose marks disagree.
#:
#: **One owner, deliberately.** Both sides must stamp the SAME string or every
#: round grades
#: :data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_MARK_MISMATCH`,
#: so the post-apply side imports this constant rather than spelling the
#: literal a second time. It is a stable identity, not a coordinate: nothing
#: measures where the mark physically is, and the flow makes no claim that two
#: sessions' marks are the same place — only that within ONE round the mic did
#: not move between the two captures, which is what the round's own
#: choreography (baseline last in stage 1, VERIFY first in stage 2, no prompted
#: move between them) is for.
REFERENCE_MARK_DESIGN_AXIS = "design_axis_mark"


# --------------------------------------------------------------------------- #
# The engine's measure/analyze parameter vocabulary
# (docs/REFACTOR-TUNING-2026-08.md §3 wave 1, ruling S12)
# --------------------------------------------------------------------------- #
#
# These live HERE rather than beside the engine's `MeasureSpec` for the reason
# this package's own `__init__` gives for not re-exporting `forward_model`:
# reaching a handful of string literals must not drag `numpy` and the analysis
# stack into every importer. Read from their owning modules
# (`spatial.POSITION_AXES`, `driver_acoustics.CAPTURE_GEOMETRIES`,
# `program_analysis.polarity_label`) the vocabulary costs ~1,100 modules to
# quote; declared here it costs none beyond this module, and
# `tests/test_crossover_v2_engine_skeleton.py` pins every one of them equal to
# its owner's spelling so the cheap copy cannot drift off the real one.

#: The three parameterizations of the one `measure` verb — ruling S1's
#: "measuring is measuring" made visible in the data, and wave 4j's `kind`
#: index column. A baseline, a candidate check and a re-measure differ by this
#: word and by nothing else in the code that runs them.
MEASURE_KIND_BASELINE = "baseline"
MEASURE_KIND_CANDIDATE = "candidate"
MEASURE_KIND_VERIFY = "verify"
MEASURE_KINDS = (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
)

#: The two capture regimes. Owner: `driver_acoustics.CAPTURE_GEOMETRIES`.
REGIME_NEAR_FIELD = "near_field"
REGIME_REFERENCE_AXIS = "reference_axis"
MEASURE_REGIMES = (REGIME_NEAR_FIELD, REGIME_REFERENCE_AXIS)

#: The measurement frame's polarity words. Owner:
#: `program_analysis.polarity_label`, which calls itself "the ONE spelling of
#: the map". Distinct from the candidate's polarity ACTIONS
#: (`crossover_alignment.POLARITY_KEEP` / `POLARITY_INVERT`), which say what a
#: speaker should DO rather than how a capture was taken.
POLARITY_NORMAL = "normal"
POLARITY_INVERTED = "inverted"
POLARITIES = (POLARITY_NORMAL, POLARITY_INVERTED)

#: The pose axes. Owner: `spatial.POSITION_AXES`.
POSITION_AXIS_HORIZONTAL = "horizontal"
POSITION_AXIS_VERTICAL = "vertical"
POSITION_AXES = (POSITION_AXIS_HORIZONTAL, POSITION_AXIS_VERTICAL)

#: The design axis, in `PositionGeometry`'s own spelling for it: a capture with
#: no prompted move of its own is a design-axis capture at `0`, which is what
#: `spatial._DESIGN_AXIS_GEOMETRY` declares. `None` is a different fact — "no
#: side was declared" — and must never be minted here as a synonym for this.
DESIGN_AXIS_DEG = 0
