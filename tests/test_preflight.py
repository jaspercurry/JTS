# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import math
import random
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import yaml

from jasper.active_speaker.angle_capture import (
    AngleCaptureRequest, AngleStop, LevelPolicy, REGIME_PER_DRIVER, REGIME_SUMMED, request_for_preset,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, REASON_REGISTRY, REASON_WALK_BRANCH_PAIR_UNDECLARED,
    REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS, TEMPLATE_HARD_STOP,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.graph_transfer import complex_channel_transfer
from jasper.active_speaker.crossover_section import CrossoverSection
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.measured_crossover_candidate import candidate_room_peqs, plays_rear
from jasper.active_speaker.measurement_emit import (
    MeasurementGraphProfile, compile_tuning_graph, room_layer_charge_db, timing_floor_db,
)
from jasper.active_speaker.measurement_programs import REGIME_BRANCHES, Pose, available_presets, preset, run_preset
from jasper.active_speaker.preflight import (
    PreflightFacts, PreflightIssue, bass_lift_db, preflight, rise_without_room_db,
)
from jasper.active_speaker.profile import DRIVER_ROLES_BY_WAY
from jasper.active_speaker.run_levels import preflight_levels
from jasper.active_speaker import arm_walk, candidate_parts, preflight_live
from jasper.active_speaker.anchor_provenance import read_pose
from jasper.active_speaker.seat_level_reference import (
    SCHEMA_VERSION as SEAT_LEVEL_SCHEMA_VERSION, AnchorFacts, resolve_anchor_level,
)
from jasper.audio_measurement import measurement_geometry
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.measurement_geometry import DECLARED_GEOMETRY_UNREADABLE
from jasper.audio_measurement.program import FrequencyBand, RoleBand
from jasper.platform.biquad import FilterSpec, PeqFilter, filter_response_db, freq_trig
from jasper.platform.speaker_layout import measurement_target_id
from jasper.platform import control_client
from tests.active_speaker_fixtures import mono_output_topology
from tests._log_events import event_field_maps
from tests.test_active_speaker_audition import ACTIVE_PCM
from tests.test_rear_output_foundation import _rear_document, _rear_pair
from tests.test_active_speaker_program_admission import _profile_and_targets
from tests.test_crossover_v2_tuning_scope import (
    BASS_EXTENSION, _room_candidate, _trial_candidate, tuning_profile as tuning_profile,
)
from tests.test_active_speaker_measured_crossover_candidate import _room_correction

BASS, NEAR_FIELD = preset("bass/axis").stimulus, preset("nearfield/each").stimulus


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
    ), **changes)


@pytest.mark.parametrize("muted", [True, False, None])
def test_preflight_output_mute(monkeypatch, caplog, muted):
    response = control_client.ControlResponse(200, b'{"muted": true, "percent": 0}' if muted else b'{"muted": false, "percent": 35}')
    read = Mock(return_value=response, side_effect=control_client.ControlError() if muted is None else None)
    monkeypatch.setattr(control_client, "get", read)
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
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


def test_an_unreadable_declared_geometry_refuses_the_run_before_it_plays(monkeypatch):
    """Every take gates to the declared room, so an unreadable one refuses by its field (ADR-0388)."""
    def unreadable():
        raise measurement_geometry.GeometryFieldError("front_wall_m", "front_wall_m is no longer read")

    monkeypatch.setattr(measurement_geometry, "load_declared_geometry", unreadable)
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    ready = ready_facts(plan)
    issue = preflight(plan, replace(ready, anchor=replace(ready.anchor, pose=read_pose()))).blocking_issue
    assert (issue.code, issue.evidence, issue.next_action["id"]) == (
        DECLARED_GEOMETRY_UNREADABLE, {"field": "front_wall_m"}, "declare_geometry")


