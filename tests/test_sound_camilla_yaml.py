# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.camilla_config_contract import DEFAULT_LEAN_CAPTURE_FIFO, PeqFilter
from jasper.sound.camilla_yaml import (
    emit_sound_config,
    extract_room_peqs_from_config_text,
)
from jasper.sound.profile import SimpleEq, SoundProfile


def test_sound_config_preserves_room_peqs_before_preference_eq():
    profile = SoundProfile(
        enabled=True,
        curve_id="harman",
        simple_eq=SimpleEq(bass_db=2.0, mid_db=-1.0, treble_db=1.5),
    )
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        profile_id="abc123",
    )

    assert "Source: jasper.sound.camilla_yaml.emit_sound_config" in yaml
    assert "volume_limit: 0.0" in yaml
    assert 'device: "outputd_content_playback"' in yaml
    assert "room_peq_1:" in yaml
    assert "sound_preamp" not in yaml  # default trim 0: boosts boost
    assert "sound_curve_harman_bass:" in yaml
    assert "type: Lowshelf" in yaml
    assert "type: Highshelf" in yaml
    assert "sound_simple_mid:" in yaml
    assert "names: [room_peq_1, sound_curve_harman_bass" in yaml
    assert yaml.count("channels: [0]") == 1
    assert yaml.count("channels: [1]") == 1


def test_emit_sound_config_writes_group_readable_file(tmp_path):
    # Regression (false "bond degraded" incident, 2026-06-21): the bonded-leader
    # config grouping_leader.yml is written by emit_sound_config and read off-disk
    # by the non-root jasper-control /state leader-pipe health check
    # (active_leader_pipe_path). The old hand-rolled writer left it at the
    # NamedTemporaryFile default 0600, so after the WS1 non-root drop jasper-control
    # (group jasper) could not read it -> /state falsely reported the bond
    # "degraded — stream is silent" while audio was flowing. The writer now
    # delegates to atomic_write_text(mode=0o640), matching the active-speaker
    # emitter. Pin the group-readable mode so a non-root reader keeps working.
    import os
    import stat

    profile = SoundProfile(
        enabled=True,
        curve_id="harman",
        simple_eq=SimpleEq(bass_db=2.0, mid_db=-1.0, treble_db=1.5),
    )
    out = tmp_path / "grouping_leader.yml"
    emit_sound_config(profile, out_path=out)

    mode = stat.S_IMODE(os.stat(out).st_mode)
    assert mode == 0o640, f"expected 0o640 (group-readable), got {oct(mode)}"
    assert mode & stat.S_IRGRP, "config must be group-readable for non-root jasper-control"


def test_disabled_sound_config_bypasses_preference_eq_but_keeps_room_peqs():
    profile = SoundProfile(enabled=False, curve_id="bk", simple_eq=SimpleEq(bass_db=6.0))
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=120.0, q=3.0, gain=-2.0)],
    )

    assert "room_peq_1:" in yaml
    assert "sound_curve_bk_bass" not in yaml
    assert "sound_simple_bass" not in yaml
    assert "sound_preamp" not in yaml
    assert "names: [room_peq_1, flat]" in yaml


def test_output_trim_emits_single_preamp_before_filters():
    profile = SoundProfile(
        enabled=True, curve_id="harman", simple_eq=SimpleEq(bass_db=6.0)
    )
    yaml = emit_sound_config(profile, output_trim_db=4.0)

    assert "sound_preamp:" in yaml
    assert "gain: -4.0000" in yaml
    assert "names: [sound_preamp, sound_curve_harman_bass" in yaml


def test_default_has_no_preamp_so_boosts_boost():
    profile = SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=6.0))
    yaml = emit_sound_config(profile)

    assert "sound_preamp" not in yaml
    assert "sound_simple_bass:" in yaml


def test_output_trim_is_ignored_when_profile_has_no_filters():
    # A flat profile can't clip from EQ, so a configured trim is a no-op.
    yaml = emit_sound_config(
        SoundProfile(enabled=True, curve_id="flat"), output_trim_db=6.0
    )

    assert "sound_preamp" not in yaml


