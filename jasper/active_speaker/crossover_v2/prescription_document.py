# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Judge one document atomically; candidate_parts owns layer resolution (ADR-0303)."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any

from jasper.active_speaker.candidate_bank import BankedCandidate, CandidateBankRefusal
from jasper.active_speaker.alignment_evidence import commissioning_alignment, round_alignment
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile, compose_candidate
from jasper.active_speaker.camilla_yaml import _branch_context
from jasper.active_speaker.linearization_fit import linearization_filters_by_role
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate, MeasuredCrossoverCandidateError, room_peqs_from_correction, driver_corrections,
)
from jasper.active_speaker.profile import SIDES_BY_LAYOUT
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.active_speaker import rear_calibration
from jasper.camilla_config_contract import DEFAULT_SAMPLE_RATE
from jasper import output_topology
from .topology_prescription import apply_topology_pin

from . import alignment_prescription as alignment
from . import bass_prescription as bass
from . import blend_prescription as blend
from . import driver_prescription as driver
from . import room_prescription as room
from . import topology_prescription as topology
from .capture_prediction import capture_prediction
from .forward_model import ForwardModelError
from .evidence_packet import packet_feature_classifications, packet_positional_evidence
from .prescription_contract import contract_digests, contract_json, prescription_contracts
from .refusal_copy import refusal_copy_for
from .rear_preview import preview_rear_section
from .round_captures import RoundCapturesRefused
from .round_inputs import prescription_sources, read_run_manifest, round_inputs