def test_the_dry_run_publishes_each_drivers_cap_and_its_source(monkeypatch):
    _topology, safety, targets = _profile_and_targets(
        woofer_peak=None, tweeter_peak=None, sensitivities={"woofer": 84.0, "tweeter": 109.2})
    monkeypatch.setattr(control_client, "get", Mock(return_value=control_client.ControlResponse(
        200, b'{"muted": false, "percent": 35}')))
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
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
    monkeypatch.setattr(preflight_live, "program_charge_db", lambda _: 0.0)
    monkeypatch.setattr(preflight_live, "plays_rear", lambda _: False)
    monkeypatch.setattr(preflight_live, "load_tuning_declaration", lambda _: None)
    monkeypatch.setattr(preflight_live, "timing_floor_db", lambda *_: {})
    monkeypatch.setattr(preflight_live.candidate_bank, "find_banked_candidate", lambda _: SimpleNamespace(candidate=candidate))
    facts = preflight_live.read_preflight_facts(plan)
    assert facts.declared_target_ids == tuple(role_targets)
    missing = tuple(sorted({"woofer", "woofer:rear"} - role_targets.keys())) if name in {"rear", "front_rear"} else ()
    invalid_pairs = (tuple(role.role for role in roles),) if name == "branches" and len(roles) != 2 else ()
    blocked = bool(missing or invalid_pairs)
    report = preflight_levels(plan, facts)
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
        AngleStop(Pose(0, 0, kind="close", distance_m=0.015, driver=driver), REGIME_PER_DRIVER, purpose="reference", stimulus=NEAR_FIELD)
        for driver in ("woofer", "woofer:rear")))
    report = preflight(plan, ready_facts(plan, declared_target_ids=("tweeter", "woofer", "woofer:rear"),
                                         near_field_drivers=offered))
    assert report.blocking is bool(unoffered)
    assert [(issue.code, issue.evidence["unoffered_drivers"]) for issue in report.issues] == (
        [(REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, unoffered)] if unoffered else [])


def test_a_stop_naming_its_driver_is_no_branch_take_on_the_branches_regime():
    """A stop naming its driver plays that driver alone on the drivers graph
    whatever its regime, so preflight checks no branch pair for it and prices
    its one take (ADR-0366)."""
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0, driver="woofer"), "branches", purpose="reference", branch_pair="front_rear"),))
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


@pytest.mark.parametrize("stimulus,kind,distance_m,tweeter_floor_hz,offered", [
    (NEAR_FIELD, "close", 0.015, 800.0, True), (NEAR_FIELD, "close", 0.015, 1000.0, False),
    (NEAR_FIELD, "bearing", None, 1000.0, False), (None, "close", 0.015, 1000.0, True),
    (None, "bearing", None, 1000.0, True)])
def test_a_near_field_driver_the_view_cannot_read_is_not_offered(
        monkeypatch, stimulus, kind, distance_m, tweeter_floor_hz, offered):
    """The declared near-field sweep stops at 2 kHz and the view reads its top
    band, 800 Hz - 2 kHz, only whole, so a driver whose band starts above
    800 Hz is refused that sweep at any distance before a session plays takes
    no band can read; with no declared band it plays MEASURE's (#5696)."""
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0, kind=kind, distance_m=distance_m, driver="tweeter"), REGIME_PER_DRIVER, purpose="reference", stimulus=stimulus),))
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
    report = preflight(plan, ready_facts(
        plan, declared_target_ids=tuple(measurement_target_id(t["role"], t.get("output_variant", "primary")) for t in targets),
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
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0, kind="seat", seat_offset_m=(0, 0, 0)), REGIME_SUMMED, purpose="room"),))
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
        tuple(AngleStop(Pose(angle, 0), REGIME_SUMMED, candidate_id=cid, purpose="speaker") for angle in (0, 20, 0) for cid in ("", name)),
        candidates=("base", name), repeats=2, program="tournament/express",
    )
    report = preflight(plan, ready_facts(plan, candidates={name: candidate}))
    assert report.issues == ()
    assert [(row.pose, row.candidate_id, row.repeat) for row in report.schedule] == [
        (stop.pose.place, stop.candidate_id or "base", repeat)
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

    plan = request_for_preset(preset("branches/express"), candidates=("candidate",)) if branch else AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
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

    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
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
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, candidate_id=name, purpose="speaker"),), candidates=(name,))
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
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, candidate_id=name, purpose="speaker"),), candidates=(name,))
    report = preflight(plan, ready_facts(plan, candidates={name: candidate}))
    issue, = report.issues
    assert issue.code == "measurement_candidate_invalid"
    assert issue.blocking and issue.next_action


@pytest.mark.parametrize("banked,serial,sens_factor,delta", [
    ("1234", "other", -20, 0), ("1234", "1234", -10, -2), ("1234", "1234", -14, 2),
    ("1234", None, -10, 0), ("1234", None, -14, 2), (None, "1234", -10, 0), (None, "1234", -14, 2),
])
def test_calibrated_microphones_resolve_the_banked_anchor(banked, serial, sens_factor, delta):
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),), level=LevelPolicy(level_db=0))
    facts = ready_facts(plan)
    record = {**facts.anchor.record, "mic_sensitivity": {"sens_factor_db": -12.0, "serial": banked}}
    facts = replace(facts, anchor=AnchorFacts(record, MicSensitivity(sens_factor, 18, serial)))
    report = preflight(plan, facts)
    assert not report.blocking
    row = report.rung_admission
    assert row["anchor_mic_serial"] == banked and row["anchor_rebased_db"] == delta
    assert report.plan.level.resolved.mic_serial == serial
    assert report.plan.level.level_db == 0


