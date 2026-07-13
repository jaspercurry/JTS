# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
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

TARGET = "1" * 64
PROFILE = "2" * 64
REQUIREMENT = "3" * 64
PLAN = "4" * 64
PROOF = "5" * 64
OTHER_TARGET = "a" * 64
OTHER_PROFILE = "b" * 64
OTHER_REQUIREMENT = "c" * 64
OTHER_PLAN = "d" * 64
OTHER_PROOF = "e" * 64
OTHER_AUTHORITY = "f" * 64


def _limits(**changes: object) -> ExcitationLimits:
    values: dict[str, object] = {
        "permitted_band": FrequencyBand(500, 10_000),
        "maximum_effective_peak_dbfs": -12,
        "maximum_duration_s": 8,
        "maximum_repeat_count": 3,
        "target_fingerprint": TARGET,
        "safety_profile_fingerprint": PROFILE,
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
        "safety_profile_fingerprint": authority.safety_profile_fingerprint,
        "authority_fingerprint": authority.fingerprint,
        "excitation_plan_fingerprint": authority.excitation_plan_fingerprint,
    }
    values.update(changes)
    return ExcitationRequest(**values)  # type: ignore[arg-type]


def _evidence(
    *,
    limits: ExcitationLimits | None = None,
    **changes: object,
) -> ProtectionEvidence:
    authority = limits or _limits()
    values: dict[str, object] = {
        "target_fingerprint": authority.target_fingerprint,
        "safety_profile_fingerprint": authority.safety_profile_fingerprint,
        "protection_requirement_fingerprint": (
            authority.protection_requirement_fingerprint
        ),
        "authority_fingerprint": authority.fingerprint,
        "excitation_plan_fingerprint": authority.excitation_plan_fingerprint,
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
    authority = limits or _limits()
    return admit_excitation(
        request or _request(limits=authority),
        authority,
        protection_evidence=evidence or _evidence(limits=authority),
    )


def test_exact_closed_boundaries_are_allowed_and_canonical() -> None:
    limits = _limits()
    request = _request(
        limits=limits,
        band=FrequencyBand(500, 10_000),
        effective_peak_dbfs=-12,
        duration_s=8,
        repeat_count=3,
    )

    decision = _decide(request=request, limits=limits)

    assert decision.allowed is True
    assert decision.refusal_reasons == ()
    assert decision.request.band.is_subset_of(decision.limits.permitted_band)
    assert decision.request.band.lower_hz == 500.0
    assert decision.request.effective_peak_dbfs == -12.0
    assert decision.request.authority_fingerprint == limits.fingerprint
    assert decision.request.excitation_plan_fingerprint == PLAN
    assert decision.fingerprint == decision.to_dict()["fingerprint"]


def test_zero_width_tone_uses_the_same_closed_band_contract() -> None:
    decision = _decide(request=_request(band=FrequencyBand(2_000, 2_000)))

    assert decision.allowed is True


def test_both_frequency_edges_are_reported_in_stable_order() -> None:
    decision = _decide(request=_request(band=FrequencyBand(400, 12_000)))

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.FREQUENCY_BELOW_PERMITTED_BAND,
        ExcitationRefusalReason.FREQUENCY_ABOVE_PERMITTED_BAND,
    )


def test_peak_duration_and_repeat_overages_are_all_reported() -> None:
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
            target_fingerprint=None,
            safety_profile_fingerprint=None,
            authority_fingerprint=None,
            excitation_plan_fingerprint=None,
        )
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.TARGET_IDENTITY_MISSING,
        ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISSING,
        ExcitationRefusalReason.AUTHORITY_IDENTITY_MISSING,
        ExcitationRefusalReason.EXCITATION_PLAN_IDENTITY_MISSING,
    )


def test_mismatched_request_identities_fail_closed() -> None:
    decision = _decide(
        request=_request(
            target_fingerprint=OTHER_TARGET,
            safety_profile_fingerprint=OTHER_PROFILE,
            authority_fingerprint=OTHER_AUTHORITY,
            excitation_plan_fingerprint=OTHER_PLAN,
        )
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.TARGET_IDENTITY_MISMATCH,
        ExcitationRefusalReason.SAFETY_PROFILE_IDENTITY_MISMATCH,
        ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH,
        ExcitationRefusalReason.EXCITATION_PLAN_IDENTITY_MISMATCH,
    )


