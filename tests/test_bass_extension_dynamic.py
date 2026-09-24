from __future__ import annotations

import json

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import camilla_filter_response
from jasper.active_speaker.graph_transfer import mixer_mapping
from jasper.biquad import biquad_response_complex, freq_trig

from jasper.bass_extension.dynamic import (
    DynamicBassDescriptor,
    _delta_response,
    _linkwitz_coeffs,
    boost_biquad,
    dynamic_bass_gain_reserve_db,
    expected_boost_db,
    validate_dynamic_bass_descriptor,
)
from jasper.bass_extension.dynamic_graph import (
    apply_dynamic_bass_graph,
    build_native_dynamic_bass_graph,
    validated_base_graph,
)

# jts3's tune B (#5710): the woofer pair's alignment moved to a 30 Hz target.
JTS3_SHAPE = {"source_hz": 107.0, "source_q": 0.44, "target_hz": 30.0, "target_q": 0.707}
# ADR-0352 sections, as stored candidates carry them.
OLD_PLAIN = {"low_boost_db": 6, "reference_level_db": -6, "detector_lowpass_hz": 90, "compressor_threshold_dbfs": -8}
OLD_SHAPED = {"low_boost_db": 20, "reference_level_db": 0, "detector_lowpass_hz": 100, "compressor_threshold_dbfs": -18,
              "delta_highpass_hz": 22, "linkwitz_transform": JTS3_SHAPE}


def _descriptor(**changes) -> DynamicBassDescriptor:
    values = {"linkwitz_transform": JTS3_SHAPE, "delta_highpass_hz": 22.0,
              "detector_lowpass_hz": 100.0, "compressor_threshold_dbfs": -9.0}
    return DynamicBassDescriptor(**{**values, **changes})


DESCRIPTORS = {
    "new": _descriptor(),
    "old_plain": DynamicBassDescriptor(**OLD_PLAIN),
    "old_plain_highpass": DynamicBassDescriptor(**{**OLD_PLAIN, "delta_highpass_hz": 25.0}),
}


@pytest.mark.parametrize(("changes", "reason"), [
    ({"detector_lowpass_hz": 201.0}, "bass_detector_lowpass_hz_invalid"),
    ({"compressor_threshold_dbfs": 0.1}, "bass_compressor_threshold_dbfs_invalid"),
    ({"compressor_factor": True}, "bass_compressor_factor_invalid"),
    ({"delta_highpass_hz": 100.0}, "bass_delta_highpass_hz_invalid"),
    ({"delta_highpass_hz": None}, "bass_delta_highpass_hz_invalid"),
    ({"linkwitz_transform": None}, "bass_linkwitz_transform_invalid"),
    ({"linkwitz_transform": {**JTS3_SHAPE, "target_hz": 107.0}}, "bass_linkwitz_transform_invalid"),
    ({"linkwitz_transform": {**JTS3_SHAPE, "source_q": 0.29}}, "bass_linkwitz_transform_invalid"),
    ({"linkwitz_transform": {**JTS3_SHAPE, "gain_db": 3.0}}, "bass_linkwitz_transform_invalid"),
    ({"linkwitz_transform": [107.0, 0.44, 30.0, 0.707]}, "bass_linkwitz_transform_invalid"),
    ({"duplicate_limit": 1}, "bass_descriptor_malformed"),
    ({"low_boost_db": None, "reference_level_db": None}, "bass_descriptor_malformed"),
    ({"low_boost_db": 6.0}, "bass_descriptor_malformed"),
    ({"low_boost_db": 20.01, "reference_level_db": 0.0}, "bass_low_boost_db_invalid"),
    ({"low_boost_db": "6", "reference_level_db": 0.0}, "bass_low_boost_db_invalid"),
])
def test_descriptor_refuses_a_section_outside_its_bounds(changes, reason) -> None:
    with pytest.raises(ValueError) as refused:
        validate_dynamic_bass_descriptor({**_descriptor().payload(), **changes})
    assert refused.value.reason == reason


