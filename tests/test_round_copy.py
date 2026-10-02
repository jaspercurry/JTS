# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import re

import pytest

from jasper.active_speaker.crossover_v2.refusal_copy import (
    CAPTURE_QUALITY_REFUSAL_CODES, REASON_CAPTURE_OVERRUN, REASON_CLIPPED, REASON_LEVEL_UNSOLVED, REASON_NOT_REACHED,
    REASON_REGISTRY, REASON_SNR_FLOOR, TEMPLATE_SILENT_AUTO_RETRY, refusal_copy_for,
)
from jasper.active_speaker import round_copy
from jasper.active_speaker.round_copy import (
    LEVEL_STEP_LINES, PLACE_MICROPHONE, RUN_ENDED, round_lines, coverage_lines, pose_name, round_verdict, status_lines,
    take_counts,
)
from jasper.active_speaker.measurement_programs import plan_poses, run_preset
from jasper.active_speaker.measurement_view import round_status


@pytest.mark.parametrize("pending", [False, True])
def test_live_and_placement_lines(pending):
    facts = {"pose": 2, "poses": 3, "mover": "human", "measurements_per_pose": [8, 8, 8],
             "measurements": 24, "measurement": 11, "role": "summed", "pose_details": [{}, {}, {}]}
    lines = round_lines(facts, pending=pending)
    assert [int(n) for n in re.findall(r"\d+", lines[0])] == ([2, 3, 9, 16, 0] if pending else [11, 24, 2, 3])
    if pending:
        assert lines[-1] == PLACE_MICROPHONE


@pytest.mark.parametrize("step", [None, *LEVEL_STEP_LINES])
def test_a_take_at_a_driver_pose_says_which_level_step_plays(step):
    facts = {"pose": 1, "poses": 3, "mover": "human", "measurements": 3, "measurement": 1, "role": "woofer",
             "pose_details": [{}, {}, {}], **({"level_step": step} if step else {})}
    lines = round_lines(facts)
    assert [line for line in lines if line in LEVEL_STEP_LINES.values()] == ([LEVEL_STEP_LINES[step]] if step else [])
    assert not set(LEVEL_STEP_LINES.values()) & set(round_lines(facts, pending=True))


@pytest.mark.parametrize("facts, expected", [({}, RUN_ENDED), ({"poses": 3}, ""), ({"poses": 3, "status": "complete"}, RUN_ENDED)])
def test_round_verdict(facts, expected):
    assert round_verdict(facts, RUN_ENDED) == expected


@pytest.mark.parametrize("reason", sorted(CAPTURE_QUALITY_REFUSAL_CODES | {
    "channel_map_mismatch", "clipped",
}))
def test_retake_uses_registry_words_without_codes(reason):
    line, = round_lines({"retake_pose": 2, "retake_measurement": 1, "retake_reason": reason})
    assert refusal_copy_for(reason)[0] in line
    assert "_" not in line
    coverage = coverage_lines({}, {"honoured": {"retakes": 0}, "not_measured": [{"pose": {"azimuth_deg": 20}, "reason": reason}]})
    assert refusal_copy_for(reason)[0] in coverage[-1]
    assert "_" not in coverage[-1]


@pytest.mark.parametrize("reasons", [
    *([retry, REASON_LEVEL_UNSOLVED] for retry in sorted(
        code for code, spec in REASON_REGISTRY.items() if spec.template == TEMPLATE_SILENT_AUTO_RETRY)),
    [REASON_SNR_FLOOR, REASON_LEVEL_UNSOLVED],
    [REASON_CLIPPED, REASON_CAPTURE_OVERRUN],
])
def test_a_not_measured_line_keeps_every_cause_of_its_pose(reasons):
    """A pose that lists a retry's cause and another reason says both: JTS was busy, and then found no level."""
    rows = [{"pose": {"azimuth_deg": 20}, "reason": reason} for reason in reasons]

    line = coverage_lines({}, {"honoured": {"retakes": 0}, "not_measured": rows})[-1]

    assert line.endswith(" ".join(refusal_copy_for(code)[0] for code in reasons))


def test_each_not_measured_pose_says_its_own_reason():
    """A pose the run never reached does not take the stop reason of the pose it stopped at."""
    rows = [{"pose": {"azimuth_deg": 0}, "reason": REASON_CLIPPED},
            {"pose": {"azimuth_deg": 30}, "reason": REASON_NOT_REACHED}]

    stopped, unreached = coverage_lines({}, {"honoured": {"retakes": 7}, "not_measured": rows})[-2:]

    assert stopped.endswith(refusal_copy_for(REASON_CLIPPED)[0])
    assert unreached.endswith(refusal_copy_for(REASON_NOT_REACHED)[0])


