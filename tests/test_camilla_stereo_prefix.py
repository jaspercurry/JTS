"""Unit tests for the neutral stereo-prefix builder.

``build_stereo_prefix`` (jasper.camilla_stereo_prefix) is the shared
program-domain assembly: room PEQs -> worst-case-boost headroom -> optional
preamp -> preference filters, returning filter DEFINITIONS plus per-channel
chain NAMES (not the mixer/pipeline). These tests exercise it directly on
DATA inputs — built FilterSpecs + PeqFilters — with no SoundProfile, proving
it is reusable from the neutral leaf layer.

Byte-identity of the full emitted config is pinned separately by
tests/test_sound_camilla_yaml_golden.py.
"""

from __future__ import annotations

# FilterSpec is imported from the neutral contract (not jasper.sound) on
# purpose: the builder must be usable without depending on the sound package.
from jasper.camilla_config_contract import FilterSpec, PeqFilter
from jasper.camilla_stereo_prefix import build_stereo_prefix, emit_filter_spec


def test_solo_cuts_only_no_headroom_no_preamp():
    specs = [FilterSpec("sound_simple_bass", "Peaking", 150.0, 3.0, q=1.0)]
    yaml, left, right, trim = build_stereo_prefix(
        specs, [PeqFilter(freq=80.0, q=4.0, gain=-3.0)]
    )

    # Solo => right chain duplicates left (None signals the duplication).
    assert right is None
    assert trim == 0.0
    # Room cut + preference band + the closing `flat` anchor, in order.
    assert left == ["room_peq_1", "sound_simple_bass", "flat"]
    # Definitions exist for each named filter and the flat anchor.
    assert "  room_peq_1:" in yaml
    assert "  sound_simple_bass:" in yaml
    assert "  flat:" in yaml
    # Cuts-only correction adds no headroom and no preamp.
    assert "room_headroom" not in yaml
    assert "sound_preamp" not in yaml


def test_room_boost_inserts_worst_case_headroom():
    yaml, left, right, trim = build_stereo_prefix(
        [],
        [
            PeqFilter(freq=45.0, q=5.0, gain=2.0),   # +2 boost
            PeqFilter(freq=80.0, q=6.0, gain=-4.0),  # cut (ignored for headroom)
            PeqFilter(freq=120.0, q=4.0, gain=1.0),  # +1 boost
        ],
    )

    # Worst-case additive boost is +3 dB => a single -3 dB headroom gain.
    assert "  room_headroom:" in yaml
    assert "gain: -3.0000" in yaml
    assert left == ["room_peq_1", "room_peq_2", "room_peq_3", "room_headroom", "flat"]
    assert right is None
    # No preference filters => no preamp even if a trim were passed.
    assert "sound_preamp" not in yaml


def test_output_trim_emits_single_preamp_only_with_preference_filters():
    specs = [FilterSpec("sound_simple_bass", "Peaking", 150.0, 6.0, q=1.0)]
    yaml, left, _right, trim = build_stereo_prefix(specs, [], output_trim_db=4.0)

    assert trim == 4.0
    assert "  sound_preamp:" in yaml
    assert "gain: -4.0000" in yaml
    # Preamp sits ahead of the preference band, then the flat anchor.
    assert left == ["sound_preamp", "sound_simple_bass", "flat"]


def test_trim_ignored_when_no_preference_filters():
    # A flat program can't clip from EQ, so a configured trim is a no-op.
    yaml, left, _right, trim = build_stereo_prefix([], [], output_trim_db=6.0)

    assert trim == 0.0
    assert "sound_preamp" not in yaml
    assert left == ["flat"]


