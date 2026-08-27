# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.camilla_config_contract import PeqFilter
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
    assert 'device: "jts_ring_playback"' in yaml
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
    assert 'device: "jts_ring_playback"' not in yaml
    # …but the capture side stays the emitter's default, Ring A.
    assert 'device: "jts_ring_capture"' in yaml
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
    assert 'device: "jts_ring_playback"' in without_axis
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


def test_playback_pipe_path_format_stays_pinned_regardless_of_playback_format():
    """D4 (wide-output-path program): the pipe sink's format is
    DEFAULT_PIPE_SINK_FORMAT, decoupled from playback_format. No monkeypatch
    needed — the guard is a pure ``is not None`` presence check now (SF1 fix:
    the old ``!= DEFAULT_PLAYBACK_FORMAT`` comparison mixed a live global with
    a def-time-bound default and could diverge), so passing a wide value
    directly is enough to prove it would be refused rather than honored (see
    the raise test below) — this test proves the OTHER half: the pipe sink
    never reads playback_format for its emitted format at all."""
    yaml = emit_sound_config(
        SoundProfile(enabled=False),
        enable_rate_adjust=False,
        playback_pipe_path="/run/jasper-snapserver/snapfifo",
    )
    # Scope the assertion to the playback BLOCK — the capture block legitimately
    # emits format: S32_LE always (DEFAULT_CAPTURE_FORMAT, unrelated to this
    # axis), so a whole-document substring check would false-pass here.
    playback_block = yaml.split("playback:", 1)[1]
    assert "type: File" in playback_block
    assert "format: S16_LE" in playback_block


def test_playback_pipe_path_with_explicit_playback_format_raises_regardless_of_value():
    """The ValueError case: playback_format is not applicable to a pipe sink
    at all, so passing it EXPLICITLY — even a value that happens to equal
    today's DEFAULT_PLAYBACK_FORMAT — is refused. Genuinely a presence check
    (``is not None``), not a "does it match the current default" check."""
    import pytest

    with pytest.raises(ValueError, match="different axes"):
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            playback_format="S16_LE",  # == today's DEFAULT_PLAYBACK_FORMAT —
            # still refused: the guard is presence-based, not value-based.
            playback_pipe_path="/run/jasper-snapserver/snapfifo",
        )


def test_playback_pipe_path_omitted_never_raises_even_after_the_default_rebinds(
    monkeypatch,
):
    """SF1 regression (PR-1 gate review): the bug this reproduces — a caller
    that omits playback_format entirely (the ONLY calling convention every
    production caller uses) must NEVER raise, even if DEFAULT_PLAYBACK_FORMAT
    is rebound after this module was imported (the exact scenario a
    ``!= DEFAULT_PLAYBACK_FORMAT`` body-level comparison gets wrong, because
    it compares a LIVE global against playback_format's def-time-bound
    default — two different bindings the moment they diverge). The sentinel
    (``str | None = None``) guard sidesteps this: omitted means None,
    unconditionally, regardless of what the module global currently reads."""
    import jasper.sound.camilla_yaml as camilla_yaml

    monkeypatch.setattr(camilla_yaml, "DEFAULT_PLAYBACK_FORMAT", "S32_LE")
    yaml = emit_sound_config(
        SoundProfile(enabled=False),
        enable_rate_adjust=False,
        playback_pipe_path="/run/jasper-snapserver/snapfifo",
        # playback_format deliberately omitted.
    )
    playback_block = yaml.split("playback:", 1)[1]
    assert "format: S16_LE" in playback_block  # DEFAULT_PIPE_SINK_FORMAT, untouched


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


def test_solo_default_uses_alsa_capture_without_resampler():
    yaml = emit_sound_config(SoundProfile(enabled=False))
    assert "resampler:" not in yaml
    assert "type: Alsa" in yaml
    assert "type: File" not in yaml


# --- ring flat config emitter (shm_ring statefile seed, P2) -------------------


