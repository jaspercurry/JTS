from __future__ import annotations

import numpy as np
import pytest

from jasper.camilla_config_contract import FilterSpec
from jasper.sound.profile import _filter_response_complex
from jasper.bass_extension.delta_composite import (
    COMPOSITE_DIP_TOLERANCE_DB, PROOF_SHELF_HZ, composite_response, modeled_composite_dip,
)

from jasper.bass_extension.dynamic import (
    DynamicBassDescriptor,
    DynamicBassDescriptorError,
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


@pytest.mark.parametrize("boost_db", [6.0, 12.0, 15.0, 20.0])
def test_native_loudness_law_withdraws_over_twenty_db(boost_db: float) -> None:
    descriptor = _descriptor(low_boost_db=boost_db)

    assert loudness_boost_db(-30.0, descriptor) == boost_db
    assert loudness_boost_db(-16.0, descriptor) == boost_db / 2.0
    assert loudness_boost_db(-6.0, descriptor) == 0.0
    assert loudness_boost_db(0.0, descriptor) == 0.0


def _proof_shelf_and_delta(descriptor, frequencies):
    shelf = np.asarray(_filter_response_complex(
        FilterSpec("proof_low", "Lowshelf", PROOF_SHELF_HZ, descriptor.low_boost_db), frequencies,
    ))
    if descriptor.delta_lowpass_hz is not None:
        return shelf, np.asarray(composite_response(
            frequencies, boost_db=descriptor.low_boost_db, highpass_hz=descriptor.delta_highpass_hz,
            lowpass_hz=descriptor.delta_lowpass_hz,
        )) - 1.0
    delta = shelf - 1.0
    graph = build_native_dynamic_bass_graph(channels=2, owner_channels=(0,), descriptor=descriptor)
    for name, definition in graph.filters.items():
        if name.startswith("bass_ext_dynamic_delta_"):
            params = definition["parameters"]
            delta *= _filter_response_complex(FilterSpec(
                name, params["type"].removeprefix("Butterworth"), params["freq"], 0.0, 2.0 ** -0.5,
            ), frequencies)
    return shelf, delta


@pytest.mark.parametrize("boost_db", [0.1, 1.0, 3.0, 6.0, 12.0, 15.0, 18.0, 20.0])
@pytest.mark.parametrize("highpass, lowpass", [(None, None), (63.0, None), (None, 100.0), (63.0, 100.0)])
def test_gain_reserve_covers_native_shelf_delta_phase(boost_db, highpass, lowpass) -> None:
    descriptor = _descriptor(low_boost_db=boost_db, delta_highpass_hz=highpass, delta_lowpass_hz=lowpass)
    frequencies = np.geomspace(0.01, 23000.0, 4096)
    _, delta = _proof_shelf_and_delta(descriptor, frequencies)
    gain_envelope = 1.0 + np.abs(delta)

    assert np.max(gain_envelope) <= 10.0 ** (dynamic_bass_gain_reserve_db(descriptor) / 20.0)


@pytest.mark.parametrize("order, corner, dip_db, dip_hz, tail_db", [
    (1, 100, 1.940336, 175.166, [-0.136783, -1.843275, -1.806674]),
    (1, 125, 1.941253, 14.385, [0.798663, -1.440681, -1.835381]),
    (1, 150, 2.014049, 14.577, [1.548601, -0.951482, -1.725152]),
    (1, 200, 2.109990, 14.824, [2.580712, -0.040584, -1.293317]),
    (2, 100, 7.719724, 119.183, [-7.290052, -2.857323, -1.123046]),
    (2, 125, 7.050439, 135.370, [-5.994108, -4.744229, -1.988013]),
    (2, 150, 6.307370, 150.908, [-2.804634, -5.981328, -3.040096]),
    (2, 200, 5.061270, 180.704, [0.690393, -4.167488, -4.583787]),
])
def test_band_limited_composite_discloses_phase_cancellation(order, corner, dip_db, dip_hz, tail_db) -> None:
    """The noiseless identity |1+D|²=1+|D|²+2|D|cos(arg D) admits zero dB of dip; both orders fail."""
    parameters = {"boost_db": 18.0, "highpass_hz": 63.0, "lowpass_hz": corner, "lowpass_order": order}
    dip = modeled_composite_dip(**parameters)
    tail = 20.0 * np.log10(np.abs(composite_response([125.0, 160.0, 200.0], **parameters)))

    assert dip["modeled_dip_db"] == pytest.approx(dip_db, abs=1e-6)
    assert dip["modeled_dip_hz"] == pytest.approx(dip_hz, abs=0.01)
    assert tail == pytest.approx(tail_db, abs=1e-6)
    assert dip_db > COMPOSITE_DIP_TOLERANCE_DB == 0.0
    if order == 2:
        descriptor = _descriptor(low_boost_db=18.0, delta_highpass_hz=63.0, delta_lowpass_hz=corner)
        with pytest.raises(DynamicBassDescriptorError) as refused:
            descriptor.validate_for_playback()
        assert refused.value.reason == "bass_delta_lowpass_hz_invalid"
        assert refused.value.evidence["modeled_dip_db"] == dip["modeled_dip_db"]
        assert refused.value.evidence["modeled_dip_hz"] == dip["modeled_dip_hz"]


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"low_boost_db": 20.01}, "low_boost_db"),
        ({"detector_lowpass_hz": 201.0}, "detector_lowpass_hz"),
        ({"compressor_threshold_dbfs": 0.1}, "compressor_threshold_dbfs"),
        ({"delta_highpass_hz": 90.0}, "delta_highpass_hz"),
        ({"delta_lowpass_hz": 10.0}, "delta_lowpass_hz"),
        ({"delta_lowpass_hz": 9.0}, "delta_lowpass_hz"),
        ({"delta_highpass_hz": 63.0, "delta_lowpass_hz": 62.0}, "delta_lowpass_hz"),
        ({"delta_highpass_hz": 63.0, "delta_lowpass_hz": 63.0}, "delta_lowpass_hz"),
        ({"delta_lowpass_hz": 200.01}, "delta_lowpass_hz"),
        ({"delta_lowpass_hz": float("nan")}, "delta_lowpass_hz"),
        ({"delta_lowpass_hz": float("inf")}, "delta_lowpass_hz"),
        ({"delta_lowpass_hz": True}, "delta_lowpass_hz"),
        ({"delta_lowpass_hz": "100"}, "delta_lowpass_hz"),
        ({"low_boost_db": "6"}, "low_boost_db"),
        ({"compressor_factor": True}, "compressor_factor"),
    ],
)
def test_descriptor_refuses_values_outside_runtime_bounds(change, field) -> None:
    with pytest.raises(DynamicBassDescriptorError) as refused:
        _descriptor(**change)
    assert refused.value.field == field
    assert refused.value.reason == f"bass_{field}_invalid"


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


