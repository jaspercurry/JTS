from __future__ import annotations

import dataclasses
import hashlib
import json

import numpy as np
import pytest

from jasper.biquad import (
    FilterSpec,
    SHELF_Q,
    biquad_response_complex,
    filter_response_complex,
    freq_trig,
)

from jasper.bass_extension.dynamic import (
    DynamicBassDescriptor,
    DynamicBassDescriptorError,
    LOUDNESS_TAPER_DB,
    NATIVE_LOUDNESS_CORNER_HZ,
    _delta_response,
    _linkwitz_coeffs,
    _lowshelf_fo_coeffs,
    expected_boost_db,
    loudness_boost_db,
    dynamic_bass_gain_reserve_db,
    validate_dynamic_bass_descriptor,
)
from jasper.bass_extension.dynamic_graph import (
    apply_dynamic_bass_graph,
    build_native_dynamic_bass_graph,
    validated_base_graph,
)


def _descriptor(**changes) -> DynamicBassDescriptor:
    values = {
        "low_boost_db": 6.0,
        "reference_level_db": -6.0,
        "detector_lowpass_hz": 90.0,
        "compressor_threshold_dbfs": -8.0,
    }
    values.update(changes)
    return DynamicBassDescriptor(**values)


# jts3's woofer pair on axis with its rear stage, moved to a 22 Hz Butterworth (#5692).
JTS3_SHAPE = {"source_hz": 90.0, "source_q": 0.6, "target_hz": 22.0, "target_q": 0.707}


def _shaped(**changes) -> DynamicBassDescriptor:
    return _descriptor(**{"low_boost_db": 20.0, "detector_lowpass_hz": 125.0, "delta_highpass_hz": 15.0,
                          "linkwitz_transform": JTS3_SHAPE, **changes})


@pytest.mark.parametrize("boost_db", [6.0, 12.0, 15.0, 20.0])
def test_native_loudness_law_withdraws_over_twenty_db(boost_db: float) -> None:
    descriptor = _descriptor(low_boost_db=boost_db)

    assert loudness_boost_db(-30.0, descriptor) == boost_db
    assert loudness_boost_db(-16.0, descriptor) == boost_db / 2.0
    assert loudness_boost_db(-6.0, descriptor) == 0.0
    assert loudness_boost_db(0.0, descriptor) == 0.0


@pytest.mark.parametrize("boost_db", [0.1, 1.0, 3.0, 6.0, 12.0, 15.0, 20.0])
def test_gain_reserve_covers_native_shelf_delta_phase(boost_db: float) -> None:
    descriptor = _descriptor(low_boost_db=boost_db)
    frequencies = np.geomspace(0.01, 23000.0, 4096)
    shelf = np.asarray(filter_response_complex(
        FilterSpec("native_low", "Lowshelf", NATIVE_LOUDNESS_CORNER_HZ, boost_db), frequencies,
    ))
    gain_envelope = 1.0 + np.abs(shelf - 1.0)

    assert np.max(gain_envelope) <= 10.0 ** (dynamic_bass_gain_reserve_db(descriptor) / 20.0)


@pytest.mark.parametrize("boost_db", [0.1, 6.0, 12.0, 20.0])
@pytest.mark.parametrize("fader_db", [-30.0, -16.5, -6.0, 0.0])
@pytest.mark.parametrize("highpass_hz", [None, 25.0])
def test_expected_boost_matches_native_shelf_delta_proof(boost_db, fader_db, highpass_hz):
    descriptor = _descriptor(low_boost_db=boost_db, delta_highpass_hz=highpass_hz)
    frequencies = np.geomspace(0.01, 23000.0, 4096)
    shelf = np.asarray(filter_response_complex(
        FilterSpec("native_low", "Lowshelf", NATIVE_LOUDNESS_CORNER_HZ, loudness_boost_db(fader_db, descriptor)), frequencies,
    ))
    highpass = np.asarray(filter_response_complex(
        FilterSpec("delta_highpass", "Highpass", highpass_hz, 0.0, SHELF_Q), frequencies,
    )) if highpass_hz is not None else 1.0
    proof = 20 * np.log10(np.abs(1 + (shelf - 1) * highpass))
    assert expected_boost_db(descriptor, fader_db, iter(frequencies)) == pytest.approx(proof, abs=1e-9)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"low_boost_db": 20.01}, "low_boost_db"),
        ({"detector_lowpass_hz": 201.0}, "detector_lowpass_hz"),
        ({"compressor_threshold_dbfs": 0.1}, "compressor_threshold_dbfs"),
        ({"delta_highpass_hz": 90.0}, "delta_highpass_hz"),
        ({"low_boost_db": "6"}, "real number"),
        ({"compressor_factor": True}, "real number"),
    ],
)
def test_descriptor_refuses_values_outside_runtime_bounds(change, message) -> None:
    with pytest.raises(ValueError, match=message):
        _descriptor(**change)


