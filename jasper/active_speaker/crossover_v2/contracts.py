# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Immutable domain contracts for the crossover-v2 intervention loop (#2291).

**What this module is for.** The v2 conductor currently owns measurement
context, candidate context, prescription policy, verification semantics, and
lifecycle in one mutable object.  On 2026-08-10 that produced a candidate whose
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

Phase 1 of #2291 introduces these types and wires
:class:`InterventionProposal` alongside the existing planner.  The types whose
docstrings name a later phase are defined and validated here but have no
producer or consumer yet; that is the point of contracts-first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from jasper.audio_measurement.evidence_identity import json_fingerprint

from ..branch_chain import CrossoverSection

__all__ = [
    "AdoptionDecision",
    "AdoptionOutcome",
    "BenefitStatus",
    "CandidateAcousticContext",
    "CandidateFcDisagreementError",
    "CaptureValidity",
    "CrossoverV2ContractError",
    "InterventionProposal",
    "NoCrossoverSectionsError",
    "PLAN_REFUSAL_REASONS",
    "PlanRefusal",
    "RealizationStatus",
    "ResponseCurve",
    "RoundReceipt",
    "SpecStatus",
    "TrimStrategy",
    "VerificationResult",
]

SCHEMA_VERSION = 1


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


def _detached(value: Any) -> Any:
    """One JSON-shaped value with no container shared with the caller.

    Recursive on purpose. A shallow ``dict(value)`` detaches only the top
    level, so a caller holding the *nested* dict it passed in could still
    mutate a "frozen" proposal after construction — and the fingerprint, taken
    once at construction, would no longer describe the object a reader sees.
    That divergence is the exact failure mode these contracts exist to prevent,
    one level down (#2307 gate note N1).

    Leaves are returned as they are: this normalizes *containers*, not values,
    so a numpy scalar or a small dataclass passes through untouched. Sequences
    become tuples, which is both immutable and fingerprint-neutral —
    ``json.dumps`` renders a tuple and a list identically, so no existing
    digest moves.
    """

    if isinstance(value, Mapping):
        return {key: _detached(item) for key, item in value.items()}
    if isinstance(value, (str, bytes, bytearray)):
        return value
    if isinstance(value, Sequence):
        return tuple(_detached(item) for item in value)
    return value


