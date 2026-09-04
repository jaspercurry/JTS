# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor audio domain.

Every assertion pins ``status`` and ``reason`` — never ``detail`` prose
(ADR-0233 rule 3). ``audio.REASON_*`` is the closed vocabulary.
"""

import grp
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import jasper.active_speaker._common as _common
import jasper.active_speaker.setup_status as setup_status_mod
from jasper.camilla import CamillaUnavailable
from jasper.cli import doctor
from jasper.cli.doctor import audio
from jasper.cli.doctor._evidence import evidence
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
from .doctor_test_support import _fresh_cfg, record_active_dac


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

    assert result.status == "skipped"
    assert result.reason == audio.REASON_APPLE_DONGLE_ABSENT


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
    assert result.reason == audio.REASON_APPLE_DONGLE_NO_AUDIO_CARD


def test_apple_dongle_check_skips_for_non_apple_output_dac(monkeypatch):
    def fail_probe(*_args, **_kwargs):
        raise AssertionError("Apple USB probe should not run")

    record_active_dac("hifiberry_dac8x")
    monkeypatch.setattr(doctor.audio, "_run", fail_probe)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "skipped"
    assert result.reason == audio.REASON_APPLE_DONGLE_NOT_APPLICABLE


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
    assert result.reason == ""


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
    assert result.reason == ""


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

    evidence.seed("output_hardware_state", state)
    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "warn"
    assert result.reason == audio.REASON_APPLE_DONGLE_CARDS_MISSING


def _dual_apple_topology():
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

    return OutputTopology.from_mapping(
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


def test_active_speaker_hardware_mismatch_is_separate_from_basic_output_health(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import save_output_topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_dual_apple_topology(), path=topology_path)
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
    evidence.seed("output_hardware_state", state)

    output = doctor.check_output_hardware_state()
    active = doctor.check_active_speaker_output_hardware_match()

    # Basic output health stays green; only the saved-topology check fails.
    assert output.status == "ok"
    assert output.reason == ""
    assert active.status == "fail"
    assert active.reason == audio.REASON_OUTPUT_HARDWARE_MISMATCH


def test_active_speaker_hardware_match_checks_dual_apple_child_serials(
    monkeypatch,
    tmp_path,
):
    """The saved pair is attached, but the observed serials are not the banked
    ones — a clock-domain blocker, distinct from a device_id mismatch."""
    from jasper.output_topology import save_output_topology

    topology_path = tmp_path / "output_topology.json"
    hardware_path = tmp_path / "output_hardware.json"
    save_output_topology(_dual_apple_topology(), path=topology_path)
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
    assert active.status == "fail"
    assert active.reason == audio.REASON_OUTPUT_HARDWARE_CLOCK_BLOCKED


def test_output_hardware_state_warns_without_a_record():
    evidence.seed("output_hardware_state", None)

    result = doctor.check_output_hardware_state()

    assert result.status == "warn"
    assert result.reason == audio.REASON_OUTPUT_HARDWARE_STATE_UNAVAILABLE


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


@pytest.mark.parametrize(
    "arecord_l, status, reason",
    [
        (
            "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n",
            "ok", "",
        ),
        # The shorthand points at a card/device that is gone: the most common
        # cause is the bridge having moved to UDP with JASPER_MIC_DEVICE stale.
        (
            "card 0: dongle [USB Audio], device 0: USB Audio\n",
            "fail", audio.REASON_MIC_CARD_ABSENT,
        ),
    ],
    ids=["present", "absent"],
)
def test_check_mic_card_routes_shorthand_through_arecord_l(
    monkeypatch, arecord_l, status, reason
):
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    with (
        patch.object(
            doctor.audio,
            "_run",
            return_value=type(
                "FakeProc", (), {"stdout": arecord_l, "returncode": 0}
            )(),
        ),
        patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"),
    ):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == status
    assert r.reason == reason


def test_check_mic_capture_falls_back_to_daemon_active(monkeypatch):
    """When PortAudio refuses to open the mic AND jasper-voice is running, the
    daemon owning the capture handle IS the evidence — not a spurious fail."""
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
    assert r.status == "skipped"
    assert r.reason == audio.REASON_MIC_HELD_BY_VOICE


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
    assert r.reason == audio.REASON_MIC_CAPTURE_OPEN_FAILED


# ---- check_dac_usb_sync_mode (clock-coherence advisory) ---------------------


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

    result = doctor.check_dac_usb_sync_mode()

    assert result.status == "skipped"
    assert result.reason == audio.REASON_DAC_SYNC_NOT_APPLICABLE


@pytest.mark.parametrize(
    "state, status, reason",
    [
        (_sync_mode_state("SYNC"), "ok", ""),
        (_sync_mode_state("ADAPTIVE"), "ok", ""),
        # Advisory only: the binding chip-AEC gate is fixed DAC qualification,
        # so an async endpoint warns and never fails.
        (_sync_mode_state("ASYNC"), "warn", audio.REASON_DAC_SYNC_ASYNC),
        # A HiFiBerry/I2S HAT is a known profile with no USB endpoint sync
        # tag, so there is no USB sync mode to classify at all.
        (_I2S_STATE, "skipped", audio.REASON_DAC_SYNC_I2S),
        (_I2S_STATE_PARTIAL, "skipped", audio.REASON_DAC_SYNC_I2S),
        (None, "warn", audio.REASON_OUTPUT_HARDWARE_STATE_UNAVAILABLE),
    ],
    ids=["sync", "adaptive", "async", "i2s", "i2s-partial", "state-unavailable"],
)
def test_check_dac_usb_sync_mode_verdicts(monkeypatch, state, status, reason):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio, "_output_hardware_state_or_none", lambda: state
    )

    result = doctor.check_dac_usb_sync_mode()

    assert result.status == status
    assert result.reason == reason


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


def _push_to_talk_only() -> MicPresence:
    """Gate open (a remote is paired), but no local mic to probe."""
    return MicPresence(present=True, accessory_sources=("wiim_remote_2",))


def _local_mic_and_accessory() -> MicPresence:
    """A healthy non-XVF local mic on a box that ALSO has a remote paired."""
    return MicPresence(present=True, accessory_sources=("wiim_remote_2",))


@pytest.fixture(autouse=True)
def _not_bonded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the bonded-follower short-circuit out of the way — we're exercising
    # the mic-absence path specifically.
    monkeypatch.setattr(audio, "_parked_follower_result", lambda _label: None)


@pytest.mark.parametrize(
    "presence, status, reason",
    [
        (_absent, "warn", audio.REASON_MIC_ABSENT),
        (_present, "ok", ""),
        # B1 guard: a present non-XVF mic must read as present (it has no XVF
        # enrichment, but it IS a microphone).
        (_present_non_xvf, "ok", ""),
        # The headline reads the OR verdict, so an accessory-satisfied gate is
        # `ok`; `mic ALSA card` / `mic capture` are what tell it from a local
        # mic (issue #2205).
        (_push_to_talk_only, "ok", ""),
        (_local_mic_and_accessory, "ok", ""),
    ],
    ids=["absent", "xvf", "non-xvf", "accessory-only", "local-plus-accessory"],
)
def test_microphone_headline_verdicts(
    monkeypatch: pytest.MonkeyPatch, presence, status, reason
) -> None:
    evidence.seed("mic_presence", presence())
    r = audio.check_microphone()
    assert r.name == "microphone"
    assert r.status == status
    assert r.reason == reason


def test_card_and_capture_defer_when_absent() -> None:
    evidence.seed("mic_presence", _absent())
    card = audio.check_mic_card_matches_config(_CFG)
    cap = audio.check_mic_capture(_CFG)
    # Both defer to the headline, which owns the verdict: nothing was probed
    # here, so neither may claim a green tick.
    assert card.status == "skipped"
    assert card.reason == audio.REASON_MIC_ABSENT_DEFERRED
    assert cap.status == "skipped"
    assert cap.reason == audio.REASON_MIC_ABSENT_DEFERRED


def test_absent_mic_is_one_flag_zero_failures() -> None:
    """The whole point of the cleanup: no mic == one warn, never a cascade."""
    evidence.seed("mic_presence", _absent())
    statuses = [
        r.status for r in (
            audio.check_microphone(),
            audio.check_mic_card_matches_config(_CFG),
            audio.check_mic_capture(_CFG),
        )
    ]
    assert statuses.count("fail") == 0
    assert statuses.count("warn") == 1


# --- push-to-talk-only box (issue #2205) -------------------------------------


def test_soften_never_upgrades_a_passing_or_warning_result() -> None:
    presence = _push_to_talk_only()
    for status in ("ok", "warn"):
        original = audio.CheckResult(
            "mic capture", status, "detail",
            reason="" if status == "ok" else "mic_capture_iffy",
        )
        assert audio._soften_for_push_to_talk(original, presence) is original


def test_soften_leaves_failures_alone_without_an_accessory() -> None:
    """No accessory means a missing local mic really is the whole story — the
    existing red failure must survive untouched."""
    original = audio.CheckResult(
        "mic capture", "fail", "Array: no such device",
        reason="mic_capture_missing",
    )
    assert audio._soften_for_push_to_talk(original, _present_non_xvf()) is original


def test_recorded_silence_stays_a_failure_even_with_an_accessory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope guard on the softening: a mic that OPENS but records silence is a
    present-and-broken local mic, not an absent one."""
    evidence.seed("mic_presence", _push_to_talk_only())
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
    monkeypatch.setitem(sys.modules, "numpy", _FakeNp)
    result = audio.check_mic_capture(_CFG)
    assert result.status == "fail"
    assert result.reason == audio.REASON_MIC_CAPTURE_SILENT


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
        lambda *a, **k: audio.CheckResult(
            "mic ALSA card (Array)", "fail", "absent",
            reason=audio.REASON_ALSA_CARD_ABSENT,
        ),
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
    "scenario,probe_reason",
    [
        (_drive_hw_shorthand_site, audio.REASON_MIC_CARD_ABSENT),
        (_drive_card_name_site, audio.REASON_ALSA_CARD_ABSENT),
        (_drive_capture_open_failure_site, audio.REASON_MIC_CAPTURE_OPEN_FAILED),
    ],
    ids=lambda v: getattr(v, "__name__", v),
)
def test_every_soften_call_site_keeps_the_probe_reason(
    monkeypatch: pytest.MonkeyPatch, scenario, probe_reason
) -> None:
    evidence.seed("mic_presence", _push_to_talk_only())

    result = scenario(monkeypatch)

    assert result.status == "warn"
    assert result.reason == probe_reason


