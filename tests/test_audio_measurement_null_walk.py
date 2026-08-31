# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.audio_measurement.null_walk import (
    MAX_COARSE_CANDIDATES,
    MAX_SCHEDULED_CANDIDATES,
    BoundedNullWalkSchedule,
    DspPredecessor,
    NullWalkError,
    NullWalkSpec,
    geometry_seed_us,
    select_scheduled_delay,
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


def test_predecessor_fingerprint_is_derived_from_canonical_frozen_state():
    first = DspPredecessor(state={"path": "/entry.yml", "raw": {"b": 2, "a": 1}})
    second = DspPredecessor(state={"raw": {"a": 1, "b": 2}, "path": "/entry.yml"})

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_predecessor_state_access_cannot_mutate_the_frozen_rollback_anchor():
    predecessor = DspPredecessor(
        state={"path": "/entry.yml", "raw": {"filters": ["entry"]}}
    )
    state_copy = predecessor.state

    state_copy["raw"]["filters"].append("candidate")

    assert predecessor.state == {
        "path": "/entry.yml",
        "raw": {"filters": ["entry"]},
    }


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"raw": float("nan")},
        {"raw": object()},
        {1: "ambiguous-key"},
        {"raw": ("tuple-is-not-json",)},
    ],
)
def test_predecessor_requires_nonempty_canonical_json_state(state):
    with pytest.raises(NullWalkError, match="predecessor state"):
        DspPredecessor(state=state)


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


def test_350_hz_schedule_is_bounded_symmetric_deterministic_and_locally_refined():
    spec = _spec(fc=350.0, seed=37.5)

    assert spec.candidate_count == 29
    with pytest.raises(NullWalkError, match="candidate budget"):
        spec.candidate_delays_us()

    first_coarse = spec.coarse_candidate_delays_us()
    second_coarse = spec.coarse_candidate_delays_us()
    schedule = BoundedNullWalkSchedule(spec, refinement_anchor_us=37.5)

    assert first_coarse == second_coarse
    assert len(first_coarse) <= MAX_COARSE_CANDIDATES
    assert first_coarse[0] == spec.fine_grid_coordinate(spec.fine_grid_index_min)
    assert first_coarse[-1] == spec.fine_grid_coordinate(spec.fine_grid_index_max)
    assert 37.5 in first_coarse
    assert tuple(value - 37.5 for value in first_coarse) == pytest.approx(
        tuple(-(value - 37.5) for value in reversed(first_coarse))
    )
    assert schedule.refinement_delays_us == (-62.5, 137.5)
    assert len(schedule.scheduled_delays_us) <= MAX_SCHEDULED_CANDIDATES
    assert schedule.scheduled_delays_us == tuple(
        sorted({*first_coarse, *schedule.refinement_delays_us})
    )
    assert spec.dsp_candidate(-62.5).relative_delay_us == -62.5


def test_bounded_schedule_contract_holds_across_supported_grid_widths():
    for step_us, maximum_half_width_steps in ((100.0, 200), (50.0, 400)):
        for half_width_steps in range(maximum_half_width_steps + 1):
            crossover_fc_hz = (
                1_000_000.0 / step_us
                if half_width_steps == 0
                else 1_000_000.0 / (2.0 * half_width_steps * step_us)
            )
            spec = _spec(fc=crossover_fc_hz, step=step_us)
            coarse = spec.coarse_candidate_delays_us()

            assert spec.steps_each_side == half_width_steps
            assert coarse == spec.coarse_candidate_delays_us()
            assert len(coarse) <= MAX_COARSE_CANDIDATES
            assert coarse[0] == spec.fine_grid_coordinate(spec.fine_grid_index_min)
            assert coarse[-1] == spec.fine_grid_coordinate(spec.fine_grid_index_max)
            assert 0.0 in coarse
            assert coarse == tuple(-value for value in reversed(coarse))

            for anchor in coarse:
                schedule = BoundedNullWalkSchedule(
                    spec,
                    refinement_anchor_us=anchor,
                )
                anchor_index = spec.fine_grid_index(anchor)
                refinement_indexes = {
                    spec.fine_grid_index(value)
                    for value in schedule.refinement_delays_us
                }

                assert len(schedule.refinement_delays_us) <= 2
                assert len(schedule.scheduled_delays_us) <= MAX_SCHEDULED_CANDIDATES
                assert refinement_indexes <= {anchor_index - 1, anchor_index + 1}
                assert not set(schedule.refinement_delays_us) & set(coarse)
                assert schedule.scheduled_delays_us == tuple(
                    sorted({*coarse, *schedule.refinement_delays_us})
                )