def test_the_flat_startup_graph_names_both_ring_devices_s32le(monkeypatch):
    # RENAMED from ..._s16le: jasper.fanin_coupling.resolve_ring_wire()'s
    # default flipped WIDE in PR #2601, and the cutover emitter has no way
    # to take an explicit wire — it always resolves through that function
    # (unlike jasper.ring_assets.render_ring_conf_wire, which takes a RingWire
    # parameter directly). The resolved wire is PINNED here via monkeypatch
    # rather than left to the ambient default: resolve_ring_wire() reads
    # /etc/jasper/jasper.env and /var/lib/jasper/fanin.env file-fresh, and a
    # real one of either on the host running the suite (a Pi, or a dev laptop
    # that ever ran the installer) would reach this test — the same hazard
    # tests/test_fanin_coupling_reconcile.py's setup fixture documents.
    import jasper.fanin_coupling as fc
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format=fc.RING_WIRE_FORMAT_WIDE,
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )
    yaml = emit_flat_outputd_cutover_config()
    # Capture = Ring A (jts_ring_capture), playback = Ring B (jts_ring_playback),
    # both S32_LE ALSA devices — the end-to-end ring topology.
    capture = yaml.split("  capture:\n", 1)[1].split("\n  playback:\n", 1)[0]
    playback = yaml.split("\n  playback:\n", 1)[1].split("\nfilters:\n", 1)[0]
    assert 'device: "jts_ring_capture"' in capture
    assert "format: S32_LE" in capture
    assert "type: Alsa" in capture
    assert 'device: "jts_ring_playback"' in playback
    assert "format: S32_LE" in playback
    assert "type: Alsa" in playback
    # Ring graph geometry is the hardware-validated low-latency shape.
    assert "chunksize: 128" in yaml
    assert "queuelimit: 1" in yaml
    assert "target_level: 128" in yaml
    assert "enable_rate_adjust: false" in yaml
    # It is the disabled (flat) profile — no preference EQ filters.
    assert "volume_limit: 0.0" in yaml


def _mono_topology(output_index: int):
    from jasper.output_topology import OutputTopology

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "mono",
        "name": "Mono passive output",
        "status": "verified",
        "hardware": {
            "device_id": "innomaker_hifi_amp_pro",
            "device_label": "InnoMaker HiFi AMP Pro",
            "card_id": "sndrpimerusamp",
            "physical_output_count": 2,
        },
        "speaker_groups": [{
            "id": "main",
            "label": "Main speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{
                "role": "full_range",
                "physical_output_index": output_index,
                "identity_verified": True,
            }],
        }],
        "routing": {"mono_group_id": "main"},
    })


def _stereo_topology():
    from jasper.output_topology import OutputTopology

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "stereo",
        "name": "Stereo passive output",
        "status": "verified",
        "hardware": {
            "device_id": "innomaker_hifi_amp_pro",
            "device_label": "InnoMaker HiFi AMP Pro",
            "card_id": "sndrpimerusamp",
            "physical_output_count": 2,
        },
        "speaker_groups": [
            {
                "id": "left", "label": "Left", "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right", "label": "Right", "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
        ],
        "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
    })


def _pipeline_names(yaml_text: str, channel: int) -> str:
    """The ``names: [...]`` line of the Filter step targeting ``channel``."""
    lines = yaml_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"channels: [{channel}]":
            return lines[index + 1].strip()
    raise AssertionError(f"no Filter step for channel {channel} in:\n{yaml_text}")


def test_mono_on_output_0_renders_channel_1_hard_muted():
    from jasper.active_speaker.camilla_yaml import (
        STARTUP_MUTE_GAIN_DB,
        output_commission_mute_name,
    )
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    yaml = emit_flat_outputd_cutover_config(topology=_mono_topology(0))

    mute = output_commission_mute_name(1)
    # The ALSA device stays at the hardware width — outputd negotiates 2ch on
    # the InnoMaker; the silence lives in the graph, not the device.
    assert "channels: 2" in yaml
    assert f"  {mute}:\n    type: Gain" in yaml
    assert f"gain: {STARTUP_MUTE_GAIN_DB:.4f}" in yaml
    assert "mute: true" in yaml
    # Terminal: the mute is the LAST name in its channel's chain, and the
    # claimed channel keeps its ordinary chain untouched.
    assert _pipeline_names(yaml, 1) == f"names: [flat, {mute}]"
    assert _pipeline_names(yaml, 0) == "names: [flat]"
    assert output_commission_mute_name(0) not in yaml


