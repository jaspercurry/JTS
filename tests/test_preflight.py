# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import yaml

from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop, LevelPolicy, REGIME_SUMMED, request_for_preset
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, REASON_REGISTRY, REASON_WALK_BRANCH_PAIR_UNDECLARED,
    REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS, TEMPLATE_HARD_STOP,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.graph_transfer import complex_channel_transfer
from jasper.active_speaker.measured_crossover_candidate import candidate_room_peqs, compile_candidate_config
from jasper.active_speaker.measurement_programs import preset, run_preset
from jasper.active_speaker.preflight import NEAR_FIELD_SPL_BASIS, PreflightFacts, PreflightIssue, preflight
from jasper.active_speaker.profile import DRIVER_ROLES_BY_WAY, SPL_RAISE_MARGIN_DB
from jasper.active_speaker.run_levels import preflight_levels
from jasper.active_speaker import arm_walk, candidate_parts, preflight_live
from jasper.active_speaker.seat_level_reference import (
    SCHEMA_VERSION as SEAT_LEVEL_SCHEMA_VERSION, AnchorFacts, ResolvedLevel, predicted_rung_admission,
    resolve_anchor_level, rise_without_room_db, rung_lift_bound_db,
)
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.program import FrequencyBand, RoleBand
from jasper.biquad import PeqFilter
from jasper.bass_extension.dynamic import DynamicBassDescriptor, dynamic_bass_gain_reserve_db
from jasper.speaker_layout import measurement_target_id
from jasper.platform import control_client
from tests.active_speaker_fixtures import mono_output_topology
from tests._log_events import event_field_maps
from tests.test_rear_output_foundation import _rear_pair
from tests.test_active_speaker_program_admission import _profile_and_targets
from tests.test_crossover_v2_tuning_scope import (
    BASS_EXTENSION, _room_candidate, tuning_profile as tuning_profile,
)
from tests.test_active_speaker_measured_crossover_candidate import _room_correction


def _boost(boost_db, **changes):
    """BASS_EXTENSION with its transform's DC lift set to ``boost_db``."""
    shape = BASS_EXTENSION["linkwitz_transform"]
    return {**BASS_EXTENSION, "linkwitz_transform": {**shape, "target_hz": shape["source_hz"] / 10 ** (boost_db / 40)},
            **changes}


def ready_facts(plan, **changes):
    return replace(PreflightFacts(
        candidates={}, mic_present=True, mic_identified=True,
        anchor=AnchorFacts({"artifact_schema_version": SEAT_LEVEL_SCHEMA_VERSION, "session_id": "session", "leveled_at": "2026-09-12T00:00:00Z",
                            "target": {"target_db_spl": 75.0, "tolerance_db": 1.0}, "measured_db_spl": 75.0, "reference_volume_db": -18.0,
                            "stimulus": {"stimulus_id": "fixture-sweep"},
                            "mic_sensitivity": {"sens_factor_db": -12.0, "serial": "1234"}},
                           MicSensitivity(-12.0, 18.0, "1234")),
        commissioning_stop_db_spl=85.0, mover=plan.mover, applied_bass_extension={},
        stimulus_ids_for=lambda _plan: ("fixture-sweep",),
    ), **changes)


@pytest.mark.parametrize("muted", [True, False, None])
def test_preflight_output_mute(monkeypatch, caplog, muted):
    response = control_client.ControlResponse(200, b'{"muted": true, "percent": 0}' if muted else b'{"muted": false, "percent": 35}')
    read = Mock(return_value=response, side_effect=control_client.ControlError() if muted is None else None)
    monkeypatch.setattr(control_client, "get", read)
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    ready = ready_facts(plan)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    caplog.set_level(logging.INFO)
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    report = preflight(plan, replace(ready, output_volume=facts.output_volume))
    read.assert_called_once_with("/volume", base_url=control_client.DEFAULT_BASE_URL, timeout=control_client.DEFAULT_TIMEOUT)
    assert report.blocking is (muted is True)
    assert event_field_maps(caplog, "active_speaker.measurement_output_muted") == (
        [{"muted": "true", "household_percent": "0"}] if muted else [])
    if muted:
        issue, = report.issues
        assert issue.code == "measurement_output_muted" and issue.blocking
        assert issue.evidence == {"muted": True, "household_percent": 0}
        assert issue.next_action["id"] == "raise_volume"
        assert REASON_REGISTRY[issue.code].retry_budget == 0
    else:
        assert report.issues == ()


def test_the_dry_run_publishes_each_drivers_cap_and_its_source(monkeypatch):
    _topology, safety, targets = _profile_and_targets(
        woofer_peak=None, tweeter_peak=None, sensitivities={"woofer": 84.0, "tweeter": 109.2})
    monkeypatch.setattr(control_client, "get", Mock(return_value=control_client.ControlResponse(
        200, b'{"muted": false, "percent": 35}')))
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    ready = ready_facts(plan)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), role_targets=targets, safety_profile=safety,
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert preflight(plan, replace(ready, driver_caps=facts.driver_caps)).to_dict()["driver_caps"] == {
        "woofer": {"cap_dbfs": 0.0, "cap_source": "class_default"},
        "tweeter": {"cap_dbfs": pytest.approx(-25.2), "cap_source": "sensitivity_delta:class_default"},
    }


