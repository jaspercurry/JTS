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
    explicit testing request, so `permits` is the whole gate — the retired
    arm/trial flags said "yes" to every selection alike and could not be.
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


def test_runtime_env_gate_answers_only_the_selection_it_was_recorded_for():
    """A record written for production cannot answer a testing request.

    Serving it would report `needs_calibration` where the testing-aware
    resolve says `testing`, so the caller must fall through and resolve.
    """
    recorded = {
        "JASPER_AUDIO_DAC_ID": "mystery_usb_audio",
        "JASPER_AEC_CHIP_AEC_DAC_ID": "mystery_usb_audio",
        "JASPER_AEC_CHIP_AEC_DAC_STATUS": "needs_calibration",
        "JASPER_AEC_CHIP_AEC_TESTING_REQUESTED": "0",
    }

    assert gate_from_runtime_env(recorded, testing_requested=True) is None
    assert gate_from_runtime_env(recorded, testing_requested=False) is not None
    # No selection named: the record answers as written, as it always has.
    assert gate_from_runtime_env(recorded) is not None


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
