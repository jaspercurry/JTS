# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `_configured_wake_legs`, the pure wake-leg selection
decision, and the `_LEG_DB`/`_LEG_DEVICE_ATTR` completeness guards."""
from __future__ import annotations

import pytest


def test_leg_db_covers_all_wake_input_legs():
    """Every wake-input leg in the registry must have a _LEG_DB telemetry
    mapping — otherwise _handle_wake_frame would KeyError on a leg present
    in self._legs but missing from _LEG_DB. (voice_daemon also guards this
    at import; this gives a targeted, discoverable failure if it drifts.)"""
    from jasper.voice_daemon import _LEG_DB
    from jasper.wake_legs import wake_input_legs

    missing = {leg.token for leg in wake_input_legs()} - set(_LEG_DB)
    assert not missing, f"wake legs missing _LEG_DB mapping: {sorted(missing)}"


# ---------------------------------------------------------------------------
# _configured_wake_legs — the pure leg-selection decision (0.3)
#
# run()'s AsyncExitStack wiring is not hardware-free-testable (it opens
# real mics), so the *decision* of which legs to build is factored into
# this pure function and covered here. The mic-open + lifecycle layer on
# top is exercised by the Pi smoke-test.
# ---------------------------------------------------------------------------


def _cfg(
    mic_device="udp:9876",
    mic_device_raw="",
    mic_device_dtln="",
    mic_device_chip_aec_150="",
    mic_device_chip_aec_210="",
    local_mic_present=None,
    manual_mic_sources=None,
):
    """Minimal Config stand-in for _configured_wake_legs (which reads each
    wake-input leg's device attr by name). SimpleNamespace, not MagicMock —
    a MagicMock's auto-created attrs are truthy and would defeat the
    empty-string gating the function under test relies on."""
    from types import SimpleNamespace
    return SimpleNamespace(
        mic_device=mic_device,
        mic_device_raw=mic_device_raw,
        mic_device_dtln=mic_device_dtln,
        mic_device_chip_aec_150=mic_device_chip_aec_150,
        mic_device_chip_aec_210=mic_device_chip_aec_210,
        local_mic_present=local_mic_present,
        manual_mic_sources=manual_mic_sources or {},
    )


@pytest.mark.parametrize(
    ("cfg_kwargs", "expected"),
    [
        pytest.param(
            {"mic_device": "Array"},
            [("on", "Array")],
            id="configured_wake_legs_single_stream",
        ),
        pytest.param(
            {"mic_device": "udp:9876", "mic_device_raw": "udp:9877"},
            [("on", "udp:9876"), ("off", "udp:9877")],
            id="configured_wake_legs_dual_stream",
        ),
        pytest.param(
            {
                "mic_device": "udp:9876",
                "mic_device_raw": "udp:9877",
                "mic_device_dtln": "udp:9878",
            },
            [("on", "udp:9876"), ("off", "udp:9877"), ("dtln", "udp:9878")],
            id="configured_wake_legs_triple_stream",
        ),
        pytest.param(
            {
                "mic_device": "udp:9876",
                "mic_device_chip_aec_150": "udp:9887",
                "mic_device_chip_aec_210": "udp:9888",
            },
            [
                ("on", "udp:9876"),
                ("chip_aec_150", "udp:9887"),
                ("chip_aec_210", "udp:9888"),
            ],
            id="configured_wake_legs_chip_legs_built_when_set",
        ),
    ],
)
def test_configured_wake_legs(cfg_kwargs, expected):
    """Each leg is built, with its device, exactly when its device var
    is set: "on" alone for a bare primary device; software (off, dtln)
    or chip-beam legs join it as their vars are set."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(**cfg_kwargs))
    assert [(s.token, dev) for s, dev in legs] == expected


@pytest.mark.parametrize(
    ("cfg_kwargs", "expected_tokens"),
    [
        pytest.param(
            {
                "mic_device": "udp:9876",
                "mic_device_raw": "",
                "mic_device_dtln": "udp:9878",
            },
            ["on", "dtln"],
            id="configured_wake_legs_independent_gating",
        ),
        pytest.param(
            {"mic_device": ""},
            ["on"],
            id="configured_wake_legs_primary_always_present",
        ),
        pytest.param(
            {"mic_device": "udp:9876", "mic_device_chip_aec_150": "udp:9887"},
            ["on", "chip_aec_150"],
            id="configured_wake_legs_chip_beams_gate_independently",
        ),
    ],
)
def test_configured_wake_legs_tokens_only(cfg_kwargs, expected_tokens):
    """Optional legs gate independently — voice never opens a UDP
    listener for an unconfigured leg. "on" is always present, even with
    an empty device (the AEC reconciler owns making it real, or parking
    voice), so `self._legs["on"]` never KeyErrors."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(**cfg_kwargs))
    assert [s.token for s, _ in legs] == expected_tokens


def test_configured_wake_legs_chip_legs_not_built_when_unset():
    """Byte-identical-when-off proof for the chip-AEC promotion: with the
    chip device vars empty (the default), the chip legs are NOT built — so
    an install that hasn't opted in opens no chip UDP listener and the
    configured leg set is exactly the pre-promotion software legs."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="udp:9876", mic_device_raw="udp:9877",
        mic_device_dtln="udp:9878",
    ))
    tokens = [s.token for s, _ in legs]
    assert tokens == ["on", "off", "dtln"]
    assert "chip_aec_150" not in tokens
    assert "chip_aec_210" not in tokens