def test_leader_bake_distinct_room_chains_share_preference_tail():
    specs = [FilterSpec("sound_simple_bass", "Peaking", 150.0, 2.0, q=1.0)]
    yaml, left, right, trim = build_stereo_prefix(
        specs,
        [PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        room_peqs_right=[
            PeqFilter(freq=120.0, q=3.0, gain=-2.0),
            PeqFilter(freq=4000.0, q=2.0, gain=1.0),  # +1 boost on the right seat
        ],
        output_trim_db=4.0,
    )

    assert right is not None
    # Per-seat ROOM segments differ; both seats carry the same shared tail
    # (headroom -> preamp -> preference -> flat). The right seat's +1 boost
    # drives a shared -1 dB room headroom protecting both chains.
    assert left == [
        "room_peq_1", "room_headroom", "sound_preamp", "sound_simple_bass", "flat",
    ]
    assert right == [
        "room_peq_r1", "room_peq_r2",
        "room_headroom", "sound_preamp", "sound_simple_bass", "flat",
    ]
    # Shared filters are DEFINED once, referenced by both chains.
    assert yaml.count("  room_headroom:") == 1
    assert yaml.count("  sound_preamp:") == 1
    assert yaml.count("  sound_simple_bass:") == 1
    assert "gain: -1.0000" in yaml  # headroom = louder (right) channel boost


def test_empty_right_bakes_flat_right_segment_distinct_from_solo():
    # [] (an uncalibrated follower) is distinct from None (solo): the right
    # chain exists but carries no room filter.
    _yaml, left, right, _trim = build_stereo_prefix(
        [], [PeqFilter(freq=120.0, q=3.0, gain=-2.0)], room_peqs_right=[]
    )
    assert left == ["room_peq_1", "flat"]
    assert right == ["flat"]


def test_channel_delays_emit_delay_filters_on_each_room_chain():
    yaml, left, right, _trim = build_stereo_prefix(
        [], [], room_peqs_right=[], channel_delays_ms=(1.25, 0.5)
    )
    assert "  room_delay_l:" in yaml
    assert "  room_delay_r:" in yaml
    assert "type: Delay" in yaml
    assert "delay: 1.2500" in yaml
    assert left == ["room_delay_l", "flat"]
    assert right == ["room_delay_r", "flat"]


def test_zero_delays_emit_nothing():
    yaml, left, right, _trim = build_stereo_prefix(
        [], [], room_peqs_right=[], channel_delays_ms=(0.0, 0.0)
    )
    assert "room_delay" not in yaml
    assert left == ["flat"]
    assert right == ["flat"]


def test_emit_filter_spec_dispatches_by_biquad_type():
    # Both shelf types: slope + gain, no q.
    for kind, gain in (("Lowshelf", 4.0), ("Highshelf", -2.5)):
        shelf = "\n".join(emit_filter_spec(FilterSpec("f", kind, 100.0, gain, slope=6.0)))
        assert f"type: {kind}" in shelf and "slope: 6.0000" in shelf
        assert f"gain: {gain:.4f}" in shelf
        assert "\n      q:" not in shelf  # no q param line (freq: contains "q:")
    # Gainless (Highpass): q only, no gain term.
    hp = "\n".join(emit_filter_spec(FilterSpec("f", "Highpass", 30.0, 0.0, q=0.7)))
    assert "type: Highpass" in hp and "q: 0.7000" in hp
    assert "gain:" not in hp
    # Peaking: q + gain.
    peak = "\n".join(emit_filter_spec(FilterSpec("f", "Peaking", 1000.0, 3.0, q=1.0)))
    assert "type: Peaking" in peak and "q: 1.0000" in peak and "gain: 3.0000" in peak


def test_sound_filters_input_is_normalized_so_a_generator_is_safe():
    """The builder is shared (stereo today, active pre-split next), so it must
    normalize sound_filters at the boundary: a one-shot generator must iterate
    AND gate the preamp by emptiness — not be truthy-but-empty or consumed."""
    specs = [FilterSpec("sound_simple_bass", "Peaking", 150.0, 6.0, q=1.0)]
    _yaml, left, _right, trim = build_stereo_prefix(
        (s for s in specs), [], output_trim_db=4.0
    )
    assert trim == 4.0
    assert left == ["sound_preamp", "sound_simple_bass", "flat"]

    # An empty generator is falsy-by-content after normalization: no preamp.
    _yaml, left, _right, trim = build_stereo_prefix(
        (s for s in []), [], output_trim_db=4.0
    )
    assert trim == 0.0
    assert left == ["flat"]