def test_mono_on_output_1_renders_channel_0_hard_muted():
    from jasper.active_speaker.camilla_yaml import output_commission_mute_name
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    yaml = emit_flat_outputd_cutover_config(topology=_mono_topology(1))

    mute = output_commission_mute_name(0)
    assert _pipeline_names(yaml, 0) == f"names: [flat, {mute}]"
    assert _pipeline_names(yaml, 1) == "names: [flat]"
    assert output_commission_mute_name(1) not in yaml


def test_stereo_and_unconfigured_topologies_render_byte_identical_flat_config():
    from jasper.output_topology import new_topology_draft
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    # Rendering is intentionally topology-shape-only: a saved stereo topology
    # and a fresh empty draft produce identical flat bytes. The runtime contract
    # separately refuses to SELECT this artifact for an unconfigured speaker.
    baseline = emit_flat_outputd_cutover_config(topology=new_topology_draft())

    assert emit_flat_outputd_cutover_config(topology=_stereo_topology()) == baseline
    assert "commission_mute" not in baseline
    assert _pipeline_names(baseline, 0) == "names: [flat]"
    assert _pipeline_names(baseline, 1) == "names: [flat]"


def _unconfigured_draft():
    from jasper.output_topology import new_topology_draft

    return new_topology_draft()


def _composite_mono_topology():
    """A mono cabinet on a dual-Apple composite sink.

    outputd fans the program across the child DACs, so Camilla channel *i* is
    not physical output *i*. This is the shape a fold would actively break.
    """
    from jasper.output_topology import OutputTopology

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "dual-mono",
        "name": "Mono on a composite sink",
        "status": "verified",
        "hardware": {
            "device_id": "dual_apple_usb_c_dac_4ch",
            "device_label": "Dual Apple",
            "physical_output_count": 4,
            "child_devices": [
                {"child_id": "a", "device_id": "apple_usb_c_dongle",
                 "device_label": "Apple A", "physical_output_indexes": [0, 1]},
                {"child_id": "b", "device_id": "apple_usb_c_dongle",
                 "device_label": "Apple B", "physical_output_indexes": [2, 3]},
            ],
        },
        "speaker_groups": [{
            "id": "main", "label": "Main speaker", "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        }],
        "routing": {"mono_group_id": "main"},
    })


def _master_gain_mapping(doc) -> dict[int, list[dict]]:
    mixer = doc["mixers"]["master_gain"]
    assert mixer["channels"] == {"in": 2, "out": 2}
    return {entry["dest"]: entry["sources"] for entry in mixer["mapping"]}


def _channel_filter_names(doc, channel: int) -> list[str]:
    for step in doc["pipeline"]:
        if step.get("type") == "Filter" and step.get("channels") == [channel]:
            return step["names"]
    raise AssertionError(f"no Filter step for channel {channel}")