@pytest.mark.parametrize("layout,name,preset,poses", [
    (layout, name, preset, poses)
    for layout in ("full_range_passive", "active_2_way", "active_3_way", "cardioid")
    for name, preset, poses in (
        ("rear", "rear/express", "rear_express"), ("rear", "rear/express", "rear_wide"),
        ("rear", "rear/express", "rear_behind"), ("rear", "rear/pair", "rear_express"),
        ("rear", "rear/pair", "rear_behind"), ("front_rear", "front_rear/express", "tournament_express"),
        ("branches", "branches/express", "tournament_express"))
] + [("active_2_way", name, preset, poses) for name, preset, poses in (
    ("speaker", "speaker/mark", "speaker_mark"), ("room", "room/seat", "room_quick"), ("bass", "bass/axis", "bass_axis"))])
def test_preflight_requires_declared_capture_targets(monkeypatch, tuning_profile, layout, name, preset, poses):
    topology = _rear_pair("mono")[1] if layout == "cardioid" else mono_output_topology(mode=layout)
    targets = active_driver_targets(topology)
    role_targets = {measurement_target_id(t["role"], t.get("output_variant", "primary")): t["target_fingerprint"]
                    for t in targets}
    roles = tuple(RoleBand(t["role"], index, FrequencyBand(20, 20000)) for index, t in enumerate(targets)
                  if t.get("output_variant", "primary") == "primary")
    candidate = _room_candidate(tuning_profile)
    selected = run_preset(preset, poses)
    plan = request_for_preset(selected, mover=selected.mover or "human",
                               candidates=(candidate.fingerprint,) if name in {"rear", "front_rear", "branches"} else ())
    ready = ready_facts(plan)
    context = SimpleNamespace(topology=topology, roles_bands=roles, safety_profile={}, role_targets=role_targets,
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    monkeypatch.setattr(preflight_live, "conductor_status", lambda: {})
    monkeypatch.setattr(preflight_live, "resolve_conductor_context", lambda _: context)
    monkeypatch.setattr(preflight_live, "require_wired_mic", lambda: SimpleNamespace(model_key="minidsp_umik2"))
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: {})
    monkeypatch.setattr(preflight_live, "candidate_from_applied_profile",
                        lambda *a: SimpleNamespace(bass_extension={}, room_correction={}, source_preset=None))
    monkeypatch.setattr(preflight_live.candidate_bank, "find_banked_candidate", lambda _: SimpleNamespace(candidate=candidate))
    facts = preflight_live.read_preflight_facts(plan)
    assert facts.declared_target_ids == tuple(role_targets)
    missing = tuple(sorted({"woofer", "woofer:rear"} - role_targets.keys())) if name in {"rear", "front_rear"} else ()
    invalid_pairs = (tuple(role.role for role in roles),) if name == "branches" and len(roles) != 2 else ()
    blocked = bool(missing or invalid_pairs)

    def stimulus_ids(_plan):
        assert not blocked
        return ("fixture-sweep",)

    report = preflight_levels(plan, replace(facts, stimulus_ids_for=stimulus_ids))
    assert report.blocking is blocked
    if blocked:
        issue, = report.issues
        assert issue.code == REASON_WALK_BRANCH_PAIR_UNDECLARED
        assert issue.blocking
        assert issue.evidence == {"missing_target_ids": missing, "declared_target_ids": tuple(role_targets),
                                  "invalid_branch_target_ids": invalid_pairs}
        assert REASON_REGISTRY[issue.code].template == TEMPLATE_HARD_STOP
        assert REASON_REGISTRY[issue.code].retry_budget == 0
    else:
        assert report.issues == ()


@pytest.mark.parametrize("offered,unoffered", [(("tweeter", "woofer"), ("woofer:rear",)),
                                               (("tweeter", "woofer", "woofer:rear"), ()),
                                               ((), ("woofer", "woofer:rear"))],
                         ids=["two_way", "cardioid", "stereo_pair"])
def test_preflight_refuses_a_near_field_driver_this_speaker_does_not_offer(offered, unoffered):
    plan = AngleCaptureRequest(tuple(
        AngleStop(0, "near_field", kind="close", distance_m=0.015, purpose="reference", driver=driver)
        for driver in ("woofer", "woofer:rear")))
    report = preflight(plan, ready_facts(plan, declared_target_ids=("tweeter", "woofer", "woofer:rear"),
                                         near_field_drivers=offered))
    assert report.blocking is bool(unoffered)
    assert [(issue.code, issue.evidence["unoffered_drivers"]) for issue in report.issues] == (
        [(REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, unoffered)] if unoffered else [])
    if not unoffered:
        assert report.rung_admission["predicted_spl_basis"] == NEAR_FIELD_SPL_BASIS


def test_a_stop_naming_its_driver_is_no_branch_take_on_the_branches_regime():
    """A stop naming its driver plays that driver alone on the drivers graph
    whatever its regime, so preflight checks no branch pair for it and prices
    its one take (ADR-0366)."""
    plan = AngleCaptureRequest((AngleStop(0, "branches", purpose="reference", driver="woofer",
                                          branch_pair="front_rear"),))
    report = preflight(plan, ready_facts(plan, declared_target_ids=("tweeter", "woofer"),
                                         near_field_drivers=("tweeter", "woofer")))
    assert [issue.code for issue in report.issues] == []
    assert [row.graph_scope for row in report.schedule] == ["drivers"]
    assert report.price["captures"] == 1


@pytest.mark.parametrize("program_id,banks", [("nearfield/each", True), ("nearfield", True), ("nearfield/mark", False)])
def test_preflight_refuses_a_program_id_banking_cannot_resolve(program_id, banks):
    """A round banks under its program id, so a plan naming one the registry
    does not hold is refused before it plays, whichever door it came through
    (ADR-0277)."""
    plan = replace(request_for_preset(run_preset("nearfield")), program=program_id)
    report = preflight(plan, ready_facts(plan, near_field_drivers=("woofer", "woofer:rear")))
    assert [issue.code for issue in report.issues if issue.code == REASON_MEASUREMENT_PROGRAM_NOT_OFFERED] == (
        [] if banks else [REASON_MEASUREMENT_PROGRAM_NOT_OFFERED])


