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
    snd_aloop_rate_adjust_oscillation_reason,
    total_positive_boost_db,
)

def test_camilla_latency_knobs_default_to_literals_when_unset():
    """G7: with the env vars unset and no profile floor the resolvers return the
    shipped literals. ``profile_floor=None`` pins the no-floor path so this tests
    pure env-vs-default behavior independent of any active-DAC state on the host."""
    assert resolve_camilla_chunksize({}, profile_floor=None) == DEFAULT_CHUNKSIZE == 1024
    assert (
        resolve_camilla_target_level({}, profile_floor=None)
        == DEFAULT_TARGET_LEVEL
        == 2048
    )


def test_camilla_latency_knobs_read_env_override():
    """A valid positive override is honored."""
    assert (
        resolve_camilla_chunksize(
            {"JASPER_CAMILLA_CHUNKSIZE": "512"}, profile_floor=None
        )
        == 512
    )
    assert (
        resolve_camilla_target_level(
            {"JASPER_CAMILLA_TARGET_LEVEL": "1024"}, profile_floor=None
        )
        == 1024
    )


def test_camilla_latency_knobs_reject_malformed_to_default():
    """A bad override must degrade to the default rather than produce a config
    that won't load (non-int, zero, negative, blank all fall back)."""
    for bad in ("", "  ", "bogus", "0", "-256", "1.5"):
        assert resolve_camilla_chunksize(
            {"JASPER_CAMILLA_CHUNKSIZE": bad}, profile_floor=None
        ) == DEFAULT_CHUNKSIZE, bad
        assert resolve_camilla_target_level(
            {"JASPER_CAMILLA_TARGET_LEVEL": bad}, profile_floor=None
        ) == DEFAULT_TARGET_LEVEL, bad


def test_camilla_latency_knobs_use_profile_floor_when_env_unset():
    """#27: with the operator env unset, the active DAC's profile floor wins
    over the global default."""
    assert resolve_camilla_chunksize({}, profile_floor=256) == 256
    assert resolve_camilla_target_level({}, profile_floor=1024) == 1024


def test_camilla_latency_knobs_operator_env_can_raise_above_profile_floor():
    """#27 precedence: explicit operator env can raise above the floor."""
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


def test_camilla_latency_knobs_clamp_operator_env_below_profile_floor():
    """Saved/stale env below a measured DAC floor is clamped to the floor.

    This pins the jts5 256/512 saved-profile gap: once the Apple DAC declares
    256/1536, an old ``JASPER_CAMILLA_TARGET_LEVEL=512`` must not keep
    regenerated CamillaDSP configs below the measured floor.
    """
    assert (
        resolve_camilla_chunksize(
            {"JASPER_CAMILLA_CHUNKSIZE": "128"}, profile_floor=256
        )
        == 256
    )
    assert (
        resolve_camilla_target_level(
            {"JASPER_CAMILLA_TARGET_LEVEL": "512"}, profile_floor=1536
        )
        == 1536
    )