def _legacy_correction_config_text(
    peqs: list[PeqFilter],
    *,
    peqs_right: list[PeqFilter] | None = None,
) -> str:
    """Minimal historical correction-emitter shape for parser compatibility.

    The production legacy emitter is gone; this fixture keeps the extractor's
    old ``peq_*`` / ``peq_r*`` compatibility contract pinned without keeping a
    dead generator import alive.
    """
    lines = ["---", "filters:"]
    for prefix, filters in (("peq_", peqs), ("peq_r", peqs_right or [])):
        for i, peq in enumerate(filters, start=1):
            lines.extend([
                f"  {prefix}{i}:",
                "    type: Biquad",
                "    parameters:",
                "      type: Peaking",
                f"      freq: {peq.freq:.4f}",
                f"      q: {peq.q:.4f}",
                f"      gain: {peq.gain:.4f}",
            ])
    lines.append("mixers:")
    return "\n".join(lines)


def test_extract_room_peqs_from_legacy_correction_config():
    old_yaml = _legacy_correction_config_text([
        PeqFilter(freq=80.0, q=4.0, gain=-3.0),
        PeqFilter(freq=140.0, q=2.0, gain=-1.5),
    ])

    assert extract_room_peqs_from_config_text(old_yaml) == [
        PeqFilter(freq=80.0, q=4.0, gain=-3.0),
        PeqFilter(freq=140.0, q=2.0, gain=-1.5),
    ]


def test_extract_room_peqs_from_legacy_right_channel_config_warns(caplog):
    """Historical standalone correction configs used peq_r* for the
    leader-bake right channel. Keep that compatibility loud: extraction
    returns the solo/left chain only and warns that right-channel filters
    were intentionally ignored."""
    import logging

    old_yaml = _legacy_correction_config_text(
        [PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        peqs_right=[PeqFilter(freq=120.0, q=3.0, gain=-2.0)],
    )

    with caplog.at_level(logging.WARNING):
        extracted = extract_room_peqs_from_config_text(old_yaml)

    assert extracted == [PeqFilter(freq=80.0, q=4.0, gain=-3.0)]
    assert any(
        "event=sound.extract_room_peqs" in record.message
        and "result=right_channel_ignored" in record.message
        for record in caplog.records
    )


def test_extract_room_peqs_ignores_sound_peaking_filters():
    profile = SoundProfile.from_mapping({
        "parametric_bands": [
            {"type": "peaking", "freq_hz": 2000, "gain_db": -2, "q": 2},
        ],
    })
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=90.0, q=4.0, gain=-3.5)],
    )

    assert "sound_advanced_1:" in yaml
    assert extract_room_peqs_from_config_text(yaml) == [
        PeqFilter(freq=90.0, q=4.0, gain=-3.5),
    ]


def test_emit_sound_config_rejects_positive_volume_limit():
    """Loud-output safety (audit C6): the emitter refuses to build a
    config whose master fader could boost above full scale. Mirrors the
    guard in jasper.active_speaker.camilla_yaml."""
    import pytest

    with pytest.raises(ValueError, match="must not exceed 0 dB"):
        emit_sound_config(
            SoundProfile(enabled=False), volume_limit_db=1.0,
        )


def test_emit_sound_config_rejects_non_finite_volume_limit():
    import math

    import pytest

    with pytest.raises(ValueError, match="must be finite"):
        emit_sound_config(
            SoundProfile(enabled=False), volume_limit_db=math.nan,
        )


# ---------------------------------------------------------------------------
# room_peqs_right — the multi-room leader-bake axis (HANDOFF-multiroom.md §2).
# Only the ROOM segment is per-channel (per-seat correction); preference EQ
# is shared household taste and stays identical on both chains.
# ---------------------------------------------------------------------------


def _pipeline_chains(yaml: str) -> list[str]:
    """The two per-channel `names: [...]` pipeline lines, in channel order."""
    return [
        line for line in yaml.splitlines() if line.startswith("    names: [")
    ]