@pytest.mark.parametrize("regime,kind,distance_m,tweeter_floor_hz,offered", [
    ("near_field", "close", 0.015, 800.0, True), ("near_field", "close", 0.015, 1000.0, False),
    ("per_driver", "bearing", None, 1000.0, True)])
def test_a_near_field_driver_the_view_cannot_read_is_not_offered(
        monkeypatch, regime, kind, distance_m, tweeter_floor_hz, offered):
    """The near-field sweep stops at 2 kHz and the view reads its top band,
    800 Hz - 2 kHz, only whole, so a driver whose band starts above 800 Hz is
    refused at a near-field pose before a session plays takes no band can read;
    in the far field it plays MEASURE's band (#5696)."""
    plan = AngleCaptureRequest((AngleStop(0, regime, kind=kind, distance_m=distance_m, purpose="reference",
                                          driver="tweeter"),))
    ready = ready_facts(plan)
    context = SimpleNamespace(topology=mono_output_topology(), roles_bands=(), safety_profile={}, role_targets={},
                              driver_bands={"woofer": FrequencyBand(20, 4000), "tweeter": FrequencyBand(tweeter_floor_hz, 20000)},
                              preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: {})
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    report = preflight(plan, replace(ready, near_field_drivers=facts.near_field_drivers))
    assert [issue.code for issue in report.issues] == ([] if offered else [REASON_MEASUREMENT_PROGRAM_NOT_OFFERED])


@pytest.mark.parametrize("layout", ["active_3_way", "cardioid", "active_2_way"],
                         ids=["three_way_active", "cardioid", "two_way_active"])
def test_preflight_per_driver_layout(layout):
    topology = _rear_pair("mono")[1] if layout == "cardioid" else mono_output_topology(mode=layout)
    targets = active_driver_targets(topology)
    plan = request_for_preset(preset("speaker/mark"), mover="human")
    stimulus_ids = Mock(return_value=("fixture-sweep",))
    report = preflight(plan, ready_facts(
        plan, stimulus_ids_for=stimulus_ids,
        declared_target_ids=tuple(measurement_target_id(t["role"], t.get("output_variant", "primary")) for t in targets),
        roles_bands=tuple(RoleBand(t["role"], index, FrequencyBand(20, 20000)) for index, t in enumerate(targets)
                          if t.get("output_variant", "primary") == "primary"),
    ))
    assert report.blocking is (layout == "active_3_way")
    if report.blocking:
        issue, = report.issues
        assert issue.code == REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS
        assert issue.evidence == {"driver_roles": DRIVER_ROLES_BY_WAY[3]}
        assert report.schedule == () and report.price == {}
        assert REASON_REGISTRY[issue.code].template == TEMPLATE_HARD_STOP
        assert REASON_REGISTRY[issue.code].retry_budget == 0
        stimulus_ids.assert_not_called()
    else:
        assert report.issues == ()


@pytest.mark.parametrize("change,code", [
    ("calibration", "measure_spl_calibration_required"),
    ("mover", "walk_over_mover_envelope"),
    ("mic", "wired_mic_missing"),
    ("identity", "measurement_mic_unidentified"),
    ("anchor", "seat_anchor_unusable"),
    ("stop", "walk_commissioning_stop_unset"),
    ("context", "measure_box_not_ready"),
    ("context", "program_measurement_inputs_invalid"),
    ("candidate", "not_found"),
    ("capacity", "walk_over_capture_capacity"),
])
def test_preflight_issues(change, code):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, kind="seat", seat_offset_m=(0, 0, 0)),))
    facts = ready_facts(plan)
    if change == "candidate":
        plan = replace(plan, candidates=("missing",), stops=(replace(plan.stops[0], candidate_id="missing"),))
    elif change == "calibration":
        facts = replace(facts, anchor=replace(facts.anchor, sensitivity=None))
    elif change == "mover":
        facts = replace(facts, mover="arm")
    elif change == "mic":
        facts = replace(facts, mic_present=False)
    elif change == "identity":
        facts = replace(facts, mic_identified=False)
    elif change == "anchor":
        facts = replace(facts, anchor=replace(facts.anchor, record={}))
    elif change in {"stop", "context"}:
        facts = replace(facts, commissioning_stop_db_spl=None,
                        issues=(PreflightIssue.from_code(code, ""),) if change == "context" else ())
    elif change == "capacity":
        plan = replace(plan, repeats=129)
    report = preflight(plan, facts)
    issue, = report.issues
    assert issue.code == code
    assert report.blocking and issue.blocking and issue.next_action
    if change == "capacity":
        assert report.schedule == ()


def test_clean_schedule_preserves_consecutive_places_and_repeat_order(tuning_profile):
    candidate = _room_candidate(tuning_profile)
    name = candidate.fingerprint
    plan = AngleCaptureRequest(
        tuple(AngleStop(angle, REGIME_SUMMED, candidate_id=cid) for angle in (0, 20, 0) for cid in ("", name)),
        candidates=("base", name), repeats=2,
    )
    report = preflight(plan, ready_facts(plan, candidates={name: candidate}))
    assert report.issues == ()
    assert [(row.pose, row.candidate_id, row.repeat) for row in report.schedule] == [
        (stop.place, stop.candidate_id or "base", repeat)
        for stop in plan.stops for repeat in (1, 2)
    ]
    assert report.mic_moves == report.price["mic_moves"] == 3
    assert report.price["captures"] == 14
    assert report.price["ceiling_min"] > 0
    assert report.spl_ceiling_db_spl == 85
    assert report.plan.level.resolved.anchor_db_spl == 75
    assert {row.graph_scope for row in report.schedule} == {"candidate"}


