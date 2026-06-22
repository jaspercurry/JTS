# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jasper.voice.input_policy import (
    build_effective_speech_input_policy,
    validate_openai_noise_reduction,
)


def _cfg(**overrides):
    values = {
        "voice_provider": "openai",
        "mic_device": "Array",
        "mic_device_chip_aec_150": "",
        "mic_device_chip_aec_210": "",
        "aec_chip_aec_enabled": False,
        "server_vad_enabled": False,
        "openai_noise_reduction": "auto",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_auto_disables_openai_noise_reduction_for_chip_aec_input():
    policy = build_effective_speech_input_policy(_cfg(
        mic_device="udp:9876",
        mic_device_chip_aec_150="udp:9887",
        mic_device_chip_aec_210="udp:9888",
        aec_chip_aec_enabled=True,
    ))

    assert policy.input_contract.profile == "xvf_chip_aec"
    assert policy.input_contract.already_processed is True
    assert policy.openai_noise_reduction is None
    assert policy.openai_noise_reduction_source == "auto_processed_input"
    assert policy.warnings == ()


def test_auto_disables_openai_noise_reduction_for_software_aec3_input():
    policy = build_effective_speech_input_policy(_cfg(mic_device="udp:9876"))

    assert policy.input_contract.profile == "xvf_software_aec3"
    assert policy.openai_noise_reduction is None
    assert policy.openai_noise_reduction_source == "auto_processed_input"


def test_auto_uses_far_field_for_raw_direct_mic_input():
    policy = build_effective_speech_input_policy(_cfg(mic_device="Array"))

    assert policy.input_contract.profile == "direct_mic"
    assert policy.input_contract.raw is True
    assert policy.openai_noise_reduction == "far_field"
    assert policy.openai_noise_reduction_source == "auto_raw_far_field"


def test_custom_udp_auto_leaves_provider_denoising_off_with_warning():
    policy = build_effective_speech_input_policy(_cfg(mic_device="udp:9999"))

    assert policy.input_contract.profile == "custom_udp"
    assert policy.openai_noise_reduction is None
    assert policy.openai_noise_reduction_source == "auto_unknown_udp"
    assert "Custom UDP input profile" in policy.warnings[0]


def test_explicit_far_field_is_preserved_but_warns_on_processed_input():
    policy = build_effective_speech_input_policy(_cfg(
        mic_device="udp:9876",
        openai_noise_reduction="far_field",
    ))

    assert policy.openai_noise_reduction == "far_field"
    assert policy.openai_noise_reduction_source == "explicit"
    assert "already processed" in policy.warnings[0]


def test_invalid_openai_noise_reduction_rejected():
    with pytest.raises(RuntimeError, match="JASPER_OPENAI_NOISE_REDUCTION"):
        validate_openai_noise_reduction("aggressive_magic")
