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
    from tests.test_active_speaker_runtime_contract import _active_topology
    from tests.test_active_speaker_audition import _applied_profile

    topology = _active_topology("mono", "active_2_way")
    return replace(PreflightFacts(
        applied_profile=_applied_profile(topology), topology=topology, candidates={}, mic_present=True, mic_identified=True,
        anchor=AnchorFacts({"measured_db_spl": 75.0, "reference_volume_db": -18.0,
                            "mic_sensitivity": {"sens_factor_db": -12.0, "serial": "1234"}},
                           MicSensitivity(-12.0, 18.0, "1234")),
        commissioning_stop_db_spl=85.0, mover=plan.mover,
    ), **changes)


@pytest.mark.parametrize("change,code", [
    ("room", "measurement_candidate_room_mismatch"),
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
def test_preflight_issues(tuning_profile, change, code):
    plan = AngleCaptureRequest((AngleStop(0, REGIME_SUMMED, kind="seat", seat_offset_m=(0, 0, 0)),), spl_ceiling_db_spl=80)
    facts = ready_facts(plan)
    if change in {"room", "candidate"}:
        candidate = replace(_room_candidate(tuning_profile), bass_extension=BASS_EXTENSION)
        name = candidate.fingerprint
        plan = replace(plan, candidates=(name,), stops=(replace(plan.stops[0], candidate_id=name),))
        facts = replace(facts, applied_profile=tuning_profile.applied_profile,
                        topology=tuning_profile.topology, candidates={name: candidate} if change == "room" else {})
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
    report = preflight(plan, ready_facts(plan, candidates={name: candidate},
        topology=tuning_profile.topology, applied_profile=tuning_profile.applied_profile))
    assert report.issues == ()
    assert [(row.pose, row.level_window_db, row.candidate_id, row.repeat) for row in report.schedule] == [
        (stop.place, -18.0, stop.candidate_id or "base", repeat)
        for stop in plan.stops for repeat in (1, 2)
    ]
    assert report.mic_moves == report.price["mic_moves"] == 3
    assert report.price["captures"] == 12
    assert report.price["ceiling_min"] > 0
    assert report.spl_ceiling_db_spl == 85
    assert report.plan.level.anchor_db_spl == 75
    assert report.plan.baseline_graph_scope == "base"


def test_cli_plan_prints_blocking_preflight(monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_preflight_facts", lambda plan: ready_facts(plan))
    assert cli.main(["plan", "--angles", "0", "--spl-ceiling-db-spl", "90"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["code"] == "walk_ceiling_above_stop"
    assert body["issues"][0]["blocking"] is True
    assert body["next_action"]
