# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
)

TARGET = "1" * 64
REQUIREMENT = "3" * 64
PLAN = "4" * 64
OTHER_TARGET = "a" * 64


def _limits(**changes: object) -> ExcitationLimits:
    values: dict[str, object] = {
        "permitted_band": FrequencyBand(500, 10_000),
        "maximum_effective_peak_dbfs": -12,
        "maximum_duration_s": 8,
        "maximum_repeat_count": 3,
        "target_fingerprint": TARGET,
        "protection_requirement_fingerprint": REQUIREMENT,
        "excitation_plan_fingerprint": PLAN,
    }
    values.update(changes)
    return ExcitationLimits(**values)  # type: ignore[arg-type]


def _request(
    *,
    limits: ExcitationLimits | None = None,
    **changes: object,
) -> ExcitationRequest:
    authority = limits or _limits()
    values: dict[str, object] = {
        "band": FrequencyBand(1_000, 8_000),
        "effective_peak_dbfs": -18,
        "duration_s": 4,
        "repeat_count": 2,
        "target_fingerprint": authority.target_fingerprint,
        "authority_fingerprint": authority.fingerprint,
        "excitation_plan_fingerprint": authority.excitation_plan_fingerprint,
    }
    values.update(changes)
    return ExcitationRequest(**values)  # type: ignore[arg-type]


def test_closed_band_and_request_are_canonical() -> None:
    limits = _limits()
    request = _request(
        limits=limits,
        band=FrequencyBand(500, 10_000),
        effective_peak_dbfs=-12,
        duration_s=8,
        repeat_count=3,
    )

    assert request.band.is_subset_of(limits.permitted_band)
    assert request.band.lower_hz == 500.0
    assert request.effective_peak_dbfs == -12.0
    assert request.authority_fingerprint == limits.fingerprint
    assert request.excitation_plan_fingerprint == PLAN
    assert request.fingerprint == request.to_dict()["fingerprint"]


def test_zero_width_tone_uses_the_same_closed_band_contract() -> None:
    request = _request(band=FrequencyBand(2_000, 2_000))

    assert request.band.is_subset_of(_limits().permitted_band)


def test_broadened_limits_with_same_target_and_profile_are_not_same_authority() -> None:
    original = _limits()
    broadened = _limits(
        permitted_band=FrequencyBand(100, 20_000),
        maximum_effective_peak_dbfs=-6,
        maximum_duration_s=30,
        maximum_repeat_count=10,
    )
    assert broadened.target_fingerprint == original.target_fingerprint
    assert broadened.fingerprint != original.fingerprint
    with pytest.raises(ValueError, match="expected fingerprint"):
        ExcitationLimits.from_dict(
            broadened.to_dict(),
            expected_fingerprint=original.fingerprint,
        )


@pytest.mark.parametrize(
    ("factory", "match"),
    (
        (lambda: FrequencyBand(0, 1_000), "positive"),
        (lambda: FrequencyBand(2_000, 1_000), "must not exceed"),
        (lambda: FrequencyBand(float("nan"), 1_000), "finite"),
        (lambda: FrequencyBand(10**400, 10**400), "finite"),
        (lambda: _request(effective_peak_dbfs="-12"), "finite"),
        (lambda: _request(duration_s=float("inf")), "finite"),
        (lambda: _request(repeat_count=True), "positive integer"),
        (lambda: _request(target_fingerprint=7), "canonical lowercase"),
        (lambda: _request(target_fingerprint=f" {TARGET}"), "canonical lowercase"),
        (
            lambda: _request(target_fingerprint=OTHER_TARGET.upper()),
            "canonical lowercase",
        ),
        (
            lambda: _limits(maximum_effective_peak_dbfs=0.1),
            "must not exceed 0 dBFS",
        ),
        (lambda: _limits(maximum_repeat_count=2.0), "positive integer"),
        (
            lambda: _limits(protection_requirement_fingerprint=""),
            "canonical lowercase",
        ),
    ),
)
def test_malformed_inputs_never_reach_admission(factory, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        factory()


def test_schema_versioned_artifacts_round_trip_through_json() -> None:
    limits = _limits()
    request = _request(limits=limits)

    request_wire = json.loads(json.dumps(request.to_dict()))
    limits_wire = json.loads(json.dumps(limits.to_dict()))

    assert (
        ExcitationRequest.from_dict(
            request_wire,
            expected_fingerprint=request.fingerprint,
        )
        == request
    )
    assert (
        ExcitationLimits.from_dict(
            limits_wire,
            expected_fingerprint=limits.fingerprint,
        )
        == limits
    )

    for artifact in (request_wire, limits_wire):
        assert artifact["schema_version"] == 2
        assert len(artifact["fingerprint"]) == 64


def test_serialized_numeric_authority_is_canonical_and_tamper_evident() -> None:
    integer_inputs = _limits(
        maximum_effective_peak_dbfs=-12,
        maximum_duration_s=8,
    )
    float_inputs = _limits(
        maximum_effective_peak_dbfs=-12.0,
        maximum_duration_s=8.0,
    )
    assert integer_inputs.fingerprint == float_inputs.fingerprint
    assert integer_inputs.to_dict() == float_inputs.to_dict()

    tampered = integer_inputs.to_dict()
    tampered["maximum_duration_s"] = 9.0
    with pytest.raises(ValueError, match="fingerprint does not match"):
        ExcitationLimits.from_dict(tampered)

    wrong_schema = integer_inputs.to_dict()
    wrong_schema["schema_version"] = 1
    with pytest.raises(ValueError, match="fingerprint does not match"):
        ExcitationLimits.from_dict(wrong_schema)


def test_contract_values_are_immutable() -> None:
    request = _request()
    limits = _limits()

    for value, field, replacement in (
        (request, "duration_s", 99.0),
        (limits, "maximum_repeat_count", 99),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