# ------------------------------------------------ CamillaDSP config dir posture
#
# Pins the jts3 2026-07-06 incident: a deploy left /var/lib/camilladsp/configs
# root-only (setgid kept, group-write stripped — mode 2755), so the non-root
# jasper-web user could not atomically write the staged active-speaker config
# and staging failed with PermissionError.


def _own_group() -> str:
    return grp.getgrgid(os.getgid()).gr_name


@pytest.mark.parametrize(
    "mode, group, status, reason",
    [
        (0o2775, None, "ok", ""),
        # the exact regression: group-write stripped
        (0o2755, None, "fail", audio.REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE),
        # setgid lost (2775 -> 0775): a root-run process creating a NEW
        # subdirectory later would land it group-root, not group-jasper.
        (0o0775, None, "fail", audio.REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE),
        (
            0o2775, "jts-no-such-group-xyz", "fail",
            audio.REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE,
        ),
        (None, None, "warn", audio.REASON_CAMILLA_CONFIG_DIR_MISSING),
    ],
    ids=["group-writable", "group-readonly", "setgid-lost", "wrong-group", "absent"],
)
def test_camilla_configs_writable_verdicts(tmp_path, mode, group, status, reason):
    d = tmp_path / "configs"
    if mode is not None:
        d.mkdir()
        os.chmod(d, mode)

    res = doctor.audio._camilla_configs_writable_result(
        d, expected_group=group or _own_group()
    )

    assert res.status == status
    assert res.reason == reason


