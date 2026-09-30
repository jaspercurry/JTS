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
from jasper.active_speaker.candidate_parts import COMPOSITION_INVALID, candidate_from_applied_profile, compose_candidate, program_charge_db
from jasper.active_speaker.linearization_fit import linearization_filters_by_role
from ..measured_crossover_candidate import (
    MeasuredCrossoverCandidate, MeasuredCrossoverCandidateError,
)
from jasper.active_speaker.measurement_programs import PRESCRIPTION_SECTIONS, PROGRAM_DOCUMENT_ORDER, prescription_sections
from jasper.active_speaker.profile import SIDES_BY_LAYOUT, required_driver_roles
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.active_speaker import rear_calibration
from jasper.audio_measurement.evidence_reasons import REASON_UNREADABLE
from jasper.dsp_control.camilla_config_contract import DEFAULT_SAMPLE_RATE
from jasper.audio_routes import output_topology_store as output_topology
from ._prescription_common import PRESCRIPTION_MALFORMED

from . import alignment_prescription as alignment
from . import bass_prescription as bass
from . import blend_prescription as blend
from . import driver_prescription as driver
from . import room_prescription as room
from . import topology_prescription as topology
from .capture_prediction import capture_prediction
from .forward_model import ForwardModelError
from .evidence_packet.readers import packet_feature_classifications
from .prescription_contract import contract_digests, contract_json, contract_programs, prescription_contracts
from .refusal_copy import refusal_copy_for
from .rear_preview import preview_rear_section
from .round_inputs import prescription_sources, read_run_manifest, round_inputs

DOCUMENT_KIND = "jts_prescription"
_SECTIONS = {section.name: section for section in PRESCRIPTION_SECTIONS}
SECTION_KINDS = {name: section.kind for name, section in _SECTIONS.items()}
_SECTION_PROGRAMS = {section.name: row.purpose for row in PROGRAM_DOCUMENT_ORDER for section in row.sections}
_JUDGE_ORDER = tuple(section.name for section in sorted(PRESCRIPTION_SECTIONS, key=lambda section: section.judge_order))


def blamed_section(sections: Mapping[str, Any]) -> str | None:
    """The section an evidence refusal names: a stated room, whose median the round's
    set selects, else the first section stated, else the first named (document order)."""
    named = [name for name in SECTION_KINDS if name in sections]
    stated = [name for name in named if sections[name]]
    return "room" if "room" in stated else next(iter(stated or named), None)


class PrescriptionDocumentRefused(ValueError):
    def __init__(self, code: str, section: str | None, error: str, *, evidence: Mapping[str, Any] | None = None):
        super().__init__(error)
        self.code, self.section, self.error = code, section, error
        self.evidence = dict(evidence or {})

    def failure_detail(self) -> dict[str, Any]:
        """The CLI failure document's ``detail`` (ADR-0237)."""
        return {"section": self.section, "error": self.error, "evidence": self.evidence}

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
        raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, None, "unknown document fields")
    if not isinstance(raw.get("base"), str) or not raw["base"].strip():
        raise PrescriptionDocumentRefused("composition_base_required", None, "name a base fingerprint or saved")
    if not isinstance(raw.get("rationale"), str) or not isinstance(raw.get("sections"), Mapping):
        raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, None, "sections must be an object and rationale must be text")
    for name, value in raw["sections"].items():
        if name not in SECTION_KINDS:
            raise PrescriptionDocumentRefused("prescription_section_unknown", str(name), "unknown section")
        if value is not None and not isinstance(value, Mapping):
            raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, name, "section must be an object or null")
        prohibited = blend.find_prohibited_keys(value) if name in {"driver", "blend", "room"} else []
        if prohibited:
            raise PrescriptionDocumentRefused(blend.PRESCRIPTION_PROHIBITED_FIELD, name, "prohibited fields", evidence={"fields": prohibited})
    return raw


