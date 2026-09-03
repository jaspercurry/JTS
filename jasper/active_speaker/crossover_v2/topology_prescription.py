# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE crossover corner and its order, PINNED for one round.

Pure functions, no I/O, no session. A pinned round SOLVES at the named
topology — trims, linearization and delay re-solve underneath it — so nothing
downstream models a corner the graph does not carry.

Every bound here is an ADMISSIBILITY bound (a declaration already made about
this hardware), not an excursion from a basis, and each is asked of the module
that owns it: ``order`` of ``SUPPORTED_LR_ORDERS``, ``fc_hz`` of
:func:`.fc_sweep._fc_rejection`, and ``order * 6`` dB/octave of the protected
role's PUBLISHED slope condition. That slope check lives here because nothing
downstream enforces a crossover slope above 12 dB/octave, so a published
condition not checked here is not checked anywhere. Polarity is refused
outright — it is :mod:`.alignment_prescription`'s field, and one knob may not
have two doors. Refusals raise and are never clamped or inherited.
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
    "TOPOLOGY_NO_CROSSOVER_REGION",
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
#: emits, so an empty receipt slot stays distinguishable from a mangled one.
PRESCRIPTION_UNREADABLE_EVENT = "correction.crossover_v2_topology_prescription_unreadable"

#: The request-body key a prescription arrives under.
TOPOLOGY_PRESCRIPTION_KEY = "topology_prescription"

#: A document naming another version is refused, never best-effort parsed.
TOPOLOGY_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, distinct from every sibling class's own string.
TOPOLOGY_PRESCRIPTION_KIND = "jts_crossover_topology_prescription"

#: Stamped by the gate onto every accepted prescription: a topology pin has no
#: measured ranking authority, and a receipt that did not say so would read
#: like one that did.
TOPOLOGY_AUTHORITY_OPERATOR_PINNED = "operator_pinned_no_measured_ranking"

#: The closed refusal vocabulary: a caller branches on a code, never on prose.
TOPOLOGY_MALFORMED = "topology_malformed"
TOPOLOGY_FC_INVALID = "topology_fc_invalid"
#: A way-1 speaker has no corner to re-topologize; its own reason, on
#: :data:`~.alignment_prescription.ALIGNMENT_NO_CROSSOVER_REGION`'s rule.
TOPOLOGY_NO_CROSSOVER_REGION = "topology_no_crossover_region"
TOPOLOGY_ORDER_INVALID = "topology_order_invalid"
#: An order the graph cannot emit. Its own reason so a well-formed but
#: unsupported integer sends a prescriber to the supported set, not the shape.
TOPOLOGY_ORDER_UNSUPPORTED = "topology_order_unsupported"
TOPOLOGY_PROVENANCE_MISSING = "topology_provenance_missing"
#: The published-slope bound: only a manufacturer's PUBLISHED condition may
#: refuse a corner, never the commissioning figure derived from it (#2874).
#: The code is kept across that narrowing so receipts banked either side of it
#: stay searchable together.
TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT = "topology_slope_below_declared_requirement"
TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED = "topology_prescription_schema_unsupported"
TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    TOPOLOGY_MALFORMED,
    TOPOLOGY_FC_INVALID,
    TOPOLOGY_NO_CROSSOVER_REGION,
    TOPOLOGY_ORDER_INVALID,
    TOPOLOGY_ORDER_UNSUPPORTED,
    TOPOLOGY_PROVENANCE_MISSING,
    TOPOLOGY_SLOPE_BELOW_DECLARED_REQUIREMENT,
    TOPOLOGY_PRESCRIPTION_SCHEMA_UNSUPPORTED,
    # The AUTOMATIC path's own two frequency codes, reused rather than
    # re-spelled: a pin and a proposal must be admissible on identical terms.
    FC_REJECT_BELOW_DECLARED_FLOOR,
    FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
})