def test_camilla_configs_writable_targets_the_constant_dir(monkeypatch, tmp_path):
    """The decorated check reads CAMILLA_CONFIGS_DIR, so the guard stays
    pointed at the dir the deploy actually permissions."""
    monkeypatch.setattr(doctor.audio, "CAMILLA_CONFIGS_DIR", tmp_path / "nope")

    res = doctor.audio.check_camilla_configs_writable()

    assert res.status == "warn"
    assert res.reason == audio.REASON_CAMILLA_CONFIG_DIR_MISSING


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
    "volume, clipped, status, reason",
    [
        (-12.5, 0, "ok", ""),
        (
            CamillaUnavailable("operation exceeded 5.0s"), 0, "fail",
            audio.REASON_CAMILLA_UNREACHABLE,
        ),
        # clipped_samples is optional: an unavailable status command must not
        # sink the probe.
        (-18.0, CamillaUnavailable("status command unavailable"), "ok", ""),
        # Non-negotiable #1's live half: a fader above the ceiling is a fail.
        (6.0, 0, "fail", audio.REASON_CAMILLA_VOLUME_ABOVE_CEILING),
    ],
    ids=["healthy", "timeout", "clipped-optional", "above-ceiling"],
)
async def test_check_camilla_websocket_verdicts(
    monkeypatch, volume, clipped, status, reason
):
    constructed = _camilla_controller(monkeypatch, volume=volume, clipped=clipped)
    cfg = SimpleNamespace(camilla_host="127.0.0.1", camilla_port=1234)

    result = await doctor.audio.check_camilla_websocket(cfg)

    assert result.status == status
    assert result.reason == reason
    assert constructed == [("127.0.0.1", 1234)]


# ---------------------------------------------------- installed prerequisites


@pytest.mark.parametrize(
    "aplay_l, status, reason",
    [
        ("null\nhw:CARD=Loopback,DEV=0\n", "ok", ""),
        ("null\ndefault\n", "fail", audio.REASON_LOOPBACK_MISSING),
    ],
    ids=["present", "missing"],
)
def test_check_loopback_verdicts(monkeypatch, aplay_l, status, reason) -> None:
    def fake_run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
        assert timeout == 5.0
        assert cmd == ["aplay", "-L"]
        return subprocess.CompletedProcess(cmd, 0, stdout=aplay_l, stderr="")

    monkeypatch.setattr(audio, "_run", fake_run)

    result = audio.check_loopback()

    assert result.name == "snd-aloop"
    assert result.status == status
    assert result.reason == reason


# ------------------------------------------- CamillaDSP volume_limit (NN #1)


def _point_at_config(monkeypatch, tmp_path, text, *, name="v1.yml"):
    config = tmp_path / name
    config.write_text(text)
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    return config


@pytest.mark.parametrize(
    "text, status, reason",
    [
        ("devices:\n  samplerate: 48000\n  volume_limit: 0.0\n", "ok", ""),
        (
            "devices:\n  samplerate: 48000\n", "fail",
            audio.REASON_VOLUME_LIMIT_ABSENT,
        ),
        (
            "devices:\n  samplerate: 48000\n  volume_limit: 6.0\n", "fail",
            audio.REASON_VOLUME_LIMIT_ABOVE_CEILING,
        ),
        # Ambiguous ownership never resolves to "capped": a nested or
        # duplicated key is not the global fader ceiling.
        (
            "devices:\n  playback:\n    volume_limit: 0.0\n", "fail",
            audio.REASON_VOLUME_LIMIT_ABSENT,
        ),
        (
            "devices:\n  volume_limit: 0.0\ndevices: {volume_limit: 9.0}\n", "fail",
            audio.REASON_VOLUME_LIMIT_ABSENT,
        ),
        (
            "devices:\n  volume_limit: 0.0\n  volume_limit: 9.0\n", "fail",
            audio.REASON_VOLUME_LIMIT_ABSENT,
        ),
    ],
    ids=[
        "capped", "omitted", "positive", "nested-only", "duplicate-block",
        "duplicate-key",
    ],
)
def test_check_camilla_volume_limit_verdicts(
    monkeypatch, tmp_path, text, status, reason
):
    _point_at_config(monkeypatch, tmp_path, text)

    r = doctor.check_camilla_volume_limit()

    assert r.status == status
    assert r.reason == reason


def test_check_camilla_volume_limit_fails_on_a_missing_config(monkeypatch, tmp_path):
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {tmp_path / 'gone.yml'}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "fail"
    assert r.reason == audio.REASON_CAMILLA_CONFIG_MISSING


