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
    ParametricBand,
    SimpleEq,
    SoundProfile,
    build_sound_filter_slots,
    build_sound_filters,
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
        # The emitter writes this step only for a non-empty name list, so a
        # flat household has no preference step at all.
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
    "saved",
    [
        pytest.param(build_sound_filters(SAVED), id="durable_active_only"),
        pytest.param(build_sound_filter_slots(SAVED), id="fixed_frame_slots"),
    ],
)
def test_a_preference_eq_save_is_not_a_comparability_boundary(saved):
    """The false boundary this exists to prevent, in both graph shapes.

    ``durable_active_only`` is the graph that carries just the bands a
    household actually set; ``fixed_frame_slots`` is the one that carries every
    declared slot with the idle ones at 0 dB. The save is real either way — the
    whole-graph content hash moves, which is the assertion that keeps this from
    passing vacuously — and the tuning scope does not, because nothing the
    round measures through changed.
    """

    flat = household_graph(build_sound_filters(FLAT))
    after_save = household_graph(saved)

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
