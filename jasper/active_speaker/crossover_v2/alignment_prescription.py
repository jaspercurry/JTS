# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE inter-driver delay — and optionally its polarity basin — prescribed from
a named measurement (#2662).

Pure functions, no I/O, no session. A prescription enters at
``AlignmentEstimate`` (via ``MeasurementPriors.explicit_alignment_delay_us`` /
``explicit_alignment_polarity_sign``), never stamped on the candidate after,
so every downstream consumer reads one field. Two gates compose: provenance
(a named basis) and the lobe bound, measured from that declared basis and NOT
from the incumbent delay. Refusals raise and are never clamped to the boundary.
One parser, two policies: :func:`read_alignment_prescription` is the request
gate and the only place the bound is applied;
:func:`alignment_prescription_from_mapping` re-checks shape only and returns
``None``.
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
    "ALIGNMENT_NO_CROSSOVER_REGION",
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
#: emits, so an empty provenance slot stays distinguishable from a mangled one.
PRESCRIPTION_UNREADABLE_EVENT = "correction.crossover_v2_alignment_prescription_unreadable"

#: The request-body key a prescription arrives under.
ALIGNMENT_PRESCRIPTION_KEY = "alignment_prescription"

#: A document naming another version is refused, never best-effort parsed.
ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, distinct from every sibling class's own string.
ALIGNMENT_PRESCRIPTION_KIND = "jts_crossover_alignment_prescription"

#: The closed refusal vocabulary: a caller branches on a code, never on prose.
ALIGNMENT_PRESCRIPTION_MALFORMED = "prescription_malformed"
PRESCRIPTION_DELAY_INVALID = "prescription_delay_invalid"
PRESCRIPTION_BASIS_INVALID = "prescription_basis_invalid"
ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING = "prescription_provenance_missing"
PRESCRIPTION_FC_UNKNOWN = "prescription_fc_unknown"
#: A way-1 speaker: no corner and no second driver, so nothing to align. Its
#: own reason because an unknown corner is a number to go and derive while this
#: one never exists (#3480).
ALIGNMENT_NO_CROSSOVER_REGION = "alignment_no_crossover_region"
PRESCRIPTION_OUT_OF_LOBE = "prescription_out_of_lobe"
#: The preset's own declared delay window — the one bound here that does not
#: depend on a number the operator supplied.
PRESCRIPTION_OUTSIDE_DECLARED_WINDOW = "prescription_outside_declared_window"
#: A ``polarity`` outside the candidate's two action words. Its own reason so a
#: misspelled basin sends the operator to the vocabulary, not to the shape.
PRESCRIPTION_POLARITY_INVALID = "prescription_polarity_invalid"
ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED = "alignment_prescription_schema_unsupported"
ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    ALIGNMENT_PRESCRIPTION_MALFORMED,
    PRESCRIPTION_DELAY_INVALID,
    PRESCRIPTION_BASIS_INVALID,
    ALIGNMENT_PRESCRIPTION_PROVENANCE_MISSING,
    PRESCRIPTION_FC_UNKNOWN,
    ALIGNMENT_NO_CROSSOVER_REGION,
    PRESCRIPTION_OUT_OF_LOBE,
    PRESCRIPTION_OUTSIDE_DECLARED_WINDOW,
    PRESCRIPTION_POLARITY_INVALID,
    ALIGNMENT_PRESCRIPTION_SCHEMA_UNSUPPORTED,
})

#: The field names a prescription may carry. Anything else is refused rather
#: than ignored: a misspelled ``basis_artifact`` would leave the bound checking
#: against a basis nobody declared.
_PRESCRIPTION_FIELDS = frozenset({
    "kind",
    "artifact_schema_version",
    "delay_us",
    "basis_delay_us",
    "basis_artifacts",
    "basis_note",
    # Optional; absent is the automatic path.
    "polarity",
    # Written BY the gate, accepted on the way back in so a durable block
    # round-trips through this parser. A request that supplies them is
    # harmless: the gate overwrites both with what it actually checked.
    "checked_at_fc_hz",
    "lobe_us",
    "residual_us",
})


