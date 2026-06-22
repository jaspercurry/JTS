# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "aec-probe-xvf-ref-level.sh"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_probe_is_bounded_when_services_are_stopped() -> None:
    text = _script_text()

    assert 'PROBE_TIMEOUT_SECONDS="${PROBE_TIMEOUT_SECONDS:-15}"' in text
    assert 'AEC_PROBE_TIMEOUT_SECONDS="${PROBE_TIMEOUT_SECONDS}"' in text
    assert 'timeout --kill-after=2s "${PROBE_TIMEOUT_SECONDS}s"' in text
    assert "probe_timeout = float(os.environ[\"AEC_PROBE_TIMEOUT_SECONDS\"])" in text
    assert "PROBE_TIMEOUT_SECONDS must be at least CAPTURE_SECONDS + 2" in text
    assert "timeout=max(cap_dur + 3.0, 5.0)" in text


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
    assert 'exit "${restore_rc}"' in text


def test_probe_keeps_diagnostic_only_contract() -> None:
    text = _script_text()

    forbidden_fragments = [
        "SAVE_CONFIGURATION",
        "REBOOT",
        "/etc/jasper",
        "/var/lib/jasper",
        "jasper_env_file_set",
        "tee /etc",
        "tee /var/lib",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in text


def test_probe_measures_reference_format_level_and_xvf_channels() -> None:
    text = _script_text()

    assert 'REF_UDP_HOST="${REF_UDP_HOST:-127.0.0.1}"' in text
    assert 'REF_UDP_PORT="${REF_UDP_PORT:-9891}"' in text
    assert "reference_udp_48k:" in text
    assert "left_right_rms_delta_db" in text
    assert "chip_ref_model_16k_mono" in text
    assert "chip_ref_after_AUDIO_MGR_REF_GAIN" in text
    assert "xvf_capture_channels:" in text
    assert "AEC_AECCONVERGED" in text
    assert "correction_substream" in text


def test_probe_uses_outputd_path_not_a_dac_specific_shortcut() -> None:
    text = _script_text()
    lowered = text.lower()

    assert "correction_substream" in text
    assert 'REF_UDP_HOST="${REF_UDP_HOST:-127.0.0.1}"' in text
    assert 'REF_UDP_PORT="${REF_UDP_PORT:-9891}"' in text
    assert "apple" not in lowered
    assert "hifiberry" not in lowered
    assert "dac8x" not in lowered
    assert "hw:CARD=A,DEV" not in text
    assert "plughw:CARD=A,DEV" not in text
    assert "outputd_dac" not in text
    assert "JASPER_AUDIO_DAC" not in text
