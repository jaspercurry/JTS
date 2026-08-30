# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for bounding and grading the reverse-null delay walk."""

import json
import math

import pytest

from jasper.active_speaker.delay_sweep import (
    ROBUST_NULL_DEPTH_DB,
    USABLE_NULL_DEPTH_DB,
    VERDICT_AXIS_LIMITED,
    VERDICT_ROBUST,
    VERDICT_WEAK,
    rows_at_pose,
    sweep_spec,
    sweep_verdict,
)
from jasper.audio_measurement.null_walk import (
    BoundedNullWalkSchedule,
    select_delay,
    select_scheduled_delay,
)

FC_HZ = 1800.0


def _spec(seed_m=0.0):
    return sweep_spec(
        crossover_fc_hz=FC_HZ,
        upper_role="tweeter",
        lower_role="woofer",
        signed_acoustic_path_difference_m=seed_m,
    )


# --------------------------------------------------------------------------- #
# the null-depth search recovers a known offset
# --------------------------------------------------------------------------- #


def _synthetic_depth(applied_us: float, true_offset_us: float) -> float:
    """Null depth for two equal, inverted branches mistimed by the residual.

    Summing a signal with its inverted, delayed copy gives 2*sin(pi*f*tau); the
    null depth relative to the aligned case is -20*log10 of that, so a perfect
    cancellation is deep and a half-period error is 0 dB. Capped well below the
    analytic infinity at tau=0 because a real null floors on noise.
    """

    residual_s = (applied_us - true_offset_us) * 1e-6
    ratio = abs(2.0 * math.sin(math.pi * FC_HZ * residual_s))
    if ratio < 1e-6:
        return 40.0
    return min(40.0, max(0.0, -20.0 * math.log10(ratio / 2.0)))


def _rows(depth: float, *, pose_deg=None, count=5):
    acoustic = {
        "null_depth_db": depth,
        "null_depth_capped": False,
        "mic_clipping": False,
        "calibrated": True,
        "expect_null": True,
        "crossover_fc_hz": FC_HZ,
        "gating": {"applied": True},
        "above_validity_floor": True,
        "snr": {"decision_class": "alignment", "verdict": "ok"},
        "verdict": "blend_ok",
    }
    return [
        {"acoustic": dict(acoustic), "pose_deg": pose_deg} for _ in range(count)
    ]


def _graded(true_offset_us: float, *, poses=(None,), scale=1.0):
    spec = _spec()
    coarse = spec.coarse_candidate_delays_us()
    rows = {
        coordinate: [
            row
            for pose in poses
            for row in _rows(
                _synthetic_depth(coordinate, true_offset_us) * scale, pose_deg=pose
            )
        ]
        for coordinate in coarse
    }
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(
        spec, {coordinate: rows[coordinate] for coordinate in coarse}
    )
    for coordinate in schedule.refinement_delays_us:
        rows[coordinate] = [
            row
            for pose in poses
            for row in _rows(
                _synthetic_depth(coordinate, true_offset_us) * scale, pose_deg=pose
            )
        ]
    selection = select_scheduled_delay(spec, schedule, rows)
    return spec, rows, selection


@pytest.mark.parametrize("true_offset_us", [-200.0, -100.0, 0.0, 100.0, 200.0])
def test_known_offset_is_recovered_within_one_step(true_offset_us):
    spec, rows, selection = _graded(true_offset_us)
    assert selection["status"] == "selected"
    assert abs(selection["selected_relative_delay_us"] - true_offset_us) <= spec.step_us

    verdict = sweep_verdict(selection, spec=_spec(), rows_by_delay=rows)
    assert verdict["verdict"] == VERDICT_ROBUST
    assert verdict["meets_robustness_bar"] is True
    assert verdict["best_measured_null_depth_db"] >= ROBUST_NULL_DEPTH_DB


def test_positive_offset_delays_the_upper_branch_and_negative_the_lower():
    _, _, positive = _graded(200.0)
    _, _, negative = _graded(-200.0)
    assert positive["selected_delay_target"] == "tweeter"
    assert negative["selected_delay_target"] == "woofer"
    assert positive["selected_delay_us"] >= 0.0
    assert negative["selected_delay_us"] >= 0.0


# --------------------------------------------------------------------------- #
# the honest verdict
# --------------------------------------------------------------------------- #


def test_no_coordinate_reaching_the_floor_reads_as_axis_limited_not_an_error():
    # Every depth scaled under the usable floor: a real null never forms, so the
    # residual at Fc is not something a delay can move.
    _, rows, selection = _graded(0.0, scale=0.2)
    verdict = sweep_verdict(selection, spec=_spec(), rows_by_delay=rows)
    assert verdict["verdict"] == VERDICT_AXIS_LIMITED
    assert verdict["best_measured_null_depth_db"] < USABLE_NULL_DEPTH_DB
    assert verdict["meets_robustness_bar"] is False
    # Disclosed, never raised: the selected coordinate is still reported.
    assert verdict["selected_delay_us"] is not None


def test_a_shallow_but_usable_null_grades_weak():
    _, rows, selection = _graded(0.0, scale=0.45)
    verdict = sweep_verdict(selection, spec=_spec(), rows_by_delay=rows)
    assert USABLE_NULL_DEPTH_DB <= verdict["best_measured_null_depth_db"]
    assert verdict["best_measured_null_depth_db"] < ROBUST_NULL_DEPTH_DB
    assert verdict["verdict"] == VERDICT_WEAK


