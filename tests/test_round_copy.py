# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.active_speaker.crossover_v2.refusal_copy import CAPTURE_QUALITY_REFUSAL_CODES, refusal_copy_for
from jasper.active_speaker.round_copy import PLACE_MICROPHONE, RUN_ENDED, round_lines, coverage_lines, pose_name, round_verdict
from jasper.active_speaker.commissioning_coordinator import round_status


@pytest.mark.parametrize("facts, pending, expected", [
    ({"pose": 2, "poses": 3, "mover": "human", "sweeps_per_pose": [7, 7, 7], "pose_details": [{}, {}, {}],
      "sweep": 4, "role": "tweeter", "repeat": 2, "repeats": 3}, False,
     ["Pose 2 of 3, sweep 4 of 7: tweeter repeat 2 of 3.", "Keep the microphone still until the tone stops."]),
    ({"pose": 2, "poses": 3, "mover": "human", "pose_details": [{}, {"deg": -20, "elevation_deg": 15}, {}]}, True,
     ["Pose 2 of 3: -20° · 15° up (human).", "Place the microphone. Confirm it is placed to play this pose's sweeps."]),
    ({"retake_pose": 2, "retake_sweep": 5, "retake_reason": "snr_floor",
      "retake_action": "retake_louder", "level_raise_dbfs": -12}, False,
     ["Pose 2, sweep 5: The room is too loud right now, or the microphone is too far away. "
      "Quiet the room or move the microphone closer, then try again. Taking it again louder.",
      "Raising the sweep level to -12 dBFS."]),
])
def test_live_and_placement_lines(facts, pending, expected):
    assert round_lines(facts, pending=pending) == expected


@pytest.mark.parametrize("facts, expected", [({}, "verdict"), ({"poses": 3}, "")])
def test_round_verdict(facts, expected):
    assert round_verdict(facts, "verdict") == expected


@pytest.mark.parametrize("reason", sorted(CAPTURE_QUALITY_REFUSAL_CODES | {
    "channel_map_mismatch", "clipped", "measure_gain_adjusted",
}))
def test_retake_uses_registry_words_without_codes(reason):
    line, = round_lines({"retake_pose": 2, "retake_sweep": 1, "retake_sweep_end": 8, "retake_reason": reason})
    assert refusal_copy_for(reason)[0] in line
    assert "_" not in line
    coverage = coverage_lines({}, {"not_measured": [{"pose": {"deg": 20}, "reason": reason}]})
    assert refusal_copy_for(reason)[0] in coverage[-1]
    assert "_" not in coverage[-1]


def test_manual_redo_and_ended_round_lines():
    assert round_lines({"retake_pose": 2, "retake_reason": "operator"}) == ["Pose 2: you asked to redo this pose."]
    assert RUN_ENDED == "This run has ended. Choose a pose set to start the next one."


@pytest.mark.parametrize("error", ["OSError", "packet_save_failed"])
def test_packet_failure_keeps_details_out_of_copy(error):
    assert round_lines({"status": "complete", "takes": 3, "packet_error": error}) == [
        "Measured: 3 takes.", "Not measured: 0 planned captures.",
        "The round packet could not be saved. Run jasper-round wait to try again."]


def test_pre_round_lines_count_the_supplied_schedule():
    sweeps = [{"role": "woofer", "kind": "sweep", "phase": "measure", "repeats": 3}] * 3
    facts = {"program": "tournament/full", "poses": 3, "mover": "human", "sweeps_per_pose": [3, 3, 3],
             "sweeps": 9, "pose_sweeps": [sweeps] * 3, "estimated_seconds": 126}
    assert round_lines(facts) == [
        "Pose set tournament/full: 3 poses (human).", "Sweeps per pose: 3, 3, 3; 9 sweeps in total.",
        "Pose 1: 3 woofer sweeps.", "Pose 2: 3 woofer sweeps.", "Pose 3: 3 woofer sweeps.",
        "3 repeats per driver, to measure the noise floor.",
        "A sweep that is too quiet can be taken again louder.",
        "Allow about 3 minutes, plus time for retakes."]
    assert round_status({"run": facts, "join": {"mover": "human"}}) == round_lines(facts) + [PLACE_MICROPHONE]


def test_post_round_coverage_keeps_packet_words():
    packet = {"sets": [{"takes": [{"take_id": "a", "role": "woofer", "selected": True, "trusted_floor_hz": 250}]}],
              "next_action": {"label": "Measure timing again"}, "disclosures": ["packet disclosure"]}
    manifest = {"not_measured": [{"pose": {"deg": 20}, "reason": "complete_requested"}]}
    assert coverage_lines(packet, manifest) == ["Measured: 1 take.",
        "Waived: +20°.", "Unqualified band (woofer): below 250 Hz.",
        "packet disclosure", "Measure timing again"]


def test_unmeasured_poses_are_distinct_and_counted_once():
    poses = [{"deg": 0, "kind": "bearing"}, {"deg": 0, "kind": "behind"}]
    manifest = {"not_measured": [{"pose": pose, "reason": "user_stopped"} for pose in poses for _ in range(2)]}
    lines = coverage_lines({}, manifest)[1:]
    assert len(lines) == 2
    assert pose_name(poses[0]) != pose_name(poses[1])
    for pose, line in zip(poses, lines, strict=True):
        assert pose["kind"] in pose_name(pose)
        assert pose_name(pose) in line
        assert "2" in line