# --------------------------------------------------------- camilla ring chunk


def _stage_ring_config(tmp_path, monkeypatch, chunksize: int, extra: str = "") -> None:
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE

    _point_at_config(
        monkeypatch,
        tmp_path,
        "devices:\n"
        "  samplerate: 48000\n"
        f"  chunksize: {chunksize}\n"
        f"{extra}"
        "  capture:\n"
        "    type: Alsa\n"
        f'    device: "{RING_CAPTURE_DEVICE}"\n'
        "  playback:\n"
        "    type: Alsa\n"
        f'    device: "{RING_PLAYBACK_DEVICE}"\n',
        name="ring.yml",
    )


def test_check_camilla_ring_chunk_fails_over_capacity(monkeypatch, tmp_path):
    """jts4's shape: a chunk the ring cannot open, so the box is silent."""
    from jasper.fanin_coupling import ring_capacity_frames

    _stage_ring_config(tmp_path, monkeypatch, ring_capacity_frames() * 4)

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "fail"
    assert r.reason == audio.REASON_RING_CHUNK_ABOVE_CAPACITY
    assert r.speaker_silent is True


def test_check_camilla_ring_chunk_ok_at_capacity(monkeypatch, tmp_path):
    """jts.local's shape: a floor that exactly fills the ring is fine."""
    from jasper.fanin_coupling import ring_capacity_frames

    _stage_ring_config(tmp_path, monkeypatch, ring_capacity_frames())

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "ok"


def test_check_camilla_ring_chunk_fails_a_target_over_camillas_ceiling(
    monkeypatch, tmp_path
):
    """The state jts4 actually landed in: chunk fits the ring, box still dead.

    256/4096 passes the ring-capacity half and is still refused by CamillaDSP
    (ceiling is chunk x (queuelimit + 4) = 2048), so the box crash-loops with
    the ring half of this check green.
    """
    _stage_ring_config(
        tmp_path, monkeypatch, 256, extra="  queuelimit: 4\n  target_level: 4096\n",
    )

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "fail"
    assert r.reason == audio.REASON_RING_TARGET_LEVEL_ABOVE_CEILING
    assert r.speaker_silent is True


def test_check_camilla_ring_chunk_discloses_the_clamp(monkeypatch, tmp_path):
    """A clamped box says so, so the running chunk is never unexplained.

    A floorless HiFiBerry DAC8x Studio resolves the 1024 default and runs 256.
    Not the InnoMaker: since #3542 it declares the already-clamped 256/1024
    outright, so it no longer takes this path.
    """
    from jasper.fanin_coupling import ring_capacity_frames

    record_active_dac("hifiberry_dac8x_studio")
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    _stage_ring_config(tmp_path, monkeypatch, ring_capacity_frames())

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "ok"
    assert r.reason == audio.REASON_RING_CHUNK_CLAMPED


def test_check_camilla_ring_chunk_not_applicable_off_the_ring(monkeypatch, tmp_path):
    _point_at_config(
        monkeypatch, tmp_path, "devices:\n  samplerate: 48000\n  chunksize: 1024\n",
    )

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "skipped"
    assert r.reason == audio.REASON_RING_CHUNK_NOT_APPLICABLE


# ------------------------------------------------- active speaker runtime graph


def _save_topology(monkeypatch, tmp_path, topology):
    from jasper.output_topology import save_output_topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    return topology_path


def _point_at_topology_and_config(monkeypatch, tmp_path, topology, config_text, name):
    _save_topology(monkeypatch, tmp_path, topology)
    return _point_at_config(monkeypatch, tmp_path, config_text, name=name)


def _blocker_bearing_roleful_topology():
    """A roleful topology whose tweeter has no DAC output assigned."""
    from dataclasses import replace

    from tests.test_active_speaker_runtime_contract import _active_topology

    topology = _active_topology("mono", "active_2_way")
    return replace(
        topology,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    replace(channel, physical_output_index=None)
                    if channel.role == "tweeter"
                    else channel
                    for channel in group.channels
                ),
            )
            for group in topology.speaker_groups
        ),
    )


def _incomplete_passive_topology():
    """A passive stereo layout whose right channels have no DAC output."""
    from dataclasses import replace

    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    complete = _full_range_stereo()
    return replace(
        complete,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    replace(channel, physical_output_index=None)
                    if group.kind == "right"
                    else channel
                    for channel in group.channels
                ),
            )
            for group in complete.speaker_groups
        ),
    )


def _stage_parked(monkeypatch, tmp_path, topology):
    """Point the box at the independently PROVED parked graph for ``topology``."""
    from jasper.active_speaker.runtime_contract import build_parked_muted_graph

    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed, "fixture must stage a proved parked graph"
    _point_at_topology_and_config(
        monkeypatch, tmp_path, topology, text, "active_speaker_parked.yml",
    )


def _stage_corrupt_topology(monkeypatch, tmp_path):
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))


def _stage_complete_passive_layout(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    _save_topology(monkeypatch, tmp_path, _full_range_stereo())


def _stage_unreadable_statefile(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _active_topology

    _save_topology(monkeypatch, tmp_path, _active_topology("mono", "active_2_way"))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "gone-statefile.yml"))


def _stage_missing_config(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _active_topology

    _save_topology(monkeypatch, tmp_path, _active_topology("mono", "active_2_way"))
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {tmp_path / 'gone.yml'}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))