def test_missing_protection_evidence_fails_closed() -> None:
    limits = _limits()
    decision = admit_excitation(
        _request(limits=limits),
        limits,
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
    decision = _decide(
        evidence=_evidence(
            target_fingerprint=OTHER_TARGET,
            safety_profile_fingerprint=OTHER_PROFILE,
            protection_requirement_fingerprint=OTHER_REQUIREMENT,
            authority_fingerprint=OTHER_AUTHORITY,
            excitation_plan_fingerprint=OTHER_PLAN,
        )
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.PROTECTION_TARGET_IDENTITY_MISMATCH,
        ExcitationRefusalReason.PROTECTION_PROFILE_IDENTITY_MISMATCH,
        ExcitationRefusalReason.PROTECTION_REQUIREMENT_MISMATCH,
        ExcitationRefusalReason.PROTECTION_AUTHORITY_MISMATCH,
        ExcitationRefusalReason.PROTECTION_PLAN_IDENTITY_MISMATCH,
    )


def test_partial_protection_evidence_reports_every_missing_binding() -> None:
    decision = _decide(
        evidence=_evidence(
            target_fingerprint=None,
            safety_profile_fingerprint=None,
            protection_requirement_fingerprint=None,
            authority_fingerprint=None,
            excitation_plan_fingerprint=None,
            evidence_fingerprint=None,
        )
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.PROTECTION_TARGET_IDENTITY_MISSING,
        ExcitationRefusalReason.PROTECTION_PROFILE_IDENTITY_MISSING,
        ExcitationRefusalReason.PROTECTION_REQUIREMENT_MISSING,
        ExcitationRefusalReason.PROTECTION_AUTHORITY_MISSING,
        ExcitationRefusalReason.PROTECTION_PLAN_IDENTITY_MISSING,
        ExcitationRefusalReason.PROTECTION_PROOF_MISSING,
    )


def test_broadened_limits_with_same_target_and_profile_are_not_same_authority() -> None:
    original = _limits()
    broadened = _limits(
        permitted_band=FrequencyBand(100, 20_000),
        maximum_effective_peak_dbfs=-6,
        maximum_duration_s=30,
        maximum_repeat_count=10,
    )
    request = _request(limits=original)
    evidence = _evidence(limits=original)

    assert broadened.target_fingerprint == original.target_fingerprint
    assert broadened.safety_profile_fingerprint == original.safety_profile_fingerprint
    assert broadened.fingerprint != original.fingerprint
    decision = _decide(request=request, limits=broadened, evidence=evidence)
    assert decision.refusal_reasons == (
        ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH,
        ExcitationRefusalReason.PROTECTION_AUTHORITY_MISMATCH,
    )
    with pytest.raises(ValueError, match="expected fingerprint"):
        ExcitationLimits.from_dict(
            broadened.to_dict(),
            expected_fingerprint=original.fingerprint,
        )


def test_changed_plan_breaks_request_authority_and_protection_bindings() -> None:
    original = _limits()
    changed = _limits(excitation_plan_fingerprint=OTHER_PLAN)

    decision = _decide(
        request=_request(limits=original),
        limits=changed,
        evidence=_evidence(limits=original),
    )

    assert decision.refusal_reasons == (
        ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH,
        ExcitationRefusalReason.EXCITATION_PLAN_IDENTITY_MISMATCH,
        ExcitationRefusalReason.PROTECTION_AUTHORITY_MISMATCH,
        ExcitationRefusalReason.PROTECTION_PLAN_IDENTITY_MISMATCH,
    )


def test_proof_identity_is_content_bound_and_tamper_is_rejected() -> None:
    evidence = _evidence()
    other_proof = _evidence(evidence_fingerprint=OTHER_PROOF)

    assert evidence.fingerprint != other_proof.fingerprint
    serialized = evidence.to_dict()
    serialized["evidence_fingerprint"] = OTHER_PROOF
    with pytest.raises(ValueError, match="fingerprint does not match"):
        ProtectionEvidence.from_dict(serialized)


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
        (lambda: _evidence(current=1), "must be a bool"),
        (lambda: _evidence(evidence_fingerprint="proof"), "canonical lowercase"),
    ),
)
def test_malformed_inputs_never_reach_admission(factory, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        factory()


def test_schema_versioned_artifacts_round_trip_through_json() -> None:
    limits = _limits()
    request = _request(limits=limits)
    evidence = _evidence(limits=limits)
    admission = _decide(request=request, limits=limits, evidence=evidence)

    request_wire = json.loads(json.dumps(request.to_dict()))
    limits_wire = json.loads(json.dumps(limits.to_dict()))
    evidence_wire = json.loads(json.dumps(evidence.to_dict()))
    admission_wire = json.loads(json.dumps(admission.to_dict()))

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
    assert (
        ProtectionEvidence.from_dict(
            evidence_wire,
            expected_fingerprint=evidence.fingerprint,
        )
        == evidence
    )
    assert (
        ExcitationAdmission.from_dict(
            admission_wire,
            expected_fingerprint=admission.fingerprint,
        )
        == admission
    )

    for artifact in (request_wire, limits_wire, evidence_wire, admission_wire):
        assert artifact["schema_version"] == 1
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
    wrong_schema["schema_version"] = 2
    with pytest.raises(ValueError, match="fingerprint does not match"):
        ExcitationLimits.from_dict(wrong_schema)


def test_nested_admission_tamper_is_rejected() -> None:
    serialized = copy.deepcopy(_decide().to_dict())
    request = serialized["request"]
    assert isinstance(request, dict)
    request["duration_s"] = 99.0

    with pytest.raises(ValueError, match="fingerprint does not match"):
        ExcitationAdmission.from_dict(serialized)


def test_admission_constructor_rejects_forged_allow_and_refusal() -> None:
    limits = _limits()
    request = _request(limits=limits)
    evidence = _evidence(limits=limits)

    with pytest.raises(ValueError, match="do not match"):
        ExcitationAdmission(
            request=request,
            limits=limits,
            protection_evidence=None,
            refusal_reasons=(),
        )
    with pytest.raises(ValueError, match="do not match"):
        ExcitationAdmission(
            request=request,
            limits=limits,
            protection_evidence=evidence,
            refusal_reasons=(ExcitationRefusalReason.DURATION_ABOVE_LIMIT,),
        )


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