@pytest.mark.parametrize("requested", [None, 0])
def test_a_carried_anchor_is_replaced_and_sets_no_level(requested):
    """A plan carrying another anchor runs with the banked one, at the level it
    states or at none: the saved level sets no played level (ADR-0403 §4)."""
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    facts = ready_facts(plan)
    banked, _ = resolve_anchor_level(facts=facts.anchor)
    carried = replace(banked, anchor_db_spl=60, reference_volume_db=-25)
    plan = replace(plan, level=LevelPolicy(level_db=requested, resolved=carried))
    report = preflight(AngleCaptureRequest.from_mapping(json.loads(json.dumps(plan.to_dict()))), facts)
    assert not report.blocking
    assert (report.plan.level.resolved, report.plan.level.level_db) == (banked, requested)
    assert report.rung_admission["carried_anchor_replaced"] is True


_CUT = PeqFilter(50.0, 8.0, -6.0)


@pytest.mark.parametrize("room,lin,rise_db", [
    ((), None, 0.0),
    (({"freq": 50.0, "q": 8.0, "gain": -6.0},), None, 6.0),
    (({"freq": 120.0, "q": 2.0, "gain": 3.0},), None, 3.88),  # a lone boost pays its peak and the margin
    (({"freq": 120.0, "q": 2.0, "gain": -3.0},), (120.0, 3.0), 0.0),  # the room cut nets a driver boost
    (({"freq": 120.0, "q": 2.0, "gain": 3.0},), (28.0, 6.0), 2.96),  # the declared high-pass nets a driver boost
])
def test_the_room_off_rise_is_the_rooms_charge_less_its_lowest_response_in_band(tuning_profile, room, lin, rise_db):
    """Clearing the applied room layer moves the program charge by what the layer adds to it and
    gives back the layer's response, so the room-off take plays at most that much louder across
    the band, read off the graphs the anchor and the take play (ADR-0385)."""
    profile = replace(tuning_profile, protection_sections_by_role={
        "woofer": (CrossoverSection(40, 4, True),), "tweeter": (CrossoverSection(1800, 4, True),)})
    band_hz, spend = (20.0, 1100.0), sum(entry["gain"] for entry in room if entry["gain"] > 0.0)
    candidate = replace(
        _room_candidate(profile), blend_correction=(), role_attenuations_db={"woofer": 0.0, "tweeter": -3.0},
        linearization={"woofer": {"filters": [{"biquad_type": "Peaking", "freq": lin[0], "q": 2.0, "gain": lin[1]}]}} if lin else {},
        room_correction=_room_correction(sides={"mono": list(room)}, boost_db_total=spend, level_cost_db=spend, basis={
            **_room_correction()["basis"], "admitted_boosts_hz": [e["freq"] for e in room if e["gain"] > 0.0]}) if room else {})
    rise = rise_without_room_db(candidate_room_peqs(candidate), band_hz, charge_db=room_layer_charge_db(profile, candidate))
    hz = np.geomspace(*band_hz, 4001)
    with_room, without = (np.abs(complex_channel_transfer(
        yaml.safe_load(compile_tuning_graph(profile, candidate, cleared_layers=cleared)),
        hz, input_weights={0: 1.0}, output_channels={"woofer": 0}, allow_limiter_passthrough=True,
    )["woofer"]) for cleared in ((), ("room_correction",)))
    assert rise == pytest.approx(rise_db, abs=0.02)
    assert rise == pytest.approx(max(0.0, float(np.max(20.0 * np.log10(without / with_room)))), abs=0.01)


def _rise_before_the_shared_cascade(room_peqs, band_hz, charge_db):
    """``rise_without_room_db`` as it read before it shared ``biquad.peaking_cascade_response_db``
    with the stereo room charge (#5909 H5): the oracle the shared form must match bit for bit."""
    low, high = band_hz
    steps = max(1, math.ceil(48 * math.log2(high / low)))
    grid = sorted({*(low * (high / low) ** (step / steps) for step in range(steps + 1)),
                   *(peq.freq for peq in room_peqs if low <= peq.freq <= high)})
    trig = freq_trig(grid)
    response = [sum(values) for values in zip(*(
        filter_response_db(FilterSpec("room", "Peaking", peq.freq, peq.gain, peq.q), grid, trig)
        for peq in room_peqs))]
    return max(0.0, charge_db - min(response))


