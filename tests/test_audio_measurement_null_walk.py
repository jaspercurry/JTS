# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.audio_measurement.null_walk import (
    DspPredecessor,
    NullWalkError,
    NullWalkSpec,
    geometry_seed_us,
)


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


def _fine_grid(spec):
    return tuple(
        spec.fine_grid_coordinate(index)
        for index in range(spec.fine_grid_index_min, spec.fine_grid_index_max + 1)
    )


def test_geometry_bound_is_half_one_crossover_period_and_grid_contains_seed():
    spec = _spec(fc=1600.0, seed=250.0)

    assert spec.half_period_us == pytest.approx(312.5)
    assert spec.lower_bound_us == pytest.approx(-62.5)
    assert spec.upper_bound_us == pytest.approx(562.5)
    assert _fine_grid(spec) == (
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


def test_walk_refuses_before_dsp_when_any_candidate_exceeds_delay_ceiling():
    spec = _spec(fc=5000.0, seed=20_000.0)

    with pytest.raises(NullWalkError, match="20 ms delay ceiling"):
        _fine_grid(spec)


def test_divisible_half_period_includes_bounds_and_fragment_does_not():
    divisible = _spec(fc=5000.0)
    fragment = _spec(fc=4000.0)

    assert _fine_grid(divisible) == (-100.0, 0.0, 100.0)
    assert fragment.lower_bound_us == -125.0
    assert fragment.upper_bound_us == 125.0
    assert _fine_grid(fragment) == (-100.0, 0.0, 100.0)


def test_bounded_spec_has_strict_fingerprinted_roundtrip():
    spec = _spec(fc=350.0)

    assert "candidate_delays_us" not in spec.to_dict()
    assert NullWalkSpec.from_mapping(spec.to_dict()) == spec
    assert len(spec.fingerprint) == 64

    tampered_spec = spec.to_dict()
    tampered_spec["fingerprint"] = "0" * 64
    with pytest.raises(NullWalkError, match="exact canonical grid"):
        NullWalkSpec.from_mapping(tampered_spec)


@pytest.mark.parametrize("relative_delay_us", [50.0, 1500.0, True])
def test_nonallocating_fine_grid_membership_refuses_offgrid_or_out_of_bounds(
    relative_delay_us,
):
    spec = _spec(fc=350.0)

    with pytest.raises(NullWalkError):
        spec.dsp_candidate(relative_delay_us)
