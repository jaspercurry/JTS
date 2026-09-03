# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor audio domain."""

import grp
import os
import subprocess
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest


from jasper.camilla import CamillaUnavailable
from jasper.cli import doctor
from jasper.cli.doctor import audio
from jasper.mic_presence import MicPresence
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
    write_state as write_output_hardware_state,
)


from ._sounddevice_stub import stub_sounddevice
from .doctor_test_support import (
    _fresh_cfg,
    record_active_dac,
)


def _lsusb_only(stdout: str):
    def fake_run(cmd, *args, **kwargs):
        if cmd == ["lsusb"]:
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        raise AssertionError(cmd)
    return fake_run


def test_apple_dongle_check_never_assumes_apple_without_a_record(monkeypatch):
    """No record and no Apple chip on USB is not an Apple box — whatever the
    env publication says (the doctor used to default to the Apple dongle)."""
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", "apple_usb_c_dongle")
    monkeypatch.setattr(
        doctor.audio, "_run", _lsusb_only("Bus 001 Device 002: ID 1d6b:0002 hub\n")
    )

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"
    assert "no Apple dongle on USB" in result.detail


def test_apple_dongle_check_warns_when_the_chip_is_on_usb_but_no_card_enumerated(
    monkeypatch,
):
    """A dongle with nothing in its jack is a USB device and no audio card, so
    the record names no DAC; the bus is the only place the dongle shows."""
    monkeypatch.setattr(
        doctor.audio, "_run", _lsusb_only("Bus 001 Device 002: ID 05ac:110a Apple\n")
    )

    result = doctor.check_apple_dongle_audio()

    assert result.status == "warn"
    assert "3.5mm" in result.detail


def test_apple_dongle_check_skips_for_non_apple_output_dac(monkeypatch):
    def fail_probe(*_args, **_kwargs):
        raise AssertionError("Apple USB probe should not run")

    record_active_dac("hifiberry_dac8x")
    monkeypatch.setattr(doctor.audio, "_run", fail_probe)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"
    assert "active output DAC is hifiberry_dac8x" in result.detail


