# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The Save-to-speaker door (the baseline-profile review and apply in
`web.sound_active_speaker`): compile, validate, verify under the DSP lock,
then load; route and composer refusals; apply outcomes and rollback."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from jasper.active_speaker import baseline_apply, baseline_record
from jasper.active_speaker.candidate_bank import bank_candidate


@pytest.fixture
def commissioning_box(tmp_path, monkeypatch):
    from jasper.active_speaker.design_draft import load_design_draft
    from tests.test_correction_crossover_v2_endpoints import _seed_baseline_apply_environment, _FakeApplyCam

    topology, _ = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    draft = load_design_draft()
    draft["manual_settings"]["drivers"][1]["gain_offset_db"] = -11.0
    draft["driver_research"]["crossover_candidates"][0].update(
        delay_target_role="woofer", delay_ms=0.35, upper_polarity="inverted",
    )
    (tmp_path / "design_draft.json").write_text(json.dumps(draft))
    monkeypatch.setattr("jasper.sound.settings.saved_sound_layers", lambda: ([], 0.0))
    monkeypatch.setattr("jasper.web.sound_active_speaker.mux_socket_command", AsyncMock(return_value={}))
    monkeypatch.setattr("jasper.web.sound_active_speaker.trigger_reconcile", lambda **kw: {"ok": True})
    return topology, _FakeApplyCam()


@pytest.mark.parametrize("applied", [False, True], ids=["declared", "banked"])
async def test_accepted_candidate_can_compile_without_a_banked_candidate_id(tmp_path, monkeypatch, commissioning_box, applied):
    from dataclasses import replace
    from jasper.active_speaker import baseline_profile
    from jasper.platform.json_fields import sha256_text
    from jasper.active_speaker.candidate_bank import find_banked_candidate
    from jasper.active_speaker.candidate_parts import candidate_from_design_draft
    from jasper.active_speaker.crossover_v2 import door
    from jasper.active_speaker.crossover_v2.conductor_context import measurement_role_channels
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement_emit import compile_tuning_graph, load_tuning_declaration
    from jasper.web import sound_active_speaker as web

    topology, cam = commissioning_box
    draft = load_design_draft()
    profile = load_tuning_declaration(topology, design_draft=draft)
    profile = replace(profile, role_channels=measurement_role_channels(profile.preset))
    if applied:
        candidate = replace(candidate_from_design_draft(topology, draft), role_attenuations_db={"woofer": -3.0, "tweeter": -8.0},
                            blend_correction=[{"biquad_type": "Peaking", "freq": 2000, "q": 1.0, "gain": -2.0}])
        prepared = baseline_record.prepare_applied_baseline_profile(bank_candidate(candidate), declaration=profile, design_draft=draft)
        baseline_apply.persist_applied_baseline_profile(prepared, apply_state={"result": "success"})
    reviewed = web._active_speaker_baseline_profile_payload()
    assert reviewed["status"] == "ready_to_compile", reviewed["issues"]
    if applied:
        assert reviewed["source"]["measured_candidate_fingerprint"] == candidate.fingerprint
    candidate = find_banked_candidate(reviewed["source"]["measured_candidate_fingerprint"]).candidate
    if not applied:
        assert candidate.analysis["measurement_status"] == "unmeasured"
        assert not (candidate.linearization or candidate.blend_correction or candidate.room_correction or candidate.bass_extension)
        assert candidate.driver_corrections() == {
            "woofer": {"gain_db": 0.0, "delay_ms": 0.35, "inverted": False},
            "tweeter": {"gain_db": -11.0, "delay_ms": 0.0, "inverted": True},
        }
    lookup = Mock(side_effect=AssertionError("accepted candidate went to the bank"))
    monkeypatch.setattr(door, "find_banked_candidate", lookup)
    graph = door.bind_measurement_graph(profile, candidate=candidate, camilla_factory=Mock(), config_dir=tmp_path)
    assert graph.graph_yaml() == compile_tuning_graph(profile, scope="candidate", candidate=candidate)
    assert reviewed["config"]["sha256"] == sha256_text(graph.graph_yaml())
    result = await web._active_speaker_baseline_profile_apply_payload(camilla_factory=lambda: cam)
    assert result["status"] == "applied", result
    assert Path(cam.path).read_text() == graph.graph_yaml()
    assert baseline_profile.load_applied_baseline_profile_state()["source"]["measured_candidate_fingerprint"] == candidate.fingerprint
    graph.select_scope("drivers")
    from jasper.active_speaker.measurement_emit import emit_measurement_graph
    assert graph.graph_yaml() == emit_measurement_graph(profile)
    lookup.assert_not_called()


