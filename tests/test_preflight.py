# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop, LevelPolicy, REGIME_SUMMED, request_for_program
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY, REASON_WALK_BRANCH_PAIR_UNDECLARED, TEMPLATE_HARD_STOP
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.measurement_programs import program
from jasper.active_speaker.preflight import PreflightFacts, preflight
from jasper.active_speaker.run_levels import preflight_levels
from jasper.active_speaker import arm_walk, candidate_parts, preflight_live
from jasper.active_speaker.seat_level_reference import AnchorFacts
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.program import FrequencyBand, RoleBand
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
        commissioning_stop_db_spl=85.0, mover=plan.mover, rig_clear_attested=True, applied_bass_extension={},
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
    monkeypatch.setattr(arm_walk.TurntableMover, "available", lambda self: True)
    facts = preflight_live.read_preflight_facts(plan, rig_clear_attested=True)
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


@pytest.mark.parametrize("change,code", [
    ("calibration", "measure_spl_calibration_required"),
    ("mover", "walk_over_mover_envelope"),
    ("mic", "wired_mic_missing"),
    ("identity", "measurement_mic_unidentified"),
    ("anchor", "seat_anchor_unusable"),
    ("serial", "seat_anchor_unusable"),
    ("sensitivity", "seat_anchor_unusable"),
    ("stop", "walk_commissioning_stop_unset"),
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
    elif change in {"serial", "sensitivity"}:
        sensitivity = replace(facts.anchor.sensitivity, **({"serial": "other"} if change == "serial" else {"sens_factor_db": -10}))
        facts = replace(facts, anchor=replace(facts.anchor, sensitivity=sensitivity))
    elif change == "stop":
        facts = replace(facts, commissioning_stop_db_spl=None)
    elif change == "capacity":
        plan = replace(plan, repeats=129)
    report = preflight(plan, facts)
    issue = next(issue for issue in report.issues if issue.code == code)
    assert report.blocking and issue.blocking and issue.next_action
    assert all(issue.code in REASON_REGISTRY for issue in report.issues)
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


def test_missing_calibration_is_one_blocking_cause():
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),))
    facts = ready_facts(plan)
    report = preflight(plan, replace(facts, anchor=replace(facts.anchor, sensitivity=None)))
    assert [(issue.code, issue.blocking) for issue in report.issues] == [
        ("measure_spl_calibration_required", True),
    ]


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


@pytest.mark.parametrize("level_db,blocked", [(None, False), (-25, False), (-9, False), (-8.9, True), (0, True)])
def test_run_level_keeps_anchor_and_obeys_statement_ceiling(level_db, blocked):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), level=LevelPolicy(level_db=level_db))
    facts = ready_facts(plan)
    report = preflight(plan, facts)
    assert report.plan.level.level_db == level_db
    assert report.plan.level.resolved.reference_volume_db == -18
    assert report.to_dict()["level"]["predicted_db_spl"] == pytest.approx(75 + (level_db + 18 if level_db is not None else 0))
    assert report.blocking is blocked
    assert preflight(report.plan, facts) == report
    if blocked:
        issue, = report.to_dict()["issues"]
        assert issue["code"] == "walk_level_policy_invalid"
        assert issue["evidence"] == {
            "level_db": level_db, "predicted_db_spl": pytest.approx(75 + level_db + 18), "ceiling_db_spl": 85,
            "candidate_id": "base", "anchor_tolerance_db": 1, "lift_bound_db": 0, "margin_db": 1, "bound_db_spl": 84,
        }


@pytest.mark.parametrize("boost,tolerance,admitted,refused", [
    (18, 1, 84.0, 84.1), (0, 1, 84.0, 84.1), (20, 1, 82.65, 82.66), (18, 0.5, 84.5, 84.6),
])
def test_jts3_rung_margin_uses_the_applied_stack(tuning_profile, boost, tolerance, admitted, refused):
    applied = {**BASS_EXTENSION, "low_boost_db": 18, "reference_level_db": 0,
               "delta_highpass_hz": 63, "detector_lowpass_hz": 100}
    candidate = replace(_room_candidate(tuning_profile), bass_extension={**applied, "low_boost_db": boost} if boost else {})
    name = candidate.fingerprint
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, candidate_id=name, purpose="bass"),), candidates=(name,))
    facts = ready_facts(plan, candidates={name: candidate}, applied_bass_extension=applied)
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record, "reference_volume_db": -21,
        "target": {"target_db_spl": 75, "tolerance_db": tolerance}}))
    for spl in (admitted, refused):
        report = preflight(replace(plan, level=LevelPolicy(level_db=-21 + spl - 75)), facts)
        assert report.blocking is (spl == refused)
        if report.blocking:
            issue, = report.issues
            assert issue.code == "walk_level_policy_invalid"
            assert issue.evidence["anchor_tolerance_db"] == tolerance
            assert issue.evidence["lift_bound_db"] == pytest.approx(1.34283 if boost == 20 else 0, abs=0.001)
            assert issue.evidence["bound_db_spl"] == pytest.approx(82.65717 if boost == 20 else 85 - tolerance, abs=0.001)