@pytest.mark.parametrize("fault,branch", [("box", False), ("box", True), ("wrong_mic", False), ("no_calibration", False)])
def test_live_facts_surface_owner_refusals(monkeypatch, fault, branch):
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
    from jasper.audio_measurement import calibration, household_mic

    plan = request_for_preset(preset("branches/express"), candidates=("candidate",)) if branch else AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    facts = ready_facts(plan)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: facts.anchor.record)
    monkeypatch.setattr(preflight_live, "conductor_status", lambda: {})

    def context(_status):
        if fault == "box":
            raise CrossoverV2Refused("setup incomplete")
        return SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
            preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))

    monkeypatch.setattr(preflight_live, "resolve_conductor_context", context)
    monkeypatch.setattr(preflight_live, "require_wired_mic", lambda: SimpleNamespace(
        model_key="minidsp_umik2", model_label="UMIK-2"))
    monkeypatch.setattr(household_mic, "resolved_household_mic", lambda: None if fault == "no_calibration" else (
        object(), SimpleNamespace(model="dayton_imm6" if fault == "wrong_mic" else "minidsp_umik2", raw_path="unused")))
    monkeypatch.setattr(calibration, "resolve_mic_sensitivity", lambda **kwargs: facts.anchor.sensitivity)
    report = preflight(plan, preflight_live.read_preflight_facts(plan))
    code = "measure_box_not_ready" if fault == "box" else "measure_spl_calibration_required"
    assert any(issue.code == code and issue.blocking and issue.next_action for issue in report.issues)
    if branch:
        assert report.price == {}


def test_supplied_facts_do_not_read_files(monkeypatch):
    from jasper.active_speaker import seat_level_reference
    from jasper.audio_measurement import calibration

    def unexpected_read(*args, **kwargs):
        pytest.fail("preflight attempted an external read")

    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    facts = ready_facts(plan)
    monkeypatch.setattr(seat_level_reference, "load_seat_level_reference", unexpected_read)
    monkeypatch.setattr(calibration, "resolve_mic_sensitivity", unexpected_read)
    assert preflight(plan, facts).issues == ()


@pytest.mark.parametrize("bass", [False, True])
def test_candidates_are_proved_as_composed(tuning_profile, bass):
    candidate = _room_candidate(tuning_profile)
    if bass:
        candidate = replace(candidate, bass_extension=BASS_EXTENSION)
    name = candidate.fingerprint
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, candidate_id=name),), candidates=(name,))
    report = preflight(plan, ready_facts(plan, candidates={name: candidate}))
    assert report.issues == ()
    assert report.schedule[0].graph_scope == "candidate"


def test_incomplete_candidate_graph_refuses_preflight(monkeypatch, tuning_profile):
    import importlib
    import yaml

    owner = importlib.import_module("jasper.active_speaker.preflight")
    candidate = _room_candidate(tuning_profile)
    name = candidate.fingerprint
    graph = yaml.safe_load(owner.compile_candidate_config(candidate, playback_device="null"))
    for step in graph["pipeline"]:
        if step["type"] == "Filter":
            step["names"] = [n for n in step["names"] if not n.endswith("_hp")]
    monkeypatch.setattr(owner, "compile_candidate_config", lambda *args, **kwargs: yaml.safe_dump(graph))
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, candidate_id=name),), candidates=(name,))
    report = preflight(plan, ready_facts(plan, candidates={name: candidate}))
    issue, = report.issues
    assert issue.code == "measurement_candidate_invalid"
    assert issue.blocking and issue.next_action


@pytest.mark.parametrize("level_db", [None, -25, -9, -8.9, 0])
def test_run_level_keeps_anchor_and_clamps_to_statement_ceiling(level_db):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), level=LevelPolicy(level_db=level_db))
    facts = ready_facts(plan)
    report = preflight(plan, facts)
    requested = -18 if level_db is None else level_db
    admitted = min(requested, -9)
    assert report.plan.level.volume_db == pytest.approx(admitted)
    assert report.plan.level.level_db == (None if level_db is None else pytest.approx(admitted))
    assert report.plan.level.resolved.reference_volume_db == -18
    assert report.to_dict()["level"]["predicted_db_spl"] == pytest.approx(75 + admitted + 18)
    assert not report.blocking
    assert preflight(report.plan, facts).plan == report.plan
    row = report.rung_admission
    assert row.get("bound_by") == ("commissioning_margin" if requested > admitted else None)
    assert row["admitted_db_spl"] <= row["margin_bound_db_spl"] == 84
    assert row["requested_level_db"] == requested


