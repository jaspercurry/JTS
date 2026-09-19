# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import re

import pytest

from jasper.active_speaker.crossover_v2.refusal_copy import CAPTURE_QUALITY_REFUSAL_CODES, refusal_copy_for
from jasper.active_speaker.round_copy import PLACE_MICROPHONE, RUN_ENDED, round_lines, coverage_lines, pose_name, round_verdict, take_counts
from jasper.active_speaker.commissioning_coordinator import round_status


@pytest.mark.parametrize("pending", [False, True])
def test_live_and_placement_lines(pending):
    facts = {"pose": 2, "poses": 3, "mover": "human", "measurements_per_pose": [8, 8, 8],
             "measurements": 24, "measurement": 11, "role": "summed", "pose_details": [{}, {}, {}]}
    lines = round_lines(facts, pending=pending)
    assert [int(n) for n in re.findall(r"\d+", lines[0])] == ([2, 3, 9, 16, 0] if pending else [11, 24, 2, 3])
    if pending:
        assert lines[-1] == PLACE_MICROPHONE


@pytest.mark.parametrize("facts, expected", [({}, RUN_ENDED), ({"poses": 3}, ""), ({"poses": 3, "status": "complete"}, RUN_ENDED)])
def test_round_verdict(facts, expected):
    assert round_verdict(facts, RUN_ENDED) == expected


@pytest.mark.parametrize("reason", sorted(CAPTURE_QUALITY_REFUSAL_CODES | {
    "channel_map_mismatch", "clipped", "measure_gain_adjusted",
}))
def test_retake_uses_registry_words_without_codes(reason):
    line, = round_lines({"retake_pose": 2, "retake_measurement": 1, "retake_reason": reason})
    assert refusal_copy_for(reason)[0] in line
    assert "_" not in line
    coverage = coverage_lines({}, {"not_measured": [{"pose": {"deg": 20}, "reason": reason}]})
    assert refusal_copy_for(reason)[0] in coverage[-1]
    assert "_" not in coverage[-1]


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


def test_post_round_coverage_keeps_packet_words():
    packet = {"sets": [{"takes": [{"take_id": "a", "role": "woofer", "selected": True, "trusted_floor_hz": 250}]}],
              "next_action": {"label": "Measure timing again"}, "disclosures": ["packet disclosure"]}
    manifest = {"sets": packet["sets"], "not_measured": [{"pose": {"deg": 20}, "reason": "complete_requested"}]}
    lines = coverage_lines(packet, manifest)
    assert re.findall(r"\d+", " ".join(lines[:3])) == ["1", "0", "20", "250"]
    assert lines[-2:] == [*packet["disclosures"], packet["next_action"]["label"]]


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


@pytest.mark.parametrize("kept,retakes", [(9, 1), (8, 0), (0, 0)])
def test_ended_counts_use_selected_takes_once(kept, retakes):
    takes = [{"take_id": str(n), "selected": n >= retakes, "attempt": 2 if n == retakes and retakes else 1}
             for n in range(kept + retakes)]
    manifest = {"sets": [{"takes": takes}, {"takes": takes}]}
    counts = take_counts(manifest)
    assert counts == {"takes": kept, "retakes": retakes}
    lines = round_lines({"status": "complete", **counts})
    assert re.findall(r"\d+", lines[0]) == [str(kept), str(retakes)]
    assert coverage_lines(manifest, manifest)[0] == lines[0]
