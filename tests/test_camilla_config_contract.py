# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.audio_hardware.dac import HIFIBERRY_DAC8X_STUDIO_ID
from jasper.camilla_config_contract import (
    CamillaFloor,
    DEFAULT_CHUNKSIZE,
    POST_DSP_PLAYBACK_DEVICES,
    UNPAIRED_POST_DSP_PLAYBACK_DEVICES,
    _OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE,
    DEFAULT_PIPE_SINK_FORMAT,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_TARGET_LEVEL,
    PeqFilter,
    parse_camilla_devices_config,
    read_camilla_device_field,
    resolve_camilla_chunksize,
    resolve_camilla_target_level,
    resolve_enable_rate_adjust,
    total_positive_boost_db,
)
from jasper.fanin_coupling import (
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAMILLA_GEOMETRY,
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    ring_capacity_frames,
)


def test_pipe_sink_format_stays_narrow_while_the_alsa_lane_is_wide():
    """D4 (wide-output-path program): the pipe/File-sink format is its own
    constant, separate from the ALSA loopback lane's DEFAULT_PLAYBACK_FORMAT.
    PR-1 split them while both still read ``S16_LE``; PR-6 widened the ALSA lane
    and this is where that split stopped being latent. The pipe sink MUST stay
    narrow: snapserver's pipe source is a fixed wire contract
    (`sampleformat=48000:16:2`, jasper.multiroom.reconcile.snapserver_argv), so
    a shared name here would have silently corrupted every bonded leader's
    multiroom wire the moment the lane widened."""
    assert DEFAULT_PIPE_SINK_FORMAT == "S16_LE"
    assert DEFAULT_PLAYBACK_FORMAT == "S32_LE"
    assert DEFAULT_PIPE_SINK_FORMAT != DEFAULT_PLAYBACK_FORMAT


def test_pipe_sink_format_matches_snapserver_wire_contract():
    """NIT2 (PR-1 gate review): pin the promise between the two owners of the
    snapserver pipe wire format — DEFAULT_PIPE_SINK_FORMAT (this module) and
    the ``sampleformat=`` literal baked into
    jasper.multiroom.reconcile.snapserver_argv. They can't share code (one is
    a Python constant the CamillaDSP emitters read, the other is a literal
    inside a DIFFERENT daemon's argv builder), so this test pins them
    together the same way tests/test_wifi_profile_hardening_contract.py pins
    its three writers to one canonical contract: widening either side alone,
    without the other, fails a test naming the other.

    "16:2" is snapserver's own ``<bits>:<channels>`` syntax for its pipe
    source, not a JTS format literal — it can only mean ``S16_LE`` (no other
    live format has a 16-bit depth), so the assertion below is a genuine
    "iff": the argv carries ``48000:16:2`` iff the constant is ``S16_LE``.
    """
    from jasper.multiroom.config import DEFAULT_BUFFER_MS, DEFAULT_CODEC, GroupingConfig
    from jasper.multiroom.reconcile import snapserver_argv

    cfg = GroupingConfig(
        enabled=True,
        role="leader",
        channel="left",
        bond_id="contract-test",
        leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error=None,
    )
    argv_carries_16_2 = "sampleformat=48000:16:2" in " ".join(snapserver_argv(cfg))
    assert argv_carries_16_2 == (DEFAULT_PIPE_SINK_FORMAT == "S16_LE")


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
    # At a sink with its own buffer. The ring end is the deliberate exception —
    # there the sentinel path clamps to the ring's capacity while an explicit
    # caller value still passes through, so the two calls are NOT byte-identical
    # for a default above that capacity (see
    # test_a_ring_end_never_emits_a_chunk_larger_than_the_ring).
    explicit = emit_sound_config(
        profile,
        chunksize=DEFAULT_CHUNKSIZE,
        target_level=DEFAULT_TARGET_LEVEL,
        playback_device=NON_RING_SINK,
    )
    sentinel = emit_sound_config(profile, playback_device=NON_RING_SINK)
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
            selected_card_id=profile_id,
        ),
        state_path,
    )