def _json_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    """A defensive DEEP copy of one JSON-shaped mapping, or ``{}`` for ``None``.

    The copy matters, and its depth matters: a frozen dataclass holding a
    caller's live dict — at any level — is immutable in name only. See
    :func:`_detached`.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CrossoverV2ContractError(f"{field_name} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise CrossoverV2ContractError(f"{field_name} keys must be strings")
    return {key: _detached(item) for key, item in value.items()}


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
        """Normalize the conductor's ``(freqs, db)`` pair; ``None`` passes through."""

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

    **The vocabulary this replaces was actively misleading.** Today
    ``_fit_linearization`` records ``linearization_outcome =
    "trim_rejected" if wild else "fitted"``, where ``wild`` means only that the
    ripple scan drifted more than
    ``LINEARIZATION_TRIM_SANITY_MARGIN_DB`` (6 dB) from the level-preserving
    anchor.  Which pair was *committed* is decided separately and immediately
    afterwards, by whichever pair realizes the better inter-driver level.  On
    2026-08-10 those two facts pointed opposite ways: the artifact said
    ``trim_rejected`` and the −13.013 dB resolved trim was committed anyway.
    The word "rejected" described a drift observation, not an outcome.

    Every member below therefore names **what was committed**.  Drift is a
    qualifier on a commitment, never a substitute for one.

    Reachability today: Phase 1 assembles proposals from the persisted
    candidate artifact, which records drift but *not* the winning pair — so a
    fitted candidate maps to :attr:`COMMITTED_PAIR_UNRECORDED` or
    :attr:`COMMITTED_PAIR_UNRECORDED_AFTER_SANITY_DRIFT`, which is the honest
    statement of that evidence gap.  The four precise members are the target
    vocabulary for #2291 Phase 2, when the planner returns the winning pair as
    data instead of leaving it in a log line.
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

    The 2026-08-10 jts3 case.  Legal under the current level-graded policy, but
    it must never be recorded under a name containing "rejected".
    """

    COMMITTED_PAIR_UNRECORDED = "committed_pair_unrecorded"
    """A trim pair was committed, in margin; the artifact does not say which."""

    COMMITTED_PAIR_UNRECORDED_AFTER_SANITY_DRIFT = (
        "committed_pair_unrecorded_after_sanity_drift"
    )
    """A trim pair was committed after drift; the artifact does not say which."""


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

    **Nothing produces or consumes this yet.** #2291 Phase 3 computes the four
    statuses independently and feeds them to :class:`AdoptionDecision`; Phase 1
    only fixes the vocabulary and the invariants.
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


class AdoptionOutcome(str, Enum):
    """What the round did with the intervention.

    Names mined from the R21 accept receipt's terminal vocabulary, which
    already learned this lesson: a status says what actually happened
    (``accepted_not_applied`` rather than ``applied``), and
    ``recovery_required`` always travels with a typed reason.
    """

    KEEP = "keep"
    RESTORE = "restore"
    USER_DECISION = "user_decision"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, init=False)
class AdoptionDecision:
    """Keep, restore, ask, or escalate — with a reason that is never optional.

    :attr:`AdoptionOutcome.RECOVERY_REQUIRED` is the one state that must never
    be reported as a restore: it means a restore was attempted and did not
    complete, so the speaker is in neither the entry graph nor the intended
    one.  Its reason is mandatory, mirroring the R21 receipt's
    ``recovery_reason``.

    **Nothing produces or consumes this yet** — #2291 Phase 3 applies the
    issue's adoption table.
    """

    outcome: AdoptionOutcome
    reason: str

    def __init__(self, *, outcome: AdoptionOutcome, reason: str = "") -> None:
        if not isinstance(outcome, AdoptionOutcome):
            raise CrossoverV2ContractError("outcome must be an AdoptionOutcome")
        if not isinstance(reason, str):
            raise CrossoverV2ContractError("reason must be a string")
        if outcome in (
            AdoptionOutcome.RECOVERY_REQUIRED,
            AdoptionOutcome.RESTORE,
            AdoptionOutcome.USER_DECISION,
        ) and not reason.strip():
            raise CrossoverV2ContractError(
                f"adoption outcome {outcome.value!r} must state a reason"
            )
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "reason", reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_crossover_v2_adoption_decision",
            "outcome": self.outcome.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, init=False)
class RoundReceipt:
    """The immutable record one correction round leaves behind.

    #2291's receipt field list, bound together so a later round can treat the
    currently active profile as its new entry graph without re-deriving
    history.  ``restore_result`` is present only when a restore was attempted,
    and a :attr:`AdoptionOutcome.RECOVERY_REQUIRED` adoption must carry one —
    a recovery that cannot say what the restore did is not a receipt.

    **Nothing produces or consumes this yet** — #2291 Phase 3 persists it.
    """

    round_id: str
    entry_graph_fingerprint: str
    rollback_anchor: Mapping[str, Any]
    entry_baseline: Mapping[str, Any]
    proposal_fingerprint: str
    applied_graph_fingerprint: str
    post_measurement: Mapping[str, Any]
    verification: VerificationResult
    adoption: AdoptionDecision
    restore_result: Mapping[str, Any]
    evidence_identities: Mapping[str, Any]
    created_at: str
    fingerprint: str = field(init=False)

    def __init__(
        self,
        *,
        round_id: str,
        entry_graph_fingerprint: str,
        proposal_fingerprint: str,
        verification: VerificationResult,
        adoption: AdoptionDecision,
        created_at: str,
        rollback_anchor: Mapping[str, Any] | None = None,
        entry_baseline: Mapping[str, Any] | None = None,
        applied_graph_fingerprint: str = "",
        post_measurement: Mapping[str, Any] | None = None,
        restore_result: Mapping[str, Any] | None = None,
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
        object.__setattr__(self, "restore_result", restore)
        object.__setattr__(
            self,
            "evidence_identities",
            _json_mapping(evidence_identities, field_name="evidence_identities"),
        )
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_crossover_v2_round_receipt",
            "round_id": self.round_id,
            "entry_graph_fingerprint": self.entry_graph_fingerprint,
            "rollback_anchor": dict(self.rollback_anchor),
            "entry_baseline": dict(self.entry_baseline),
            "proposal_fingerprint": self.proposal_fingerprint,
            "applied_graph_fingerprint": self.applied_graph_fingerprint,
            "post_measurement": dict(self.post_measurement),
            "verification": self.verification.to_dict(),
            "adoption": self.adoption.to_dict(),
            "restore_result": dict(self.restore_result),
            "evidence_identities": dict(self.evidence_identities),
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}