def test_each_side_has_an_independent_post_volume_detector() -> None:
    graph = build_native_dynamic_bass_graph(
        channels=4, owner_channels=(0, 2), descriptor=_descriptor()
    )

    left = graph.processors["bass_ext_dynamic_compress_0"]["parameters"]
    right = graph.processors["bass_ext_dynamic_compress_2"]["parameters"]
    assert left["monitor_channels"] == [6]
    assert left["process_channels"] == [4]
    assert right["monitor_channels"] == [7]
    assert right["process_channels"] == [5]
    assert left["makeup_gain"] == right["makeup_gain"] == 0.0


@pytest.mark.parametrize("highpass, lowpass", [(63.0, None), (None, 100.0), (63.0, 100.0)])
def test_optional_filters_touch_only_the_extra_delta(highpass, lowpass) -> None:
    if lowpass is not None:
        with pytest.raises(DynamicBassDescriptorError) as refused:
            build_native_dynamic_bass_graph(channels=4, owner_channels=(0, 2),
                                           descriptor=_descriptor(delta_highpass_hz=highpass, delta_lowpass_hz=lowpass))
        assert refused.value.evidence["constraint"] == "delta_lowpass_unavailable"
        return
    graph = build_native_dynamic_bass_graph(
        channels=4,
        owner_channels=(0, 2),
        descriptor=_descriptor(delta_highpass_hz=highpass, delta_lowpass_hz=lowpass),
    )
    names = []
    for kind, corner in (("Highpass", highpass), ("Lowpass", lowpass)):
        if corner is not None:
            name = f"bass_ext_dynamic_delta_{kind.lower()}"
            names.append(name)
            assert graph.filters[name] == {
                "type": "BiquadCombo",
                "parameters": {"type": f"Butterworth{kind}", "freq": corner, "order": 2},
            }
    assert graph.pipeline[5] == {"type": "Filter", "channels": [4, 5], "names": names}
    assert len(graph.pipeline) == 10
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


@pytest.mark.parametrize("highpass, lowpass", [(None, None), (63.0, None)])
def test_decorator_is_exactly_reversible_for_static_graph_proof(highpass, lowpass) -> None:
    base = _base_graph()
    descriptor = _descriptor(delta_highpass_hz=highpass, delta_lowpass_hz=lowpass)

    decorated = apply_dynamic_bass_graph(base, descriptor, (0, 2))

    assert base == _base_graph()
    assert decorated["devices"] == base["devices"]
    assert decorated["pipeline"][-3:-1] == [
        {"type": "Mixer", "name": "bass_ext_dynamic_reduce"},
        {"type": "Filter", "channels": [0, 2], "names": ["woofer_limiter"]},
    ]
    assert validated_base_graph(decorated, descriptor, (0, 2)) == base


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