@pytest.mark.parametrize(
    ("make_topology", "fold_output"),
    [
        pytest.param(lambda: _mono_topology(0), 0, id="mono-on-output-0"),
        pytest.param(lambda: _mono_topology(1), 1, id="mono-on-output-1"),
        pytest.param(_stereo_topology, None, id="stereo"),
        pytest.param(_unconfigured_draft, None, id="unconfigured-draft"),
        pytest.param(_composite_mono_topology, None, id="mono-on-composite-sink"),
    ],
)
def test_master_gain_folds_both_program_channels_only_onto_a_declared_mono_output(
    make_topology, fold_output
):
    """One full-range speaker on a 2-channel amp must hear the WHOLE program.

    An identity mixer drops the program's right channel into the output a mono
    topology never declared, so the box plays half the record. The fold sums
    both channels onto the one declared output at the clip-safe
    ``MONO_SUM_GAIN_DB``, and ONLY for an explicit mono layout whose
    channel-to-output mapping is proven index-wise — a composite sink fans the
    program inside outputd, so folding there breaks a working multi-DAC box.
    """
    import yaml as yaml_lib

    from jasper.active_speaker.camilla_yaml import output_commission_mute_name
    from jasper.camilla_emit import MONO_SUM_GAIN_DB
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    doc = yaml_lib.safe_load(
        emit_flat_outputd_cutover_config(topology=make_topology())
    )

    # Non-negotiable on every shape: the master fader can never boost.
    assert doc["devices"]["volume_limit"] == 0.0
    # Capture and playback stay at the DAC's real stereo width. The fold lives
    # in the graph; the device is never narrowed.
    assert doc["devices"]["capture"]["channels"] == 2
    assert doc["devices"]["playback"]["channels"] == 2

    mapping = _master_gain_mapping(doc)
    assert sorted(mapping) == [0, 1]
    for dest, sources in mapping.items():
        if dest == fold_output:
            assert [source["channel"] for source in sources] == [0, 1]
            assert [source["gain"] for source in sources] == [
                pytest.approx(MONO_SUM_GAIN_DB, abs=1e-4)
            ] * 2
            assert [source["inverted"] for source in sources] == [False, False]
        else:
            assert sources == [{"channel": dest, "gain": 0, "inverted": False}]

    # The fold's other half: the unclaimed output stays terminally hard muted.
    # Folding without it would sum the program onto the claimed output AND
    # leave raw program on one the topology never declared.
    for channel in (0, 1):
        names = _channel_filter_names(doc, channel)
        expect_muted = fold_output is not None and channel != fold_output
        mute_name = output_commission_mute_name(channel)
        assert (names[-1] == mute_name) is expect_muted
        if expect_muted:
            assert doc["filters"][mute_name]["parameters"]["mute"] is True


def test_mono_fold_is_refused_when_it_would_leave_an_output_live():
    """The fold and the complement mute are two halves of one answer.

    Either half alone emits program on an output the topology does not assign,
    so the emitter refuses rather than shipping a graph nobody meant.
    """
    from jasper.sound.camilla_yaml import FLAT_GRAPH_WIDTH

    # Fold with no mute at all: channel 1 stays live on an undeclared output.
    with pytest.raises(ValueError):
        emit_sound_config(SoundProfile(enabled=False), mono_fold_output=0)
    # An out-of-width destination needs no guard of its own: it leaves an
    # in-width channel unmuted and trips the same one.
    with pytest.raises(ValueError):
        emit_sound_config(
            SoundProfile(enabled=False),
            muted_outputs=[0],
            mono_fold_output=FLAT_GRAPH_WIDTH,
        )


def test_composite_sink_is_never_index_muted():
    """A dual-Apple composite puts L on output 0 and R on output 2.

    outputd fans the stereo program across the child DACs, so Camilla channel 1
    is NOT physical output 1 there. Muting by index would silence a working
    speaker, so the emitter must decline.
    """
    from jasper.output_topology import OutputTopology
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    composite = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "dual",
        "name": "Dual Apple",
        "status": "verified",
        "hardware": {
            "device_id": "dual_apple_usb_c_dac_4ch",
            "device_label": "Dual Apple",
            "physical_output_count": 4,
            "child_devices": [
                {"child_id": "a", "device_id": "apple_usb_c_dongle",
                 "device_label": "Apple A", "physical_output_indexes": [0, 1]},
                {"child_id": "b", "device_id": "apple_usb_c_dongle",
                 "device_label": "Apple B", "physical_output_indexes": [2, 3]},
            ],
        },
        "speaker_groups": [
            {"id": "left", "label": "Left", "kind": "left",
             "mode": "full_range_passive",
             "channels": [{"role": "full_range", "physical_output_index": 0}]},
            {"id": "right", "label": "Right", "kind": "right",
             "mode": "full_range_passive",
             "channels": [{"role": "full_range", "physical_output_index": 2}]},
        ],
        "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
    })

    assert "commission_mute" not in emit_flat_outputd_cutover_config(topology=composite)


