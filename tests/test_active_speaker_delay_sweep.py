# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the inter-driver reverse-null delay sweep."""

import asyncio
import copy
import json
import math

import pytest

from jasper.active_speaker.delay_sweep import (
    REFUSE_MEASUREMENT_ACTIVE,
    ROBUST_NULL_DEPTH_DB,
    USABLE_NULL_DEPTH_DB,
    VERDICT_AXIS_LIMITED,
    VERDICT_ROBUST,
    VERDICT_WEAK,
    DelaySweepPlan,
    DelaySweepRefused,
    DelaySweepSeams,
    reverse_null_graph,
    rows_at_pose,
    run_delay_sweep,
    sweep_spec,
    sweep_verdict,
)
from jasper.audio_measurement.delay_graph import DelayGraphProofError, quantized_delay_ms
from jasper.audio_measurement.null_walk import (
    BoundedNullWalkSchedule,
    select_delay,
    select_scheduled_delay,
)

FC_HZ = 1800.0
TWEETER_CHANNELS = (1,)
WOOFER_CHANNELS = (0,)


def _live_graph():
    """A minimal stand-in for the applied crossover: two lanes, trims, a limiter."""

    return {
        "devices": {"volume_limit": 0.0, "samplerate": 48000},
        "filters": {
            "as_woofer_delay": {
                "type": "Delay",
                "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
            },
            "as_tweeter_delay": {
                "type": "Delay",
                "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
            },
            "as_woofer_baseline_gain": {
                "type": "Gain",
                "parameters": {"gain": -3.5, "inverted": False, "mute": False},
            },
            "as_tweeter_baseline_gain": {
                "type": "Gain",
                "parameters": {"gain": -6.0, "inverted": False, "mute": False},
            },
            "as_woofer_limiter": {
                "type": "Limiter",
                "parameters": {"clip_limit": -1.0, "soft_clip": True},
            },
        },
        "pipeline": [
            {"type": "Filter", "channels": [0],
             "names": ["as_woofer_delay", "as_woofer_baseline_gain",
                       "as_woofer_limiter"]},
            {"type": "Filter", "channels": [1],
             "names": ["as_tweeter_delay", "as_tweeter_baseline_gain"]},
        ],
    }


def _spec(seed_m=0.0):
    return sweep_spec(
        crossover_fc_hz=FC_HZ,
        upper_role="tweeter",
        lower_role="woofer",
        signed_acoustic_path_difference_m=seed_m,
    )


# --------------------------------------------------------------------------- #
# (a) the emitted graph
# --------------------------------------------------------------------------- #


def test_emitted_graph_carries_inversion_and_delay_and_nothing_else():
    live = _live_graph()
    patched = reverse_null_graph(
        live,
        inverted_role="tweeter",
        delay_role="tweeter",
        delay_us=250.0,
        delay_channels=TWEETER_CHANNELS,
    )

    tweeter_gain = patched["filters"]["as_tweeter_baseline_gain"]["parameters"]
    tweeter_delay = patched["filters"]["as_tweeter_delay"]["parameters"]
    assert tweeter_gain["inverted"] is True
    assert tweeter_gain["gain"] == -6.0  # the trim is carried, not re-solved
    assert tweeter_delay["delay"] == quantized_delay_ms(250.0)
    assert tweeter_delay["unit"] == "ms"

    # Everything that is not those two values is byte-identical to the live
    # graph, so "otherwise identical to the applied crossover and trims" is
    # structural rather than asserted.
    rebuilt = copy.deepcopy(patched)
    rebuilt["filters"]["as_tweeter_baseline_gain"]["parameters"]["inverted"] = False
    rebuilt["filters"]["as_tweeter_delay"]["parameters"]["delay"] = 0.0
    assert rebuilt == live


def test_emitted_graph_leaves_the_live_mapping_untouched():
    live = _live_graph()
    before = copy.deepcopy(live)
    reverse_null_graph(
        live, inverted_role="tweeter", delay_role="tweeter",
        delay_us=100.0, delay_channels=TWEETER_CHANNELS,
    )
    assert live == before