def test_camilla_latency_lab_override_can_probe_below_profile_floor(tmp_path):
    from jasper.audio_runtime_overrides import (
        AUDIO_RUNTIME_OVERRIDES_PATH_ENV,
        set_runtime_override,
    )

    override_path = tmp_path / "audio_runtime_overrides.json"
    set_runtime_override(
        key="JASPER_CAMILLA_TARGET_LEVEL",
        value="1024",
        reason="test low-latency target",
        path=override_path,
        allowed_keys={"JASPER_CAMILLA_TARGET_LEVEL"},
    )

    assert (
        resolve_camilla_target_level(
            {
                "JASPER_CAMILLA_TARGET_LEVEL": "1024",
                AUDIO_RUNTIME_OVERRIDES_PATH_ENV: str(override_path),
            },
            profile_floor=1536,
        )
        == 1024
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
    sentinel (env unset, no resolvable profile) must equal the pre-G7
    explicit-literal call."""
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    # Point profile resolution at an absent state file so no floor resolves —
    # the global-default (byte-identical) path.
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH", "/nonexistent/jts-output-hardware.json"
    )
    profile = SoundProfile()
    explicit = emit_sound_config(
        profile, chunksize=DEFAULT_CHUNKSIZE, target_level=DEFAULT_TARGET_LEVEL
    )
    sentinel = emit_sound_config(profile)  # None → resolve → defaults
    assert sentinel == explicit


# --- #27: the active DAC profile floor reaches a GENERATED CamillaDSP config ---
# The keystone claim the prior tests did NOT cover: not "the resolver returns N"
# but "a config GENERATED for the Apple-dongle profile actually carries
# chunksize 256 / target_level 1536." These run the live emitters (sound +
# active-speaker) with the active output-hardware state staged, then parse the
# emitted YAML's devices: block — proving the floor is in the config a daemon
# would load, with max(operator-env, profile-floor) > global precedence.


def _stage_output_profile(monkeypatch, tmp_path, profile_id: str) -> None:
    """Write an output-hardware state file the generators resolve the floor from.

    Mirrors what jasper-audio-hardware-reconcile writes to
    /run/jasper-output-hardware/output_hardware.json; the generators read the
    active profile id from it (env-independent) and look up its codified floor.
    """
    from jasper.output_hardware import OutputHardwareState, write_state

    state_path = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_path))
    write_state(
        OutputHardwareState(
            profile_id=profile_id,
            profile_label=profile_id,
            status="ready",
            physical_output_count=2,
        ),
        state_path,
    )


def _generated_sound_devices(monkeypatch, tmp_path, profile_id: str) -> dict:
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    _stage_output_profile(monkeypatch, tmp_path, profile_id)
    return parse_camilla_devices_config(emit_sound_config(SoundProfile()))


def test_generated_sound_config_uses_apple_dongle_floor(monkeypatch, tmp_path):
    """Apple-dongle profile => generated CamillaDSP config carries 256 / 1536."""
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    parsed = _generated_sound_devices(monkeypatch, tmp_path, "apple_usb_c_dongle")
    assert parsed["chunksize"] == 256
    assert parsed["target_level"] == 1536


def test_fresh_flat_outputd_cutover_uses_apple_dongle_floor(monkeypatch, tmp_path):
    """Fresh flat startup config is generated with the active profile floor.

    This pins the #27 blocker: the installed flat cutover path must not keep
    booting an Apple-dongle box at the static 1024 / 2048 default.
    """
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    _stage_output_profile(monkeypatch, tmp_path, "apple_usb_c_dongle")

    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    out = tmp_path / "outputd-cutover.yml"
    parsed = parse_camilla_devices_config(
        emit_flat_outputd_cutover_config(out_path=out)
    )
    assert out.exists()
    assert parsed["chunksize"] == 256
    assert parsed["target_level"] == 1536
    assert parsed["playback_device"] == "outputd_content_playback"


def test_generated_sound_config_dac8x_uses_global_default(monkeypatch, tmp_path):
    """DAC8x declares no floor => the generated config keeps 1024 / 2048."""
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    parsed = _generated_sound_devices(monkeypatch, tmp_path, "hifiberry_dac8x")
    assert parsed["chunksize"] == DEFAULT_CHUNKSIZE == 1024
    assert parsed["target_level"] == DEFAULT_TARGET_LEVEL == 2048


def test_generated_sound_config_operator_env_can_raise_above_profile_floor(
    monkeypatch, tmp_path
):
    """Operator env can raise above the Apple floor in generated config."""
    monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", "384")
    monkeypatch.setenv("JASPER_CAMILLA_TARGET_LEVEL", "1536")
    parsed = _generated_sound_devices(monkeypatch, tmp_path, "apple_usb_c_dongle")
    assert parsed["chunksize"] == 384
    assert parsed["target_level"] == 1536


def test_generated_sound_config_clamps_stale_env_below_apple_dongle_floor(
    monkeypatch, tmp_path,
):
    """A saved-profile re-render must lift old 256/512 env to 256/1536."""
    monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", "256")
    monkeypatch.setenv("JASPER_CAMILLA_TARGET_LEVEL", "512")
    parsed = _generated_sound_devices(monkeypatch, tmp_path, "apple_usb_c_dongle")
    assert parsed["chunksize"] == 256
    assert parsed["target_level"] == 1536


def test_generated_sound_config_no_state_file_uses_global_default(
    monkeypatch, tmp_path
):
    """No resolvable profile (state file absent) => global default, unchanged."""
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    monkeypatch.setenv(
        "JASPER_OUTPUT_HARDWARE_STATE_PATH", str(tmp_path / "absent.json")
    )
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    parsed = parse_camilla_devices_config(emit_sound_config(SoundProfile()))
    assert parsed["chunksize"] == DEFAULT_CHUNKSIZE
    assert parsed["target_level"] == DEFAULT_TARGET_LEVEL


def test_generated_active_speaker_baseline_uses_apple_dongle_floor(
    monkeypatch, tmp_path
):
    """The active-speaker baseline generator (the install.sh runtime-safe-graph
    and jasper-control path) also carries the Apple-dongle floor."""
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    _stage_output_profile(monkeypatch, tmp_path, "apple_usb_c_dongle")
    from jasper.active_speaker import (
        ActiveSpeakerPreset,
        emit_active_speaker_baseline_config,
    )
    from tests.test_active_speaker_profile import _two_way_preset

    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    yaml = emit_active_speaker_baseline_config(
        preset, playback_device="outputd_active_content_playback"
    )
    parsed = parse_camilla_devices_config(yaml)
    assert parsed["chunksize"] == 256
    assert parsed["target_level"] == 1536


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


def test_parse_camilla_devices_config_rejects_ambiguous_volume_limit() -> None:
    assert "volume_limit" not in parse_camilla_devices_config(
        "devices:\n"
        "  volume_limit: 0.0\n"
        "  volume_limit: 9.0\n"
    )
    assert "volume_limit" not in parse_camilla_devices_config(
        "devices:\n"
        "  volume_limit: 0.0\n"
        "devices: {volume_limit: 9.0}\n"
    )
    for value in ("nan", "inf", "-inf"):
        assert "volume_limit" not in parse_camilla_devices_config(
            f"devices:\n  volume_limit: {value}\n"
        )


def test_parse_camilla_devices_config_ignores_nested_volume_limit() -> None:
    for nested_block in ("playback", "metadata"):
        parsed = parse_camilla_devices_config(
            "devices:\n"
            f"  {nested_block}:\n"
            "    volume_limit: 0.0\n"
        )

        assert "volume_limit" not in parsed


# --- G8: snd-aloop rate_adjust + async-resampler oscillation guard ---
# The documented failure is the metastable AirPlay-dropout oscillation when a
# snd-aloop capture
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
        "  enable_rate_adjust: true\n"
        "  resampler:\n"
        "    type: AsyncSinc\n"
        "    profile: Balanced",
    )
    reason = snd_aloop_rate_adjust_oscillation_reason(oscillating)
    assert reason is not None
    assert "oscillation" in reason
    assert "AsyncSinc" in reason


def test_guard_ignores_stale_raw_file_capture_config():
    """The snd-aloop guard ignores a stale legacy RawFile capture config."""
    file_capture = """devices:
  samplerate: 48000
  enable_rate_adjust: true
  resampler:
    type: AsyncSinc
    profile: Balanced
  capture:
    type: RawFile
    channels: 2
    filename: "/run/jasper-fanin/camilla.pipe"
    format: S32_LE
"""
    assert snd_aloop_rate_adjust_oscillation_reason(file_capture) is None


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