def test_candidate_descriptor_parser_is_strict() -> None:
    raw = {
        "low_boost_db": 6.0,
        "reference_level_db": -6.0,
        "detector_lowpass_hz": 90.0,
        "compressor_threshold_dbfs": -8.0,
    }

    assert validate_dynamic_bass_descriptor(raw) == {
        **raw,
        "compressor_factor": 10.0,
        "compressor_attack_s": 0.01,
        "compressor_release_s": 0.25,
        "delta_highpass_hz": None,
    }
    with pytest.raises(ValueError, match="unknown or missing"):
        validate_dynamic_bass_descriptor({**raw, "duplicate_limit": 1})


def test_graph_forms_delta_and_preserves_non_owner_channels() -> None:
    graph = build_native_dynamic_bass_graph(
        channels=4, owner_channels=(0, 2), descriptor=_descriptor()
    )

    assert [step["type"] for step in graph.pipeline] == [
        "Mixer", "Filter", "Mixer", "Filter", "Mixer", "Filter", "Processor", "Processor", "Mixer"
    ]
    assert graph.filters["bass_ext_dynamic_loudness"]["parameters"]["fader"] == "Aux1"
    form = graph.mixers["bass_ext_dynamic_form_delta"]
    assert form["channels"] == {"in": 6, "out": 8}
    assert form["mapping"][4]["sources"] == [
        {"channel": 4, "gain": 0.0, "inverted": False},
        {"channel": 0, "gain": 0.0, "inverted": True},
    ]
    reduce = graph.mixers["bass_ext_dynamic_reduce"]
    assert reduce["mapping"][1]["sources"] == [
        {"channel": 1, "gain": 0.0, "inverted": False}
    ]
    assert reduce["mapping"][3]["sources"] == [
        {"channel": 3, "gain": 0.0, "inverted": False}
    ]


@pytest.mark.parametrize("groups, expected", [
    ((), {0: ([6], [4]), 2: ([7], [5])}),
    (((0, 2),), {0: ([6], [4, 5])}),
    (((2, 0),), {2: ([6], [5, 4])}),
])
def test_owner_groups_select_one_front_detector_per_reduction(groups, expected) -> None:
    graph = build_native_dynamic_bass_graph(
        channels=4, owner_channels=(0, 2), descriptor=_descriptor(), owner_groups=groups,
    )
    assert set(graph.processors) == {f"bass_ext_dynamic_compress_{front}" for front in expected}
    form = graph.mixers["bass_ext_dynamic_form_delta"]
    assert form["channels"] == {"in": 6, "out": 6 + len(expected)}
    assert graph.pipeline[-len(expected) - 2] == {
        "type": "Filter", "channels": list(range(6, 6 + len(expected))),
        "names": ["bass_ext_dynamic_detector_lowpass"],
    }
    for front, (monitor, process) in expected.items():
        compressor = graph.processors[f"bass_ext_dynamic_compress_{front}"]
        assert compressor["type"] == "Compressor"
        assert compressor["parameters"]["monitor_channels"] == monitor
        assert compressor["parameters"]["process_channels"] == process
        assert compressor["parameters"]["makeup_gain"] == 0.0
        assert form["mapping"][monitor[0]]["sources"] == [
            {"channel": process[0], "gain": 0.0, "inverted": False},
        ]
        assert graph.mixers["bass_ext_dynamic_expand"]["mapping"][process[0]]["sources"][0]["channel"] == front