def test_poses_disagreeing_about_the_null_downgrades_a_deep_result():
    spec = _spec()
    coarse = spec.coarse_candidate_delays_us()
    # On axis the null sits at +100 us; off axis it sits at -200 us. A deep null
    # that moves with the microphone is not a delay answer.
    rows = {
        coordinate: (
            _rows(_synthetic_depth(coordinate, 100.0), pose_deg=0)
            + _rows(_synthetic_depth(coordinate, -200.0), pose_deg=15)
        )
        for coordinate in coarse
    }
    on_axis = rows_at_pose(rows, 0)
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, dict(on_axis))
    for coordinate in schedule.refinement_delays_us:
        rows[coordinate] = (
            _rows(_synthetic_depth(coordinate, 100.0), pose_deg=0)
            + _rows(_synthetic_depth(coordinate, -200.0), pose_deg=15)
        )
    selection = select_scheduled_delay(spec, schedule, rows_at_pose(rows, 0))
    verdict = sweep_verdict(selection, spec=spec, rows_by_delay=rows, poses_deg=(0, 15))
    assert verdict["poses_agree"] is False
    assert verdict["verdict"] == VERDICT_WEAK


def test_incomplete_evidence_is_refused_with_the_selectors_own_reason():
    spec = _spec()
    rows = {coordinate: _rows(30.0) for coordinate in spec.candidate_delays_us()}
    # One coordinate short of MIN_CAPTURE_COUNT is enough to refuse the walk.
    rows[spec.candidate_delays_us()[0]] = _rows(30.0, count=2)
    selection = select_delay(spec, rows)
    verdict = sweep_verdict(selection, spec=_spec(), rows_by_delay=rows)
    assert verdict["verdict"] == "evidence_incomplete"
    assert verdict["reason"] == selection["reason"]
    assert verdict["selected_delay_us"] is None


# --------------------------------------------------------------------------- #
# the operator door
# --------------------------------------------------------------------------- #


def test_plan_prints_the_bounded_grid_and_its_capture_cost(capsys):
    from jasper.cli.delay_sweep import main

    assert main(["plan", "--fc-hz", "1800", "--repeats", "5"]) == 0
    payload = json.loads(capsys.readouterr().out)
    spec = _spec()
    assert payload["coarse_delays_us"] == list(spec.coarse_candidate_delays_us())
    assert payload["spec"]["half_period_us"] == spec.half_period_us
    # The cost is knowable before any sound: coarse plus at most two refinement
    # neighbours, times the repeats, times the poses.
    assert payload["maximum_captures"] == (len(payload["coarse_delays_us"]) + 2) * 5


def test_grade_reads_banked_rows_and_reports_the_prescription_number(tmp_path, capsys):
    from jasper.cli.delay_sweep import main

    spec = _spec()
    rows = {
        str(coordinate): _rows(_synthetic_depth(coordinate, 100.0))
        for coordinate in spec.candidate_delays_us()
    }
    captures = tmp_path / "rows.json"
    captures.write_text(json.dumps(rows), encoding="utf-8")

    assert main(["grade", "--fc-hz", "1800", "--captures", str(captures)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "graded"
    assert payload["verdict"]["verdict"] == VERDICT_ROBUST
    assert abs(payload["verdict"]["selected_relative_delay_us"] - 100.0) <= spec.step_us
    assert payload["verdict"]["selected_delay_target"] == "tweeter"


def test_grade_refuses_unreadable_captures_without_a_traceback(tmp_path, capsys):
    from jasper.cli.delay_sweep import main

    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    assert main(["grade", "--fc-hz", "1800", "--captures", str(broken)]) == 2
    assert "unreadable captures" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# review findings, pinned
# --------------------------------------------------------------------------- #


def test_a_refusal_verdict_still_carries_every_key_a_consumer_reads():
    spec = _spec()
    rows = {coordinate: _rows(30.0) for coordinate in spec.candidate_delays_us()}
    rows[spec.candidate_delays_us()[0]] = _rows(30.0, count=2)
    verdict = sweep_verdict(select_delay(spec, rows), spec=spec, rows_by_delay=rows)
    assert verdict["selected_relative_delay_us"] is None
    assert set(verdict) == set(
        sweep_verdict(_graded(100.0)[2], spec=spec, rows_by_delay=_graded(100.0)[1])
    )


def test_an_ungradeable_off_axis_pose_cannot_agree_by_silence():
    spec = _spec()
    coarse = spec.coarse_candidate_delays_us()
    rows = {
        coordinate: (
            _rows(_synthetic_depth(coordinate, 100.0), pose_deg=0)
            # The off-axis pose clipped: gradeable rows, zero of them valid.
            + _rows(30.0, pose_deg=15, count=5)
        )
        for coordinate in coarse
    }
    for coordinate in coarse:
        for row in rows[coordinate]:
            if row["pose_deg"] == 15:
                row["acoustic"]["mic_clipping"] = True
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(
        spec, dict(rows_at_pose(rows, 0))
    )
    selection = select_scheduled_delay(spec, schedule, rows_at_pose(rows, 0))
    verdict = sweep_verdict(
        selection, spec=spec, rows_by_delay=rows, poses_deg=(0, 15)
    )
    assert verdict["pose_best_delays_us"]["15"] is None
    assert verdict["unmeasured_poses_deg"] == ["15"]
    assert verdict["poses_agree"] is False
    assert verdict["verdict"] == VERDICT_WEAK


