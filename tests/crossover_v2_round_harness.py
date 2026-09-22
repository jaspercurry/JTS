# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

from typing import Any

from jasper.active_speaker import baseline_profile as baseline_profile_mod
from jasper.active_speaker.crossover_v2 import journey
from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2.round_evidence import (
    EntryBaseline,
    measured_response_from_analysis,
)
from jasper.active_speaker.crossover_v2.contracts import REFERENCE_MARK_DESIGN_AXIS
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_status as v2status

from tests.crossover_v2_fixtures import _in_room_summed_db, _verify_analysis
from tests.crossover_v2_fixtures import (
    _COMMANDED_FREQS_HZ,
    _seed_applied_stage_1_state,
    _status,
)

__all__ = [
    "_PREVIOUS_CANDIDATE_FINGERPRINT",
    "_bg_run_async",
    "_consume_verify",
    "_household_sentence",
    "_install_applied_graph",
    "_install_entry_baseline",
    "_post_apply_analysis",
    "_restoring_stage_2",
    "_seed_round_state",
    "_stub_restore_doors",
    "_tracking_curve_change_from_entry",
]

def _install_entry_baseline(conductor: Any, *, scale: float) -> EntryBaseline:
    measured = measured_response_from_analysis(
        _verify_analysis(
            conductor.program_for_phase(journey.PHASE_VERIFY), summed_db=_in_room_summed_db() * scale,
        ),
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
    )
    assert measured is not None
    baseline = EntryBaseline.from_measurement(
        measured,
        graph_fingerprint="fp-entry-graph",
        captured_at="2026-08-10T00:00:00Z",
        artifact_ref="entry_baseline_09_a01",
    )
    conductor._measure_entry_baseline = baseline
    return baseline

def _tracking_curve_change_from_entry(
    conductor: Any, *, change_db: float, louder_spike_db: float | None = None,
) -> tuple:
    import numpy as np

    freqs = np.asarray(_COMMANDED_FREQS_HZ, dtype=float)
    baseline = conductor.measure_entry_baseline
    assert baseline is not None, "install the entry baseline first"
    measured_pre = np.interp(
        freqs,
        np.asarray(baseline.curve.hz, dtype=float),
        np.asarray(baseline.curve.db, dtype=float),
    )
    commanded = conductor.measure_commanded_delta
    assert commanded is not None, "the commanded axis is what change_db is relative to"
    commanded_db = np.interp(
        freqs,
        np.asarray(commanded[0], dtype=float),
        np.asarray(commanded[1], dtype=float),
    )
    predicted = measured_pre + commanded_db
    measured = predicted + change_db
    if louder_spike_db is not None:
        measured = measured.copy()
        measured[len(measured) // 2] += louder_spike_db
    return (freqs, measured, predicted)

def _install_applied_graph(monkeypatch, *, boosts: bool) -> None:
    gain_db = 3.0 if boosts else -3.0
    monkeypatch.setattr(
        baseline_profile_mod,
        "load_applied_baseline_profile_state",
        lambda *a, **k: {
            "candidate_fingerprint": "fp-live-graph",
            "recomposition_snapshot": {
                "linearization": {"woofer": [{"gain": gain_db}]},
            },
        },
    )

def _post_apply_analysis(conductor: Any, *, scale: float = 1.0, max_db: float = 0.9):
    return _verify_analysis(
        conductor.program_for_phase(journey.PHASE_VERIFY),
        max_db=max_db,
        summed_db=_in_room_summed_db() * scale,
    )

_HARNESS_VERIFY_INDEX = 0

def _consume_verify(
    conductor: Any, analysis: Any, *, attempt: int = 1,
    index: int = _HARNESS_VERIFY_INDEX, result: Any = None,
) -> Any:
    return conductor._consume_verify(
        index, attempt, analysis, result, phase=journey.PHASE_VERIFY,
    )

_PREVIOUS_CANDIDATE_FINGERPRINT = "fp-previous"

def _seed_round_state(*, previous_candidate: bool = True) -> dict[str, Any]:
    state = _seed_applied_stage_1_state()
    state["verify_priors"]["entry_baseline"] = None
    if previous_candidate:
        state["previous_candidate_fingerprint"] = _PREVIOUS_CANDIDATE_FINGERPRINT
        state["previous_candidate_displaced_by"] = "fp-stage-1"
    v2state.save_v2_state(state)
    return state

def _bg_run_async(coro: Any, *, timeout: Any = None) -> Any:
    import asyncio

    return asyncio.run(coro)

def _stub_restore_doors(monkeypatch) -> list[int]:
    from jasper.active_speaker import staging
    from dataclasses import replace
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.web import correction_crossover_v2_apply as apply_host
    from jasper.active_speaker.design_draft import design_draft_view
    from tests.test_active_speaker_driver_safety import _manual_settings
    from tests.test_active_speaker_baseline_profile import _draft, _MEASURE_EVIDENCE
    from tests.test_active_speaker_measured_crossover_candidate import _candidate
    from tests.crossover_v2_fixtures import _topology

    root = v2state._state_path().parent
    topology = _topology()
    draft = _draft(topology)
    draft["manual_settings"] = _manual_settings()
    draft["manual_settings"]["drivers"][1]["recommended_highpass_hz"] = 2000
    draft = design_draft_view(draft)
    preview = build_crossover_preview(draft)
    preset, _, _ = staging.compile_preset_from_crossover_preview(topology, preview)
    if preset is None:
        raise ValueError("Previous graph fixture has no crossover preset")
    measured = replace(_candidate(), source_preset=preset, analysis=_MEASURE_EVIDENCE)
    from tests.apply_fixtures import prepare_candidate
    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: root / "sessions")
    profile = prepare_candidate(measured, topology, root / "previous.yml", design_draft=draft)
    profile["status"] = "applied"
    state = v2state.load_v2_state() or {}
    if state.get("previous_candidate_fingerprint"):
        state.update(previous_candidate_fingerprint=measured.fingerprint, previous_applied_profile=profile)
        v2state.save_v2_state(state)
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE", str(root / "applied.json"))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(root / "baseline.yml"))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(root / "dsp-apply.json"))
    monkeypatch.setattr(baseline_profile_mod, "load_applied_baseline_profile_state", lambda *a, **k: None)
    monkeypatch.setattr(apply_host, "load_output_topology", lambda: topology)
    monkeypatch.setattr(apply_host, "load_design_draft", lambda **kwargs: draft)
    from jasper.active_speaker.measurement_emit import load_tuning_declaration
    monkeypatch.setattr(apply_host, "load_tuning_declaration", load_tuning_declaration)
    return []

