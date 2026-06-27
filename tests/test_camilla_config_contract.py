# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.camilla_config_contract import (
    DEFAULT_CHUNKSIZE,
    DEFAULT_TARGET_LEVEL,
    PeqFilter,
    parse_camilla_devices_config,
    resolve_camilla_chunksize,
    resolve_camilla_target_level,
    total_positive_boost_db,
)


def test_camilla_latency_knobs_default_to_literals_when_unset():
    """G7: with the env vars unset the resolvers return the shipped literals, so
    threading them through the emitters changes no emitted YAML."""
    assert resolve_camilla_chunksize({}) == DEFAULT_CHUNKSIZE == 1024
    assert resolve_camilla_target_level({}) == DEFAULT_TARGET_LEVEL == 2048


def test_camilla_latency_knobs_read_env_override():
    """A valid positive override is honored."""
    assert resolve_camilla_chunksize({"JASPER_CAMILLA_CHUNKSIZE": "512"}) == 512
    assert (
        resolve_camilla_target_level({"JASPER_CAMILLA_TARGET_LEVEL": "1024"}) == 1024
    )


def test_camilla_latency_knobs_reject_malformed_to_default():
    """A bad override must degrade to the default rather than produce a config
    that won't load (non-int, zero, negative, blank all fall back)."""
    for bad in ("", "  ", "bogus", "0", "-256", "1.5"):
        assert resolve_camilla_chunksize({"JASPER_CAMILLA_CHUNKSIZE": bad}) == (
            DEFAULT_CHUNKSIZE
        ), bad
        assert resolve_camilla_target_level(
            {"JASPER_CAMILLA_TARGET_LEVEL": bad}
        ) == DEFAULT_TARGET_LEVEL, bad


def test_camilla_emitters_emit_byte_identical_yaml_when_env_unset(monkeypatch):
    """The end-to-end byte-identical contract: the sound emitter with the None
    sentinel (env unset) must equal the pre-G7 explicit-literal call."""
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    profile = SoundProfile()
    explicit = emit_sound_config(
        profile, chunksize=DEFAULT_CHUNKSIZE, target_level=DEFAULT_TARGET_LEVEL
    )
    sentinel = emit_sound_config(profile)  # None → resolve → defaults
    assert sentinel == explicit


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
