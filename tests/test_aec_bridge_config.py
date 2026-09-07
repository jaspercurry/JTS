# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `jasper.aec.bridge_config`.

`BridgeConfig.from_env` is the bridge's only env-reading surface, and the
two device-presence validators and the ref_source/usb_mic_source resolvers
sit behind it. These pins cover env parsing and the resolution logic in
isolation, before any of it reaches `main()` or `_aec_loop`.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jasper.cli import aec_bridge
from jasper.aec import bridge_config
from jasper.aec.bridge_config import (
    BridgeConfig,
    MicDeviceUnavailable,
    resolve_usb_mic_source,
    validate_mic_device,
)
from tests._sounddevice_stub import stub_sounddevice


def test_validate_mic_device_raises_before_bridge_starts(monkeypatch):
    """A missing XVF/Array device must fail before the bridge opens any
    audio endpoint at all."""
    sd_mod = MagicMock()
    sd_mod.query_devices.side_effect = ValueError(
        "No input device matching 'Array'"
    )
    stub_sounddevice(monkeypatch, sd_mod)

    with pytest.raises(MicDeviceUnavailable):
        validate_mic_device()

    sd_mod.query_devices.assert_called_once_with("Array", "input")


def test_raw_port_overridable_via_env(monkeypatch):
    """Operators can move the raw stream off the default 9877
    (e.g. for two-bridge testing) without touching the AEC port."""
    monkeypatch.setenv("JASPER_AEC_UDP_PORT_RAW", "19877")

    config = BridgeConfig.from_env()

    assert config.out_port_raw == 19877
    # Default AEC port unaffected; compatibility constant remains canonical.
    assert config.out_port == 9876
    assert aec_bridge.OUT_PORT_RAW == 9877


def test_usb_mic_leg_config_defaults_and_parses_env(monkeypatch):
    monkeypatch.delenv("JASPER_USB_MIC_LEG", raising=False)
    assert BridgeConfig.from_env().usb_mic_leg == "primary"

    monkeypatch.setenv("JASPER_USB_MIC_LEG", "raw0")
    assert BridgeConfig.from_env().usb_mic_leg == "raw0"


def test_usb_mic_source_resolves_primary_stale_and_software_modes() -> None:
    from jasper.mics import xvf3800

    plan = xvf3800.SQUARE_FIXED_150_210_PLAN
    assert resolve_usb_mic_source(
        "primary",
        plan=plan,
        production_chip_aec_enabled=True,
        chip_aec_primary_leg="chip_aec_150",
    ) == {
        "selection": "primary",
        "mode": "chip_aec",
        "leg": "chip_aec_150",
        "fallback_active": False,
    }
    assert resolve_usb_mic_source(
        "stale_plan_leg",
        plan=plan,
        production_chip_aec_enabled=True,
        chip_aec_primary_leg="chip_aec_150",
    ) == {
        "selection": "primary",
        "mode": "chip_aec",
        "leg": "chip_aec_150",
        "fallback_active": False,
    }
    assert resolve_usb_mic_source(
        "chip_aec_210",
        plan=plan,
        production_chip_aec_enabled=False,
        chip_aec_primary_leg="chip_aec_150",
    ) == {
        "selection": "chip_aec_210",
        "mode": "software_aec3",
        "leg": "clean",
        "fallback_active": True,
    }
    assert resolve_usb_mic_source(
        "raw0",
        plan=plan,
        production_chip_aec_enabled=True,
        chip_aec_primary_leg="chip_aec_150",
    ) == {
        "selection": "raw0",
        "mode": "raw",
        "leg": "raw0",
        "fallback_active": False,
    }
    assert resolve_usb_mic_source(
        "raw0",
        plan=None,
        production_chip_aec_enabled=False,
        chip_aec_primary_leg="chip_aec_150",
    ) == {
        "selection": "primary",
        "mode": "software_aec3",
        "leg": "clean",
        "fallback_active": False,
    }


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("low", "low"), ("0.04", "0.04"), ("", "")],
)
def test_capture_latency_parses_from_env(monkeypatch, configured, expected):
    monkeypatch.setenv("JASPER_AEC_CAPTURE_LATENCY", configured)

    config = BridgeConfig.from_env()

    assert config.capture_latency == expected


@pytest.mark.parametrize(
    "configured", ["fast", "0", "-0.1", "0.251", "nan", "inf"]
)
def test_capture_latency_invalid_values_fall_back_to_default(monkeypatch, configured):
    monkeypatch.setenv("JASPER_AEC_CAPTURE_LATENCY", configured)
    event = MagicMock()
    monkeypatch.setattr(bridge_config, "log_event", event)
    test_logger = MagicMock()

    config = BridgeConfig.from_env(logger_=test_logger)

    assert config.capture_latency == ""
    event.assert_called_once_with(
        test_logger,
        "aec.capture_latency_invalid",
        value=configured,
        fallback="default",
        level=bridge_config.logging.WARNING,
    )
