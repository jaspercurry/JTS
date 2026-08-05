# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor audio domain."""

import sys
from types import SimpleNamespace
from unittest.mock import patch


from jasper.cli import doctor
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
    write_state as write_output_hardware_state,
)


from .doctor_test_support import (
    _fresh_cfg,
)


def test_apple_dongle_check_skips_for_non_apple_output_dac(monkeypatch):
    def fail_probe(*_args, **_kwargs):
        raise AssertionError("Apple USB probe should not run")

    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "hifiberry_dac8x"},
    )
    monkeypatch.setattr(doctor.audio, "_run", fail_probe)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"
    assert "active output DAC is hifiberry_dac8x" in result.detail


def test_apple_dongle_check_matches_usb_id_case_insensitively(monkeypatch):
    calls = []

    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd == ["lsusb"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Bus 001 Device 002: ID 05AC:110A Apple\n",
                stderr="",
            )
        if cmd == ["aplay", "-l"]:
            return SimpleNamespace(
                returncode=0,
                stdout="card 2: Apple [Apple USB-C to 3.5mm Headphone Jack]\n",
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"
    assert calls == [["lsusb"], ["aplay", "-l"]]


def test_apple_dongle_check_reads_usb_id_from_active_profile(monkeypatch):
    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )
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
        if cmd == ["aplay", "-l"]:
            return SimpleNamespace(
                returncode=0,
                stdout="card 2: Apple [Apple USB-C to 3.5mm Headphone Jack]\n",
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"


def test_dongle_headphone_gain_check_skips_for_non_apple_output_dac(monkeypatch):
    def fail_probe(*_args, **_kwargs):
        raise AssertionError("Apple mixer probe should not run")

    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "hifiberry_dac8x"},
    )
    monkeypatch.setattr(doctor.audio, "_run", fail_probe)

    result = doctor.check_dongle_headphone_at_max()

    assert result.status == "ok"
    assert "active output DAC is hifiberry_dac8x" in result.detail


def test_dongle_headphone_gain_check_uses_reconciled_card(monkeypatch):
    calls = []

    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.delenv("JASPER_AUDIO_DAC_CARD", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AUDIO_DAC_CARD": "Apple2",
        },
    )

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="Front Left: Playback 100 [100%] [0.00dB] [on]\n",
            stderr="",
        )

    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_dongle_headphone_at_max()

    assert result.status == "ok"
    assert calls == [["amixer", "-c", "Apple2", "sget", "Headphone"]]


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
                "clock_domain_evidence": {
                    "evidence_kind": "dual_apple_usb_c_dac_drift_measurement",
                    "measurement_id": "doctor-serial-contract",
                    "status": "passed",
                    "duration_seconds": 900,
                    "sample_rate_hz": 48000,
                    "offset_frames": -7,
                    "max_offset_delta_frames": 0,
                    "drift_ppm": 0,
                    "xrun_count": 0,
                    "dac_serials": [
                        "DWH53530FHL2FN3AC",
                        "DWH53530FLL2FN3A3",
                    ],
                },
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