async def test_commissioning_validates_then_verifies_under_lock_before_loading(monkeypatch, commissioning_box):
    from jasper.web import correction_crossover_v2_apply as apply_host
    from jasper.web import sound_active_speaker as web

    _, cam = commissioning_box
    events = []
    validate = apply_host.validate_camilla_config
    load = cam.set_config_file_path
    def checked(path):
        events.append("validated")
        return validate(path)
    async def loaded(path, **kwargs):
        events.append("loaded")
        return await load(path, **kwargs)
    async def verified():
        from jasper.dsp_control.dsp_apply import _DSP_LOCK_OWNERSHIP
        assert _DSP_LOCK_OWNERSHIP.get() is not None
        events.append("verified")
    monkeypatch.setattr(apply_host, "validate_camilla_config", checked)
    monkeypatch.setattr(cam, "set_config_file_path", loaded)
    result = await web._active_speaker_baseline_profile_apply_payload(
        on_candidate_verified=verified, camilla_factory=lambda: cam,
    )
    assert result["status"] == "applied"
    assert events == ["validated", "verified", "loaded"]


def test_commissioning_review_compiles_without_writing_the_config(commissioning_box):
    from jasper.web.sound_active_speaker import _active_speaker_baseline_profile_payload

    profile = _active_speaker_baseline_profile_payload()
    assert profile["status"] == "ready_to_compile"
    assert profile["permissions"] == {"may_compile": True}
    assert not Path(profile["config"]["path"]).exists()


@pytest.mark.parametrize("change,code", [
    ("trim", None),
    ("protection", "tweeter:required_highpass_missing"),
    ("validation", "baseline_config_validation_failed"),
])
async def test_commissioning_uses_current_draft_and_checks_protection_before_cleanup(tmp_path, monkeypatch, commissioning_box, change, code):
    from jasper.active_speaker import baseline_profile
    from jasper.dsp_control.dsp_apply import CamillaConfigValidationResult, ValidationStatus
    from jasper.web import correction_crossover_v2_apply as apply_host
    from jasper.web import sound_active_speaker as web

    _, cam = commissioning_box
    web._active_speaker_baseline_profile_payload()
    if change == "validation":
        monkeypatch.setattr(apply_host, "validate_camilla_config", lambda path:
                            CamillaConfigValidationResult(ValidationStatus.INVALID_CONFIG, str(path)))
    else:
        path = tmp_path / "design_draft.json"
        draft = json.loads(path.read_text())
        if change == "protection":
            for driver in (draft["manual_settings"]["drivers"][1], *draft["driver_research"]["drivers"]):
                driver.pop("recommended_highpass_hz", None)
                driver.pop("recommended_highpass_slope_db_per_octave", None)
            draft["manual_settings"]["drivers"][1].pop("required_protection_filters", None)
        else:
            draft["manual_settings"]["drivers"][1]["gain_offset_db"] = -12.0
        path.write_text(json.dumps(draft))
    verified = AsyncMock()
    result = await web._active_speaker_baseline_profile_apply_payload(
        on_candidate_verified=verified, camilla_factory=lambda: cam,
    )
    if code is None:
        assert result["status"] == "applied"
        assert result["profile"]["recomposition_snapshot"]["corrections"]["tweeter"]["gain_db"] == -12.0
        verified.assert_awaited_once()
        return
    assert result["status"] == "blocked"
    assert code in {issue["code"] for issue in result["issues"]}
    assert cam.path is None
    assert baseline_profile.load_applied_baseline_profile_state() is None
    verified.assert_not_awaited()


@pytest.mark.parametrize("route,code", [
    ("narrow", "active_playback_route_too_narrow"),
    ("missing", "baseline_playback_device_missing"),
    ("direct", "baseline_output_handoff_not_supported"),
    ("saved_ring", None),
])
async def test_commissioning_and_declaration_refuse_unusable_routes(monkeypatch, commissioning_box, route, code):
    from dataclasses import replace
    from jasper.active_speaker import playback_route
    from jasper.active_speaker.measurement_emit import load_tuning_declaration, MeasurementGraphRefused
    from jasper.web import sound_active_speaker as web

    topology, cam = commissioning_box
    declaration = load_tuning_declaration(topology)
    if route in {"narrow", "missing"}:
        dac = playback_route._dac_by_id(topology.hardware.device_id)
        dac = replace(dac, supports_active_outputd_lane=route == "narrow",
                      active_outputd_lane_channels=1 if route == "narrow" else None)
        monkeypatch.setattr(playback_route, "_dac_by_id", lambda _: dac)
    else:
        monkeypatch.setenv(playback_route.ACTIVE_PLAYBACK_DEVICE_ENV,
                          declaration.playback_device if route == "saved_ring" else "hw:CARD=DAC,DEV=0")
    if code:
        with pytest.raises(MeasurementGraphRefused) as exc:
            load_tuning_declaration(topology)
        assert exc.value.code == code
    else:
        assert load_tuning_declaration(topology).playback_device == declaration.playback_device
    reviewed = web._active_speaker_baseline_profile_payload()
    verified = AsyncMock()
    result = await web._active_speaker_baseline_profile_apply_payload(
        on_candidate_verified=verified, camilla_factory=lambda: cam,
    )
    if code:
        assert reviewed["status"] == result["status"] == "blocked"
        assert code in {issue["code"] for issue in result["issues"]}
        assert cam.path is None
        verified.assert_not_awaited()
    else:
        assert result["status"] == "applied"


