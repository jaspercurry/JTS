# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lock down the jasper-outputd service shape."""

from __future__ import annotations

from pathlib import Path

from jasper.tts_routing import (
    DUCK_TRANSPORT_ENV,
    FANIN_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET_ENV,
    TTS_TRANSPORT_ENV,
    VOICE_TTS_SOCKET_ENV,
)
from tests.install_surface import installer_text

from ._voice_runtime_text import voice_runtime_text


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-outputd.service"
VOICE_UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-voice.service"
ROLLBACK_SCRIPT_PATH = REPO / "scripts" / "disable-outputd-cutover.sh"


def _read_unit() -> str:
    return UNIT_PATH.read_text()


def _value_for(unit_text: str, key: str) -> str | None:
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def test_outputd_unit_file_exists():
    assert UNIT_PATH.exists()


def test_outputd_unit_is_notify_and_watchdog_managed():
    unit = _read_unit()
    assert _value_for(unit, "Type") == "notify"
    assert _value_for(unit, "WatchdogSec") == "30s"
    assert _value_for(unit, "TimeoutStopSec") == "5s"
    assert _value_for(unit, "Restart") == "on-failure"


def test_outputd_unit_is_mainline_default_not_flag_gated():
    unit = _read_unit()
    assert _value_for(unit, "ConditionPathExists") is None
    assert _value_for(unit, "StartLimitAction") == "reboot"


def test_outputd_unit_has_audio_realtime_shape():
    unit = _read_unit()
    assert _value_for(unit, "Slice") == "jts-audio.slice"
    assert _value_for(unit, "LimitMEMLOCK") == "infinity"
    assert _value_for(unit, "CPUSchedulingPolicy") == "fifo"
    assert _value_for(unit, "CPUSchedulingPriority") == "35"
    assert _value_for(unit, "OOMScoreAdjust") == "-950"


def test_outputd_unit_runtime_and_exec_paths():
    unit = _read_unit()
    assert _value_for(unit, "RuntimeDirectory") == "jasper-outputd"
    assert _value_for(unit, "ExecStart") == "/opt/jasper/bin/jasper-outputd"
    assert _value_for(unit, "ExecStopPost") == (
        "-/usr/local/sbin/jasper-outputd-failure-reconcile"
    )
    assert OUTPUTD_TTS_SOCKET_ENV not in unit
    for expected in [
        'Environment="JASPER_OUTPUTD_BACKEND=alsa"',
        'Environment="JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture"',
        'Environment="JASPER_OUTPUTD_DAC_PCM=outputd_dac"',
        'Environment="JASPER_OUTPUTD_PERIOD_FRAMES=1024"',
        'Environment="JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=4096"',
        'Environment="JASPER_OUTPUTD_DAC_BUFFER_FRAMES=3072"',
        'Environment="JASPER_OUTPUTD_CONTENT_BRIDGE=direct"',
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


def test_outputd_operator_retune_file_is_after_packaged_defaults():
    unit = _read_unit()
    assert unit.index(
        'Environment="JASPER_OUTPUTD_DAC_BUFFER_FRAMES=3072"'
    ) < unit.index("EnvironmentFile=-/var/lib/jasper/outputd.env")


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


def test_voice_unit_routes_tts_to_fanin_pre_dsp_on_mainline():
    unit = VOICE_UNIT_PATH.read_text()
    assert (
        "After=jasper-fanin.service jasper-camilla.service "
        "jasper-outputd.service network-online.target"
    ) in unit
    assert "jasper-fanin.service" in _value_for(unit, "Wants")
    assert "jasper-outputd.service" in _value_for(unit, "Wants")
    assert f'Environment="{TTS_TRANSPORT_ENV}=outputd"' in unit
    assert f'Environment="{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}"' in unit
    assert f'Environment="{DUCK_TRANSPORT_ENV}=fanin"' in unit
    assert "EnvironmentFile=-/var/lib/jasper/tts.env" not in unit


def test_voice_unit_parks_cleanly_when_provider_is_unconfigured():
    unit = VOICE_UNIT_PATH.read_text()
    assert _value_for(unit, "StartLimitAction") == "reboot"
    # 78 (provider unconfigured) parks cleanly. 66 (no usable mic) parks
    # the same way and now shares these lists — the exact "66 78" set is
    # pinned by tests/test_voice_input_gate.py; here we only assert this
    # test's own concern, that 78 stays a clean-park code.
    assert "78" in _value_for(unit, "SuccessExitStatus").split()
    assert "78" in _value_for(unit, "RestartPreventExitStatus").split()


def test_voice_daemon_maps_unconfigured_provider_to_ex_config():
    source = voice_runtime_text()
    assert "EX_CONFIG_EXIT = 78" in source
    assert "VOICE_PROVIDER_NOT_CONFIGURED_EXIT = EX_CONFIG_EXIT" in source
    assert "except VoiceProviderNotConfigured as e:" in source
    # Emitted via the canonical log_event emitter (renders
    # `event=voice.unconfigured …` at runtime); the source carries the
    # bare event name, the `event=` prefix is added by log_event.
    assert '"voice.unconfigured"' in source
    assert "sys.exit(VOICE_PROVIDER_NOT_CONFIGURED_EXIT)" in source


def test_voice_daemon_maps_vad_setup_failure_to_ex_config():
    source = voice_runtime_text()
    assert "EX_CONFIG_EXIT = 78" in source
    assert "VOICE_STARTUP_CONFIG_ERROR_EXIT = EX_CONFIG_EXIT" in source
    assert "except SpeechVADSetupError as e:" in source
    # Emitted via log_event (renders `event=voice.vad_setup_failed …`
    # at runtime); the source carries the bare event name.
    assert '"voice.vad_setup_failed"' in source
    assert "sys.exit(VOICE_STARTUP_CONFIG_ERROR_EXIT)" in source


def test_cutover_rollback_helper_disables_persistent_outputd_unit():
    script = ROLLBACK_SCRIPT_PATH.read_text()
    assert "systemctl disable --now jasper-outputd.service" in script
    assert "systemctl reset-failed jasper-outputd.service" in script
    assert "JASPER_TTS_TRANSPORT=outputd" in script
    assert "pre-outputd" in script
    assert "Deploy main next" not in script


def test_voice_unit_has_stage2_memory_high_throttle():
    """Audit C3: the deferred Stage 2 memory bound. MemoryHigh (throttle)
    not MemoryMax (kill) — voice is the most-protected daemon and must
    never be cgroup-killed outright; value sized ~2.5x the ~150 MB Pss
    steady state from README's resource table."""
    unit = VOICE_UNIT_PATH.read_text()
    assert _value_for(unit, "MemoryHigh") == "384M"
    assert _value_for(unit, "MemoryMax") is None