def test_manual_redo_and_ended_round_lines():
    assert re.findall(r"\d+", round_lines({"retake_pose": 2, "retake_reason": "operator"})[0]) == ["2"]


@pytest.mark.parametrize("error", ["OSError", "packet_save_failed"])
def test_packet_failure_keeps_details_out_of_copy(error):
    lines = round_lines({"status": "complete", "takes": 3, "packet_error": error})
    assert len(lines) == 3
    assert re.findall(r"\d+", " ".join(lines)) == ["3", "0", "0"]
    assert error not in " ".join(lines)


def test_pre_round_lines_count_the_supplied_schedule():
    facts = {"program": "trial", "poses": 1, "mover": "human", "measurements_per_pose": [8],
             "measurements": 8, "estimated_seconds": 126}
    lines = round_lines(facts)
    assert re.findall(r"\d+", lines[1]) == ["8", "8"]
    assert round_status({"run": facts, "join": {"mover": "human"}}) == round_lines(facts) + [PLACE_MICROPHONE]


def test_a_runs_status_is_its_banked_rounds_coverage_once_it_has_one(monkeypatch):
    """A refused round's live facts count no unmeasured measurement. The page and the console list the
    spots its banked packet names, and an unreadable packet says so."""
    facts = {"status": "complete", "takes": 0, "not_measured": 0}
    banked = {**facts, "round_dir": "round-1"}
    monkeypatch.setattr(round_copy, "packet_lines", lambda directory: ["covered spot"])
    assert status_lines(banked) == round_status({"run": banked}) == ["covered spot"]
    assert status_lines(facts) == round_lines(facts)
    monkeypatch.setattr(round_copy, "packet_lines", lambda directory: [])
    assert status_lines(banked) == round_lines({**banked, "packet_error": "packet_unreadable"}) != round_lines(facts)


def test_post_round_coverage_keeps_packet_words():
    packet = {"sets": [{"takes": [{"take_id": "a", "role": "woofer", "selected": True, "trusted_floor_hz": 250}]}],
              "next_action": {"label": "Measure timing again"}, "disclosures": ["packet disclosure"]}
    manifest = {"sets": packet["sets"], "honoured": {"retakes": 0},
                "not_measured": [{"pose": {"azimuth_deg": 20}, "reason": "complete_requested"}]}
    lines = coverage_lines(packet, manifest)
    assert re.findall(r"\d+", " ".join(lines[:3])) == ["1", "0", "20", "250"]
    assert lines[-2:] == [*packet["disclosures"], packet["next_action"]["label"]]


def test_unmeasured_poses_are_distinct_and_counted_once():
    poses = [{"azimuth_deg": 0, "kind": "bearing"}, {"azimuth_deg": 0, "kind": "behind"}]
    manifest = {"honoured": {"retakes": 0},
                "not_measured": [{"pose": pose, "reason": "user_stopped"} for pose in poses for _ in range(2)]}
    lines = coverage_lines({}, manifest)[1:]
    assert len(lines) == 2
    assert pose_name(poses[0]) != pose_name(poses[1])
    for pose, line in zip(poses, lines, strict=True):
        assert pose["kind"] in pose_name(pose)
        assert pose_name(pose) in line
        assert "2" in line


def test_a_near_field_pose_is_named_by_its_driver_and_distance():
    """A cardioid near-field round names each placement by its driver and
    distance (ADR-0360)."""
    names = [pose_name({"kind": pose.kind, "distance_m": pose.distance_m, "driver": pose.driver})
             for pose in plan_poses(run_preset("nearfield"), ("tweeter", "woofer", "woofer:rear"))]
    assert names == ["woofer at 15 mm", "woofer at 30 mm", "rear woofer at 15 mm", "rear woofer at 30 mm"]


@pytest.mark.parametrize("kept,retakes", [(9, 1), (8, 0), (0, 0)])
def test_ended_counts_use_selected_takes_once(kept, retakes):
    """A kept take counts once across its roles' sets, and the retakes are the
    run's own count (``honoured.retakes``, ADR-0395)."""
    takes = [{"take_id": str(n), "record_id": f"{n}.json", "selected": n >= retakes} for n in range(kept + retakes)]
    manifest = {"sets": [{"takes": takes}, {"takes": takes}], "honoured": {"retakes": retakes}}
    counts = take_counts(manifest)
    assert counts == {"takes": kept, "retakes": retakes, "not_measured": 0}
    lines = round_lines({"status": "complete", **counts})
    assert re.findall(r"\d+", lines[0]) == [str(kept), str(retakes)]
    assert coverage_lines(manifest, manifest)[0] == lines[0]