def test_dual_apple_headphone_gain_checks_every_card(monkeypatch):
    state = OutputHardwareState(
        profile_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        profile_label="Dual Apple USB-C DAC 4-channel pair",
        status="ready",
        physical_output_count=4,
        apple_dac_count=2,
        child_devices=(
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            ),
            OutputCardFact(
                card_id="A_1",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            ),
        ),
    )
    commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        if cmd[:3] == ["amixer", "-c", "A"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Front Left: Playback 120 [100%] [0.00dB] [on]\n",
                stderr="",
            )
        if cmd[:3] == ["amixer", "-c", "A_1"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Front Left: Playback 90 [75%] [-10.00dB] [on]\n",
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_load_output_hardware_state", lambda: state)
    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_dongle_headphone_at_max()

    assert result.status == "warn"
    assert "A_1:75%" in result.detail
    assert ["amixer", "-c", "A", "sget", "Headphone"] in commands
    assert ["amixer", "-c", "A_1", "sget", "Headphone"] in commands


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

    fake_sd = FakeSD()

    def fake_import(*args, **kwargs):
        if args and args[0] == "sounddevice":
            return fake_sd
        return __import__(*args, **kwargs)

    # Use a sd-stub by monkeypatching the import inside the function.
    # Easier: patch a wrapper. Instead, patch _jasper_voice_active and
    # mock sd.rec via injecting into sys.modules.
    sys.modules["sounddevice"] = fake_sd
    try:
        with patch.object(doctor.audio, "_jasper_voice_active", return_value=True):
            r = doctor.check_mic_capture(cfg)
        assert r.status == "ok"
        assert "skipped" in r.detail
        assert "jasper-voice holds" in r.detail
    finally:
        del sys.modules["sounddevice"]


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

    sys.modules["sounddevice"] = FakeSD()
    try:
        with patch.object(doctor.audio, "_jasper_voice_active", return_value=False):
            r = doctor.check_mic_capture(cfg)
        assert r.status == "fail"
    finally:
        del sys.modules["sounddevice"]


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


def test_dac_sync_mode_skips_when_no_xvf_mic(monkeypatch):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: False)
    # Must short-circuit before reading output state when chip-AEC is moot.
    monkeypatch.setattr(
        doctor.audio,
        "_output_hardware_state_or_none",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "ok"
    assert "no XVF3800 mic present" in result.detail


def test_dac_sync_mode_ok_for_sync_apple_dongle(monkeypatch):
    # Mirrors the real jts capture: Apple dongle reports (SYNC).
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio,
        "_output_hardware_state_or_none",
        lambda: _sync_mode_state("SYNC"),
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "ok"
    assert "synchronous USB playback endpoint" in result.detail
    # Advisory clock-coherence wording, not an enable/disable gate.
    assert "clock-coherence observation only" in result.detail
    assert "fixed DAC-profile qualification" in result.detail


def test_dac_sync_mode_ok_for_adaptive_endpoint(monkeypatch):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio,
        "_output_hardware_state_or_none",
        lambda: _sync_mode_state("ADAPTIVE"),
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "ok"
    assert "synchronous USB playback endpoint" in result.detail


def test_dac_sync_mode_warns_fail_closed_for_async(monkeypatch):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio,
        "_output_hardware_state_or_none",
        lambda: _sync_mode_state("ASYNC"),
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "warn"
    assert "async USB playback endpoint" in result.detail
    # Advisory only: neither endpoint sync nor the diagnostic SRO verdict
    # authorizes production; the fixed DAC profile does.
    assert "fixed DAC-profile qualification" in result.detail


def test_dac_sync_mode_na_for_i2s_dac(monkeypatch):
    # HiFiBerry/I2S HAT: known DAC profile, no USB endpoint sync tag.
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    state = OutputHardwareState(
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
    monkeypatch.setattr(doctor.audio, "_output_hardware_state_or_none", lambda: state)
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "ok"
    assert "I2S clock slave" in result.detail


def test_dac_sync_mode_warns_when_state_unavailable(monkeypatch):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(doctor.audio, "_output_hardware_state_or_none", lambda: None)
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "warn"
    assert "output hardware state unavailable" in result.detail


# --- G3: outputd xrun-rate WARN tier (audio-latency foundation) ---


def _xrun_section(rate_per_hour, last_xrun_age_ms):
    """Minimal outputd STATUS content/dac section for the xrun-rate helper."""
    return {
        "xrun_count": 0,
        "xrun_rate_per_hour": rate_per_hour,
        "last_xrun_age_ms": last_xrun_age_ms,
    }


def test_outputd_xrun_warning_none_when_no_recent_xrun():
    """last_xrun_age_ms=null (no xrun ever) → never warn, regardless of rate."""
    quiet = _xrun_section(rate_per_hour=0.0, last_xrun_age_ms=None)
    assert doctor.audio_runtime._outputd_xrun_rate_warning(quiet, quiet) is None


def test_outputd_xrun_warning_suppressed_for_stale_burst():
    """A high all-time rate whose last xrun is OLD (a cleared deploy-time
    burst) must NOT warn — the WARN is for a sustained, *current* problem."""
    stale = _xrun_section(
        rate_per_hour=50.0,
        last_xrun_age_ms=doctor.audio_runtime._OUTPUTD_XRUN_RECENT_AGE_MS + 1,
    )
    assert doctor.audio_runtime._outputd_xrun_rate_warning(stale, stale) is None


def test_outputd_xrun_warning_suppressed_for_recent_single_blip():
    """A recent xrun with a LOW sustained rate (one transient blip) must not
    warn — only a rate at/above the threshold qualifies."""
    blip = _xrun_section(
        rate_per_hour=doctor.audio_runtime._OUTPUTD_XRUN_RATE_WARN_PER_HOUR - 0.1,
        last_xrun_age_ms=1000,
    )
    assert doctor.audio_runtime._outputd_xrun_rate_warning(blip, blip) is None


def test_outputd_xrun_warning_fires_on_recent_sustained_rate():
    """Recent xrun AND a sustained rate at/above threshold → warn, naming the
    offending lane and both fields."""
    hot = _xrun_section(
        rate_per_hour=doctor.audio_runtime._OUTPUTD_XRUN_RATE_WARN_PER_HOUR,
        last_xrun_age_ms=2000,
    )
    quiet = _xrun_section(rate_per_hour=0.0, last_xrun_age_ms=None)
    reason = doctor.audio_runtime._outputd_xrun_rate_warning(quiet, hot)
    assert reason is not None
    assert "dac" in reason
    assert "xrun_rate_per_hour" in reason
    assert "last_xrun_age_ms" in reason


def test_outputd_xrun_warning_reports_worst_lane():
    """When both lanes qualify, the higher-rate lane is reported."""
    content = _xrun_section(rate_per_hour=8.0, last_xrun_age_ms=1000)
    dac = _xrun_section(rate_per_hour=40.0, last_xrun_age_ms=1000)
    reason = doctor.audio_runtime._outputd_xrun_rate_warning(content, dac)
    assert reason is not None
    assert reason.startswith("dac ")
