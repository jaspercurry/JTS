from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "aec-probe-latency.sh"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_probe_uses_direct_alsa_mic_capture_not_portaudio_aliases() -> None:
    text = _script_text()

    assert "sounddevice" not in text
    assert "sd.InputStream" not in text
    assert 'MIC_DEVICE="${MIC_DEVICE:-hw:CARD=Array,DEV=0}"' in text
    assert "AEC_PROBE_MIC_DEVICE" in text
    assert "alsaaudio.PCM" in text
    assert "device=mic_device" in text


def test_probe_uses_only_outputd_final_reference() -> None:
    text = _script_text()

    assert 'REF_UDP_HOST="${REF_UDP_HOST:-127.0.0.1}"' in text
    assert 'REF_UDP_PORT="${REF_UDP_PORT:-9891}"' in text
    assert "ref_capture_outputd_udp" in text
    assert "REF_SOURCE" not in text
    assert "jasper_capture" not in text
    assert "ref_capture_alsa" not in text
    assert "AEC_PROBE_REF_SOURCE" not in text


def test_probe_restores_only_services_that_were_active_at_entry() -> None:
    text = _script_text()

    assert "trap on_exit EXIT" in text
    assert "stop_if_active shairport-sync.service shairport_was_active" in text
    assert "stop_if_active jasper-voice.service voice_was_active" in text
    assert "stop_if_active jasper-aec-bridge.service bridge_was_active" in text
    assert '[[ "${bridge_was_active}" == "1" ]]' in text
    assert "sudo systemctl reset-failed jasper-aec-bridge.service" in text
    assert "sudo systemctl start jasper-aec-bridge.service" in text
    assert '[[ "${voice_was_active}" == "1" ]]' in text
    assert "sudo systemctl start jasper-voice.service" in text
    assert "local restore_rc=$?" in text
    assert 'exit "${restore_rc}"' in text


def test_probe_exposes_chip_beam_channel_selection() -> None:
    text = _script_text()

    assert 'MIC_CHANNELS="${MIC_CHANNELS:-6}"' in text
    assert 'MIC_CHANNEL="${MIC_CHANNEL:-0}"' in text
    assert "chip_aec_210 beam" in text
    assert "mic source:" in text
    assert "channel {mic_channel}/{mic_channels}" in text