def test_solo_sound_pipeline_unchanged_without_room_peqs_right():
    """The solo-impact contract: no room_peqs_right ⇒ both channels carry
    the SAME chain and no right-channel room filter exists anywhere."""
    profile = SoundProfile(
        enabled=True,
        curve_id="harman",
        simple_eq=SimpleEq(bass_db=2.0),
    )
    yaml = emit_sound_config(
        profile, room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)]
    )
    chains = _pipeline_chains(yaml)
    assert len(chains) == 2
    assert chains[0] == chains[1]
    assert "room_peq_r" not in yaml


def test_room_peqs_right_bakes_per_seat_room_segment_with_shared_tail():
    profile = SoundProfile(
        enabled=True,
        curve_id="harman",
        simple_eq=SimpleEq(bass_db=2.0),
    )
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        room_peqs_right=[
            PeqFilter(freq=120.0, q=3.0, gain=-2.0),
            PeqFilter(freq=4000.0, q=2.0, gain=1.0),
        ],
        output_trim_db=4.0,
    )
    left, right = _pipeline_chains(yaml)
    # Per-seat ROOM segments differ…  The right seat carries a +1 dB boost,
    # so a shared -1 dB room_headroom rides the tail (audio-safety; see
    # test_room_boost_emits_headroom_preamp).
    assert left.startswith("    names: [room_peq_1, room_headroom, sound_preamp,")
    assert right.startswith(
        "    names: [room_peq_r1, room_peq_r2, room_headroom, sound_preamp,"
    )
    # …the preference tail (preamp + curve/EQ + flat) is IDENTICAL…
    assert left.split("sound_preamp", 1)[1] == right.split("sound_preamp", 1)[1]
    # …and shared filters are DEFINED once (referenced by both chains).
    assert yaml.count("sound_preamp:") == 1
    assert yaml.count("room_headroom:") == 1
    assert "room_peq_r1:" in yaml and "room_peq_r2:" in yaml


def test_room_peqs_right_empty_bakes_flat_right_room_segment():
    """[] is distinct from None: an uncalibrated follower's room segment
    ships FLAT, never the leader's wrong-room curve (HANDOFF-multiroom.md
    §2, Increment 6)."""
    profile = SoundProfile(
        enabled=False, curve_id="bk", simple_eq=SimpleEq(bass_db=6.0)
    )
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=120.0, q=3.0, gain=-2.0)],
        room_peqs_right=[],
    )
    assert "    names: [room_peq_1, flat]" in yaml
    assert "    names: [flat]" in yaml
    assert "room_peq_r" not in yaml


# --- Audio-safety: room-correction boost headroom ---------------------------
# Room-correction BOOSTS (the assertive strategy, cuts_only=False) raise
# specific bands with no compensating attenuation, so a hot note in a boosted
# band can clip above full scale. The emitter pulls the whole signal down by
# the worst-case additive room boost. Cuts-only correction has zero boost, so
# the trim emits nothing and the config stays byte-identical.
def test_cuts_only_room_correction_emits_no_headroom():
    profile = SoundProfile(enabled=False, curve_id="bk", simple_eq=SimpleEq())
    yaml = emit_sound_config(
        profile,
        room_peqs=[
            PeqFilter(freq=60.0, q=3.0, gain=-6.0),
            PeqFilter(freq=120.0, q=4.0, gain=-3.0),
        ],
    )
    assert "room_headroom" not in yaml
    assert "    names: [room_peq_1, room_peq_2, flat]" in yaml


def test_room_boost_emits_headroom_preamp_so_net_gain_stays_at_unity():
    profile = SoundProfile(enabled=False, curve_id="bk", simple_eq=SimpleEq())
    yaml = emit_sound_config(
        profile,
        room_peqs=[
            PeqFilter(freq=45.0, q=5.0, gain=2.0),   # boost
            PeqFilter(freq=80.0, q=6.0, gain=-4.0),  # cut (ignored for headroom)
            PeqFilter(freq=120.0, q=4.0, gain=1.0),  # boost
        ],
    )
    # Worst-case additive boost is +3 dB (2 + 1); the headroom preamp is -3 dB.
    assert "room_headroom:" in yaml
    assert "gain: -3.0000" in yaml
    # …and it rides the chain right after the room PEQs.
    assert (
        "    names: [room_peq_1, room_peq_2, room_peq_3, room_headroom, flat]"
        in yaml
    )