def test_out_of_width_assignment_is_never_index_muted():
    """A claim on output 5 proves the graph is not addressing outputs by index.

    Muting every channel of a 2-wide graph would ship a silently silent
    speaker. The emitter declines; the runtime contract then refuses the
    unmuted graph out loud, which is the fail-loud direction.
    """
    from jasper.output_topology import OutputTopology
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    far = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "far",
        "name": "Mono on a high output",
        "status": "verified",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [{
            "id": "main", "label": "Main speaker", "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 5}],
        }],
        "routing": {"mono_group_id": "main"},
    })

    assert "commission_mute" not in emit_flat_outputd_cutover_config(topology=far)


def test_emit_sound_config_rejects_muting_every_channel():
    import pytest

    from jasper.sound.camilla_yaml import FLAT_GRAPH_WIDTH

    with pytest.raises(ValueError, match="wholly silent"):
        emit_sound_config(
            SoundProfile(enabled=False), muted_outputs=range(FLAT_GRAPH_WIDTH)
        )


def test_emit_sound_config_rejects_out_of_range_muted_output():
    import pytest

    with pytest.raises(ValueError, match="playback channels"):
        emit_sound_config(SoundProfile(enabled=False), muted_outputs=[2])


def test_muted_outputs_none_and_empty_are_byte_identical_to_default():
    profile = SoundProfile(enabled=False)
    baseline = emit_sound_config(profile)

    assert emit_sound_config(profile, muted_outputs=None) == baseline
    assert emit_sound_config(profile, muted_outputs=[]) == baseline


def test_muted_outputs_leaves_the_CLAIMED_channel_byte_identical():
    """Muting one channel must not perturb the other's chain.

    The claimed output is the one the household actually hears; the only
    permitted difference between the golden graph and a width-matched one is
    the muted channel's own Filter step plus the mute's filter definition.
    """
    profile = SoundProfile(enabled=False)
    baseline = emit_sound_config(profile)
    width_matched = emit_sound_config(profile, muted_outputs=[1])

    assert _pipeline_names(width_matched, 0) == _pipeline_names(baseline, 0)
    # Everything above the `filters:` block (devices, samplerate, volume_limit,
    # capture/playback) is untouched, and so is the mixer.
    assert width_matched.split("filters:")[0] == baseline.split("filters:")[0]
    assert width_matched.split("mixers:")[1].split("pipeline:")[0] == (
        baseline.split("mixers:")[1].split("pipeline:")[0]
    )


# --- the production call shape ------------------------------------------------
#
# deploy/install.sh calls emit_flat_outputd_cutover_config with out_path ONLY,
# so `topology=None` -> flat_graph_muted_outputs(None, ...) ->
# load_output_topology_strict(). That is the branch a real box executes; every
# other test here injects a topology and skips it.


def _write_topology_artifact(tmp_path, monkeypatch, payload) -> None:
    import json

    artifact = tmp_path / "output_topology.json"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(artifact))


def test_production_call_shape_reads_the_saved_topology_from_disk(
    tmp_path, monkeypatch
):
    from jasper.active_speaker.camilla_yaml import output_commission_mute_name
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    _write_topology_artifact(tmp_path, monkeypatch, _mono_topology(0).to_dict())

    yaml = emit_flat_outputd_cutover_config()

    assert _pipeline_names(yaml, 1) == (
        f"names: [flat, {output_commission_mute_name(1)}]"
    )


def test_production_call_shape_missing_topology_renders_the_golden(
    tmp_path, monkeypatch
):
    """A fresh box has no topology artifact — it must boot the golden graph."""
    from jasper.output_topology import new_topology_draft
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "does-not-exist.json")
    )

    assert emit_flat_outputd_cutover_config() == emit_flat_outputd_cutover_config(
        topology=new_topology_draft()
    )


def test_production_call_shape_corrupt_topology_renders_the_golden_without_raising(
    tmp_path, monkeypatch
):
    """A corrupt topology must not take the RENDER down.

    The renderer declines to guess and emits the unmuted graph; the statefile
    guard is what fails closed on the same corrupt artifact, and it can say why.
    A raise here would abort the deploy at the render step with a traceback
    instead.
    """
    from jasper.output_topology import new_topology_draft
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    artifact = tmp_path / "output_topology.json"
    artifact.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(artifact))

    assert emit_flat_outputd_cutover_config() == emit_flat_outputd_cutover_config(
        topology=new_topology_draft()
    )