@pytest.mark.parametrize("missing", ["applied_bass_extension", "anchor_tolerance_db"])
def test_rung_requires_margin_facts(missing):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass"),))
    facts = ready_facts(plan)
    if missing == "applied_bass_extension":
        facts = replace(facts, applied_bass_extension=None)
    else:
        facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record, "target": {"target_db_spl": 75}}))
    report = preflight(plan, facts)
    issue, = report.issues
    assert report.blocking and issue.code == "walk_level_policy_invalid"
    assert issue.evidence["unavailable"] == missing


@pytest.mark.parametrize("missing", ["max_window_db_spl", "loudest_half_second_db_spl", "ceiling_db_spl"])
def test_later_rung_requires_its_previous_capture_spl(missing):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass"),), level=LevelPolicy(level_db=-14.46))
    spl = {"max_window_db_spl": 80.53, "loudest_half_second_db_spl": 76.04, "ceiling_db_spl": 85}
    spl.pop(missing)
    report = preflight(plan, ready_facts(plan), previous_rung=[{"level_db": -21.46, "spl": spl}])
    assert report.blocking
    assert [issue.code for issue in report.issues] == ["walk_level_policy_invalid"]
    assert report.issues[0].evidence["unavailable"] == [missing]
    assert report.issues[0].evidence["requested_level_db"] == -14.46


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
    assert report.rung_admission["stimulus_mismatch"] is (not same)


@pytest.mark.parametrize("descriptor", [None, {}, BASS_EXTENSION])
def test_live_facts_resolve_applied_bass_from_the_candidate_bank(monkeypatch, tuning_profile, descriptor):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass"),))
    anchor = ready_facts(plan).anchor
    applied = replace(_room_candidate(tuning_profile), bass_extension=descriptor or {})
    state = {"status": "applied", "source": {"measured_candidate_fingerprint": applied.fingerprint}}
    monkeypatch.setattr(preflight_live, "load_applied_baseline_profile_state", lambda: state if descriptor is not None else {})
    monkeypatch.setattr(candidate_parts, "find_banked_candidate", lambda name: {applied.fingerprint: SimpleNamespace(candidate=applied)}[name])
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda device: anchor.sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), role_targets={},
        driver_caps_dbfs={}, fc_hz=None, driver_sweep_duration_limits_s={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert facts.applied_bass_extension == (applied.bass_extension if descriptor is not None else None)
    assert preflight(plan, facts).blocking is (descriptor is None)


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


@pytest.mark.parametrize("mover,attested,available", [
    ("human", False, False), ("arm", False, True), ("arm", True, False), ("arm", True, True),
])
def test_live_preflight_discovers_only_the_arm(monkeypatch, mover, attested, available):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), mover=mover)
    ready = ready_facts(plan)
    detect = Mock(return_value=available)
    monkeypatch.setattr(arm_walk.TurntableMover, "available", detect)
    monkeypatch.setattr(preflight_live, "read_output_volume", lambda: {})
    monkeypatch.setattr(preflight_live, "load_seat_level_reference", lambda: ready.anchor.record)
    monkeypatch.setattr(preflight_live, "resolved_household_sensitivity", lambda _: ready.anchor.sensitivity)
    context = SimpleNamespace(topology=None, roles_bands=(), role_targets={},
        preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)))
    facts = preflight_live.read_preflight_facts(
        plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"),
        rig_clear_attested=attested,
    )
    assert facts.rig_clear_attested is attested
    assert facts.mover_available is (available if mover == "arm" else True)
    assert detect.call_count == (1 if mover == "arm" else 0)
