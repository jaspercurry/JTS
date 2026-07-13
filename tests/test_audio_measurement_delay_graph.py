# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy

import pytest
import yaml

from jasper.active_speaker.alignment_walk import (
    DRIVER_DELAY_WALK_SCOPE,
    driver_delay_walk_spec,
)
from jasper.audio_measurement.delay_graph import (
    DelayGraphProofError,
    DelayGraphSnapshot,
    DelayLaneBinding,
    confirm_delay_candidate,
)
from jasper.audio_measurement.null_walk import (
    MAX_DSP_DELAY_US,
    DelayCandidate,
    DspPredecessor,
    NullWalkSpec,
)
from jasper.bass_alignment import SUB_MAINS_DELAY_WALK_SCOPE, sub_mains_delay_walk_spec

POSITIVE_IDENTITY_FILTER = "positive_lane_identity"
NEGATIVE_IDENTITY_FILTER = "negative_lane_identity"


def _graph(
    positive_filter: str,
    negative_filter: str,
    *,
    compensated_peq: bool = False,
) -> dict:
    filters = {
        positive_filter: {
            "type": "Delay",
            "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
        },
        negative_filter: {
            "type": "Delay",
            "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
        },
        POSITIVE_IDENTITY_FILTER: {
            "type": "BiquadCombo",
            "parameters": {"type": "LinkwitzRileyHighpass", "freq": 5000.0},
        },
        NEGATIVE_IDENTITY_FILTER: {
            "type": "BiquadCombo",
            "parameters": {"type": "LinkwitzRileyLowpass", "freq": 5000.0},
        },
        "cut_only": {
            "type": "Gain",
            "parameters": {
                "gain": -6.0 if compensated_peq else -3.0,
                "inverted": False,
                "mute": False,
            },
        },
    }
    positive_names = [POSITIVE_IDENTITY_FILTER, positive_filter, "cut_only"]
    if compensated_peq:
        filters["compensated_peq"] = {
            "type": "Biquad",
            "parameters": {
                "type": "Peaking",
                "freq": 900.0,
                "q": 1.0,
                "gain": 4.0,
            },
        }
        positive_names.append("compensated_peq")
    return {
        "devices": {
            "samplerate": 48_000,
            "chunksize": 1024,
            "volume_limit": 0.0,
            "capture": {"type": "Alsa", "device": "capture"},
            "playback": {"type": "Alsa", "device": "playback"},
        },
        "filters": filters,
        "mixers": {
            "route": {
                "channels": {"in": 2, "out": 2},
                "mapping": [
                    {
                        "dest": 0,
                        "sources": [{"channel": 0, "gain": 0.0, "inverted": False}],
                    }
                ],
            }
        },
        "pipeline": [
            {"type": "Mixer", "name": "route"},
            {"type": "Filter", "channels": [0], "names": positive_names},
            {
                "type": "Filter",
                "channels": [1],
                "names": [NEGATIVE_IDENTITY_FILTER, negative_filter],
            },
        ],
    }


def _active_spec() -> NullWalkSpec:
    return driver_delay_walk_spec(
        crossover_fc_hz=5000.0,
        positive_delay_target_role="upper",
        negative_delay_target_role="lower",
        signed_acoustic_path_difference_m=0.0,
    )


def _snapshot(
    spec: NullWalkSpec | None = None,
    *,
    scope=DRIVER_DELAY_WALK_SCOPE,
    topology_id: str = "active-topology",
    graph: dict | None = None,
    positive_lane: DelayLaneBinding | None = None,
    negative_lane: DelayLaneBinding | None = None,
) -> DelayGraphSnapshot:
    spec = spec or _active_spec()
    positive_filter = "as_positive_delay"
    negative_filter = "as_negative_delay"
    return DelayGraphSnapshot(
        spec,
        scope=scope,
        topology_id=topology_id,
        positive_lane=positive_lane
        or DelayLaneBinding(
            spec.positive_delay_target,
            positive_filter,
            POSITIVE_IDENTITY_FILTER,
            0,
        ),
        negative_lane=negative_lane
        or DelayLaneBinding(
            spec.negative_delay_target,
            negative_filter,
            NEGATIVE_IDENTITY_FILTER,
            1,
        ),
        predecessor=DspPredecessor(
            {
                "config_path": "/configs/entry.yml",
                "active_raw": graph or _graph(positive_filter, negative_filter),
            }
        ),
    )


