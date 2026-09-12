# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compose candidate interventions without carrying their parents' measurement claims."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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

from .measurement_emit import MeasurementGraphRefused
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
        # The current candidate model can refine one region; verify its inverse
        # through the same correction reducer instead of inventing another one.
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
    scope = baseline_scope(purpose) if purpose is not None else None
    return candidate if scope is None else replace(candidate, bass_extension={},
                   room_correction=candidate.room_correction if scope == "room" else {},
                   linearization={} if scope == "preset" else candidate.linearization,
                   blend_correction=() if scope == "preset" else candidate.blend_correction)


def baseline_candidate_ids(purposes: Iterable[str | None]) -> dict[str, str]:
    programs = set(purpose or "speaker" for purpose in purposes)
    if not programs:
        return {}
    try:
        topology, applied = load_output_topology_strict(), load_applied_baseline_profile_state() or {}
        return {purpose: publish_authored_candidate(candidate_from_applied_profile(
            topology, applied, purpose=purpose,
        )).fingerprint for purpose in sorted(programs)}
    except (CandidateBankRefusal, OSError, ValueError) as exc:
        raise MeasurementGraphRefused("measurement_baseline_unavailable", str(exc)) from exc


def compose_candidate(
    base: BankedCandidate,
    *,
    rationale: str = "",
    room_prescription_sha256: str = "",
    room_measured_basis: Mapping[str, Any] | None = None,
    sections: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> MeasuredCrossoverCandidate:
    """Replace selected parts without inheriting their measurement claims."""
    selected = dict(sections or {})
    if "topology" in selected and not selected["topology"]:
        raise CandidateBankRefusal("composition_topology_required", "the hardware topology cannot be cleared")
    preset = base.candidate.source_preset
    trims = dict(base.candidate.role_attenuations_db)
    linearization = dict(base.candidate.linearization)
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
    resolved_alignment = base.candidate.alignment
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
        "rationale": rationale,
    }
    if room and "room" not in selected:
        analysis["room_source"] = dict(base.candidate.analysis.get("room_source", {}))
        analysis["room_source"].pop("base_match", None)
    elif room:
        measured_basis = dict(room_measured_basis or {})
        analysis["room_source"] = {
            "prescription_sha256": room_prescription_sha256,
            ROOM_MEDIAN_FIELD: room["basis"][ROOM_MEDIAN_FIELD],
            "measured_basis": measured_basis,
        }
    candidate = MeasuredCrossoverCandidate(
        program_id=COMPOSITION_KIND, analysis=analysis, source_preset=preset, role_attenuations_db=trims,
        alignment=resolved_alignment,
        linearization=linearization,
        blend_correction=selected.get("blend", base.candidate.blend_correction) or (),
        room_correction=room, bass_extension=bass,
    )
    # The room set is emitted here so the emitter's headroom charge runs at
    # compose rather than at apply.
    prove_candidate_config(candidate, compile_candidate_config(
        candidate, playback_device="null", room_peqs=candidate_room_peqs(candidate),
    ))
    return candidate
