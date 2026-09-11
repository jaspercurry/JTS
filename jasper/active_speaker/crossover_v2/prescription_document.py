# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Judge one document atomically; candidate_parts owns layer resolution (ADR-0303)."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jasper.active_speaker.candidate_bank import BankedCandidate, CandidateBankRefusal
from jasper.active_speaker.candidate_parts import compose_candidate
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate, MeasuredCrossoverCandidateError
from jasper.active_speaker.profile import SIDES_BY_LAYOUT
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor

from . import alignment_prescription as alignment
from . import blend_prescription as blend
from . import driver_prescription as driver
from . import room_prescription as room
from . import topology_prescription as topology
from .evidence_packet import packet_feature_classifications, packet_incumbent_linearization, packet_positional_evidence
from .prescription_contract import contract_digests, contract_json, prescription_contracts
from .refusal_copy import refusal_copy_for

DOCUMENT_KIND = "jts_prescription"
SECTION_KINDS = {
    "driver": driver.DRIVER_PRESCRIPTION_KIND,
    "blend": blend.PRESCRIPTION_KIND,
    "alignment": alignment.ALIGNMENT_PRESCRIPTION_KIND,
    "topology": topology.TOPOLOGY_PRESCRIPTION_KIND,
    "room": room.ROOM_PRESCRIPTION_KIND,
    "bass": None,
}


class PrescriptionDocumentRefused(ValueError):
    def __init__(self, code: str, section: str | None, error: str, *, evidence: Mapping[str, Any] | None = None):
        super().__init__(error)
        self.code, self.section, self.error = code, section, error
        self.evidence = dict(evidence or {})

    def to_dict(self) -> dict[str, Any]:
        _, action = refusal_copy_for(self.code)
        return {"ok": False, "code": self.code, "section": self.section,
                "next_action": action, "error": self.error,
                **({"evidence": self.evidence} if self.evidence else {})}


@dataclass(frozen=True)
class PrescriptionEvidence:
    sources: Mapping[str, Any] = field(default_factory=dict)
    packet: Mapping[str, Any] = field(default_factory=dict)
    room_median_sha256: str = ""
    round_id: str = ""


