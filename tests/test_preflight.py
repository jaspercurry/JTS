# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import logging
import math
from itertools import groupby
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jasper.active_speaker.angle_capture import (
    AngleCaptureRequest, AngleStop, REGIME_PER_DRIVER, REGIME_SUMMED, level_sets, request_for_preset,
)
from jasper.active_speaker.capture_schedule import prepare_plan_captures, run_probe_index
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_MEASUREMENT_PROGRAM_NOT_OFFERED, REASON_REGISTRY, REASON_WALK_BRANCH_PAIR_UNDECLARED,
    REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS, TEMPLATE_HARD_STOP,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.measurement_programs import REGIME_BRANCHES, Pose, available_presets, preset, run_preset
from jasper.active_speaker.preflight import PreflightFacts, PreflightIssue, preflight
from jasper.active_speaker.profile import DRIVER_ROLES_BY_WAY
from jasper.active_speaker import arm_walk, preflight_live
from jasper.audio_measurement import measurement_geometry
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.measurement_geometry import DECLARED_GEOMETRY_UNREADABLE
from jasper.audio_measurement.program import FrequencyBand, RoleBand
from jasper.platform.speaker_layout import measurement_target_id
from jasper.platform import control_client
from tests.active_speaker_fixtures import mono_output_topology
from tests._log_events import event_field_maps
from tests.test_rear_output_foundation import _rear_document, _rear_pair
from tests.test_active_speaker_program_admission import _profile_and_targets
from tests.test_crossover_v2_tuning_scope import (
    BASS_EXTENSION, _room_candidate, _trial_candidate, tuning_profile as tuning_profile,
)

NEAR_FIELD = preset("nearfield/each").stimulus


def _boost(boost_db, **changes):
    """BASS_EXTENSION with its transform's DC lift set to ``boost_db``."""
    shape = BASS_EXTENSION["linkwitz_transform"]
    return {**BASS_EXTENSION, "linkwitz_transform": {**shape, "target_hz": shape["source_hz"] / 10 ** (boost_db / 40)},
            **changes}


def ready_facts(plan, **changes):
    return replace(PreflightFacts(
        candidates={}, mic_present=True, mic_identified=True, mic_sensitivity=MicSensitivity(-12.0, 18.0, "1234"),
        commissioning_stop_db_spl=85.0, mover=plan.mover,
    ), **changes)


@pytest.mark.parametrize("muted", [True, False, None])
def test_preflight_output_mute(monkeypatch, caplog, muted):
    response = control_client.ControlResponse(200, b'{"muted": true, "percent": 0}' if muted else b'{"muted": false, "percent": 35}')
    read = Mock(return_value=response, side_effect=control_client.ControlError() if muted is None else None)
    monkeypatch.setattr(control_client, "get", read)
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    ready = ready_facts(plan)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.mic_sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(preflight_live, "require_wired_mic", lambda: SimpleNamespace(model_key="minidsp_umik2"))
    facts = preflight_live.read_preflight_facts(plan, context=context)
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
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: None)
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    monkeypatch.setattr(preflight_live, "require_wired_mic", lambda: SimpleNamespace(model_key="minidsp_umik2"))
    facts = preflight_live.read_preflight_facts(plan, context=context)
    issue = preflight(plan, replace(ready_facts(plan), geometry_unreadable=facts.geometry_unreadable)).blocking_issue
    assert (issue.code, issue.evidence, issue.next_action["id"]) == (
        DECLARED_GEOMETRY_UNREADABLE, {"field": "front_wall_m"}, "declare_geometry")