@pytest.mark.parametrize("boost,tolerance,admitted,clamped", [
    (18, 1, 84.0, 84.1), (0, 1, 84.0, 84.1), (20, 1, 83.8, 83.81), (18, 0.5, 84.5, 84.6),
])
def test_jts3_rung_margin_uses_the_applied_stack(tuning_profile, boost, tolerance, admitted, clamped):
    applied = _boost(18, delta_highpass_hz=63, detector_lowpass_hz=100)
    candidate = replace(_room_candidate(tuning_profile),
                        bass_extension=_boost(boost, delta_highpass_hz=63, detector_lowpass_hz=100) if boost else {})
    name = candidate.fingerprint
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, candidate_id=name, purpose="bass"),), candidates=(name,))
    facts = ready_facts(plan, candidates={name: candidate}, applied_bass_extension=applied)
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record, "reference_volume_db": -21,
        "target": {"target_db_spl": 75, "tolerance_db": tolerance}}))
    for spl in (admitted, clamped):
        requested = -21 + spl - 75
        report = preflight(replace(plan, level=LevelPolicy(level_db=requested)), facts)
        assert not report.blocking
        row = report.rung_admission
        fader = report.plan.level.volume_db
        assert fader <= requested
        assert row.get("bound_by") == ("commissioning_margin" if spl == clamped else None)
        assert row["anchor_tolerance_db"] == tolerance
        assert row["lift_bound_db"] == rung_lift_bound_db(candidate.bass_extension, applied)
        assert row["admitted_db_spl"] == report.plan.level.predicted_db_spl
        assert row["admitted_db_spl"] <= row["margin_bound_db_spl"] == 85 - (tolerance + row["lift_bound_db"])
        if spl == admitted:
            assert fader == requested
            assert row["lift_bound_db"] == pytest.approx(0.19923 if boost == 20 else 0, abs=0.001)
            assert row["margin_bound_db_spl"] == pytest.approx(83.80077 if boost == 20 else 85 - tolerance, abs=0.001)


@pytest.mark.parametrize("tolerance", [None, 0, -1, float("nan")])
def test_missing_anchor_tolerance_uses_default_margin(tolerance):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass"),), level=LevelPolicy(level_db=0))
    facts = ready_facts(plan)
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record,
        "target": {"target_db_spl": 75, **({"tolerance_db": tolerance} if tolerance is not None else {})}}))
    report = preflight(plan, facts)
    assert not report.blocking
    row = report.rung_admission
    assert row["margin_basis"] == "default"
    assert row["margin_db"] == SPL_RAISE_MARGIN_DB
    assert row["bound_by"] == "commissioning_margin"
    assert report.plan.level.volume_db == pytest.approx(-11)
    assert report.plan.level.predicted_db_spl == row["admitted_db_spl"] <= row["margin_bound_db_spl"] == 82


@pytest.mark.parametrize("missing", ["max_window_db_spl", "loudest_half_second_db_spl", "ceiling_db_spl", "level_db", "previous_rung"])
@pytest.mark.parametrize("requested", [-14.46, -25])
def test_later_rung_holds_when_previous_capture_spl_is_missing(missing, requested):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass"),), level=LevelPolicy(level_db=requested))
    observation = {"level_db": -21.46, "spl": {"max_window_db_spl": 80.53, "loudest_half_second_db_spl": 76.04, "ceiling_db_spl": 85}}
    if missing == "level_db":
        observation.pop(missing)
    else:
        observation["spl"].pop(missing, None)
    report = preflight(plan, ready_facts(plan), previous_rung=[] if missing == "previous_rung" else [observation])
    blocked = missing in {"level_db", "previous_rung"}
    assert report.blocking is blocked
    row = report.rung_admission
    assert row["unavailable"] == [missing]
    assert row["requested_level_db"] == requested
    if blocked:
        assert [issue.code for issue in report.issues] == ["walk_level_policy_invalid"]
        assert report.issues[0].evidence["unavailable"] == [missing]
        assert row["admitted_db_spl"] is None and row["previous_level_db"] is None
    else:
        assert report.plan.level.volume_db == min(requested, -21.46)
        assert row["previous_level_db"] == -21.46
        assert row.get("bound_by") == ("previous_rung_unmeasured" if requested > -21.46 else None)
        assert report.plan.level.predicted_db_spl == row["admitted_db_spl"] <= row["margin_bound_db_spl"]


@pytest.mark.parametrize("banked,serial,sens_factor,delta", [
    ("1234", "other", -20, 0), ("1234", "1234", -10, -2), ("1234", "1234", -14, 2),
    ("1234", None, -10, 0), ("1234", None, -14, 2), (None, "1234", -10, 0), (None, "1234", -14, 2),
])
def test_calibrated_microphones_resolve_the_banked_anchor(banked, serial, sens_factor, delta):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), level=LevelPolicy(level_db=0))
    facts = ready_facts(plan)
    record = {**facts.anchor.record, "mic_sensitivity": {"sens_factor_db": -12.0, "serial": banked}}
    facts = replace(facts, anchor=AnchorFacts(record, MicSensitivity(sens_factor, 18, serial)))
    report = preflight(plan, facts)
    assert not report.blocking
    row = report.rung_admission
    assert row["anchor_mic_serial"] == banked and row["anchor_rebased_db"] == delta
    assert report.plan.level.resolved.mic_serial == serial
    assert report.plan.level.volume_db == pytest.approx(-9 - delta)
    assert row["bound_by"] == "commissioning_margin"
    assert report.plan.level.predicted_db_spl == row["admitted_db_spl"] <= row["margin_bound_db_spl"]


@pytest.mark.parametrize("carried_reference", [-25, -10])
@pytest.mark.parametrize("requested", [None, 0])
def test_plan_replaces_carried_anchor_without_raising_the_fader(carried_reference, requested):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    facts = ready_facts(plan)
    banked, _ = resolve_anchor_level(facts=facts.anchor)
    carried = replace(banked, anchor_db_spl=60, reference_volume_db=carried_reference)
    plan = replace(plan, level=LevelPolicy(level_db=requested, resolved=carried))
    received = AngleCaptureRequest.from_mapping(json.loads(json.dumps(plan.to_dict())))
    report = preflight(received, facts)
    assert not report.blocking
    assert report.plan.level.resolved == banked
    assert report.plan.level.volume_db == pytest.approx(min(requested, -9) if requested is not None else min(carried_reference, -18))
    assert report.plan.level.volume_db <= plan.level.volume_db
    assert report.rung_admission["carried_anchor_replaced"] is True
    assert report.rung_admission.get("bound_by") == ("commissioning_margin" if requested == 0 else None)
    assert report.plan.level.predicted_db_spl == report.rung_admission["admitted_db_spl"] <= report.rung_admission["margin_bound_db_spl"]