def _stage_unconfigured_parked(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _topology

    _stage_parked(monkeypatch, tmp_path, _topology([]))


def _stage_roleful_parked(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _active_topology

    _stage_parked(monkeypatch, tmp_path, _active_topology("mono", "active_2_way"))


def _stage_blocker_bearing_parked(monkeypatch, tmp_path):
    _stage_parked(monkeypatch, tmp_path, _blocker_bearing_roleful_topology())


def _stage_incomplete_passive_parked(monkeypatch, tmp_path):
    _stage_parked(monkeypatch, tmp_path, _incomplete_passive_topology())


def _stage_flat_graph_without_a_layout(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _flat_yaml, _topology

    _point_at_topology_and_config(
        monkeypatch, tmp_path, _topology([]), _flat_yaml(), "outputd-cutover.yml",
    )


def _stage_flat_graph_on_a_tweeter_layout(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import _active_topology, _flat_yaml

    _point_at_topology_and_config(
        monkeypatch,
        tmp_path,
        _active_topology("mono", "active_2_way"),
        _flat_yaml(),
        "outputd-cutover.yml",
    )


def _stage_staged_active_startup(monkeypatch, tmp_path):
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _active_yaml,
        _staged_metadata,
    )

    topology = _active_topology("mono", "active_2_way")
    config = _point_at_topology_and_config(
        monkeypatch,
        tmp_path,
        topology,
        _active_yaml("mono", 2, frozenset()),
        "active_speaker_staged_startup.yml",
    )
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(json.dumps(_staged_metadata(topology, config)), encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH", str(metadata))


@pytest.mark.parametrize(
    "stage, status, reason, silent",
    [
        (_stage_corrupt_topology, "fail", audio.REASON_TOPOLOGY_UNREADABLE, False),
        (
            _stage_complete_passive_layout, "ok",
            audio.REASON_GRAPH_PASSIVE_LAYOUT, False,
        ),
        (
            _stage_unreadable_statefile, "fail",
            audio.REASON_CAMILLA_STATEFILE_UNREADABLE, False,
        ),
        (_stage_missing_config, "fail", audio.REASON_CAMILLA_CONFIG_MISSING, False),
        (
            _stage_unconfigured_parked, "warn",
            audio.REASON_GRAPH_PARKED_SILENT, True,
        ),
        (_stage_roleful_parked, "warn", audio.REASON_GRAPH_PARKED_SILENT, True),
        (
            _stage_blocker_bearing_parked, "warn",
            audio.REASON_GRAPH_PARKED_SILENT, True,
        ),
        # Parked over an unfixable layout: a `fail` that is also provably
        # silent, which the summary line supports (#2471).
        (
            _stage_incomplete_passive_parked, "fail",
            audio.REASON_GRAPH_LAYOUT_INCOMPLETE, True,
        ),
        (
            _stage_flat_graph_without_a_layout, "fail",
            audio.REASON_GRAPH_UNSAFE, False,
        ),
        (
            _stage_flat_graph_on_a_tweeter_layout, "fail",
            audio.REASON_GRAPH_UNSAFE, False,
        ),
        (_stage_staged_active_startup, "ok", "", False),
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
def test_active_speaker_runtime_graph_branches(
    monkeypatch, tmp_path, stage, status, reason, silent
):
    """One row per outcome of the merged check.

    ``speaker_silent`` rides along because only the parked branch may claim it
    (#2471): an unreadable topology is proof of not knowing, and a legal graph
    is not silence.
    """
    stage(monkeypatch, tmp_path)

    r = doctor.check_active_speaker_runtime_graph()

    assert (r.status, r.reason, r.speaker_silent) == (status, reason, silent)


def test_active_speaker_runtime_graph_names_the_blockers_it_is_parked_over(
    monkeypatch, tmp_path
):
    """Parking is gated on the missing startup graph, not on the blockers, so
    the parked row still has to name the layout blockers a household must clear.
    Asserted on the contract's own blocker CODES, not on the sentence."""
    from jasper.active_speaker.runtime_contract import classify_output_contract

    topology = _blocker_bearing_roleful_topology()
    _stage_parked(monkeypatch, tmp_path, topology)
    codes = [str(issue["code"]) for issue in classify_output_contract(topology).issues]
    assert codes, "fixture must carry at least one topology blocker"

    r = doctor.check_active_speaker_runtime_graph()

    assert r.reason == audio.REASON_GRAPH_PARKED_SILENT
    assert all(code in r.detail for code in codes)


def test_active_speaker_runtime_graph_exits_are_capability_aware(monkeypatch, tmp_path):
    """The doctor detail must resolve the exits through the OWNED helper.

    The one deliberate `detail` assertion in this file: which of two equal-status
    strings the line carries is the whole finding, and no structured field can
    express it. On an ordinary DAC `parked_muted_exits(topology) ==
    PARKED_MUTED_EXITS`, so only a DAC that declares NO active outputd lane
    separates them — there "finish crossover preview" can never succeed, so the
    helper drops it and the bare constant still offers it.
    """
    from jasper.active_speaker.runtime_contract import (
        PARKED_MUTED_EXITS,
        parked_muted_exits,
    )
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
    from tests.active_speaker_fixtures import register_passive_only_dac

    profile = register_passive_only_dac(monkeypatch)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench speaker",
        "status": "draft",
        "hardware": {
            "device_id": profile.id,
            "device_label": profile.label,
            "physical_output_count": profile.physical_output_count,
        },
        "speaker_groups": [{
            "id": "mono",
            "label": "Mono",
            "kind": "mono",
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": 0,
                    "identity_verified": True,
                },
                {
                    # Unassigned -> the topology-level blocker this row names.
                    "role": "tweeter",
                    "physical_output_index": None,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "present",
                },
            ],
        }],
        "routing": {"mono_group_id": "mono"},
    })
    # The fixture is only meaningful if the helper and the constant DISAGREE.
    assert parked_muted_exits(topology) != PARKED_MUTED_EXITS
    _stage_parked(monkeypatch, tmp_path, topology)

    r = doctor.check_active_speaker_runtime_graph()

    assert r.reason == audio.REASON_GRAPH_PARKED_SILENT
    # Follows the helper, and therefore does NOT offer the impossible action.
    assert parked_muted_exits(topology) in r.detail
    assert PARKED_MUTED_EXITS not in r.detail


# ------------------------------------------------------------- sound profile


def test_check_sound_profile_reports_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing.json"))

    r = doctor.check_sound_profile()

    assert r.status == "ok"
    assert r.reason == audio.REASON_SOUND_PROFILE_DEFAULT


_SAVED_SOUND_PROFILE = {
    "enabled": True,
    "curve_id": "harman",
    "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
}


def test_check_sound_profile_warns_when_saved_profile_not_active(
    monkeypatch,
    tmp_path,
):
    profile = tmp_path / "sound_profile.json"
    profile.write_text(json.dumps(_SAVED_SOUND_PROFILE))
    _point_at_config(monkeypatch, tmp_path, "# base\n")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = doctor.check_sound_profile()

    assert r.status == "warn"
    assert r.reason == audio.REASON_SOUND_PROFILE_NOT_ACTIVE


@pytest.mark.parametrize(
    "active_name",
    [
        # The four shapes the check's old literal set did not know about.
        "sound_reset_s1_1717000000.yml",
        "sound_snapshot_s1_1717000000.yml",
        "sound_lean_current.yml",
        "grouping_leader.yml",
    ],
)
def test_check_sound_profile_accepts_every_jts_generated_active_name(
    monkeypatch, tmp_path, active_name,
):
    """One owner decides what "JTS-generated" means, and the doctor asks it.

    `check_sound_profile` held a second, narrower copy of that predicate as a
    literal set and had fallen four shapes behind
    :func:`jasper.sound.camilla_yaml.is_jts_generated_config`, so it told a
    household its saved profile was not reflected in a graph that
    demonstrably carries it. Permanent since #2572: the reconcile now leaves a
    content-identical graph running under whatever it is named.

    The negative half is
    `test_check_sound_profile_warns_when_saved_profile_not_active` above.
    """
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile, build_sound_filters

    profile = tmp_path / "sound_profile.json"
    profile.write_text(json.dumps(_SAVED_SOUND_PROFILE))

    # The active graph genuinely carries the saved preference EQ.
    saved = SoundProfile.from_mapping(_SAVED_SOUND_PROFILE)
    assert build_sound_filters(saved)
    _point_at_config(
        monkeypatch,
        tmp_path,
        emit_sound_config(saved, profile_id="t"),
        name=active_name,
    )
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = doctor.check_sound_profile()

    assert r.status == "ok"
    assert r.reason == ""


def test_check_sound_profile_fails_on_corrupt_json(monkeypatch, tmp_path):
    profile = tmp_path / "sound_profile.json"
    profile.write_text("{not json")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = doctor.check_sound_profile()

    assert r.status == "fail"
    assert r.reason == audio.REASON_SOUND_PROFILE_UNREADABLE


# ----------------------------------------------------------- DSP apply state


@pytest.mark.parametrize(
    "state, status, reason",
    [
        (None, "ok", audio.REASON_DSP_APPLY_NONE),
        (
            {
                "op_id": "abcdef123456",
                "source": "sound",
                "phase": "done",
                "result": "success",
                "candidate_config_path":
                    "/var/lib/camilladsp/configs/sound_current.yml",
            },
            "ok", "",
        ),
        (
            {
                "op_id": "abcdef123456",
                "source": "correction",
                "phase": "load",
                "result": "load_failed_rollback_failed",
                "rollback_attempted": True,
                "rollback_succeeded": False,
            },
            "fail", audio.REASON_DSP_APPLY_ROLLBACK_FAILED,
        ),
        (
            {
                "op_id": "abcdef123456",
                "source": "correction",
                "phase": "load",
                "result": "load_failed",
                "rollback_attempted": True,
                "rollback_succeeded": True,
            },
            "warn", audio.REASON_DSP_APPLY_UNSUCCESSFUL,
        ),
    ],
    ids=["none-recorded", "success", "rollback-failed", "rolled-back"],
)
def test_check_dsp_apply_state_verdicts(monkeypatch, tmp_path, state, status, reason):
    path = tmp_path / "dsp_apply_state.json"
    if state is not None:
        path.write_text(json.dumps(state))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(path))

    r = doctor.check_dsp_apply_state()

    assert r.status == status
    assert r.reason == reason


# --- #1666: canonical active-speaker baseline durability -------------------


def _point_at_baseline(monkeypatch, *, live, canonical, statefile):
    if live is not None:
        statefile.write_text(f"config_path: {live}\n", encoding="utf-8")
        monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    else:
        monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(canonical))