@pytest.mark.parametrize("groups", [((0,),), ((0, 2), (2,)), ((0, 1),), ((), (0, 2))])
def test_owner_groups_must_cover_each_owner_once(groups):
    with pytest.raises(ValueError):
        build_native_dynamic_bass_graph(channels=4, owner_channels=(0, 2), descriptor=_descriptor(), owner_groups=groups)


def test_optional_highpass_touches_only_the_extra_delta() -> None:
    graph = build_native_dynamic_bass_graph(
        channels=4,
        owner_channels=(0, 2),
        descriptor=_descriptor(delta_highpass_hz=25.0),
    )

    step = next(
        item
        for item in graph.pipeline
        if item.get("names") == ["bass_ext_dynamic_delta_highpass"]
    )
    assert step["channels"] == [4, 5]
    assert graph.mixers["bass_ext_dynamic_reduce"]["channels"]["out"] == 4


def _base_graph() -> dict:
    return {
        "devices": {"playback": {"channels": 4}},
        "filters": {
            "woofer_lowpass": {"type": "Biquad"},
            "woofer_limiter": {"type": "Limiter"},
            "tweeter_highpass": {"type": "Biquad"},
            "tweeter_limiter": {"type": "Limiter"},
        },
        "mixers": {},
        "pipeline": [
            {
                "type": "Filter",
                "channels": [0, 2],
                "names": ["woofer_lowpass", "woofer_limiter"],
            },
            {
                "type": "Filter",
                "channels": [1, 3],
                "names": ["tweeter_highpass", "tweeter_limiter"],
            },
        ],
    }


@pytest.mark.parametrize("descriptor", [_descriptor(), _shaped()], ids=["shelf", "shaped"])
@pytest.mark.parametrize("groups", [(), ((0, 2),), ((2, 0),)])
def test_decorator_is_exactly_reversible_for_static_graph_proof(descriptor, groups) -> None:
    base = _base_graph()

    decorated = apply_dynamic_bass_graph(base, descriptor, (0, 2), groups)

    assert base == _base_graph()
    assert validated_base_graph(decorated, descriptor, (0, 2), groups) == base


def test_projection_refuses_a_changed_native_definition() -> None:
    decorated = apply_dynamic_bass_graph(_base_graph(), _descriptor(), (0, 2))
    decorated["filters"]["bass_ext_dynamic_loudness"]["parameters"]["low_boost"] = 9.0

    with pytest.raises(ValueError, match="do not match"):
        validated_base_graph(decorated, _descriptor(), (0, 2))


def test_projection_refuses_split_step_metadata_drift() -> None:
    decorated = apply_dynamic_bass_graph(_base_graph(), _descriptor(), (0, 2))
    block_end = next(
        index
        for index, step in enumerate(decorated["pipeline"])
        if step == {"type": "Mixer", "name": "bass_ext_dynamic_reduce"}
    )
    decorated["pipeline"][block_end + 1]["bypassed"] = True

    with pytest.raises(ValueError, match="different metadata"):
        validated_base_graph(decorated, _descriptor(), (0, 2))


def test_projection_requires_the_owner_limiter_immediately_after_block() -> None:
    decorated = apply_dynamic_bass_graph(_base_graph(), _descriptor(), (0, 2))
    block_end = next(
        index
        for index, step in enumerate(decorated["pipeline"])
        if step == {"type": "Mixer", "name": "bass_ext_dynamic_reduce"}
    )
    decorated["pipeline"][block_end + 1]["names"].insert(0, "woofer_lowpass")

    with pytest.raises(ValueError, match="immediately before"):
        validated_base_graph(decorated, _descriptor(), (0, 2))


@pytest.mark.parametrize(("changes", "groups", "digest"), [
    ({}, (), "17db5563133976d3d1d9780a53becd3a654766f05a0554e973318da6831a7042"),
    ({"delta_highpass_hz": 25.0}, (), "ecd3dbebd8f6a4aa7056fff24827e7c5e1568cf0dee187e65f31e8b971170152"),
    ({"delta_highpass_hz": 25.0}, ((0, 2),), "92c74dfd2a374c2d4930dd519bdefa9ae60b09110917be697c84ab514774f537"),
])
def test_an_unshaped_descriptor_emits_the_same_bytes_as_before_the_shape(changes, groups, digest) -> None:
    graph = build_native_dynamic_bass_graph(
        channels=4, owner_channels=(0, 2), descriptor=_descriptor(low_boost_db=12.0, **changes), owner_groups=groups,
    )
    assert hashlib.sha256(json.dumps(dataclasses.asdict(graph)).encode()).hexdigest() == digest


