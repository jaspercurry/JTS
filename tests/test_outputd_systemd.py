# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lock down the jasper-outputd service shape."""

from __future__ import annotations

from pathlib import Path

from jasper.tts_routing import (
    FANIN_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET_ENV,
    VOICE_TTS_SOCKET_ENV,
)
from tests.install_surface import installer_text
from tests.systemd_unit_helpers import (
    assignments_for as _assignments_for,
    value_for as _value_for,
    values_for as _values_for,
)


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-outputd.service"
VOICE_UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-voice.service"


def _read_unit() -> str:
    return UNIT_PATH.read_text()


def test_outputd_unit_file_exists():
    assert UNIT_PATH.exists()


def test_outputd_unit_is_notify_and_watchdog_managed():
    unit = _read_unit()
    assert _value_for(unit, "Type") == "notify"
    assert _value_for(unit, "WatchdogSec") == "30s"
    # TimeoutStopSec=5s and Restart=on-failure are pinned, with the rest of
    # the restart ladder, by tests/test_systemd_hardening.py's
    # RESTART_POLICY table (R22, #4416).


def test_outputd_unit_is_mainline_default_not_flag_gated():
    unit = _read_unit()
    assert _values_for(unit, "ConditionPathExists") == ()
    # StartLimitAction=reboot is pinned by RESTART_POLICY (R22, #4416).


def test_outputd_starts_before_camilla_for_local_pipe_reader():
    unit = _read_unit()

    assert "jasper-camilla.service" not in _values_for(unit, "After")
    assert "jasper-fanin.service" not in _values_for(unit, "After")
    assert "jasper-camilla.service" in _values_for(unit, "Before")


def test_outputd_unit_has_audio_realtime_shape():
    unit = _read_unit()
    assert _value_for(unit, "Slice") == "jts-audio.slice"
    assert _value_for(unit, "LimitMEMLOCK") == "infinity"
    assert _value_for(unit, "CPUSchedulingPolicy") == "fifo"
    assert _value_for(unit, "CPUSchedulingPriority") == "35"
    assert _value_for(unit, "OOMScoreAdjust") == "-950"
    # G4: every FIFO unit caps RT-thread runaway so a spinning thread can't
    # starve PID 1 and trip the hardware watchdog into a reboot. outputd owns
    # the DAC write loop at the top FIFO priority (35), so this bound matters
    # most here.
    assert _value_for(unit, "LimitRTTIME") == "200000"


def test_outputd_unit_runtime_and_exec_paths():
    unit = _read_unit()
    assert _values_for(unit, "RuntimeDirectory") == ("jasper-outputd",)
    assert _value_for(unit, "RuntimeDirectoryPreserve") == "restart"
    assert _assignments_for(unit, "ExecStart") == (
        "/opt/jasper/bin/jasper-outputd",
    )
    assert _assignments_for(unit, "ExecStopPost") == (
        "-/usr/local/sbin/jasper-outputd-failure-reconcile",
    )
    assert OUTPUTD_TTS_SOCKET_ENV not in unit
    for expected in [
        'Environment="JASPER_OUTPUTD_BACKEND=alsa"',
        'Environment="JASPER_OUTPUTD_DAC_PCM=outputd_dac"',
        'Environment="JASPER_OUTPUTD_CONTROL_SOCKET=/run/jasper-outputd/control.sock"',
    ]:
        assert expected in unit
    read_write = " ".join(
        line.split("=", 1)[1]
        for line in unit.splitlines()
        if line.strip().startswith("ReadWritePaths=")
    )
    assert "/var/lib/jasper" in read_write
    assert "/run/jasper-outputd" in read_write


def test_the_unit_pins_neither_frame_default_over_the_operator_seam():
    """systemd applies env in FILE ORDER and a later line wins.

    An `Environment=` for either frame key sits BELOW `EnvironmentFile=`
    /etc/jasper/jasper.env and would therefore beat it — making the packaged
    default a middle layer while every reader (the plan, the doctor, the ring
    assets) documents jasper.env as the operator override. outputd resolves both
    keys against its own compile-time defaults when absent, so the layering the
    plan models is true only while these lines stay out.
    """
    unit = _read_unit()

    for key in ("JASPER_OUTPUTD_PERIOD_FRAMES", "JASPER_OUTPUTD_DAC_BUFFER_FRAMES"):
        assert f"Environment=\"{key}=" not in unit, key
    assert unit.index("EnvironmentFile=/etc/jasper/jasper.env") < unit.index(
        "EnvironmentFile=-/var/lib/jasper/outputd.env"
    )


