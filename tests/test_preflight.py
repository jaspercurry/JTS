# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
import json

import pytest

from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop, REGIME_SUMMED
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY
from jasper.active_speaker.preflight import PreflightFacts, preflight
from jasper.active_speaker.seat_level_reference import AnchorFacts
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.cli import angle_capture as cli
from tests.test_crossover_v2_tuning_scope import (
    BASS_EXTENSION, _room_candidate, tuning_profile as tuning_profile,
)


def ready_facts(plan, **changes):
    return replace(PreflightFacts(
        candidates={}, mic_present=True, mic_identified=True,
        anchor=AnchorFacts({"measured_db_spl": 75.0, "reference_volume_db": -18.0,
                            "mic_sensitivity": {"sens_factor_db": -12.0, "serial": "1234"}},
                           MicSensitivity(-12.0, 18.0, "1234")),
        commissioning_stop_db_spl=85.0, mover=plan.mover,
    ), **changes)


@pytest.mark.parametrize("change,code", [
    ("ceiling", "walk_ceiling_above_stop"),
    ("calibration", "measure_spl_calibration_required"),
    ("mover", "walk_over_mover_envelope"),
    ("mic", "wired_mic_missing"),
    ("identity", "measurement_mic_unidentified"),
    ("anchor", "seat_anchor_unusable"),
    ("serial", "seat_anchor_unusable"),
    ("sensitivity", "seat_anchor_unusable"),
    ("anchor_over_stop", "level_over_ceiling"),
    ("stop", "walk_commissioning_stop_unset"),
    ("candidate", "not_found"),
    ("capacity", "walk_over_capture_capacity"),
])
def test_preflight_issues(change, code):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, kind="seat", seat_offset_m=(0, 0, 0)),), spl_ceiling_db_spl=80)
    facts = ready_facts(plan)
    if change == "candidate":
        plan = replace(plan, candidates=("missing",), stops=(replace(plan.stops[0], candidate_id="missing"),))
    elif change == "ceiling":
        plan = replace(plan, spl_ceiling_db_spl=86)
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
    elif change == "anchor_over_stop":
        facts = replace(facts, anchor=replace(facts.anchor, record={**facts.anchor.record, "measured_db_spl": 90}))
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
    assert [(row.pose, row.level_window_db, row.candidate_id, row.repeat) for row in report.schedule] == [
        (stop.place, -18.0, stop.candidate_id or "base", repeat)
        for stop in plan.stops for repeat in (1, 2)
    ]
    assert report.mic_moves == report.price["mic_moves"] == 3
    assert report.price["captures"] == 12
    assert report.price["ceiling_min"] > 0
    assert report.spl_ceiling_db_spl == 85
    assert report.plan.level.resolved.anchor_db_spl == 75
    assert report.plan.baseline_graph_scope == "base"


@pytest.mark.parametrize("verb", ["plan", "stage"])
def test_cli_plan_prints_blocking_preflight(monkeypatch, capsys, verb):
    monkeypatch.setattr(cli, "read_preflight_facts", lambda plan: ready_facts(plan))
    assert cli.main([verb, "--angles", "0", "--spl-ceiling-db-spl", "90"]) == 1
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert captured.err
    assert body["code"] == "walk_ceiling_above_stop"
    assert body["detail"]["issues"][0]["blocking"] is True
    assert body["next_action"]


@pytest.mark.parametrize("fault", ["box", "wrong_mic", "no_calibration"])
def test_live_facts_surface_owner_refusals(monkeypatch, fault):
    from types import SimpleNamespace
    from jasper.active_speaker import preflight_live
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
    from jasper.audio_measurement import calibration, household_mic

    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), spl_ceiling_db_spl=80)
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


@pytest.mark.parametrize("ceiling", [None, 80])
def test_missing_calibration_is_one_blocking_cause(ceiling):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED),), spl_ceiling_db_spl=ceiling)
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
    assert report.schedule[0].graph_scope == ("bass_candidate" if bass else "room_candidate")
    assert report.to_dict()["baseline_graph_scope"] == plan.baseline_graph_scope


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