def test_zero_coordinate_inverts_without_delaying_either_branch():
    patched = reverse_null_graph(
        _live_graph(), inverted_role="tweeter", delay_role=None,
        delay_us=0.0, delay_channels=(),
    )
    assert patched["filters"]["as_tweeter_baseline_gain"]["parameters"]["inverted"]
    assert patched["filters"]["as_tweeter_delay"]["parameters"]["delay"] == 0.0
    assert patched["filters"]["as_woofer_delay"]["parameters"]["delay"] == 0.0


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"devices": {"volume_limit": 3.0}}, id="volume_limit_above_ceiling"),
        pytest.param({"pipeline": []}, id="delay_filter_unwired"),
    ],
)
def test_graph_proof_refuses_rather_than_emitting(mutation):
    live = _live_graph()
    live.update(copy.deepcopy(mutation))
    with pytest.raises(DelayGraphProofError):
        reverse_null_graph(
            live, inverted_role="tweeter", delay_role="tweeter",
            delay_us=250.0, delay_channels=TWEETER_CHANNELS,
        )


def test_graph_refuses_a_branch_the_live_graph_does_not_carry():
    with pytest.raises(DelaySweepRefused):
        reverse_null_graph(
            _live_graph(), inverted_role="midrange", delay_role="tweeter",
            delay_us=100.0, delay_channels=TWEETER_CHANNELS,
        )


def test_delay_lane_bound_to_the_wrong_channels_is_refused():
    with pytest.raises(DelayGraphProofError):
        reverse_null_graph(
            _live_graph(), inverted_role="tweeter", delay_role="tweeter",
            delay_us=250.0, delay_channels=WOOFER_CHANNELS,
        )


# --------------------------------------------------------------------------- #
# (b) the null-depth search recovers a known offset
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

    verdict = sweep_verdict(selection, rows_by_delay=rows)
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
    verdict = sweep_verdict(selection, rows_by_delay=rows)
    assert verdict["verdict"] == VERDICT_AXIS_LIMITED
    assert verdict["best_measured_null_depth_db"] < USABLE_NULL_DEPTH_DB
    assert verdict["meets_robustness_bar"] is False
    # Disclosed, never raised: the selected coordinate is still reported.
    assert verdict["selected_delay_us"] is not None


def test_a_shallow_but_usable_null_grades_weak():
    _, rows, selection = _graded(0.0, scale=0.45)
    verdict = sweep_verdict(selection, rows_by_delay=rows)
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
    verdict = sweep_verdict(selection, rows_by_delay=rows, poses_deg=(0, 15))
    assert verdict["poses_agree"] is False
    assert verdict["verdict"] == VERDICT_WEAK


def test_incomplete_evidence_is_refused_with_the_selectors_own_reason():
    spec = _spec()
    rows = {coordinate: _rows(30.0) for coordinate in spec.candidate_delays_us()}
    # One coordinate short of MIN_CAPTURE_COUNT is enough to refuse the walk.
    rows[spec.candidate_delays_us()[0]] = _rows(30.0, count=2)
    selection = select_delay(spec, rows)
    verdict = sweep_verdict(selection, rows_by_delay=rows)
    assert verdict["verdict"] == "evidence_incomplete"
    assert verdict["reason"] == selection["reason"]
    assert verdict["selected_delay_us"] is None


# --------------------------------------------------------------------------- #
# (c) (d) (e) the runner
# --------------------------------------------------------------------------- #