def test_room_headroom_trims_by_the_louder_channel_for_leader_bake():
    """Asymmetric per-seat correction: the shared headroom must protect the
    louder channel so neither can clip."""
    profile = SoundProfile(enabled=False, curve_id="bk", simple_eq=SimpleEq())
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=50.0, q=4.0, gain=1.0)],        # +1 left
        room_peqs_right=[PeqFilter(freq=90.0, q=4.0, gain=3.0)],  # +3 right (louder)
    )
    # Trim by the louder (+3 dB) channel, defined once and shared by both chains.
    assert yaml.count("room_headroom:") == 1
    assert "gain: -3.0000" in yaml
    left, right = _pipeline_chains(yaml)
    assert "room_headroom" in left and "room_headroom" in right


def test_extract_room_peqs_skips_right_channel_filters_and_warns(caplog):
    """The extractor serves the SOLO re-emit path: it must return the
    left/solo chain only and stay blind to leader-bake right-channel
    filters (the leader apply path composes from stored profiles —
    HANDOFF-multiroom.md §2, Increment 5). Blindness must be LOUD, not
    silent: re-emitting from this extraction alone would drop the
    follower's correction, so seeing *_r* filters logs a WARNING (the
    no-silent-failure rule)."""
    import logging

    yaml = emit_sound_config(
        SoundProfile(enabled=False),
        room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        room_peqs_right=[PeqFilter(freq=120.0, q=3.0, gain=-2.0)],
    )
    with caplog.at_level(logging.WARNING):
        extracted = extract_room_peqs_from_config_text(yaml)
    assert extracted == [PeqFilter(freq=80.0, q=4.0, gain=-3.0)]
    # Stable, parseable event= line (house observability style), not prose.
    assert any(
        "event=sound.extract_room_peqs" in record.message
        and "result=right_channel_ignored" in record.message
        for record in caplog.records
    )


def test_extract_room_peqs_stays_quiet_on_solo_configs(caplog):
    """No right-channel filters ⇒ no warning — the solo path must not
    acquire log noise (solo-impact contract)."""
    import logging

    yaml = emit_sound_config(
        SoundProfile(enabled=False),
        room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
    )
    with caplog.at_level(logging.WARNING):
        extract_room_peqs_from_config_text(yaml)
    assert not any(
        "event=sound.extract_room_peqs" in record.message
        for record in caplog.records
    )


def test_room_peqs_right_and_channel_split_are_mutually_exclusive():
    """The two axes belong to different topology models (leader-bake
    pre-stream correction vs. member-side channel-selection weave);
    combining them would channel-select AHEAD of per-channel filters and
    'correct' a duplicated program channel with the other seat's chain.
    The emitter fails LOUD at the API boundary — even for a passthrough
    split (both-present indicates a wiring bug)."""
    import pytest

    from jasper.multiroom.channel_split import build_channel_split

    for channel in ("left", "stereo"):
        with pytest.raises(ValueError, match="mutually exclusive"):
            emit_sound_config(
                SoundProfile(enabled=False),
                room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
                room_peqs_right=[],
                channel_split=build_channel_split(channel),
            )


def test_playback_pipe_path_emits_file_sink_for_the_bonded_leader():
    """The bonded-leader playback axis (HANDOFF-multiroom.md §2,
    Increment 5): playback becomes a File sink writing the shared stereo
    program to snapserver's FIFO; capture and the rest of the config are
    untouched. Pairs with room_peqs_right (the leader-bake combo)."""
    yaml = emit_sound_config(
        SoundProfile(enabled=True, curve_id="harman", simple_eq=SimpleEq()),
        room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        room_peqs_right=[PeqFilter(freq=120.0, q=2.0, gain=-2.0)],
        enable_rate_adjust=False,
        playback_pipe_path="/run/jasper-snapserver/snapfifo",
    )
    assert "type: File" in yaml
    assert 'filename: "/run/jasper-snapserver/snapfifo"' in yaml
    assert "enable_rate_adjust: false" in yaml
    # The ALSA loopback sink is fully replaced…
    assert 'device: "outputd_content_playback"' not in yaml
    # …but the capture side stays the normal ALSA lane.
    assert 'device: "plug:jasper_capture"' in yaml
    # Loud-output safety survives the sink swap.
    assert "volume_limit: 0.0" in yaml


