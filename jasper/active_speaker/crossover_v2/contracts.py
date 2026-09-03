# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Immutable domain contracts for the crossover-v2 intervention loop (#2291).

The honest shape as a TYPE rather than a convention, after the 2026-08-10
defect where a candidate's sections said 1,648.7 Hz while its trim arithmetic
read 2,000 Hz. Not :mod:`jasper.active_speaker.crossover_contract`, which owns
whether an ALREADY APPLIED graph matches its declaration; this owns what a
PROPOSED intervention is. Every fingerprinted value uses
:mod:`jasper.audio_measurement.evidence_identity`'s ``_core()`` payload and
``json_fingerprint``: one canonicalizer, one digest domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.json_fields import finite_float

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
    "DRIVER_ROLES",
    "DRIVER_ROLE_TWEETER",
    "DRIVER_ROLE_WOOFER",
    "EvidenceTrust",
    "InterventionProposal",
    "IterationHeadroom",
    "LINEARIZATION_OUTCOME_SINGLE_BRANCH",
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
#: ``RoundReceipt.round_measurements``. A reader that branches on this should
#: treat 1 as "no blend record, ever" rather than as "the blend record is absent
#: for this round" — different facts, and only the version separates them.
SCHEMA_VERSION = 2

#: What a banked round receipt calls itself — the discriminator a store routes
#: on, named beside the type that emits it.
ROUND_RECEIPT_KIND = "jts_crossover_v2_round_receipt"


class CrossoverV2FlowError(RuntimeError):
    """The v2 session could not form a safe phase transition.

    Here rather than in the flow because two modules raise it and neither may
    import the other. ``angle_capture_spool.AngleRequestRefused`` subclasses it,
    which is what lets one ``except`` clause cover both.
    """


class CrossoverV2ContractError(ValueError):
    """A crossover-v2 contract value is malformed, ambiguous, or inconsistent.

    Carries the :data:`PLAN_REFUSAL_REASONS` member a raise means, so a caller
    reads an attribute set at the raise site instead of parsing the message. The
    reason is part of the contract; the message may be reworded freely.
    """

    #: Overridden by the subclasses below; the generic default is honest.
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
    number = finite_float(value)
    if number is None:
        raise CrossoverV2ContractError(f"{field_name} must be a finite real number")
    return number


def _positive(value: Any, *, field_name: str) -> float:
    number = _finite(value, field_name=field_name)
    if number <= 0.0:
        raise CrossoverV2ContractError(f"{field_name} must be positive")
    return number


def _rounded(value: Any, digits: int) -> float | None:
    """``round(value, digits)`` for a real number, ``None`` for anything else.

    Keeps a diagnostic line's absent values as ``None`` rather than letting a
    missing field become ``0.0``.
    """
    return round(float(value), digits) if isinstance(value, (int, float)) else None


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CrossoverV2ContractError(
            f"{field_name} must be a non-empty trimmed string"
        )
    return value


