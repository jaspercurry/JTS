# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jasper.config import Config
from jasper.voice.input_policy import (
    build_effective_speech_input_policy,
    contract_from_config,
    validate_openai_noise_reduction,
)


def _cfg(**overrides):
    values = {
        "voice_provider": "openai",
        "mic_device": "Array",
        "mic_device_chip_aec_150": "",
        "mic_device_chip_aec_210": "",
        "aec_chip_aec_enabled": False,
        "openai_noise_reduction": "auto",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_auto_disables_openai_noise_reduction_for_chip_aec_input():
    policy = build_effective_speech_input_policy(_cfg(
        mic_device="udp:9876",
        aec_chip_aec_enabled=True,
    ))

    assert policy.input_contract.profile == "xvf_chip_aec"
    assert policy.input_contract.beamformed is True
    assert policy.input_contract.already_processed is True
    assert policy.openai_noise_reduction is None
    assert policy.openai_noise_reduction_source == "auto_processed_input"
    assert policy.warnings == ()


def test_auto_disables_openai_noise_reduction_for_software_aec3_input():
    policy = build_effective_speech_input_policy(_cfg(mic_device="udp:9876"))

    assert policy.input_contract.profile == "xvf_software_aec3"
    assert policy.openai_noise_reduction is None
    assert policy.openai_noise_reduction_source == "auto_processed_input"


@pytest.mark.parametrize("chip", [False, True])
@pytest.mark.parametrize("device,port,host,processed", [
    ("udp:9876", None, None, True),
    (" UDP:9876 ", None, None, True),
    ("udp://127.0.0.1:9876", None, None, True),
    ("udp:5555", "5555", None, True),
    ("UDP://127.0.0.1:5555", "5555", None, True),
    ("udp:9876", "5555", None, False),
    ("udp:9877", None, None, False),
    ("udp:9999", None, None, False),
    ("Array", None, None, False),
    ("udp://192.0.2.10:9876", None, None, False),
    ("udp:9876", None, "192.0.2.10", False),
    ("udp://192.0.2.10:5555", "5555", "192.0.2.10", True),
    ("udp://0.0.0.0:9876", None, None, True),
    ("udp://:9876", None, None, True),
    ("udp://localhost:9876", None, "localhost", True),
    ("udp://localhost:9876", None, None, False),
])
def test_applied_stream_processing_uses_configured_bridge_port(
    monkeypatch, chip, device, port, host, processed,
):
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_MIC_DEVICE", device)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", str(int(chip)))
    if port is None:
        monkeypatch.delenv("JASPER_AEC_UDP_PORT", raising=False)
    else:
        monkeypatch.setenv("JASPER_AEC_UDP_PORT", port)
    if host is None:
        monkeypatch.delenv("JASPER_AEC_UDP_HOST", raising=False)
    else:
        monkeypatch.setenv("JASPER_AEC_UDP_HOST", host)
    contract = contract_from_config(Config.from_env())
    assert contract.echo_cancelled is processed
    assert contract.beamformed is (processed and chip)
    assert contract.source == device
    if not processed:
        assert contract.profile == ("direct_mic" if device == "Array" else "custom_udp")


@pytest.mark.parametrize(
    "mic_device, expected_provenance",
    [
        ("Array", "default"),  # known XVF3800 ALSA card name
        ("L16K6Ch", "default"),  # known flex-linear ALSA card name
        ("Some USB Mic", "operator"),  # not a known ALSA card name
        (None, "default"),  # not configured
    ],
)
def test_direct_mic_provenance_reflects_known_alsa_cards(
    mic_device, expected_provenance,
):
    contract = contract_from_config(_cfg(mic_device=mic_device))

    assert contract.profile == "direct_mic"
    assert contract.provenance == expected_provenance


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