def test_playback_pipe_path_none_is_byte_identical_solo():
    """The solo-impact contract for the new axis: omitting it and passing
    the explicit default produce the SAME BYTES as each other (and the
    emitted config still carries the ALSA loopback sink)."""
    profile = SoundProfile(
        enabled=True, curve_id="harman", simple_eq=SimpleEq(bass_db=2.0)
    )
    kwargs = dict(
        room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        profile_id="solo-bytes",
    )
    without_axis = emit_sound_config(profile, **kwargs)
    with_default = emit_sound_config(profile, playback_pipe_path=None, **kwargs)
    assert without_axis == with_default
    assert 'device: "outputd_content_playback"' in without_axis
    assert "type: File" not in without_axis


def test_playback_pipe_path_requires_rate_adjust_off():
    """A File sink has no output clock — rate_adjust has nothing to steer,
    and the synced chain's ONE rate-tracker is snapclient (§2 invariant 5).
    The emitter fails loud instead of silently emitting a config whose
    rate_adjust flag is a lie."""
    import pytest

    with pytest.raises(ValueError, match="enable_rate_adjust=False"):
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=True,
            playback_pipe_path="/run/jasper-snapserver/snapfifo",
        )


def test_playback_pipe_path_and_channel_split_are_mutually_exclusive():
    """The pipe carries the SHARED stereo program; a member's
    channel-selection weave on it would strip the other speaker's channel
    out of the stream. Members drop channels downstream (outputd
    ChannelPick), never inside the stream."""
    import pytest

    from jasper.multiroom.channel_split import build_channel_split

    with pytest.raises(ValueError, match="mutually exclusive"):
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            channel_split=build_channel_split("left"),
            playback_pipe_path="/run/jasper-snapserver/snapfifo",
        )


def test_channel_delays_emit_delay_filters_only_on_distinct_room_chains():
    """Time-of-arrival correction is a room/pair axis: it uses CamillaDSP
    Delay filters, no gain, and requires explicit L/R chains."""
    yaml = emit_sound_config(
        SoundProfile(enabled=False),
        room_peqs=[],
        room_peqs_right=[],
        channel_delays_ms=(1.25, 0.0),
    )

    assert "room_delay_l:" in yaml
    assert "type: Delay" in yaml
    assert "delay: 1.2500" in yaml
    assert "unit: ms" in yaml
    assert "gain: 1.2500" not in yaml
    assert "volume_limit: 0.0" in yaml
    assert "    names: [room_delay_l, flat]" in yaml
    assert "    names: [flat]" in yaml


def test_channel_delays_default_and_zero_are_solo_byte_identical():
    profile = SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=2.0))
    base = emit_sound_config(profile, room_peqs=[PeqFilter(freq=80, q=4, gain=-3)])
    explicit_zero = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=80, q=4, gain=-3)],
        channel_delays_ms=(0.0, 0.0),
    )

    assert explicit_zero == base
    assert "room_delay_" not in base


def test_nonzero_channel_delay_requires_leader_bake_right_chain():
    import pytest

    with pytest.raises(ValueError, match="requires room_peqs_right"):
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs=[],
            channel_delays_ms=(0.0, 1.0),
        )


def test_channel_delays_reject_negative_or_non_finite_values():
    import math
    import pytest

    with pytest.raises(ValueError, match="positive-only"):
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs_right=[],
            channel_delays_ms=(-0.1, 0.0),
        )
    with pytest.raises(ValueError, match="finite"):
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs_right=[],
            channel_delays_ms=(math.nan, 0.0),
        )