DOCUMENT_KIND = "jts_prescription"
SECTION_KINDS = {
    "driver": driver.DRIVER_PRESCRIPTION_KIND,
    "blend": blend.PRESCRIPTION_KIND,
    "alignment": alignment.ALIGNMENT_PRESCRIPTION_KIND,
    "topology": topology.TOPOLOGY_PRESCRIPTION_KIND,
    "room": room.ROOM_PRESCRIPTION_KIND,
    "bass": None,
    "rear_calibration": rear_calibration.KIND,
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
    sources: Mapping[str, Any] = field(default_factory=lambda: prescription_sources(None))
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


def parse_vary_axis(text: str) -> tuple[tuple[str, ...], tuple[Any, ...]]:
    paths_text, separator, values_text = text.partition("=")
    paths = tuple(path.strip() for path in paths_text.split(","))
    if not separator or not all(paths) or not all(token.strip() for token in values_text.split(",")):
        raise PrescriptionDocumentRefused("prescription_malformed", "sections", "expected PATH[,PATH...]=VALUE[,VALUE...]")
    values = []
    for token in values_text.split(","):
        try:
            value = json.loads(token)
        except json.JSONDecodeError:
            value = token.strip()
        if isinstance(value, (dict, list)):
            raise PrescriptionDocumentRefused("prescription_malformed", "sections", "axis values must be JSON scalars")
        values.append(value)
    return paths, tuple(values)


def _vary_target(sections: Mapping[str, Any], path: str) -> tuple[Any, str | int]:
    section = path.split(".")[0]
    node: Any = sections
    try:
        for component in path.split("."):
            match = re.fullmatch(r"([^.[\]]+)(?:\[([0-9]+)\])?", component)
            if match is None:
                raise ValueError("expected a dotted key with an optional list index")
            key, index = match.groups()
            parent, node = node, node[key]
            if index is not None:
                if not isinstance(node, list):
                    raise TypeError("an index requires a list")
                key = int(index)
                parent, node = node, node[key]
        return parent, key
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise PrescriptionDocumentRefused("prescription_malformed", section if section in sections else "sections",
                                          f"axis path does not resolve: {path}") from exc


def vary_document(
    document: Mapping[str, Any], axes: Sequence[tuple[tuple[str, ...], tuple[Any, ...]]],
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    seen: set[str] = set()
    for paths, _ in axes:
        for path in paths:
            _vary_target(document["sections"], path)
            if path in seen:
                section = path.split(".")[0]
                raise PrescriptionDocumentRefused("prescription_malformed", section if section in document["sections"] else "sections",
                                                  f"axis path is repeated: {path}")
            seen.add(path)
    for combination in product(*(values for _, values in axes)):
        variant = deepcopy(dict(document))
        values_by_path = {path: value for (paths, _), value in zip(axes, combination) for path in paths}
        for path, value in values_by_path.items():
            parent, key = _vary_target(variant["sections"], path)
            parent[key] = value
        yield values_by_path, variant


def _judge_section(name: str, raw: Mapping[str, Any], *, base: BankedCandidate,
                   contracts: Mapping[str, Any], evidence: PrescriptionEvidence,
                   fc_hz: float | None, selected: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
    packet = dict(evidence.packet)
    speaker = contracts["speaker"]
    if name == "driver":
        preset, _ = apply_topology_pin(selected.get("topology"), preset=base.candidate.source_preset, fc_hz=None)
        driver.check_driver_document_size(json.dumps(raw).encode())
        prescription = driver.read_driver_prescription(
            raw, packet_fingerprint=packet.get("packet_fingerprint"),
            passbands_hz=speaker["driver"]["bounds"]["passbands_hz"],
            branch_context=_branch_context(preset, driver_corrections(base.candidate)),
            room_peqs=room_peqs_from_correction(selected.get("room", base.candidate.room_correction) or {}, preset),
            classifications=packet_feature_classifications(packet),
            incumbent_filters=linearization_filters_by_role(base.candidate.linearization),
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
            beaming_ceiling_hz=bounds["beaming_ceiling_hz"], way_count=base.candidate.source_preset.way_count,
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
        return room.room_prescription_to_candidate_fields(room_pin)["room_correction"], {**room_pin.to_dict(), "measured_basis": room_pin.measured_basis}
    if name == "bass":
        bass_pin = bass.read_bass_prescription(raw, evidence=evidence.sources["bass_evidence"])
        return bass_pin.descriptor, bass_pin.to_dict()
    if name == "rear_calibration":
        document = rear_calibration.read_rear_calibration(raw, sample_rate=DEFAULT_SAMPLE_RATE)
        return document, document
    raise PrescriptionDocumentRefused("prescription_kind_unknown", name, "unknown section kind")


def _section_payload(name: str, section: Mapping[str, Any], rationale: str,
                     contracts: Mapping[str, Any]) -> Mapping[str, Any]:
    kind = SECTION_KINDS[name]
    if kind is None:
        return section
    # A rear calibration is authored whole and carries its own kind and schema;
    # read_rear_calibration refuses any extra key a prescription header adds.
    if name != "rear_calibration":
        contract = contracts[name] if name == "room" else contracts["speaker"][name]
        section = {"kind": kind, "artifact_schema_version": contract["schema"]["properties"]["artifact_schema_version"]["const"],
                   **({"rationale": rationale} if name in {"driver", "blend", "room"} else {}), **section}
    if section.get("kind") != kind:
        raise PrescriptionDocumentRefused("prescription_kind_unknown", name, "section kind does not match its name")
    return section


def _preview_emitted_graph(document: Mapping[str, Any], *, round_dir: Path,
                           base: BankedCandidate, evidence: PrescriptionEvidence,
                           capture_id: str) -> dict[str, Any]:
    composed = judge_prescription_document(document, base=base, evidence=evidence)
    section = "driver" if "driver" in document["sections"] else "blend"
    try:
        return capture_prediction(round_dir, capture_id=capture_id,
                                  candidate=composed, basis_candidate=base.candidate)
    except ForwardModelError as exc:
        raise PrescriptionDocumentRefused(exc.refusal_reason, section, str(exc), evidence=exc.detail) from exc
    except RoundCapturesRefused as exc:
        raise PrescriptionDocumentRefused(exc.reason, section, str(exc), evidence=exc.detail) from exc


_PREVIEW_ROWS: dict[str, tuple[set[str], Callable[..., dict[str, Any]]]] = {
    "rear_calibration": ({"rear_calibration"}, preview_rear_section),
    "room": ({"room"}, room.preview_room_prescription),
    "emitted_graph": ({"driver", "blend"}, _preview_emitted_graph),
}


def preview_kind(document: Mapping[str, Any]) -> str:
    sections = set(document["sections"])
    if "bass" in sections:
        raise PrescriptionDocumentRefused("prescription_malformed", "bass", "bass has no preview model")
    for kind, (names, _) in _PREVIEW_ROWS.items():
        if sections & names:
            if sections <= names:
                return kind
            raise PrescriptionDocumentRefused("prescription_malformed", sorted(sections & names)[0],
                                              "preview sections must use one model")
    raise PrescriptionDocumentRefused("prescription_malformed", None, "no preview model for these sections")


def preview_prescription_document(
    document: Mapping[str, Any], *, round_dir: Path | None, base: BankedCandidate | None = None,
    evidence: PrescriptionEvidence | None = None, capture_id: str | None = None,
) -> dict[str, Any]:
    kind = preview_kind(document)
    _, preview_function = _PREVIEW_ROWS[kind]
    sections = document["sections"]
    payload: Any = document
    kwargs: dict[str, Any]
    try:
        if kind == "rear_calibration":
            if round_dir is None:
                raise PrescriptionDocumentRefused("evidence_unreadable", kind, "a rear preview needs --round <pair round>")
            inputs = round_inputs(round_dir)
            payload = sections[kind]
            kwargs = {"inputs": inputs, "manifest": read_run_manifest(inputs)}
        else:
            if kind == "emitted_graph" and (round_dir is None or capture_id is None):
                raise PrescriptionDocumentRefused("evidence_unreadable", "driver" if "driver" in sections else "blend",
                                                  "a driver/blend preview needs --round <diagnostic round>")
            assert base is not None and evidence is not None
            if kind == "room":
                if not sections[kind]:
                    raise PrescriptionDocumentRefused("prescription_malformed", kind, "preview requires a room section")
                contracts = prescription_contracts(**{**evidence.sources, "candidate": base.candidate.to_dict()})
                payload = _section_payload(kind, sections[kind], document["rationale"], contracts)
                kwargs = {"room_median": room.read_room_median(evidence.sources.get("room_median", {})),
                          "room_median_sha256": evidence.room_median_sha256, "round_id": evidence.round_id,
                          "sides": SIDES_BY_LAYOUT[base.candidate.source_preset.channel_map.layout]}
            else:
                kwargs = {"round_dir": round_dir, "base": base, "evidence": evidence, "capture_id": capture_id}
        preview = preview_function(payload, **kwargs)
    except room.RoomPrescriptionRefused as exc:
        raise PrescriptionDocumentRefused(exc.reason, kind, exc.detail, evidence=exc.evidence) from exc
    except rear_calibration.RearCalibrationError as exc:
        raise PrescriptionDocumentRefused("rear_calibration_invalid", kind, str(exc)) from exc
    except RoundCapturesRefused as exc:
        raise PrescriptionDocumentRefused(exc.reason, kind, str(exc), evidence=exc.detail) from exc
    except PrescriptionDocumentRefused:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PrescriptionDocumentRefused("evidence_unreadable", kind, str(exc)) from exc
    return {"ok": True, "section": kind, "sections": sorted(sections), "preview": preview, "adopted": False, "banked": False}


def _refused_section(code: str) -> str | None:
    """The section a composition refusal came from, when its code names one."""
    if code == "composition_topology_required":
        return "topology"
    return "rear_calibration" if code.startswith("rear_calibration_") else None


def saved_base() -> tuple[BankedCandidate, Mapping[str, Any]]:
    """The applied baseline as a composition base (a document's ``base: saved``),
    with the profile state it was read from: composing that base needs the same
    state, and this is the one read of it."""
    state = load_applied_baseline_profile_state() or {}
    saved = candidate_from_applied_profile(output_topology.load_output_topology_strict(), state)
    return BankedCandidate(saved, "", "", baseline_profile_state_path()), state


def bank_section(name: str, section: Any, *, rationale: str) -> MeasuredCrossoverCandidate:
    """Judge ONE authored section on the applied baseline, as ``--base saved`` does.

    The candidate is composed and returned, never banked and never applied.
    """
    base, base_profile = saved_base()
    return judge_prescription_document(
        {"kind": DOCUMENT_KIND, "schema": 1, "base": "saved",
         "sections": {name: section if isinstance(section, dict) else {}},
         "rationale": rationale},
        base=base, base_profile=base_profile,
    )


def judge_prescription_document(raw: Any, *, base: BankedCandidate,
                               evidence: PrescriptionEvidence | None = None,
                               base_profile: Mapping[str, Any] | None = None) -> MeasuredCrossoverCandidate:
    document = read_prescription_document(raw)
    if document["base"] != "saved" and document["base"] != base.fingerprint:
        raise PrescriptionDocumentRefused("composition_base_mismatch", None, "document and resolved base differ")
    evidence = evidence or PrescriptionEvidence()
    contracts = prescription_contracts(**{**evidence.sources, "candidate": base.candidate.to_dict()})
    selected: dict[str, Any] = {}
    judged: dict[str, Any] = {}
    fc_hz = contracts["speaker"]["alignment"]["bounds"]["fc_hz"]
    for name in ("topology", "blend", "alignment", "room", "bass", "rear_calibration", "driver"):
        if name not in document["sections"]:
            continue
        section = document["sections"][name]
        if not section:
            selected[name] = None
            continue
        section = _section_payload(name, section, document["rationale"], contracts)
        try:
            selected[name], judged[name] = _judge_section(
                name, section, base=base, contracts=contracts, evidence=evidence, fc_hz=fc_hz, selected=selected,
            )
            if name == "topology":
                fc_hz = selected[name].fc_hz
        except (blend.BlendPrescriptionRefused, alignment.AlignmentPrescriptionRefused,
                topology.TopologyPrescriptionRefused) as exc:
            raise PrescriptionDocumentRefused(exc.reason, name, exc.detail, evidence=getattr(exc, "evidence", {})) from exc
        except rear_calibration.RearCalibrationError as exc:
            raise PrescriptionDocumentRefused("rear_calibration_invalid", name, str(exc)) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise PrescriptionDocumentRefused("prescription_malformed", name, str(exc)) from exc
    try:
        rows, _ = round_alignment({**(evidence.sources.get("manifest") or {}), "round_id": evidence.round_id},
                                 evidence.sources) if evidence.round_id else ([], {})
        read = commissioning_alignment(rows, base.fingerprint)
        if evidence.round_id and document["base"] != "saved":
            base_profile = evidence.sources.get("applied_profile")
        elif base_profile is None:
            # Not resolved with the base (:func:`saved_base` hands its state over).
            base_profile = load_applied_baseline_profile_state()
        return compose_candidate(
            base, sections=selected, rationale=document["rationale"],
            base_profile=base_profile,
            room_prescription_sha256=(blend.prescription_sha256(contract_json(judged["room"]).encode())
                                      if selected.get("room") else ""),
            room_measured_basis=judged.get("room", {}).get("measured_basis"),
            evidence={"packet_fingerprint": evidence.packet.get("packet_fingerprint"),
                      "contracts": contract_digests(contracts), "prescriptions": judged,
                      **({"commissioning": {"alignment": read}} if read is not None else {})},
        )
    except (CandidateBankRefusal, MeasuredCrossoverCandidateError) as exc:
        raise PrescriptionDocumentRefused(exc.code, _refused_section(exc.code), exc.detail) from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise PrescriptionDocumentRefused("composition_invalid", None, str(exc)) from exc