@pytest.mark.parametrize(("raw", "canonical", "biquad"), [
    (OLD_PLAIN, '{"compressor_attack_s":0.01,"compressor_factor":10.0,"compressor_release_s":0.25,'
                '"compressor_threshold_dbfs":-8.0,"delta_highpass_hz":null,"detector_lowpass_hz":90.0,'
                '"low_boost_db":6.0,"reference_level_db":-6.0}',
     {"type": "Lowshelf", "freq": 70.0, "q": 0.7071068, "gain": 6.0}),
    (OLD_SHAPED, '{"compressor_attack_s":0.01,"compressor_factor":10.0,"compressor_release_s":0.25,'
                 '"compressor_threshold_dbfs":-18.0,"delta_highpass_hz":22.0,"detector_lowpass_hz":100.0,'
                 '"linkwitz_transform":{"source_hz":107.0,"source_q":0.44,"target_hz":30.0,"target_q":0.707},'
                 '"low_boost_db":20.0,"reference_level_db":0.0}',
     boost_biquad(_descriptor())),
])
def test_an_old_section_keeps_its_payload_bytes_and_plays_its_full_boost(raw, canonical, biquad) -> None:
    payload = validate_dynamic_bass_descriptor(raw)

    # Banked candidate fingerprints hash these bytes.
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == canonical
    assert validate_dynamic_bass_descriptor(payload) == payload
    assert boost_biquad(DynamicBassDescriptor(**payload)) == biquad
    for section in (raw, {**raw, "low_boost_db": 25}, {**raw, "reference_level_db": float("nan")}):
        with pytest.raises(ValueError) as refused:
            validate_dynamic_bass_descriptor(section, new_section=True)
        assert refused.value.reason == "bass_descriptor_malformed"


def _response(definition: dict, freqs: np.ndarray, trig):
    parameters = definition["parameters"]
    if parameters["type"] == "LinkwitzTransform":
        return np.asarray(biquad_response_complex(_linkwitz_coeffs(parameters), trig))
    return camilla_filter_response([definition], freqs)