def _closed_form_db(freqs: np.ndarray, shape: dict, highpass_hz: float) -> np.ndarray:
    """Analog 1 + HP(T - 1): T the Linkwitz transform, HP a 2nd-order Butterworth high-pass."""
    s = 2j * np.pi * freqs
    w0, wt, wh = (2 * np.pi * value for value in (shape["source_hz"], shape["target_hz"], highpass_hz))
    transform = (s * s + w0 / shape["source_q"] * s + w0 * w0) / (s * s + wt / shape["target_q"] * s + wt * wt)
    highpass = s * s / (s * s + np.sqrt(2.0) * wh * s + wh * wh)
    return 20 * np.log10(np.abs(1 + highpass * (transform - 1)))


@pytest.mark.parametrize("shape", [
    JTS3_SHAPE,
    {"source_hz": 60.0, "source_q": 1.0, "target_hz": 30.0, "target_q": 0.707},
    {"source_hz": 150.0, "source_q": 0.4, "target_hz": 40.0, "target_q": 1.2},
])
@pytest.mark.parametrize("boost_db", [1.0, 12.0, 20.0])
def test_full_boost_is_the_closed_form_linkwitz_transform(shape, boost_db) -> None:
    descriptor = _shaped(low_boost_db=boost_db, linkwitz_transform=shape)
    freqs = np.geomspace(5.0, 20000.0, 2000)
    full = expected_boost_db(descriptor, descriptor.reference_level_db - LOUDNESS_TAPER_DB, freqs)
    # The 48 kHz biquads sit within 0.005 dB of the analog transform across the band.
    assert full == pytest.approx(_closed_form_db(freqs, shape, 15.0), abs=0.01)


def _response(definition: dict, descriptor: DynamicBassDescriptor, fader_db: float, freqs, trig):
    parameters = definition["parameters"]
    kind = parameters.get("type", definition["type"])
    if kind == "Loudness":
        boost = loudness_boost_db(fader_db, descriptor)
        return filter_response_complex(FilterSpec("l", "Lowshelf", NATIVE_LOUDNESS_CORNER_HZ, boost), freqs, trig)
    if kind == "LinkwitzTransform":
        return biquad_response_complex(_linkwitz_coeffs(parameters), trig)
    if kind == "LowshelfFO":
        return biquad_response_complex(_lowshelf_fo_coeffs(parameters), trig)
    if kind == "ButterworthHighpass" and parameters["order"] == 2:
        return filter_response_complex(FilterSpec("h", "Highpass", parameters["freq"], 0.0, SHELF_Q), freqs, trig)
    assert kind in {"Volume", "LinkwitzRileyLowpass"}  # Aux1's silent ramp and the detector reach no output.
    return 1.0


def _fragment_output(graph, descriptor: DynamicBassDescriptor, fader_db: float, freqs: np.ndarray) -> np.ndarray:
    """Owner 0 alone through the emitted fragment, compressors idle, at every output."""
    trig = freq_trig(freqs)
    state = np.zeros((4, len(freqs)), dtype=complex)
    state[0] = 1.0
    for step in graph.pipeline:
        if step["type"] == "Mixer":
            mixer = graph.mixers[step["name"]]
            mixed = np.zeros((mixer["channels"]["out"], len(freqs)), dtype=complex)
            for row in mixer["mapping"]:
                for source in row["sources"]:
                    sign = -1.0 if source["inverted"] else 1.0
                    mixed[row["dest"]] += sign * 10 ** (source["gain"] / 20) * state[source["channel"]]
            state = mixed
        elif step["type"] == "Filter":
            for name in step["names"]:
                state[step["channels"]] *= np.asarray(_response(graph.filters[name], descriptor, fader_db, freqs, trig))
    return state


