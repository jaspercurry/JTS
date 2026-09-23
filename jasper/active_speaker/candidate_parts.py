# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compose candidate parts and retain timing provenance. See ADR-0319."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Literal

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.program_analysis.model import TIMING_MEASURED
from jasper.output_topology import OutputTopology
from jasper.output_topology_store import load_output_topology_strict

from .branch_chain import branch_headroom_db, sections_by_role
from .candidate_bank import BankedCandidate, CandidateBankRefusal, find_banked_candidate, publish_authored_candidate, load_applied_candidate
from .baseline_profile import (
    load_applied_baseline_profile_state,
)
from .crossover_v2.alignment_prescription import alignment_to_candidate_fields
from .crossover_v2.room_prescription import ROOM_MEDIAN_FIELD
from .crossover_v2.topology_prescription import apply_topology_pin
from .crossover_preview import build_crossover_preview
from .commission_wiring import resolve_commission_preset
from .measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    MeasuredCrossoverCandidateError,
    candidate_room_peqs,
    compile_candidate_config,
    driver_corrections,
    prove_candidate_config,
)

from .level_trim import declared_driver_gains
from .measurement_emit import MeasurementGraphRefused
from .measurement_programs import PRESCRIPTION_SECTIONS
from .profile import ActiveSpeakerPreset, required_driver_roles

COMPOSITION_KIND = "jts_candidate_composition"
DECLARED_CROSSOVER_PROGRAM_ID = "jts_declared_crossover"
AlignmentSource = Literal["document", "cleared", "saved", "measured", "base"]


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
    topology: OutputTopology, applied_profile: Mapping[str, Any],
    *, find_candidate: Callable[[str], BankedCandidate] | None = None,
) -> MeasuredCrossoverCandidate:
    """Look up the applied candidate, migrating pre-bank records once."""
    if applied_profile.get("status") != "applied":
        raise CandidateBankRefusal("composition_saved_tune_unavailable", "there is no applied candidate")
    if find_candidate is None and applied_profile.get("candidate_artifact_path"):
        return load_applied_candidate(
            (applied_profile.get("source") or {}).get("measured_candidate_fingerprint", ""),
            applied_profile=applied_profile,
        ).candidate
    find_candidate = find_candidate or find_banked_candidate
    fingerprint = (applied_profile.get("source") or {}).get("measured_candidate_fingerprint")
    if fingerprint:
        try:
            return find_candidate(fingerprint).candidate
        except CandidateBankRefusal as exc:
            if exc.code != "not_found":
                raise
    try:
        return _migrate_applied_candidate(applied_profile, find_candidate)
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateBankRefusal("composition_saved_tune_unavailable", str(exc)) from exc


def candidate_from_design_draft(
    topology: OutputTopology, design_draft: Mapping[str, Any],
) -> MeasuredCrossoverCandidate:
    """Build the declared crossover and trims in memory, without measured layers."""
    preview = build_crossover_preview(design_draft)
    preset = resolve_commission_preset(topology, crossover_preview=preview)
    gains, _, _, issues = declared_driver_gains(required_driver_roles(preset.way_count), preview["drivers"])
    return MeasuredCrossoverCandidate(
        program_id=DECLARED_CROSSOVER_PROGRAM_ID, analysis={"measurement_status": "unmeasured", "issues": issues},
        source_preset=preset, role_attenuations_db=gains,
    )