@pytest.mark.parametrize("seed", range(3))
def test_the_room_off_rise_is_bit_identical_on_the_shared_cascade(seed):
    """The ADR-0370 rise bounds an SPL raise, so sharing its grid with the stereo room charge
    must not move one bit of it: random rooms and bands, compared with exact equality."""
    rng = random.Random(seed)
    for _ in range(20):
        room = [PeqFilter(rng.uniform(15.0, 600.0), rng.uniform(0.5, 12.0), rng.uniform(-12.0, 6.0))
                for _ in range(rng.randint(1, 10))]
        low = rng.uniform(15.0, 200.0)
        band_hz, charge_db = (low, low * rng.uniform(1.0, 30.0)), rng.uniform(0.0, 6.0)
        assert rise_without_room_db(room, band_hz, charge_db=charge_db) == _rise_before_the_shared_cascade(
            room, band_hz, charge_db)


@pytest.mark.parametrize("purposes,room,blocked", [
    (("bass",), None, False), (("speaker", "bass"), None, True), (("speaker", "bass"), (_CUT,), True),
    (("speaker", "room"), None, False)])
def test_an_unreadable_room_layer_refuses_only_a_rise_over_the_probe(purposes, room, blocked):
    """A take that clears the room layer the run's probe plays has an unknown rise
    when that layer or its charge cannot be read, so preflight refuses its plan.
    A run whose probe clears the layer too, or whose takes all play it, needs no
    rise (ADR-0370, ADR-0403 §4)."""
    plan = AngleCaptureRequest(tuple(AngleStop(Pose(azimuth, 0), REGIME_SUMMED, purpose=purpose)
                                     for azimuth, purpose in zip((0, 20), purposes)), level=LevelPolicy(level_db=0))
    report = preflight(plan, ready_facts(plan, applied_room_peqs=room))
    assert [issue.code for issue in report.issues if issue.blocking] == (["walk_level_policy_invalid"] if blocked else [])


@pytest.mark.parametrize("program,layout,applied_db,trial_db,lift", [
    ("speaker", "speaker_mark", 18, None, "applied"), ("room", "seat_express", 18, None, "none"),
    ("room", "seat_express", 6, 18, "trial over applied")])
def test_the_margins_are_measured_against_the_graph_the_probe_plays(tuning_profile, program, layout, applied_db,
                                                                    trial_db, lift):
    """A run's margin is how much more its takes' dynamic bass may lift than the
    graph its probe plays: all of the applied lift over a speaker run's timing
    take, which plays none; none over a take on the same graph; and a trial's
    lift over the applied one (ADR-0403 §4, ADR-0370)."""
    applied = _boost(applied_db)
    trial = replace(_room_candidate(tuning_profile), bass_extension=_boost(trial_db)) if trial_db else None
    plan = request_for_preset(run_preset(program, layout), candidates=("base", trial.fingerprint) if trial else ("base",))
    report = preflight(plan, ready_facts(plan, applied_bass_extension=applied,
                                         candidates={trial.fingerprint: trial} if trial else {}))
    assert not report.blocking
    expected = {"applied": bass_lift_db(applied, {}), "none": 0.0,
                "trial over applied": bass_lift_db(trial.bass_extension if trial else {}, applied)}[lift]
    assert report.rung_admission["run_margin_db"] == pytest.approx(expected) and expected >= 0.0
    assert (lift == "none") is (expected == 0.0)


def test_a_take_clearing_the_room_layer_the_probe_plays_adds_its_rise():
    """A take that clears the room layer the run's probe plays can play louder
    than the probe by that layer's rise across its band (ADR-0370, ADR-0385)."""
    plan = AngleCaptureRequest(tuple(AngleStop(Pose(azimuth, 0), REGIME_SUMMED, purpose=purpose)
                                     for azimuth, purpose in ((0, "speaker"), (20, "bass"))), level=LevelPolicy(level_db=0))
    report = preflight(plan, ready_facts(plan, applied_room_peqs=(_CUT,), applied_room_charge_db=0.0))
    assert not report.blocking
    assert report.rung_admission["room_off_rise_db"] == pytest.approx(6.0, abs=0.1)
    assert report.rung_admission["run_margin_db"] == pytest.approx(report.rung_admission["room_off_rise_db"])


