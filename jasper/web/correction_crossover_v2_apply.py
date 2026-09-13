# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0
"""Compose, prove, load and record one banked candidate."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

from jasper.active_speaker import baseline_profile, runtime_contract
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate
from jasper.active_speaker.candidate_trials import candidate_boost_issue
from jasper.active_speaker.crossover_declaration import (
    assert_crossover_honours_declared_floor, change_to_record, declaration_change_for_candidate,
)
from jasper.active_speaker.crossover_v2.conductor_context import measurement_role_channels
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.active_speaker.design_draft import load_design_draft
from jasper.active_speaker.driver_safety import evaluate_driver_safety_profile
from jasper.active_speaker.measurement import load_measurement_state
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
from jasper.active_speaker.playback_route import resolve_active_playback_device
from jasper.atomic_io import atomic_write_text
from jasper.dsp_apply import DspApplyError, apply_dsp_config, dsp_writer_lock
from jasper.log_event import log_event
from jasper.output_topology import load_output_topology
from . import correction_crossover_v2 as host
from .sound_active_speaker import apply_measured_crossover_geometry

def handle_v2_apply(raw: Mapping[str, Any], run_async: Any, camilla_factory: Any) -> dict[str, Any]:
    expected = str(raw.get("expected_candidate_fingerprint") or "").strip()
    try:
        candidate = find_banked_candidate(expected).candidate
    except CandidateBankRefusal as exc:
        raise CrossoverV2Refused(exc.detail, code=exc.code) from exc

    async def apply() -> dict[str, Any]:
        target = baseline_profile.baseline_config_path()
        async with dsp_writer_lock(target.parent, source="active_speaker_baseline_apply"):
            topology = load_output_topology()
            draft = load_design_draft(topology=topology)
            safety = draft.get("driver_safety_profile")
            if not isinstance(safety, Mapping) or not evaluate_driver_safety_profile(safety, topology).confirmed_and_current:
                raise CrossoverV2Refused("Confirm the declared driver limits.", code="driver_safety_profile_not_confirmed")
            try:
                playback, _ = resolve_active_playback_device(topology)
                declaration = MeasurementGraphProfile(
                    candidate.source_preset, topology, measurement_role_channels(candidate.source_preset),
                    str(playback or ""), confirmed_protection_sections(safety),
                )
                assert_crossover_honours_declared_floor(candidate.source_preset)
                text = compile_tuning_graph(declaration, candidate=candidate)
            except ValueError as exc:
                raise CrossoverV2Refused(str(exc), code=getattr(exc, "code", getattr(exc, "reason", "baseline_graph_safety_proof_failed"))) from exc
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            proof = runtime_contract.classify_bass_extension_graph(
                topology, evidence_source="desired", graph_text=text,
                applied_baseline_state={"recomposition_snapshot": {"bass_extension": candidate.bass_extension}},
            )
            if not proof.allowed or proof.classification != runtime_contract.GRAPH_APPROVED_ACTIVE_RUNTIME:
                raise CrossoverV2Refused(proof.classification, code="baseline_graph_safety_proof_failed")
            issue = candidate_boost_issue(sha[:16])
            if issue:
                return {"status": "blocked", "issue": {"code": issue["code"], "message": issue["message"]}}
            target = target.with_name(f"{target.stem}_candidate_{sha[:12]}{target.suffix}")
            atomic_write_text(target, text, mode=0o640)
            cam = camilla_factory()
            try:
                applied = await apply_dsp_config(
                    source="active_speaker_baseline_apply", candidate_path=target,
                    load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
                    get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
                    expected_candidate_sha256=sha,
                )
            except DspApplyError as exc:
                return {"status": "apply_failed", "apply": exc.state.to_dict(),
                        "issue": {"code": "apply_failed", "message": str(exc)}}
            previous = baseline_profile.load_applied_baseline_profile_state()
            profile = baseline_profile.persist_applied_baseline_profile(
                candidate, declaration=declaration, design_draft=draft, measurements=load_measurement_state(topology),
                config_path=target, config_sha256=sha, apply_state=applied.to_dict(),
            )
            baseline_profile.promote_applied_baseline_candidate(profile)
            offset = baseline_profile.applied_program_level_delta_db(previous, profile)
            with host._state_lock:
                host.observe_apply_success(expected, selected_candidate=host._candidate_summary(candidate, topology_pinned=True),
                    previous_candidate_fingerprint=((previous or {}).get("source") or {}).get("measured_candidate_fingerprint"),
                    previous_applied_profile=previous, expected_post_apply_offset_db=offset)
                change = declaration_change_for_candidate(source_preset=candidate.source_preset, design_draft=draft)
                if change:
                    saved = apply_measured_crossover_geometry(
                        expected_revision=draft.get("revision", 0), between_roles=change.between_roles,
                        configured=change.configured, selected=change.selected,
                    )
                    state = host.load_v2_state() or {}
                    state.update(accepted_sound_revision=saved["revision"], accepted_sound_declaration_change=change_to_record(change), accepted_sound_candidate_fingerprint=expected)
                    host.save_v2_state(state, durable=True)
            log_event(host.logger, "correction.crossover_v2_apply", status="applied", candidate_fingerprint=expected, config_sha256=sha)
            return {"status": "applied", "profile": profile, "apply": applied.to_dict(), "expected_post_apply_offset_db": round(offset, 3)}
    return run_async(apply())