# ---- Stage 4a: File-CAPTURE lean lane -------------------------------------


def test_file_capture_emits_lean_lane_shape():
    """The named-pipe CAPTURE lean lane — capture type RawFile (a named pipe),
    playback type Alsa (the REAL DAC), enable_rate_adjust true, an async
    resampler so the DAC clock can discipline the clockless capture, and the
    0 dB ceiling preserved. The mirror of the File-SINK *playback* path.

    Capture is `RawFile`, NOT `File`: CamillaDSP v4 has no `File` capture
    variant, so the old `type: File` here produced a config the DSP rejected
    with "unknown variant `File`" (caught on jts5 / CamillaDSP 4.1.3,
    2026-06-27). `File` remains the correct *playback*-sink type."""
    yaml = emit_sound_config(
        SoundProfile(enabled=False),
        capture_pipe_path=DEFAULT_LEAN_CAPTURE_FIFO,
        playback_device="hw:DAC8x,0",
        enable_rate_adjust=True,
        resampler_type="AsyncSinc",
        chunksize=2048,
        target_level=4096,
    )
    assert "type: RawFile" in yaml
    # The invalid capture variant must never reappear (the regression).
    assert "type: File" not in yaml
    assert f'filename: "{DEFAULT_LEAN_CAPTURE_FIFO}"' in yaml
    assert "type: Alsa" in yaml
    assert 'device: "hw:DAC8x,0"' in yaml
    assert "enable_rate_adjust: true" in yaml
    assert "resampler:" in yaml
    assert "type: AsyncSinc" in yaml
    assert "profile: Balanced" in yaml
    assert "volume_limit: 0.0" in yaml
    assert "chunksize: 2048" in yaml
    assert "target_level: 4096" in yaml


def test_file_capture_resampler_line_absent_on_solo_default():
    """Byte-contract: the resampler line appears ONLY when requested, so
    every existing (ALSA-capture, no-resampler) caller is byte-identical."""
    yaml = emit_sound_config(SoundProfile(enabled=False))
    assert "resampler:" not in yaml
    assert "type: Alsa" in yaml
    assert "type: File" not in yaml


def test_file_capture_rejects_rate_adjust_off():
    """Fail LOUD: a File capture with rate_adjust off would free-run against
    the DAC (no clock on capture, no compensator) — the Stage-1 hazard."""
    import pytest

    with pytest.raises(ValueError, match="requires enable_rate_adjust=True"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path=DEFAULT_LEAN_CAPTURE_FIFO,
            enable_rate_adjust=False,
            resampler_type="AsyncSinc",
        )


def test_file_capture_rejects_non_async_resampler():
    """Fail LOUD: enable_rate_adjust on a clockless File capture has nothing
    to steer without an async resampler — reject a fixed/sync resampler and
    reject the no-resampler case."""
    import pytest

    with pytest.raises(ValueError, match="requires an async resampler"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path=DEFAULT_LEAN_CAPTURE_FIFO,
            enable_rate_adjust=True,
            resampler_type="Synchronous",
        )
    with pytest.raises(ValueError, match="requires an async resampler"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path=DEFAULT_LEAN_CAPTURE_FIFO,
            enable_rate_adjust=True,
            resampler_type=None,
        )


def test_file_capture_rejects_combined_pipe_in_and_pipe_out():
    """A File-in/File-out config has no clock anywhere — refuse it. The
    capture guard is placed above the sink guard so this raises its own
    message first."""
    import pytest

    with pytest.raises(ValueError, match="only be combined with transport_paced_pipe"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path=DEFAULT_LEAN_CAPTURE_FIFO,
            playback_pipe_path="/run/jasper-snapserver/snapfifo",
            enable_rate_adjust=True,
            resampler_type="AsyncSinc",
        )


