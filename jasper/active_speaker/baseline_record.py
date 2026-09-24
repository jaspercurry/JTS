# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Build the applied-baseline record a graph is applied under, from resolved
inputs, without reading or writing any store."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from jasper.json_fields import utc_now_iso as _utc_now
from jasper.output_topology import (
    OutputTopology,
    canonical_fingerprint as _fingerprint,
    topology_config_fingerprint,
)

from .baseline_profile import (
    BASELINE_PROFILE_KIND, PROVENANCE_AUTHORED_BY_MODEL, PROVENANCE_MANUAL, PROVENANCE_MEASURED, SCHEMA_VERSION,
)
from .candidate_bank import BankedCandidate
from .crossover_preview import build_crossover_preview, crossover_preview_fingerprint
from .measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    candidate_on_declaration, driver_corrections, effective_preset,
)
from .measurement import empty_driver_check_summary
from .measurement_emit import MeasurementGraphProfile
from .measurement_programs import PROGRAM_DOCUMENT_ORDER
from .profile import ActiveSpeakerPreset, required_driver_roles


def _source_payload(
    topology: OutputTopology,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    *,
    measured_candidate_fingerprint: str | None = None,
    driver_protection: Mapping[str, Any] | None = None,
    candidate_graph_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fingerprint declaration and evidence inputs, not emitted bytes."""
    source = {
        "topology_id": topology.topology_id,
        "topology_fingerprint": topology_config_fingerprint(topology),
        # Banked driver trims must match the declaration they measured.
        "crossover_preview_fingerprint": crossover_preview_fingerprint(
            crossover_preview, design_draft
        ),
        # Frozen at the retired driver-check record's no-record answer; see
        # empty_driver_check_summary.
        "measurements_updated_at": None,
        "measurement_summary_fingerprint": _fingerprint(empty_driver_check_summary(topology)),
    }
    if measured_candidate_fingerprint is not None:
        source["measured_candidate_fingerprint"] = measured_candidate_fingerprint
    if driver_protection is not None:
        source["driver_protection_fingerprint"] = _fingerprint(driver_protection)
    if candidate_graph_context is not None:
        device_context = {
            key: value for key, value in candidate_graph_context.items()
            if key != "measured_candidate_fingerprint"
        }
        source["candidate_graph_context_fingerprint"] = _fingerprint(device_context)
    return {**source, "fingerprint": _fingerprint(source),
            "design_draft_updated_at": design_draft.get("updated_at")}


def _measured_candidate_metadata(
    candidate: MeasuredCrossoverCandidate, preset: ActiveSpeakerPreset,
    topology: OutputTopology, created_at: str,
) -> dict[str, Any]:
    roles = required_driver_roles(preset.way_count)
    groups = sorted(group.id for group in topology.speaker_groups if group.mode in {"active_2_way", "active_3_way"})
    measured = candidate.analysis.get("measurement_status") != "unmeasured"
    origin = PROVENANCE_MEASURED if measured else PROVENANCE_MANUAL
    return {
        "sources": {role: "measured" if measured else "operator_pinned" for role in roles},
        "gain_provenance": {role: "measured" if measured else "operator_pinned" for role in roles},
        "provisional": False,
        "corrections_provenance": {role: {"gain_db": origin} for role in roles},
        "level_match": {"groups_total": len(groups), "groups_measured": len(groups) if measured else 0,
                        "comparison": "strict_measured_candidate" if measured else "", "incomparable_groups": [],
                        "applied": measured, "newest_capture_at": created_at if measured else None},
        "automatic_candidate": {"ready": measured, "reason": None, "detail": "",
                                "required_group_ids": groups, "measured_group_ids": groups if measured else [],
                                "summed_group_ids": groups if measured else [],
                                "measurement_comparable": measured, "excitation_comparable": measured},
    }


def protection_projection(profile: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "targets": [{
            "role": target["role"],
            "target_fingerprint": target["target_fingerprint"],
            "required_protection_filters": [dict(requirement) for requirement in target["required_protection_filters"]],
        } for target in profile["targets"]],
    }


def _candidate_timing(
    candidate: MeasuredCrossoverCandidate, at: str, provenance: Mapping[str, Any] | None,
    saved_timing: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    from jasper.audio_measurement.program_analysis.model import TIMING_AUTHORED  # lazy: analysis loads NumPy

    evidence = candidate.analysis
    source = (evidence.get("resolution") or {}).get("alignment")
    if source == "saved":
        return provenance.get("timing") if provenance is not None else dict(saved_timing) if saved_timing else None
    if source in ("cleared", "base"):
        return None
    if source is None and provenance is not None and provenance.get("timing") is not None:
        return provenance["timing"]
    commissioning = (evidence.get("evidence") or {}).get("commissioning") or {}
    read = commissioning.get("alignment") or {}
    if source == "measured":
        pair = read["committed"]
        return {"delay_us": pair["delay_us"], "polarity": pair["polarity"], "provenance": PROVENANCE_MEASURED,
                "measured": {**{key: read[key] for key in ("margin_db", "residual_rms_db", "repeat_spread_db", "repeat_spread_us",
                                                         "repeat_count", "round_id", "take_id", "graph_fingerprint")}, "at": at}}
    if (source == "document"
            or evidence.get("timing_verdict") == TIMING_AUTHORED) and candidate.alignment.delay_us is not None:
        roles = required_driver_roles(candidate.source_preset.way_count)
        return {"delay_us": candidate.alignment.delay_us * (1 if candidate.alignment.delay_role == roles[1] else -1),
                "polarity": "inverted" if candidate.alignment.polarity == "invert" else "normal",
                "provenance": PROVENANCE_AUTHORED_BY_MODEL}
    return None


def recomposition_snapshot_for(
    candidate: MeasuredCrossoverCandidate,
    *,
    declaration: MeasurementGraphProfile,
    design_draft: Mapping[str, Any],
    projected: MeasuredCrossoverCandidate | None = None,
    topology_fingerprint: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """THE writer of the section set every graph-safety proof recomposes from.

    The pre-apply proof and the persisted profile must recompose the same
    sections; one a single caller assembles by hand is a graph the runtime
    door cannot prove (ADR-0322's ``rear_calibration``).
    """
    from .linearization_fit import linearization_filters_by_role  # lazy: applied graph recording imports NumPy

    shaped = candidate if projected is None else projected
    return {
        **((provenance or {}).get("recomposition_snapshot") or {}),
        "schema_version": 1, "domain": "full", "topology_id": declaration.topology.topology_id,
        "topology_fingerprint": topology_fingerprint or topology_config_fingerprint(declaration.topology),
        "preset": effective_preset(candidate_on_declaration(shaped, declaration.preset)).to_dict(),
        "corrections": driver_corrections(shaped),
        "linearization": linearization_filters_by_role(candidate.linearization),
        **{field.name: field.type(getattr(candidate, field.name)) for row in PROGRAM_DOCUMENT_ORDER
           for field in row.candidate_fields if field.snapshot and field.name != "linearization"},
        "driver_protection": protection_projection(design_draft.get("driver_safety_profile")),
        "playback_device": declaration.playback_device,
        "measured_candidate_fingerprint": candidate.fingerprint,
    }


def prepare_applied_baseline_profile(
    banked: BankedCandidate,
    *,
    declaration: MeasurementGraphProfile,
    design_draft: Mapping[str, Any],
    config_path: str | Path | None = None,
    crossover_preview: Mapping[str, Any] | None = None,
    config_sha256: str | None = None,
    applied_at: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    saved_timing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an apply record from resolved inputs without reading or writing the bank."""
    candidate = banked.candidate
    protection = protection_projection(design_draft.get("driver_safety_profile"))
    if crossover_preview is None:
        crossover_preview = build_crossover_preview(design_draft)
    source = _source_payload(
        declaration.topology, design_draft, crossover_preview,
        measured_candidate_fingerprint=candidate.fingerprint, driver_protection=protection,
    )
    source = {**source, **((provenance or {}).get("source") or {}),
              **({"driver_protection_fingerprint": _fingerprint(protection)} if protection is not None else {}),
              "measured_candidate_fingerprint": candidate.fingerprint}
    source["fingerprint"] = _fingerprint({key: value for key, value in source.items() if key != "fingerprint"})
    from .crossover_v2.alignment_prescription import alignment_to_candidate_fields  # lazy: alignment_prescription loads NumPy

    at = applied_at or _utc_now()
    timing = _candidate_timing(candidate, at, provenance, saved_timing)
    projected = candidate
    if timing is not None:
        fields = alignment_to_candidate_fields({**timing, "alignment_status": "ok"},
                                              roles=required_driver_roles(candidate.source_preset.way_count))
        projected = replace(candidate, alignment=MeasuredCrossoverAlignment(*fields))
    meta = _measured_candidate_metadata(candidate, declaration.preset, declaration.topology, at)
    snapshot = recomposition_snapshot_for(candidate, declaration=declaration, design_draft=design_draft,
        projected=projected, topology_fingerprint=source["topology_fingerprint"], provenance=provenance)
    corrections, linearization = snapshot["corrections"], snapshot["linearization"]
    applied = {
        **(provenance or {}),
        "artifact_schema_version": SCHEMA_VERSION, "kind": BASELINE_PROFILE_KIND,
        "candidate_artifact_path": str(banked.path),
        "source": source,
        "config": {**((provenance or {}).get("config") or {}), "path": str(config_path or ""),
                   "basename": Path(config_path).name if config_path else "", "sha256": config_sha256, "exists": bool(config_path),
                   "playback_device": declaration.playback_device, "domain": "full"},
        "corrections": corrections, "linearization": linearization,
        "corrections_source": (provenance or {}).get("corrections_source", meta["sources"]),
        **{key: (provenance or {}).get(key, meta[key]) for key in
           ("gain_provenance", "corrections_provenance", "level_match", "automatic_candidate")},
        "linearization_outcome": (provenance or {}).get("linearization_outcome", candidate.linearization_outcome),
        "trim_decision": (provenance or {}).get("trim_decision", dict(candidate.trim_decision)),
        "tuning_owner": (provenance or {}).get("tuning_owner", "automatic"),
        "blend_correction": snapshot["blend_correction"], "room_correction": snapshot["room_correction"],
        "recomposition_snapshot": snapshot,
    }
    if timing is not None:
        applied["timing"] = timing
    else:
        applied.pop("timing", None)
    snapshot.pop("corrections_provenance", None)
    return applied