def _readback(
    snapshot: DelayGraphSnapshot,
    *,
    positive_ms: float = 0.0,
    negative_ms: float = 0.0,
) -> dict:
    graph = snapshot.graph
    graph["filters"][snapshot.positive_lane.filter_name]["parameters"]["delay"] = (
        positive_ms
    )
    graph["filters"][snapshot.negative_lane.filter_name]["parameters"]["delay"] = (
        negative_ms
    )
    return graph


def _confirm(
    snapshot: DelayGraphSnapshot,
    candidate: DelayCandidate,
    readback,
    **overrides,
):
    kwargs = {
        "expected_snapshot_fingerprint": snapshot.fingerprint,
        "expected_scope": snapshot.scope,
        "expected_topology_id": snapshot.topology_id,
        "expected_crossover_fc_hz": snapshot.crossover_fc_hz,
    }
    kwargs.update(overrides)
    return confirm_delay_candidate(snapshot, candidate, readback, **kwargs)


@pytest.mark.parametrize("consumer", ["active_crossover", "bass_management"])
def test_named_consumers_share_typed_lane_content_proof(consumer: str):
    if consumer == "active_crossover":
        spec = _active_spec()
        snapshot = _snapshot(spec)
    else:
        spec = sub_mains_delay_walk_spec(
            corner_hz=5000.0,
            sub_path_minus_mains_m=0.0,
        )
        snapshot = _snapshot(
            spec,
            scope=SUB_MAINS_DELAY_WALK_SCOPE,
            topology_id="bass-topology",
            graph=_graph("bass_positive_delay", "bass_negative_delay"),
            positive_lane=DelayLaneBinding(
                spec.positive_delay_target,
                "bass_positive_delay",
                POSITIVE_IDENTITY_FILTER,
                0,
            ),
            negative_lane=DelayLaneBinding(
                spec.negative_delay_target,
                "bass_negative_delay",
                NEGATIVE_IDENTITY_FILTER,
                1,
            ),
        )

    candidate = spec.dsp_candidate(100.0)
    normalized = yaml.safe_load(
        yaml.safe_dump(_readback(snapshot, positive_ms=0.1), sort_keys=True)
    )
    confirmation = _confirm(snapshot, candidate, normalized)

    assert confirmation.scope == consumer
    assert confirmation.readback_relative_delay_us == pytest.approx(100.0)
    assert confirmation.delay_filter == snapshot.positive_lane.filter_name
    assert confirmation.effective_delay_us == pytest.approx(100.0)
    assert confirmation.to_dict()["schema_version"] == 2


def test_snapshot_reuses_exact_f1_predecessor_and_binds_lane_proof():
    spec = _active_spec()
    graph = _graph("as_positive_delay", "as_negative_delay")
    predecessor = DspPredecessor(
        {"config_path": "/configs/entry.yml", "active_raw": graph}
    )
    snapshot = DelayGraphSnapshot(
        spec,
        scope=DRIVER_DELAY_WALK_SCOPE,
        topology_id="active-topology",
        positive_lane=DelayLaneBinding(
            "upper", "as_positive_delay", POSITIVE_IDENTITY_FILTER, 0
        ),
        negative_lane=DelayLaneBinding(
            "lower", "as_negative_delay", NEGATIVE_IDENTITY_FILTER, 1
        ),
        predecessor=predecessor,
    )

    assert snapshot.predecessor_fingerprint == predecessor.fingerprint
    assert snapshot.graph == predecessor.state["active_raw"]
    assert snapshot.fingerprint != predecessor.fingerprint


def test_positive_only_candidate_derives_signed_relative_delay():
    snapshot = _snapshot()
    confirmation = _confirm(
        snapshot,
        snapshot._spec.dsp_candidate(100.0),
        _readback(snapshot, positive_ms=0.1),
    )

    assert confirmation.readback_relative_delay_us == pytest.approx(100.0)
    assert confirmation.delay_target == "upper"