# An ordinary ALSA sink — NOT one of the ring PCMs. The floor-resolution tests
# below emit against it because the ring CAPS what a resolved floor can put in
# the YAML (resolve_camilla_latency_for_devices): at a ring end a chunk above
# the ring's capacity is clamped, so a resolver answer larger than that is only
# observable at a sink that has its own buffer. The clamp itself is pinned by
# test_a_ring_end_never_emits_a_chunk_larger_than_the_ring.
NON_RING_SINK = "hw:CARD=DAC8x,DEV=0"


def _generated_sound_devices(
    monkeypatch, tmp_path, profile_id: str, *, playback_device: str | None = None
) -> dict:
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    _stage_output_profile(monkeypatch, tmp_path, profile_id)
    kwargs = {} if playback_device is None else {"playback_device": playback_device}
    return parse_camilla_devices_config(emit_sound_config(SoundProfile(), **kwargs))


def test_generated_sound_config_uses_apple_dongle_floor(monkeypatch, tmp_path):
    """Apple-dongle profile => generated CamillaDSP config carries 256 / 1536."""
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    parsed = _generated_sound_devices(monkeypatch, tmp_path, "apple_usb_c_dongle")
    assert parsed["chunksize"] == 256
    assert parsed["target_level"] == 1536


def test_fresh_flat_outputd_cutover_takes_the_ring_geometry(monkeypatch, tmp_path):
    """The flat startup config carries the RING's geometry, not the DAC floor.

    The DAC profile's Camilla floor governs graphs whose playback is an ordinary
    ALSA device. This graph's is Ring B, and the ioplug pins the ring's period
    bytes min==max — an Apple-dongle floor of chunk 256 cannot negotiate it at
    all, so a floor here would fail the open rather than raise it. The floor
    still reaches every other emitted config.
    """
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    _stage_output_profile(monkeypatch, tmp_path, "apple_usb_c_dongle")

    from jasper.fanin_coupling import (
        RING_CAMILLA_CHUNKSIZE,
        RING_CAMILLA_TARGET_LEVEL,
        RING_PLAYBACK_DEVICE,
    )
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    out = tmp_path / "outputd-cutover.yml"
    parsed = parse_camilla_devices_config(
        emit_flat_outputd_cutover_config(out_path=out)
    )
    assert out.exists()
    assert parsed["chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert parsed["target_level"] == RING_CAMILLA_TARGET_LEVEL
    assert parsed["playback_device"] == RING_PLAYBACK_DEVICE


def test_generated_sound_config_floorless_dac_uses_global_default(
    monkeypatch, tmp_path
):
    """A profile with no declared floor => the generated config keeps 1024/2048."""
    from jasper.audio_hardware.dac import (
        HIFIBERRY_DAC8X_STUDIO_ID,
        camilla_floor_for,
    )

    # Asserted, not assumed: a later floor declaration for this profile must
    # fail HERE rather than silently turning the two assertions below into a
    # test of the floor path (what an R7a DAC8x floor did to this test, and
    # what jts4's measured floor then did to its InnoMaker replacement).
    assert camilla_floor_for(HIFIBERRY_DAC8X_STUDIO_ID) is None
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    parsed = _generated_sound_devices(
        monkeypatch,
        tmp_path,
        HIFIBERRY_DAC8X_STUDIO_ID,
        playback_device=NON_RING_SINK,
    )
    assert parsed["chunksize"] == DEFAULT_CHUNKSIZE == 1024
    assert parsed["target_level"] == DEFAULT_TARGET_LEVEL == 2048


def test_generated_sound_config_operator_env_can_raise_above_profile_floor(
    monkeypatch, tmp_path
):
    """Operator env can raise above the Apple floor in generated config."""
    monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", "384")
    monkeypatch.setenv("JASPER_CAMILLA_TARGET_LEVEL", "1536")
    parsed = _generated_sound_devices(
        monkeypatch, tmp_path, "apple_usb_c_dongle", playback_device=NON_RING_SINK
    )
    assert parsed["chunksize"] == 384
    assert parsed["target_level"] == 1536


@pytest.mark.parametrize(
    "profile_id,env_chunk",
    [
        # A DAC that declares no floor at all falls to the 1024 global default.
        (HIFIBERRY_DAC8X_STUDIO_ID, None),
        # An operator raise lands above the ring's capacity too.
        ("apple_usb_c_dongle", "384"),
    ],
)
def test_a_ring_end_never_emits_a_chunk_larger_than_the_ring(
    monkeypatch, tmp_path, profile_id, env_chunk
):
    """A ring-crossing graph carries a chunk the ring can actually negotiate.

    CamillaDSP sets avail_min to its chunk and ALSA refuses an avail_min above
    the device's buffer, so a chunk over the ring's capacity does not degrade —
    it exits at open and systemd restart-loops it (observed on jts4: "Trying to
    set avail_min to 1024, must be smaller than or equal to device buffer size
    of 256", ten restarts, no audio on the box at all).

    The sources a chunk can arrive from are covered together because the
    clamp is about the TRANSPORT, not about where the number came from. The
    InnoMaker is not one of them any more: its declared 256/1024 already fits
    the ring (see test_a_floor_that_already_fits_the_ring_is_not_clamped).
    """
    if env_chunk is None:
        monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    else:
        monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", env_chunk)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)

    parsed = _generated_sound_devices(monkeypatch, tmp_path, profile_id)

    assert parsed["playback_device"] == RING_PLAYBACK_DEVICE
    assert parsed["chunksize"] == ring_capacity_frames()


def test_a_clamped_chunk_scales_its_target_with_it(monkeypatch, tmp_path):
    """The (chunk, target) pair moves together, because its ceiling does.

    CamillaDSP bounds target_level at ``chunksize x (queuelimit + 4)``
    (measured against 4.1.3 on jts4; exact across chunk 128/256/512 and
    queuelimit 1/2/4), so shrinking the chunk shrinks the ceiling. Clamping
    the chunk alone put jts4's floor-declared 4096 over the new 2048 ceiling
    and swapped one crash loop for another (the InnoMaker now declares the
    already-scaled 256/1024 instead, so a floorless profile exercises the
    live scaling path here).

    Scaling preserves the RATIO already in play (the floorless global
    default's 2x) rather than substituting a number of our own.
    """
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)

    parsed = _generated_sound_devices(
        monkeypatch, tmp_path, HIFIBERRY_DAC8X_STUDIO_ID
    )

    chunk, target = parsed["chunksize"], parsed["target_level"]
    assert chunk == ring_capacity_frames()
    # The default 1024/2048 scaled by the same factor the chunk was.
    assert target == 512
    # And inside CamillaDSP's ceiling for the emitted queuelimit.
    assert target <= chunk * (parsed["queuelimit"] + 4)