@pytest.mark.parametrize("state,room", [({}, ()), ({"status": "applied"}, None)])
def test_live_facts_tell_no_applied_room_layer_from_an_unreadable_one(monkeypatch, state, room):
    """No applied profile plays no room layer, charge or rear woofer, and the run's
    base is the declared draft; an applied one whose candidate cannot be read has
    unknown ones (ADR-0370, ADR-0385)."""
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="bass"),))
    ready = ready_facts(plan)

    def unreadable(*_args):
        raise candidate_parts.CandidateBankRefusal("composition_saved_tune_unavailable", "gone")

    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: state)
    monkeypatch.setattr(preflight_live, "candidate_from_applied_profile", unreadable)
    monkeypatch.setattr(preflight_live, "_draft_floor_db", lambda _: {"woofer": -2.0})
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
                              preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert (facts.applied_room_peqs, facts.applied_room_charge_db, facts.applied_program_charge_db,
            facts.applied_timing_floor_db, facts.applied_rear_plays) == (
        (room, None, None, None, None) if room is None else (room, None, 0.0, {"woofer": -2.0}, False))


#: The test tune (``_room_candidate``) with a 6 dB room boost at 120 Hz: its trims, and that boost's charge.
_TUNE_FLOOR_DB, _TUNE_CHARGE_DB = {"woofer": -1.5, "tweeter": -4.25}, 2.821
_REAR_SUM_DB = 20 * math.log10(2)


def _boosted_tune(tuning_profile):
    return replace(_room_candidate(tuning_profile), room_correction=_room_correction(
        sides={"mono": [{"freq": 120.0, "q": 2.0, "gain": 6.0}]}, boost_db_total=6.0, level_cost_db=6.0,
        basis={**_room_correction()["basis"], "admitted_boosts_hz": [120.0]}))


def _cardioid_profile():
    topology, safety, targets = _profile_and_targets(rear=True, woofer_floor=30, woofer_upper=4000, tweeter_peak=0,
                                                     max_sweep_duration_s=4)
    return MeasurementGraphProfile(_rear_pair("mono")[0], topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM,
                                   protection_sections_by_role=confirmed_protection_sections(safety, targets))


def _cardioid_trial(profile=None):
    """A trial on a cardioid cabinet whose rear woofer plays (ADR-0318)."""
    return replace(_trial_candidate(profile or SimpleNamespace(preset=_rear_pair("mono")[0])),
                   rear_calibration=_rear_document())


@pytest.mark.parametrize(("program", "layout", "rear", "trial", "excess_db", "rear_sum_db"), [
    ("speaker", "speaker_mark", False, "plain", _TUNE_CHARGE_DB + 4.25, 0.0),
    ("speaker", "speaker_mark", True, "plain", _TUNE_CHARGE_DB + 1.5 + _REAR_SUM_DB, _REAR_SUM_DB),
    ("speaker", "speaker_mark", True, None, 0.0, 0.0),
    ("room", "seat_express", True, "plain", 0.0, 0.0),
    ("room", "seat_express", False, "cardioid", _REAR_SUM_DB, _REAR_SUM_DB),
    ("room", "seat_express", True, "cardioid", 0.0, 0.0),
    ("room", "seat_express", None, "plain first", _REAR_SUM_DB, _REAR_SUM_DB),
    ("room", "seat_express", None, "cardioid", _REAR_SUM_DB, _REAR_SUM_DB)],
    ids=["no rear woofer: the tweeter's trim sets it", "a rear woofer the timing take mutes", "only drivers after it",
         "probe on the candidate's own graph", "a seat probe that mutes the rear", "a seat probe that plays it too",
         "an unread base after a probe that mutes it", "an unread probe under a trial that plays it"])
