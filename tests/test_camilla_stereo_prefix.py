# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the neutral stereo-prefix builder.

``build_stereo_prefix`` (jasper.audio_routes.camilla_stereo_prefix) is the shared
program-domain assembly: room PEQs -> netted-peak room headroom -> optional
preamp -> preference filters, returning filter DEFINITIONS plus per-channel
chain NAMES (not the mixer/pipeline). These tests exercise it directly on
DATA inputs — built FilterSpecs + PeqFilters — with no SoundProfile, proving
it is reusable from the neutral leaf layer.

Byte-identity of the full emitted config is pinned separately by
tests/test_sound_camilla_yaml_golden.py.
"""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml as pyyaml

from jasper.audio_measurement.room_boundary import ROOM_BOUNDARY_MAX_HZ, ROOM_FLOOR_HZ
from jasper.audio_measurement.room_limits import (
    ROOM_MAX_CUT_DB,
    ROOM_MAX_FILTER_BOOST_DB,
    ROOM_MAX_FILTERS_PER_SIDE,
    ROOM_PEQ_Q_MAX,
    ROOM_PEQ_Q_MIN,
)
from jasper.platform.biquad import (
    EVALUABLE_HZ_MAX,
    EVALUABLE_HZ_MIN,
    HEADROOM_MARGIN_DB,
    PEAK_EPS_DB,
    RESPONSE_SAMPLE_RATE_HZ,
    FilterSpec,
    PeqFilter,
    biquad_coeffs,
)
from jasper.audio_routes.camilla_stereo_prefix import build_stereo_prefix, emit_filter_spec

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The most the room chain's grid peak may read under its dense peak, dB, for a
#: room inside the declared bounds (measured worst: about 0.08 dB; ADR-0399).
_GRID_RESIDUE_DB = 0.1


def _dense_peak_db(peqs: list[PeqFilter]) -> float:
    """The cascade's true peak: a 1/1000-octave grid over the evaluable span, every centre in it."""
    freqs = np.unique(np.concatenate([
        np.geomspace(EVALUABLE_HZ_MIN, EVALUABLE_HZ_MAX, 15_000), [peq.freq for peq in peqs],
    ]))
    z = np.exp(-2j * np.pi * freqs / RESPONSE_SAMPLE_RATE_HZ)
    response = np.ones_like(z)
    for peq in peqs:
        b0, b1, b2, a0, a1, a2 = biquad_coeffs("Peaking", peq.freq, peq.gain, peq.q)
        response *= (b0 + b1 * z + b2 * z * z) / (a0 + a1 * z + a2 * z * z)
    return float(20.0 * np.log10(np.max(np.abs(response))))


def _charged_db(filters_yaml: str) -> float:
    """The attenuation the emitted ``room_headroom`` applies, dB; 0.0 when none is emitted."""
    filters = pyyaml.safe_load(filters_yaml)
    return -filters["room_headroom"]["parameters"]["gain"] if "room_headroom" in filters else 0.0