@pytest.mark.parametrize(
    "profile_id,chunksize,target_level",
    [
        ("apple_usb_c_dongle", 256, 1536),
        # jts4's declared pair is the ring clamp's OWN output (#3542): 256 is
        # already the ring's capacity, so nothing here clamps or scales it.
        ("innomaker_hifi_amp_pro", 256, 1024),
    ],
)
def test_a_floor_that_already_fits_the_ring_is_not_clamped(
    monkeypatch, tmp_path, profile_id, chunksize, target_level
):
    """The clamp is a ceiling, not a retune.

    jts.local runs the Apple floor's 256 across this ring healthily, so a floor
    at or under the ring's capacity is the box's own tuning and is emitted
    whole. Were this to answer the certified RING_CAMILLA_CHUNKSIZE instead,
    every commissioned box would be silently re-tuned by a bug fix.
    """
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)

    parsed = _generated_sound_devices(monkeypatch, tmp_path, profile_id)

    assert parsed["playback_device"] == RING_PLAYBACK_DEVICE
    assert parsed["chunksize"] == chunksize <= ring_capacity_frames()
    assert parsed["target_level"] == target_level


def test_a_clockless_sink_clamps_on_its_ring_capture(monkeypatch, tmp_path):
    """A File sink declares no buffer, so the ring CAPTURE is the bound.

    The parked graph's /dev/null sink and the bonded leader's snapserver FIFO
    both still capture Ring A, so an oversized chunk fails their open just as it
    fails a ring playback's.
    """
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    _stage_output_profile(monkeypatch, tmp_path, HIFIBERRY_DAC8X_STUDIO_ID)

    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    parsed = parse_camilla_devices_config(
        emit_sound_config(
            SoundProfile(),
            playback_pipe_path=str(tmp_path / "snapfifo"),
            enable_rate_adjust=False,
        )
    )

    assert parsed["capture_device"] == RING_CAPTURE_DEVICE
    assert parsed["chunksize"] == ring_capacity_frames()