def test_install_builds_installs_and_enables_outputd():
    install_sh = installer_text()
    assert "build_install_jasper_outputd" in install_sh
    assert "ERROR: jasper-outputd source missing" in install_sh
    assert "/opt/jasper/bin/jasper-outputd" in install_sh
    assert "deploy/systemd/jasper-outputd.service" in install_sh
    enable_block = install_sh.split(
        "systemctl enable jasper-camilla.service jasper-fanin.service",
        1,
    )[1].split("park_audio_clients_for_core_graph_restart", 1)[0]
    assert "jasper-outputd.service" in enable_block
    assert "jasper-audio-hardware-reconcile.service" in enable_block
    assert "systemctl restart jasper-outputd.service" in install_sh
    assert "require_outputd_ready" in install_sh
    assert "jasper-outputd STATUS probe failed" in install_sh
    assert "timeout --kill-after=5s 30s" in install_sh
    assert "jasper-sound reconcile-current-dsp --fail-open" in install_sh
    assert "sound DSP reconcile timed out after 30s" in install_sh
    assert "park_audio_clients_for_core_graph_restart" in install_sh
    restart_block = install_sh.rsplit(
        "systemctl enable jasper-camilla.service jasper-fanin.service",
        1,
    )[1].split("systemctl enable jasper-wifi-guardian.service", 1)[0]
    assert restart_block.index(
        "park_audio_clients_for_core_graph_restart"
    ) < restart_block.index(
        "jasper-audio-hardware-reconcile --reason install"
    )
    assert restart_block.index(
        "jasper-audio-hardware-reconcile --reason install"
    ) < restart_block.index("require_outputd_ready")
    assert restart_block.index("require_outputd_ready") < restart_block.index(
        "reconcile_sound_dsp_state"
    )
    assert restart_block.index("reconcile_sound_dsp_state") < restart_block.index(
        "reconcile_aec_state"
    )


def test_install_reloads_audio_udev_rules_without_synthetic_hotplug():
    install_sh = installer_text()

    assert "reload_audio_recovery_udev_rules_for_install" in install_sh
    assert "pin_attached_apple_dongle_power_control" in install_sh
    assert "udevadm control --reload-rules" in install_sh
    assert 'printf \'on\\n\' 2>/dev/null > "${control}"' in install_sh
    assert "udevadm trigger --action=add --subsystem-match=sound" not in install_sh
    assert "udevadm trigger --action=add --subsystem-match=usb" not in install_sh


def test_voice_unit_routes_tts_to_fanin_pre_dsp_on_mainline():
    unit = VOICE_UNIT_PATH.read_text()
    after = set(_values_for(unit, "After"))
    assert {
        "jasper-fanin.service",
        "jasper-camilla.service",
        "jasper-outputd.service",
        "network-online.target",
        "jasper-accessory-reconcile.service",
    } <= after
    assert "jasper-fanin.service" in _values_for(unit, "Wants")
    assert "jasper-outputd.service" in _values_for(unit, "Wants")
    assert "jasper-accessory-reconcile.service" in _values_for(unit, "Wants")
    assert f'Environment="{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}"' in unit
    assert "EnvironmentFile=-/var/lib/jasper/tts.env" not in unit


# StartLimitAction=reboot and the exact "66 78" SuccessExitStatus /
# RestartPreventExitStatus set for jasper-voice are pinned, with the rest of
# the restart ladder, by tests/test_systemd_hardening.py's RESTART_POLICY
# table (R22, #4416).


def test_every_config_fault_parks_on_the_code_the_unit_calls_clean():
    """The join this file owns. Which exception takes which exit, and what
    it says on the way out, is pinned end to end by
    tests/test_voice_input_gate.py; the unit lists 78 as a clean park just
    above. Only the constants' VALUE ties the two halves together — a
    daemon that parked on any other code would restart-loop into
    StartLimitAction=reboot instead."""
    from jasper.voice_daemon import (
        VOICE_PROVIDER_NOT_CONFIGURED_EXIT,
        VOICE_STARTUP_CONFIG_ERROR_EXIT,
    )

    assert VOICE_PROVIDER_NOT_CONFIGURED_EXIT == 78
    assert VOICE_STARTUP_CONFIG_ERROR_EXIT == 78


def test_voice_unit_has_stage2_memory_high_throttle():
    """Stage 2 memory bound. MemoryHigh (throttle) not MemoryMax (kill) —
    voice is the most-protected daemon and must never be cgroup-killed
    outright; value sized ~2.5x the daemon's steady-state footprint."""
    unit = VOICE_UNIT_PATH.read_text()
    assert _value_for(unit, "MemoryHigh") == "256M"
    assert _value_for(unit, "MemoryMax") is None
