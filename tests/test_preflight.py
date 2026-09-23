# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop, LevelPolicy, REGIME_SUMMED, request_for_program
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY, REASON_WALK_BRANCH_PAIR_UNDECLARED,
    REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS, TEMPLATE_HARD_STOP,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.measurement_programs import program
from jasper.active_speaker.preflight import PreflightFacts, PreflightIssue, preflight
from jasper.active_speaker.profile import DRIVER_ROLES_BY_WAY, SPL_RAISE_MARGIN_DB
from jasper.active_speaker.run_levels import preflight_levels
from jasper.active_speaker import arm_walk, candidate_parts, preflight_live
from jasper.active_speaker.seat_level_reference import (
    AnchorFacts, ResolvedLevel, predicted_rung_admission, resolve_anchor_level, rung_lift_bound_db,
)
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.program import FrequencyBand, RoleBand
from jasper.bass_extension.dynamic import DynamicBassDescriptor, dynamic_bass_gain_reserve_db, loudness_boost_db
from jasper.output_topology import measurement_target_id
from jasper.platform import control_client
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_rear_output_foundation import _rear_pair
from tests.test_crossover_v2_tuning_scope import (
    BASS_EXTENSION, _room_candidate, tuning_profile as tuning_profile,
)


def ready_facts(plan, **changes):
    return replace(PreflightFacts(
        candidates={}, mic_present=True, mic_identified=True,
        anchor=AnchorFacts({"artifact_schema_version": 2, "session_id": "session", "leveled_at": "2026-09-12T00:00:00Z",
                            "target": {"target_db_spl": 75.0, "tolerance_db": 1.0}, "measured_db_spl": 75.0, "reference_volume_db": -18.0,
                            "stimulus": {"program_id": "fixture-sweep"},
                            "mic_sensitivity": {"sens_factor_db": -12.0, "serial": "1234"}},
                           MicSensitivity(-12.0, 18.0, "1234")),
        commissioning_stop_db_spl=85.0, mover=plan.mover, applied_bass_extension={},
        program_ids_for=lambda _plan: ("fixture-sweep",),
    ), **changes)