def _migrate_applied_candidate(
    applied_profile: Mapping[str, Any], find_candidate: Callable[[str], BankedCandidate],
) -> MeasuredCrossoverCandidate:
    snapshot = applied_profile.get("recomposition_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != 1:
        raise CandidateBankRefusal("composition_saved_tune_unavailable", "saved candidate inputs are missing")
    preset = ActiveSpeakerPreset.from_mapping(snapshot.get("preset"))
    corrections = snapshot.get("corrections")
    if not isinstance(corrections, Mapping) or set(corrections) != set(required_driver_roles(preset.way_count)):
        raise CandidateBankRefusal("composition_saved_tune_unavailable", "saved driver corrections are missing")
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
        rear_calibration=snapshot.get("rear_calibration", {}),
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
    try:
        return find_candidate(candidate.fingerprint).candidate
    except CandidateBankRefusal as exc:
        if exc.code != "not_found":
            raise
        return publish_authored_candidate(candidate).candidate


def baseline_candidate_id() -> str:
    from .design_draft import load_design_draft  # lazy: design draft imports candidate parts

    try:
        topology, applied = load_output_topology_strict(), load_applied_baseline_profile_state()
        if applied is None:
            return publish_authored_candidate(candidate_from_design_draft(topology, load_design_draft(topology=topology))).fingerprint
        return candidate_from_applied_profile(topology, applied).fingerprint
    except (CandidateBankRefusal, OSError, ValueError) as exc:
        raise MeasurementGraphRefused("measurement_baseline_unavailable", str(exc)) from exc


def resolve_alignment(
    base: MeasuredCrossoverCandidate, selected: Mapping[str, Any], *, roles: Sequence[str],
    saved: Mapping[str, Any] | None, commissioning: Mapping[str, Any],
) -> tuple[MeasuredCrossoverAlignment, AlignmentSource]:
    """Resolve timing once for the trial graph and apply record. See ADR-0319."""
    read = commissioning.get("alignment") or {}
    measured = read.get("timing_verdict") == TIMING_MEASURED and commissioning.get("status") in (None, "awaiting_apply")
    if "alignment" in selected:
        pin = selected["alignment"]
        if not pin:
            return MeasuredCrossoverAlignment(), "cleared"
        if not isinstance(pin, MeasuredCrossoverAlignment):
            fields = alignment_to_candidate_fields(pin, roles=roles)
            return MeasuredCrossoverAlignment(*fields[:2], fields[2] or base.alignment.polarity or "keep"), "document"
        return pin, "measured" if measured else "document"
    source: AlignmentSource = "saved" if saved is not None else "measured"
    pair = saved if saved is not None else read.get("committed") or {}
    if saved is not None or measured:
        fields = alignment_to_candidate_fields({**pair, "alignment_status": "ok"}, roles=roles)
        return MeasuredCrossoverAlignment(*fields), source
    return base.alignment, "base"


def compose_candidate(
    base: BankedCandidate,
    *,
    rationale: str = "",
    room_prescription_sha256: str = "",
    room_measured_basis: Mapping[str, Any] | None = None,
    sections: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    base_profile: Mapping[str, Any] | None = None,
) -> MeasuredCrossoverCandidate:
    """Replace selected parts and retain unchanged measured timing."""
    selected = dict(sections or {})
    evidence = dict(evidence or {})
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
            linearization = {**linearization, **driver["linearization"]} if driver["linearization"] else {}
        else:
            trims = {role: 0.0 for role in trims}
            linearization = {}
    sections_by_driver = sections_by_role(preset.crossover_regions)
    linearization = {
        role: _linearization_entry(entry["filters"], role=role, sections=sections_by_driver, trim_db=trims[role])
        for role, entry in linearization.items()
    }
    roles = required_driver_roles(preset.way_count)
    resolved_alignment, alignment_source = resolve_alignment(
        base.candidate, selected, roles=roles,
        saved=(base_profile or {}).get("timing"), commissioning=evidence.get("commissioning") or {},
    )
    base_analysis = base.candidate.analysis
    read = ((base_analysis.get("evidence") or {}).get("commissioning") or {}).get("alignment") or {}
    if alignment_source == "base" and read.get("committed") and (
        (base_analysis.get("resolution") or {}).get("alignment") == "measured"
        or read.get("timing_verdict") == TIMING_MEASURED
    ):
        fields = alignment_to_candidate_fields({**read["committed"], "alignment_status": "ok"}, roles=roles)
        if resolved_alignment == base.candidate.alignment == MeasuredCrossoverAlignment(*fields):
            evidence["commissioning"] = {
                **(evidence.get("commissioning") or {}), "alignment": {**read, "timing_verdict": TIMING_MEASURED},
            }
            alignment_source = "measured"
    room = dict(selected.get("room", base.candidate.room_correction) or {})
    bass = dict(selected.get("bass", base.candidate.bass_extension) or {})
    rear = dict(selected.get("rear_calibration", base.candidate.rear_calibration) or {})
    # Empty rear sections stay absent from pre-cardioid fingerprints (ADR-0322).
    names = [section.name for section in PRESCRIPTION_SECTIONS if section.compose
             and (section.name != "rear_calibration" or rear or section.name in selected)]
    resolution = {
        name: "base" if name not in selected else "document" if selected[name] else "cleared"
        for name in names
    }
    resolution["alignment"] = alignment_source
    analysis: dict[str, Any] = {
        "kind": COMPOSITION_KIND,
        "measurement_status": "unmeasured",
        "resolution": resolution,
        "evidence": evidence,
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
        room_correction=room, bass_extension=bass, rear_calibration=rear,
    )
    # The room set is emitted here so the emitter's headroom charge runs at
    # compose rather than at apply.
    prove_candidate_config(candidate, compile_candidate_config(
        candidate, playback_device="null", room_peqs=candidate_room_peqs(candidate),
    ))
    return candidate