def detached_json(value: Any) -> Any:
    """One JSON-shaped value with no container shared with the caller.

    Recursive on purpose: a shallow ``dict(value)`` detaches only the top level,
    so a caller holding a NESTED dict it passed in could still mutate a frozen
    proposal after its fingerprint was taken (#2307 gate note N1). Leaves are
    returned as they are — this normalizes containers, not values. A copied list
    stays a ``list`` because the shared fingerprinter's ``_freeze_json`` admits
    ``type(value) is list`` exactly and refuses a tuple, so no digest moves.
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

    The depth matters: a frozen dataclass holding a caller's live dict at any
    level is immutable in name only. See :func:`detached_json`.
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

    The planner's ``(freqs_hz, db)`` numpy arrays are normalized to plain float
    tuples: an array is mutable, is not JSON-canonicalizable by the shared
    fingerprinter, and a non-finite bin must be refused rather than hashed.
    Values are stored exactly — rounding would be a precision POLICY, and this
    module owns none.
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

    A context owns the corner AND the sections together, so a planner holding
    one cannot ask a second question about which crossover it is planning — the
    2026-08-10 dual-Fc defect, made impossible.

    Agreement is checked at construction and is EXACT, not toleranced: these
    sections are built in-process from a single float, so any inequality is a
    real disagreement (``REGION_FC_MATCH_TOLERANCE_HZ`` answers a different
    question, about a corner round-tripped through persisted JSON). A role with
    no sections is legitimate and preserved — a driver with no crossover region
    runs full range — but at least one section must exist overall.
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
                # A section cornered anywhere other than this context's Fc is
                # the mixed-Fc defect; it fails closed, never re-cornered.
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

        A caller holding one candidate's sections has no reason to also carry an
        Fc, and carrying one is how a session corner reaches candidate planning.
        The sections must be unanimous; a split set fails closed.
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

    @classmethod
    def for_candidate(
        cls,
        sections_by_role: Mapping[str, Sequence[CrossoverSection]],
        *,
        roles: Sequence[str],
    ) -> "CandidateAcousticContext | None":
        """The context a candidate of this SHAPE is planned and proposed at.

        ``None`` for the one shape that legitimately has no corner: a 1-way
        main. THE one derivation, asked by both the planner and the proposal
        assembler.
        """

        if len(roles) == 1 and not any(sections_by_role.values()):
            return None
        return cls.from_sections(sections_by_role)

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

    Every member names WHAT WAS COMMITTED; drift is a qualifier on a commitment,
    never a substitute for one. The retired vocabulary recorded
    ``trim_rejected`` for a drift observation while the drifted pair was
    committed anyway — the 2026-08-10 defect.

    :attr:`COMMITTED_PAIR_UNRECORDED` is the artifact-derived member: the
    proposal assembled at the commit seam reads the ``linearization_outcome``
    string the build stamped on the candidate, which does not record which pair
    won the realized-level grading, so it states that gap rather than guessing.
    It has no drift-qualified sibling: the string determines
    :attr:`ANCHORED_COMMITTED_AFTER_SANITY_DRIFT` precisely, and
    ``tests/test_crossover_v2_proposal.py::test_the_unrecorded_drift_member_is_gone_and_referenced_nowhere``
    keeps that name out of the tree.
    """

    NOT_FITTED = "not_fitted"
    """No linearization fit produced a trim pair (ineligible, or the fit failed)."""

    ANCHORED_COMMITTED = "anchored_committed"
    """The level-preserving anchored trim was committed; the scan stayed in margin."""

    RESOLVED_COMMITTED = "resolved_committed"
    """The ripple-scan trim was committed; the scan stayed in margin."""

    ANCHORED_COMMITTED_AFTER_SANITY_DRIFT = "anchored_committed_after_sanity_drift"
    """The scan drifted beyond the margin and the anchor was committed instead.

    The only genuine fallback, and the only case an older reader would have been
    right to call a rejection.
    """

    RESOLVED_COMMITTED_AFTER_SANITY_DRIFT = "resolved_committed_after_sanity_drift"
    """The scan drifted beyond the margin and was committed anyway.

    Unreachable from the live path: the planner commits the anchor beyond the
    margin. RETAINED because it is the honest name for what already-persisted
    artifacts describe, and whatever else is true of it, it must never be
    recorded under a name containing "rejected".
    """

    COMMITTED_PAIR_UNRECORDED = "committed_pair_unrecorded"
    """A trim pair was committed, in margin; the artifact does not say which."""

    NO_PAIR_TO_TRIM = "no_pair_to_trim"
    """The speaker has ONE branch, so no inter-driver trim exists to commit.

    Distinct from :attr:`NOT_FITTED`: a 1-way main's branch IS fitted and ships
    at a fixed 0 dB. Reached from :data:`LINEARIZATION_OUTCOME_SINGLE_BRANCH`.
    """


#: :attr:`~.plan_assembly.LinearizationPlan.outcome` for a round whose speaker
#: has ONE branch. A sibling of ``"fitted"`` rather than that value, which maps
#: to :attr:`TrimStrategy.COMMITTED_PAIR_UNRECORDED` and would claim a committed
#: trim pair for a speaker that solved none.
LINEARIZATION_OUTCOME_SINGLE_BRANCH = "fitted_single_branch"