class AlignmentPrescriptionRefused(ValueError):
    """One prescription this module would not accept, and why.

    ``reason`` is from :data:`ALIGNMENT_PRESCRIPTION_REFUSAL_REASONS`, so the
    classification travels with the raise.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class AlignmentPrescription:
    """A validated inter-driver delay, and the measurement that justifies it.

    ``delay_us`` and ``basis_delay_us`` are both in ``AlignmentEstimate``'s
    signed frame, ``(D_woofer − D_tweeter)``: positive delays the tweeter,
    negative delays the woofer. One frame for both is what makes
    :attr:`residual_us` a physical quantity. ``basis_artifacts`` is required —
    a bound checked against an undeclared basis is arithmetic, not provenance.
    ``polarity`` is the optional basin pin in the candidate's own vocabulary
    (``POLARITY_KEEP``/``POLARITY_INVERT``), never ``POLARITY_REVIEW``;
    ``None`` leaves the polarity to the objective that owns it.
    """

    delay_us: float
    basis_delay_us: float
    basis_artifacts: tuple[str, ...]
    basis_note: str = ""
    polarity: str | None = None
    #: The corner the bound was evaluated at, and the lobe it produced, so a
    #: receipt says what the residual was compared against. ``None`` on a record
    #: that has not been through the bound.
    checked_at_fc_hz: float | None = None
    lobe_us: float | None = None

    @property
    def polarity_sign(self) -> int | None:
        """The pin in the MEASUREMENT frame, or ``None`` when unpinned."""
        if self.polarity is None:
            return None
        return -1 if self.polarity == POLARITY_INVERT else 1

    @property
    def residual_us(self) -> float:
        """How far this prescription leaves the drivers from the basis's answer.

        The quantity the bound is expressed in: ``0.0`` prescribes exactly what
        the measurement says, and the sign says which driver is left early.
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

    ``bool`` is refused explicitly (it is an ``int``, and ``float(True)`` is
    ``1.0``); strings are refused because ``float("-450")`` succeeds.
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

    Shared whole between the request gate and the durable read-back, so gate
    policy is the only difference between them. Under ``read_back`` a record
    naming NEITHER ``kind`` nor ``artifact_schema_version`` is the envelope-less
    shape prior releases persisted, and reads as this build's own kind and
    version 1; a record naming either field is refused normally.
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


#: The basins a round may be pinned to: the candidate's two polarity ACTIONS,
#: deliberately without ``POLARITY_REVIEW``.
_PINNABLE_POLARITIES = frozenset({POLARITY_KEEP, POLARITY_INVERT})


def _read_polarity(value: Any) -> str | None:
    """The optional basin pin, strictly, or ``None`` for the automatic path.

    ``None`` and absent are one answer here, unlike ``delay_us``.
    """
    if value is None:
        return None
    # ``isinstance`` before membership: a JSON list is unhashable, so
    # ``value in frozenset`` would raise TypeError past every refusal handler.
    if not isinstance(value, str) or value not in _PINNABLE_POLARITIES:
        raise AlignmentPrescriptionRefused(
            PRESCRIPTION_POLARITY_INVALID,
            f"polarity must be one of {sorted(_PINNABLE_POLARITIES)} or absent, "
            f"got {value!r}",
        )
    return value


def _optional_number(value: Any) -> float | None:
    """A finite number, or ``None`` — never a raise.

    These fields are the GATE's own record of what it checked, not a
    requester's claim, so an unreadable one is missing context rather than a
    malformed prescription.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def read_alignment_prescription(
    raw: Mapping[str, Any] | None,
    *,
    fc_hz: float | None,
    declared_bounds_us: tuple[float, float] | None,
    way_count: int | None = None,
) -> AlignmentPrescription | None:
    """THE request gate, and the one derivation of the lobe bound.

    ``None`` when the request carries no prescription — the automatic path,
    untouched. Otherwise a validated :class:`AlignmentPrescription`, or
    :class:`AlignmentPrescriptionRefused` naming which gate said no.

    ``declared_bounds_us`` is the PRESET's own unsigned delay-magnitude window,
    already margin-expanded; the caller derives it from
    ``crossover_v2_flow.alignment_delay_search_bounds_us`` because this module
    may not import the flow. ``None`` means the preset declares no window.
    Required and undefaulted: it is the only bound here that does not rest on a
    number the requester supplied.

    ``fc_hz`` is the corner THIS round runs at, the half-period of which is the
    bound. The bound is INCLUSIVE — exactly half a period from the basis is
    legal — so a round's legality does not turn on float noise in the corner.
    ``way_count`` ``1`` is a ``full_range_passive`` speaker with nothing to
    align; ``None`` means the caller did not state it and leaves the gate as-is.
    """
    if raw is None:
        return None
    # Before the parse and the corner: on a way-1 speaker any other answer
    # sends a prescriber to re-derive a number that cannot exist.
    if way_count == 1:
        raise AlignmentPrescriptionRefused(
            ALIGNMENT_NO_CROSSOVER_REGION,
            "this speaker is full_range_passive (way-1): it has no crossover "
            "region, so there is no handoff for a delay to align",
        )
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
    # The HARDWARE's bound first, then the measurement's: the two send an
    # operator to different places (re-declare the region vs re-derive
    # the basis).
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

    Shape and provenance only — the bound has one owner, and it is the request
    gate; re-applying it here could only refuse a round that really ran.
    Anything unreadable is ``None`` plus one WARNING.
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
    """What a prescriber must send to pin the inter-driver delay, and where."""
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