def test_a_drivers_excess_is_its_gap_under_the_probe_and_a_rear_it_mutes(tuning_profile, program, layout, rear,
                                                                          trial, excess_db, rear_sum_db):
    """A later take's graph plays no driver over unity, and the timing take plays
    each front driver its trim under unity, less the applied charge it folds in:
    so over a timing take each driver counts that charge plus its own trim's gap,
    and a deep tweeter trim can set the margin. A rear woofer the probe's graph
    mutes and a later take plays adds the coherent sum of two woofers, 6.02 dB, to
    the woofer's band; a speaker with no rear woofer adds none (ADR-0403 §4, ADR-0385)."""
    candidates = ("trial", "base") if trial == "plain first" else ("base", "trial") if trial else ()
    plan = request_for_preset(run_preset(program, layout), candidates=candidates)
    report = preflight(plan, ready_facts(
        plan, applied_program_charge_db=_TUNE_CHARGE_DB, applied_timing_floor_db=_TUNE_FLOOR_DB, applied_rear_plays=rear,
        candidates={"trial": _cardioid_trial() if trial == "cardioid" else _room_candidate(tuning_profile)}))
    assert not report.blocking
    assert report.rung_admission["driver_excess_db"] == pytest.approx(excess_db)
    assert report.rung_admission["run_margin_db"] == pytest.approx(excess_db)
    assert report.rung_admission["rear_sum_db"] == pytest.approx(rear_sum_db)


@pytest.mark.parametrize("unread", [{"applied_program_charge_db": None}, {"applied_timing_floor_db": None},
                                    {"applied_timing_floor_db": {"woofer": -math.inf}}],
                         ids=["charge", "floor", "a muted front woofer"])
@pytest.mark.parametrize(("program", "layout", "blocked"), [("speaker", "speaker_mark", True),
                                                            ("room", "seat_express", False)])
def test_an_unread_applied_tune_refuses_only_a_take_over_the_timing_take(tuning_profile, unread, program, layout,
                                                                         blocked):
    """Over a timing take a later take's rise needs the applied charge and the
    timing floor, and a timing take that plays no front woofer has none; a run
    probed on a candidate's own graph needs neither (ADR-0403 §4)."""
    plan = request_for_preset(run_preset(program, layout), candidates=("base", "trial"))
    report = preflight(plan, ready_facts(plan, candidates={"trial": _room_candidate(tuning_profile)}, **unread))
    assert [issue.code for issue in report.issues if issue.blocking] == (["walk_level_policy_invalid"] if blocked else [])


def _over_timing_db(profile, candidate):
    """The most each driver's band of ``candidate``'s graph plays over its timing
    graph where the timing graph plays it, read from the two compiled graphs; a
    cardioid's front and rear woofers add in phase."""
    hz = np.geomspace(20.0, 20000.0, 4001)
    outputs = {(output.driver_role, output.output_variant): output.index for output in profile.preset.channel_map.outputs}

    def bands(scope):
        graph = yaml.safe_load(compile_tuning_graph(profile, candidate, scope=scope))
        played = {index: np.abs(response) for index, response in complex_channel_transfer(
            graph, hz, input_weights={0: 1.0}, output_channels={index: index for index in outputs.values()},
            allow_limiter_passthrough=True, dynamic_bass_at_rest=True).items()}
        return {role: sum(played[index] for (driver, _), index in outputs.items() if driver == role)
                for role in {driver for driver, _ in outputs}}

    candidate_bands, timing_bands = bands("candidate"), bands("timing")
    return {role: float(np.max(20 * np.log10(candidate_bands[role][live] / timing[live])))
            for role, timing in timing_bands.items() if np.any(live := timing > np.max(timing) * 1e-3)}


@pytest.mark.parametrize(("front", "woofer_db"), [({}, 0.0), ({"gain_db": -2.0}, -2.0), ({"filters": [
    {"type": "Biquad", "parameters": {"type": "Lowshelf", "freq": 80.0, "q": 0.7, "gain": -3.0}},
    {"type": "Biquad", "parameters": {"type": "Peaking", "freq": 300.0, "q": 1.0, "gain": 2.0}},
    {"type": "Biquad", "parameters": {"type": "Highpass", "freq": 30.0, "q": 0.7}}]}, -3.0), ({"muted": True}, -math.inf)],
    ids=["flat", "a gain", "a cut, a boost and a high-pass", "muted"])
def test_the_timing_floor_counts_the_front_chains_lowest_static_response(front, woofer_db):
    """On a cardioid cabinet the timing take keeps the front chain the rear muted
    leaves, so the front woofer's floor under unity falls by that chain's gain and
    each of its cuts; a muted chain plays no front woofer (ADR-0385)."""
    profile = _cardioid_profile()
    candidate = replace(_cardioid_trial(profile), rear_calibration=_rear_document(
        front={**_rear_document()["front"], **front}))
    floors = timing_floor_db(profile, candidate)
    trims = candidate.role_attenuations_db
    assert floors["woofer"] - floors["tweeter"] == pytest.approx(trims["woofer"] - trims["tweeter"] + woofer_db)