@pytest.mark.parametrize("anchor_spl", [126, 135])
def test_margin_clamp_below_policy_floor_refuses(anchor_spl):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    facts = ready_facts(plan)
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record, "measured_db_spl": anchor_spl}))
    report = preflight(plan, facts)
    assert report.blocking
    assert report.blocking_issue.code == "walk_level_policy_invalid"
    assert report.rung_admission["level_db"] <= -60
    assert report.rung_admission["admitted_db_spl"] is None


@pytest.mark.parametrize("applied_boost", [0, 6, 18])
def test_admitted_fader_and_spl_stay_bounded_over_candidate_grid(tuning_profile, applied_boost):
    base = _room_candidate(tuning_profile)
    descriptors = [{}, *(_boost(boost) for boost in (6, 18, 20))]
    candidates = [replace(base, bass_extension=descriptor) for descriptor in descriptors]
    plan = AngleCaptureRequest(tuple(AngleStop(0, REGIME_SUMMED, candidate_id=c.fingerprint) for c in candidates),
                               candidates=tuple(c.fingerprint for c in candidates))
    applied = _boost(applied_boost) if applied_boost else {}
    facts = ready_facts(plan, candidates={c.fingerprint: c for c in candidates}, applied_bass_extension=applied)
    for requested in (-59, -35, -25, -18, -10, -1, 0):
        report = preflight(replace(plan, level=LevelPolicy(level_db=requested)), facts)
        assert not report.blocking
        fader = report.plan.level.volume_db
        assert fader <= requested
        row = report.rung_admission
        assert report.plan.level.predicted_db_spl == row["admitted_db_spl"] <= row["margin_bound_db_spl"]
        for descriptor in descriptors:
            margin = 1 + rung_lift_bound_db(descriptor, applied)
            assert report.plan.level.predicted_db_spl <= 85 - margin


_CUT = PeqFilter(50.0, 8.0, -6.0)


@pytest.mark.parametrize("room,boost,rise_db", [
    ((), None, 0.0),
    (({"freq": 50.0, "q": 8.0, "gain": -6.0},), None, 6.0),
    (({"freq": 120.0, "q": 2.0, "gain": 3.0},), None, 3.99),  # a lone boost pays its peak and the margin
    (({"freq": 120.0, "q": 2.0, "gain": -3.0},), 3.0, 0.0),  # the room cut nets a driver boost
])
def test_the_room_off_rise_is_the_rooms_charge_less_its_lowest_response_in_band(tuning_profile, room, boost, rise_db):
    """Clearing the applied room layer moves the program charge by what the layer adds to it and
    gives back the layer's response, so the room-off graph plays at most that much louder across
    the band, read off the two compiled graphs (ADR-0385)."""
    band_hz = (20.0, 1100.0)
    boosts = [entry["freq"] for entry in room if entry["gain"] > 0.0]
    candidate = replace(
        _room_candidate(tuning_profile), blend_correction=(), role_attenuations_db={"woofer": 0.0, "tweeter": -3.0},
        linearization={"woofer": {"filters": [{"biquad_type": "Peaking", "freq": 120.0, "q": 2.0, "gain": boost}]}}
        if boost else {},
        room_correction=_room_correction(
            sides={"mono": list(room)}, basis={**_room_correction()["basis"], "admitted_boosts_hz": boosts},
            boost_db_total=sum(entry["gain"] for entry in room if entry["gain"] > 0.0),
            level_cost_db=sum(entry["gain"] for entry in room if entry["gain"] > 0.0),
        ) if room else {},
    )
    rise = rise_without_room_db(candidate_room_peqs(candidate), band_hz,
                                charge_db=candidate_parts.room_layer_charge_db(candidate))
    hz = np.geomspace(*band_hz, 4001)
    with_room, without = (np.abs(complex_channel_transfer(
        yaml.safe_load(compile_candidate_config(played, playback_device="null", room_peqs=candidate_room_peqs(played))),
        hz, input_weights={0: 1.0}, output_channels={"woofer": 0}, allow_limiter_passthrough=True,
    )["woofer"]) for played in (candidate, replace(candidate, room_correction={})))
    assert rise == pytest.approx(rise_db, abs=0.02)
    assert rise == pytest.approx(max(0.0, float(np.max(20.0 * np.log10(without / with_room)))), abs=0.01)


@pytest.mark.parametrize("purpose,candidate_id,stimulus,room,rise_db", [
    ("bass", "", None, _CUT, 6.0), ("bass", "trial", None, _CUT, 6.0),
    ("bass", "", {"ceiling_hz": 1100.0}, PeqFilter(3000.0, 8.0, -6.0), 0.0),
    ("room", "", None, _CUT, None), ("speaker", "", None, _CUT, None)])
def test_a_take_clearing_the_room_layer_folds_its_rise_into_the_opener_margin(
        tuning_profile, purpose, candidate_id, stimulus, room, rise_db):
    """A bass take plays with the applied room layer off, so it can play louder
    than the anchor's graph across its stimulus band; the opener's margin holds
    that rise and the run discloses it. Other purposes play the room layer as
    composed (ADR-0370)."""
    candidates = {"trial": _room_candidate(tuning_profile)} if candidate_id else {}
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose=purpose, candidate_id=candidate_id,
                                          stimulus=stimulus),),
                               candidates=tuple(candidates), level=LevelPolicy(level_db=0))
    report = preflight(plan, ready_facts(plan, candidates=candidates, applied_room_peqs=(room,)))
    row = report.rung_admission
    assert not report.blocking
    assert row.get("room_off_rise_db") == (None if rise_db is None else pytest.approx(rise_db, abs=0.1))
    assert row["margin_bound_db_spl"] == pytest.approx(85 - 1 - row["lift_bound_db"] - (row.get("room_off_rise_db") or 0))
    assert report.plan.level.predicted_db_spl == row["admitted_db_spl"] <= row["margin_bound_db_spl"]


