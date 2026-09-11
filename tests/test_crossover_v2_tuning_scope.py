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

from copy import deepcopy
from dataclasses import replace
from typing import Sequence

import pytest
import yaml

from jasper.active_speaker.commissioning_admission import (
    ActiveCommissioningAdmissionError,
    running_graph_fingerprint,
)
from jasper.active_speaker.crossover_v2.measure_spec import (
    CANDIDATE_SCOPES,
    GRAPH_SCOPES,
    MeasureSpec,
)
from jasper.active_speaker.crossover_v2.tuning_scope import tuning_scope_fingerprint
from jasper.active_speaker.baseline_profile import recompose_applied_baseline_yaml
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
from jasper.active_speaker.measurement_emit import (
    MeasurementGraphProfile,
    MeasurementGraphRefused,
    compile_tuning_graph,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    MeasuredCrossoverCandidateError,
    compile_candidate_config,
    candidate_room_peqs,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.camilla_config_contract import FilterSpec
from jasper.camilla_emit import emit_gain_filter
from jasper.camilla_stereo_prefix import emit_filter_spec
from jasper.sound.camilla_yaml import extract_room_peqs_from_config_text
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
from tests.test_active_speaker_audition import ACTIVE_PCM, LINEARIZATION, _applied_profile
from tests.test_active_speaker_measured_crossover_candidate import _room_correction
from tests.test_active_speaker_runtime_contract import _active_topology

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


@pytest.fixture
def tuning_profile():
    topology = _active_topology("mono", "active_2_way")
    applied = _applied_profile(topology)
    preset = ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"])
    region = replace(preset.crossover_regions[0], delay_target_driver="woofer", delay_ms=0.4)
    preset = replace(preset, crossover_regions=(region,))
    applied["recomposition_snapshot"]["preset"] = preset.to_dict()
    return MeasurementGraphProfile(
        preset, topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM,
        applied_profile=applied,
    )


@pytest.mark.parametrize("purpose", ["speaker", "room", "bass"])
def test_program_baselines_keep_only_their_lower_layers(tuning_profile, tmp_path, purpose):
    snapshot = tuning_profile.applied_profile["recomposition_snapshot"]
    snapshot["room_correction"] = _room_correction()
    snapshot["bass_extension"] = BASS_EXTENSION
    saved = deepcopy(tuning_profile.applied_profile)
    candidate = candidate_from_applied_profile(tuning_profile.topology, saved, purpose=purpose)
    graph = yaml.safe_load(compile_tuning_graph(tuning_profile, candidate=candidate))
    assert bool(candidate.linearization) is (purpose != "speaker")
    assert bool(candidate.blend_correction) is (purpose != "speaker")
    assert bool(candidate.room_correction) is (purpose == "bass")
    assert not candidate.bass_extension
    if purpose == "speaker":
        assert candidate.role_attenuations_db == {"woofer": 0.0, "tweeter": 0.0}
        assert candidate.source_preset == tuning_profile.preset
    else:
        expected, issues = recompose_applied_baseline_yaml(
            tuning_profile.topology, applied_profile=saved, bass_extension={},
            room_peqs=None if purpose == "bass" else (),
        )
        assert not issues and graph == yaml.safe_load(expected)
    assert graph["devices"]["volume_limit"] == 0.0
    assert not set(graph["filters"]) & sound_filter_slot_names()
    assert tuning_profile.applied_profile == saved


def _trial_candidate(profile, *, trim=-3.0, gain=-2.0):
    return MeasuredCrossoverCandidate(
        program_id="trial", analysis={"source": "prescribed"},
        source_preset=profile.preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": trim},
        linearization={"woofer": {"filters": [
            {"biquad_type": "Peaking", "freq": 420.0, "q": 3.0, "gain": gain},
        ]}},
        blend_correction=({"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -1.0},),
    )


def test_candidate_compilation_carries_all_parts_and_its_own_identity(tuning_profile):
    a = _trial_candidate(tuning_profile)
    b = _trial_candidate(tuning_profile, trim=-5.0, gain=4.0)
    text_a = compile_tuning_graph(tuning_profile, candidate=a)
    text_b = compile_tuning_graph(tuning_profile, candidate=b)
    filters_a = yaml.safe_load(text_a)["filters"]
    filters_b = yaml.safe_load(text_b)["filters"]
    for filters, trim, gain in ((filters_a, -3.0, -2.0), (filters_b, -5.0, 4.0)):
        assert filters["as_tweeter_baseline_gain"]["parameters"]["gain"] == trim
        assert filters["as_woofer_linearization_peak_1"]["parameters"]["gain"] == gain
        assert filters["as_blend_1"]["parameters"]["gain"] == -1.0
        assert filters["as_woofer_delay"]["parameters"]["delay"] == 0.4
        assert not set(filters) & sound_filter_slot_names()
    assert filters_b["active_baseline_headroom"] != filters_a["active_baseline_headroom"]
    assert running_graph_fingerprint(text_a) != running_graph_fingerprint(text_b)
    assert a.fingerprint != b.fingerprint
    assert tuning_profile.applied_profile["recomposition_snapshot"]["linearization"] == LINEARIZATION


@pytest.mark.parametrize("problem, reason", [
    ("unknown_role", "measurement_filters_invalid"),
    ("malformed_filter", "measurement_filters_invalid"),
])
def test_candidate_compile_refuses_unrenderable_identity(tuning_profile, problem, reason):
    candidate = _trial_candidate(tuning_profile)
    candidate = replace(candidate, linearization={
        "other" if problem == "unknown_role" else "woofer": {"filters": [
            "broken" if problem == "malformed_filter" else
            {"biquad_type": "Peaking", "freq": 420.0, "q": 3.0, "gain": -2.0},
        ]},
    })
    with pytest.raises(MeasurementGraphRefused) as exc:
        compile_tuning_graph(tuning_profile, candidate=candidate)
    assert exc.value.reason == reason


@pytest.mark.parametrize("scope", sorted(CANDIDATE_SCOPES))
def test_candidate_scopes_require_a_named_candidate(scope):
    assert set(GRAPH_SCOPES) == {"drivers", "candidate", "candidate_branches"}
    with pytest.raises(ValueError):
        MeasureSpec(kind="baseline", graph_scope=scope)
    assert MeasureSpec(kind="baseline", graph_scope=scope, candidate_id="fp-a").candidate_id == "fp-a"


def _room_candidate(tuning_profile, *, linearization_gain: float | None = None):
    """A room candidate whose speaker layer IS the fixture's applied tune.

    Every part is read back out of the applied snapshot, so the fixture stays
    the one source of the tune. ``polarity="invert"`` because that snapshot's
    corrections invert the tweeter while the declared preset does not, and
    ``linearization_gain`` moves one filter off the applied tune.
    """

    snapshot = tuning_profile.applied_profile["recomposition_snapshot"]
    corrections = snapshot["corrections"]
    linearization = deepcopy(snapshot["linearization"])
    if linearization_gain is not None:
        linearization["woofer"][0]["gain"] = linearization_gain
    return MeasuredCrossoverCandidate(
        program_id="room-trial", analysis={"source": "prescribed"},
        source_preset=tuning_profile.preset,
        role_attenuations_db={
            role: entry["gain_db"] for role, entry in corrections.items()
        },
        alignment=MeasuredCrossoverAlignment(
            corrections["woofer"]["delay_ms"] * 1000.0, "woofer", "invert",
        ),
        linearization={
            role: {"filters": filters} for role, filters in linearization.items()
        },
        blend_correction=tuple(snapshot["blend_correction"]),
        room_correction=_room_correction(),
    )


@pytest.mark.parametrize("layers", ["speaker", "room", "bass"])
def test_trial_plays_composed_layers_independent_of_the_applied_profile(tuning_profile, layers):
    candidate = _room_candidate(tuning_profile, linearization_gain=-9.0)
    candidate = replace(candidate, room_correction={} if layers == "speaker" else candidate.room_correction,
                        bass_extension=BASS_EXTENSION if layers == "bass" else {})
    applied_room = deepcopy(_room_correction())
    for filters in applied_room["sides"].values():
        for entry in filters:
            entry["gain"] = -1.0
    tuning_profile.applied_profile["recomposition_snapshot"]["room_correction"] = applied_room
    text = compile_tuning_graph(tuning_profile, candidate=candidate)
    expected = compile_candidate_config(candidate, playback_device=tuning_profile.playback_device,
                                        room_peqs=candidate_room_peqs(candidate))
    assert yaml.safe_load(text) == yaml.safe_load(expected)
    assert extract_room_peqs_from_config_text(text) == list(candidate_room_peqs(candidate))


@pytest.mark.parametrize("scope", ["candidate", "candidate_branches"])
def test_an_unprovable_composed_graph_refuses_before_play(tuning_profile, monkeypatch, scope):
    from jasper.active_speaker import measurement_emit

    candidate = replace(_room_candidate(tuning_profile), bass_extension=BASS_EXTENSION)
    graph = yaml.safe_load(compile_tuning_graph(tuning_profile, candidate=candidate))
    for entry in graph["filters"].values():
        if "Highpass" in entry.get("parameters", {}).get("type", ""):
            entry["parameters"]["type"] = entry["parameters"]["type"].replace("Highpass", "Lowpass")
    monkeypatch.setattr(measurement_emit, "compile_candidate_config", lambda *a, **kw: yaml.safe_dump(graph))
    with pytest.raises(MeasuredCrossoverCandidateError) as exc:
        compile_tuning_graph(tuning_profile, scope=scope, candidate=candidate)
    assert exc.value.code == "tweeter_unprotected"


@pytest.mark.parametrize("layers", ["speaker", "room", "bass"])
def test_branch_routing_preserves_every_candidate_filter_and_output_chain(tuning_profile, layers):
    candidate = _trial_candidate(tuning_profile, trim=-5, gain=4)
    candidate = replace(candidate, room_correction={} if layers == "speaker" else _room_correction(),
                        bass_extension=BASS_EXTENSION if layers == "bass" else {})
    original = yaml.safe_load(compile_tuning_graph(tuning_profile, candidate=candidate))
    split = yaml.safe_load(compile_tuning_graph(tuning_profile, scope="candidate_branches", candidate=candidate))
    assert split["filters"] == original["filters"]
    assert split["pipeline"] == original["pipeline"]
    assert split["devices"] == original["devices"]
    mapping = next(iter(split["mixers"].values()))["mapping"]
    for output in tuning_profile.preset.channel_map.outputs:
        entry, = [row for row in mapping if row["dest"] == output.index]
        source, = entry["sources"]
        assert source["channel"] == tuning_profile.role_channels[output.driver_role]
        assert source["gain"] == 0
        assert not source["inverted"]


BASS_EXTENSION = {
    "low_boost_db": 4.0,
    "reference_level_db": -10.0,
    "detector_lowpass_hz": 120.0,
    "compressor_threshold_dbfs": -15.0,
}


@pytest.mark.parametrize("scope", sorted(CANDIDATE_SCOPES))
def test_peak_admission_uses_the_composed_bass_layer(tuning_profile, scope):
    from jasper.active_speaker.measurement_emit import measurement_bass_extension

    candidate = replace(_room_candidate(tuning_profile), bass_extension=BASS_EXTENSION)
    assert measurement_bass_extension(tuning_profile, scope=scope, candidate=candidate) == candidate.bass_extension


@pytest.fixture(autouse=True)
def banked_program_baseline(monkeypatch):
    from jasper.active_speaker import candidate_parts

    monkeypatch.setattr(candidate_parts, "baseline_candidate_id", lambda purpose: "baseline-" + (purpose or "speaker"))


@pytest.mark.parametrize("purpose", ["speaker", "room", "bass"])
def test_program_base_is_banked_and_reopens_by_its_fingerprint(tuning_profile, tmp_path, purpose):
    from jasper.active_speaker.candidate_bank import find_banked_candidate, publish_authored_candidate

    candidate = candidate_from_applied_profile(tuning_profile.topology, tuning_profile.applied_profile, purpose=purpose)
    banked = publish_authored_candidate(candidate, root=tmp_path)
    reopened = find_banked_candidate(banked.fingerprint, root=tmp_path).candidate
    assert reopened.fingerprint == candidate.fingerprint
    assert yaml.safe_load(compile_tuning_graph(tuning_profile, candidate=reopened)) == yaml.safe_load(
        compile_tuning_graph(tuning_profile, candidate=candidate)
    )
