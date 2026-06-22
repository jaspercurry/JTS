# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.camilla_config_contract import (
    PeqFilter,
    parse_camilla_devices_config,
    total_positive_boost_db,
)


def test_total_positive_boost_db_sums_only_boosts():
    # The canonical audio-safety primitive: worst-case additive boost.
    # Cuts are ignored; the result is the headroom a config must reserve so
    # boosts can't clip above unity. Shared by the emitter trim and the PEQ
    # boost-cap check, so pin it here.
    assert total_positive_boost_db([]) == 0.0
    assert total_positive_boost_db([PeqFilter(80, 4, -6.0)]) == 0.0  # cuts-only
    assert total_positive_boost_db(
        [PeqFilter(45, 5, 2.0), PeqFilter(80, 6, -4.0), PeqFilter(120, 4, 1.0)]
    ) == 3.0  # +2 and +1 stack; the -4 cut is not subtracted


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
            channels: 2
            device: "plug:jasper_capture"
          playback:
            type: Alsa
            channels: 2
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
        "volume_limit": 0.0,
        "capture_channels": 2,
        "capture_device": "plug:jasper_capture",
        "playback_channels": 2,
        "playback_device": "outputd_content_playback",
    }
