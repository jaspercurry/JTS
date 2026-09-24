# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Resolve saved tune inputs separately from configuration and record building,
and review a tune before it is applied: compile and prove the graph an apply
would load, and disclose what the rear calibration does to it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from jasper.audio_measurement import measurement_geometry
from jasper.audio_measurement.peq import bell_half_width_oct
from jasper.camilla_config_contract import FilterSpec
from jasper import output_topology_store as output_topology
from jasper.output_topology import OutputTopology
from jasper.sound import settings as sound_settings

from . import baseline_profile, baseline_record, candidate_bank, measurement_emit, runtime_contract
from . import design_draft as design_drafts
from ._common import issue as _issue
from .crossover_declaration import assert_crossover_honours_declared_floor
from .measured_crossover_candidate import MeasuredCrossoverCandidate, candidate_on_declaration
from .rear_calibration import rear_operating_facts
from .state_paths import baseline_candidate_config_path, config_text_sha256

REAR_CALIBRATION_WALL_GAP_MISMATCH = "rear_calibration_wall_gap_differs"
REAR_CALIBRATION_FRONT_DELAY_SHIFTS_TIMING = "rear_calibration_front_delay_shifts_timing"
REAR_CALIBRATION_ROOM_BAND_OVERLAP = "rear_calibration_room_band_overlap"
# The wizard declares the wall gap in millimetres while a document carries an
# inch-derived value (0.2032 m), so only a millimetre-scale difference is real.
REAR_CALIBRATION_WALL_GAP_TOLERANCE_M = 0.001


@dataclass(frozen=True)
class AppliedTune:
    banked: candidate_bank.BankedCandidate
    declaration: measurement_emit.MeasurementGraphProfile
    draft: Mapping[str, Any]
    applied: Mapping[str, Any]


def load_applied_tune() -> AppliedTune:
    applied = baseline_profile.load_applied_baseline_profile_state() or {}
    banked = candidate_bank.load_applied_candidate(
        (applied.get("source") or {}).get("measured_candidate_fingerprint", ""), applied_profile=applied,
    )
    topology = output_topology.load_output_topology_strict()
    draft = design_drafts.load_design_draft(topology=topology)
    declaration = measurement_emit.load_tuning_declaration(topology, design_draft=draft)
    measurement_emit.require_candidate_speaker_identity(banked.candidate, declaration.preset)
    bound = candidate_on_declaration(banked.candidate, declaration.preset)
    assert_crossover_honours_declared_floor(bound.source_preset)
    return AppliedTune(banked, declaration, draft, applied)


def compile_applied_tune(
    tune: AppliedTune, *, preference_filters: Sequence[FilterSpec], output_trim_db: float,
) -> tuple[str, dict[str, Any]]:
    text = measurement_emit.compile_tuning_graph(tune.declaration, candidate=tune.banked.candidate,
        preference_filters=preference_filters, output_trim_db=output_trim_db)
    prepared = baseline_record.prepare_applied_baseline_profile(tune.banked, declaration=tune.declaration,
        design_draft=tune.draft, provenance=tune.applied)
    proof = runtime_contract.prove_desired_graph(tune.declaration.topology, text,
                                                 snapshot=prepared.get("recomposition_snapshot"))
    if not runtime_contract.desired_graph_approved(proof):
        raise ValueError(proof.classification)
    return text, prepared


def reviewed_candidate_refusal(
    candidate: Mapping[str, Any], expected_candidate_fingerprint: str,
) -> dict[str, Any] | None:
    if expected_candidate_fingerprint and candidate.get("candidate_fingerprint") == expected_candidate_fingerprint:
        return None
    refused = dict(candidate)
    refused["issues"] = [*(refused.get("issues") or []), _issue(
        "blocker", "baseline_candidate_fingerprint_mismatch",
        "the crossover candidate changed after review; refresh and review the current candidate before applying",
    )]
    return {"status": "blocked", "profile": refused, "apply": None, "issues": refused["issues"]}


def commissioning_refusal(profile: dict[str, Any], exc: Exception) -> None:
    """Mark ``profile`` blocked by ``exc``: its own issues, or one naming its code."""
    profile.update(status="blocked", permissions={"may_compile": False})
    profile["issues"] = getattr(exc, "issues", None) or [_issue(
        "blocker", getattr(exc, "code", None) or getattr(exc, "reason", None) or "compose_refused", str(exc),
    )]


