# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The comparability fingerprint a round banks: scoped to what it measures through.

#3489. A round has to be able to tell that the bytes under its own captures
moved, and it has to NOT say so when what moved was the household's taste EQ —
a layer no capture in the round goes through. Both halves are pinned here, and
the second one is pinned against the whole-graph hash so the exclusion cannot
pass by hashing nothing.

Preference filters come from the real owner (``jasper.sound.profile``) and are
spelled by the real emitter, because the claim under test is that the exclusion
set matches the names a graph actually carries — a fixture with hand-typed
names would pin the test's spelling instead of the product's.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from jasper.active_speaker.commissioning_admission import (
    ActiveCommissioningAdmissionError,
    running_graph_fingerprint,
)
from jasper.active_speaker.crossover_v2.tuning_scope import tuning_scope_fingerprint
from jasper.camilla_config_contract import FilterSpec
from jasper.camilla_emit import emit_gain_filter
from jasper.camilla_stereo_prefix import emit_filter_spec
from jasper.sound.profile import (
    CURVE_PRESETS,
    MAX_PARAMETRIC_BANDS,
    ParametricBand,
    SimpleEq,
    SoundProfile,
    build_sound_filter_slots,
    build_sound_filters,
    sound_filter_slot_names,
)

FLAT = SoundProfile()
#: One household save: a bass lift on a Simple band, one advanced band taken
#: into use, and a stock curve chosen — three of the layer's three families, so
#: the exclusion is exercised across all of them rather than on Simple alone.
SAVED = SoundProfile(
    curve_id="harman",
    simple_eq=SimpleEq(bass_db=4.0),
    parametric_bands=(ParametricBand(freq_hz=2500.0, gain_db=-2.5, q=2.0),),
)


def household_graph(
    preference: Sequence[FilterSpec], *, woofer_gain_db: float = -3.0,
) -> str:
    """A CamillaDSP-shaped graph: one tuning layer, one preference layer.

    ``woofer_gain_db`` is the tuning layer's one movable number — a per-driver
    trim, below the split, which is exactly the kind of change a round MUST
    see.
    """

    lines = ["devices:", "  samplerate: 48000", "filters:"]
    lines += emit_gain_filter("active_baseline_headroom", -6.0)
    lines += emit_gain_filter("driver_woofer_gain", woofer_gain_db)
    for spec in preference:
        lines += emit_filter_spec(spec)
    lines += [
        "pipeline:",
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_baseline_headroom]",
    ]
    if preference:
        lines += [
            "  - type: Filter",
            "    channels: [0, 1]",
            f"    names: [{', '.join(spec.name for spec in preference)}]",
        ]
    lines += [
        "  - type: Filter",
        "    channels: [0]",
        "    names: [driver_woofer_gain]",
    ]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "before, after",
    [
        pytest.param(
            build_sound_filters(FLAT), build_sound_filters(SAVED),
            id="legacy_active_bands_only",
        ),
        pytest.param(
            build_sound_filter_slots(FLAT), build_sound_filter_slots(SAVED),
            id="fixed_frame_slots",
        ),
        pytest.param(
            build_sound_filters(FLAT), build_sound_filter_slots(SAVED),
            id="frame_arrives_under_the_round",
        ),
    ],
)
def test_a_preference_eq_save_is_not_a_comparability_boundary(before, after):
    """The false boundary this exists to prevent, in every graph shape.

    ``fixed_frame_slots`` is what every emitter writes since #3492: a slot per
    declared band, idle ones neutral. ``legacy_active_bands_only`` is what a
    graph banked before it carries — still reachable, because a round can
    re-read an entry graph written by an older build.

    ``frame_arrives_under_the_round`` is the migration itself, and it is the
    sharpest: a round enters on a pre-frame graph, the box is
    re-anchored onto a framed one underneath it, and thirteen filters plus a
    whole pipeline step appear. Nothing the round measures through moved, so
    the scope must not budge.

    The save is real in every case — the whole-graph content hash moves, which
    is the assertion that keeps this from passing vacuously — and the tuning
    scope does not.
    """

    flat = household_graph(before)
    after_save = household_graph(after)

    assert running_graph_fingerprint(flat) != running_graph_fingerprint(after_save)
    assert tuning_scope_fingerprint(flat) == tuning_scope_fingerprint(after_save)


def test_a_change_to_a_tuning_layer_moves_the_scope_fingerprint():
    """The boundary that must fire: one number in a layer under tune.

    Asserted with the preference layer held at the saved profile, so the only
    difference between the two graphs is the per-driver trim.
    """

    saved = build_sound_filters(SAVED)

    assert tuning_scope_fingerprint(
        household_graph(saved)
    ) != tuning_scope_fingerprint(
        household_graph(saved, woofer_gain_db=-4.0)
    )


def test_the_scope_refuses_a_graph_it_cannot_parse():
    """An unparseable readback is refused, not hashed as the empty document.

    Same refusal the whole-graph substrate makes, because a fingerprint over
    nothing compares equal to every other fingerprint over nothing — which
    would silently answer "comparable" for a session that could read no graph
    at all.
    """

    with pytest.raises(ActiveCommissioningAdmissionError):
        tuning_scope_fingerprint("- not: a mapping\n")


def test_the_exclusion_set_covers_every_name_the_emitter_can_produce():
    """The invariant the two pins above only reach by consequence.

    ``sound_filter_slot_names`` is a CLOSED set derived from the three
    declarations; ``build_sound_filter_slots`` is what a profile actually
    emits. A name the second can produce and the first does not know is a real
    preference slot leaking into the tuning-scope hash, and it leaks SILENTLY:
    an idle slot is identical either side of most saves, so the round-trip pins
    stay green while the exclusion is wrong. This asks the question directly.

    The advanced pool is the specific way this breaks. It is fixed at
    ``MAX_PARAMETRIC_BANDS`` whatever a profile declares (#3492), so a
    derivation reading a PROFILE's bands rather than the pool would
    under-report for every household with fewer than eight.
    """

    names = sound_filter_slot_names()
    pool = {f"sound_advanced_{i}" for i in range(1, MAX_PARAMETRIC_BANDS + 1)}
    assert pool <= names

    emitted: set[str] = set()
    for enabled in (True, False):
        for preset in CURVE_PRESETS:
            for band_count in (0, 1, MAX_PARAMETRIC_BANDS):
                for biquad_type in ("Peaking", "Highshelf", "Notch"):
                    emitted |= {
                        spec.name
                        for spec in build_sound_filter_slots(
                            SoundProfile(
                                enabled=enabled,
                                curve_id=preset.id,
                                simple_eq=SimpleEq(bass_db=3.0),
                                parametric_bands=tuple(
                                    ParametricBand(
                                        biquad_type=biquad_type,
                                        freq_hz=800.0 + index,
                                        gain_db=2.0,
                                    )
                                    for index in range(band_count)
                                ),
                            )
                        )
                    }

    assert emitted, "the sweep emitted nothing — it would pass vacuously"
    assert emitted <= names