def parse_vary_axis(text: str) -> tuple[tuple[str, ...], tuple[Any, ...]]:
    paths_text, separator, values_text = text.partition("=")
    paths = tuple(path.strip() for path in paths_text.split(","))
    if not separator or not all(paths) or not all(token.strip() for token in values_text.split(",")):
        raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, "sections", "expected PATH[,PATH...]=VALUE[,VALUE...]")
    values = []
    for token in values_text.split(","):
        try:
            value = json.loads(token)
        except json.JSONDecodeError:
            value = token.strip()
        if isinstance(value, (dict, list)):
            raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, "sections", "axis values must be JSON scalars")
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
        raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, section if section in sections else "sections",
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
                raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, section if section in document["sections"] else "sections",
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
                   fc_hz: float | None) -> tuple[Any, Mapping[str, Any]]:
    packet = dict(evidence.packet)
    speaker = contracts.get("speaker", {})
    if name == "driver":
        driver.check_driver_document_size(json.dumps(raw).encode())
        prescription = driver.read_driver_prescription(
            raw, packet_fingerprint=packet.get("packet_fingerprint"),
            passbands_hz=speaker["driver"]["bounds"]["passbands_hz"],
            speaker_roles=required_driver_roles(base.candidate.source_preset.way_count),
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
    row = _SECTIONS[name]
    header: dict[str, Any] = {"kind": row.kind, "rationale": rationale}
    if "artifact_schema_version" in row.envelope:
        program = contracts[_SECTION_PROGRAMS[name]]
        # A one-section program's contract is its section's; the speaker's nests each section's.
        header["artifact_schema_version"] = program.get(name, program)["schema"]["properties"]["artifact_schema_version"]["const"]
    section = {**{key: header[key] for key in row.envelope}, **section}
    if row.kind is not None and section.get("kind") != row.kind:
        raise PrescriptionDocumentRefused("prescription_kind_unknown", name, "section kind does not match its name")
    return section


def _preview_emitted_graph(document: Mapping[str, Any], *, round_dir: Path,
                           base: BankedCandidate, evidence: PrescriptionEvidence,
                           capture_id: str) -> dict[str, Any]:
    composed = judge_prescription_document(document, base=base, evidence=evidence)
    section = blamed_section(document["sections"])
    try:
        return capture_prediction(round_dir, capture_id=capture_id,
                                  candidate=composed, basis_candidate=base.candidate)
    except ForwardModelError as exc:
        raise PrescriptionDocumentRefused(exc.refusal_reason, section, str(exc), evidence=exc.detail) from exc


_PREVIEW_ROWS = {kind: set(names) for _, kind, names in sorted(row.preview for row in PROGRAM_DOCUMENT_ORDER if row.preview)}


def preview_kind(document: Mapping[str, Any]) -> str:
    sections = set(document["sections"])
    if "bass" in sections:
        raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, "bass", "bass has no preview model")
    for kind, names in _PREVIEW_ROWS.items():
        if sections & names:
            if sections <= names:
                return kind
            raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, sorted(sections & names)[0],
                                              "preview sections must use one model")
    raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, None, "no preview model for these sections")


def preview_prescription_document(
    document: Mapping[str, Any], *, round_dir: Path | None, base: BankedCandidate,
    evidence: PrescriptionEvidence, capture_id: str | None = None,
    cabinet: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    """``cabinet`` is the declared ``(front woofer, rear woofer, tweeter)``
    outputs a rear preview compiles its stage at; without one it refuses."""
    kind = preview_kind(document)
    sections = document["sections"]
    payload: Any = document
    kwargs: dict[str, Any]
    preview_function: Callable[..., dict[str, Any]]
    extra: dict[str, Any] = {}
    try:
        if kind == "rear_calibration":
            if cabinet is None:
                raise PrescriptionDocumentRefused(
                    "rear_calibration_topology_unsupported", kind,
                    "the declared layout has no cabinet of one front woofer, one rear woofer and one tweeter")
            if round_dir is None:
                raise PrescriptionDocumentRefused(REASON_UNREADABLE, kind, "a rear preview needs --round <pair round>")
            preview_function = preview_rear_section
            inputs = round_inputs(round_dir)
            payload = sections[kind]
            kwargs = {"inputs": inputs, "manifest": read_run_manifest(inputs)}
        else:
            if kind == "emitted_graph" and (round_dir is None or capture_id is None):
                raise PrescriptionDocumentRefused(REASON_UNREADABLE, blamed_section(sections),
                                                  "a speaker preview needs --round <diagnostic round>")
            if kind == "room":
                preview_function = room.preview_room_prescription
                if not sections[kind]:
                    raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, kind, "preview requires a room section")
                sources = {**evidence.sources, "candidate": base.candidate.to_dict()}
                contracts = prescription_contracts(programs=contract_programs(sources), **sources)
                payload = _section_payload(kind, sections[kind], document["rationale"], contracts)
                kwargs = {"room_median": room.read_room_median(evidence.sources.get("room_median", {})),
                          "room_median_sha256": evidence.room_median_sha256, "round_id": evidence.round_id,
                          "sides": SIDES_BY_LAYOUT[base.candidate.source_preset.channel_map.layout]}
            else:
                preview_function = _preview_emitted_graph
                kwargs = {"round_dir": round_dir, "base": base, "evidence": evidence, "capture_id": capture_id}
        preview = preview_function(payload, **kwargs)
        if cabinet is not None and kind == "rear_calibration":
            front, rear, tweeter = cabinet
            validated = rear_calibration.read_rear_calibration(payload, sample_rate=DEFAULT_SAMPLE_RATE)
            extra["compiled_stage"] = rear_calibration.compile_rear_stage(
                validated, front_channel=front, rear_channel=rear, tweeter_channel=tweeter,
                channel_count=max(cabinet) + 1) if validated["case"] == "electrical_dsp" else None
            extra["program_charge_db"] = program_charge_db(judge_prescription_document(document, base=base, evidence=evidence))
    except room.RoomPrescriptionRefused as exc:
        raise PrescriptionDocumentRefused(exc.reason, kind, exc.detail, evidence=exc.evidence) from exc
    except rear_calibration.RearCalibrationError as exc:
        raise PrescriptionDocumentRefused("rear_calibration_invalid", kind, str(exc)) from exc
    except PrescriptionDocumentRefused:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PrescriptionDocumentRefused(REASON_UNREADABLE, kind, str(exc)) from exc
    return {"section": kind, "sections": sorted(sections), "preview": preview, **extra,
            "adopted": False, "banked": False}


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