def read_prescription_document(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("kind") != DOCUMENT_KIND:
        raise PrescriptionDocumentRefused("prescription_kind_unknown", None, "expected a jts_prescription document")
    if type(raw.get("schema")) is not int or raw["schema"] != 1:
        raise PrescriptionDocumentRefused("prescription_schema_unsupported", None, "unsupported document schema")
    if set(raw) - {"kind", "schema", "base", "sections", "rationale"}:
        raise PrescriptionDocumentRefused("prescription_malformed", None, "unknown document fields")
    if not isinstance(raw.get("base"), str) or not raw["base"].strip():
        raise PrescriptionDocumentRefused("composition_base_required", None, "name a base fingerprint or saved")
    if not isinstance(raw.get("rationale"), str) or not isinstance(raw.get("sections"), Mapping):
        raise PrescriptionDocumentRefused("prescription_malformed", None, "sections must be an object and rationale must be text")
    for name, value in raw["sections"].items():
        if name not in SECTION_KINDS:
            raise PrescriptionDocumentRefused("prescription_section_unknown", str(name), "unknown section")
        if value is not None and not isinstance(value, Mapping):
            raise PrescriptionDocumentRefused("prescription_malformed", name, "section must be an object or null")
        prohibited = blend.find_prohibited_keys(value) if name in {"driver", "blend", "room"} else []
        if prohibited:
            raise PrescriptionDocumentRefused(blend.PRESCRIPTION_PROHIBITED_FIELD, name, "prohibited fields", evidence={"fields": prohibited})
    return raw


def _judge_section(name: str, raw: Mapping[str, Any], *, base: BankedCandidate,
                   contracts: Mapping[str, Any], evidence: PrescriptionEvidence,
                   fc_hz: float | None) -> tuple[Any, Mapping[str, Any]]:
    packet = dict(evidence.packet)
    speaker = contracts["speaker"]
    if name == "driver":
        driver.check_driver_document_size(json.dumps(raw).encode())
        prescription = driver.read_driver_prescription(
            raw, packet_fingerprint=packet.get("packet_fingerprint"),
            passbands_hz=speaker["driver"]["bounds"]["passbands_hz"],
            classifications=packet_feature_classifications(packet),
            incumbent_filters=packet_incumbent_linearization(packet),
        )
        assert prescription is not None
        fields = driver.driver_prescription_to_candidate_fields(prescription, fitted=None)
        return {**fields, "role_attenuations_db": dict(prescription.pinned_trim_db)}, prescription.to_dict()
    if name == "blend":
        prescription_blend = blend.read_blend_prescription(
            raw, packet_fingerprint=packet.get("packet_fingerprint"),
            band_hz=speaker["blend"]["bounds"]["band_hz"],
            positional_evidence=packet_positional_evidence(packet),
        )
        assert prescription_blend is not None
        return blend.blend_prescription_to_candidate_fields(prescription_blend)["blend_correction"], prescription_blend.to_dict()
    if name == "alignment":
        pin = alignment.read_alignment_prescription(
            raw, fc_hz=fc_hz,
            declared_bounds_us=speaker["alignment"]["bounds"]["declared_delay_magnitude_us"],
            way_count=base.candidate.source_preset.way_count,
        )
        assert pin is not None
        return pin, pin.to_dict()
    if name == "topology":
        bounds = speaker["topology"]["bounds"]
        band = bounds["fc_hz"] or (None, None)
        topology_pin = topology.read_topology_prescription(
            raw, declared_floor_hz=band[0], lower_driver_ceiling_hz=band[1],
            minimum_slope_db_per_octave=bounds["minimum_slope_db_per_octave"],
            beaming_ceiling_hz=None, way_count=base.candidate.source_preset.way_count,
        )
        assert topology_pin is not None
        return topology_pin, topology_pin.to_dict()
    if name == "room":
        room_pin = room.read_room_prescription(
            raw, room_median=room.read_room_median(evidence.sources.get("room_median", {})),
            room_median_sha256=evidence.room_median_sha256, round_id=evidence.round_id,
            sides=SIDES_BY_LAYOUT[base.candidate.source_preset.channel_map.layout],
        )
        assert room_pin is not None
        return room.room_prescription_to_candidate_fields(room_pin)["room_correction"], room_pin.to_dict()
    if name == "bass":
        return validate_dynamic_bass_descriptor(raw), dict(raw)
    raise PrescriptionDocumentRefused("prescription_kind_unknown", name, "unknown section kind")


def judge_prescription_document(raw: Any, *, base: BankedCandidate,
                               evidence: PrescriptionEvidence | None = None) -> MeasuredCrossoverCandidate:
    document = read_prescription_document(raw)
    if document["base"] != "saved" and document["base"] != base.fingerprint:
        raise PrescriptionDocumentRefused("composition_base_mismatch", None, "document and resolved base differ")
    evidence = evidence or PrescriptionEvidence()
    contracts = prescription_contracts(**{**evidence.sources, "candidate": base.candidate.to_dict()})
    selected: dict[str, Any] = {}
    judged: dict[str, Any] = {}
    fc_hz = contracts["speaker"]["alignment"]["bounds"]["fc_hz"]
    for name in ("topology", "driver", "blend", "alignment", "room", "bass"):
        if name not in document["sections"]:
            continue
        section = document["sections"][name]
        if not section:
            selected[name] = None
            continue
        kind = SECTION_KINDS[name]
        if kind is not None:
            contract = contracts[name] if name == "room" else contracts["speaker"][name]
            section = {"kind": kind, "artifact_schema_version": contract["schema"]["properties"]["artifact_schema_version"]["const"],
                       **({"rationale": document["rationale"]} if name in {"driver", "blend", "room"} else {}),
                       **section}
            if section["kind"] != kind:
                raise PrescriptionDocumentRefused("prescription_kind_unknown", name, "section kind does not match its name")
        try:
            selected[name], judged[name] = _judge_section(
                name, section, base=base, contracts=contracts, evidence=evidence, fc_hz=fc_hz,
            )
            if name == "topology":
                fc_hz = selected[name].fc_hz
        except (blend.BlendPrescriptionRefused, alignment.AlignmentPrescriptionRefused,
                topology.TopologyPrescriptionRefused) as exc:
            raise PrescriptionDocumentRefused(exc.reason, name, exc.detail, evidence=getattr(exc, "evidence", {})) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise PrescriptionDocumentRefused("bass_extension_invalid" if name == "bass" else "prescription_malformed", name, str(exc)) from exc
    try:
        return compose_candidate(
            base, {}, sections=selected, rationale=document["rationale"],
            room_prescription_sha256=(blend.prescription_sha256(contract_json(document["sections"]["room"]).encode())
                                      if selected.get("room") else ""),
            room_measured_basis=judged.get("room", {}).get("measured_basis"),
            evidence={"packet_fingerprint": evidence.packet.get("packet_fingerprint"),
                      "contracts": contract_digests(contracts), "prescriptions": judged},
        )
    except (CandidateBankRefusal, MeasuredCrossoverCandidateError) as exc:
        raise PrescriptionDocumentRefused(exc.code, "topology" if exc.code == "composition_topology_required" else None, exc.detail) from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise PrescriptionDocumentRefused("composition_invalid", None, str(exc)) from exc