@pytest.mark.parametrize(
    ("playback_device", "rate_adjust"),
    [
        (RING_PLAYBACK_DEVICE, False),
        (RING_ACTIVE_PLAYBACK_DEVICE, False),
        (None, False),
        ("hw:CARD=Dac,DEV=0", True),
    ],
    ids=["ring-b", "ring-active", "file", "alsa-dac"],
)
def test_enable_rate_adjust_follows_the_sink(playback_device, rate_adjust):
    """The sink decides, never the graph's role: a File sink (``None``) has no
    output clock to steer, and a ring PCM is an ioplug alsa-lib reports as card
    -1, so CamillaDSP builds no HCtl and can actuate nothing. An ordinary ALSA
    sink can.
    """

    assert resolve_enable_rate_adjust(playback_device) is rate_adjust


def test_emit_sound_config_defaults_rate_adjust_from_the_sink():
    """The emitter's own default is the resolver's answer for the sink it
    emits — ring by default, True once pointed at an ordinary DAC."""

    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    assert (
        parse_camilla_devices_config(emit_sound_config(SoundProfile()))[
            "enable_rate_adjust"
        ]
        is False
    )
    assert (
        parse_camilla_devices_config(
            emit_sound_config(SoundProfile(), playback_device="hw:CARD=Dac,DEV=0")
        )["enable_rate_adjust"]
        is True
    )


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

    parsed = parse_camilla_devices_config(
        emit_sound_config(SoundProfile(), playback_device=NON_RING_SINK)
    )
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


def test_generated_active_speaker_baseline_carries_the_dac8x_floor(
    monkeypatch, tmp_path
):
    """R7a: an EMITTED roleful baseline carries the DAC8x floor (soak gap).

    The jts3 soak proved the 256/1536 geometry by pushing it into the RUNNING
    CamillaDSP over the websocket — it never re-emitted the applied baseline, so
    it could not prove that the next regeneration of that config (install.sh's
    runtime-safe-graph, a jasper-control re-emit, a re-apply after a
    correction) reproduces the geometry from the declaration alone. That is what
    this pins, on the shape jts3 actually runs: a roleful two-way baseline
    resolving the floor through ``resolve_camilla_chunksize`` /
    ``resolve_camilla_target_level``.

    The floorless control below is what makes the assertion mean "the DECLARED
    floor moved this", not "256 happens to be the default".
    """
    from jasper.active_speaker import (
        ActiveSpeakerPreset,
        emit_active_speaker_baseline_config,
    )
    from jasper.active_speaker.runtime_contract import (
        classify_output_contract,
        topology_supports_shm_ring,
    )
    from jasper.audio_hardware.dac import HIFIBERRY_DAC8X_STUDIO_ID
    from tests.test_active_speaker_profile import _two_way_preset
    from tests.test_active_speaker_runtime_contract import _active_topology

    # The topology this preset stands for really is roleful — the jts3 shape,
    # and the reason this box has no ring width of its own.
    topology = _active_topology("mono", "active_2_way")
    assert classify_output_contract(topology).requires_roleful_graph
    assert not topology_supports_shm_ring(topology)

    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))

    _stage_output_profile(monkeypatch, tmp_path, "hifiberry_dac8x")
    parsed = parse_camilla_devices_config(
        emit_active_speaker_baseline_config(
            preset, playback_device="outputd_active_content_playback"
        )
    )
    assert parsed["chunksize"] == 256
    assert parsed["target_level"] == 1536

    # Control: same emit, same preset, a profile that declares NO floor.
    # (Named the InnoMaker until it declared jts4's measured floor.)
    _stage_output_profile(monkeypatch, tmp_path, HIFIBERRY_DAC8X_STUDIO_ID)
    control = parse_camilla_devices_config(
        emit_active_speaker_baseline_config(
            preset, playback_device="outputd_active_content_playback"
        )
    )
    assert control["chunksize"] == DEFAULT_CHUNKSIZE == 1024
    assert control["target_level"] == DEFAULT_TARGET_LEVEL == 2048


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


