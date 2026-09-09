# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WHICH members of one bass family to adopt, prescribed from outside.

This door owns the bass class: a margin policy, the target ids adopted from
the round's ``bass_fit.json``, and the order the gates run in. The FAMILY is
:mod:`jasper.bass_extension.seat_fit`'s — every corner, transform and boost
figure comes from the fit, never from the document — and the admission is
:mod:`jasper.bass_extension.candidate_field`'s.

The hard stop is not the prescriber's to move: a target with
``boost_headroom_db > 0`` is admissible only with BOTH protection evidences
attached (the bass/ladder program's evidence for its level, and the limiter
bench's bundle). Neither artifact exists yet, so a boosted target refuses
today — the intended state — and the natural target, which spends nothing, is
always adopted. The prescriber never chooses how loud a target may play: every
``max_level_db`` here is read off the ladder evidence, never off the document.

Shape and posture are :mod:`.blend_prescription`'s, and what the doors share
is imported from it rather than restated, exactly as :mod:`.room_prescription`
does: the prohibited-key walk, the intake readers, the refusal values whose
meaning is identical, and the exception class.

`See ADR-0259` section 1 (layer order), ADR-0257 section 3 (one headroom
budget) and ADR-0260 section 3 (the protection basis).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jasper.active_speaker._common import require_sha256_hex
from jasper.bass_extension.candidate_field import (
    BASS_EXTENSION_CANDIDATE_FIELD,
    EVIDENCE_PASS,
    NATURAL_TARGET_ID,
    BassCandidateFieldError,
    validate_bass_extension_field,
)
from jasper.bass_extension.targets import MARGINS

from .blend_prescription import (
    EXECUTION_BOUNDARY,
    PACKET_FINGERPRINT_FIELD,
    PRESCRIPTION_PACKET_MISMATCH,
    PRESCRIPTION_PROHIBITED_FIELD,
    PRESCRIPTION_SCHEMA_UNSUPPORTED,
    PROHIBITED_PRESCRIPTION_KEYS,
    RATIONALE_HELP,
    BlendPrescriptionRefused,
    find_prohibited_keys,
    # Renamed only to stay distinct from this module's own identifiers: the
    # VALUES are that door's, which is what makes one vocabulary cover both.
    BLEND_PRESCRIPTION_MALFORMED as PRESCRIPTION_MALFORMED,
    BLEND_PRESCRIPTION_PROVENANCE_MISSING as PRESCRIPTION_PROVENANCE_MISSING,
    # Shared with the blend door rather than re-typed: this door raises the
    # same exception class and refuses under both readers' own values.
    _prescriber,
    _rationale,
    _refuse,
)

__all__ = [
    "BASS_PRESCRIPTION_KIND",
    "BASS_PRESCRIPTION_REFUSAL_REASONS",
    "BASS_PRESCRIPTION_SCHEMA_VERSION",
    "FIT_UNAVAILABLE",
    "MARGIN_MISMATCH",
    "OWNER_MISMATCH",
    "OWNER_UNRESOLVED",
    "PROTECTION_MISSING",
    "TARGET_NOT_IN_FAMILY",
    "BassPrescription",
    "BassPrescriptionRefused",
    "bass_fit_body",
    "bass_prescription_response_format",
    "bass_prescription_to_candidate_fields",
    "read_bass_prescription",
]


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

#: A proposal naming a version this build does not speak is refused, never
#: best-effort parsed.
BASS_PRESCRIPTION_SCHEMA_VERSION = 1

#: The ``kind`` discriminator, and what the CLI switches its evidence on.
BASS_PRESCRIPTION_KIND = "jts_bass_prescription"


# --------------------------------------------------------------------------- #
# refusal vocabulary — closed, by slug, never by prose
# --------------------------------------------------------------------------- #

#: No ``bass_fit.json``, or one this door cannot read a family out of. The
#: family is the evidence: without it there is nothing to adopt FROM.
FIT_UNAVAILABLE = "bass_prescription_fit_unavailable"
#: The document names a margin policy the family was not generated under. Every
#: boost figure in the fit is that policy's, so re-run bass-fit rather than
#: reinterpreting the numbers under another one.
MARGIN_MISMATCH = "bass_prescription_margin_mismatch"
#: A target id the family does not contain.
TARGET_NOT_IN_FAMILY = "bass_prescription_target_not_in_family"
#: THE hard stop: a target spending boost with no ladder and limiter evidence.
PROTECTION_MISSING = "bass_prescription_protection_missing"
#: The fit was taken for a different bass owner than the round declares.
OWNER_MISMATCH = "bass_prescription_owner_mismatch"
#: Nothing could say which driver owns the bass, so the fit cannot be checked
#: against the speaker at all — the median's sibling in the room door.
OWNER_UNRESOLVED = "bass_prescription_owner_unresolved"

BASS_PRESCRIPTION_REFUSAL_REASONS = frozenset({
    PRESCRIPTION_MALFORMED,
    PRESCRIPTION_SCHEMA_UNSUPPORTED,
    PRESCRIPTION_PROVENANCE_MISSING,
    PRESCRIPTION_PROHIBITED_FIELD,
    PRESCRIPTION_PACKET_MISMATCH,
    FIT_UNAVAILABLE,
    MARGIN_MISMATCH,
    TARGET_NOT_IN_FAMILY,
    PROTECTION_MISSING,
    OWNER_MISMATCH,
    OWNER_UNRESOLVED,
})

#: One refusal class for every door, so the CLI's one handler and every
#: ``except`` on the seam keep working.
BassPrescriptionRefused = BlendPrescriptionRefused

#: Top-level fields a proposal may carry. Anything else is refused rather than
#: ignored: a misspelled ``targets`` would otherwise leave the gate accepting
#: an empty adoption.
_PRESCRIPTION_FIELDS = frozenset({
    "artifact_schema_version",
    "kind",
    PACKET_FINGERPRINT_FIELD,
    "prescriber",
    "margin_policy_name",
    "targets",
    "rationale",
})


# --------------------------------------------------------------------------- #
# the prescription
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BassPrescription:
    """An adopted subset of one fitted family, and what admits each member."""

    #: The policy the family was generated under, echoed and re-checked.
    margin_policy_name: str
    #: Adopted ids in FAMILY order, natural always last — appended when the
    #: document omits it, because a family with no natural member has no rung
    #: this speaker may fall back to.
    target_ids: tuple[str, ...]
    packet_fingerprint: str
    prescriber_model: str
    prescriber_operator: str
    #: The fit document this answered, content-addressed, and the round it
    #: belongs to: the candidate field's basis.
    bass_fit_sha256: str
    round_id: str
    adapter_id: str
    owner_role: str
    effective_plant: Mapping[str, Any]
    #: The fit's own rows for the adopted ids, each carrying the protection
    #: this door attached and the level that protection measured.
    rungs: tuple[Mapping[str, Any], ...]
    #: The prescriber's own words. NEVER parsed for behaviour.
    rationale: str = ""
    rationale_dropped_chars: int | None = None
    #: The receipt's attribution key, spelled as every other class spells it.
    prescription_class: str = "bass"
    #: This class prescribes no biquads of its own — the family's transforms
    #: are the fit's. Present because the spool and the printer read it.
    filters: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """The receipt's view: what was adopted, and what admits it."""
        return {
            "artifact_schema_version": BASS_PRESCRIPTION_SCHEMA_VERSION,
            "kind": BASS_PRESCRIPTION_KIND,
            "prescription_class": self.prescription_class,
            "margin_policy_name": self.margin_policy_name,
            "targets": list(self.target_ids),
            "adapter_id": self.adapter_id,
            "owner_role": self.owner_role,
            "effective_plant": dict(self.effective_plant),
            "rungs": [dict(rung) for rung in self.rungs],
            "round_id": self.round_id,
            "bass_fit_sha256": self.bass_fit_sha256,
            PACKET_FINGERPRINT_FIELD: self.packet_fingerprint,
            "prescriber": {
                "model": self.prescriber_model,
                "operator": self.prescriber_operator,
            },
            "rationale": self.rationale,
            "rationale_dropped_chars": self.rationale_dropped_chars,
        }


def bass_prescription_response_format() -> dict[str, Any]:
    """The contract a bass prescriber must satisfy, as data.

    One owner for the instructions and the gate, so the two cannot describe
    different shapes. A PURE CONSTANT: nothing measured or household-authored
    reaches it.
    """
    return {
        "artifact_schema_version": BASS_PRESCRIPTION_SCHEMA_VERSION,
        "kind": "jts_bass_prescription_contract",
        "required_top_level": {
            "artifact_schema_version": BASS_PRESCRIPTION_SCHEMA_VERSION,
            "kind": BASS_PRESCRIPTION_KIND,
            PACKET_FINGERPRINT_FIELD: (
                "copy the packet_fingerprint of the evidence packet you were "
                "given; a prescription naming a different packet is refused"
            ),
            "prescriber": {
                "model": "the model that authored this",
                "operator": "the person who ran it",
            },
            "margin_policy_name": (
                "the margin policy the family in bass_fit.json was generated "
                f"under, one of {sorted(MARGINS)}; naming another is refused "
                "rather than reinterpreted, because every boost figure in that "
                "family is the generating policy's"
            ),
            "targets": (
                "the target_ids to adopt, from that family and no other list. "
                f"The {NATURAL_TARGET_ID!r} target is adopted whether or not "
                "you name it"
            ),
        },
        "optional_top_level": {"rationale": RATIONALE_HELP},
        "you_choose_members_not_levels": (
            "which members of the fitted family this speaker carries is the "
            "choice on offer. How loud any of them may play is measured, not "
            "argued: every max_level_db comes from the ladder evidence"
        ),
        "bounds": {
            "boost_needs_both_evidences": (
                "a target whose boost_headroom_db is above zero is admissible "
                "only with a passing ladder evidence for its own level AND a "
                "limiter evidence; without both it is refused, not derated"
            ),
            "family_is_the_fit": (
                "targets, corners, transforms and boosts come from "
                "bass_fit.json; this document names ids and nothing else"
            ),
        },
        "refusal_reasons": sorted(BASS_PRESCRIPTION_REFUSAL_REASONS),
        "prohibited_keys": sorted(PROHIBITED_PRESCRIPTION_KEYS),
        "execution_boundary": EXECUTION_BOUNDARY,
    }


# --------------------------------------------------------------------------- #
# the request gate
# --------------------------------------------------------------------------- #


def _parse_targets(raw: Any) -> tuple[str, ...]:
    """The adopted ids' SHAPE; the family decides whether they exist."""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        _refuse(
            PRESCRIPTION_MALFORMED,
            "a bass prescription must state targets as a list of target ids",
        )
    ids: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            _refuse(PRESCRIPTION_MALFORMED, "each target must be a non-blank id")
        target_id = entry.strip()
        if target_id in ids:
            _refuse(
                PRESCRIPTION_MALFORMED, f"target {target_id!r} is named more than once"
            )
        ids.append(target_id)
    return tuple(ids)


def _parse_prescription(
    raw: Mapping[str, Any],
) -> tuple[tuple[str, ...], str, str, str, str, str, int]:
    """Shape, identity and provenance — and none of the bounds."""
    if not isinstance(raw, Mapping):
        _refuse(
            PRESCRIPTION_MALFORMED,
            f"a prescription must be a mapping, got {type(raw).__name__}",
        )
    # BEFORE the unknown-field check: every prohibited key is also an unknown
    # one, so checking shape first would report a prescriber reaching for
    # `volume_db` as a typo.
    prohibited = sorted(set(find_prohibited_keys(raw)))
    if prohibited:
        _refuse(
            PRESCRIPTION_PROHIBITED_FIELD,
            f"a prescription may not name {', '.join(prohibited)}: it supplies "
            "numbers into a fixed shape, never configuration, coefficients, or "
            "a per-role value",
            prohibited=prohibited,
        )
    unknown = sorted(set(raw) - _PRESCRIPTION_FIELDS)
    if unknown:
        _refuse(
            PRESCRIPTION_MALFORMED,
            f"unknown prescription field(s): {', '.join(unknown)}",
        )
    if raw.get("kind") != BASS_PRESCRIPTION_KIND:
        _refuse(
            PRESCRIPTION_MALFORMED,
            f"a prescription must name kind={BASS_PRESCRIPTION_KIND!r}, got "
            f"{raw.get('kind')!r}",
        )
    version = raw.get("artifact_schema_version")
    if version != BASS_PRESCRIPTION_SCHEMA_VERSION:
        _refuse(
            PRESCRIPTION_SCHEMA_UNSUPPORTED,
            "this build speaks bass prescription schema "
            f"{BASS_PRESCRIPTION_SCHEMA_VERSION}, got {version!r}",
            supported=BASS_PRESCRIPTION_SCHEMA_VERSION,
        )
    fingerprint = raw.get(PACKET_FINGERPRINT_FIELD)
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        _refuse(
            PRESCRIPTION_PROVENANCE_MISSING,
            f"a prescription must echo the packet's {PACKET_FINGERPRINT_FIELD}",
        )
    margin = raw.get("margin_policy_name")
    if not isinstance(margin, str) or not margin.strip():
        _refuse(
            PRESCRIPTION_MALFORMED,
            "a bass prescription must name the margin policy its family was "
            "generated under",
        )
    model, operator = _prescriber(raw.get("prescriber"))
    rationale, dropped = _rationale(raw.get("rationale"))
    return (
        _parse_targets(raw.get("targets")),
        fingerprint.strip(),
        margin.strip(),
        model,
        operator,
        rationale,
        dropped,
    )


def bass_fit_body(document: Any) -> Any:
    """The fit inside the ``bass-fit`` view's wrapper, else the value itself.

    The view writes ``{"status": ..., "bass_fit": {...}}``; a caller holding
    either shape asks the same question of the fit.
    """
    if isinstance(document, Mapping) and isinstance(document.get("bass_fit"), Mapping):
        return document["bass_fit"]
    return document


def _family(
    bass_fit: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
    """The fit and its rungs, in the order it published them."""
    bass_fit = bass_fit_body(bass_fit)
    if not isinstance(bass_fit, Mapping):
        _refuse(
            FIT_UNAVAILABLE,
            "a bass prescription adopts members of a fitted family, and this "
            "round banked no readable bass_fit.json",
        )
    rungs = bass_fit.get("rungs")
    if not isinstance(rungs, Sequence) or isinstance(rungs, (str, bytes)) or not rungs:
        _refuse(FIT_UNAVAILABLE, "the bass fit publishes no family to adopt from")
    family: list[Mapping[str, Any]] = []
    for rung in rungs:
        target = rung.get("target") if isinstance(rung, Mapping) else None
        if not isinstance(target, Mapping) or not isinstance(
            target.get("target_id"), str
        ):
            _refuse(FIT_UNAVAILABLE, "the bass fit's family is not readable")
        family.append(rung)
    return bass_fit, tuple(family)


def _checked_owner(
    bass_fit: Mapping[str, Any], expected_owner_role: str | None
) -> str:
    """WHOSE bass this family extends, agreed by the fit and the round."""
    if not isinstance(expected_owner_role, str) or not expected_owner_role.strip():
        _refuse(
            OWNER_UNRESOLVED,
            "nothing this round declares says which driver owns the bass, so "
            "a family fitted for one cannot be checked against this speaker",
        )
    owner_role = bass_fit.get("owner_role")
    if owner_role != expected_owner_role.strip():
        _refuse(
            OWNER_MISMATCH,
            f"this fit extends {owner_role!r} and this speaker's bass owner is "
            f"{expected_owner_role.strip()!r}",
            fit_owner_role=owner_role,
            expected_owner_role=expected_owner_role.strip(),
        )
    return expected_owner_role.strip()


def _adopted_rungs(
    family: Sequence[Mapping[str, Any]],
    target_ids: Sequence[str],
    *,
    ladder: Mapping[str, Mapping[str, Any]],
    limiter: Mapping[str, Any] | None,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """One candidate-field rung per adopted id, and the hard stop applied.

    ``max_level_db`` is CODE's: natural costs nothing, and a boosted rung may
    only claim the level its own ladder evidence measured.
    """
    by_id = {str(rung["target"]["target_id"]): rung for rung in family}
    unknown = [target_id for target_id in target_ids if target_id not in by_id]
    if unknown:
        _refuse(
            TARGET_NOT_IN_FAMILY,
            f"this family has no {', '.join(sorted(unknown))}; it offers "
            f"{', '.join(sorted(by_id))}",
            unknown_targets=sorted(unknown),
            family=sorted(by_id),
        )
    if NATURAL_TARGET_ID not in by_id:
        _refuse(
            FIT_UNAVAILABLE,
            f"this family names no {NATURAL_TARGET_ID!r} target, so there is "
            "no member this speaker may fall back to",
        )
    adopted = {*target_ids, NATURAL_TARGET_ID}
    ordered = [
        target_id
        for target_id in (str(rung["target"]["target_id"]) for rung in family)
        if target_id in adopted
    ]
    rungs: list[Mapping[str, Any]] = []
    for target_id in ordered:
        rung = by_id[target_id]
        target = dict(rung["target"])
        boost = target.get("boost_headroom_db")
        measured = ladder.get(target_id)
        if not isinstance(measured, Mapping) or measured.get("verdict") != EVIDENCE_PASS:
            measured = None
        if boost:
            missing = [
                name
                for name, evidence in (("ladder", measured), ("limiter", limiter))
                if evidence is None
            ]
            if missing:
                _refuse(
                    PROTECTION_MISSING,
                    f"target {target_id!r} spends {boost} dB of boost, and a "
                    "boosted target may only play with protection measured for "
                    f"it: {', '.join(missing)} missing",
                    target_id=target_id,
                    boost_headroom_db=boost,
                    missing_evidence=missing,
                )
        rungs.append({
            "target": target,
            # The fit reports the rung's transform gain; the LEVEL is measured.
            "lt_boost_db": rung.get("lt_boost_db", 0.0),
            # Natural spends nothing, so it costs nothing; every other rung's
            # level is the one its ladder evidence measured.
            "max_level_db": (
                0.0
                if measured is None or target_id == NATURAL_TARGET_ID
                else measured.get("max_level_db")
            ),
            "protection": {
                "ladder": None if measured is None else dict(measured),
                "limiter": None if limiter is None else dict(limiter),
            },
        })
    return tuple(rungs), tuple(ordered)


def read_bass_prescription(
    raw: Mapping[str, Any],
    *,
    packet_fingerprint: Any,
    bass_fit: Mapping[str, Any] | None,
    bass_fit_sha256: str | None,
    round_id: str | None,
    ladder: Mapping[str, Mapping[str, Any]],
    limiter: Mapping[str, Any] | None,
    expected_owner_role: str | None,
) -> BassPrescription:
    """THE request gate. One point, and the one place every bound is applied.

    A validated :class:`BassPrescription`, or :class:`BassPrescriptionRefused`
    naming which gate said no. The gate order is deliberate: each stage sends a
    prescriber somewhere different.
    """
    (
        target_ids, echoed, margin, model, operator, rationale, dropped,
    ) = _parse_prescription(raw)
    if not isinstance(packet_fingerprint, str) or not packet_fingerprint:
        _refuse(
            PRESCRIPTION_PROVENANCE_MISSING,
            "this round's packet states no fingerprint, so nothing can say "
            "which evidence this prescription answered",
        )
    if echoed != packet_fingerprint:
        _refuse(
            PRESCRIPTION_PACKET_MISMATCH,
            f"this prescription answers packet {echoed[:16]} and this round's "
            f"packet is {packet_fingerprint[:16]}",
            echoed=echoed,
            expected=packet_fingerprint,
        )
    fit, family = _family(bass_fit)
    if margin not in MARGINS:
        _refuse(
            MARGIN_MISMATCH,
            f"margin_policy_name must be one of {sorted(MARGINS)}, got {margin!r}",
        )
    if fit.get("margin") != margin:
        _refuse(
            MARGIN_MISMATCH,
            f"this family was generated under {fit.get('margin')!r} and the "
            f"prescription names {margin!r}; every boost figure in it is the "
            "generating policy's, so re-run bass-fit under the policy you want",
            fit_margin=fit.get("margin"),
            prescribed_margin=margin,
        )
    owner_role = _checked_owner(fit, expected_owner_role)
    rungs, ordered = _adopted_rungs(
        family, target_ids, ladder=ladder, limiter=limiter,
    )
    try:
        digest = require_sha256_hex(bass_fit_sha256, "bass_fit_sha256", ValueError)
    except ValueError as exc:
        _refuse(FIT_UNAVAILABLE, str(exc))
    if not isinstance(round_id, str) or not round_id.strip():
        _refuse(
            FIT_UNAVAILABLE,
            "a bass prescription's basis names the round its family was fitted "
            "in, and this invocation could not name one",
        )
    return BassPrescription(
        margin_policy_name=margin,
        target_ids=ordered,
        packet_fingerprint=packet_fingerprint,
        prescriber_model=model,
        prescriber_operator=operator,
        bass_fit_sha256=digest,
        round_id=round_id.strip(),
        adapter_id=str(fit.get("adapter_id") or ""),
        owner_role=owner_role,
        effective_plant=dict(fit.get("effective_plant") or {}),
        rungs=rungs,
        rationale=rationale,
        rationale_dropped_chars=dropped,
    )


def bass_prescription_to_candidate_fields(
    prescription: BassPrescription | None,
    *,
    owner_channels: Sequence[int],
) -> dict[str, Any]:
    """The candidate fields a validated prescription contributes.

    The value must enter at CANDIDATE-BUILD time:
    ``MeasuredCrossoverCandidate.fingerprint`` is ``field(init=False)``, so a
    prescription applied after construction is either invisible to the
    fingerprint or refused as ``candidate_tampered``. ``{}`` for ``None``, so a
    caller can splat it unconditionally.

    It RE-ASKS the field validator rather than trusting its input was gated: a
    :class:`BassPrescription` can be constructed directly, and the value it
    composes is fingerprinted at construction and never re-checked after, so
    "an inadmissible family cannot populate ``bass_extension``" is a property
    of this function rather than of the current call graph.
    """
    if prescription is None:
        return {}
    field = {
        "owner": {
            "role": prescription.owner_role,
            "channels": [int(channel) for channel in owner_channels],
        },
        "adapter_id": prescription.adapter_id,
        "margin_policy_name": prescription.margin_policy_name,
        "effective_plant": {
            key: prescription.effective_plant.get(key)
            for key in ("f0_hz", "q0", "source")
        },
        "rungs": [dict(rung) for rung in prescription.rungs],
        "basis": {
            "round_id": prescription.round_id,
            "bass_fit_sha256": prescription.bass_fit_sha256,
        },
    }
    try:
        return {BASS_EXTENSION_CANDIDATE_FIELD: validate_bass_extension_field(field)}
    except BassCandidateFieldError as exc:
        _refuse(PRESCRIPTION_MALFORMED, f"{exc.reason}: {exc.detail}")
