# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.audio_measurement.null_walk import (
    NullWalkError,
    NullWalkSpec,
    geometry_seed_us,
    run_null_walk,
    select_delay,
    summarize_candidate,
)


def _capture(depth: float, **acoustic_overrides):
    acoustic = {
        "null_depth_db": depth,
        "null_depth_capped": False,
        "mic_clipping": False,
        "calibrated": True,
        "expect_null": True,
        "crossover_fc_hz": 5000.0,
        "gating": {"applied": True},
        "above_validity_floor": True,
        "snr": {"decision_class": "alignment", "verdict": "ok"},
        # Deliberately present and absurd: arrival timing is never an input to
        # the selected delay.
        "ir_arrival_us": 999_999.0,
    }
    acoustic.update(acoustic_overrides)
    return {"acoustic": acoustic}


def _spec(*, fc=5000.0, seed=0.0, step=100.0):
    return NullWalkSpec(
        crossover_fc_hz=fc,
        geometry_seed_us=seed,
        positive_delay_target="upper",
        negative_delay_target="lower",
        step_us=step,
    )


def test_geometry_bound_is_half_one_crossover_period_and_grid_contains_seed():
    spec = _spec(fc=1600.0, seed=250.0)

    assert spec.half_period_us == pytest.approx(312.5)
    assert spec.lower_bound_us == pytest.approx(-62.5)
    assert spec.upper_bound_us == pytest.approx(562.5)
    assert spec.candidate_delays_us() == (
        -50.0,
        50.0,
        150.0,
        250.0,
        350.0,
        450.0,
        550.0,
    )


def test_geometry_seed_is_only_the_path_plus_known_transport_bound_center():
    assert geometry_seed_us(0.343) == pytest.approx(1000.0)
    assert geometry_seed_us(
        -0.1715,
        signed_transport_difference_us=2500.0,
    ) == pytest.approx(2000.0)


def test_signed_grid_coordinates_map_to_non_negative_targeted_dsp_delays():
    spec = _spec()

    negative = spec.dsp_candidate(-100.0)
    zero = spec.dsp_candidate(0.0)
    positive = spec.dsp_candidate(100.0)

    assert (negative.delay_target, negative.delay_us) == ("lower", 100.0)
    assert (zero.delay_target, zero.delay_us) == (None, 0.0)
    assert (positive.delay_target, positive.delay_us) == ("upper", 100.0)
    assert negative.positive_delay_target == "upper"
    assert negative.negative_delay_target == "lower"


@pytest.mark.parametrize("step", [49.9, 100.1])
def test_step_must_stay_inside_the_pinned_50_to_100_microsecond_range(step):
    with pytest.raises(NullWalkError, match="step_us"):
        _spec(fc=2000.0, step=step)


def test_candidate_requires_five_good_gated_alignment_snr_captures():
    four = [_capture(20.0), _capture(20.2), _capture(19.9), _capture(20.1)]
    out = summarize_candidate(_spec(), 0.0, four)
    assert out["repeatable"] is False
    assert {issue["code"] for issue in out["issues"]} == {"captures_missing"}

    bad_snr = four + [
        _capture(20.0, snr={"decision_class": "alignment", "verdict": "reduced"})
    ]
    out = summarize_candidate(_spec(), 0.0, bad_snr)
    assert out["repeatable"] is False
    assert "alignment_snr_insufficient" in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"gating": {"applied": False}}, "gated_null_required"),
        (
            {"above_validity_floor": False},
            "below_validity_floor",
        ),
        ({"mic_clipping": True}, "clipping"),
        ({"null_depth_capped": True}, "null_depth_capped"),
    ],
)
def test_candidate_rejects_each_capture_quality_failure(override, code):
    captures = [_capture(20.0) for _ in range(5)]
    captures[-1] = _capture(20.0, **override)
    out = summarize_candidate(_spec(), 0.0, captures)
    assert out["repeatable"] is False
    assert code in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize("floor_value", [None, False])
def test_candidate_requires_canonical_top_level_validity_floor_true(floor_value):
    captures = [_capture(20.0) for _ in range(5)]
    captures[-1]["acoustic"]["above_validity_floor"] = floor_value

    out = summarize_candidate(_spec(), 0.0, captures)

    assert out["repeatable"] is False
    assert "below_validity_floor" in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"calibrated": False}, "calibrated_mic_required"),
        ({"expect_null": False}, "reverse_null_required"),
        ({"crossover_fc_hz": 999.0}, "crossover_region_mismatch"),
        ({"crossover_fc_hz": None}, "crossover_region_mismatch"),
    ],
)
def test_candidate_requires_calibrated_reverse_null_for_the_spec_region(
    override,
    code,
):
    captures = [_capture(20.0) for _ in range(5)]
    captures[-1] = _capture(20.0, **override)

    out = summarize_candidate(_spec(), 0.0, captures)

    assert out["repeatable"] is False
    assert code in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("calibrated", "calibrated_mic_required"),
        ("expect_null", "reverse_null_required"),
        ("crossover_fc_hz", "crossover_region_mismatch"),
    ],
)
def test_candidate_refuses_missing_reverse_null_identity(field, code):
    captures = [_capture(20.0) for _ in range(5)]
    captures[-1]["acoustic"].pop(field)

    out = summarize_candidate(_spec(), 0.0, captures)

    assert out["repeatable"] is False
    assert code in {issue["code"] for issue in out["issues"]}