@pytest.mark.parametrize("error_kind,code", [
    ("graph", "measurement_candidate_speaker_mismatch"),
    ("candidate", "attenuation_out_of_range"),
    ("bank", "not_found"),
    ("emitter", "compose_refused"),
])
@pytest.mark.parametrize("phase", ["review", "apply_preflight"])
async def test_commissioning_maps_composer_refusals(monkeypatch, commissioning_box, error_kind, code, phase):
    from jasper.active_speaker import measurement_emit
    from jasper.web import correction_crossover_v2_apply as apply_host
    from jasper.active_speaker.candidate_bank import CandidateBankRefusal
    from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError
    from jasper.active_speaker.profile import ActiveSpeakerConfigError
    from jasper.web import sound_active_speaker as web

    _, cam = commissioning_box
    error = {
        "graph": measurement_emit.MeasurementGraphRefused(code, {}),
        "candidate": MeasuredCrossoverCandidateError(code),
        "bank": CandidateBankRefusal(code, "candidate unavailable"),
        "emitter": ActiveSpeakerConfigError("invalid graph"),
    }[error_kind]
    if phase == "review":
        monkeypatch.setattr(measurement_emit, "compile_tuning_graph", Mock(side_effect=error))
        monkeypatch.setattr(apply_host, "compile_tuning_graph", Mock(side_effect=error))
        refused = web._active_speaker_baseline_profile_payload()
        assert refused["status"] == "blocked"
        assert refused["issues"][0]["code"] == code
    else:
        monkeypatch.setattr(apply_host, "load_tuning_declaration", Mock(side_effect=error))
    verified = AsyncMock()
    result = await web._active_speaker_baseline_profile_apply_payload(
        on_candidate_verified=verified, camilla_factory=lambda: cam,
    )
    assert result["status"] == "blocked"
    assert result["issues"][0]["code"] == code
    assert cam.path is None
    verified.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["applied", "apply_failed"])
async def test_commissioning_records_apply_outcomes(tmp_path, monkeypatch, caplog, commissioning_box, outcome):
    from jasper.active_speaker import baseline_profile
    from jasper.web import sound_active_speaker as web
    from tests._log_events import event_fields

    _, cam = commissioning_box
    if outcome == "apply_failed":
        first = await web._active_speaker_baseline_profile_apply_payload(camilla_factory=lambda: cam)
        assert first["status"] == "applied"
        previous = baseline_profile.load_applied_baseline_profile_state()
        load = cam.set_config_file_path
        calls = 0
        async def fail_once(path, **kwargs):
            nonlocal calls
            calls += 1
            return False if calls == 1 else await load(path, **kwargs)
        monkeypatch.setattr(cam, "set_config_file_path", fail_once)
    reviewed = web._active_speaker_baseline_profile_payload()
    caplog.clear()
    caplog.set_level("INFO", logger=baseline_apply.__name__)
    result = await web._active_speaker_baseline_profile_apply_payload(camilla_factory=lambda: cam)
    assert result["status"] == outcome
    started = event_fields(caplog, "correction.crossover_apply_started")
    assert started["candidate_fingerprint"] == reviewed["candidate_fingerprint"]
    if outcome == "apply_failed":
        failed = json.loads((tmp_path / "baseline_profile.json").read_text())
        assert failed["status"] == "apply_failed"
        assert failed["apply"] == result["apply"]
        assert failed["issues"][-1]["code"] == "baseline_profile_apply_failed"
        assert baseline_profile.load_applied_baseline_profile_state() == previous
        rolled_back = event_fields(caplog, "correction.crossover_apply_rolled_back")
        assert rolled_back["rollback_attempted"] == rolled_back["rollback_succeeded"] == "true"
    else:
        succeeded = event_fields(caplog, "correction.crossover_apply_succeeded")
        assert succeeded["candidate_fingerprint"] == succeeded["applied_fingerprint"] == reviewed["candidate_fingerprint"]