def _restoring_stage_2(monkeypatch, *, load_ok=True) -> tuple[Any, list[int]]:
    """A real stage 2 with a banked prior graph and a hardware stand-in."""
    from pathlib import Path
    import hashlib

    attempts = _stub_restore_doors(monkeypatch)
    class Camilla:
        path = None
        async def get_config_file_path(self, **kwargs):
            return self.path
        async def set_config_file_path(self, path, **kwargs):
            attempts.append(1)
            if not load_ok:
                return False
            self.path = path
            live = baseline_profile_mod.load_applied_baseline_profile_state()
            if live is not None:
                live.update(candidate_fingerprint="previous", config={"sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()})
            return True
    camilla = Camilla()
    return _round_session(camilla_factory=lambda: camilla), attempts

def _household_sentence(conductor: Any, code: str) -> str:
    v2state.persist_conductor_state(conductor, failure_code=code)
    envelope = build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2status.crossover_v2_status_block(),
    })
    return str(envelope["verdict_text"])

def _round_session(*, camilla_factory, index_phase_map=None):
    from jasper.active_speaker.crossover_v2 import durable_state, coordinator
    from jasper.web import correction_crossover_v2_evidence as evidence
    from tests.crossover_v2_fixtures import FakeSeams, _conductor, _MINTED_CAPTURE_SESSION_ID
    import numpy as np

    state = v2state.load_v2_state() or {}
    priors = state.get("verify_priors") or {}
    context = v2host.resolve_conductor_context(_status())
    store, _ = evidence.open_v2_evidence_store(context.topology)
    publish_check, publish_candidate, refs = evidence.bind_evidence_publishers(
        store, _MINTED_CAPTURE_SESSION_ID, _bg_run_async)
    phases = index_phase_map or {1: journey.PHASE_VERIFY}
    opening = journey.open_stage(journey.STAGE_VERIFY_CAPABILITIES, index_phase_map=phases)
    seams = v2host.bind_v2_stage_seams(opening, evidence_store=store,
        capture_session_id=_MINTED_CAPTURE_SESSION_ID, refs=refs,
        publish_check=publish_check, publish_candidate=publish_candidate,
        run_async=_bg_run_async, camilla_factory=camilla_factory, layout=context.preset.channel_map.layout)
    predicted = priors.get("predicted_sum")
    conductor = _conductor(FakeSeams(), session_id=_MINTED_CAPTURE_SESSION_ID, source_preset=context.preset,
        roles_bands=context.roles_bands, fc_hz=context.fc_hz,
        driver_caps_dbfs=context.driver_caps_dbfs, seams=seams,
        index_phase_map=phases, accepted_phases=(journey.PHASE_CHECK, journey.PHASE_MEASURE), applied=True,
        gain_plan_db=state.get("gain_plan_db"), measure_gain_ceiling_db=state.get("measure_gain_ceiling_db"),
        measure_predicted_sum=(np.asarray(predicted["freqs_hz"]), np.asarray(predicted["magnitude_db"])) if predicted else None,
        measure_predicted_spec_report=priors.get("predicted_spec"),
        measure_commanded_delta=durable_state.commanded_delta_prior_from_state(state),
        measure_declared_transfer=durable_state.declared_transfer_prior_from_state(state),
        measure_proposal_fingerprint=priors.get("proposal_fingerprint", ""),
        measure_entry_baseline=durable_state.entry_baseline_prior_from_state(state),
        measure_alignment_objective=priors.get("alignment_objective", ""),
        measure_gate_window_ms=priors.get("gate_window_ms"),
        verify_pilot_transfer_prior=durable_state.pilot_transfer_prior_from_state(state),
        attempt_history=durable_state.attempt_history_from_state(state),
        series_position=coordinator.series_position_from_state(state), speaker_id=context.topology.topology_id,
        tuning_attempt_id=(state.get("candidate") or {}).get("fingerprint", ""))
    v2state.persist_conductor_state(conductor, failure_code=None, evidence=refs)
    return conductor
