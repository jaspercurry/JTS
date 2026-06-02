"""Lock down the jasper-outputd service shape."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-outputd.service"
VOICE_UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-voice.service"
ROLLBACK_SCRIPT_PATH = REPO / "scripts" / "disable-outputd-cutover.sh"
VOICE_DAEMON_PATH = REPO / "jasper" / "voice_daemon.py"


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
    assert (
        'Environment="JASPER_OUTPUTD_TTS_SOCKET=/run/jasper-outputd/tts.sock"'
        in unit
    )
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
    assert (
        unit.index('Environment="JASPER_OUTPUTD_DAC_BUFFER_FRAMES=3072"')
        < unit.index("EnvironmentFile=-/var/lib/jasper/outputd.env")
    )


def test_install_builds_installs_and_enables_outputd():
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    assert "build_install_jasper_outputd" in install_sh
    assert "ERROR: jasper-outputd source missing" in install_sh
    assert "/opt/jasper/bin/jasper-outputd" in install_sh
    assert "deploy/systemd/jasper-outputd.service" in install_sh
    enable_block = install_sh.split(
        "systemctl enable jasper-camilla.service jasper-fanin.service",
        1,
    )[1].split("systemctl stop jasper-voice.service", 1)[0]
    assert "jasper-outputd.service" in enable_block
    assert "jasper-audio-hardware-reconcile.service" in enable_block
    assert "systemctl restart jasper-outputd.service" in install_sh
    assert "require_outputd_ready" in install_sh
    assert "jasper-outputd STATUS probe failed" in install_sh
    assert "systemctl stop jasper-voice.service" in install_sh
    restart_block = install_sh.split(
        "systemctl stop jasper-voice.service", 1,
    )[1].split("systemctl enable jasper-wifi-guardian.service", 1)[0]
    assert (
        restart_block.index("jasper-audio-hardware-reconcile --reason install")
        < restart_block.index("require_outputd_ready")
    )
    assert (
        restart_block.index("require_outputd_ready")
        < restart_block.index("reconcile_aec_state")
    )


def test_voice_unit_routes_tts_to_outputd_on_mainline():
    unit = VOICE_UNIT_PATH.read_text()
    assert "After=jasper-camilla.service jasper-outputd.service network-online.target" in unit
    assert "jasper-outputd.service" in _value_for(unit, "Wants")
    assert 'Environment="JASPER_TTS_TRANSPORT=outputd"' in unit
    assert (
        'Environment="JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock"'
        in unit
    )


def test_voice_unit_parks_cleanly_when_provider_is_unconfigured():
    unit = VOICE_UNIT_PATH.read_text()
    assert _value_for(unit, "StartLimitAction") == "reboot"
    assert _value_for(unit, "SuccessExitStatus") == "78"
    assert _value_for(unit, "RestartPreventExitStatus") == "78"


def test_voice_daemon_maps_unconfigured_provider_to_ex_config():
    source = VOICE_DAEMON_PATH.read_text()
    assert "EX_CONFIG_EXIT = 78" in source
    assert "VOICE_PROVIDER_NOT_CONFIGURED_EXIT = EX_CONFIG_EXIT" in source
    assert "except VoiceProviderNotConfigured as e:" in source
    assert "event=voice.unconfigured" in source
    assert "sys.exit(VOICE_PROVIDER_NOT_CONFIGURED_EXIT)" in source


def test_voice_daemon_maps_vad_setup_failure_to_ex_config():
    source = VOICE_DAEMON_PATH.read_text()
    assert "EX_CONFIG_EXIT = 78" in source
    assert "VOICE_STARTUP_CONFIG_ERROR_EXIT = EX_CONFIG_EXIT" in source
    assert "except SpeechVADSetupError as e:" in source
    assert "event=voice.vad_setup_failed" in source
    assert "sys.exit(VOICE_STARTUP_CONFIG_ERROR_EXIT)" in source


def test_cutover_rollback_helper_disables_persistent_outputd_unit():
    script = ROLLBACK_SCRIPT_PATH.read_text()
    assert "systemctl disable --now jasper-outputd.service" in script
    assert "systemctl reset-failed jasper-outputd.service" in script
    assert "JASPER_TTS_TRANSPORT=outputd" in script
    assert "pre-outputd" in script
    assert "Deploy main next" not in script