def test_apple_dongle_check_matches_usb_id_case_insensitively(monkeypatch):
    calls = []

    record_active_dac("apple_usb_c_dongle", card_id="Apple")

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd == ["lsusb"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Bus 001 Device 002: ID 05AC:110A Apple\n",
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"
    # The audio card comes from the reconciler's record, never a second probe.
    assert calls == [["lsusb"]]


def test_apple_dongle_check_reads_usb_id_from_active_profile(monkeypatch):
    record_active_dac("apple_usb_c_dongle", card_id="Apple")
    monkeypatch.setattr(
        doctor.audio,
        "_dac_profile_for",
        lambda _profile_id: SimpleNamespace(usb_ids=("1234:abcd",)),
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd == ["lsusb"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Bus 001 Device 002: ID 1234:ABCD Test adapter\n",
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"


def test_apple_dongle_check_ok_for_a_partial_record_naming_its_card(monkeypatch):
    """This check reads observed hardware, not whether the reconciler drives
    it: a partial record still names the card it saw, so a parked single
    dongle with an analog load reports ok, not the 'no audio card' warn."""
    record_active_dac("apple_usb_c_dongle", card_id="A", status="partial")
    monkeypatch.setattr(
        doctor.audio, "_run", _lsusb_only("Bus 001 Device 002: ID 05ac:110a Apple\n")
    )

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"
    assert "A" in result.detail


def test_dual_apple_dongle_check_requires_two_audio_cards(monkeypatch):
    state = OutputHardwareState(
        profile_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        profile_label="Dual Apple USB-C DAC 4-channel pair",
        status="partial",
        physical_output_count=4,
        apple_dac_count=1,
        child_devices=(
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            ),
        ),
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd == ["lsusb"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Bus 001 Device 002: ID 05ac:110a Apple USB-C\n"
                    "Bus 001 Device 003: ID 05ac:110a Apple USB-C\n"
                ),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_load_output_hardware_state", lambda: state)
    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "warn"
    assert "only 1 Apple audio card(s)" in result.detail


def test_active_speaker_hardware_mismatch_is_separate_from_basic_output_health(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, save_output_topology
    from jasper.output_topology import OutputTopology

    topology = OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "dual_apple_pair",
            "name": "Dual Apple active pair",
            "status": "draft",
            "hardware": {
                "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "child_devices": [
                    {
                        "child_id": "left_dac",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "DWH53530FHL2FN3AC",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "right_dac",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "DWH53530FLL2FN3A3",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [
                {
                    "id": "left",
                    "label": "Left speaker",
                    "kind": "left",
                    "mode": "active_2_way",
                    "channels": [
                        {"role": "woofer", "physical_output_index": 0},
                        {
                            "role": "tweeter",
                            "physical_output_index": 1,
                            "startup_muted": True,
                            "protection_required": True,
                            "protection_status": "present",
                        },
                    ],
                },
                {
                    "id": "right",
                    "label": "Right speaker",
                    "kind": "right",
                    "mode": "active_2_way",
                    "channels": [
                        {"role": "woofer", "physical_output_index": 2},
                        {
                            "role": "tweeter",
                            "physical_output_index": 3,
                            "startup_muted": True,
                            "protection_required": True,
                            "protection_status": "present",
                        },
                    ],
                },
            ],
            "routing": {
                "main_left_group_id": "left",
                "main_right_group_id": "right",
            },
        }
    )
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state = OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
        selected_card_id="A",
        selected_pcm="hw:CARD=A,DEV=0",
        apple_dac_count=1,
        child_devices=(
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FHL2FN3AC",
            ),
        ),
    )
    monkeypatch.setattr(doctor.audio, "_load_output_hardware_state", lambda: state)

    output = doctor.check_output_hardware_state()
    active = doctor.check_active_speaker_output_hardware_match()

    assert output.status == "ok"
    assert "profile=apple_usb_c_dongle status=ready outputs=2" in output.detail
    assert active.status == "fail"
    assert f"saved={DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID}" in active.detail
    assert f"current={APPLE_USB_C_DONGLE_DEVICE_ID} status=ready" in active.detail
    assert "active speaker actions are blocked" in active.detail
    assert "Basic output hardware is reported separately" in active.detail


def test_active_speaker_hardware_match_checks_dual_apple_child_serials(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
    from jasper.output_topology import save_output_topology

    topology = OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "dual_apple_pair",
            "name": "Dual Apple active pair",
            "status": "draft",
            "hardware": {
                "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "child_devices": [
                    {
                        "child_id": "left_dac",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "DWH53530FHL2FN3AC",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "right_dac",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "DWH53530FLL2FN3A3",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [
                {
                    "id": "left",
                    "label": "Left speaker",
                    "kind": "left",
                    "mode": "active_2_way",
                    "channels": [
                        {"role": "woofer", "physical_output_index": 0},
                        {
                            "role": "tweeter",
                            "physical_output_index": 1,
                            "startup_muted": True,
                            "protection_required": True,
                            "protection_status": "present",
                        },
                    ],
                },
                {
                    "id": "right",
                    "label": "Right speaker",
                    "kind": "right",
                    "mode": "active_2_way",
                    "channels": [
                        {"role": "woofer", "physical_output_index": 2},
                        {
                            "role": "tweeter",
                            "physical_output_index": 3,
                            "startup_muted": True,
                            "protection_required": True,
                            "protection_status": "present",
                        },
                    ],
                },
            ],
            "routing": {
                "main_left_group_id": "left",
                "main_right_group_id": "right",
            },
        }
    )
    topology_path = tmp_path / "output_topology.json"
    hardware_path = tmp_path / "output_hardware.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(hardware_path))
    write_output_hardware_state(
        classify_output_cards(
            [
                OutputCardFact(
                    card_id="A",
                    device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                    serial="WRONGLEFTSERIAL",
                    usb_path="usb1/1-2",
                    busnum="1",
                    controller="xhci-hcd.0",
                    endpoint_sync="SYNC",
                ),
                OutputCardFact(
                    card_id="A_1",
                    device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                    serial="WRONGRIGHTSERIAL",
                    usb_path="usb1/1-1",
                    busnum="1",
                    controller="xhci-hcd.0",
                    endpoint_sync="SYNC",
                ),
            ]
        ),
        path=hardware_path,
    )

    output = doctor.check_output_hardware_state()
    active = doctor.check_active_speaker_output_hardware_match()

    assert output.status == "ok"
    assert (
        f"profile={DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID} status=ready outputs=4"
        in output.detail
    )
    assert active.status == "fail"
    assert f"saved={DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID}" in active.detail
    assert f"current={DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID} status=ready" in active.detail
    assert (
        "current-hardware clock blockers=dual_apple_observed_serial_mismatch"
        in active.detail
    )
    assert "active speaker actions are blocked" in active.detail
    assert "Basic output hardware is reported separately" in active.detail


def test_output_hardware_state_surfaces_the_declared_final_edge(monkeypatch):
    # The final edge is the electrical fact the chip-AEC alignment identity is
    # commissioned against, so the operator-facing line must name it. Read from
    # the reconciler-emitted env (env_load sources outputd.env), never
    # re-derived from the registry — a drift between the two is the thing this
    # line exists to expose.
    state = OutputHardwareState(
        profile_id="innomaker_hifi_amp_pro",
        profile_label="InnoMaker HiFi AMP Pro",
        status="ready",
        physical_output_count=2,
    )
    monkeypatch.setattr(doctor.audio, "_load_output_hardware_state", lambda: state)

    monkeypatch.setenv("JASPER_OUTPUTD_DAC_FORMAT", "S32_LE")
    assert "final_edge=S32_LE (declared)" in (
        doctor.check_output_hardware_state().detail
    )

    # Unset (a box predating the emit) and explicit-empty (the reconciler's own
    # value for an unrecognized DAC) both mean the historical S16_LE edge —
    # the same resolution outputd's own parse applies.
    monkeypatch.setenv("JASPER_OUTPUTD_DAC_FORMAT", "")
    assert "final_edge=S16_LE (declared)" in (
        doctor.check_output_hardware_state().detail
    )
    monkeypatch.delenv("JASPER_OUTPUTD_DAC_FORMAT", raising=False)
    assert "final_edge=S16_LE (declared)" in (
        doctor.check_output_hardware_state().detail
    )


# ------------------------------------------------ ALSA shorthand mic lookup


def test_extract_card_name_returns_none_for_shorthand():
    assert doctor._extract_card_name("hw:7,1") is None
    assert doctor._extract_card_name("plughw:0,0") is None


def test_extract_card_name_named_card_passthrough():
    assert doctor._extract_card_name("Array") == "Array"
    assert doctor._extract_card_name("plughw:CARD=Loopback") == "Loopback"


def test_check_arecord_l_card_device_match():
    """Mock arecord -l output for a 6-card system that includes the
    LoopbackAEC bridge target (card 7, device 1)."""
    fake_output = (
        "card 0: dongle [USB Audio], device 0: USB Audio [USB Audio]\n"
        "card 1: Array [XVF3800 Voice Capture], device 0: USB Audio\n"
        "card 6: Loopback [Loopback], device 0: Loopback PCM\n"
        "card 6: Loopback [Loopback], device 1: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 0: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n"
    )
    with (
        patch.object(
            doctor.audio,
            "_run",
            return_value=type(
                "FakeProc", (), {"stdout": fake_output, "returncode": 0}
            )(),
        ),
        patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"),
    ):
        assert doctor._check_arecord_l_card_device(7, 1) is True
        assert doctor._check_arecord_l_card_device(7, 0) is True
        assert doctor._check_arecord_l_card_device(99, 0) is False


def test_check_arecord_l_does_not_match_wrong_card():
    """`device 1:` paired with card 6 must NOT satisfy a query for
    card 7 device 1 — both numbers must come from the same line."""
    fake_output = (
        "card 6: Loopback [Loopback], device 1: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 0: Loopback PCM\n"
    )
    with (
        patch.object(
            doctor.audio,
            "_run",
            return_value=type(
                "FakeProc", (), {"stdout": fake_output, "returncode": 0}
            )(),
        ),
        patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"),
    ):
        assert doctor._check_arecord_l_card_device(7, 1) is False


def test_check_mic_card_routes_shorthand_through_arecord_l(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    fake_output = "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n"
    with (
        patch.object(
            doctor.audio,
            "_run",
            return_value=type(
                "FakeProc", (), {"stdout": fake_output, "returncode": 0}
            )(),
        ),
        patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"),
    ):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == "ok"
    assert "card 7 device 1 present" in r.detail


def test_check_mic_capture_falls_back_to_daemon_active(monkeypatch):
    """When PortAudio refuses to open the mic AND jasper-voice is
    running, the check returns ok with a 'daemon holds device' note
    instead of a spurious fail. This is the snd-aloop / AEC bridge
    case where the daemon owns the capture handle exclusively."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )

    class FakeSD:
        def rec(self, *a, **kw):
            raise ValueError("No input device matching 'hw:7,1'")

    stub_sounddevice(monkeypatch, FakeSD())
    with patch.object(doctor.audio, "_jasper_voice_active", return_value=True):
        r = doctor.check_mic_capture(cfg)
    assert r.status == "ok"
    assert "skipped" in r.detail
    assert "jasper-voice holds" in r.detail


def test_check_mic_capture_fails_hard_when_daemon_inactive(monkeypatch):
    """If jasper-voice ISN'T running and the open still fails, the
    fail is real — the device is missing or misconfigured."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )

    class FakeSD:
        def rec(self, *a, **kw):
            raise ValueError("No input device matching 'hw:7,1'")

    stub_sounddevice(monkeypatch, FakeSD())
    with patch.object(doctor.audio, "_jasper_voice_active", return_value=False):
        r = doctor.check_mic_capture(cfg)
    assert r.status == "fail"


def test_check_mic_card_shorthand_failure_actionable(monkeypatch):
    """When the shorthand points at a card/device that's missing, the
    failure detail must mention the AEC bridge — that's the most
    common cause (bridge disabled but JASPER_MIC_DEVICE still set)."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    fake_output = "card 0: dongle [USB Audio], device 0: USB Audio\n"
    with (
        patch.object(
            doctor.audio,
            "_run",
            return_value=type(
                "FakeProc", (), {"stdout": fake_output, "returncode": 0}
            )(),
        ),
        patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"),
    ):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == "fail"
    assert "AEC bridge" in r.detail


# ---- check_dac_usb_sync_mode (Stage 6 clock-coherence advisory) -------------


def _sync_mode_state(*syncs):
    """An OutputHardwareState with one Apple playback child per sync tag."""
    return OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
        apple_dac_count=len(syncs),
        child_devices=tuple(
            OutputCardFact(
                card_id=f"A{i or ''}",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                endpoint_sync=tag,
                has_playback=True,
            )
            for i, tag in enumerate(syncs)
        ),
    )


_I2S_STATE = OutputHardwareState(
    profile_id="hifiberry_dac8x",
    profile_label="HiFiBerry DAC8x",
    status="ready",
    physical_output_count=8,
    child_devices=(
        OutputCardFact(
            card_id="DAC8x",
            device_id="hifiberry_dac8x",
            endpoint_sync=None,
            has_playback=True,
        ),
    ),
)

# A partial record — e.g. a saved topology mismatch (apply_saved_topology_policy)
# — still NAMES this I2S DAC; the check asks what hardware was observed, not
# whether the reconciler is driving it.
_I2S_STATE_PARTIAL = OutputHardwareState(
    profile_id="hifiberry_dac8x",
    profile_label="HiFiBerry DAC8x",
    status="partial",
    physical_output_count=8,
    child_devices=(
        OutputCardFact(
            card_id="DAC8x",
            device_id="hifiberry_dac8x",
            endpoint_sync=None,
            has_playback=True,
        ),
    ),
)


def test_dac_sync_mode_skips_before_probing_when_no_xvf_mic(monkeypatch):
    """Chip-AEC is moot without the mic, so the output probe must not run."""
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: False)
    monkeypatch.setattr(
        doctor.audio,
        "_output_hardware_state_or_none",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe")),
    )

    assert doctor.check_dac_usb_sync_mode().status == "ok"


@pytest.mark.parametrize(
    "state, status, must_name",
    [
        (_sync_mode_state("SYNC"), "ok", "synchronous USB playback endpoint"),
        (_sync_mode_state("ADAPTIVE"), "ok", "synchronous USB playback endpoint"),
        (_sync_mode_state("ASYNC"), "warn", "async USB playback endpoint"),
        # A HiFiBerry/I2S HAT is a known profile with no USB endpoint sync tag.
        (_I2S_STATE, "ok", "I2S clock slave"),
        # A partial record still names the I2S DAC it observed, so this stays
        # the ok/I2S branch rather than falling through to "profile is unknown".
        (_I2S_STATE_PARTIAL, "ok", "I2S clock slave"),
        (None, "warn", "output hardware state unavailable"),
    ],
    ids=["sync", "adaptive", "async", "i2s", "i2s-partial", "state-unavailable"],
)
def test_check_dac_usb_sync_mode_verdicts(monkeypatch, state, status, must_name):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio, "_output_hardware_state_or_none", lambda: state
    )

    result = doctor.check_dac_usb_sync_mode()

    assert result.status == status
    assert must_name in result.detail


def test_check_dac_usb_sync_mode_stays_advisory_about_qualification(monkeypatch):
    """Neither endpoint sync nor the diagnostic SRO verdict authorizes
    production — the fixed DAC profile does."""
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio,
        "_output_hardware_state_or_none",
        lambda: _sync_mode_state("ASYNC"),
    )

    assert (
        "fixed DAC-profile qualification"
        in doctor.check_dac_usb_sync_mode().detail
    )


# ============================== microphone coherence ==========================
#
# A confirmed-absent microphone must yield exactly one yellow `microphone`
# headline and zero red failures: the downstream `mic ALSA card` and
# `mic capture` checks defer to jasper.mic_presence instead of independently
# re-probing ALSA and contradicting it. The reader itself is covered
# hardware-free in tests/test_mic_presence.py.


_CFG = types.SimpleNamespace(
    mic_device="Array", mic_capture_rate=16000, mic_capture_channels=1
)


def _absent() -> MicPresence:
    return MicPresence(present=False, reason="No supported XVF3800 ALSA card detected")


def _present() -> MicPresence:
    return MicPresence(present=True, is_xvf=True, alsa_card="Array", capture_channels=6)


def _present_non_xvf() -> MicPresence:
    # A custom/non-XVF mic is up: present, but no XVF enrichment.
    return MicPresence(present=True)


@pytest.fixture(autouse=True)
def _not_bonded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the bonded-follower short-circuit out of the way — we're exercising
    # the mic-absence path specifically.
    monkeypatch.setattr(audio, "_parked_as_bonded_follower", lambda: False)


def test_headline_absent_is_one_yellow_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "read_mic_presence", _absent)
    r = audio.check_microphone()
    assert r.name == "microphone"
    assert r.status == "warn"  # the single flag — never a red fail
    assert "input unavailable" in r.detail
    assert "No supported XVF3800 ALSA card detected" in r.detail


def test_headline_present_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "read_mic_presence", _present)
    r = audio.check_microphone()
    assert r.status == "ok"
    assert "present" in r.detail


def test_headline_non_xvf_present_is_ok_not_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1 guard at the headline: a present non-XVF mic must read as present,
    never "not detected" (it has no XVF enrichment, but it IS a microphone)."""
    monkeypatch.setattr(audio, "read_mic_presence", _present_non_xvf)
    r = audio.check_microphone()
    assert r.status == "ok"
    assert "not detected" not in r.detail


def test_card_and_capture_defer_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "read_mic_presence", _absent)
    card = audio.check_mic_card_matches_config(_CFG)
    cap = audio.check_mic_capture(_CFG)
    # Both defer to the headline — expected idle, never a red failure.
    assert card.status == "ok"
    assert "microphone" in card.detail
    assert cap.status == "ok"


def test_absent_mic_is_one_flag_zero_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the cleanup: no mic == one warn, never a cascade."""
    monkeypatch.setattr(audio, "read_mic_presence", _absent)
    results = [
        audio.check_microphone(),
        audio.check_mic_card_matches_config(_CFG),
        audio.check_mic_capture(_CFG),
    ]
    statuses = [r.status for r in results]
    assert statuses.count("fail") == 0
    assert statuses.count("warn") == 1


# --- push-to-talk-only box (issue #2205) -------------------------------------

def _push_to_talk_only() -> MicPresence:
    """Gate open (a remote is paired), but no local mic to probe."""
    return MicPresence(present=True, accessory_sources=("wiim_remote_2",))


def test_push_to_talk_box_reads_as_advisory_not_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this PR must not ship: opening the gate for an accessory
    makes the local-device checks actually probe, and on a box with no local mic
    they would find nothing and go RED. A box whose only microphone is a paired
    remote must not look broken."""
    monkeypatch.setattr(audio, "read_mic_presence", _push_to_talk_only)
    monkeypatch.setattr(
        audio, "check_alsa_card",
        lambda *a, **k: audio.CheckResult("mic ALSA card (Array)", "fail", "absent"),
    )
    card = audio.check_mic_card_matches_config(_CFG)
    assert card.status == "warn"
    assert "wiim_remote_2" in card.detail
    # GATE state, not runtime state. The daemon half of issue #2205 has landed,
    # so such a box can answer — but this check never looks at the daemon, so
    # it still must not claim voice is running on the accessory.
    assert "the voice-input gate is open for it" in card.detail
    assert "#2205" in card.detail
    assert "runs push-to-talk" not in card.detail
    # The local finding is preserved, not swallowed.
    assert "absent" in card.detail


def test_push_to_talk_box_is_distinguishable_from_a_working_local_mic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator must be able to tell the two apart. The headline names the
    accessory and never claims "present"; the local probe says which half is
    actually there."""
    monkeypatch.setattr(audio, "read_mic_presence", _push_to_talk_only)
    headline = audio.check_microphone()
    assert headline.status == "ok"
    assert "push-to-talk accessory paired: wiim_remote_2" in headline.detail
    assert not headline.detail.startswith("present")
    # Never a present-tense claim about a daemon this check does not look at.
    assert "runs" not in headline.detail

    monkeypatch.setattr(audio, "read_mic_presence", _present)
    assert "push-to-talk" not in audio.check_microphone().detail


def test_soften_never_upgrades_a_passing_or_warning_result() -> None:
    presence = _push_to_talk_only()
    for status in ("ok", "warn"):
        original = audio.CheckResult("mic capture", status, "detail")
        assert audio._soften_for_push_to_talk(original, presence) is original


def test_recorded_silence_stays_a_failure_even_with_an_accessory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope guard on the softening: a mic that OPENS but records silence is a
    present-and-broken local mic, not an absent one. Calling that
    "no local microphone" would be a lie, and hiding it behind an accessory
    would let a muted array go unnoticed."""
    monkeypatch.setattr(audio, "read_mic_presence", _push_to_talk_only)
    rec = types.SimpleNamespace()

    class _FakeSd:
        @staticmethod
        def rec(*_a: object, **_k: object) -> object:
            return rec

    class _FakeNp:
        @staticmethod
        def abs(_x: object) -> object:
            return types.SimpleNamespace(max=lambda: 0)

    stub_sounddevice(monkeypatch, _FakeSd)
    monkeypatch.setitem(__import__("sys").modules, "numpy", _FakeNp)
    result = audio.check_mic_capture(_CFG)
    assert result.status == "fail"
    assert "recorded silence" in result.detail
    assert "push-to-talk" not in result.detail


def test_soften_leaves_failures_alone_without_an_accessory() -> None:
    """No accessory means a missing local mic really is the whole story — the
    existing red failure must survive untouched."""
    original = audio.CheckResult("mic capture", "fail", "Array: no such device")
    assert audio._soften_for_push_to_talk(original, _present_non_xvf()) is original


# --- every softening CALL SITE softens, not just the function ----------------
#
# Testing `_soften_for_push_to_talk` directly proves the function behaves; it
# says nothing about whether each place that should call it does. Two of the
# three sites went unguarded for exactly that reason. `check_mic_capture`'s is
# the expensive one: on a push-to-talk box the `_jasper_voice_active()`
# short-circuit above it does not fire (voice exits 66), so a silent revert
# there flips jasper-doctor from exit 0 to exit 1 on a correct speaker.

def _drive_hw_shorthand_site(monkeypatch: pytest.MonkeyPatch):
    """check_mic_card_matches_config, positional `hw:N,M` branch."""
    monkeypatch.setattr(audio, "_check_arecord_l_card_device", lambda *a: False)
    cfg = types.SimpleNamespace(
        mic_device="hw:7,1", mic_capture_rate=16000, mic_capture_channels=1,
    )
    return audio.check_mic_card_matches_config(cfg)


def _drive_card_name_site(monkeypatch: pytest.MonkeyPatch):
    """check_mic_card_matches_config, named-card branch."""
    monkeypatch.setattr(
        audio, "check_alsa_card",
        lambda *a, **k: audio.CheckResult("mic ALSA card (Array)", "fail", "absent"),
    )
    return audio.check_mic_card_matches_config(_CFG)


def _drive_capture_open_failure_site(monkeypatch: pytest.MonkeyPatch):
    """check_mic_capture, device-open-failure branch (the live one on a
    push-to-talk box: voice is not holding the mic, it exited 66)."""
    monkeypatch.setattr(audio, "_jasper_voice_active", lambda: False)

    class _FakeSd:
        @staticmethod
        def rec(*_a: object, **_k: object) -> object:
            raise OSError("no such device")

    stub_sounddevice(monkeypatch, _FakeSd)
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace())
    return audio.check_mic_capture(_CFG)


@pytest.mark.parametrize(
    "scenario",
    [
        _drive_hw_shorthand_site,
        _drive_card_name_site,
        _drive_capture_open_failure_site,
    ],
    ids=lambda fn: fn.__name__,
)
def test_every_soften_call_site_still_softens(
    monkeypatch: pytest.MonkeyPatch, scenario
) -> None:
    monkeypatch.setattr(audio, "read_mic_presence", _push_to_talk_only)

    result = scenario(monkeypatch)

    assert result.status == "warn"
    assert "wiim_remote_2" in result.detail
    assert "#2205" in result.detail


def _local_mic_and_accessory() -> MicPresence:
    """A healthy non-XVF local mic on a box that ALSO has a remote paired."""
    return MicPresence(present=True, accessory_sources=("wiim_remote_2",))


def test_headline_stays_ok_when_a_working_local_mic_also_has_a_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio, "read_mic_presence", _local_mic_and_accessory)

    assert audio.check_microphone().status == "ok"


# ------------------------------------------------ CamillaDSP config dir posture
#
# Pins the jts3 2026-07-06 incident: a deploy left /var/lib/camilladsp/configs
# root-only (setgid kept, group-write stripped — mode 2755), so the non-root
# jasper-web user could not atomically write the staged active-speaker config
# and staging failed with PermissionError, surfacing to the household as
# "could not load the silent active-speaker setup".


def _own_group() -> str:
    return grp.getgrgid(os.getgid()).gr_name


@pytest.mark.parametrize(
    "mode, group, status",
    [
        (0o2775, None, "ok"),
        (0o2755, None, "fail"),  # the exact regression: group-write stripped
        # setgid lost (2775 -> 0775): a root-run process creating a NEW
        # subdirectory later would land it group-root, not group-jasper.
        (0o0775, None, "fail"),
        (0o2775, "jts-no-such-group-xyz", "fail"),
        (None, None, "warn"),  # dir absent
    ],
    ids=["group-writable", "group-readonly", "setgid-lost", "wrong-group", "absent"],
)
def test_camilla_configs_writable_verdicts(tmp_path, mode, group, status):
    d = tmp_path / "configs"
    if mode is not None:
        d.mkdir()
        os.chmod(d, mode)

    res = doctor.audio._camilla_configs_writable_result(
        d, expected_group=group or _own_group()
    )

    assert res.status == status


def test_camilla_configs_writable_targets_the_constant_dir(monkeypatch, tmp_path):
    """The decorated check reads CAMILLA_CONFIGS_DIR, so the guard stays
    pointed at the dir the deploy actually permissions."""
    missing = tmp_path / "nope"
    monkeypatch.setattr(doctor.audio, "CAMILLA_CONFIGS_DIR", missing)

    res = doctor.audio.check_camilla_configs_writable()

    assert res.status == "warn"
    assert str(missing) in res.detail


# ------------------------------------------------------- CamillaDSP websocket


def _camilla_controller(monkeypatch, *, volume, clipped):
    constructed: list[tuple[str, int]] = []

    class Controller:
        def __init__(self, host: str, port: int) -> None:
            constructed.append((host, port))

        async def get_volume_db(self):
            if isinstance(volume, Exception):
                raise volume
            return volume

        async def get_clipped_samples(self):
            if isinstance(clipped, Exception):
                raise clipped
            return clipped

        async def close(self):
            pass

    monkeypatch.setattr(doctor.audio, "CamillaController", Controller)
    return constructed


@pytest.mark.parametrize(
    "volume, clipped, status, must_name",
    [
        (-12.5, 0, "ok", "volume=-12.5 dB clipped_samples=0"),
        (CamillaUnavailable("operation exceeded 5.0s"), 0, "fail", "5.0s"),
        # clipped_samples is optional: an unavailable status command must not
        # sink the probe.
        (
            -18.0,
            CamillaUnavailable("status command unavailable"),
            "ok",
            "volume=-18.0 dB clipped_samples=?",
        ),
    ],
    ids=["healthy", "timeout", "clipped-optional"],
)
async def test_check_camilla_websocket_verdicts(
    monkeypatch, volume, clipped, status, must_name
):
    constructed = _camilla_controller(monkeypatch, volume=volume, clipped=clipped)
    cfg = SimpleNamespace(camilla_host="127.0.0.1", camilla_port=1234)

    result = await doctor.audio.check_camilla_websocket(cfg)

    assert result.status == status
    assert must_name in result.detail
    assert constructed == [("127.0.0.1", 1234)]


# ---------------------------------------------------- installed prerequisites

def test_check_loopback_reports_present_card(monkeypatch) -> None:
    def fake_run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
        assert timeout == 5.0
        assert cmd == ["aplay", "-L"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="null\nhw:CARD=Loopback,DEV=0\n",
            stderr="",
        )

    monkeypatch.setattr(audio, "_run", fake_run)

    result = audio.check_loopback()

    assert result.name == "snd-aloop"
    assert result.status == "ok"
    assert result.detail == "CARD=Loopback present"


def test_check_loopback_reports_missing_card_with_remediation(monkeypatch) -> None:
    def fake_run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
        assert timeout == 5.0
        assert cmd == ["aplay", "-L"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="null\ndefault\n",
            stderr="",
        )

    monkeypatch.setattr(audio, "_run", fake_run)

    result = audio.check_loopback()

    assert result.name == "snd-aloop"
    assert result.status == "fail"
    assert result.detail == (
        "Loopback device missing. `sudo modprobe snd-aloop` or check "
        "/etc/modules-load.d/snd-aloop.conf"
    )