def _netted_front_boost(profile):
    """A cardioid tune whose woofer cut nets its front chain's 3 dB boost, which the
    timing graph, playing no linearization, charges itself (ADR-0385)."""
    front = {**_rear_document()["front"], "filters": [
        {"type": "Biquad", "parameters": {"type": "Peaking", "freq": 60.0, "q": 1.0, "gain": 3.0}}]}
    return replace(_cardioid_trial(profile), blend_correction=(), rear_calibration=_rear_document(rear_muted=True, front=front),
                   linearization={"woofer": {"filters": [{"biquad_type": "Peaking", "freq": 60.0, "q": 1.0, "gain": -3.0}]}})


@pytest.mark.parametrize("cabinet", ["two-way", "cardioid", "a front boost its tune nets"])
def test_each_drivers_gap_counts_what_its_band_plays_over_the_timing_take(tuning_profile, cabinet):
    """Read from the compiled graphs, the test tune's woofer plays 3.3 dB over its
    timing take where a 6 dB room boost meets its trim, and its tweeter 1.6 dB at a
    linearization boost its −4.25 dB trim keeps out of the charge. Each driver's
    gap under the timing take counts that, on a cardioid cabinet the woofers'
    coherent sum counts the rear woofer the timing take mutes, and the gap counts
    what the timing graph still charges itself (ADR-0385)."""
    profile = tuning_profile if cabinet == "two-way" else _cardioid_profile()
    applied = {"two-way": lambda: _boosted_tune(tuning_profile), "cardioid": lambda: _cardioid_trial(profile),
               "a front boost its tune nets": lambda: _netted_front_boost(profile)}[cabinet]()
    over = _over_timing_db(profile, applied)
    if cabinet == "two-way":
        assert over == {"woofer": pytest.approx(3.32, abs=0.02), "tweeter": pytest.approx(1.61, abs=0.02)}
    charge, floors = candidate_parts.program_charge_db(applied), timing_floor_db(profile, applied)
    rear_sum = _REAR_SUM_DB if plays_rear(applied) else 0.0
    assert plays_rear(applied) is (cabinet == "cardioid") and over
    for role, read in over.items():
        assert read <= charge - floors[role] + (rear_sum if role == "woofer" else 0.0) + 0.01, role


def _unprobed_plans():
    """The review's two plans whose take at the run's fader would play before any
    probe: a driver run's spot at the mark with no summed take, and a rear run
    whose CHECK at the mark comes before its first summed take, at 20°."""
    return {"no summed take": AngleCaptureRequest(
                (AngleStop(Pose(0, 0), REGIME_PER_DRIVER, purpose="speaker"),), program="drivers/each"),
            "check before the probe": AngleCaptureRequest(
                (AngleStop(Pose(20, 0), REGIME_SUMMED, purpose="rear"),
                 AngleStop(Pose(0, 0), REGIME_PER_DRIVER, purpose="speaker")), program="rear/express")}


@pytest.mark.parametrize("shape", ["no summed take", "check before the probe"])
@pytest.mark.parametrize("finds_fader", [True, False], ids=["finds its fader", "plays a stated level"])
def test_a_plan_whose_take_at_the_run_fader_plays_before_its_probe_is_refused(shape, finds_fader):
    """A run that finds its fader with a probe plays a take that does not level
    itself only after that probe, so preflight refuses a plan where such a take
    would come first, or that has no probe. A ladder's later rung plays at the
    level stated to it and probes nothing (ADR-0403 §4)."""
    plan = _unprobed_plans()[shape]
    report = preflight(plan, ready_facts(plan), finds_fader=finds_fader)
    assert [issue.code for issue in report.issues if issue.blocking] == (
        ["walk_level_policy_invalid"] if finds_fader else [])


def test_every_shipped_preset_plans_its_probe_before_the_takes_at_its_fader(tuning_profile):
    """Every shipped preset at every layout it offers, with the applied tune and
    with an A/B trial, plays its run's probe before any take at the run's fader
    (ADR-0403 §4)."""
    roles = (RoleBand("woofer", 0, FrequencyBand(20, 4000)), RoleBand("tweeter", 1, FrequencyBand(1500, 20000)))
    trial = _room_candidate(tuning_profile)
    for name in available_presets():
        for layout in preset(name).layouts:
            selected = run_preset(name, layout)
            if selected.regime == REGIME_BRANCHES:
                trials = ((trial.fingerprint,),)
            else:
                trials = ((),) if any(pose.driver for pose in selected.poses) else ((), ("base", trial.fingerprint))
            for candidates in trials:
                plan = request_for_preset(selected, mover=selected.mover or "human", targets=("woofer", "tweeter"),
                                          candidates=candidates)
                report = preflight(plan, ready_facts(plan, roles_bands=roles, candidates={trial.fingerprint: trial}))
                assert not report.blocking, (name, layout, candidates, report.issues)


