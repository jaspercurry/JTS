# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compose candidate interventions without carrying their parents' measurement claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import yaml

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.output_topology import OutputTopology, load_output_topology_strict

from .branch_chain import branch_headroom_db, sections_by_role
from .candidate_bank import BankedCandidate, CandidateBankRefusal, publish_authored_candidate
from .baseline_profile import applied_baseline_hardware_match, load_applied_baseline_profile_state, recompose_applied_baseline_yaml
from .crossover_v2.room_prescription import ROOM_MEDIAN_FIELD
from .crossover_v2.topology_prescription import apply_topology_pin
from .measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    MeasuredCrossoverCandidateError,
    candidate_room_peqs,
    compile_candidate_config,
    driver_corrections,
    prove_candidate_config,
)

from .profile import ActiveSpeakerPreset, required_driver_roles
from .measurement_programs import baseline_scope

COMPOSITION_KIND = "jts_candidate_composition"


def _source(parent: BankedCandidate) -> dict[str, str]:
    return {"fingerprint": parent.fingerprint, "artifact_path": str(parent.path)}


def _linearization_entry(filters: Any, *, role: str, sections: Mapping[str, Any], trim_db: float) -> dict[str, Any]:
    if (
        not isinstance(filters, Sequence) or isinstance(filters, (str, bytes))
        or any(not isinstance(item, Mapping) for item in filters)
    ):
        raise CandidateBankRefusal("composition_filters_invalid", f"invalid filters for {role}")
    return {
        "filters": [dict(item) for item in filters],
        "headroom_cost_db": branch_headroom_db(filters, sections=sections.get(role, ()), trim_db=trim_db),
    }


def candidate_from_applied_profile(
    topology: OutputTopology, applied_profile: Mapping[str, Any], *, purpose: str | None = None,
) -> MeasuredCrossoverCandidate:
    """Compose the saved tune or a program baseline from the applied layers."""
    snapshot, issues = applied_baseline_hardware_match(topology, applied_profile=applied_profile)
    if snapshot is None:
        raise CandidateBankRefusal("composition_saved_tune_unavailable", str(issues))
    preset = ActiveSpeakerPreset.from_mapping(dict(snapshot["preset"]))
    if purpose is not None and baseline_scope(purpose) == "preset":
        candidate = MeasuredCrossoverCandidate(
            program_id="jts_saved_tune", source_preset=preset,
            analysis={"measurement_status": "unmeasured"},
            role_attenuations_db={role: 0.0 for role in required_driver_roles(preset.way_count)},
        )
        prove_candidate_config(candidate, compile_candidate_config(candidate, playback_device="null"))
        return candidate
    corrections = snapshot["corrections"]
    sections = sections_by_role(preset.crossover_regions)
    candidate = MeasuredCrossoverCandidate(
        program_id="jts_saved_tune",
        analysis={"measurement_status": "unmeasured", "saved_snapshot_sha256": json_fingerprint(snapshot)},
        source_preset=preset,
        role_attenuations_db={role: values["gain_db"] for role, values in corrections.items()},
        linearization={
            role: _linearization_entry(filters, role=role, sections=sections, trim_db=corrections[role]["gain_db"])
            for role, filters in snapshot.get("linearization", {}).items()
        },
        blend_correction=snapshot.get("blend_correction", ()),
        room_correction=snapshot.get("room_correction", applied_profile.get("room_correction", {})),
        bass_extension=snapshot.get("bass_extension", {}),
    )
    if driver_corrections(candidate) != corrections:
        for role, values in corrections.items():
            for polarity in ("keep", "invert"):
                try:
                    aligned = replace(candidate, alignment=MeasuredCrossoverAlignment(
                        values["delay_ms"] * 1000, role, polarity,
                    ))
                except MeasuredCrossoverCandidateError:
                    continue
                if driver_corrections(aligned) == corrections:
                    candidate = aligned
                    break
            if driver_corrections(candidate) == corrections:
                break
        else:
            raise CandidateBankRefusal("composition_saved_tune_unrepresentable", "saved driver corrections cannot be represented")
    emitted = compile_candidate_config(candidate, playback_device="null", room_peqs=candidate_room_peqs(candidate))
    saved, issues = recompose_applied_baseline_yaml(topology, applied_profile=applied_profile, playback_device="null")
    if saved is None:
        raise CandidateBankRefusal("composition_saved_tune_unavailable", str(issues))
    desired, actual = yaml.safe_load(saved), yaml.safe_load(emitted)
    if any(desired.get(key) != actual.get(key) for key in ("filters", "mixers", "processors", "pipeline")):
        raise CandidateBankRefusal("composition_saved_tune_unrepresentable", "candidate would change saved processing or protection")
    prove_candidate_config(candidate, emitted)
    if purpose is not None:
        candidate = replace(candidate, bass_extension={}, room_correction=(
            candidate.room_correction if baseline_scope(purpose) == "room" else {}
        ))
    return candidate


def baseline_candidate_id(purpose: str | None) -> str:
    return publish_authored_candidate(candidate_from_applied_profile(
        load_output_topology_strict(), load_applied_baseline_profile_state() or {}, purpose=purpose or "speaker",
    )).fingerprint