def rear_calibration_issues(candidate: MeasuredCrossoverCandidate) -> list[dict[str, Any]]:
    """Disclose geometry, timing and room-band interactions (ADR-0101, ADR-0322)."""
    document = candidate.rear_calibration
    if not document:
        return []
    issues: list[dict[str, Any]] = []
    fitted_m = (document.get("geometry") or {}).get("cabinet_back_wall_m")
    geometry = measurement_geometry.load_declared_geometry()
    declared_m = None if geometry is None else geometry.cabinet_back_wall_m
    if fitted_m is not None and declared_m is not None and (
        abs(fitted_m - declared_m) > REAR_CALIBRATION_WALL_GAP_TOLERANCE_M
    ):
        issues.append(_issue(
            "warning", REAR_CALIBRATION_WALL_GAP_MISMATCH,
            f"the rear calibration was fitted {fitted_m:g} m from the wall behind the cabinet, "
            f"but the declared rig geometry says {declared_m:g} m",
        ))
    front_delay_ms = (document.get("front") or {}).get("delay_ms")
    if front_delay_ms and (candidate.analysis.get("resolution") or {}).get("alignment") == "measured":
        issues.append(_issue(
            "warning", REAR_CALIBRATION_FRONT_DELAY_SHIFTS_TIMING,
            f"the rear calibration delays the front woofer by {front_delay_ms:g} ms, which moves it "
            "away from the measured woofer/tweeter arrival difference",
        ))
    cancellation_band = rear_operating_facts(document)["band_hz"]
    if cancellation_band:
        for peq in (entry for filters in candidate.room_correction.get("sides", {}).values()
                    for entry in filters if entry["gain"]):
            width = 2 ** bell_half_width_oct(peq["q"])
            room_band = [peq["freq"] / width, peq["freq"] * width]
            if max(room_band[0], cancellation_band[0]) < min(room_band[1], cancellation_band[1]):
                issues.append({
                    **_issue("warning", REAR_CALIBRATION_ROOM_BAND_OVERLAP,
                             "the room correction layer and the rear cancellation branch are "
                             "not reconciled against each other in v1"),
                    "room_band_hz": room_band, "cancellation_band_hz": cancellation_band,
                })
    return issues


def compile_commissioning_profile(
    *, applied_profile: Mapping[str, Any] | None, topology: OutputTopology | None = None,
    design_draft: Mapping[str, Any] | None = None,
    crossover_preview: Mapping[str, Any] | None = None,
    find_candidate: Callable[[str], candidate_bank.BankedCandidate] | None = None,
) -> dict[str, Any]:
    """Review the applied candidate, or bootstrap from the declared crossover.

    The review compiles and proves the graph an apply would load and writes no
    CamillaDSP config."""
    profile: dict[str, Any] = {"artifact_schema_version": baseline_profile.SCHEMA_VERSION,
                              "kind": baseline_profile.BASELINE_PROFILE_KIND,
                              "status": "blocked", "permissions": {"may_compile": False}, "issues": []}
    try:
        topology = topology if topology is not None else output_topology.load_output_topology()
        draft = design_draft if design_draft is not None else design_drafts.load_design_draft(topology=topology)
        declaration = measurement_emit.load_tuning_declaration(topology, design_draft=draft)
        applied = applied_profile
        if applied is not None:
            fingerprint = (applied.get("source") or {}).get("measured_candidate_fingerprint", "")
            banked = (find_candidate(fingerprint) if find_candidate is not None
                      else candidate_bank.load_applied_candidate(fingerprint, applied_profile=applied))
        else:
            from .commissioning_experiment import commissioning_candidate  # lazy: import cost (the program analysis)

            banked = candidate_bank.bank_candidate(
                commissioning_candidate(topology, draft), find_candidate=find_candidate)
        candidate = banked.candidate
        preference_filters, trim_db = sound_settings.saved_sound_layers()
        text = measurement_emit.compile_tuning_graph(declaration, candidate=candidate,
                                                     preference_filters=preference_filters, output_trim_db=trim_db)
        target = baseline_candidate_config_path(text)
        profile.update(baseline_record.prepare_applied_baseline_profile(
            banked, declaration=declaration, design_draft=draft,
            config_path=target, config_sha256=config_text_sha256(text), crossover_preview=crossover_preview,
            saved_timing=(applied or {}).get("timing"),
        ))
        profile["issues"] = [*(candidate.analysis.get("issues") or []), *rear_calibration_issues(candidate)]
        profile["candidate_fingerprint"] = baseline_profile.baseline_candidate_fingerprint(profile)
        profile["config"]["exists"] = target.exists()
        proof = runtime_contract.prove_desired_graph(topology, text, snapshot=profile.get("recomposition_snapshot"))
        if not runtime_contract.desired_graph_approved(proof):
            raise measurement_emit.MeasurementGraphRefused("baseline_graph_safety_proof_failed", proof.classification)
        profile.update(status="ready_to_compile", permissions={"may_compile": True})
    except (candidate_bank.CandidateBankRefusal, ValueError) as exc:
        commissioning_refusal(profile, exc)
    return profile
