# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
from types import SimpleNamespace

import pytest

from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop, LevelPolicy, REGIME_SUMMED
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.preflight import PreflightFacts, preflight
from jasper.active_speaker import preflight_live
from jasper.active_speaker.seat_level_reference import AnchorFacts
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.program import FrequencyBand, RoleBand
from tests.test_crossover_v2_tuning_scope import (
    BASS_EXTENSION, _room_candidate, tuning_profile as tuning_profile,
)


def ready_facts(plan, **changes):
    return replace(PreflightFacts(
        candidates={}, mic_present=True, mic_identified=True,
        anchor=AnchorFacts({"artifact_schema_version": 2, "session_id": "session", "leveled_at": "2026-09-12T00:00:00Z",
                            "target": {"target_db_spl": 75.0}, "measured_db_spl": 75.0, "reference_volume_db": -18.0,
                            "mic_sensitivity": {"sens_factor_db": -12.0, "serial": "1234"}},
                           MicSensitivity(-12.0, 18.0, "1234")),
        commissioning_stop_db_spl=85.0, mover=plan.mover,
    ), **changes)


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
        return SimpleNamespace(topology=None,
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


@pytest.mark.parametrize("level_db,blocked", [(None, False), (-25, False), (-17, False), (-16.99, True), (0, True)])
def test_run_level_keeps_anchor_and_obeys_statement_ceiling(level_db, blocked):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), level=LevelPolicy(level_db=level_db))
    facts = ready_facts(plan)
    report = preflight(plan, facts)
    assert report.plan.level.level_db == level_db
    assert report.plan.level.resolved.reference_volume_db == -18
    assert report.blocking is blocked
    assert preflight(report.plan, facts) == report
    if blocked:
        issue, = report.to_dict()["issues"]
        assert issue["code"] == "walk_level_policy_invalid"
        assert issue["evidence"] == {"level_db": level_db, "predicted_db_spl": pytest.approx(75 + level_db + 18), "ceiling_db_spl": 85}


@pytest.mark.parametrize("level_db,has_ambient,blocked", [(-24.809, True, False), (-34.809, True, True), (-34.809, False, False)])
@pytest.mark.parametrize("fc_hz,band,ambient_row,floor", [
    (2000, (200, 800), (160, 350, -68.4), -43.4),
    (625, (200, 250), (160, 350, -68.4), -43.4),
    (None, (550, 800), (350, 1000, -71.3), -46.3),
])
def test_summed_pilot_floor_uses_banked_ambient(monkeypatch, level_db, has_ambient, blocked, fc_hz, band, ambient_row, floor):
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
    context = SimpleNamespace(
        topology=None, preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=85)),
        roles_bands=(RoleBand("woofer", 0, FrequencyBand(550 if fc_hz is None else 20, 20000)),),
        fc_hz=fc_hz, driver_caps_dbfs={"woofer": -8}, session_volume_db=record["reference_volume_db"], driver_sweep_duration_limits_s={},
    )
    facts = preflight_live.read_preflight_facts(plan, context=context, device=SimpleNamespace(model_key="minidsp_umik2"))
    assert facts.summed_pilot_band_hz == (band if has_ambient else None)
    outcome = preflight(plan, facts)
    assert outcome.blocking is blocked
    assert preflight(outcome.plan, facts) == outcome
    if blocked:
        issue, = outcome.to_dict()["issues"]
        assert issue["code"] == "run_level_pilots_under_ambient"
        assert issue["next_action"]
        lo, hi, dbfs = ambient_row
        assert issue["evidence"] == {
            "level_db": -34.809, "predicted_pilot_capture_dbfs": pytest.approx(-48.1597),
            "pilot_band_hz": band, "ambient_row": {"band_hz": (lo, hi), "level_dbfs": dbfs},
            "floor_dbfs": pytest.approx(floor),
        }
    else:
        assert outcome.issues == ()


@pytest.mark.parametrize("level_db,blocked", [(-18, False), (-38, True)])
def test_bass_preflight_uses_target_band_noise(level_db, blocked):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, purpose="bass"),),
                               level=LevelPolicy(level_db=level_db))
    facts = ready_facts(plan, summed_pilot_band_hz=(200, 800))
    facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record,
        "ambient_report": {"bands": [{"band_hz": [20, 80], "level_dbfs": -60},
                                       {"band_hz": [200, 800], "level_dbfs": -100}]}}))
    report = preflight(plan, facts)
    assert report.blocking is blocked
    if blocked:
        issue, = report.issues
        assert issue.code == "run_level_pilots_under_ambient"
        assert issue.evidence["pilot_band_hz"] == (20, 60)
        assert issue.evidence["ambient_row"] == {"band_hz": (20, 80), "level_dbfs": -60}
    else:
        assert report.issues == ()