@pytest.mark.parametrize("descriptor", [_descriptor(delta_highpass_hz=25.0), _shaped()], ids=["shelf", "shaped"])
@pytest.mark.parametrize("groups", [(), ((0, 2),)])
@pytest.mark.parametrize("fader_db", [-40.0, -26.0, -16.0, -8.0, -6.0])
def test_emitted_graph_realizes_the_model_at_every_fader(descriptor, groups, fader_db) -> None:
    graph = build_native_dynamic_bass_graph(channels=4, owner_channels=(0, 2), descriptor=descriptor, owner_groups=groups)
    freqs = np.geomspace(5.0, 20000.0, 400)

    output = _fragment_output(graph, descriptor, fader_db, freqs)

    assert 20 * np.log10(np.abs(output[0])) == pytest.approx(expected_boost_db(descriptor, fader_db, freqs), abs=1e-9)
    assert not np.any(output[1:])


def test_shaped_reserve_bounds_the_boost_and_fades_with_it() -> None:
    descriptor = _shaped()
    freqs = np.geomspace(1.0, 24000.0, 4000).tolist()
    trig = freq_trig(freqs)
    faders = np.arange(-30.0, 1.0, 2.0)
    reserves = [dynamic_bass_gain_reserve_db(descriptor, fader) for fader in faders]

    for fader, reserve in zip(faders, reserves):
        delta = np.abs(_delta_response(descriptor, loudness_boost_db(fader, descriptor), freqs, trig))
        assert reserve >= 20 * np.log10(1 + delta.max())
    assert reserves[0] == dynamic_bass_gain_reserve_db(descriptor)
    assert all(later <= earlier for earlier, later in zip(reserves, reserves[1:])) and reserves[-1] == 0.0
    assert expected_boost_db(descriptor, descriptor.reference_level_db, freqs) == [0.0] * len(freqs)


@pytest.mark.parametrize(("changes", "reason"), [
    ({"delta_highpass_hz": None}, "bass_delta_highpass_hz_invalid"),
    ({"linkwitz_transform": {**JTS3_SHAPE, "target_hz": 90.0}}, "bass_linkwitz_transform_invalid"),
    ({"linkwitz_transform": {**JTS3_SHAPE, "source_q": 0.29}}, "bass_linkwitz_transform_invalid"),
    # 90/1.5 is below 60/0.3: T - 1 has no left-half-plane zero for a LowshelfFO to place.
    ({"linkwitz_transform": {**JTS3_SHAPE, "source_q": 1.5, "target_hz": 60.0, "target_q": 0.3}},
     "bass_linkwitz_transform_invalid"),
    # A 0.05 Hz damping margin puts that zero near 37 kHz.
    ({"linkwitz_transform": {"source_hz": 60.0, "source_q": 1.0, "target_hz": 41.965, "target_q": 0.7}},
     "bass_linkwitz_transform_invalid"),
    ({"linkwitz_transform": {**JTS3_SHAPE, "gain_db": 3.0}}, "bass_linkwitz_transform_invalid"),
    ({"linkwitz_transform": [90.0, 0.6, 22.0, 0.707]}, "bass_linkwitz_transform_invalid"),
    ({"linkwitz_transform": {**JTS3_SHAPE, "source_hz": True}}, "bass_linkwitz_transform_invalid"),
])
def test_a_shape_the_block_cannot_realize_is_refused(changes, reason) -> None:
    raw = {**validate_dynamic_bass_descriptor(dataclasses.asdict(_shaped())), **changes}

    with pytest.raises(DynamicBassDescriptorError) as refused:
        validate_dynamic_bass_descriptor(raw)
    assert refused.value.reason == reason


def test_a_shape_round_trips_and_an_absent_shape_adds_no_key() -> None:
    shaped = validate_dynamic_bass_descriptor(dataclasses.asdict(_shaped()))

    assert shaped["linkwitz_transform"] == JTS3_SHAPE
    assert validate_dynamic_bass_descriptor(shaped) == shaped
    assert "linkwitz_transform" not in validate_dynamic_bass_descriptor(dataclasses.asdict(_descriptor()))