def saved_document(sections: Mapping[str, Any], rationale: str) -> dict[str, Any]:
    """A prescription document that composes ``sections`` on the applied baseline (``base: saved``)."""
    return {"kind": DOCUMENT_KIND, "schema": 1, "base": "saved", "sections": dict(sections), "rationale": rationale}


def reset_prescription_document(
    *, keep_timing: bool, trims_db: Mapping[str, float] | None, program: str | None = None,
) -> dict[str, Any]:
    sections: dict[str, Any] = {
        name: None if name == "rear_calibration" else {}
        for name in prescription_sections(program) if not (keep_timing and name == "alignment")
    }
    if "driver" in sections:
        sections["driver"] = {"filters": [], **({"pinned_trim_db": dict(trims_db)} if trims_db else {})}
    return saved_document(sections, "Reset the applied tuning layers.")


def bank_section(name: str, section: Any, *, rationale: str) -> MeasuredCrossoverCandidate:
    """Judge ONE authored section on the applied baseline, as a ``base: saved`` document does.

    The candidate is composed and returned, never banked and never applied.
    """
    base, base_profile = saved_base()
    return judge_prescription_document(saved_document({name: section if isinstance(section, dict) else {}}, rationale),
                                       base=base, base_profile=base_profile)


def judge_prescription_document(raw: Any, *, base: BankedCandidate,
                               evidence: PrescriptionEvidence | None = None,
                               base_profile: Mapping[str, Any] | None = None) -> MeasuredCrossoverCandidate:
    document = read_prescription_document(raw)
    evidence = evidence or PrescriptionEvidence()
    sources = {**evidence.sources, "candidate": base.candidate.to_dict()}
    contracts = prescription_contracts(programs=contract_programs(sources), **sources)
    selected: dict[str, Any] = {}
    judged: dict[str, Any] = {}
    fc_hz = contracts["speaker"]["alignment"]["bounds"]["fc_hz"] if "speaker" in contracts else None
    for name in _JUDGE_ORDER:
        if name not in document["sections"]:
            continue
        section = document["sections"][name]
        if not section:
            selected[name] = None
            continue
        if _SECTION_PROGRAMS[name] not in contracts:
            raise PrescriptionDocumentRefused("prescription_section_unavailable", name, "section unavailable for this topology")
        section = _section_payload(name, section, document["rationale"], contracts)
        try:
            selected[name], judged[name] = _judge_section(
                name, section, base=base, contracts=contracts, evidence=evidence, fc_hz=fc_hz,
            )
            if name == "topology":
                fc_hz = selected[name].fc_hz
        except (blend.BlendPrescriptionRefused, alignment.AlignmentPrescriptionRefused,
                topology.TopologyPrescriptionRefused) as exc:
            raise PrescriptionDocumentRefused(exc.reason, name, exc.detail, evidence=getattr(exc, "evidence", {})) from exc
        except rear_calibration.RearCalibrationError as exc:
            raise PrescriptionDocumentRefused("rear_calibration_invalid", name, str(exc)) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise PrescriptionDocumentRefused(PRESCRIPTION_MALFORMED, name, str(exc)) from exc
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
        raise PrescriptionDocumentRefused(exc.code, _refused_section(exc.code), exc.detail,
                                          evidence=getattr(exc, "evidence", None)) from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise PrescriptionDocumentRefused(COMPOSITION_INVALID, None, str(exc)) from exc
