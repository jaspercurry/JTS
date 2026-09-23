# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover-v2 domain contracts: its refusal errors, a candidate's acoustic
context, and the engine's shared vocabulary.

Every fingerprinted value uses
:mod:`jasper.audio_measurement.evidence_identity`'s ``_core()`` payload and
``json_fingerprint``: one canonicalizer, one digest domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from jasper.audio_measurement.evidence_identity import (
    FingerprintedRecord,
    json_fingerprint,
)
from jasper.json_fields import finite_float

from ..branch_chain import CrossoverSection

__all__ = [
    "ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED",
    "CandidateAcousticContext",
    "CandidateFcDisagreementError",
    "CrossoverV2ContractError",
    "CrossoverV2FlowError",
    "DEFAULT_CLOUD_MEASURE_POSITIONS",
    "DESIGN_AXIS_DEG",
    "DRIVER_ROLES",
    "DRIVER_ROLE_TWEETER",
    "DRIVER_ROLE_WOOFER",
    "LINEARIZATION_OUTCOME_SINGLE_BRANCH",
    "MEASURE_KINDS",
    "MEASURE_KIND_BASELINE",
    "MEASURE_KIND_CANDIDATE",
    "MEASURE_KIND_VERIFY",
    "MEASURE_REGIMES",
    "NoCrossoverSectionsError",
    "POLARITIES",
    "POLARITY_INVERT",
    "POLARITY_INVERTED",
    "POLARITY_KEEP",
    "POLARITY_NORMAL",
    "POSITION_AXES",
    "POSITION_AXIS_HORIZONTAL",
    "POSITION_AXIS_VERTICAL",
    "REFERENCE_MARK_DESIGN_AXIS",
    "REGIME_NEAR_FIELD",
    "REGIME_REFERENCE_AXIS",
    "ROUND_RECEIPT_KIND",
    "ResponseCurve",
    "VERIFY_TOLERANCE_DB",
]

SCHEMA_VERSION = 3

#: What a banked round receipt calls itself — the discriminator a store routes
#: on, named beside the type that emits it.
ROUND_RECEIPT_KIND = "jts_crossover_v2_round_receipt"


class CrossoverV2FlowError(RuntimeError):
    """The v2 session could not form a safe phase transition.

    Here rather than in the flow because two modules raise it and neither may
    import the other. Callers can catch all flow refusals at this boundary.
    """


class CrossoverV2ContractError(ValueError):
    """A crossover-v2 contract value is malformed, ambiguous, or inconsistent.

    Carries the refusal code a raise means, so a caller reads an attribute set
    at the raise site instead of parsing the message. The reason is part of the
    contract; the message may be reworded freely.
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


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CrossoverV2ContractError(
            f"{field_name} must be a non-empty trimmed string"
        )
    return value


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


    def to_json(self) -> dict[str, Any]:
        return {"hz": list(self.hz), "db": list(self.db)}


# --------------------------------------------------------------------------
# candidate acoustic context — the one Fc owner
# --------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class CandidateAcousticContext(FingerprintedRecord):
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


#: A candidate's ``linearization_outcome`` when its speaker has ONE branch. A
#: sibling of ``"fitted"`` rather than that value, which would claim a
#: committed trim pair for a speaker that solved none.
LINEARIZATION_OUTCOME_SINGLE_BRANCH = "fitted_single_branch"


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
# measured against the notch-excluded max
# (`program_analysis.VERIFY_NOTCH_EXCLUSION_DB`) rather than the raw max.
VERIFY_TOLERANCE_DB = 1.5
# …and the key that number is compared against, which is why it lives beside it:
# the absolute VERIFY tracking error read by both the live attempts loop and the
# offline repeat-floor replay. Lower is better.
ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED = "max_db_notch_excluded"

#: WHERE the two sides of #2291's before→after comparison were measured — the
#: one spot CHECK asks the household to stand the microphone on, where both the
#: entry baseline and the post-apply VERIFY are taken. ``program_id`` equality
#: cannot see position (a capture a metre away replays the identical program),
#: so a capture carries this second identity.
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

REGIME_NEAR_FIELD = "near_field"
REGIME_REFERENCE_AXIS = "reference_axis"
MEASURE_REGIMES = (REGIME_NEAR_FIELD, REGIME_REFERENCE_AXIS)

#: Capture polarity describes the take; candidate polarity names the action.
POLARITY_KEEP = "keep"
POLARITY_INVERT = "invert"
POLARITY_NORMAL = "normal"
POLARITY_INVERTED = "inverted"
POLARITIES = (POLARITY_NORMAL, POLARITY_INVERTED)

#: The driver branches a `polarity=inverted` measurement may flip. Owner:
#: `profile.DRIVER_ROLES_BY_WAY[2]`. A polarity flip is a statement about two
#: branches summing, so a 1-way's MeasureSpec names no inverted role.
DRIVER_ROLE_WOOFER = "woofer"
DRIVER_ROLE_TWEETER = "tweeter"
DRIVER_ROLES = (DRIVER_ROLE_WOOFER, DRIVER_ROLE_TWEETER)

#: A pose whose stated displacement from the mark lies in the HORIZONTAL
#: plane. Names where the pose's STATED offset lies, not a promise that
#: nothing else moved: a compound move records its extra rise in
#: `spatial.PositionGeometry.vertical_deg`, never in `axis`.
POSITION_AXIS_HORIZONTAL = "horizontal"

#: A pose stated as a move ABOVE or BELOW mark height. Nothing rotates in
#: elevation, so such a pose commands no horizontal bearing
#: (`spatial.PositionGeometry.degrees` is `None`), a different fact from
#: "0 deg". Where it was raised to is `spatial.PositionGeometry.vertical_deg`.
POSITION_AXIS_VERTICAL = "vertical"

#: Every axis a pose can be stated on, so a reader can CHECK the value.
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