def test_negative_only_candidate_derives_signed_relative_delay():
    snapshot = _snapshot()
    confirmation = _confirm(
        snapshot,
        snapshot._spec.dsp_candidate(-100.0),
        _readback(snapshot, negative_ms=0.1),
    )

    assert confirmation.readback_relative_delay_us == pytest.approx(-100.0)
    assert confirmation.delay_target == "lower"


def test_zero_candidate_requires_exact_zero_relative_predecessor():
    snapshot = _snapshot()
    confirmation = _confirm(
        snapshot,
        snapshot._spec.dsp_candidate(0.0),
        snapshot.graph,
    )

    assert confirmation.readback_relative_delay_us == 0.0
    assert confirmation.delay_filter is None
    assert confirmation.readback_graph_fingerprint == snapshot.graph_fingerprint


def test_unequal_common_mode_is_not_admitted_by_signed_difference_alone():
    snapshot = _snapshot()
    candidate = snapshot._spec.dsp_candidate(100.0)

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(
            snapshot,
            candidate,
            _readback(snapshot, positive_ms=0.2, negative_ms=0.1),
        )

    assert caught.value.code == "graph_mismatch"


def test_equal_common_mode_is_not_a_zero_candidate():
    snapshot = _snapshot()

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(
            snapshot,
            snapshot._spec.dsp_candidate(0.0),
            _readback(snapshot, positive_ms=0.1, negative_ms=0.1),
        )

    assert caught.value.code == "graph_mismatch"


def test_wrong_signed_relative_delay_is_refused_before_graph_comparison():
    snapshot = _snapshot()

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(
            snapshot,
            snapshot._spec.dsp_candidate(100.0),
            _readback(snapshot, positive_ms=0.2),
        )

    assert caught.value.code == "delay_mismatch"


def test_camilla_delay_quantization_is_the_effective_relative_coordinate():
    spec = NullWalkSpec(
        crossover_fc_hz=5000.0,
        geometry_seed_us=123.456,
        positive_delay_target="upper",
        negative_delay_target="lower",
    )
    snapshot = _snapshot(spec)
    confirmation = _confirm(
        snapshot,
        spec.dsp_candidate(123.456),
        _readback(snapshot, positive_ms=0.1235),
    )

    assert confirmation.relative_delay_us == 123.456
    assert confirmation.readback_relative_delay_us == pytest.approx(123.5)
    assert confirmation.effective_delay_us == pytest.approx(123.5)


def test_approved_compensated_positive_peq_is_preserved_unchanged():
    graph = _graph(
        "as_positive_delay",
        "as_negative_delay",
        compensated_peq=True,
    )
    snapshot = _snapshot(graph=graph)
    readback = _readback(snapshot, positive_ms=0.1)

    confirmation = _confirm(
        snapshot,
        snapshot._spec.dsp_candidate(100.0),
        readback,
    )

    assert readback["filters"]["compensated_peq"]["parameters"]["gain"] == 4.0
    assert confirmation.delay_us == 100.0


def test_existing_compensated_peq_gain_cannot_change_during_candidate():
    graph = _graph(
        "as_positive_delay",
        "as_negative_delay",
        compensated_peq=True,
    )
    snapshot = _snapshot(graph=graph)
    readback = _readback(snapshot, positive_ms=0.1)
    readback["filters"]["compensated_peq"]["parameters"]["gain"] = 4.1

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, snapshot._spec.dsp_candidate(100.0), readback)

    assert caught.value.code == "graph_mismatch"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda graph: graph["pipeline"][1]["names"].remove("as_positive_delay"),
        lambda graph: graph["pipeline"][1].update({"channels": [0, 1]}),
        lambda graph: graph["pipeline"][1]["names"].append("as_positive_delay"),
        lambda graph: graph["pipeline"].append(
            {"type": "Filter", "channels": [0], "names": ["as_positive_delay"]}
        ),
        lambda graph: graph["pipeline"][1]["names"].remove(POSITIVE_IDENTITY_FILTER),
        lambda graph: graph["pipeline"][2]["names"].append(POSITIVE_IDENTITY_FILTER),
    ],
    ids=[
        "unused",
        "shared",
        "duplicated-name",
        "duplicated-step",
        "identity-unused",
        "identity-wrong-lane",
    ],
)
def test_snapshot_refuses_unproven_or_duplicated_pipeline_lane(mutation):
    graph = _graph("as_positive_delay", "as_negative_delay")
    mutation(graph)

    with pytest.raises(DelayGraphProofError) as caught:
        _snapshot(graph=graph)

    assert caught.value.code == "lane_binding_invalid"