@pytest.mark.parametrize("room", [
    [PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
    # A boost inside a deeper, wider cut nets under unity everywhere.
    [PeqFilter(freq=100.0, q=1.0, gain=-6.0), PeqFilter(freq=100.0, q=8.0, gain=3.0)],
])
def test_a_room_that_never_leaves_unity_adds_no_headroom_and_an_inert_preamp(room):
    specs = [FilterSpec("sound_simple_bass", "Peaking", 150.0, 3.0, q=1.0)]
    yaml, left, right, trim = build_stereo_prefix(specs, room)

    # Solo => right chain duplicates left (None signals the duplication).
    assert right is None
    assert trim == 0.0
    # Room filters + preference band + the closing `flat` anchor, in order.
    room_names = [f"room_peq_{i}" for i in range(1, len(room) + 1)]
    assert left == [*room_names, "sound_preamp", "sound_simple_bass", "flat"]
    # Definitions exist for each named filter and the flat anchor.
    assert "  room_peq_1:" in yaml
    assert "  sound_simple_bass:" in yaml
    assert "  flat:" in yaml
    # A room that never leaves unity adds no headroom, and the always-present
    # preamp is inert -- 0 dB, spelled without a negative zero so it compares cleanly.
    assert "room_headroom" not in yaml
    assert "  sound_preamp:" in yaml
    assert "gain: 0.0000" in yaml
    assert "gain: -0.0000" not in yaml


@pytest.mark.parametrize("room", [
    # A lone boost reads its own gain, so it charges one margin more than its gain.
    [PeqFilter(freq=90.0, q=4.0, gain=3.0)],
    # The cut nets against both boosts: under their 3.0 dB sum, margin included.
    [
        PeqFilter(freq=45.0, q=5.0, gain=2.0),
        PeqFilter(freq=80.0, q=6.0, gain=-4.0),
        PeqFilter(freq=120.0, q=4.0, gain=1.0),
    ],
])
def test_a_boosted_room_charges_its_netted_peak_plus_the_margin(room):
    yaml, left, right, _trim = build_stereo_prefix([], room)

    assert _charged_db(yaml) == pytest.approx(_dense_peak_db(room) + HEADROOM_MARGIN_DB, abs=1e-3)
    room_names = [f"room_peq_{i}" for i in range(1, len(room) + 1)]
    assert left == [*room_names, "room_headroom", "sound_preamp", "flat"]
    assert right is None
    # The preamp is always defined; with no trim configured it is 0 dB, so it
    # is present and inert rather than absent. Its presence never depends on a
    # value -- that is what keeps a flat-window crossing a parameter write.
    assert "  sound_preamp:" in yaml


def _room_limits_room(rng: random.Random) -> list[PeqFilter]:
    return [
        PeqFilter(
            freq=rng.uniform(ROOM_FLOOR_HZ, ROOM_BOUNDARY_MAX_HZ),
            q=rng.uniform(ROOM_PEQ_Q_MIN, ROOM_PEQ_Q_MAX),
            gain=rng.uniform(ROOM_MAX_CUT_DB, ROOM_MAX_FILTER_BOOST_DB),
        )
        for _ in range(rng.randint(1, ROOM_MAX_FILTERS_PER_SIDE))
    ]


def _retired_strategy_room(rng: random.Random) -> list[PeqFilter]:
    """The retired passive strategy's bounds, which made the rooms on disk: q 0.7-10, at
    most +2 dB a filter and +3 dB in all (ADR-0399)."""
    while True:
        room = [
            PeqFilter(
                freq=rng.uniform(ROOM_FLOOR_HZ, ROOM_BOUNDARY_MAX_HZ),
                q=rng.uniform(0.7, 10.0),
                gain=rng.uniform(ROOM_MAX_CUT_DB, 2.0),
            )
            for _ in range(rng.randint(1, ROOM_MAX_FILTERS_PER_SIDE))
        ]
        if sum(peq.gain for peq in room if peq.gain > 0.0) <= 3.0:
            return room


@pytest.mark.parametrize("make_room", [_room_limits_room, _retired_strategy_room])
@pytest.mark.parametrize("seed", range(3))
def test_the_room_charge_never_under_states_the_netted_peak(make_room, seed):
    """Random boost/cut rooms inside each producer's bounds, one per seat (ADR-0399).

    The emitted charge covers the louder seat's dense peak within ε plus the grid
    residue; a charged room keeps the margin less that residue; a cuts-only room
    is never charged.
    """
    rng = random.Random(seed)
    for _ in range(40):
        left, right = make_room(rng), make_room(rng)
        yaml, *_ = build_stereo_prefix([], left, room_peqs_right=right)
        charged = _charged_db(yaml)
        truth = max(_dense_peak_db(left), _dense_peak_db(right))

        assert charged >= truth - PEAK_EPS_DB - _GRID_RESIDUE_DB
        assert not charged or charged >= truth + HEADROOM_MARGIN_DB - _GRID_RESIDUE_DB
        if all(peq.gain <= 0.0 for peq in left + right):
            assert "room_headroom" not in yaml


def test_the_room_charge_emits_without_numpy():
    """The emitter re-runs on every /sound live-draft move, so a boosted room is
    charged with numpy unimportable (ADR-0226, ADR-0399)."""
    code = (
        "import sys\n"
        "sys.modules['numpy'] = None\n"
        "from jasper.audio_routes.camilla_stereo_prefix import build_stereo_prefix\n"
        "from jasper.platform.biquad import PeqFilter\n"
        "text, *_ = build_stereo_prefix([], [PeqFilter(90.0, 4.0, 3.0)])\n"
        "raise SystemExit(0 if '  room_headroom:' in text else 1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, check=False, timeout=60,
    )

    assert result.returncode == 0


def test_output_trim_emits_single_preamp_only_with_preference_filters():
    specs = [FilterSpec("sound_simple_bass", "Peaking", 150.0, 6.0, q=1.0)]
    yaml, left, _right, trim = build_stereo_prefix(specs, [], output_trim_db=4.0)

    assert trim == 4.0
    assert "  sound_preamp:" in yaml
    assert "gain: -4.0000" in yaml
    # Preamp sits ahead of the preference band, then the flat anchor.
    assert left == ["sound_preamp", "sound_simple_bass", "flat"]


def test_the_trim_is_a_number_and_the_preamp_is_always_there():
    """The trim applies whatever the profile is doing, and that is the point.

    It used to be gated on the profile having active filters — "a flat program
    can't clip from EQ, so a configured trim is a no-op". That made
    ``sound_preamp`` appear and disappear as a gain crossed the flat window,
    which is a STRUCTURAL change, and CamillaDSP rebuilds the filter group and
    resets every filter's state across one (measured: an acoustically-null
    identity filter added to a chain tore a tone 24 dB above the noise floor).
    Emitting the preamp always makes that crossing a parameter write.

    The dropped promise is user-visible and deliberate: a configured headroom
    trim now attenuates even while the profile is flat. ``volume_limit`` remains
    the hard clip guard; the trim is comfort accounting.
    """
    trimmed, left, _right, trim = build_stereo_prefix([], [], output_trim_db=6.0)
    untrimmed, _l, _r, no_trim = build_stereo_prefix([], [], output_trim_db=0.0)

    assert trim == 6.0
    assert no_trim == 0.0
    # Present either way, so its presence never depends on a value.
    assert "sound_preamp" in trimmed
    assert "sound_preamp" in untrimmed
    assert left == ["sound_preamp", "flat"]


def test_leader_bake_distinct_room_chains_share_preference_tail():
    specs = [FilterSpec("sound_simple_bass", "Peaking", 150.0, 2.0, q=1.0)]
    right_room = [
        PeqFilter(freq=120.0, q=3.0, gain=-2.0),
        PeqFilter(freq=4000.0, q=2.0, gain=1.0),  # +1 boost on the right seat
    ]
    yaml, left, right, trim = build_stereo_prefix(
        specs,
        [PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        room_peqs_right=right_room,
        output_trim_db=4.0,
    )

    assert right is not None
    # Per-seat ROOM segments differ; both seats carry the same shared tail
    # (headroom -> preamp -> preference -> flat). The right seat's +1 boost
    # drives the shared room headroom protecting both chains.
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
    # The louder (right) chain's netted peak plus the margin.
    assert _charged_db(yaml) == pytest.approx(_dense_peak_db(right_room) + HEADROOM_MARGIN_DB, abs=1e-3)


def test_empty_right_bakes_flat_right_segment_distinct_from_solo():
    # [] (an uncalibrated follower) is distinct from None (solo): the right
    # chain exists but carries no room filter.
    _yaml, left, right, _trim = build_stereo_prefix(
        [], [PeqFilter(freq=120.0, q=3.0, gain=-2.0)], room_peqs_right=[]
    )
    assert left == ["room_peq_1", "sound_preamp", "flat"]
    assert right == ["sound_preamp", "flat"]


def test_channel_delays_emit_delay_filters_on_each_room_chain():
    yaml, left, right, _trim = build_stereo_prefix(
        [], [], room_peqs_right=[], channel_delays_ms=(1.25, 0.5)
    )
    assert "  room_delay_l:" in yaml
    assert "  room_delay_r:" in yaml
    assert "type: Delay" in yaml
    assert "delay: 1.2500" in yaml
    assert left == ["room_delay_l", "sound_preamp", "flat"]
    assert right == ["room_delay_r", "sound_preamp", "flat"]


def test_zero_delays_emit_nothing():
    yaml, left, right, _trim = build_stereo_prefix(
        [], [], room_peqs_right=[], channel_delays_ms=(0.0, 0.0)
    )
    assert "room_delay" not in yaml
    assert left == ["sound_preamp", "flat"]
    assert right == ["sound_preamp", "flat"]


def test_emit_filter_spec_dispatches_by_biquad_type():
    # Both shelf types: the fixed Butterworth q + gain, never a slope.
    # SHELF_Q is the number every evaluator in this codebase draws a shelf at,
    # so it is the honest one to write (see biquad.SHELF_Q).
    # A stray `slope` would NOT be caught downstream — CamillaDSP's
    # ShelfSteepness is #[serde(untagged)] and silently ignores it once `q`
    # matches — so the guarantee is structural: FilterSpec carries no
    # steepness field, so the emitter cannot write one. This asserts that.
    for kind, gain in (("Lowshelf", 4.0), ("Highshelf", -2.5)):
        shelf = "\n".join(emit_filter_spec(FilterSpec("f", kind, 100.0, gain)))
        assert f"type: {kind}" in shelf and "\n      q: 0.7071068" in shelf
        assert f"gain: {gain:.4f}" in shelf
        assert "slope" not in shelf
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

    # An empty generator normalizes to an empty tuple without being consumed
    # twice; the trim still applies, because it is a number and not a gate.
    _yaml, left, _right, trim = build_stereo_prefix(
        (s for s in []), [], output_trim_db=4.0
    )
    assert trim == 4.0
    assert left == ["sound_preamp", "flat"]
