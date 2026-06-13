from __future__ import annotations

from jasper.camilla_config_contract import PeqFilter
from jasper.correction.camilla_yaml import emit_correction_config
from jasper.correction.peq import PEQ
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


def test_extract_room_peqs_from_legacy_correction_config():
    old_yaml = emit_correction_config([
        PEQ(freq=80.0, q=4.0, gain=-3.0),
        PEQ(freq=140.0, q=2.0, gain=-1.5),
    ])

    assert extract_room_peqs_from_config_text(old_yaml) == [
        PeqFilter(freq=80.0, q=4.0, gain=-3.0),
        PeqFilter(freq=140.0, q=2.0, gain=-1.5),
    ]


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
    # Per-seat ROOM segments differ…
    assert left.startswith("    names: [room_peq_1, sound_preamp,")
    assert right.startswith(
        "    names: [room_peq_r1, room_peq_r2, sound_preamp,"
    )
    # …the preference tail (preamp + curve/EQ + flat) is IDENTICAL…
    assert left.split("sound_preamp", 1)[1] == right.split("sound_preamp", 1)[1]
    # …and shared filters are DEFINED once (referenced by both chains).
    assert yaml.count("sound_preamp:") == 1
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


def test_extract_room_peqs_skips_right_channel_filters_and_warns(caplog):
    """The extractor serves the SOLO re-emit path: it must return the
    left/solo chain only and stay blind to leader-bake right-channel
    filters (the leader apply path composes from stored profiles —
    HANDOFF-multiroom.md §2, Increment 5). Blindness must be LOUD, not
    silent: re-emitting from this extraction alone would drop the
    follower's correction, so seeing *_r* filters logs a WARNING (the
    no-silent-failure rule)."""
    import logging

    yaml = emit_correction_config(
        [PEQ(freq=80.0, q=4.0, gain=-3.0)],
        peqs_right=[PEQ(freq=120.0, q=3.0, gain=-2.0)],
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

    yaml = emit_correction_config([PEQ(freq=80.0, q=4.0, gain=-3.0)])
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