@pytest.mark.parametrize("last_depth", [22.0, 22.1])
def test_candidate_refuses_two_db_or_greater_null_depth_spread(last_depth):
    out = summarize_candidate(
        _spec(),
        100.0,
        [
            _capture(20.0),
            _capture(20.2),
            _capture(20.0),
            _capture(20.1),
            _capture(last_depth),
        ],
    )
    assert out["spread_db"] >= 2.0
    assert out["repeatable"] is False
    assert "repeatability_low" in {issue["code"] for issue in out["issues"]}


def test_selection_uses_only_dsp_candidate_and_deepest_repeatable_null():
    spec = _spec()
    evidence = {
        -100.0: [_capture(v) for v in (15.0, 15.2, 14.9, 15.1, 15.0)],
        0.0: [_capture(v) for v in (19.8, 20.0, 20.1, 20.0, 19.9)],
        100.0: [_capture(v) for v in (17.0, 17.1, 16.9, 17.0, 17.2)],
    }

    out = select_delay(spec, evidence)

    assert out["status"] == "selected"
    assert out["selected_relative_delay_us"] == 0.0
    assert out["selected_delay_us"] == 0.0
    assert out["selected_delay_target"] is None
    assert out["selected_null_depth_db"] == pytest.approx(20.0)
    assert out["selected_delay_us"] != 999_999.0
    candidates = {row["relative_delay_us"]: row for row in out["candidates"]}
    assert candidates[-100.0]["delay_target"] == "lower"
    assert candidates[-100.0]["delay_us"] == 100.0
    assert candidates[100.0]["delay_target"] == "upper"
    assert candidates[100.0]["delay_us"] == 100.0


def test_selection_refuses_when_every_candidate_fails_repeatability():
    spec = _spec()
    evidence = {
        -100.0: [_capture(10.0)] * 5,
        0.0: [_capture(v) for v in (10.0, 12.0, 8.0, 11.0, 9.0)],
        100.0: [_capture(10.0)] * 5,
    }

    out = select_delay(spec, evidence)

    assert out["status"] == "refused"
    assert out["reason"] == "candidate_repeatability_failed"
    assert out["selected_delay_us"] is None


def test_selection_refuses_a_hole_in_the_declared_exhaustive_grid():
    spec = _spec()
    evidence = {0.0: [_capture(20.0)] * 5}

    out = select_delay(spec, evidence)

    assert out["status"] == "refused"
    assert out["reason"] == "candidate_evidence_incomplete"
    assert out["selected_delay_target"] is None


def test_selection_refuses_complete_grid_of_wrong_capture_kind():
    spec = _spec()
    wrong = _capture(
        20.0,
        calibrated=False,
        expect_null=False,
        crossover_fc_hz=999.0,
    )
    evidence = {candidate: [wrong] * 5 for candidate in spec.candidate_delays_us()}

    out = select_delay(spec, evidence)

    assert out["status"] == "refused"
    assert out["reason"] == "candidate_evidence_incomplete"
    assert out["selected_relative_delay_us"] is None


def test_selection_prefers_geometry_seed_inside_an_unresolvable_null_plateau():
    spec = _spec()
    evidence = {
        -100.0: [_capture(v) for v in (20.0, 20.2, 20.1, 20.0, 20.1)],
        0.0: [_capture(v) for v in (19.9, 20.1, 20.0, 20.0, 20.1)],
        100.0: [_capture(v) for v in (20.0, 20.2, 20.1, 20.0, 20.1)],
    }

    out = select_delay(spec, evidence)

    assert out["selected_delay_us"] == 0.0
    assert out["indistinguishable_delays_us"] == [-100.0, 0.0, 100.0]


