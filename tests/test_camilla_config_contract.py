# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.camilla_config_contract import (
    DEFAULT_CHUNKSIZE,
    DEFAULT_LEAN_CAPTURE_FIFO,
    DEFAULT_TARGET_LEVEL,
    PeqFilter,
    file_capture_resampler_yaml,
    parse_camilla_devices_config,
    resolve_camilla_chunksize,
    resolve_camilla_target_level,
    snd_aloop_rate_adjust_oscillation_reason,
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


def test_camilla_latency_knobs_use_profile_floor_when_env_unset():
    """#27: with the operator env unset, the active DAC's profile floor wins
    over the global default."""
    assert resolve_camilla_chunksize({}, profile_floor=256) == 256
    assert resolve_camilla_target_level({}, profile_floor=1024) == 1024


def test_camilla_latency_knobs_operator_env_beats_profile_floor():
    """#27 precedence: explicit operator env > active DacProfile floor."""
    assert (
        resolve_camilla_chunksize(
            {"JASPER_CAMILLA_CHUNKSIZE": "512"}, profile_floor=256
        )
        == 512
    )
    assert (
        resolve_camilla_target_level(
            {"JASPER_CAMILLA_TARGET_LEVEL": "2048"}, profile_floor=1024
        )
        == 2048
    )


def test_camilla_latency_knobs_malformed_env_falls_back_to_profile_floor():
    """A bad operator override degrades to the profile floor (not the global
    default) when a floor is present — still never an unloadable config."""
    for bad in ("", "bogus", "0", "-1"):
        assert (
            resolve_camilla_chunksize(
                {"JASPER_CAMILLA_CHUNKSIZE": bad}, profile_floor=256
            )
            == 256
        ), bad


def test_camilla_latency_knobs_none_floor_keeps_global_default():
    """profile_floor=None is the non-breaking path — byte-identical to the
    no-floor behavior."""
    assert resolve_camilla_chunksize({}, profile_floor=None) == DEFAULT_CHUNKSIZE
    assert (
        resolve_camilla_target_level({}, profile_floor=None) == DEFAULT_TARGET_LEVEL
    )


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


# --- G8: snd-aloop rate_adjust + async-resampler oscillation guard ---
# The MIRROR of the lean-lane File-capture guard. The documented failure is the
# metastable AirPlay-dropout oscillation when a snd-aloop capture
# (plug:jasper_capture / hw:Loopback) at capture-rate == playback-rate runs BOTH
# enable_rate_adjust AND an async resampler — CamillaDSP's adjuster fights the
# loopback's own rate tracking. The safe shape is enable_rate_adjust true AND no
# async resampler block. These pin that contract so a future emitter edit can't
# silently re-introduce the oscillation.


def _standard_sound_config(**kwargs) -> str:
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    return emit_sound_config(SoundProfile(), **kwargs)


def test_standard_snd_aloop_config_is_safe_and_rate_adjusted():
    """The shipped default config taps the snd-aloop summed lane: it MUST carry
    enable_rate_adjust true (the loopback round-trip needs it) and MUST NOT
    carry an async resampler block."""
    yaml = _standard_sound_config()
    assert 'device: "plug:jasper_capture"' in yaml
    assert "enable_rate_adjust: true" in yaml
    assert "resampler:" not in yaml
    assert snd_aloop_rate_adjust_oscillation_reason(yaml) is None


def test_guard_flags_async_resampler_on_snd_aloop_capture():
    """Inject the oscillation: an async resampler block alongside
    enable_rate_adjust on the snd-aloop capture. The guard must catch it."""
    safe = _standard_sound_config()
    oscillating = safe.replace(
        "  enable_rate_adjust: true",
        "  enable_rate_adjust: true"
        + file_capture_resampler_yaml("AsyncSinc", "Balanced"),
    )
    reason = snd_aloop_rate_adjust_oscillation_reason(oscillating)
    assert reason is not None
    assert "oscillation" in reason
    assert "AsyncSinc" in reason


def test_guard_ignores_file_capture_lean_config():
    """A File-capture lean config legitimately pairs enable_rate_adjust with an
    async resampler — it is clockless and has its OWN guard. The snd-aloop guard
    must not fire on it (its capture is a File pipe, not the loopback)."""
    lean = _standard_sound_config(
        capture_pipe_path=DEFAULT_LEAN_CAPTURE_FIFO,
        enable_rate_adjust=True,
        resampler_type="AsyncSinc",
    )
    assert "resampler:" in lean  # it DOES carry one — but on a File capture
    assert snd_aloop_rate_adjust_oscillation_reason(lean) is None


def test_guard_ignores_bonded_leader_pipe_config():
    """The bonded-leader pipe sink sets enable_rate_adjust:false on its snd-aloop
    capture (snapclient is the sole rate-tracker). That is NOT the oscillation
    and must not be flagged."""
    leader = _standard_sound_config(
        playback_pipe_path="/run/snapserver/snapfifo",
        enable_rate_adjust=False,
    )
    assert 'device: "plug:jasper_capture"' in leader
    assert snd_aloop_rate_adjust_oscillation_reason(leader) is None


def test_active_speaker_baseline_snd_aloop_config_is_safe():
    """The active-speaker baseline emitter also taps plug:jasper_capture; assert
    it ships the safe rate-adjust shape too."""
    from jasper.active_speaker import (
        ActiveSpeakerPreset,
        emit_active_speaker_baseline_config,
    )
    from tests.test_active_speaker_profile import _two_way_preset

    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    yaml = emit_active_speaker_baseline_config(
        preset, playback_device="active_dac_playback"
    )
    assert 'device: "plug:jasper_capture"' in yaml
    assert "enable_rate_adjust: true" in yaml
    assert snd_aloop_rate_adjust_oscillation_reason(yaml) is None