def test_transport_paced_pipe_allows_only_dual_pipe_transport_shape():
    """The end-to-end local pipe path is a distinct topology from the lean
    File-capture path: both pipe ends are present, rate_adjust/resampler are
    off, and outputd's blocking DAC write owns pacing."""
    yaml = emit_sound_config(
        SoundProfile(enabled=False),
        capture_pipe_path="/run/jasper-fanin/camilla.pipe",
        playback_pipe_path="/run/jasper-outputd/content.pipe",
        enable_rate_adjust=False,
        playback_format="S32_LE",
        resampler_type=None,
        transport_paced_pipe=True,
        chunksize=256,
        target_level=512,
    )

    assert "enable_rate_adjust: false" in yaml
    assert "resampler:" not in yaml
    assert "type: RawFile" in yaml
    assert 'filename: "/run/jasper-fanin/camilla.pipe"' in yaml
    assert "format: S32_LE" in yaml
    assert "type: File" in yaml
    assert 'filename: "/run/jasper-outputd/content.pipe"' in yaml
    assert "format: S32_LE" in yaml
    assert "chunksize: 256" in yaml
    assert "target_level: 512" in yaml


def test_transport_paced_pipe_does_not_inherit_loopback_target_override():
    """The dual-pipe topology is DAC-paced by outputd, not Camilla rate-adjust.

    CamillaDSP still validates target_level even when rate-adjust is off; a
    valid loopback override such as 2048 must not make the transport config
    unloadable.
    """
    yaml = emit_sound_config(
        SoundProfile(enabled=False),
        capture_pipe_path="/run/jasper-fanin/camilla.pipe",
        playback_pipe_path="/run/jasper-outputd/content.pipe",
        enable_rate_adjust=False,
        playback_format="S32_LE",
        resampler_type=None,
        transport_paced_pipe=True,
        chunksize=256,
        target_level=2048,
    )

    assert "chunksize: 256" in yaml
    assert "target_level: 512" in yaml
    assert "target_level: 2048" not in yaml


def test_transport_paced_pipe_rejects_missing_or_miswired_geometry():
    import pytest

    with pytest.raises(ValueError, match="requires both capture_pipe_path"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path="/run/jasper-fanin/camilla.pipe",
            enable_rate_adjust=False,
            transport_paced_pipe=True,
        )
    with pytest.raises(ValueError, match="requires enable_rate_adjust=False"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path="/run/jasper-fanin/camilla.pipe",
            playback_pipe_path="/run/jasper-outputd/content.pipe",
            enable_rate_adjust=True,
            transport_paced_pipe=True,
        )
    with pytest.raises(ValueError, match="must not emit a CamillaDSP resampler"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path="/run/jasper-fanin/camilla.pipe",
            playback_pipe_path="/run/jasper-outputd/content.pipe",
            enable_rate_adjust=False,
            resampler_type="AsyncSinc",
            transport_paced_pipe=True,
        )
    with pytest.raises(ValueError, match="requires 48000 Hz"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path="/run/jasper-fanin/camilla.pipe",
            playback_pipe_path="/run/jasper-outputd/content.pipe",
            enable_rate_adjust=False,
            sample_rate=44100,
            transport_paced_pipe=True,
        )
    with pytest.raises(ValueError, match="capture format must be S32_LE"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path="/run/jasper-fanin/camilla.pipe",
            playback_pipe_path="/run/jasper-outputd/content.pipe",
            capture_format="S16LE",
            enable_rate_adjust=False,
            transport_paced_pipe=True,
        )
    with pytest.raises(ValueError, match="playback format must be S32_LE"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path="/run/jasper-fanin/camilla.pipe",
            playback_pipe_path="/run/jasper-outputd/content.pipe",
            playback_format="S16_LE",
            enable_rate_adjust=False,
            transport_paced_pipe=True,
        )


def test_file_capture_keeps_zero_db_ceiling_guard():
    """The 0 dB ceiling guard fires on the File-capture path too (it runs
    before any branch in emit_sound_config)."""
    import pytest

    with pytest.raises(ValueError, match="must not exceed 0 dB"):
        emit_sound_config(
            SoundProfile(enabled=False),
            capture_pipe_path=DEFAULT_LEAN_CAPTURE_FIFO,
            enable_rate_adjust=True,
            resampler_type="AsyncSinc",
            volume_limit_db=1.0,
        )
