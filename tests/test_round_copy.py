# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from jasper.active_speaker.round_copy import round_lines, coverage_lines


def test_live_and_placement_lines():
    facts = {"pose": 2, "poses": 3, "mover": "human", "pose_detail": {"deg": -20, "elevation_deg": 0},
             "sweep": 4, "sweep_total": 7, "role": "tweeter", "repeat": 2, "repeats": 3}
    assert round_lines(facts) == ["Pose 2 of 3, sweep 4 of 7: tweeter repeat 2 of 3.",
                                 "Keep the microphone still until the tone stops."]
    assert round_lines(facts, pending=True) == ["Pose 2 of 3: 20° left, ear height (human).",
        "Place the microphone. Confirm it is placed to play this pose's sweeps."]
    assert round_lines({"retake_pose": 2, "retake_sweep": 5, "retake_reason": "snr_floor",
                        "retake_action": "retake_louder", "level_raise_dbfs": -12}) == [
        "Pose 2, sweep 5 was under the noise floor; taking it again louder.", "Raising the sweep level to -12 dBFS."]


def test_pre_round_lines_count_the_supplied_schedule():
    sweeps = [{"role": "woofer", "kind": "sweep", "phase": "measure", "program_repeats": 3}] * 3
    facts = {"program": "tournament/full", "poses": 3, "mover": "human", "sweeps_per_pose": [3, 3, 3],
             "sweeps": 9, "pose_sweeps": [sweeps] * 3, "estimated_seconds": 126}
    assert round_lines(facts) == [
        "Pose set tournament/full: 3 poses (human).", "Sweeps per pose: 3, 3, 3; 9 sweeps in total.",
        "Pose 1: 3 woofer sweeps.", "Pose 2: 3 woofer sweeps.", "Pose 3: 3 woofer sweeps.",
        "3 repeats per driver, to measure the noise floor.",
        "A sweep that is too quiet can be taken again louder.",
        "Allow about 3 minutes, plus time for retakes."]


def test_post_round_coverage_keeps_packet_words():
    packet = {"sets": [{"takes": [{"take_id": "a", "role": "woofer", "selected": True, "trusted_floor_hz": 250}]}],
              "next_action": {"label": "Measure timing again"}, "disclosures": ["packet disclosure"]}
    manifest = {"not_measured": [{"pose": {"deg": 20}, "reason": "complete_requested"}]}
    assert coverage_lines(packet, manifest) == ["Measured: 1 take.",
        "Waived: 20° right, ear height; complete_requested.", "Unqualified band (woofer): below 250 Hz.",
        "packet disclosure", "Measure timing again"]
