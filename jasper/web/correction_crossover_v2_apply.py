# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0
"""Compose, prove, load and record one banked candidate."""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping

from jasper.active_speaker import baseline_profile, runtime_contract
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate
from jasper.active_speaker.candidate_trials import candidate_boost_issue
from jasper.active_speaker.commission_wiring import resolve_commission_preset
from jasper.active_speaker.crossover_declaration import (
    assert_crossover_honours_declared_floor, change_to_record, declaration_change_for_candidate,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.crossover_v2.conductor_context import measurement_role_channels
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.active_speaker.design_draft import load_design_draft
from jasper.active_speaker.driver_safety import evaluate_driver_safety_profile
from jasper.active_speaker.measured_crossover_candidate import candidate_on_declaration
from jasper.active_speaker.measurement import load_measurement_state
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
from jasper.active_speaker.playback_route import resolve_active_playback_device
from jasper.dsp_apply import DspApplyError
from jasper.log_event import log_event
from jasper.output_topology import load_output_topology
from . import correction_crossover_v2 as host
from .sound_active_speaker import apply_measured_crossover_geometry

def handle_v2_apply(raw: Mapping[str, Any], run_async: Any, camilla_factory: Any) -> dict[str, Any]:
    from jasper.active_speaker.linearization_fit import HEADROOM_COST_BASIS_UNKNOWN  # lazy: NumPy is needed only when applying
    expected = str(raw.get("expected_candidate_fingerprint") or "").strip()
    async def apply() -> dict[str, Any]:
        try:
            candidate = find_banked_candidate(expected).candidate
            topology = load_output_topology()
            draft = load_design_draft(topology=topology)
            safety = draft.get("driver_safety_profile")
            try:
                if not isinstance(safety, Mapping) or not evaluate_driver_safety_profile(safety, topology).confirmed_and_current:
                    raise ValueError("Confirm the declared driver limits.")
                protection = confirmed_protection_sections(safety)
            except ValueError as exc:
                raise CrossoverV2Refused(str(exc), code="driver_safety_profile_not_confirmed") from exc
            try:
                preset = resolve_commission_preset(topology, crossover_preview=build_crossover_preview(draft))
                declaration = MeasurementGraphProfile(preset, topology, measurement_role_channels(preset),
                    str(resolve_active_playback_device(topology)[0] or ""), protection)
                assert_crossover_honours_declared_floor(candidate_on_declaration(candidate, preset).source_preset)
                text = compile_tuning_graph(declaration, candidate=candidate)
            except ValueError as exc:
                raise CrossoverV2Refused(str(exc), code=getattr(exc, "code", None) or getattr(exc, "reason", None) or "compose_refused") from exc
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            proof = runtime_contract.classify_bass_extension_graph(topology, evidence_source="desired", graph_text=text,
                applied_baseline_state={"recomposition_snapshot": {"bass_extension": candidate.bass_extension}})
            if not proof.allowed or proof.classification != runtime_contract.GRAPH_APPROVED_ACTIVE_RUNTIME:
                raise CrossoverV2Refused(proof.classification, code="baseline_graph_safety_proof_failed")
            issue = candidate_boost_issue(sha[:16])
            if issue:
                log_event(host.logger, "correction.crossover_v2_apply", status="blocked", code=issue["code"], candidate_fingerprint=expected)
                return {"status": "blocked", "issue": {"code": issue["code"], "message": issue["message"]}}
            prepared = baseline_profile.prepare_applied_baseline_profile(candidate, declaration=declaration, design_draft=draft,
                measurements=load_measurement_state(topology), config_path=baseline_profile.baseline_candidate_config_path(sha), config_sha256=sha)
            previous = baseline_profile.load_applied_baseline_profile_state()
            offset = baseline_profile.applied_program_level_delta_db(previous, prepared)
            summary = host._candidate_summary(candidate, topology_pinned=True, headroom_cost_basis=HEADROOM_COST_BASIS_UNKNOWN)
            change = declaration_change_for_candidate(source_preset=candidate.source_preset, design_draft=draft)
            load, current = host.baseline_apply_seams(camilla_factory())
            async with baseline_profile.load_composed_graph(text, sha, source="active_speaker_baseline_apply", load_config=load, get_current_config_path=current) as applied:
                profile = baseline_profile.persist_applied_baseline_profile(prepared, apply_state=applied.to_dict())
                baseline_profile.promote_applied_baseline_candidate(profile)
                with host._state_lock:
                    host.observe_apply_success(expected, selected_candidate=summary, previous_applied_profile=previous,
                        previous_candidate_fingerprint=((previous or {}).get("source") or {}).get("measured_candidate_fingerprint"), expected_post_apply_offset_db=offset)
                    update: dict[str, Any] = {"status": "unchanged"}
                    if change:
                        try:
                            saved = apply_measured_crossover_geometry(expected_revision=draft.get("revision", 0),
                                between_roles=change.between_roles, configured=change.configured, selected=change.selected)
                            state = host.load_v2_state() or {}
                            state.update(accepted_sound_revision=saved["revision"], accepted_sound_declaration_change=change_to_record(change), accepted_sound_candidate_fingerprint=expected)
                            host.save_v2_state(state, durable=True)
                            update = {"status": "updated"}
                        except Exception as exc:  # noqa: BLE001
                            update = {"status": "failed", "code": getattr(exc, "code", None) or getattr(exc, "reason", None) or type(exc).__name__, "error": str(exc)}
                            log_event(host.logger, "correction.crossover_v2_declaration_update", level=logging.WARNING, **update)
            log_event(host.logger, "correction.crossover_v2_apply", status="applied", candidate_fingerprint=expected, config_sha256=sha)
            return {"status": "applied", "profile": profile, "apply": applied.to_dict(), "declaration_update": update, "expected_post_apply_offset_db": round(offset, 3)}
        except (CandidateBankRefusal, CrossoverV2Refused) as exc:
            log_event(host.logger, "correction.crossover_v2_apply", status="blocked", code=exc.code, candidate_fingerprint=expected)
            if isinstance(exc, CandidateBankRefusal):
                raise CrossoverV2Refused(exc.detail, code=exc.code) from exc
            raise
        except DspApplyError as exc:
            log_event(host.logger, "correction.crossover_v2_apply", status="apply_failed", code="apply_failed", candidate_fingerprint=expected)
            return {"status": "apply_failed", "apply": exc.state.to_dict(), "issue": {"code": "apply_failed", "message": str(exc)}}
    return run_async(apply())
