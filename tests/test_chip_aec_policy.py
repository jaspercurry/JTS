# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.chip_aec_policy import (
    ACTION_RUN_TESTING_AND_VALIDATE,
    ACTION_USE_CHIP_AEC,
    ACTION_USE_SOFTWARE_OR_TEST,
    STATUS_APPROVED,
    STATUS_NEEDS_CALIBRATION,
    STATUS_TESTING,
    effective_chip_aec_dac_gate,
    gate_from_runtime_env,
    resolve_chip_aec_dac_gate,
)


def _outputd_status(verdict: str, estimator_status: str = "locked") -> dict:
    return {
        "reference_outputs": {
            "chip_ref_writer": {"enabled": True},
            "aec_clock": {
                "verdict": verdict,
                "sro_estimator_status": estimator_status,
                "observe": True,
                "chip_ref_sro_ppm": 0.2,
                "verdict_reason": "test",
            },
        },
    }


@pytest.mark.parametrize(
    ("dac_id", "testing", "status", "auto_allowed", "action", "permits"),
    [
        ("hifiberry_dac8x", False, STATUS_APPROVED, True,
         ACTION_USE_CHIP_AEC, True),
        ("hifiberry_dac8x", True, STATUS_APPROVED, True,
         ACTION_USE_CHIP_AEC, True),
        ("mystery_usb_audio", False, STATUS_NEEDS_CALIBRATION, False,
         ACTION_USE_SOFTWARE_OR_TEST, False),
        ("mystery_usb_audio", True, STATUS_TESTING, False,
         ACTION_RUN_TESTING_AND_VALIDATE, True),
    ],
    ids=["approved_auto", "approved_testing", "uncodified_auto",
         "uncodified_testing"],
)
def test_the_dac_gate_classifies_and_permits_per_selection(
    dac_id, testing, status, auto_allowed, action, permits,
):
    """ADR-0101: the AUTOMATIC selection is the only one the DAC gates.

    An uncodified DAC refuses to be picked on its own yet still arms on an
    explicit testing request, so `permits` is the whole gate (#3073 item 3).
    """
    gate = resolve_chip_aec_dac_gate(dac_id, testing_requested=testing)

    assert gate.status == status
    assert gate.auto_allowed is auto_allowed
    assert gate.recommended_action == action
    assert gate.permits(testing_requested=testing) is permits


def test_live_coherent_outputd_clock_is_diagnostic_not_authorization():
    gate = resolve_chip_aec_dac_gate(
        "mystery_usb_audio",
        outputd_status=_outputd_status("coherent"),
    )

    assert gate.status == STATUS_NEEDS_CALIBRATION
    assert gate.auto_allowed is False
    assert gate.source == "static"
    assert "verdict=coherent" in gate.detail


def test_compensable_outputd_clock_does_not_auto_arm():
    gate = resolve_chip_aec_dac_gate(
        "mystery_usb_audio",
        outputd_status=_outputd_status("compensable"),
    )

    assert gate.status == STATUS_NEEDS_CALIBRATION
    assert gate.auto_allowed is False
    assert "verdict=compensable" in gate.detail


def test_runtime_env_gate_round_trips_reconciler_written_status():
    gate = gate_from_runtime_env({
        "JASPER_AUDIO_DAC_ID": "mystery_usb_audio",
        "JASPER_AEC_CHIP_AEC_DAC_ID": "mystery_usb_audio",
        "JASPER_AEC_CHIP_AEC_DAC_STATUS": "testing",
        "JASPER_AEC_CHIP_AEC_DAC_SOURCE": "explicit_testing",
        "JASPER_AEC_CHIP_AEC_DAC_DETAIL": "operator validation",
    })

    assert gate.status == STATUS_TESTING
    assert gate.auto_allowed is False
    assert gate.detail == "operator validation"