def test_plateau_comparison_uses_both_candidates_measurement_spread():
    spec = _spec()
    evidence = {
        -100.0: [_capture(v) for v in (19.6, 19.8, 20.5, 21.0, 21.4)],
        0.0: [_capture(v) for v in (19.95, 20.0, 20.0, 20.0, 20.05)],
        100.0: [_capture(v) for v in (18.0, 18.1, 18.0, 18.1, 18.0)],
    }

    out = select_delay(spec, evidence)

    assert out["best_measured_null_depth_db"] == pytest.approx(20.5)
    assert out["selected_relative_delay_us"] == 0.0
    assert out["indistinguishable_delays_us"] == [-100.0, 0.0]


def test_evidence_outside_candidate_grid_is_refused():
    spec = _spec()
    with pytest.raises(NullWalkError, match="bounded candidate grid"):
        select_delay(spec, {25.0: [_capture(20.0)] * 5})


@pytest.mark.asyncio
async def test_runner_applies_each_exact_candidate_and_always_restores():
    spec = _spec()
    applied = []
    restored = []

    async def apply_candidate(candidate):
        applied.append(candidate.relative_delay_us)

    async def capture(candidate, index):
        return _capture(20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01)

    out = await run_null_walk(
        spec,
        apply_candidate=apply_candidate,
        capture_null=capture,
        restore=lambda: restored.append(True),
    )

    assert applied == list(spec.candidate_delays_us())
    assert restored == [True]
    assert out["selected_delay_us"] == 0.0


@pytest.mark.asyncio
async def test_runner_restores_when_capture_fails():
    spec = _spec()
    restored = []

    async def fail(_delay, _index):
        raise RuntimeError("relay lost")

    with pytest.raises(RuntimeError, match="relay lost"):
        await run_null_walk(
            spec,
            apply_candidate=lambda _delay: None,
            capture_null=fail,
            restore=lambda: restored.append(True),
        )

    assert restored == [True]


@pytest.mark.asyncio
async def test_runner_rejects_apply_callback_explicit_failure_and_restores():
    restored = []
    with pytest.raises(NullWalkError, match="apply_candidate reported failure"):
        await run_null_walk(
            _spec(),
            apply_candidate=lambda _candidate: False,
            capture_null=lambda _candidate, _index: _capture(20.0),
            restore=lambda: restored.append(True),
        )
    assert restored == [True]


@pytest.mark.asyncio
async def test_runner_rejects_restore_callback_explicit_failure():
    with pytest.raises(NullWalkError, match="restore reported failure"):
        await run_null_walk(
            _spec(),
            apply_candidate=lambda _candidate: None,
            capture_null=lambda candidate, index: _capture(
                20.0 - abs(candidate.relative_delay_us) / 100.0 + index * 0.01
            ),
            restore=lambda: False,
        )


@pytest.mark.asyncio
async def test_runner_reports_capture_and_restore_failures_together():
    def fail_capture(_candidate, _index):
        raise RuntimeError("relay lost")

    def fail_restore():
        raise RuntimeError("restore lost")

    with pytest.raises(BaseExceptionGroup) as caught:
        await run_null_walk(
            _spec(),
            apply_candidate=lambda _candidate: None,
            capture_null=fail_capture,
            restore=fail_restore,
        )
    assert [str(exc) for exc in caught.value.exceptions] == [
        "relay lost",
        "restore lost",
    ]


@pytest.mark.asyncio
async def test_runner_refuses_unbounded_low_frequency_exhaustive_walk_before_dsp():
    spec = _spec(fc=80.0)
    applied = []
    with pytest.raises(NullWalkError, match="candidate budget"):
        await run_null_walk(
            spec,
            apply_candidate=lambda candidate: applied.append(candidate),
            capture_null=lambda _delay, _index: _capture(20.0),
            restore=lambda: None,
        )
    assert applied == []


def test_candidate_budget_is_preflighted_arithmetically_at_exact_boundary():
    exact_25 = _spec(fc=400.0)
    refused_27 = _spec(fc=370.0)

    assert exact_25.candidate_count == 25
    assert len(exact_25.candidate_delays_us()) == 25
    assert refused_27.candidate_count == 27
    with pytest.raises(NullWalkError, match="candidate budget"):
        refused_27.candidate_delays_us()


def test_walk_refuses_before_dsp_when_any_candidate_exceeds_delay_ceiling():
    spec = _spec(fc=5000.0, seed=20_000.0)

    with pytest.raises(NullWalkError, match="20 ms delay ceiling"):
        spec.candidate_delays_us()


def test_divisible_half_period_includes_bounds_and_fragment_does_not():
    divisible = _spec(fc=5000.0)
    fragment = _spec(fc=4000.0)

    assert divisible.candidate_delays_us() == (-100.0, 0.0, 100.0)
    assert fragment.lower_bound_us == -125.0
    assert fragment.upper_bound_us == 125.0
    assert fragment.candidate_delays_us() == (-100.0, 0.0, 100.0)
