# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jasper.audio_measurement.excitation_admission import (
    ExcitationAdmission,
    ExcitationLimits,
    ExcitationRefusalReason,
    ExcitationRequest,
    FrequencyBand,
    ProtectionEvidence,
    admit_excitation,
)

TARGET = "target-sha256"
PROFILE = "profile-sha256"
REQUIREMENT = "required-protection-sha256"
PROOF = "live-readback-sha256"


def _limits(**changes: object) -> ExcitationLimits:
    values: dict[str, object] = {
        "permitted_band": FrequencyBand(500, 10_000),
        "maximum_effective_peak_dbfs": -12,
        "maximum_duration_s": 8,
        "maximum_repeat_count": 3,
        "target_fingerprint": TARGET,
        "safety_profile_fingerprint": PROFILE,
        "protection_requirement_fingerprint": REQUIREMENT,
    }
    values.update(changes)
    return ExcitationLimits(**values)  # type: ignore[arg-type]


def _request(**changes: object) -> ExcitationRequest:
    values: dict[str, object] = {
        "band": FrequencyBand(1_000, 8_000),
        "effective_peak_dbfs": -18,
        "duration_s": 4,
        "repeat_count": 2,
        "target_fingerprint": TARGET,
        "safety_profile_fingerprint": PROFILE,
    }
    values.update(changes)
    return ExcitationRequest(**values)  # type: ignore[arg-type]


def _evidence(**changes: object) -> ProtectionEvidence:
    values: dict[str, object] = {
        "target_fingerprint": TARGET,
        "safety_profile_fingerprint": PROFILE,
        "protection_requirement_fingerprint": REQUIREMENT,
        "evidence_fingerprint": PROOF,
        "current": True,
    }
    values.update(changes)
    return ProtectionEvidence(**values)  # type: ignore[arg-type]


def _decide(
    *,
    request: ExcitationRequest | None = None,
    limits: ExcitationLimits | None = None,
    evidence: ProtectionEvidence | None = None,
) -> ExcitationAdmission:
    return admit_excitation(
        request or _request(),
        limits or _limits(),
        protection_evidence=_evidence() if evidence is None else evidence,
    )


def test_exact_closed_boundaries_are_allowed_and_inputs_are_normalized() -> None:
    limits = _limits()
    request = _request(
        band=FrequencyBand(500, 10_000),
        effective_peak_dbfs=-12,
        duration_s=8,
        repeat_count=3,
        target_fingerprint=f"  {TARGET}  ",
        safety_profile_fingerprint=f" {PROFILE} ",
    )

    decision = _decide(request=request, limits=limits)

    assert decision.allowed is True
    assert decision.refusal_reasons == ()
    assert decision.request.band.is_subset_of(decision.limits.permitted_band)
    assert decision.request.band.lower_hz == 500.0
    assert decision.request.effective_peak_dbfs == -12.0
    assert decision.request.target_fingerprint == TARGET


def test_single_frequency_tone_is_a_valid_subset() -> None:
    decision = _decide(request=_request(band=FrequencyBand(2_000, 2_000)))

    assert decision.allowed is True


def test_both_out_of_band_edges_are_reported_in_stable_order() -> None:
    decision = _decide(request=_request(band=FrequencyBand(400, 12_000)))

    assert decision.allowed is False
    assert decision.refusal_reasons == (
        ExcitationRefusalReason.FREQUENCY_BELOW_PERMITTED_BAND,
        ExcitationRefusalReason.FREQUENCY_ABOVE_PERMITTED_BAND,
    )


def test_peak_duration_and_repeat_overages_are_all_refused() -> None:
    decision = _decide(
        request=_request(
            effective_peak_dbfs=-11.999,
            duration_s=8.001,
            repeat_count=4,
        )
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.EFFECTIVE_PEAK_ABOVE_LIMIT,
        ExcitationRefusalReason.DURATION_ABOVE_LIMIT,
        ExcitationRefusalReason.REPEAT_COUNT_ABOVE_LIMIT,
    )


def test_missing_request_identities_fail_closed() -> None:
    decision = _decide(
        request=_request(
            target_fingerprint="   ",
            safety_profile_fingerprint=None,
        )
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.TARGET_IDENTITY_MISSING,
        ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISSING,
    )


def test_mismatched_request_identities_fail_closed() -> None:
    decision = _decide(
        request=_request(
            target_fingerprint="old-target",
            safety_profile_fingerprint="old-profile",
        )
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.TARGET_IDENTITY_MISMATCH,
        ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISMATCH,
    )


def test_missing_protection_evidence_fails_closed() -> None:
    decision = admit_excitation(
        _request(),
        _limits(),
        protection_evidence=None,
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.PROTECTION_EVIDENCE_MISSING,
    )


def test_stale_protection_evidence_fails_closed() -> None:
    decision = _decide(evidence=_evidence(current=False))

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.PROTECTION_EVIDENCE_STALE,
    )


def test_mismatched_protection_bindings_fail_closed() -> None:
    decision = _decide(evidence=_evidence(
        target_fingerprint="old-target",
        safety_profile_fingerprint="old-profile",
        protection_requirement_fingerprint="old-requirement",
    ))

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.PROTECTION_TARGET_IDENTITY_MISMATCH,
        ExcitationRefusalReason.PROTECTION_PROFILE_IDENTITY_MISMATCH,
        ExcitationRefusalReason.PROTECTION_REQUIREMENT_MISMATCH,
    )


def test_partial_protection_evidence_reports_every_missing_binding() -> None:
    decision = _decide(evidence=_evidence(
        target_fingerprint=None,
        safety_profile_fingerprint="",
        protection_requirement_fingerprint=None,
        evidence_fingerprint="  ",
    ))

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.PROTECTION_TARGET_IDENTITY_MISSING,
        ExcitationRefusalReason.PROTECTION_PROFILE_IDENTITY_MISSING,
        ExcitationRefusalReason.PROTECTION_REQUIREMENT_MISSING,
        ExcitationRefusalReason.PROTECTION_PROOF_MISSING,
    )


@pytest.mark.parametrize(
    ("factory", "match"),
    (
        (lambda: FrequencyBand(0, 1_000), "positive"),
        (lambda: FrequencyBand(2_000, 1_000), "must not exceed"),
        (lambda: FrequencyBand(float("nan"), 1_000), "finite"),
        (lambda: _request(effective_peak_dbfs="-12"), "finite"),
        (lambda: _request(duration_s=float("inf")), "finite"),
        (lambda: _request(repeat_count=True), "positive integer"),
        (lambda: _request(target_fingerprint=7), "string or None"),
        (
            lambda: _limits(maximum_effective_peak_dbfs=0.1),
            "must not exceed 0 dBFS",
        ),
        (lambda: _limits(maximum_repeat_count=2.0), "positive integer"),
        (lambda: _limits(protection_requirement_fingerprint=""), "non-empty"),
        (lambda: _evidence(current=1), "must be a bool"),
    ),
)
def test_malformed_inputs_never_reach_admission(factory, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        factory()


def test_contract_values_and_result_are_immutable() -> None:
    request = _request()
    limits = _limits()
    evidence = _evidence()
    decision = _decide(request=request, limits=limits, evidence=evidence)

    for value, field, replacement in (
        (request, "duration_s", 99.0),
        (limits, "maximum_repeat_count", 99),
        (evidence, "current", False),
        (decision, "refusal_reasons", (ExcitationRefusalReason.DURATION_ABOVE_LIMIT,)),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