def test_live_facts_read_a_cardioid_base_and_its_rear(monkeypatch):
    """An applied cardioid tune's rear woofer plays, and its timing floor is read
    off its own timing graph (ADR-0318, ADR-0385)."""
    profile = _cardioid_profile()
    applied = _cardioid_trial(profile)
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    ready = ready_facts(plan)
    state = {"status": "applied", "source": {"measured_candidate_fingerprint": applied.fingerprint}}
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: state)
    monkeypatch.setattr(candidate_parts, "find_banked_candidate", lambda _: SimpleNamespace(candidate=applied))
    monkeypatch.setattr(preflight_live, "load_tuning_declaration", lambda _: profile)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
                              preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert (facts.applied_rear_plays, facts.applied_timing_floor_db) == (True, timing_floor_db(profile, applied))


def test_unreadable_bass_descriptor_blocks_the_margin():
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    report = preflight(plan, ready_facts(plan, applied_bass_extension={"low_boost_db": 6}))
    assert report.blocking_issue.code == "walk_level_policy_invalid"
    assert report.rung_admission["status"] == "blocked"


@pytest.mark.parametrize("descriptor", [None, {}, BASS_EXTENSION])
def test_live_facts_resolve_applied_bass_from_the_candidate_bank(monkeypatch, tuning_profile, descriptor):
    candidate = replace(_room_candidate(tuning_profile), bass_extension=_boost(18))
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="bass", candidate_id=candidate.fingerprint),),
                               candidates=(candidate.fingerprint,), level=LevelPolicy(level_db=0))
    anchor = ready_facts(plan).anchor
    # A lone room boost gives the applied tune a program charge.
    applied = replace(_room_candidate(tuning_profile), bass_extension=descriptor or {}, room_correction=_room_correction(
        sides={"mono": [{"freq": 120.0, "q": 2.0, "gain": 6.0}]}, boost_db_total=6.0, level_cost_db=6.0,
        basis={**_room_correction()["basis"], "admitted_boosts_hz": [120.0]}))
    state = {"status": "applied", "source": {"measured_candidate_fingerprint": applied.fingerprint}}
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: state if descriptor is not None else {})
    monkeypatch.setattr(candidate_parts, "find_banked_candidate", lambda name: {applied.fingerprint: SimpleNamespace(candidate=applied)}[name])
    monkeypatch.setattr(preflight_live.candidate_bank, "find_banked_candidate", lambda _: SimpleNamespace(candidate=candidate))
    monkeypatch.setattr(preflight_live, "load_tuning_declaration", lambda topology: tuning_profile)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda device: anchor.sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
        driver_caps_dbfs={}, fc_hz=None, driver_sweep_duration_limits_s={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert facts.applied_bass_extension == applied.bass_extension
    assert facts.applied_room_peqs == (candidate_room_peqs(applied) if descriptor is not None else ())
    assert candidate_parts.program_charge_db(applied) > 0.0
    assert facts.applied_program_charge_db == (candidate_parts.program_charge_db(applied) if descriptor is not None else 0.0)
    if descriptor is not None:
        assert (facts.applied_timing_floor_db, facts.applied_rear_plays) == (timing_floor_db(tuning_profile, applied), False)
    report = preflight(plan, facts)
    assert not report.blocking
    assert report.rung_admission["run_margin_db"] == 0.0


@pytest.mark.parametrize("mover,attested,blocking", [
    ("arm", None, False), ("arm", False, True), ("arm", True, False), ("human", False, False),
])
def test_rig_clear_attestation_is_only_required_when_asked(mover, attested, blocking):
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),), mover=mover)
    report = preflight(plan, ready_facts(plan, rig_clear_attested=attested))
    assert report.blocking is blocking
    assert [issue.code for issue in report.issues] == (["walk_rig_clear_not_attested"] if blocking else [])


@pytest.mark.parametrize("attested,available", [(None, True), (False, False), (True, True)])
def test_live_preflight_accepts_facts_without_discovery(monkeypatch, attested, available):
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),), mover="arm")
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
