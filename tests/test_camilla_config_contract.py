from __future__ import annotations

from jasper.camilla_config_contract import parse_camilla_devices_config


def test_parse_camilla_devices_config_extracts_clock_and_outputd_lanes() -> None:
    parsed = parse_camilla_devices_config(
        """
        ---
        devices:
          samplerate: 48000
          chunksize: 1024
          target_level: 2048
          volume_limit: 0.0
          capture:
            type: Alsa
            device: "plug:jasper_capture"
          playback:
            type: Alsa
            device: "outputd_content_playback"
        filters:
          flat:
            type: Gain
        """
    )

    assert parsed == {
        "samplerate": 48000,
        "chunksize": 1024,
        "target_level": 2048,
        "capture_device": "plug:jasper_capture",
        "playback_device": "outputd_content_playback",
    }