@pytest.mark.parametrize(
    ("positive_lane", "negative_lane"),
    [
        (
            DelayLaneBinding("other", "as_positive_delay", POSITIVE_IDENTITY_FILTER, 0),
            DelayLaneBinding("lower", "as_negative_delay", NEGATIVE_IDENTITY_FILTER, 1),
        ),
        (
            DelayLaneBinding("lower", "as_positive_delay", POSITIVE_IDENTITY_FILTER, 0),
            DelayLaneBinding("upper", "as_negative_delay", NEGATIVE_IDENTITY_FILTER, 1),
        ),
        (
            DelayLaneBinding("upper", "as_positive_delay", POSITIVE_IDENTITY_FILTER, 0),
            DelayLaneBinding("lower", "as_positive_delay", NEGATIVE_IDENTITY_FILTER, 1),
        ),
        (
            DelayLaneBinding("upper", "as_positive_delay", POSITIVE_IDENTITY_FILTER, 0),
            DelayLaneBinding("lower", "as_negative_delay", NEGATIVE_IDENTITY_FILTER, 0),
        ),
        (
            # Both Delay filters use their truthful graph channels. The
            # emitter-owned identity filters keep the semantic targets from
            # being swapped along with them.
            DelayLaneBinding("upper", "as_negative_delay", POSITIVE_IDENTITY_FILTER, 1),
            DelayLaneBinding("lower", "as_positive_delay", NEGATIVE_IDENTITY_FILTER, 0),
        ),
    ],
    ids=[
        "unknown-target",
        "swapped-targets",
        "same-filter",
        "same-lane",
        "swapped-filters",
    ],
)
def test_snapshot_refuses_unknown_shared_same_or_swapped_lane_bindings(
    positive_lane,
    negative_lane,
):
    with pytest.raises(DelayGraphProofError) as caught:
        _snapshot(positive_lane=positive_lane, negative_lane=negative_lane)

    assert caught.value.code == "lane_binding_invalid"


@pytest.mark.parametrize("channel", [True, -1, 0.0, "0"])
def test_typed_lane_binding_rejects_noncanonical_channel(channel):
    with pytest.raises(DelayGraphProofError) as caught:
        DelayLaneBinding("upper", "delay", "identity", channel)

    assert caught.value.code == "lane_binding_invalid"


@pytest.mark.parametrize(
    ("delay_filter", "identity_filter"),
    [("", "identity"), ("delay", ""), ("delay", "delay")],
)
def test_typed_lane_binding_rejects_invalid_identity_contract(
    delay_filter,
    identity_filter,
):
    with pytest.raises(DelayGraphProofError) as caught:
        DelayLaneBinding("upper", delay_filter, identity_filter, 0)

    assert caught.value.code == "lane_binding_invalid"


@pytest.mark.parametrize("identity_type", [None, 7, "", "Delay"])
def test_snapshot_refuses_malformed_or_delay_identity_filter(identity_type):
    graph = _graph("as_positive_delay", "as_negative_delay")
    graph["filters"][POSITIVE_IDENTITY_FILTER] = {"type": identity_type}

    with pytest.raises(DelayGraphProofError) as caught:
        _snapshot(graph=graph)

    assert caught.value.code == "lane_binding_invalid"


@pytest.mark.parametrize("hazard", ["not_delay", "wrong_unit"])
def test_snapshot_refuses_wrong_bound_filter_contract(hazard: str):
    graph = _graph("as_positive_delay", "as_negative_delay")
    if hazard == "not_delay":
        graph["filters"]["as_positive_delay"]["type"] = "Gain"
    else:
        graph["filters"]["as_positive_delay"]["parameters"]["unit"] = "samples"

    with pytest.raises(DelayGraphProofError) as caught:
        _snapshot(graph=graph)

    assert caught.value.code == "delay_filter_invalid"