@pytest.mark.parametrize("purpose,blocked", [("bass", True), ("room", False)])
def test_an_unreadable_applied_room_layer_refuses_a_take_that_clears_it(purpose, blocked):
    """With the applied room layer unreadable, a room-off take's rise is
    unknown, so preflight refuses its plan; a take playing the room layer runs
    as before (ADR-0370)."""
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose=purpose),), level=LevelPolicy(level_db=0))
    report = preflight(plan, ready_facts(plan, applied_room_peqs=None))
    assert [issue.code for issue in report.issues if issue.blocking] == (["walk_level_policy_invalid"] if blocked else [])


@pytest.mark.parametrize("state,room", [({}, ()), ({"status": "applied"}, None)])
def test_live_facts_tell_no_applied_room_layer_from_an_unreadable_one(monkeypatch, state, room):
    """No applied profile plays no room layer; an applied one whose candidate
    cannot be read has an unknown one (ADR-0370)."""
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass"),))
    ready = ready_facts(plan)

    def unreadable(*_args):
        raise candidate_parts.CandidateBankRefusal("composition_saved_tune_unavailable", "gone")

    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: state)
    monkeypatch.setattr(preflight_live, "candidate_from_applied_profile", unreadable)
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
                              preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert (facts.applied_room_peqs, facts.applied_room_charge_db) == (room, None if room is None else 0.0)


def test_margin_clamp_lands_under_the_bound():
    requested = -17.621
    candidate = _boost(20)
    row = predicted_rung_admission(requested, ResolvedLevel(70.8695, -22.0129, "1234"), {"trial": candidate},
                                   applied={}, ceiling_db_spl=85, tolerance_db=3)
    assert row["level_db"] < requested
    assert row["admitted_db_spl"] <= row["margin_bound_db_spl"]


def test_unreadable_bass_descriptor_blocks_the_margin():
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass"),))
    report = preflight(plan, ready_facts(plan, applied_bass_extension={"low_boost_db": 6}))
    assert report.blocking_issue.code == "walk_level_policy_invalid"
    assert (report.rung_admission["status"], report.rung_admission["admitted_db_spl"]) == ("blocked", None)


@pytest.mark.parametrize("same", [True, False])
def test_live_opener_compares_the_composed_program_identity(monkeypatch, same):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), level=LevelPolicy(level_db=-12.46))
    anchor = ready_facts(plan).anchor
    record = {**anchor.record, "measured_db_spl": 74.23, "reference_volume_db": -22.23}
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: anchor.sensitivity)
    monkeypatch.setattr(preflight_live, "candidate_from_applied_profile",
                        lambda *a, **kw: SimpleNamespace(bass_extension={}, room_correction={}, source_preset=None))
    context = SimpleNamespace(topology=None, preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)),
        roles_bands=(RoleBand("woofer", 0, FrequencyBand(20, 20000)),), safety_profile={}, role_targets={"woofer": "fp-woofer"}, fc_hz=None,
        driver_caps_dbfs={"woofer": -8}, session_volume_db=-22.23, driver_sweep_duration_limits_s={})
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    ids = facts.stimulus_ids_for(plan)
    assert ids and len(set(ids)) == 1
    record["stimulus"] = {"stimulus_id": ids[0] if same else "different"}
    report = preflight(plan, facts)
    assert not report.blocking
    assert report.plan.level.predicted_db_spl == pytest.approx(84 if same else 74.23)
    row = report.rung_admission
    assert row["stimulus_mismatch"] is (not same)
    assert row["margin_bound_db_spl"] == 84
    if not same:
        assert (row["basis"], row["bound_by"], row["bound_db_spl"]) == (
            "unmeasured_stimulus_opener", "unmeasured_stimulus_opener", 74.23)


