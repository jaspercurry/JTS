from __future__ import annotations

import pytest

from jasper.bass_extension.dynamic import (
    DynamicBassDescriptor,
    loudness_boost_db,
    maximum_output_gain_db,
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


def test_native_loudness_law_withdraws_over_twenty_db() -> None:
    descriptor = _descriptor()

    assert loudness_boost_db(-30.0, descriptor) == 6.0
    assert loudness_boost_db(-16.0, descriptor) == 3.0
    assert loudness_boost_db(-6.0, descriptor) == 0.0
    assert loudness_boost_db(0.0, descriptor) == 0.0


def test_main_plus_bounded_native_boost_never_exceeds_zero_db() -> None:
    descriptor = _descriptor()

    levels = [value / 10.0 for value in range(-1500, 1)]
    assert max(maximum_output_gain_db(level, descriptor) for level in levels) <= 0.0


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"low_boost_db": 6.01}, "low_boost_db"),
        ({"detector_lowpass_hz": 201.0}, "detector_lowpass_hz"),
        ({"compressor_threshold_dbfs": 0.1}, "compressor_threshold_dbfs"),
        ({"delta_highpass_hz": 90.0}, "delta_highpass_hz"),
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
        "Mixer", "Filter", "Mixer", "Filter", "Processor", "Processor", "Mixer"
    ]
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


def test_decorator_is_exactly_reversible_for_static_graph_proof() -> None:
    base = _base_graph()

    decorated = apply_dynamic_bass_graph(base, _descriptor(), (0, 2))

    assert base == _base_graph()
    assert validated_base_graph(decorated, _descriptor(), (0, 2)) == base


def test_projection_refuses_a_changed_native_definition() -> None:
    decorated = apply_dynamic_bass_graph(_base_graph(), _descriptor(), (0, 2))
    decorated["filters"]["bass_ext_dynamic_loudness"]["parameters"]["low_boost"] = 9.0

    with pytest.raises(ValueError, match="do not match"):
        validated_base_graph(decorated, _descriptor(), (0, 2))