def compose_candidate(
    base: BankedCandidate,
    roles: Mapping[str, BankedCandidate],
    *,
    alignment: BankedCandidate | None = None,
    blend: BankedCandidate | None = None,
    expected_effect: str = "", observation_refs: Sequence[str] = (), rationale: str = "",
    room_correction: Mapping[str, Any] | None = None,
    room_prescription_sha256: str = "",
    room_measured_basis: Mapping[str, Any] | None = None,
    bass_extension: Mapping[str, Any] | None = None,
    sections: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> MeasuredCrossoverCandidate:
    """Replace selected parts without inheriting their measurement claims."""
    selected = dict(sections or {})
    if room_correction is not None:
        selected["room"] = room_correction
    if bass_extension is not None:
        if not isinstance(bass_extension, Mapping):
            raise CandidateBankRefusal("composition_bass_invalid", "bass extension must be an object")
        selected["bass"] = bass_extension
    if "topology" in selected and not selected["topology"]:
        raise CandidateBankRefusal("composition_topology_required", "the hardware topology cannot be cleared")
    preset = base.candidate.source_preset
    sources = {role: roles.get(role, base) for role in base.candidate.role_attenuations_db}
    if set(roles) - set(sources):
        raise CandidateBankRefusal("composition_role_unknown", "role is not in the base preset")
    alignment, blend = alignment or base, blend or base
    for source in (*sources.values(), alignment, blend):
        if source.candidate.source_preset != preset:
            raise CandidateBankRefusal(
                "composition_preset_mismatch", "all sources must use the same base preset"
            )
    trims = {}
    linearization: dict[str, Any] = {}
    for role, source in sources.items():
        trims[role] = source.candidate.role_attenuations_db[role]
        if role not in source.candidate.linearization:
            continue
        entry = source.candidate.linearization[role]
        filters = entry.get("filters", []) if isinstance(entry, Mapping) else None
        linearization[role] = {"filters": filters}
    preset, _ = apply_topology_pin(selected.get("topology"), preset=preset, fc_hz=None)
    if "driver" in selected:
        driver = selected["driver"] or {}
        if driver:
            trims.update(driver["role_attenuations_db"])
            linearization.update(driver["linearization"])
        else:
            trims = {role: 0.0 for role in trims}
            linearization = {}
    sections_by_driver = sections_by_role(preset.crossover_regions)
    linearization = {
        role: _linearization_entry(entry["filters"], role=role, sections=sections_by_driver, trim_db=trims[role])
        for role, entry in linearization.items()
    }
    resolved_alignment = alignment.candidate.alignment
    if "alignment" in selected:
        pin = selected["alignment"]
        role_order = required_driver_roles(preset.way_count)
        resolved_alignment = (MeasuredCrossoverAlignment(
            abs(pin.delay_us), role_order[1] if pin.delay_us >= 0 else role_order[0],
            pin.polarity or resolved_alignment.polarity or "keep",
        ) if pin else MeasuredCrossoverAlignment())
    room = dict(selected.get("room", base.candidate.room_correction) or {})
    bass = dict(selected.get("bass", base.candidate.bass_extension) or {})
    resolution = {
        name: "base" if name not in selected else "document" if selected[name] else "cleared"
        for name in ("driver", "blend", "alignment", "topology", "room", "bass")
    }
    analysis: dict[str, Any] = {
        "kind": COMPOSITION_KIND,
        "measurement_status": "unmeasured",
        "resolution": resolution,
        "evidence": dict(evidence or {}),
        "base": _source(base),
        **({"role_sources": {role: _source(source) for role, source in sources.items()}} if "driver" not in selected else {}),
        **({"alignment_source": _source(alignment)} if "alignment" not in selected else {}),
        **({"blend_source": _source(blend)} if "blend" not in selected else {}),
        "expected_effect": expected_effect, "observation_refs": list(observation_refs), "rationale": rationale,
    }
    if room and "room" not in selected:
        analysis["room_source"] = base.candidate.analysis.get("room_source", {"base": _source(base)})
    elif room:
        measured_basis = dict(room_measured_basis or {})
        measured_candidate = measured_basis.get("speaker_candidate_id") or measured_basis.get("candidate_id")
        analysis["room_source"] = {
            "prescription_sha256": room_prescription_sha256,
            ROOM_MEDIAN_FIELD: room["basis"][ROOM_MEDIAN_FIELD],
            "measured_basis": measured_basis,
            "base_match": (
                "unknown" if not measured_candidate else
                "match" if measured_candidate == base.fingerprint else "different"
            ),
        }
    candidate = MeasuredCrossoverCandidate(
        program_id=COMPOSITION_KIND, analysis=analysis, source_preset=preset, role_attenuations_db=trims,
        alignment=resolved_alignment,
        linearization=linearization,
        blend_correction=selected.get("blend", blend.candidate.blend_correction) or (),
        room_correction=room, bass_extension=bass,
    )
    prove_candidate_config(candidate, compile_candidate_config(
        candidate, playback_device="null", room_peqs=candidate_room_peqs(candidate),
    ))
    return candidate
