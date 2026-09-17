# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0
"""Compose, check, load and record a declared or banked candidate."""
from __future__ import annotations

from jasper.active_speaker.crossover_v2 import durable_state as v2durable
from jasper.web import correction_crossover_v2_state as v2state
from jasper.web.correction_crossover_v2_status import rollback_candidate

import logging
from typing import Any, Awaitable, Callable, Mapping

from jasper.active_speaker import baseline_profile, runtime_contract
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
from jasper.active_speaker.commissioning_experiment import commissioning_candidate
from jasper.active_speaker.candidate_trials import candidate_boost_issue
from jasper.active_speaker.crossover_declaration import (
    CrossoverBelowDeclaredFloor, assert_crossover_honours_declared_floor, change_to_record, declaration_change_for_candidate,
)
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.active_speaker.design_draft import load_design_draft
from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate, MeasuredCrossoverCandidateError, candidate_on_declaration
from jasper.active_speaker.measurement import load_measurement_state
from jasper.active_speaker.measurement_emit import MeasurementGraphRefused, compile_tuning_graph, load_tuning_declaration
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.atomic_io import CONFIG_FILE_MODE, atomic_write_text
from jasper.dsp_apply import DspApplyError, dsp_writer_lock
from jasper.log_event import log_event
from jasper.output_topology import load_output_topology
from jasper.sound import settings as sound_settings
from .sound_active_speaker import apply_measured_crossover_geometry

logger = logging.getLogger(__name__)