@pytest.mark.parametrize("descriptor", [None, {}, BASS_EXTENSION])
def test_live_facts_resolve_applied_bass_from_the_candidate_bank(monkeypatch, tuning_profile, descriptor):
    candidate = replace(_room_candidate(tuning_profile), bass_extension=_boost(18))
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass", candidate_id=candidate.fingerprint),),
                               candidates=(candidate.fingerprint,), level=LevelPolicy(level_db=0))
    anchor = ready_facts(plan).anchor
    applied = replace(_room_candidate(tuning_profile), bass_extension=descriptor or {})
    state = {"status": "applied", "source": {"measured_candidate_fingerprint": applied.fingerprint}}
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: state if descriptor is not None else {})
    monkeypatch.setattr(candidate_parts, "find_banked_candidate", lambda name: {applied.fingerprint: SimpleNamespace(candidate=applied)}[name])
    monkeypatch.setattr(preflight_live.candidate_bank, "find_banked_candidate", lambda _: SimpleNamespace(candidate=candidate))
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda device: anchor.sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
        driver_caps_dbfs={}, fc_hz=None, driver_sweep_duration_limits_s={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert facts.applied_bass_extension == applied.bass_extension
    assert facts.applied_room_peqs == (candidate_room_peqs(applied) if descriptor is not None else ())
    report = preflight(plan, replace(facts, stimulus_ids_for=lambda _: ("fixture-sweep",)))
    assert not report.blocking
    row = report.rung_admission
    assert report.plan.level.volume_db < 0
    assert row["bound_by"] == "commissioning_margin"
    assert report.plan.level.predicted_db_spl == row["admitted_db_spl"] <= row["margin_bound_db_spl"]
    if not descriptor:
        assert row["lift_bound_db"] == dynamic_bass_gain_reserve_db(DynamicBassDescriptor(**candidate.bass_extension))


@pytest.mark.parametrize("level_db,has_ambient,disclosed", [(-24.809, True, False), (-34.809, True, True), (-34.809, False, False)])
@pytest.mark.parametrize("fc_hz,band,ambient_row,floor", [
    (2000, (200, 800), (160, 350, -68.4), -43.4),
    (625, (200, 250), (160, 350, -68.4), -43.4),
    (None, (550, 800), (350, 1000, -71.3), -46.3),
])
def test_summed_pilot_floor_uses_banked_ambient(monkeypatch, level_db, has_ambient, disclosed, fc_hz, band, ambient_row, floor):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), level=LevelPolicy(level_db=level_db))
    anchor = ready_facts(plan).anchor
    sensitivity = replace(anchor.sensitivity, sens_factor_db=-12.07)
    report = {"bands": [{"band_hz": [lo, hi], "level_dbfs": dbfs} for lo, hi, dbfs in (
        (20, 80, -55.8), (80, 160, -66.9), (160, 350, -68.4), (350, 1000, -71.3), (1000, 4000, -75.5), (4000, 12000, -81.1),
    )]}
    record = {**anchor.record, "measured_db_spl": 74.9, "reference_volume_db": -14.809,
              "mic_sensitivity": sensitivity.to_dict(), **({"ambient_report": report} if has_ambient else {})}
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda device: sensitivity)
    monkeypatch.setattr(preflight_live, "candidate_from_applied_profile",
                        lambda *args, **kwargs: SimpleNamespace(bass_extension={}, room_correction={}, source_preset=None))
    context = SimpleNamespace(
        topology=None, preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)),
        roles_bands=(RoleBand("woofer", 0, FrequencyBand(550 if fc_hz is None else 20, 20000)),), safety_profile={}, role_targets={"woofer": "fp-woofer"},
        fc_hz=fc_hz, driver_caps_dbfs={"woofer": -8}, session_volume_db=record["reference_volume_db"], driver_sweep_duration_limits_s={},
    )
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert facts.summed_pilot_band_hz == (band if has_ambient else None)
    outcome = preflight(plan, facts)
    assert not outcome.blocking
    assert preflight(outcome.plan, facts) == outcome
    if disclosed:
        issue, = outcome.to_dict()["issues"]
        assert (issue["code"], issue["blocking"]) == ("run_level_pilots_under_ambient", False)
        assert issue["next_action"]
        lo, hi, dbfs = ambient_row
        assert issue["evidence"] == {
            "level_db": -34.809, "predicted_pilot_capture_dbfs": pytest.approx(-48.1597, abs=0.01),
            "pilot_band_hz": band, "ambient_row": {"band_hz": (lo, hi), "level_dbfs": dbfs},
            "floor_dbfs": pytest.approx(floor),
        }
    else:
        assert outcome.issues == ()


@pytest.mark.parametrize("level_db,disclosed", [(-18, False), (-38, True)])
@pytest.mark.parametrize("purposes", [("bass",), ("room",), ("bass", "room")])
def test_pilot_floor_only_checks_programs_with_pilots(level_db, disclosed, purposes):
    plan = AngleCaptureRequest(tuple(AngleStop(0, REGIME_SUMMED, purpose=purpose) for purpose in purposes),
                               level=LevelPolicy(level_db=level_db))
    facts = ready_facts(plan, summed_pilot_band_hz=(200, 800))
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record,
        "ambient_report": {"bands": [{"band_hz": [20, 80], "level_dbfs": -60},
                                       {"band_hz": [200, 800], "level_dbfs": -60}]}}))
    report = preflight(plan, facts)
    assert (report.blocking, len(report.issues)) == (False, int(disclosed and "room" in purposes))
    for issue in report.issues:
        assert (issue.code, issue.blocking) == ("run_level_pilots_under_ambient", False)
        assert issue.evidence == {
            "level_db": -38, "predicted_pilot_capture_dbfs": pytest.approx(-47.9897, abs=0.01),
            "pilot_band_hz": (200, 800),
            "ambient_row": {"band_hz": (200, 800), "level_dbfs": -60},
            "floor_dbfs": -35,
        }


@pytest.mark.parametrize("mover,attested,blocking", [
    ("arm", None, False), ("arm", False, True), ("arm", True, False), ("human", False, False),
])
def test_rig_clear_attestation_is_only_required_when_asked(mover, attested, blocking):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), mover=mover)
    report = preflight(plan, ready_facts(plan, rig_clear_attested=attested))
    assert report.blocking is blocking
    assert [issue.code for issue in report.issues] == (["walk_rig_clear_not_attested"] if blocking else [])


@pytest.mark.parametrize("attested,available", [(None, True), (False, False), (True, True)])
def test_live_preflight_accepts_facts_without_discovery(monkeypatch, attested, available):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), mover="arm")
    ready = ready_facts(plan)
    detect = Mock(side_effect=AssertionError)
    monkeypatch.setattr(arm_walk.TurntableMover, "available", detect)
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(
        plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"),
        rig_clear_attested=attested, mover_available=available,
    )
    assert facts.rig_clear_attested is attested and facts.mover_available is available
    detect.assert_not_called()