@pytest.mark.parametrize("slot", ["positive", "negative"])
@pytest.mark.parametrize("value", [0.1, -0.1, "0.0", True])
def test_snapshot_requires_both_delay_slots_at_real_numeric_zero(slot, value):
    graph = _graph("as_positive_delay", "as_negative_delay")
    name = "as_positive_delay" if slot == "positive" else "as_negative_delay"
    graph["filters"][name]["parameters"]["delay"] = value

    with pytest.raises(DelayGraphProofError):
        _snapshot(graph=graph)


@pytest.mark.parametrize("value", [None, 0.1, "0.0", True])
def test_snapshot_requires_real_nonpositive_volume_limit(value):
    graph = _graph("as_positive_delay", "as_negative_delay")
    if value is None:
        del graph["devices"]["volume_limit"]
    else:
        graph["devices"]["volume_limit"] = value

    with pytest.raises(DelayGraphProofError) as caught:
        _snapshot(graph=graph)

    assert caught.value.code == "volume_limit_invalid"


@pytest.mark.parametrize("field", ["volume_limit", "delay"])
def test_readback_rejects_numeric_strings(field: str):
    snapshot = _snapshot()
    readback = _readback(snapshot, positive_ms=0.1)
    if field == "volume_limit":
        readback["devices"]["volume_limit"] = "0.0"
        expected = "volume_limit_invalid"
    else:
        readback["filters"][snapshot.positive_lane.filter_name]["parameters"][
            "delay"
        ] = "0.1"
        expected = "delay_filter_invalid"

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, snapshot._spec.dsp_candidate(100.0), readback)

    assert caught.value.code == expected


def test_any_unrelated_graph_change_is_refused():
    snapshot = _snapshot()
    readback = _readback(snapshot, positive_ms=0.1)
    readback["devices"]["chunksize"] = 2048

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, snapshot._spec.dsp_candidate(100.0), readback)

    assert caught.value.code == "graph_mismatch"


def test_graph_equality_remains_json_type_sensitive():
    snapshot = _snapshot()
    readback = _readback(snapshot, positive_ms=0.1)
    readback["pipeline"][0]["name"] = True

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, snapshot._spec.dsp_candidate(100.0), readback)

    assert caught.value.code == "graph_mismatch"


@pytest.mark.parametrize(
    ("override", "value", "code"),
    [
        ("expected_snapshot_fingerprint", "stale", "snapshot_fingerprint_mismatch"),
        ("expected_scope", "bass_management", "scope_mismatch"),
        ("expected_topology_id", "other-topology", "topology_mismatch"),
        ("expected_crossover_fc_hz", 4000.0, "crossover_mismatch"),
        ("expected_crossover_fc_hz", "5000", "crossover_mismatch"),
    ],
)
def test_confirmation_refuses_wrong_bound_context(override, value, code):
    snapshot = _snapshot()
    kwargs = {override: value}

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(
            snapshot,
            snapshot._spec.dsp_candidate(100.0),
            _readback(snapshot, positive_ms=0.1),
            **kwargs,
        )

    assert caught.value.code == code


@pytest.mark.parametrize("readback", [None, [], {}, {1: "ambiguous"}])
def test_confirmation_refuses_noncanonical_graph_content(readback):
    snapshot = _snapshot()

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, snapshot._spec.dsp_candidate(100.0), readback)

    assert caught.value.code == "readback_invalid"


@pytest.mark.parametrize(
    "candidate",
    [
        DelayCandidate("100", "upper", "lower", "upper", 100.0),
        DelayCandidate(100.0, "upper", "lower", "upper", -100.0),
        DelayCandidate(100.0, "upper", "lower", "lower", 100.0),
        DelayCandidate(
            MAX_DSP_DELAY_US + 1.0,
            "upper",
            "lower",
            "upper",
            MAX_DSP_DELAY_US + 1.0,
        ),
    ],
)
def test_confirmation_refuses_noncanonical_or_incoherent_candidate(candidate):
    snapshot = _snapshot()

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, copy.deepcopy(snapshot.graph))

    assert caught.value.code == "candidate_invalid"