#: The field names a prescription may carry. Anything else is refused rather
#: than ignored: a misspelled ``basis_artifact`` would leave a pinned round
#: claiming a basis nobody declared. ``polarity`` is refused BY THIS SET — it
#: is the sibling module's field.
_PRESCRIPTION_FIELDS = frozenset({
    "kind",
    "artifact_schema_version",
    "fc_hz",
    "order",
    "basis_artifacts",
    "basis_note",
    # Written BY the gate, accepted on the way back in so a durable block
    # round-trips through this parser. A request that supplies them is
    # harmless: the gate overwrites each with what it actually checked.
    "authority",
    "checked_against_floor_hz",
    "checked_against_ceiling_hz",
    "checked_against_slope_db_per_octave",
    "beaming_ceiling_hz",
    "recommended_slope_db_per_octave",
    "slope_db_per_octave",
})

#: Field names this build once WROTE onto a receipt and no longer speaks.
#: Dropped on the durable read-back only, never at the request gate: a banked
#: receipt must stay readable across the deploy that retired the field, while a
#: freshly-authored pin naming one learns at the tap.
#: ``checked_against_search_band_hz`` recorded the crossover search band #2870
#: deleted.
_RETIRED_PRESCRIPTION_FIELDS = frozenset({
    "checked_against_search_band_hz",
})