async def apply_candidate(
    candidate: MeasuredCrossoverCandidate | str | None = None, *,
    camilla_factory: Callable[[], Any],
    on_candidate_verified: Callable[[], Awaitable[None]] | None = None,
    previous: bool = False,
) -> dict[str, Any]:
    from jasper.active_speaker.linearization_fit import HEADROOM_COST_BASIS_UNKNOWN  # lazy: NumPy is needed only when applying

    from_saved_draft = candidate is None and not previous
    expected = candidate if isinstance(candidate, str) else ""
    prepared: dict[str, Any] = {}
    measurements: Mapping[str, Any] = {}
    async with dsp_writer_lock(baseline_profile.baseline_config_path().parent, source="active_speaker_baseline_apply"):
        try:
            if previous:
                candidate = rollback_candidate(v2state.load_v2_state())
                if candidate is None:
                    raise CrossoverV2Refused("no previous candidate", code="previous_profile_unavailable")
            selected = find_banked_candidate(candidate).candidate if isinstance(candidate, str) else candidate
            topology = load_output_topology()
            draft = load_design_draft(topology=topology)
            measurements = load_measurement_state(topology)
            incumbent = baseline_profile.load_applied_baseline_profile_state()
            declaration = load_tuning_declaration(topology, design_draft=draft)
            if selected is None:
                selected = (candidate_from_applied_profile(topology, incumbent) if incumbent is not None
                             else commissioning_candidate(topology, draft))
            expected = selected.fingerprint
            assert_crossover_honours_declared_floor(candidate_on_declaration(selected, declaration.preset).source_preset)
            text = compile_tuning_graph(declaration, candidate=selected)
            # Boost findings identify measurement graphs, which exclude household EQ.
            measured_sha = baseline_profile.config_text_sha256(text)
            preference_filters, trim_db = sound_settings.saved_sound_layers()
            if preference_filters or trim_db:
                text = compile_tuning_graph(declaration, candidate=selected,
                    preference_filters=preference_filters, output_trim_db=trim_db)
            sha = baseline_profile.config_text_sha256(text)
            proof = runtime_contract.classify_bass_extension_graph(topology, evidence_source="desired", graph_text=text,
                applied_baseline_state={"recomposition_snapshot": baseline_profile.recomposition_snapshot_for(
                    selected, declaration=declaration, design_draft=draft)})
            if not proof.allowed or proof.classification != runtime_contract.GRAPH_APPROVED_ACTIVE_RUNTIME:
                raise CrossoverV2Refused(proof.classification, code="baseline_graph_safety_proof_failed",
                                         issues=proof.issues)
            issue = candidate_boost_issue(measured_sha[:16])
            if issue:
                log_event(logger, "correction.crossover_v2_apply", status="blocked", code=issue["code"], candidate_fingerprint=expected)
                await baseline_profile._record_apply_outcome_into_bundle(measurements, candidate={"issues": [issue]}, apply_state=None, rollback_target=None)
                return {"status": "blocked", "issue": issue, "issues": [issue], "apply": None}
            target = baseline_profile.baseline_candidate_config_path(text)
            prepared = baseline_profile.prepare_applied_baseline_profile(selected, declaration=declaration, design_draft=draft,
                measurements=measurements, config_path=target, config_sha256=sha)
            prepared.update(issues=list(selected.analysis.get("issues") or []),
                            candidate_fingerprint=baseline_profile.baseline_candidate_fingerprint(prepared))
            if from_saved_draft or on_candidate_verified is not None:
                atomic_write_text(target, text, mode=CONFIG_FILE_MODE)
                if not baseline_profile.validate_camilla_config(target).ok_to_apply:
                    raise CrossoverV2Refused("invalid configuration", code="baseline_config_validation_failed")
            offset = baseline_profile.applied_program_level_delta_db(incumbent, prepared)
            summary = v2durable._candidate_summary(selected, topology_pinned=True, headroom_cost_basis=HEADROOM_COST_BASIS_UNKNOWN)
            change = declaration_change_for_candidate(source_preset=selected.source_preset, design_draft=draft)
            if on_candidate_verified is not None:
                await on_candidate_verified()
            load_config, get_current_config_path = v2state.baseline_apply_seams(camilla_factory())
            baseline_profile._baseline_apply_started(topology, prepared)
            async with baseline_profile.load_composed_graph(text, source="active_speaker_baseline_apply", profile=prepared,
                    load_config=load_config, get_current_config_path=get_current_config_path) as (applied, profile):
                with v2state._state_lock:
                    v2state.observe_apply_success(expected, selected_candidate=summary, previous_applied_profile=incumbent,
                        previous_candidate_fingerprint=((incumbent or {}).get("source") or {}).get("measured_candidate_fingerprint"), expected_post_apply_offset_db=offset)
                    update: dict[str, Any] = {"status": "unchanged"}
                    if change:
                        try:
                            saved = apply_measured_crossover_geometry(
                                between_roles=change.between_roles, configured=change.configured, selected=change.selected)
                            state = v2state.load_v2_state() or {}
                            state.update(accepted_sound_revision=saved["revision"], accepted_sound_declaration_change=change_to_record(change), accepted_sound_candidate_fingerprint=expected)
                            v2state.save_v2_state(state, durable=True)
                            update = {"status": "updated"}
                        except Exception as exc:  # noqa: BLE001
                            update = {"status": "failed", "code": getattr(exc, "code", None) or getattr(exc, "reason", None) or type(exc).__name__, "error": str(exc)}
                            log_event(logger, "correction.crossover_v2_declaration_update", level=logging.WARNING, **update)
                result = await baseline_profile._baseline_apply_result(topology, profile, measurements, apply_state=applied)
            log_event(logger, "correction.crossover_v2_apply", status="applied", candidate_fingerprint=expected, config_sha256=sha)
            return {**result, "declaration_update": update, "expected_post_apply_offset_db": round(offset, 3)}
        except (CandidateBankRefusal, CrossoverV2Refused, MeasurementGraphRefused,
                MeasuredCrossoverCandidateError, ActiveSpeakerConfigError, CrossoverBelowDeclaredFloor) as exc:
            code = getattr(exc, "code", None) or getattr(exc, "reason", None) or "compose_refused"
            log_event(logger, "correction.crossover_v2_apply", status="blocked", code=code, candidate_fingerprint=expected)
            baseline_profile._commissioning_refusal(prepared, exc)
            await baseline_profile._record_apply_outcome_into_bundle(measurements, candidate=prepared, apply_state=None, rollback_target=None)
            if not from_saved_draft:
                raise CrossoverV2Refused(str(exc), code=code, issues=getattr(exc, "issues", ())) from exc
            return {"status": "blocked", "profile": prepared, "apply": None, "issues": prepared["issues"]}
        except DspApplyError as exc:
            log_event(logger, "correction.crossover_v2_apply", status="apply_failed", code="apply_failed", candidate_fingerprint=expected)
            result = await baseline_profile._baseline_apply_result(topology, prepared, measurements, apply_state=exc.state, error=exc)
            return {**result, "issue": {"code": "apply_failed", "message": str(exc)}}


def handle_v2_apply(raw: Mapping[str, Any], run_async: Any, camilla_factory: Any) -> dict[str, Any]:
    return run_async(apply_candidate(str(raw.get("expected_candidate_fingerprint") or "").strip(),
                                    camilla_factory=camilla_factory, previous=raw.get("previous") is True))