@pytest.mark.parametrize("recorded_under_testing", ["0", "1"])
@pytest.mark.parametrize("asking_for_testing", [False, True])
def test_one_runtime_env_record_answers_whichever_selection_asks(
    recorded_under_testing: str, asking_for_testing: bool,
) -> None:
    """The carry's semantics, which this reader used to refuse (#3073 item 4).

    deploy/bin/jasper-aec-reconcile's carry_chip_aec_dac_gate serves one record
    to both selections. Refusing the cross-selection read sent the caller off
    to re-derive a fresh static verdict, discarding whatever the reconciler had
    actually applied — including a carried or revoked one.

    The DAC is one the registry still approves, so a re-derivation permits it
    for either selection: `permits(testing_requested=False) is False` here is
    the revoked verdict surviving, which is the whole point of serving the
    record instead of looking the DAC up again.
    """
    gate = gate_from_runtime_env({
        "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
        "JASPER_AEC_CHIP_AEC_DAC_ID": "hifiberry_dac8x",
        "JASPER_AEC_CHIP_AEC_DAC_STATUS": "needs_calibration",
        "JASPER_AEC_CHIP_AEC_DAC_SOURCE": "runtime_env_carried",
        "JASPER_AEC_CHIP_AEC_DAC_DETAIL": "carrying last verdict",
        "JASPER_AEC_CHIP_AEC_TESTING_REQUESTED": recorded_under_testing,
    })

    assert gate is not None
    # The record's own verdict, not a re-derivation of it.
    assert gate.status == STATUS_NEEDS_CALIBRATION
    assert gate.source == "runtime_env_carried"
    assert gate.detail == "carrying last verdict"
    # Per-selection serving is permits(), the mapping the bash carry applies.
    assert gate.permits(testing_requested=asking_for_testing) is asking_for_testing


def test_runtime_env_gate_rejects_stale_dac_identity():
    gate = gate_from_runtime_env({
        "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x_studio",
        "JASPER_AEC_CHIP_AEC_DAC_ID": "hifiberry_dac8x",
        "JASPER_AEC_CHIP_AEC_DAC_STATUS": "approved",
        "JASPER_AEC_CHIP_AEC_DAC_SOURCE": "static",
        "JASPER_AEC_CHIP_AEC_DAC_DETAIL": "old approved gate",
    })

    assert gate is None


def test_runtime_env_gate_rejects_missing_persisted_dac_identity():
    gate = gate_from_runtime_env({
        "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
        "JASPER_AEC_CHIP_AEC_DAC_STATUS": "approved",
        "JASPER_AEC_CHIP_AEC_DAC_SOURCE": "static",
        "JASPER_AEC_CHIP_AEC_DAC_DETAIL": "old approved gate",
    })

    assert gate is None


@pytest.mark.parametrize(
    ("env", "testing_requested", "status", "action"),
    [
        ({"JASPER_AUDIO_DAC_ID": "hifiberry_dac8x"}, False,
         STATUS_APPROVED, ACTION_USE_CHIP_AEC),
        ({"JASPER_AUDIO_DAC_ID": "mystery_usb_audio"}, True,
         STATUS_TESTING, ACTION_RUN_TESTING_AND_VALIDATE),
        (
            {
                "JASPER_AUDIO_DAC_ID": "mystery_usb_audio",
                "JASPER_AEC_CHIP_AEC_DAC_ID": "mystery_usb_audio",
                "JASPER_AEC_CHIP_AEC_DAC_STATUS": "approved",
                "JASPER_AEC_CHIP_AEC_DAC_SOURCE": "runtime_env_carried",
                "JASPER_AEC_CHIP_AEC_DAC_DETAIL": "carried verdict",
            },
            False, STATUS_APPROVED, ACTION_USE_CHIP_AEC,
        ),
    ],
    ids=["approved_no_record", "uncalibrated_testing_no_record",
         "served_record_overrides_registry"],
)
def test_effective_dac_gate_prefers_served_record_over_registry(
    env, testing_requested, status, action,
):
    """The served-record-beats-registry rule (module docstring)."""
    gate = effective_chip_aec_dac_gate(env, testing_requested=testing_requested)
    assert gate.status == status
    assert gate.recommended_action == action