class _Recorder:
    """A sweep host that records what the runner did to the graph and to disk."""

    def __init__(self, *, true_offset_us=100.0, claim=None, fail_at=None):
        self.live = _live_graph()
        self.durable = copy.deepcopy(self.live)
        self.applied: list[dict] = []
        self.restores = 0
        self.claim = claim
        self.fail_at = fail_at
        self.true_offset_us = true_offset_us

    def seams(self):
        return DelaySweepSeams(
            read_live_graph=self._read,
            apply_graph=self._apply,
            restore_graph=self._restore,
            measure=self._measure,
            session_claim=(lambda: self.claim),
        )

    async def _read(self):
        return self.live

    async def _apply(self, graph):
        self.applied.append(graph)

    async def _restore(self):
        # A real put-back suspends (a writer lock, a reload, a liveness read).
        # The await is what makes this seam able to LOSE a cancellation, so it
        # is what puts `resilient_restore`'s shield under test rather than the
        # bare `finally`.
        await asyncio.sleep(0)
        self.restores += 1
        return {"restored": True}

    async def _measure(self, *, pose_deg, delay_us):
        if self.fail_at is not None and len(self.applied) >= self.fail_at:
            raise RuntimeError("capture exploded")
        applied = self.applied[-1]
        tweeter = applied["filters"]["as_tweeter_delay"]["parameters"]["delay"]
        woofer = applied["filters"]["as_woofer_delay"]["parameters"]["delay"]
        coordinate = (tweeter - woofer) * 1000.0
        return dict(
            _rows(_synthetic_depth(coordinate, self.true_offset_us))[0]["acoustic"]
        )


def _plan(**kwargs):
    return DelaySweepPlan(
        spec=_spec(),
        inverted_role="tweeter",
        role_channels={"tweeter": TWEETER_CHANNELS, "woofer": WOOFER_CHANNELS},
        **kwargs,
    )


def test_artifact_carries_every_step_the_best_delay_and_the_verdict():
    host = _Recorder(true_offset_us=100.0)
    artifact = asyncio.run(run_delay_sweep(_plan(), host.seams()))

    assert artifact["kind"] == "jts_inter_driver_delay_sweep"
    assert artifact["inverted_role"] == "tweeter"
    # One receipt per capture: every coordinate the walk measured, at every
    # pose, with the depth it read.
    assert len(artifact["steps"]) == len(host.applied) * 5
    for step in artifact["steps"]:
        assert {"relative_delay_us", "delay_target", "delay_us",
                "pose_deg", "null_depth_db"} <= set(step)
    assert artifact["selection"]["status"] == "selected"
    assert artifact["verdict"]["verdict"] == VERDICT_ROBUST
    assert abs(artifact["verdict"]["selected_relative_delay_us"] - 100.0) <= 100.0


def test_every_applied_graph_is_inverted_and_holds_the_ceiling():
    host = _Recorder()
    asyncio.run(run_delay_sweep(_plan(), host.seams()))
    assert host.applied
    for graph in host.applied:
        gain = graph["filters"]["as_tweeter_baseline_gain"]["parameters"]
        assert gain["inverted"] is True
        assert graph["devices"]["volume_limit"] == 0.0


def test_durable_config_is_byte_untouched_across_a_full_sweep():
    host = _Recorder()
    before = copy.deepcopy(host.durable)
    asyncio.run(run_delay_sweep(_plan(), host.seams()))
    # Nothing in the sweep writes a durable config: the runner is handed a read
    # seam and an apply seam, and the apply seam is the runtime-only one.
    assert host.durable == before
    assert host.live == before


def test_the_live_graph_is_restored_after_a_clean_sweep():
    host = _Recorder()
    asyncio.run(run_delay_sweep(_plan(), host.seams()))
    assert host.restores == 1


def test_the_live_graph_is_restored_when_a_capture_fails():
    host = _Recorder(fail_at=2)
    with pytest.raises(RuntimeError):
        asyncio.run(run_delay_sweep(_plan(), host.seams()))
    assert host.restores == 1


def test_the_live_graph_is_restored_when_the_sweep_is_cancelled():
    host = _Recorder()

    async def drive():
        task = asyncio.ensure_future(run_delay_sweep(_plan(), host.seams()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert host.restores == 1


def test_an_active_measurement_claim_refuses_before_the_graph_moves():
    host = _Recorder(claim="a room sweep holds the speaker")
    with pytest.raises(DelaySweepRefused) as excinfo:
        asyncio.run(run_delay_sweep(_plan(), host.seams()))
    assert excinfo.value.reason == REFUSE_MEASUREMENT_ACTIVE
    assert host.applied == []
    assert host.restores == 0


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