def test_active_speaker_baseline_canonical_ok_when_live_is_canonical(
    monkeypatch,
    tmp_path,
):
    canonical = tmp_path / "active_speaker_baseline.yml"
    canonical.write_text("# whatever is currently loaded\n", encoding="utf-8")
    _point_at_baseline(
        monkeypatch,
        live=canonical,
        canonical=canonical,
        statefile=tmp_path / "statefile.yml",
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert r.reason == ""


@pytest.mark.parametrize("live_name", [None, "outputd-cutover.yml"])
def test_active_speaker_baseline_canonical_ok_when_not_applicable(
    monkeypatch,
    tmp_path,
    live_name,
):
    """A live config that is not a baseline candidate at all (a plain
    stereo/flat topology's own generated config), and a statefile that cannot
    be read, are both outside this check's scope — never a false warning on the
    majority of the fleet that has no active baseline commissioned. A missing
    statefile is already a real failure at
    `check_active_speaker_runtime_graph`, which owns it."""
    live = None
    if live_name is not None:
        live = tmp_path / live_name
        live.write_text("devices:\n  samplerate: 48000\n", encoding="utf-8")
    _point_at_baseline(
        monkeypatch,
        live=live,
        canonical=tmp_path / "active_speaker_baseline.yml",
        statefile=tmp_path / "statefile.yml",
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "skipped"
    assert r.reason == audio.REASON_BASELINE_CANONICAL_NOT_APPLICABLE


async def test_active_speaker_baseline_canonical_ok_when_content_matches_live(
    monkeypatch,
    tmp_path,
):
    """The common case: every successful apply's post-success promote keeps
    canonical's content equal to whatever candidate is actually live."""
    from tests.test_active_speaker_baseline_profile import _apply_prior_then_run8

    (
        _state_path,
        config_path,
        _load_config,
        _current_config_path,
        _prior_payload,
        run8_payload,
        _retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    _point_at_baseline(
        monkeypatch,
        live=Path(run8_payload["profile"]["config"]["path"]),
        canonical=config_path,
        statefile=tmp_path / "statefile.yml",
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert r.reason == ""


async def test_active_speaker_baseline_canonical_warns_on_divergence(
    monkeypatch,
    tmp_path,
):
    """Canonical holds run-8's promoted bytes; simulating CamillaDSP still
    running the PRIOR candidate is a genuine content mismatch — warn, since the
    live graph is the audible truth and is correct, but the canonical file is
    stale for other readers."""
    from tests.test_active_speaker_baseline_profile import _apply_prior_then_run8

    (
        _state_path,
        config_path,
        _load_config,
        _current_config_path,
        prior_payload,
        _run8_payload,
        _retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    _point_at_baseline(
        monkeypatch,
        live=Path(prior_payload["profile"]["config"]["path"]),
        canonical=config_path,
        statefile=tmp_path / "statefile.yml",
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "warn"
    assert r.reason == audio.REASON_BASELINE_CANONICAL_STALE


def test_active_speaker_baseline_canonical_warns_on_missing_canonical(
    monkeypatch,
    tmp_path,
):
    """A live applied-candidate sibling with no canonical file yet (a box whose
    last promote never ran) warns rather than failing — the next apply or
    restore re-promotes it."""
    live = tmp_path / "active_speaker_baseline_candidate_abc123def456.yml"
    live.write_text("# a real applied candidate, never promoted\n", encoding="utf-8")
    _point_at_baseline(
        monkeypatch,
        live=live,
        canonical=tmp_path / "active_speaker_baseline.yml",
        statefile=tmp_path / "statefile.yml",
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "warn"
    assert r.reason == audio.REASON_BASELINE_CANONICAL_MISSING


# ------------------------------------------------- active speaker startup hold


def test_active_speaker_startup_hold_ok_when_no_hold_is_in_flight():
    # The conftest fixture points the marker at a per-test path that starts
    # absent, which is the no-hold baseline.
    r = doctor.check_active_speaker_startup_hold()

    assert r.status == "ok"
    assert r.reason == audio.REASON_STARTUP_HOLD_NONE


@pytest.mark.parametrize(
    "load_status, status, reason",
    [
        ("loaded", "ok", audio.REASON_STARTUP_HOLD_IN_FLIGHT),
        # A hold with no load behind it keeps a commissioned box on its SILENT
        # anchor across every reconcile.
        ("rolled_back", "warn", audio.REASON_STARTUP_HOLD_STALE),
    ],
    ids=["in-flight", "stale"],
)
def test_active_speaker_startup_hold_verdicts(
    monkeypatch, tmp_path, load_status, status, reason
):
    from jasper.active_speaker.startup_hold import hold_staged_startup

    assert hold_staged_startup() is True
    state = tmp_path / "startup_load.json"
    state.write_text(json.dumps({"status": load_status}), encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE", str(state))

    r = doctor.check_active_speaker_startup_hold()

    assert r.status == status
    assert r.reason == reason


# ---------------------------------------------------- room correction authority


@pytest.mark.parametrize(
    "acoustic, status, reason",
    [
        pytest.param(
            {"required": False}, "skipped",
            audio.REASON_ROOM_AUTHORITY_NOT_REQUIRED, id="no_authority_needed",
        ),
        pytest.param(
            {"required": True, "allowed": True, "authority": "manual_applied_profile"},
            "ok", "", id="banked",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": "active_commissioning_receipt_stale",
                "detail": "re-mint it when convenient",
            },
            "warn", audio.REASON_ROOM_AUTHORITY_UNPROVEN,
            id="unproven_runs_anyway",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_ABSENT,
                "detail": "finish commissioning when convenient",
            },
            "ok", audio.REASON_ROOM_AUTHORITY_UNBANKED,
            id="never_minted_is_the_state_most_speakers_are_in",
        ),
        # ABSENT is also the module's catch-all default, so a store code under a
        # verified lifecycle (a receipt that VANISHED) arrives here too; it stays
        # ok rather than turning the fleet's normal state into a nag (ADR-0196).
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_ABSENT,
                "cause": "missing",
                "detail": "finish commissioning when convenient",
            },
            "ok", audio.REASON_ROOM_AUTHORITY_UNBANKED, id="vanished_receipt",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_UNREADABLE,
                "cause": "PermissionError:EACCES:/var/lib/jasper/receipt.json",
                "detail": "a machine-level fault",
            },
            "warn", audio.REASON_ROOM_AUTHORITY_RECEIPT_UNREADABLE,
            id="machine_fault_still_warns",
        ),
        pytest.param(
            None, "warn", audio.REASON_ROOM_AUTHORITY_NO_DECISION,
            id="no_room_decision_published",
        ),
    ],
)
def test_room_correction_authority_warns_but_never_fails(
    monkeypatch, acoustic, status, reason,
):
    """The doctor line is the only place an unproven room run is visible.

    Ruling S10 stopped the receipt from refusing the run, so nothing else tells
    a household that the result it just measured is not banked as verified.
    WARN, never FAIL: nothing is broken and nothing is stopped.
    """
    monkeypatch.setattr(
        setup_status_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {"acoustic_commissioning": acoustic},
    )

    r = doctor.check_room_correction_authority()

    assert r.status == status
    assert r.reason == reason


def test_room_correction_authority_warns_when_setup_cannot_be_read(monkeypatch):
    def _unreadable(**_kwargs):
        raise OSError("secret filesystem detail")

    monkeypatch.setattr(
        setup_status_mod, "read_active_speaker_setup_status", _unreadable
    )

    r = doctor.check_room_correction_authority()

    assert r.status == "warn"
    assert r.reason == audio.REASON_SPEAKER_SETUP_UNREADABLE


# ------------------------------------------------- active speaker setup notices


@pytest.mark.parametrize(
    "issues, status, reason",
    [
        pytest.param([], "ok", audio.REASON_SETUP_NOTICES_NONE, id="nothing_standing"),
        pytest.param(
            [{"severity": "blocker", "code": "x", "message": "m"}],
            "ok", audio.REASON_SETUP_NOTICES_NONE,
            id="blockers_have_their_own_surfaces",
        ),
        pytest.param(
            [{
                "severity": "warning",
                "code": "active_baseline_topology_changed",
                "message": "topology changed since the applied baseline",
            }],
            "warn", audio.REASON_SETUP_NOTICES_STANDING, id="topology_notice",
        ),
    ],
)
def test_active_speaker_setup_notices_renders_only_non_blockers(
    monkeypatch, issues, status, reason,
):
    """Nothing else in the tree renders a non-blocker setup issue, so demoting
    the topology block without this line would have been a silent deletion."""
    monkeypatch.setattr(
        setup_status_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {"issues": issues},
    )

    r = doctor.check_active_speaker_setup_notices()

    assert r.status == status
    assert r.reason == reason


# -------------------------------------------------- active speaker applied graph


@pytest.mark.parametrize(
    "payload, status, reason",
    [
        pytest.param(
            {"protected_profile": None}, "skipped",
            audio.REASON_APPLIED_GRAPH_NO_PROFILE, id="nothing_applied_to_bind",
        ),
        pytest.param(
            {"protected_profile": {"layer_a_binding": {
                "status": "current", "matches": True, "loaded_fingerprint": "a",
            }}},
            "ok", "", id="durable_graph_is_the_profiles",
        ),
        pytest.param(
            {"protected_profile": {"layer_a_binding": {
                "status": "distributed_active_unsupported", "matches": False,
            }}},
            "skipped", audio.REASON_APPLIED_GRAPH_NOT_EVALUATED,
            id="grouped_active_is_not_drift",
        ),
        pytest.param(
            {
                "issues": [{
                    "severity": "blocker",
                    "code": setup_status_mod.IN_SEQUENCE_CAPTURE_ANCHOR_REASON,
                    "message": "commissioning graph is loaded",
                }],
                "protected_profile": {"layer_a_binding": {
                    "status": "mismatch", "matches": False,
                }},
            },
            "skipped", audio.REASON_APPLIED_GRAPH_STAGED_ANCHOR,
            id="staged_anchor_mismatch_is_by_design",
        ),
        pytest.param(
            {
                "active_config_path": "/var/lib/camilladsp/configs/candidate.yml",
                "protected_profile": {"layer_a_binding": {
                    "status": "mismatch", "matches": False,
                    "expected_fingerprint": "a", "loaded_fingerprint": "b",
                    "differences": [{
                        "field": "as_woofer_delay.delay",
                        "expected": "0.0",
                        "loaded": "0.1286",
                    }],
                }},
            },
            "warn", audio.REASON_APPLIED_GRAPH_MISMATCH,
            id="f6_rejected_delay_left_on_the_durable_anchor",
        ),
    ],
)
def test_active_speaker_applied_graph_discloses_never_gates(
    monkeypatch, payload, status, reason,
):
    """Blind-run finding F-6 had no surface that named the disagreement.

    WARN, never FAIL, and the two states that legitimately differ — a grouped
    active runtime, a staged/commissioning anchor — must not read as drift.
    """
    monkeypatch.setattr(
        setup_status_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: payload,
    )

    r = doctor.check_active_speaker_applied_graph()

    assert r.status == status
    assert r.reason == reason


def test_active_speaker_applied_graph_warns_when_setup_cannot_be_read(monkeypatch):
    def _unreadable(**_kwargs):
        raise OSError("secret filesystem detail")

    monkeypatch.setattr(
        setup_status_mod, "read_active_speaker_setup_status", _unreadable
    )

    r = doctor.check_active_speaker_applied_graph()

    assert r.status == "warn"
    assert r.reason == audio.REASON_SPEAKER_SETUP_UNREADABLE
