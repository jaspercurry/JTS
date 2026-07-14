# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.excitation_safety_plan import (
    DriverSweepGeneratorPlan,
    ExcitationSafetyPlanError,
    ExcitationSafetyPlanRefusal,
    PreparedDriverExcitationPlan,
    RequestedDriverExcitationPlan,
    prepare_driver_excitation_plan,
)
from jasper.active_speaker.measurement import active_driver_targets
from tests.active_speaker_fixtures import mono_output_topology


def _profile_and_targets(*, cooldown_s: float = 1):
    topology = mono_output_topology()
    common = {
        "hard_excitation_band_hz": [500, 20_000],
        "measurement_band_hz": [500, 10_000],
        "crossover_search_band_hz": [1500, 2500],
        "level_duration_limits": {
            "max_effective_peak_dbfs": -65,
            "max_sweep_duration_s": 4,
            "max_repeat_count": 3,
            "minimum_cooldown_s": cooldown_s,
        },
    }
    settings = {
        "drivers": [
            {
                **common,
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "Example W6",
                "required_protection_filters": [
                    {
                        "kind": "lowpass",
                        "cutoff_hz": 3000,
                        "minimum_slope_db_per_octave": 24,
                    }
                ],
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 132,
                    "baffle_width_mm": 210,
                },
            },
            {
                **common,
                "target_id": "mono:tweeter",
                "role": "tweeter",
                "model": "Example T1",
                "required_protection_filters": [
                    {
                        "kind": "highpass",
                        "cutoff_hz": 5000,
                        "minimum_slope_db_per_octave": 24,
                    }
                ],
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 25,
                },
            },
        ],
        "crossover_candidates": [],
    }
    profile = build_driver_safety_profile(
        topology,
        manual_settings=settings,
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )
    targets = {target["role"]: target for target in active_driver_targets(topology)}
    return topology, profile, targets


def _requested(target_fingerprint: str, **overrides):
    values = {
        "f1_hz": 1000,
        "f2_hz": 4000,
        "amplitude": 0.1,
        "duration_s": 4,
        "repeat_count": 3,
        "commissioning_gain_db": -50,
        "main_volume_db": 0,
    }
    values.update(overrides)
    return RequestedDriverExcitationPlan(
        target_fingerprint=target_fingerprint,
        commissioning_context_fingerprint="a" * 64,
        generator=DriverSweepGeneratorPlan(**values),
    )


def test_safety_plan_derives_closed_request_for_shared_admission():
    topology, profile, targets = _profile_and_targets()
    requested = _requested(targets["woofer"]["target_fingerprint"])
    prepared = prepare_driver_excitation_plan(topology, profile, requested)
    assert prepared.target_id == "mono:woofer"
    assert prepared.target_role == "woofer"
    assert prepared.execution_allowed is True
    assert prepared.request.band == requested.generator.band
    assert prepared.request.effective_peak_dbfs == pytest.approx(-70)
    assert prepared.minimum_cooldown_s == 1
    assert prepared.refusals == ()
    assert prepared.to_dict()["accepts_protection_evidence"] is True
    with pytest.raises(TypeError, match="prepare_driver_excitation_plan"):
        PreparedDriverExcitationPlan()


def test_outside_limits_remains_blocked():
    topology, profile, targets = _profile_and_targets()
    requested = _requested(
        targets["tweeter"]["target_fingerprint"],
        duration_s=5,
    )
    prepared = prepare_driver_excitation_plan(topology, profile, requested)
    assert prepared.execution_allowed is False
    assert prepared.refusals == (
        ExcitationSafetyPlanRefusal.REQUEST_OUTSIDE_LIMITS,
    )


def test_closed_generator_rejects_positive_gain():
    _topology, _profile, targets = _profile_and_targets()
    with pytest.raises(ExcitationSafetyPlanError, match="non-positive"):
        _requested(
            targets["woofer"]["target_fingerprint"],
            commissioning_gain_db=1,
        )


def test_safety_plan_refuses_unconfirmed_profile():
    topology, profile, targets = _profile_and_targets()
    unconfirmed = dict(profile)
    unconfirmed["status"] = "needs_confirmation"
    unconfirmed["confirmation"] = None
    with pytest.raises(
        ExcitationSafetyPlanError,
        match=ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value,
    ):
        prepare_driver_excitation_plan(
            topology,
            unconfirmed,
            _requested(targets["woofer"]["target_fingerprint"]),
        )