def test_leg_device_attr_covers_all_wake_input_legs():
    """Every wake-input leg must have a _LEG_DEVICE_ATTR entry, or
    _configured_wake_legs would KeyError at daemon startup."""
    from jasper.voice_daemon import _LEG_DEVICE_ATTR
    from jasper.wake_legs import wake_input_legs
    missing = {leg.token for leg in wake_input_legs()} - set(_LEG_DEVICE_ATTR)
    assert not missing, (
        f"wake legs missing _LEG_DEVICE_ATTR: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Push-to-talk-only speakers — no microphone of their own (today: a
# full-profile box whose mic is unplugged or never fitted, plus a WiiM
# Remote 2). Issue #2205: the start gate already lets these boxes run; these
# pin that the daemon then plans an input set it can actually open, and that
# a mic-BEARING speaker is never downgraded into the same shape by accident.
# ---------------------------------------------------------------------------


def test_no_local_mic_plus_accessory_plans_zero_wake_legs():
    """The published no-local-mic verdict + a published accessory source is
    the one shape that drops the primary leg.

    Without this the "on" leg is built against a card that is not there,
    run() re-raises InputDeviceUnavailable, and the daemon exits 66 before it
    ever reaches the manual-mic loop — the gate opens and the remote's button
    still does nothing.
    """
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="Array",
        local_mic_present=False,
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    ))
    assert legs == []


def test_leg_planner_never_infers_push_to_talk_from_an_empty_mic_device():
    """An empty or odd `JASPER_MIC_DEVICE` must NOT be read as "this is a
    push-to-talk speaker".

    `Config.from_env` defaults `mic_device` to the literal "Array" and the AEC
    reconciler writes a real candidate name on its no-mic paths to clear a
    stale udp: device, so "empty primary device" is not evidence of anything
    on a real box. Only the reconciler's published verdict may drop the leg.
    """
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="",
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    ))
    assert [(s.token, dev) for s, dev in legs] == [("on", "")]


def test_unresolved_local_mic_still_plans_the_primary_leg():
    """`unknown` (custom device, or no reconcile has run) is NOT `absent`.

    This is the property that keeps "this speaker has no room mic" separable
    from "the room mic should be here and isn't". Collapse them and a broken
    mic silently downgrades to push-to-talk on a box with no remote — a
    speaker that looks healthy and cannot hear.
    """
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="UMIK-2",
        local_mic_present=None,
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    ))
    assert [(s.token, dev) for s, dev in legs] == [("on", "UMIK-2")]


def test_no_local_mic_without_an_accessory_still_plans_the_primary_leg():
    """No local mic AND no accessory is a BROKEN speaker, not a PTT one.

    The gate marker should have parked it before Python ran; if it somehow
    starts, the planned leg fails to open and the daemon parks loudly on
    exit 66 rather than idling deaf with no wake detection.
    """
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="Array", local_mic_present=False,
    ))
    assert [(s.token, dev) for s, dev in legs] == [("on", "Array")]


def test_accessory_alongside_a_real_mic_keeps_wake_legs():
    """A push-to-talk remote on a speaker that DOES have a mic is additive:
    it adds a manual source without disabling wake detection."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="udp:9876",
        mic_device_raw="udp:9877",
        local_mic_present=True,
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    ))
    assert [(s.token, dev) for s, dev in legs] == [
        ("on", "udp:9876"), ("off", "udp:9877"),
    ]