def test_bounded_spec_and_schedule_have_strict_fingerprinted_roundtrips():
    spec = _spec(fc=350.0)
    schedule = BoundedNullWalkSchedule(spec, refinement_anchor_us=0.0)

    assert "candidate_delays_us" not in spec.to_dict()
    assert NullWalkSpec.from_mapping(spec.to_dict()) == spec
    assert (
        BoundedNullWalkSchedule.from_mapping(schedule.to_dict(), spec=spec) == schedule
    )
    assert len(spec.fingerprint) == 64
    assert len(schedule.fingerprint) == 64

    tampered_spec = spec.to_dict()
    tampered_spec["fingerprint"] = "0" * 64
    with pytest.raises(NullWalkError, match="exact canonical grid"):
        NullWalkSpec.from_mapping(tampered_spec)

    tampered_schedule = schedule.to_dict()
    tampered_schedule["scheduled_delays_us"] = list(
        reversed(tampered_schedule["scheduled_delays_us"])
    )
    with pytest.raises(NullWalkError, match="exact canonical schedule"):
        BoundedNullWalkSchedule.from_mapping(tampered_schedule, spec=spec)

    wrong_container = schedule.to_dict()
    wrong_container["coarse_delays_us"] = tuple(wrong_container["coarse_delays_us"])
    with pytest.raises(NullWalkError, match="coordinate fields must be lists"):
        BoundedNullWalkSchedule.from_mapping(wrong_container, spec=spec)


def test_refinement_anchor_must_be_an_explicit_coarse_coordinate():
    spec = _spec(fc=350.0)

    # 100 us is a fine coordinate, but the 350 Hz first phase uses a 200 us
    # coarse stride. The host must persist and name the coarse winner before
    # Shared admits its immediate fine neighbours.
    with pytest.raises(NullWalkError, match="exact coarse schedule coordinate"):
        BoundedNullWalkSchedule(spec, refinement_anchor_us=100.0)
    with pytest.raises(NullWalkError, match="numeric"):
        BoundedNullWalkSchedule(spec, refinement_anchor_us=None)


def test_refinement_schedule_selects_deepest_complete_repeatable_coarse_anchor():
    spec = _spec(fc=350.0)
    evidence = {
        coordinate: [_capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)] * 5
        for coordinate in spec.coarse_candidate_delays_us()
    }
    tied = BoundedNullWalkSchedule.from_coarse_evidence(spec, evidence)
    assert tied.refinement_anchor_us == 0.0

    evidence[400.0] = [_capture(30.0, crossover_fc_hz=spec.crossover_fc_hz)] * 5

    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, evidence)

    assert schedule.refinement_anchor_us == 400.0
    assert schedule.refinement_delays_us == (300.0, 500.0)

    missing = dict(evidence)
    missing.pop(spec.coarse_candidate_delays_us()[0])
    with pytest.raises(NullWalkError, match="exact coarse schedule"):
        BoundedNullWalkSchedule.from_coarse_evidence(spec, missing)

    unrepeatable = dict(evidence)
    unrepeatable[400.0] = [
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz),
        _capture(23.0, crossover_fc_hz=spec.crossover_fc_hz),
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz),
        _capture(23.0, crossover_fc_hz=spec.crossover_fc_hz),
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz),
    ]
    with pytest.raises(NullWalkError, match="complete repeatable evidence"):
        BoundedNullWalkSchedule.from_coarse_evidence(spec, unrepeatable)


@pytest.mark.parametrize("relative_delay_us", [50.0, 1500.0, True])
def test_nonallocating_fine_grid_membership_refuses_offgrid_or_out_of_bounds(
    relative_delay_us,
):
    spec = _spec(fc=350.0)

    with pytest.raises(NullWalkError):
        spec.dsp_candidate(relative_delay_us)


def test_low_frequency_schedule_reaches_aligned_dsp_bounds_and_fails_beyond_them():
    exactly_bounded = _spec(fc=25.0)
    coarse = exactly_bounded.coarse_candidate_delays_us()
    schedule = BoundedNullWalkSchedule(
        exactly_bounded,
        refinement_anchor_us=0.0,
    )

    assert coarse[0] == -20_000.0
    assert coarse[-1] == 20_000.0
    assert len(coarse) <= MAX_COARSE_CANDIDATES
    assert len(schedule.scheduled_delays_us) <= MAX_SCHEDULED_CANDIDATES

    beyond_dsp = _spec(fc=24.0)
    with pytest.raises(NullWalkError, match="20 ms delay ceiling"):
        beyond_dsp.coarse_candidate_delays_us()

    epsilon_over = _spec(fc=500_000.0, seed=20_000.000001)
    with pytest.raises(NullWalkError, match="20 ms delay ceiling"):
        epsilon_over.coarse_candidate_delays_us()