class TopologyPrescriptionRefused(ValueError):
    """One prescription this module would not accept, and why.

    ``reason`` is from :data:`TOPOLOGY_PRESCRIPTION_REFUSAL_REASONS`, so the
    classification travels with the raise.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class TopologyPrescription:
    """A validated crossover corner and order, and what proposed them.

    ``fc_hz`` is the one corner BOTH branches split at, and ``order`` the one
    Linkwitz-Riley order emitted for both: ``CrossoverSection`` has no per-role
    order, and a crossover whose halves had different orders would not sum.
    ``basis_artifacts`` is required; ``authority`` is stamped by the gate,
    never chosen.

    The three ``checked_against_*`` fields, ``beaming_ceiling_hz`` and
    ``recommended_slope_db_per_octave`` are the GATE's record of what it
    compared this pin to, so a receipt says what the pin CLEARED. ``None``
    means the record never went through the gate, or the bound was not
    declared. In particular a ``None`` ``checked_against_slope_db_per_octave``
    is NOT proof the maker prints no slope — a safety profile predating the
    field that carries one reads the same way.
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
    #: enforced (#1675). ``None`` means the lower driver declared no diameter —
    #: an absent prior, not a satisfied one.
    beaming_ceiling_hz: float | None = None
    #: This build's commissioning recommendation for a protective high-pass
    #: slope (``driver_protection.PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE``),
    #: DISCLOSED and never enforced: a code figure, which #2874 bars from
    #: refusing anything. Stamped whatever the manufacturer published; ``None``
    #: only on a record that has not been through the gate.
    recommended_slope_db_per_octave: float | None = None

    @property
    def slope_db_per_octave(self) -> float:
        """What this order attenuates at, in the units declarations use.

        ``order * 6``, the same relation
        :func:`~jasper.active_speaker.branch_chain.confirmed_protection_sections`
        inverts on the way back.
        """
        return float(self.order) * 6.0

    def to_dict(self) -> dict[str, Any]:
        """The receipt's view: what was pinned, and what it cleared."""
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

    ``bool`` is refused explicitly (it is an ``int``, and ``float(True)`` is
    ``1.0``); strings are refused because ``float("4000")`` succeeds.
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

    Two steps because they send a prescriber to two different places: a
    non-integer is malformed, an unsupported integer asks for a filter this
    system does not build. A float is refused, never truncated — ``int(4.7)``
    is ``4``, a silently different candidate.
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
    requester's claim, so an unreadable one is missing context rather than a
    malformed prescription.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_prescription(
    raw: Mapping[str, Any], *, read_back: bool = False,
) -> TopologyPrescription:
    """The shape and the provenance, and NOT the bounds.

    Shared whole between the request gate and the durable read-back, so gate
    policy is the only difference between them. Under ``read_back`` a record
    naming NEITHER ``kind`` nor ``artifact_schema_version`` is the envelope-less
    shape prior releases persisted, and reads as this build's own kind and
    version 1; a record naming either field is refused normally. ``read_back``
    also DROPS a :data:`_RETIRED_PRESCRIPTION_FIELDS` entry rather than
    refusing the record — dropped, never read.
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
    """THE request gate, and the one place a bound is applied.

    ``None`` when the request carries no prescription — the automatic path,
    untouched. Otherwise a validated :class:`TopologyPrescription`, or
    :class:`TopologyPrescriptionRefused` naming which gate said no.

    Every keyword is required and undefaulted: none rests on a number the
    requester supplied, so a caller that forgot one would silently lose the
    hardware's own opinion. ``None`` is a legal VALUE for the two that admit
    it and means that bound was not declared, never a guessed default.

    ``declared_floor_hz`` and ``lower_driver_ceiling_hz`` are
    :func:`~.fc_sweep._fc_rejection`'s ``hf_hard_floor_hz`` /
    ``lower_driver_hard_ceiling_hz``, and they are the WHOLE frequency gate
    (#2870 deleted the narrower search band).
    ``minimum_slope_db_per_octave`` is the PROTECTED (upper) role's PUBLISHED
    high-pass condition and only that role's; ``None`` means none is on the
    safety profile, and the commissioning figure never stands in (#2874).

    The bounds are INCLUSIVE at every edge, so a round's legality does not turn
    on float noise. ``way_count`` ``1`` has no corner to pin; ``None`` means the
    caller did not state it.
    """
    if raw is None:
        return None
    # Before the parse and the two declared bands: a way-1 main declares no
    # crossover, so the pinned frequency is not what is wrong.
    if way_count == 1:
        raise TopologyPrescriptionRefused(
            TOPOLOGY_NO_CROSSOVER_REGION,
            "this speaker is full_range_passive (way-1): it has no crossover "
            "region, so there is no corner or order to re-topologize",
        )
    if declared_floor_hz is None or lower_driver_ceiling_hz is None:
        # The gate above already refused the shape with no second role, so
        # reaching here would admit a pin against bounds nobody supplied.
        raise TopologyPrescriptionRefused(
            TOPOLOGY_MALFORMED,
            "the role bands a corner must sit between were not declared",
        )
    prescription = _parse_prescription(raw)
    # Frequency before slope: the two send a prescriber to different places
    # (re-declare the band vs re-choose the order).
    #
    # ``fc_sweep._fc_rejection`` is the single owner of "is this corner
    # admissible for this speaker", imported by its private name deliberately —
    # a pinned corner and a declared one must be admissible on identical terms.
    # It carries no beaming term, which is #1675's ruling rather than an
    # omission; the onset rides ``beaming_ceiling_hz`` as disclosure instead.
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
        # Unreachable while ``_fc_rejection`` speaks the two codes above; the
        # alternative to naming an unhandled code is admitting a pin the shared
        # predicate refused.
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

    Shape and provenance only — the bounds have one owner, and it is the
    request gate; re-applying them here could only refuse a round that really
    ran. Anything unreadable is ``None`` plus one WARNING.
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

    Unchanged when there is no pin. Otherwise the same preset re-cornered at
    the pinned corner and order (:func:`~.fc_sweep.recornered_preset`). BOTH
    stages call this — stage 1 measures at the pin, stage 2 grades at it — so
    a round cannot be measured at one corner and graded at another.
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

    Never off a session's ``fc_hz``: this reports the crossover the reviewed
    graph actually contains. ``None`` when the candidate declares no crossover
    region — there is no corner to name. Duck-typed on
    ``source_preset.crossover_regions``; this module imports neither the
    candidate type nor the preset schema.
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
    """What a prescriber must send to pin a topology, and where to send it."""
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