@pytest.mark.parametrize("muted", [True, False, None])
def test_preflight_output_mute(monkeypatch, muted):
    response = control_client.ControlResponse(200, b'{"muted": true, "percent": 0}' if muted else b'{"muted": false, "percent": 35}')
    read = Mock(return_value=response, side_effect=control_client.ControlError() if muted is None else None)
    monkeypatch.setattr(control_client, "get", read)
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    ready = ready_facts(plan)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), role_targets={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    report = preflight(plan, replace(ready, output_volume=facts.output_volume))
    read.assert_called_once_with("/volume", base_url=control_client.DEFAULT_BASE_URL, timeout=control_client.DEFAULT_TIMEOUT)
    assert report.blocking is (muted is True)
    if muted:
        issue, = report.issues
        assert issue.code == "measurement_output_muted" and issue.blocking
        assert issue.evidence == {"muted": True, "household_percent": 0}
        assert issue.next_action["id"] == "raise_volume"
        assert REASON_REGISTRY[issue.code].retry_budget == 0
    else:
        assert report.issues == ()


@pytest.mark.parametrize("layout,name,size", [
    (layout, name, size)
    for layout in ("full_range_passive", "active_2_way", "active_3_way", "cardioid")
    for name, size in (("rear", "express"), ("rear", "wide"), ("rear", "behind"), ("rear", "pair"), ("rear", "pair_behind"), ("front_rear", "express"), ("branches", "express"))
] + [("active_2_way", name, size) for name, size in (("speaker", "mark"), ("room", "arm"), ("bass", "axis"))])
def test_preflight_requires_declared_capture_targets(monkeypatch, tuning_profile, layout, name, size):
    topology = _rear_pair("mono")[1] if layout == "cardioid" else mono_output_topology(mode=layout)
    targets = active_driver_targets(topology)
    role_targets = {measurement_target_id(t["role"], t.get("output_variant", "primary")): t["target_fingerprint"]
                    for t in targets}
    roles = tuple(RoleBand(t["role"], index, FrequencyBand(20, 20000)) for index, t in enumerate(targets)
                  if t.get("output_variant", "primary") == "primary")
    candidate = _room_candidate(tuning_profile)
    selected = program(name, size)
    plan = request_for_program(selected, mover=selected.mover or "human",
                               candidates=(candidate.fingerprint,) if name in {"rear", "front_rear", "branches"} else ())
    ready = ready_facts(plan)
    context = SimpleNamespace(topology=topology, roles_bands=roles, role_targets=role_targets,
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    monkeypatch.setattr(preflight_live, "conductor_status", lambda: {})
    monkeypatch.setattr(preflight_live, "resolve_conductor_context", lambda _: context)
    monkeypatch.setattr(preflight_live, "require_wired_mic", lambda: SimpleNamespace(model_key="minidsp_umik2"))
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: {})
    monkeypatch.setattr(preflight_live, "candidate_from_applied_profile", lambda *a: SimpleNamespace(bass_extension={}))
    monkeypatch.setattr(preflight_live.candidate_bank, "find_banked_candidate", lambda _: SimpleNamespace(candidate=candidate))
    facts = preflight_live.read_preflight_facts(plan)
    assert facts.declared_target_ids == tuple(role_targets)
    missing = tuple(sorted({"woofer", "woofer:rear"} - role_targets.keys())) if name in {"rear", "front_rear"} else ()
    invalid_pairs = (tuple(role.role for role in roles),) if name == "branches" and len(roles) != 2 else ()
    blocked = bool(missing or invalid_pairs)

    def program_ids(_plan):
        assert not blocked
        return ("fixture-sweep",)

    report = preflight_levels(plan, replace(facts, program_ids_for=program_ids))
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


@pytest.mark.parametrize("layout", ["active_3_way", "cardioid", "active_2_way"],
                         ids=["three_way_active", "cardioid", "two_way_active"])
def test_preflight_per_driver_layout(layout):
    topology = _rear_pair("mono")[1] if layout == "cardioid" else mono_output_topology(mode=layout)
    targets = active_driver_targets(topology)
    plan = request_for_program(program("speaker", "mark"), mover="human")
    program_ids = Mock(return_value=("fixture-sweep",))
    report = preflight(plan, ready_facts(
        plan, program_ids_for=program_ids,
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
        program_ids.assert_not_called()
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
    assert report.price["captures"] == 12
    assert report.price["ceiling_min"] > 0
    assert report.spl_ceiling_db_spl == 85
    assert report.plan.level.resolved.anchor_db_spl == 75
    assert {row.graph_scope for row in report.schedule} == {"candidate"}


@pytest.mark.parametrize("fault", ["box", "wrong_mic", "no_calibration"])
def test_live_facts_surface_owner_refusals(monkeypatch, fault):
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
    from jasper.audio_measurement import calibration, household_mic

    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    facts = ready_facts(plan)
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: facts.anchor.record)
    monkeypatch.setattr(preflight_live, "conductor_status", lambda: {})

    def context(_status):
        if fault == "box":
            raise CrossoverV2Refused("setup incomplete")
        return SimpleNamespace(topology=None, roles_bands=(), role_targets={},
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
    (18, 1, 84.0, 84.1), (0, 1, 84.0, 84.1), (20, 1, 82.65, 82.66), (18, 0.5, 84.5, 84.6),
])
def test_jts3_rung_margin_uses_the_applied_stack(tuning_profile, boost, tolerance, admitted, clamped):
    applied = {**BASS_EXTENSION, "low_boost_db": 18, "reference_level_db": 0,
               "delta_highpass_hz": 63, "detector_lowpass_hz": 100}
    candidate = replace(_room_candidate(tuning_profile), bass_extension={**applied, "low_boost_db": boost} if boost else {})
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
        assert row["lift_bound_db"] == rung_lift_bound_db(candidate.bass_extension, applied, fader)
        assert row["admitted_db_spl"] == report.plan.level.predicted_db_spl
        assert row["admitted_db_spl"] <= row["margin_bound_db_spl"] == 85 - (tolerance + row["lift_bound_db"])
        if spl == admitted:
            assert fader == requested
            assert row["lift_bound_db"] == pytest.approx(1.34283 if boost == 20 else 0, abs=0.001)
            assert row["margin_bound_db_spl"] == pytest.approx(82.65717 if boost == 20 else 85 - tolerance, abs=0.001)


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
@pytest.mark.parametrize("reference", [-25, -10, 0])
def test_admitted_fader_and_spl_stay_bounded_over_candidate_grid(tuning_profile, applied_boost, reference):
    base = _room_candidate(tuning_profile)
    descriptors = [{}, *({**BASS_EXTENSION, "low_boost_db": boost, "reference_level_db": reference} for boost in (6, 18, 20))]
    candidates = [replace(base, bass_extension=descriptor) for descriptor in descriptors]
    plan = AngleCaptureRequest(tuple(AngleStop(0, REGIME_SUMMED, candidate_id=c.fingerprint) for c in candidates),
                               candidates=tuple(c.fingerprint for c in candidates))
    applied = {**BASS_EXTENSION, "low_boost_db": applied_boost, "reference_level_db": -10} if applied_boost else {}
    facts = ready_facts(plan, candidates={c.fingerprint: c for c in candidates}, applied_bass_extension=applied)
    for requested in (-59, -35, -25, -18, -10, -1, 0):
        report = preflight(replace(plan, level=LevelPolicy(level_db=requested)), facts)
        assert not report.blocking
        fader = report.plan.level.volume_db
        assert fader <= requested
        row = report.rung_admission
        assert report.plan.level.predicted_db_spl == row["admitted_db_spl"] <= row["margin_bound_db_spl"]
        for descriptor in descriptors:
            margin = 1 + rung_lift_bound_db(descriptor, applied, fader)
            assert report.plan.level.predicted_db_spl <= 85 - margin


@pytest.mark.parametrize("requested", [-17.621, -17.627])
def test_margin_clamp_converges_where_the_bound_stops_moving(requested):
    candidate = {**BASS_EXTENSION, "low_boost_db": 20, "reference_level_db": -10}
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
    monkeypatch.setattr(preflight_live, "candidate_from_applied_profile", lambda *a, **kw: SimpleNamespace(bass_extension={}))
    context = SimpleNamespace(topology=None, preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)),
        roles_bands=(RoleBand("woofer", 0, FrequencyBand(20, 20000)),), role_targets={"woofer": "fp-woofer"}, fc_hz=None,
        driver_caps_dbfs={"woofer": -8}, session_volume_db=-22.23, driver_sweep_duration_limits_s={})
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    ids = facts.program_ids_for(plan)
    assert ids and len(set(ids)) == 1
    record["stimulus"] = {"program_id": ids[0] if same else "different"}
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
    candidate = replace(_room_candidate(tuning_profile), bass_extension={**BASS_EXTENSION, "reference_level_db": 0, "low_boost_db": 18})
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
    context = SimpleNamespace(topology=None, roles_bands=(), role_targets={},
        driver_caps_dbfs={}, fc_hz=None, driver_sweep_duration_limits_s={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert facts.applied_bass_extension == applied.bass_extension
    report = preflight(plan, replace(facts, program_ids_for=lambda _: ("fixture-sweep",)))
    assert not report.blocking
    row = report.rung_admission
    assert report.plan.level.volume_db < 0
    assert row["bound_by"] == "commissioning_margin"
    assert report.plan.level.predicted_db_spl == row["admitted_db_spl"] <= row["margin_bound_db_spl"]
    if not descriptor:
        bass = DynamicBassDescriptor(**candidate.bass_extension)
        boost = loudness_boost_db(report.plan.level.volume_db, bass)
        assert row["lift_bound_db"] == (dynamic_bass_gain_reserve_db(replace(bass, low_boost_db=boost)) if boost else 0)


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
    monkeypatch.setattr(preflight_live, "candidate_from_applied_profile", lambda *args, **kwargs: SimpleNamespace(bass_extension={}))
    context = SimpleNamespace(
        topology=None, preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)),
        roles_bands=(RoleBand("woofer", 0, FrequencyBand(550 if fc_hz is None else 20, 20000)),), role_targets={"woofer": "fp-woofer"},
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
    context = SimpleNamespace(topology=None, roles_bands=(), role_targets={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(
        plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"),
        rig_clear_attested=attested, mover_available=available,
    )
    assert facts.rig_clear_attested is attested and facts.mover_available is available
    detect.assert_not_called()