def _fragment_transfer(graph, freqs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Input-to-output transfer of the emitted fragment with its compressors idle, and the detector lanes' input."""
    trig = freq_trig(freqs)
    state = np.stack([np.eye(4, dtype=complex)] * len(freqs), axis=-1)
    detector = None
    for step in graph.pipeline:
        if step["type"] == "Mixer":
            width, mapping = mixer_mapping(graph.mixers[step["name"]], len(state), step["name"])
            mixed = np.zeros((width, *state.shape[1:]), dtype=complex)
            for dest, sources in mapping:
                for source, gain in sources:
                    mixed[dest] += gain * state[source]
            state = mixed
        elif step["type"] == "Filter":
            for name in step["names"]:
                if graph.filters[name]["parameters"]["type"] == "LinkwitzRileyLowpass":
                    detector = state[step["channels"]].copy()
                    continue  # The detector reaches no output.
                state[step["channels"]] *= _response(graph.filters[name], freqs, trig)
    return state, detector


@pytest.mark.parametrize("name", DESCRIPTORS)
@pytest.mark.parametrize("groups", [(), ((0, 2),)])
def test_the_emitted_block_plays_the_model_at_every_volume(name, groups) -> None:
    descriptor = DESCRIPTORS[name]
    graph = build_native_dynamic_bass_graph(channels=4, owner_channels=(0, 2), descriptor=descriptor, owner_groups=groups)
    freqs = np.geomspace(5.0, 20000.0, 400)

    transfer, detector = _fragment_transfer(graph, freqs)

    assert set(graph.mixers) == {"bass_ext_dynamic_expand", "bass_ext_dynamic_form_delta", "bass_ext_dynamic_reduce"}
    assert graph.filters["bass_ext_dynamic_boost"] == {"type": "Biquad", "parameters": boost_biquad(descriptor)}
    assert all(item["type"] in {"Biquad", "BiquadCombo"} for item in graph.filters.values())
    boost = expected_boost_db(descriptor, freqs)
    for owner in (0, 2):
        assert 20 * np.log10(np.abs(transfer[owner, owner])) == pytest.approx(boost, abs=1e-9)
    for other in (1, 3):
        assert np.all(transfer[other, other] == 1.0)
    off_diagonal = ~np.eye(4, dtype=bool)
    assert not np.any(transfer[off_diagonal])
    # Each detector reads its group's boosted front copy, before the delta is formed.
    fronts = [group[0] for group in groups] or [0, 2]
    trig = freq_trig(freqs)
    boosted = _response(graph.filters["bass_ext_dynamic_boost"], freqs, trig)
    for lane, front in enumerate(fronts):
        assert detector[lane, front] == pytest.approx(boosted, abs=1e-12)


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
    for front, (monitor, process) in expected.items():
        compressor = graph.processors[f"bass_ext_dynamic_compress_{front}"]
        assert compressor["type"] == "Compressor"
        assert compressor["parameters"]["monitor_channels"] == monitor
        assert compressor["parameters"]["process_channels"] == process
        assert compressor["parameters"]["threshold"] == -9.0
        assert compressor["parameters"]["makeup_gain"] == 0.0


@pytest.mark.parametrize("groups", [((0,),), ((0, 2), (2,)), ((0, 1),), ((), (0, 2))])
def test_owner_groups_must_cover_each_owner_once(groups):
    with pytest.raises(ValueError):
        build_native_dynamic_bass_graph(channels=4, owner_channels=(0, 2), descriptor=_descriptor(), owner_groups=groups)


@pytest.mark.parametrize("shape", [
    JTS3_SHAPE,
    {"source_hz": 60.0, "source_q": 1.0, "target_hz": 30.0, "target_q": 0.707},
    {"source_hz": 150.0, "source_q": 0.4, "target_hz": 40.0, "target_q": 1.2},
    # T - 1 has a right-half-plane zero here; the native transform plays it like any other.
    {"source_hz": 60.0, "source_q": 1.5, "target_hz": 30.0, "target_q": 0.3},
])
@pytest.mark.parametrize("highpass_hz", [12.0, 22.0])
def test_the_boost_is_the_closed_form_linkwitz_transform(shape, highpass_hz) -> None:
    freqs = np.geomspace(5.0, 20000.0, 2000)
    s = 2j * np.pi * freqs
    w0, wt, wh = (2 * np.pi * value for value in (shape["source_hz"], shape["target_hz"], highpass_hz))
    transform = (s * s + w0 / shape["source_q"] * s + w0 * w0) / (s * s + wt / shape["target_q"] * s + wt * wt)
    highpass = s * s / (s * s + np.sqrt(2.0) * wh * s + wh * wh)

    boost = expected_boost_db(_descriptor(linkwitz_transform=shape, delta_highpass_hz=highpass_hz), freqs)

    # The 48 kHz biquads sit within 0.005 dB of the analog transform across the band.
    assert boost == pytest.approx(20 * np.log10(np.abs(1 + highpass * (transform - 1))), abs=0.01)


@pytest.mark.parametrize("name", DESCRIPTORS)
def test_the_reserve_is_the_peak_gain_at_every_compressor_setting(name) -> None:
    delta = np.asarray(_delta_response(DESCRIPTORS[name], np.geomspace(0.01, 23000.0, 20000).tolist()))
    # |1 + g * delta| <= 1 + |delta| for every compressor gain g in [0, 1].
    bound = 20 * np.log10(1 + np.max(np.abs(delta)))

    assert bound <= dynamic_bass_gain_reserve_db(DESCRIPTORS[name]) <= bound + 0.011
    assert dynamic_bass_gain_reserve_db({}) == 0.0


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


@pytest.mark.parametrize("name", DESCRIPTORS)
@pytest.mark.parametrize("groups", [(), ((0, 2),), ((2, 0),)])
def test_decorator_is_exactly_reversible_for_static_graph_proof(name, groups) -> None:
    base = _base_graph()

    decorated = apply_dynamic_bass_graph(base, DESCRIPTORS[name], (0, 2), groups)

    assert base == _base_graph()
    assert validated_base_graph(decorated, DESCRIPTORS[name], (0, 2), groups) == base


def test_projection_refuses_a_changed_native_definition() -> None:
    decorated = apply_dynamic_bass_graph(_base_graph(), _descriptor(), (0, 2))
    decorated["filters"]["bass_ext_dynamic_boost"]["parameters"]["freq_target"] = 25.0

    with pytest.raises(ValueError, match="do not match"):
        validated_base_graph(decorated, _descriptor(), (0, 2))


def _block_end(decorated: dict) -> int:
    return next(index for index, step in enumerate(decorated["pipeline"])
                if step == {"type": "Mixer", "name": "bass_ext_dynamic_reduce"})


def test_projection_refuses_split_step_metadata_drift() -> None:
    decorated = apply_dynamic_bass_graph(_base_graph(), _descriptor(), (0, 2))
    decorated["pipeline"][_block_end(decorated) + 1]["bypassed"] = True

    with pytest.raises(ValueError, match="different metadata"):
        validated_base_graph(decorated, _descriptor(), (0, 2))


def test_projection_requires_the_owner_limiter_immediately_after_block() -> None:
    decorated = apply_dynamic_bass_graph(_base_graph(), _descriptor(), (0, 2))
    decorated["pipeline"][_block_end(decorated) + 1]["names"].insert(0, "woofer_lowpass")

    with pytest.raises(ValueError, match="immediately before"):
        validated_base_graph(decorated, _descriptor(), (0, 2))