def test_scheduled_selection_reuses_plateau_policy_without_relaxing_grid_cap():
    spec = _spec(fc=350.0)
    coarse = {
        coordinate: [
            _capture(10.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in spec.coarse_candidate_delays_us()
    }
    coarse[400.0] = [
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
    ] * 5
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, coarse)
    evidence = dict(coarse)
    evidence[300.0] = [
        _capture(value, crossover_fc_hz=spec.crossover_fc_hz)
        for value in (20.0, 20.2, 20.1, 20.0, 20.1)
    ]
    evidence[500.0] = [
        _capture(value, crossover_fc_hz=spec.crossover_fc_hz)
        for value in (15.0, 15.1, 15.0, 15.1, 15.0)
    ]

    result = select_scheduled_delay(spec, schedule, evidence)

    assert spec.candidate_count == 29
    assert result["status"] == "selected"
    assert result["selected_relative_delay_us"] == 300.0
    assert result["indistinguishable_delays_us"] == [300.0, 400.0]
    assert result["schedule"] == schedule.to_dict()
    assert [row["relative_delay_us"] for row in result["candidates"]] == list(
        schedule.scheduled_delays_us
    )


def test_scheduled_selection_requires_exact_schedule_coverage():
    spec = _spec(fc=350.0)
    coarse = {
        coordinate: [
            _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in spec.coarse_candidate_delays_us()
    }
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, coarse)
    evidence = {
        coordinate: [
            _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in schedule.scheduled_delays_us
    }

    missing = dict(evidence)
    missing.pop(schedule.scheduled_delays_us[0])
    with pytest.raises(NullWalkError, match="cover the exact schedule"):
        select_scheduled_delay(spec, schedule, missing)

    outside = dict(evidence)
    outside[300.0] = [
        _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
    ] * 5
    with pytest.raises(NullWalkError, match="outside the exact schedule"):
        select_scheduled_delay(spec, schedule, outside)


def test_scheduled_selection_refuses_incomplete_or_unrepeatable_coordinates():
    spec = _spec(fc=350.0)
    coarse = {
        coordinate: [
            _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in spec.coarse_candidate_delays_us()
    }
    schedule = BoundedNullWalkSchedule.from_coarse_evidence(spec, coarse)
    evidence = {
        coordinate: [
            _capture(20.0, crossover_fc_hz=spec.crossover_fc_hz)
        ]
        * 5
        for coordinate in schedule.scheduled_delays_us
    }

    refinement = schedule.refinement_delays_us[0]
    incomplete = dict(evidence)
    incomplete[refinement] = incomplete[refinement][:-1]
    assert select_scheduled_delay(spec, schedule, incomplete)["reason"] == (
        "candidate_evidence_incomplete"
    )

    unrepeatable = dict(evidence)
    unrepeatable[refinement] = [
        _capture(value, crossover_fc_hz=spec.crossover_fc_hz)
        for value in (20.0, 23.0, 20.0, 23.0, 20.0)
    ]
    assert select_scheduled_delay(spec, schedule, unrepeatable)["reason"] == (
        "candidate_repeatability_failed"
    )


def test_scheduled_selection_rejects_a_schedule_for_another_spec():
    spec = _spec(fc=350.0)
    other = _spec(fc=300.0)
    schedule = BoundedNullWalkSchedule(
        other,
        refinement_anchor_us=0.0,
    )

    with pytest.raises(NullWalkError, match="different null-walk spec"):
        select_scheduled_delay(spec, schedule, {})


def test_scheduled_selection_rederives_refinement_from_coarse_evidence():
    spec = _spec(fc=350.0)
    wrong_schedule = BoundedNullWalkSchedule(
        spec,
        refinement_anchor_us=spec.coarse_candidate_delays_us()[0],
    )
    evidence = {
        coordinate: [
            _capture(
                30.0 if coordinate == 400.0 else 20.0,
                crossover_fc_hz=spec.crossover_fc_hz,
            )
        ]
        * 5
        for coordinate in wrong_schedule.scheduled_delays_us
    }

    with pytest.raises(NullWalkError, match="does not match its coarse evidence"):
        select_scheduled_delay(spec, wrong_schedule, evidence)