_TWO_BLOCK_CFG = """\
devices:
  samplerate: 48000
  capture:
    type: RawFile
    channels: 2
    filename: "/run/jasper-fanin/camilla.pipe"
  playback:
    type: File
    channels: 2
    device: "outputd_content_playback"
    filename: "/run/jasper-outputd/content.pipe"
filters:
"""


@pytest.mark.parametrize(
    ("text", "block", "field", "expected"),
    [
        (_TWO_BLOCK_CFG, "capture", "type", "RawFile"),
        # The playback sink's own `type` must never be read as the capture's.
        (_TWO_BLOCK_CFG, "playback", "type", "File"),
        (_TWO_BLOCK_CFG, "playback", "filename", "/run/jasper-outputd/content.pipe"),
        (_TWO_BLOCK_CFG, "playback", "device", "outputd_content_playback"),
        # The capture block has no `device`, and the lookup must stop at the
        # sibling boundary rather than falling through to playback's.
        (_TWO_BLOCK_CFG, "capture", "device", None),
        ("filters:\n  x: 1\n", "capture", "type", None),
    ],
)
def test_read_camilla_device_field_is_scoped_to_its_block(
    tmp_path, text: str, block: str, field: str, expected: str | None
) -> None:
    config = tmp_path / "c.yml"
    config.write_text(text, encoding="utf-8")

    assert read_camilla_device_field(config, block, field) == expected


def test_read_camilla_device_field_returns_none_without_a_readable_config(
    tmp_path,
) -> None:
    assert read_camilla_device_field(tmp_path / "missing.yml", "capture", "type") is None
    assert read_camilla_device_field(None, "capture", "type") is None


def test_parse_camilla_devices_config_ignores_nested_volume_limit() -> None:
    for nested_block in ("playback", "metadata"):
        parsed = parse_camilla_devices_config(
            "devices:\n"
            f"  {nested_block}:\n"
            "    volume_limit: 0.0\n"
        )

        assert "volume_limit" not in parsed




@pytest.mark.parametrize(
    "chunksize,target_level,reason",
    [
        (0, 1024, "chunksize must be > 0"),
        (256, 1023, "target_level must be >= 4 x chunksize"),
        # The declaration that took jts4 silent: a chunk the ring cannot open.
        (1024, 4096, "exceeds the ring"),
    ],
)
def test_camilla_floor_refuses_what_camilladsp_could_not_run(
    chunksize, target_level, reason,
):
    with pytest.raises(ValueError, match=reason):
        CamillaFloor(chunksize=chunksize, target_level=target_level)


def test_camilla_floor_accepts_the_ring_capacity_at_exactly_4x():
    capacity = ring_capacity_frames()

    floor = CamillaFloor(chunksize=capacity, target_level=4 * capacity)

    assert (floor.chunksize, floor.target_level) == (capacity, 4 * capacity)


def test_the_certified_ring_pairing_agrees_with_the_sink_rule():
    """RING_CAMILLA_GEOMETRY is measured data passed whole by the end-to-end
    ring graphs; its rate-adjust member must be what the sink rule resolves for
    the ring it was certified on, or the two owners drift."""

    assert RING_CAMILLA_GEOMETRY["enable_rate_adjust"] is resolve_enable_rate_adjust(
        RING_ACTIVE_PLAYBACK_DEVICE
    )


def test_the_unpaired_endpoints_are_exactly_the_ones_the_map_does_not_pair():
    """`transport_coherence_report` routes a playback device on the ABSENCE of a
    pairing, so the two sets must partition the post-DSP endpoints — a member in
    both would let one device be simultaneously paired and unpaired."""

    paired = set(_OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE)

    assert paired <= POST_DSP_PLAYBACK_DEVICES
    assert paired & UNPAIRED_POST_DSP_PLAYBACK_DEVICES == set()
    assert paired | UNPAIRED_POST_DSP_PLAYBACK_DEVICES == POST_DSP_PLAYBACK_DEVICES