# --------------------------------------------------------------------------
# the proposal
# --------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class InterventionProposal:
    """One complete, fingerprinted prescription: everything committed, together.

    Fields that are empty are empty HONESTLY rather than absent — an
    intervention whose trim evidence or pre-apply predicted spec cannot be
    stated is exactly what the incident review could not reconstruct.
    :attr:`fingerprint` covers every committed value below, so any change to the
    candidate, corner, section, trim, filter, curve, spec report or evidence
    identity produces a different digest.
    """

    candidate: Any
    """The complete ``MeasuredCrossoverCandidate`` this proposal would apply."""

    context: CandidateAcousticContext | None
    """The one Fc owner: candidate corner and sections, agreeing by construction.

    ``None`` on a 1-way main, which declares no crossover for a context to own.
    """

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
        context: CandidateAcousticContext | None,
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
        if context is not None and not isinstance(context, CandidateAcousticContext):
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
    def fc_hz(self) -> float | None:
        """The candidate corner — read from the context, never from a session.

        ``None`` is a 1-way main declaring no corner, never an unreadable one.
        """

        return None if self.context is None else self.context.fc_hz

    @property
    def candidate_fingerprint(self) -> str:
        return str(getattr(self.candidate, "fingerprint", "") or "")

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_crossover_v2_intervention_proposal",
            "candidate_fingerprint": self.candidate_fingerprint,
            "context": None if self.context is None else self.context.to_dict(),
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

    A refusal is a RESULT, never an exception escaping into the caller's path,
    so the reason survives into logs and the household surface. ``reason`` is
    drawn from the closed :data:`PLAN_REFUSAL_REASONS` set; ``detail`` is free
    text for an operator.
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

    Keeping them separate is the point (#1868): on 2026-08-10 VERIFY tracked the
    model to within 1.291 dB while the absolute crossover check failed by
    +5.456 dB, and the run still read as passed. A realization pass must not be
    able to overwrite a benefit regression or a spec failure, and "we do not
    know" must have somewhere to live — hence
    :attr:`BenefitStatus.INDETERMINATE` and :attr:`SpecStatus.UNEVALUABLE`
    rather than a default of success.
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
        # An unusable capture cannot have produced a graded answer: claiming one
        # would be the "passed because the model matched" failure in a new hat.
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

    The first of the adoption table's four axes. It does NOT gate the others:
    safety is evaluated and checked first, so a hazard visible in a bad capture
    is named as a hazard rather than as an absence. :attr:`UNTRUSTED` is the
    honest word for "no usable evidence" — deliberately not "failed", since
    nothing about the correction is being asserted.
    """

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class SafetyStatus(str, Enum):
    """Is the applied state safe to leave on a household's speaker? (#2537)

    The adoption table's hard-stop axis, and the ONLY one that can pull a
    measured graph off for something other than the absence of evidence.
    Direction is the discriminator: quieter than declared is a quality signal,
    louder than declared is a hazard.
    """

    SAFE = "safe"
    UNSAFE = "unsafe"


class QualityStatus(str, Enum):
    """How good is the measured result, once it is trusted and safe? (#2537)

    Three-valued because :attr:`MISSED` and :attr:`REGRESSED` are not the same
    answer. MISSED: the round did not hit its target and no better MEASURED
    state is known, so keeping it is the least-bad measured tune and the misses
    become the next round's targets. REGRESSED: the entry baseline measured
    flatter than the applied graph, past the margin, so going back returns to a
    state this round itself measured.
    """

    PASSED = "passed"
    MISSED = "missed"
    REGRESSED = "regressed"


class IterationHeadroom(str, Enum):
    """Is a flatter, more level result still plausibly reachable? (#2602)

    The adoption table's FOURTH axis, for the owner's ruling that IN-TOLERANCE
    IS NOT DONE: before it, :attr:`QualityStatus.PASSED` was terminal, and the
    2026-08-16 round-3 review measured a result inside every spec band whose
    250-2000 Hz sat 2.37 dB above 8000-16000 Hz.

    Two graded objectives, both read off the post-apply spec report and both
    frame-invariant, which is what lets them be compared across rounds:
    within-band flatness (the worst ``BandResult.max_ripple_db``) and
    between-band level alignment (``flat_spec.spec_band_tilt``). They are the
    exact orthogonal decomposition of a band's total deviation
    (``max_deviation_db = level_deviation_db + max_ripple_db``), so the pair
    covers the whole miss without double-counting.

    :attr:`EXHAUSTED` is the fail-closed answer: an unreadable report cannot
    show that anything is reachable.
    """

    REACHABLE = "reachable"
    EXHAUSTED = "exhausted"


class AdoptionOutcome(str, Enum):
    """What the round did with the intervention.

    A status says what actually happened (``accepted_not_applied`` rather than
    ``applied``), and ``recovery_required`` always travels with a typed reason —
    the R21 accept receipt's terminal vocabulary.
    :attr:`KEEP_FOR_ITERATION` says what the round does with a trusted, safe,
    imperfect result: it keeps it, and records what to fix next.
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
    complete, so the speaker is in neither the entry graph nor the intended one.
    Its reason is mandatory.

    ``row`` is the decision table's own stable identifier (#2537), one of
    :data:`ADOPTION_ROWS`. ``outcome`` and ``reason`` together cannot say WHICH
    RULE fired — two rows can share an outcome, and a reason travels from
    whichever axis decided — and the row does not move when a reason's wording
    does.
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
#: its principle requires. A future row APPENDS and never renumbers, so every
#: existing row keeps its meaning, and the numbers are part of the identifier so
#: a receipt lines up against the table without a lookup. Eight identifiers for
#: seven rows: :data:`ADOPTION_ROW_RESTORE_FAILED` is row 0, outside the table.
ADOPTION_ROW_KEEP = "row1_trusted_safe_passed"
ADOPTION_ROW_KEEP_FOR_ITERATION = "row2_trusted_safe_missed"
ADOPTION_ROW_RESTORE_UNSAFE = "row3_unsafe"
ADOPTION_ROW_RESTORE_UNTRUSTED = "row4_untrusted_evidence"
ADOPTION_ROW_RESTORE_REGRESSION = "row5_trusted_safe_regressed"
#: #2602's row, and the one that made the table four-axis: a round that PASSED
#: on quality, with a flatter result still reachable. Appended rather than
#: folded into :data:`ADOPTION_ROW_KEEP`, which still means "this round passed
#: and the series is over".
ADOPTION_ROW_KEEP_ITERATING = "row6_trusted_safe_passed_reachable"
#: #2656's row, and the mirror of row 6: a round that MISSED on quality, on a
#: series whose round budget is spent. Row 2 means "missed, and another round is
#: coming"; this means "missed, and that was the last one". NOT folded into
#: :data:`ADOPTION_ROW_KEEP`, whose identifier says *passed*. The OUTCOME is the
#: same ``keep`` — the measured graph stays live — and this row is what stops
#: that keep from reading as a pass.
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
#: measured through — the filler for :attr:`RoundReceipt`'s two graph
#: fingerprints and for ``round_evidence.EntryBaseline.graph_fingerprint``.
#:
#: The three ways it happens are all honest: no ``entry_graph_fingerprint`` seam
#: bound, the seam raised, or the speaker has no applied Layer-A profile yet.
#: Provenance is on the record, never a gate. A NAMED sentinel rather than ``""``
#: because both contracts require a non-empty trimmed identity on write and
#: read: an empty string would make ``from_dict`` refuse the whole record.
ENTRY_GRAPH_FINGERPRINT_UNKNOWN = "unknown"


#: What :attr:`RoundReceipt.proposal_fingerprint` identifies, as a closed word.
#:
#: The field's meaning changed in #2392 — before it the receipt was fed a
#: CANDIDATE fingerprint, after it :attr:`InterventionProposal.fingerprint` —
#: and the two cannot be told apart by inspection, since both are 64-character
#: SHA-256 hex from ``evidence_identity.json_fingerprint``. So the receipt SAYS
#: which it is, and the three states are total: the key ABSENT from a banked
#: receipt means a candidate fingerprint; ``"candidate"`` means proposal
#: assembly refused or the round was graded from a pre-proposal state;
#: ``"intervention_proposal"`` is the proposal this round actually made.
#: Closed rather than free text: a typo'd kind on a write-once artifact is a
#: mislabelled durable record, catchable only at construction.
PROPOSAL_FINGERPRINT_KINDS = frozenset({"candidate", "intervention_proposal"})


@dataclass(frozen=True, init=False)
class RoundReceipt:
    """The immutable record one correction round leaves behind.

    #2291's receipt field list, bound together so a later round can treat the
    currently active profile as its new entry graph without re-deriving history.
    ``restore_result`` is present only when a restore was attempted, and a
    :attr:`AdoptionOutcome.RECOVERY_REQUIRED` adoption must carry one.
    Persisted as a write-once evidence-bundle artifact at
    ``crossover_v2/<relay_session_id>/round_receipt.json``.

    :attr:`proposal_fingerprint_kind` is REQUIRED rather than defaulted: a
    default would let a caller claim one regime by forgetting to state the
    other. See :data:`PROPOSAL_FINGERPRINT_KINDS`.
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
    #: ``{"status": ..., "reason": ..., "evidence": {...}}`` (#2537, #2602). On
    #: the receipt because it is what the NEXT round reads. ``{}`` on a round
    #: graded before this shipped, which is an absence and not "they all
    #: passed"; a three-key mapping is a round graded before #2602.
    round_axes: Mapping[str, Any]
    restore_result: Mapping[str, Any]
    #: The round's own measured numbers that no verdict collapsed — the
    #: band-resolved realization the delta probe reported (#2649) and the
    #: per-position residual the post-apply cloud produced (§4.2). A THIRD
    #: mapping rather than keys folded into either of the two above, because the
    #: three answer different questions: ``round_axes`` is what the axes
    #: DECIDED, ``evidence_identities`` is what the evidence WAS by name, and
    #: this is what the round MEASURED and nothing graded. ``{}`` on a round
    #: graded before this shipped, and on one whose instruments produced neither.
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
# included, so the plan emits ``N − 1`` additional prompted positions after
# MEASURE. Read that literally: the cloud carries ``N − 1`` SUMMED CURVES, not
# N — the anchor is a per-driver MEASURE capture with no ``summed_response``.
#
# 9 gives ``N − 1`` = 8 curves, the "N≈8-12 gated sweeps" floor of
# docs/historical/linearization-campaign-2026-07.md fundamental 1. Beyond that
# floor it is a WALL-CLOCK choice, not a statistical optimum: more positions is
# strictly better and the session-length ceiling is what stops us at 9. Treat it
# as a constant, never as a promise about accuracy.
DEFAULT_CLOUD_MEASURE_POSITIONS = 9
# VERIFY PASS: |measured sum − predicted sum| ≤ this over [Fc/2, 2·Fc] (§5.2),
# measured against the notch-excluded max (W6.7 ruling 1 —
# `program_analysis.VERIFY_NOTCH_EXCLUSION_DB`) rather than the raw max.
VERIFY_TOLERANCE_DB = 1.5
# …and the key that number is compared against, which is why it lives beside it:
# the absolute VERIFY tracking error read by both the live attempts loop and the
# offline repeat-floor replay. Lower is better.
ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED = "max_db_notch_excluded"

#: WHERE the two sides of #2291's before→after comparison were measured — the
#: one spot CHECK asks the household to stand the microphone on, where both the
#: entry baseline and the post-apply VERIFY are taken. ``program_id`` equality
#: cannot see position (a capture a metre away replays the identical program),
#: so ``verification.MeasurementComparand`` carries this second identity and
#: ``evaluate_benefit`` refuses a pair whose marks disagree.
#:
#: One owner, deliberately: both sides must stamp the SAME string. It is a
#: stable identity, not a coordinate — nothing measures where the mark
#: physically is, and no claim is made that two sessions' marks are the same
#: place, only that within ONE round the mic did not move between the captures.
REFERENCE_MARK_DESIGN_AXIS = "design_axis_mark"


# --------------------------------------------------------------------------- #
# The engine's measure/analyze parameter vocabulary
# (ruling S12 -- see ADR-0228)
# --------------------------------------------------------------------------- #
#
# These live HERE rather than beside the engine's `MeasureSpec` for the reason
# this package's `__init__` gives for keeping its numpy-heavy modules
# unexported: quoting the vocabulary from its owning modules costs ~1,100
# modules of import, declared here it costs none.
# `tests/test_crossover_v2_engine_skeleton.py` pins every one of them equal to
# its owner's spelling so the cheap copy cannot drift.

#: The three parameterizations of the one `measure` verb (ruling S1). A
#: baseline, a candidate check and a re-measure differ by this word and by
#: nothing else in the code that runs them.
MEASURE_KIND_BASELINE = "baseline"
MEASURE_KIND_CANDIDATE = "candidate"
MEASURE_KIND_VERIFY = "verify"
MEASURE_KINDS = (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
)

#: ``kind`` on the speaker's own per-take record, stamped by
#: `record_store.BankedRecordStore.bank`. Records without it are not a take
#: reader's input, whatever else is in the directory. Here rather than in
#: `position_cycle` because `record_store` writes it and `record_index` reads
#: it, and an owner either had to import would put a cycle in the package graph.
POSITION_EVIDENCE_KIND = "jts_crossover_v2_position_evidence"

#: Where `bank` publishes one JSON record per accepted take, RELATIVE to the
#: evidence store's artifacts root — the store's own namespace, without any
#: reader's prefix onto it. A record does not land at the relative path its
#: writer passes: `publish_json_artifact` runs it through `_artifact_path`,
#: which prefixes `{EVIDENCE_ROOT}/artifacts/`. Getting that wrong is silent —
#: the glob matches nothing — which is what
#: `test_the_glob_matches_a_record_the_REAL_store_wrote` exists for.
BANKED_TAKE_GLOB = "crossover_v2/*/positions/*.json"

#: The key a banked file carries its MEASUREMENT kind under. A record's own
#: `kind` is its ARTIFACT kind, which `position_cycle`'s readers gate on, while
#: a take selection filters by the measurement kind: two questions, two keys.
#: Spelled here because `record_store` writes it and `record_index` reads it.
MEASURE_KIND_KEY = "measure_kind"

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

#: The driver branches a `polarity=inverted` measurement may flip. Owner:
#: `profile.DRIVER_ROLES_BY_WAY[2]`. A polarity flip is a statement about two
#: branches summing, so a 1-way's MeasureSpec names no inverted role.
DRIVER_ROLE_WOOFER = "woofer"
DRIVER_ROLE_TWEETER = "tweeter"
DRIVER_ROLES = (DRIVER_ROLE_WOOFER, DRIVER_ROLE_TWEETER)

#: The pose axes. Owner: `spatial.POSITION_AXES`.
POSITION_AXIS_HORIZONTAL = "horizontal"
POSITION_AXIS_VERTICAL = "vertical"
POSITION_AXES = (POSITION_AXIS_HORIZONTAL, POSITION_AXIS_VERTICAL)

#: The design axis, in `PositionGeometry`'s own spelling: a capture with no
#: prompted move of its own is a design-axis capture at `0`. `None` is a
#: different fact — "no side was declared" — never a synonym for this.
DESIGN_AXIS_DEG = 0

#: The three states a plan §7 claim can be in; ``not_evaluated`` is first-class
#: and never collapses into the other two (R18).
CLAIM_PASS = "pass"
CLAIM_FAIL = "fail"
CLAIM_NOT_EVALUATED = "not_evaluated"


def measure_pair_claim(reason: str) -> dict[str, Any]:
    """The MEASURE verdict's pair claim when there was no pair to evaluate.

    The inter-driver axes — corner, delay, polarity, trim — are statements
    about two branches, so a solo round names their absence.
    """
    return {"pair": {"status": CLAIM_NOT_EVALUATED, "reason": reason}}


def realized_branch_level(
    verdict: Mapping[str, Any] | None, *, pair_reason: str | None,
) -> Mapping[str, Any] | None:
    """The committed pair's realized level, or the named absence of a pair.

    ``None`` still means a pair existed and nothing graded it; ``pair_reason``
    names the case where there was no pair, in the MEASURE verdict's own
    ``not_evaluated`` vocabulary.
    """
    if verdict is not None or pair_reason is None:
        return verdict
    return {"status": CLAIM_NOT_EVALUATED, "reason": pair_reason}