def test_the_dry_run_publishes_each_drivers_cap_and_its_source(monkeypatch):
    _topology, safety, targets = _profile_and_targets(
        woofer_peak=None, tweeter_peak=None, sensitivities={"woofer": 84.0, "tweeter": 109.2})
    monkeypatch.setattr(control_client, "get", Mock(return_value=control_client.ControlResponse(
        200, b'{"muted": false, "percent": 35}')))
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    ready = ready_facts(plan)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.mic_sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), role_targets=targets, safety_profile=safety,
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    monkeypatch.setattr(preflight_live, "require_wired_mic", lambda: SimpleNamespace(model_key="minidsp_umik2"))
    facts = preflight_live.read_preflight_facts(plan, context=context)
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
    ("speaker", "speaker/mark", "speaker_mark"), ("room", "room/seat", "room_quick"))])
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
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.mic_sensitivity)
    monkeypatch.setattr(preflight_live.candidate_bank, "find_banked_candidate", lambda _: SimpleNamespace(candidate=candidate))
    facts = preflight_live.read_preflight_facts(plan, mover_available=True)
    assert facts.declared_target_ids == tuple(role_targets)
    missing = tuple(sorted({"woofer", "woofer:rear"} - role_targets.keys())) if name in {"rear", "front_rear"} else ()
    invalid_pairs = (tuple(role.role for role in roles),) if name == "branches" and len(roles) != 2 else ()
    blocked = bool(missing or invalid_pairs)
    report = preflight(plan, replace(facts, context=None))
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
    whatever its regime, so preflight checks no branch pair for it (ADR-0366)."""
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0, driver="woofer"), "branches", purpose="reference", branch_pair="front_rear"),))
    report = preflight(plan, ready_facts(plan, declared_target_ids=("tweeter", "woofer"),
                                         near_field_drivers=("tweeter", "woofer")))
    assert [issue.code for issue in report.issues] == []
    assert [row.graph_scope for row in report.schedule] == ["drivers"]


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
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.mic_sensitivity)
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    monkeypatch.setattr(preflight_live, "require_wired_mic", lambda: SimpleNamespace(model_key="minidsp_umik2"))
    facts = preflight_live.read_preflight_facts(plan, context=context)
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
        assert report.schedule == ()
        assert REASON_REGISTRY[issue.code].template == TEMPLATE_HARD_STOP
        assert REASON_REGISTRY[issue.code].retry_budget == 0
    else:
        assert report.issues == ()


@pytest.mark.parametrize("change,code", [
    ("calibration", "measure_spl_calibration_required"),
    ("mover", "walk_over_mover_envelope"),
    ("mic", "wired_mic_missing"),
    ("identity", "measurement_mic_unidentified"),
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
        facts = replace(facts, mic_sensitivity=None)
    elif change == "mover":
        facts = replace(facts, mover="arm")
    elif change == "mic":
        facts = replace(facts, mic_present=False)
    elif change == "identity":
        facts = replace(facts, mic_identified=False)
    elif change in {"stop", "context"}:
        facts = replace(facts, commissioning_stop_db_spl=None,
                        issues=(PreflightIssue.from_code(code, ""),) if change == "context" else ())
    elif change == "capacity":
        plan = replace(plan, stops=plan.stops * 129)
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
        tuple(AngleStop(Pose(angle, 0), REGIME_SUMMED, candidate_id=cid, purpose="speaker")
              for angle in (0, 20, 0) for _ in range(2) for cid in ("", name)),
        candidates=("base", name), program="tournament/express",
    )
    report = preflight(plan, ready_facts(plan, candidates={name: candidate}))
    assert report.issues == ()
    assert [(row.pose, row.candidate_id) for row in report.schedule] == [
        (stop.pose.place, stop.candidate_id or "base") for stop in plan.stops]
    assert report.spl_ceiling_db_spl == 85
    assert {row.graph_scope for row in report.schedule} == {"candidate"}


@pytest.mark.parametrize("fault,branch", [("box", False), ("box", True), ("wrong_mic", False), ("no_calibration", False)])
def test_live_facts_surface_owner_refusals(monkeypatch, fault, branch):
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
    from jasper.audio_measurement import calibration, household_mic

    plan = request_for_preset(preset("branches/express"), candidates=("candidate",)) if branch else AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    facts = ready_facts(plan)
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
    monkeypatch.setattr(calibration, "resolve_mic_sensitivity", lambda **kwargs: facts.mic_sensitivity)
    report = preflight(plan, replace(preflight_live.read_preflight_facts(plan), context=None))
    code = "measure_box_not_ready" if fault == "box" else "measure_spl_calibration_required"
    assert any(issue.code == code and issue.blocking and issue.next_action for issue in report.issues)


def test_supplied_facts_do_not_read_files(monkeypatch):
    from jasper.audio_measurement import calibration

    def unexpected_read(*args, **kwargs):
        pytest.fail("preflight attempted an external read")

    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),))
    facts = ready_facts(plan)
    monkeypatch.setattr(measurement_geometry, "load_declared_geometry", unexpected_read)
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


_REAR_SUM_DB = 20 * math.log10(2)


def _cardioid_trial():
    """A trial on a cardioid cabinet whose rear woofer plays (ADR-0318)."""
    return replace(_trial_candidate(SimpleNamespace(preset=_rear_pair("mono")[0])), rear_calibration=_rear_document())


def test_every_shipped_preset_plans_its_probe_before_the_takes_at_its_fader(tuning_profile):
    """Every shipped preset at every layout it offers, with the applied tune and with an
    A/B trial, places its run's probe no later than any take that plays at the run's
    fader, so no such take opens before the probe has found that fader (ADR-0403 §4,
    ADR-0405)."""
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
                captures = prepare_plan_captures(plan, roles_bands=roles)
                levelled = [start is not None for start in level_sets(
                    [capture.stop for capture in captures], [capture.spec.graph_scope for capture in captures])]
                places = [index for index, (_, group) in enumerate(
                    groupby(captures, key=lambda capture: capture.stop.pose.place)) for _ in group]
                probe = run_probe_index([(capture.spec.graph_scope, own) for capture, own in zip(captures, levelled)])
                at_fader = [place for place, own in zip(places, levelled) if not own]
                assert not at_fader or (probe is not None and min(at_fader) >= places[probe]), (name, layout, candidates)


@pytest.mark.parametrize("mover,attested,blocking", [
    ("arm", None, False), ("arm", False, True), ("arm", True, False), ("human", False, False),
])
def test_rig_clear_attestation_is_only_required_when_asked(mover, attested, blocking):
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),), mover=mover)
    report = preflight(plan, ready_facts(plan, rig_clear_attested=attested))
    assert report.blocking is blocking
    assert [issue.code for issue in report.issues] == (["walk_rig_clear_not_attested"] if blocking else [])


@pytest.mark.parametrize(("program", "layouts"), [("room/seat", ["seat_express", "seat_cloud", "seat_cube"]), ("", [])])
def test_a_missing_arm_names_the_layouts_that_need_none(program, layouts):
    """The refusal names the way on: the program's layouts that need no arm, its
    trial layout first, where it has any."""
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),), mover="arm", program=program)
    issue = preflight(plan, ready_facts(plan, mover_available=False)).blocking_issue
    assert (issue.code, issue.evidence, issue.next_action) == (
        "walk_mover_unavailable", {"layouts_without_arm": layouts}, REASON_REGISTRY["walk_mover_unavailable"].next_action)


@pytest.mark.parametrize("attested,available", [(None, True), (False, False), (True, True)])
def test_live_preflight_accepts_facts_without_discovery(monkeypatch, attested, available):
    plan = AngleCaptureRequest((AngleStop(Pose(0, 0), REGIME_SUMMED, purpose="speaker"),), mover="arm")
    ready = ready_facts(plan)
    detect = Mock(side_effect=AssertionError)
    monkeypatch.setattr(arm_walk.TurntableMover, "available", detect)
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.mic_sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), safety_profile={}, role_targets={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    monkeypatch.setattr(preflight_live, "require_wired_mic", lambda: SimpleNamespace(model_key="minidsp_umik2"))
    facts = preflight_live.read_preflight_facts(
        plan, context=context,
        rig_clear_attested=attested, mover_available=available,
    )
    assert facts.rig_clear_attested is attested and facts.mover_available is available
    detect.assert_not_called()
