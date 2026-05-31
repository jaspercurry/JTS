"""Contract tests for the wake-leg registry (``jasper.wake_legs``).

PR 0.1 of the mic-fusion architecture
(docs/HANDOFF-mic-fusion-architecture.md). The registry is the single
source of truth for leg identity + UDP ports; ``jasper.wake_ports``
derives its ``DEFAULT_*_PORT`` constants from it. These tests lock the
back-compat contract: the wire / on-disk tokens and ports must not
drift, or the historical ``wake_events`` corpus and the analysis
tooling (``scripts/analyze-three-leg.sh``) break.
"""
from __future__ import annotations

import pytest

from jasper import wake_legs, wake_ports


# The frozen wire contract: token -> udp_port. These values are baked
# into the historical telemetry corpus, the fired_legs CSV, and
# jasper.cli.aec_bridge's OUT_PORT* emit constants. Changing one is a
# breaking change to on-disk data — this test exists to make that loud.
_EXPECTED_TOKEN_PORTS = {
    "on": 9876,
    "off": 9877,
    "dtln": 9878,
    "raw0": 9879,
    "ref": 9880,
    "usb_raw": 9881,
    "usb_webrtc": 9882,
    "usb_dtln": 9883,
    "chip_aec_150": 9887,
    "chip_aec_210": 9888,
    "xvf_raw0_webrtc_aec3": 9889,
    "xvf_raw0_dtln": 9890,
}


def test_registry_tokens_and_ports_match_frozen_contract():
    assert wake_legs.all_ports() == _EXPECTED_TOKEN_PORTS


def test_tokens_names_and_ports_are_unique():
    tokens = [leg.token for leg in wake_legs.REGISTRY]
    names = [leg.name for leg in wake_legs.REGISTRY]
    ports = [leg.udp_port for leg in wake_legs.REGISTRY]
    assert len(tokens) == len(set(tokens)), "duplicate leg token"
    assert len(names) == len(set(names)), "duplicate leg name"
    assert len(ports) == len(set(ports)), "duplicate udp port"


def test_wake_input_legs_in_priority_order():
    # WakeLoop OR-gates these; order is the daemon's leg priority. The
    # hardware-conditional chip-AEC beam legs follow the always-built
    # software legs (on/off/dtln) — the chip-AEC promotion moved them from
    # corpus-only into the wake-input set.
    assert [leg.token for leg in wake_legs.wake_input_legs()] == [
        "on", "off", "dtln", "chip_aec_150", "chip_aec_210",
    ]
    assert all(leg.wake_input for leg in wake_legs.wake_input_legs())


def test_chip_aec_beam_legs_are_wake_inputs():
    # The chip-AEC promotion: the XVF3800 fixed 150°/210° ASR beams are now
    # opt-in, hardware-conditional wake inputs (default-OFF at the config
    # layer, but wake_input=True at the registry layer). Their tokens +
    # ports stay frozen so the historical corpus + analysis tooling hold.
    for token, port in (("chip_aec_150", 9887), ("chip_aec_210", 9888)):
        leg = wake_legs.by_token(token)
        assert leg.wake_input is True
        assert leg.udp_port == port
        assert leg.kind is wake_legs.LegKind.HARDWARE_AEC


def test_corpus_legs_are_not_wake_inputs():
    for token in (
        "raw0", "ref", "usb_raw", "usb_webrtc", "usb_dtln",
        "xvf_raw0_webrtc_aec3", "xvf_raw0_dtln",
    ):
        assert wake_legs.by_token(token).wake_input is False


def test_by_token_and_by_name_round_trip():
    for leg in wake_legs.REGISTRY:
        assert wake_legs.by_token(leg.token) is leg
        assert wake_legs.by_name(leg.name) is leg


def test_lookups_raise_keyerror_on_miss():
    with pytest.raises(KeyError):
        wake_legs.by_token("nope")
    with pytest.raises(KeyError):
        wake_legs.by_name("nope")


def test_wake_ports_constants_derive_from_registry():
    # The shim contract: every DEFAULT_*_PORT equals the registry port.
    assert wake_ports.DEFAULT_AEC_ON_PORT == wake_legs.by_token("on").udp_port
    assert wake_ports.DEFAULT_AEC_OFF_PORT == wake_legs.by_token("off").udp_port
    assert wake_ports.DEFAULT_AEC_DTLN_PORT == wake_legs.by_token("dtln").udp_port
    assert wake_ports.DEFAULT_AEC_RAW0_PORT == wake_legs.by_token("raw0").udp_port
    assert wake_ports.DEFAULT_AEC_REF_PORT == wake_legs.by_token("ref").udp_port
    assert wake_ports.DEFAULT_AEC_USB_RAW_PORT == wake_legs.by_token("usb_raw").udp_port
    assert wake_ports.DEFAULT_AEC_USB_WEBRTC_PORT == wake_legs.by_token("usb_webrtc").udp_port
    assert wake_ports.DEFAULT_AEC_USB_DTLN_PORT == wake_legs.by_token("usb_dtln").udp_port
    assert wake_ports.DEFAULT_AEC_CHIP_AEC_150_PORT == wake_legs.by_token("chip_aec_150").udp_port
    assert wake_ports.DEFAULT_AEC_CHIP_AEC_210_PORT == wake_legs.by_token("chip_aec_210").udp_port
    assert wake_ports.DEFAULT_AEC_XVF_RAW0_WEBRTC_AEC3_PORT == wake_legs.by_token(
        "xvf_raw0_webrtc_aec3",
    ).udp_port
    assert wake_ports.DEFAULT_AEC_XVF_RAW0_DTLN_PORT == wake_legs.by_token(
        "xvf_raw0_dtln",
    ).udp_port


def test_build_ports_default_output_unchanged():
    # Behavior-preservation: build_ports() still yields the fixed legs at
    # the frozen ports (sweep variants are parametric and added on top).
    ports = wake_ports.build_ports()
    for token, port in _EXPECTED_TOKEN_PORTS.items():
        assert ports[token] == port


def test_build_ports_include_flags_behaviour_preserved():
    # Mirrors the documented gating (and tests/test_wake_corpus_setup.py).
    no_dtln = wake_ports.build_ports(include_dtln=False)
    assert "dtln" not in no_dtln
    assert "raw0" in no_dtln  # raw0 is always present
    no_usb = wake_ports.build_ports(include_usb=False)
    for token in ("ref", "usb_raw", "usb_webrtc", "usb_dtln"):
        assert token not in no_usb
