# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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


def test_static_approved_dac_allows_auto_and_production_chip_aec():
    gate = resolve_chip_aec_dac_gate("hifiberry_dac8x")

    assert gate.status == STATUS_APPROVED
    assert gate.permitted is True
    assert gate.auto_allowed is True
    assert gate.production_allowed is True
    assert gate.testing_allowed is True
    assert gate.recommended_action == ACTION_USE_CHIP_AEC


def test_unapproved_dac_falls_back_without_explicit_testing():
    gate = resolve_chip_aec_dac_gate("mystery_usb_audio")

    assert gate.status == STATUS_NEEDS_CALIBRATION
    assert gate.permitted is False
    assert gate.auto_allowed is False
    assert gate.production_allowed is False
    assert gate.testing_allowed is True
    assert gate.recommended_action == ACTION_USE_SOFTWARE_OR_TEST


def test_explicit_testing_arms_unapproved_dac_without_auto_promotion():
    gate = resolve_chip_aec_dac_gate(
        "mystery_usb_audio",
        testing_requested=True,
    )

    assert gate.status == STATUS_TESTING
    assert gate.permitted is True
    assert gate.auto_allowed is False
    assert gate.production_allowed is False
    assert gate.testing_allowed is True
    assert gate.recommended_action == ACTION_RUN_TESTING_AND_VALIDATE


def test_live_coherent_outputd_clock_promotes_future_dac_to_approved():
    gate = resolve_chip_aec_dac_gate(
        "mystery_usb_audio",
        outputd_status=_outputd_status("coherent"),
    )

    assert gate.status == STATUS_APPROVED
    assert gate.permitted is True
    assert gate.source == "outputd_aec_clock"


def test_compensable_outputd_clock_does_not_auto_arm():
    gate = resolve_chip_aec_dac_gate(
        "mystery_usb_audio",
        outputd_status=_outputd_status("compensable"),
    )

    assert gate.status == STATUS_NEEDS_CALIBRATION
    assert gate.permitted is False
    assert "verdict=compensable" in gate.detail


def test_runtime_env_gate_round_trips_reconciler_written_status():
    gate = gate_from_runtime_env({
        "JASPER_AUDIO_DAC_ID": "mystery_usb_audio",
        "JASPER_AEC_CHIP_AEC_DAC_ID": "mystery_usb_audio",
        "JASPER_AEC_CHIP_AEC_DAC_STATUS": "testing",
        "JASPER_AEC_CHIP_AEC_DAC_SOURCE": "explicit_testing",
        "JASPER_AEC_CHIP_AEC_DAC_DETAIL": "operator validation",
        "JASPER_AEC_CHIP_AEC_DAC_ACTION": "run_chip_aec_testing_and_validate",
    })

    assert gate.status == STATUS_TESTING
    assert gate.permitted is True
    assert gate.auto_allowed is False
    assert gate.detail == "operator validation"


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
