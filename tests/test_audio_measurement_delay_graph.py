# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
import yaml

from jasper.active_speaker.alignment_walk import (
    DRIVER_DELAY_WALK_SCOPE,
    driver_delay_walk_spec,
)
from jasper.audio_measurement.delay_graph import (
    DelayGraphProofError,
    DelayGraphSnapshot,
    confirm_delay_candidate,
)
from jasper.audio_measurement.null_walk import (
    MAX_DSP_DELAY_US,
    DelayCandidate,
    DspPredecessor,
    NullWalkSpec,
)
from jasper.bass_alignment import SUB_MAINS_DELAY_WALK_SCOPE, sub_mains_delay_walk_spec


def _graph(positive_filter: str, negative_filter: str) -> dict:
    # Snapshot shape deliberately resembles CamillaDSP active_raw after YAML
    # normalization/default filling. Both snapshot and candidate confirmation
    # consume parsed live read-back mappings; no emitted-file bytes or SHA are
    # compared with CamillaDSP's re-serialized graph.
    return {
        "devices": {
            "samplerate": 48_000,
            "chunksize": 1024,
            "volume_limit": 0.0,
            "capture": {"type": "Alsa", "device": "capture"},
            "playback": {"type": "Alsa", "device": "playback"},
        },
        "filters": {
            positive_filter: {
                "type": "Delay",
                "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
            },
            negative_filter: {
                "type": "Delay",
                "parameters": {"delay": 0.0, "unit": "ms", "subsample": False},
            },
            "cut_only": {
                "type": "Gain",
                "parameters": {"gain": -3.0, "inverted": False, "mute": False},
            },
        },
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
            {
                "type": "Filter",
                "channels": [0],
                "names": [positive_filter, negative_filter, "cut_only"],
            },
        ],
    }


def _active_snapshot() -> tuple[DelayGraphSnapshot, NullWalkSpec]:
    spec = driver_delay_walk_spec(
        crossover_fc_hz=5000.0,
        positive_delay_target_role="upper",
        negative_delay_target_role="lower",
        signed_acoustic_path_difference_m=0.0,
    )
    return (
        DelayGraphSnapshot(
            spec,
            scope=DRIVER_DELAY_WALK_SCOPE,
            topology_id="active-topology",
            positive_delay_filter="as_upper_delay",
            negative_delay_filter="as_lower_delay",
            predecessor=DspPredecessor(
                {
                    "config_path": "/configs/active-entry.yml",
                    "active_raw": _graph("as_upper_delay", "as_lower_delay"),
                }
            ),
        ),
        spec,
    )


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
def test_named_consumers_share_exact_delay_only_readback_proof(consumer: str):
    if consumer == "active_crossover":
        snapshot, spec = _active_snapshot()
    else:
        spec = sub_mains_delay_walk_spec(
            corner_hz=5000.0,
            sub_path_minus_mains_m=0.0,
        )
        snapshot = DelayGraphSnapshot(
            spec,
            scope=SUB_MAINS_DELAY_WALK_SCOPE,
            topology_id="bass-topology",
            positive_delay_filter="bass_mains_delay",
            negative_delay_filter="bass_subwoofer_delay",
            predecessor=DspPredecessor(
                {
                    "config_path": "/configs/bass-entry.yml",
                    "active_raw": _graph("bass_mains_delay", "bass_subwoofer_delay"),
                }
            ),
        )

    candidate = spec.dsp_candidate(100.0)
    readback = snapshot.graph
    readback["filters"][snapshot.positive_delay_filter]["parameters"]["delay"] = 0.1
    # CamillaDSP may reorder YAML keys while returning the same normalized
    # object. The proof is semantic over parsed active_raw, not emitted text.
    normalized_readback = yaml.safe_load(yaml.safe_dump(readback, sort_keys=True))

    confirmation = _confirm(snapshot, candidate, normalized_readback)

    assert confirmation.scope == consumer
    assert confirmation.snapshot_fingerprint == snapshot.fingerprint
    assert confirmation.predecessor_fingerprint == snapshot.predecessor_fingerprint
    assert confirmation.predecessor_graph_fingerprint == snapshot.graph_fingerprint
    assert confirmation.readback_graph_fingerprint != snapshot.graph_fingerprint
    assert confirmation.delay_target == candidate.delay_target
    assert confirmation.delay_filter == snapshot.positive_delay_filter
    assert confirmation.delay_us == 100.0
    assert confirmation.effective_delay_us == 100.0
    assert len(confirmation.candidate_fingerprint) == 64
    assert confirmation.to_dict()["topology_id"] == snapshot.topology_id


def test_snapshot_reuses_the_exact_f1_restore_predecessor_identity():
    spec = driver_delay_walk_spec(
        crossover_fc_hz=5000.0,
        positive_delay_target_role="upper",
        negative_delay_target_role="lower",
        signed_acoustic_path_difference_m=0.0,
    )
    predecessor = DspPredecessor(
        {
            "config_path": "/configs/entry.yml",
            "active_raw": _graph("as_upper_delay", "as_lower_delay"),
        }
    )

    snapshot = DelayGraphSnapshot(
        spec,
        scope=DRIVER_DELAY_WALK_SCOPE,
        topology_id="active-topology",
        positive_delay_filter="as_upper_delay",
        negative_delay_filter="as_lower_delay",
        predecessor=predecessor,
    )

    assert snapshot.predecessor_fingerprint == predecessor.fingerprint
    assert snapshot.graph == predecessor.state["active_raw"]
    assert snapshot.fingerprint != predecessor.fingerprint


def test_readback_refuses_any_non_delay_graph_change():
    snapshot, spec = _active_snapshot()
    candidate = spec.dsp_candidate(100.0)
    readback = snapshot.graph
    readback["filters"]["as_upper_delay"]["parameters"]["delay"] = 0.1
    readback["devices"]["chunksize"] = 2048

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, readback)

    assert caught.value.code == "graph_mismatch"


def test_readback_exactness_is_json_type_sensitive():
    snapshot, spec = _active_snapshot()
    candidate = spec.dsp_candidate(100.0)
    readback = snapshot.graph
    readback["filters"]["as_upper_delay"]["parameters"]["delay"] = 0.1
    # bool compares equal to integer zero in Python, but it is a different JSON
    # value and cannot pass an exact graph proof.
    readback["pipeline"][1]["channels"][0] = False

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, readback)

    assert caught.value.code == "graph_mismatch"


def test_negative_candidate_changes_only_the_negative_target_filter():
    snapshot, spec = _active_snapshot()
    candidate = spec.dsp_candidate(-100.0)
    readback = snapshot.graph
    readback["filters"]["as_lower_delay"]["parameters"]["delay"] = 0.1

    confirmation = _confirm(snapshot, candidate, readback)

    assert confirmation.delay_target == "lower"
    assert confirmation.delay_filter == "as_lower_delay"
    assert confirmation.delay_us == 100.0


def test_zero_candidate_requires_the_exact_unchanged_predecessor():
    snapshot, spec = _active_snapshot()

    confirmation = _confirm(snapshot, spec.dsp_candidate(0.0), snapshot.graph)

    assert confirmation.delay_target is None
    assert confirmation.delay_filter is None
    assert confirmation.delay_us == 0.0


def test_confirmation_uses_the_shared_camilla_delay_quantization():
    spec = NullWalkSpec(
        crossover_fc_hz=5000.0,
        geometry_seed_us=123.456,
        positive_delay_target="upper",
        negative_delay_target="lower",
    )
    snapshot = DelayGraphSnapshot(
        spec,
        scope=DRIVER_DELAY_WALK_SCOPE,
        topology_id="active-topology",
        positive_delay_filter="as_upper_delay",
        negative_delay_filter="as_lower_delay",
        predecessor=DspPredecessor(
            {
                "config_path": "/configs/active-entry.yml",
                "active_raw": _graph("as_upper_delay", "as_lower_delay"),
            }
        ),
    )
    candidate = spec.dsp_candidate(123.456)
    readback = snapshot.graph
    readback["filters"]["as_upper_delay"]["parameters"]["delay"] = 0.1235

    confirmation = _confirm(snapshot, candidate, readback)

    assert confirmation.delay_us == 123.456
    assert confirmation.effective_delay_us == pytest.approx(123.5)


def test_readback_refuses_change_to_the_other_delay_slot():
    snapshot, spec = _active_snapshot()
    candidate = spec.dsp_candidate(100.0)
    readback = snapshot.graph
    readback["filters"]["as_upper_delay"]["parameters"]["delay"] = 0.1
    readback["filters"]["as_lower_delay"]["parameters"]["delay"] = 0.2

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, readback)

    assert caught.value.code == "graph_mismatch"


def test_readback_refuses_wrong_requested_delay():
    snapshot, spec = _active_snapshot()
    candidate = spec.dsp_candidate(100.0)
    readback = snapshot.graph
    readback["filters"]["as_upper_delay"]["parameters"]["delay"] = 0.2

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, readback)

    assert caught.value.code == "delay_mismatch"


@pytest.mark.parametrize("gain_owner", ["filter", "mixer"])
def test_readback_refuses_positive_gain(gain_owner: str):
    snapshot, spec = _active_snapshot()
    candidate = spec.dsp_candidate(100.0)
    readback = snapshot.graph
    readback["filters"]["as_upper_delay"]["parameters"]["delay"] = 0.1
    if gain_owner == "filter":
        readback["filters"]["cut_only"]["parameters"]["gain"] = 0.1
    else:
        readback["mixers"]["route"]["mapping"][0]["sources"][0]["gain"] = 0.1

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, readback)

    assert caught.value.code == "positive_gain_refused"


@pytest.mark.parametrize(
    ("limit", "code"),
    [
        (None, "volume_limit_invalid"),
        (0.1, "volume_limit_invalid"),
        (float("nan"), "readback_invalid"),
        ("loud", "volume_limit_invalid"),
    ],
)
def test_readback_requires_nonpositive_finite_volume_limit(limit, code):
    snapshot, spec = _active_snapshot()
    candidate = spec.dsp_candidate(100.0)
    readback = snapshot.graph
    readback["filters"]["as_upper_delay"]["parameters"]["delay"] = 0.1
    if limit is None:
        del readback["devices"]["volume_limit"]
    else:
        readback["devices"]["volume_limit"] = limit

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, readback)

    assert caught.value.code == code


@pytest.mark.parametrize(
    ("override", "value", "code"),
    [
        ("expected_snapshot_fingerprint", "stale", "snapshot_fingerprint_mismatch"),
        ("expected_scope", "bass_management", "scope_mismatch"),
        ("expected_topology_id", "other-topology", "topology_mismatch"),
        ("expected_crossover_fc_hz", 4000.0, "crossover_mismatch"),
    ],
)
def test_confirmation_refuses_stale_bound_context(override: str, value, code: str):
    snapshot, spec = _active_snapshot()
    candidate = spec.dsp_candidate(100.0)
    readback = snapshot.graph
    readback["filters"]["as_upper_delay"]["parameters"]["delay"] = 0.1

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, readback, **{override: value})

    assert caught.value.code == code


@pytest.mark.parametrize("readback", [None, [], {}, {1: "ambiguous"}])
def test_confirmation_refuses_missing_or_noncanonical_live_readback(readback):
    snapshot, spec = _active_snapshot()

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, spec.dsp_candidate(100.0), readback)

    assert caught.value.code == "readback_invalid"


@pytest.mark.parametrize(
    "candidate",
    [
        DelayCandidate(100.0, "upper", "lower", "upper", -100.0),
        DelayCandidate(100.0, "upper", "lower", "lower", 100.0),
        DelayCandidate(
            MAX_DSP_DELAY_US + 1.0,
            "upper",
            "lower",
            "upper",
            MAX_DSP_DELAY_US + 1.0,
        ),
        DelayCandidate(100.0, "other", "lower", "other", 100.0),
    ],
)
def test_confirmation_refuses_incoherent_or_unbounded_candidate(candidate):
    snapshot, _spec = _active_snapshot()

    with pytest.raises(DelayGraphProofError) as caught:
        _confirm(snapshot, candidate, snapshot.graph)

    assert caught.value.code == "candidate_invalid"


@pytest.mark.parametrize("hazard", ["positive_gain", "volume_limit", "delay_filter"])
def test_snapshot_refuses_unsafe_predecessor(hazard: str):
    graph = _graph("as_upper_delay", "as_lower_delay")
    if hazard == "positive_gain":
        graph["filters"]["cut_only"]["parameters"]["gain"] = 1.0
    elif hazard == "volume_limit":
        graph["devices"]["volume_limit"] = 1.0
    else:
        graph["filters"]["as_upper_delay"]["type"] = "Gain"

    with pytest.raises(DelayGraphProofError):
        DelayGraphSnapshot(
            driver_delay_walk_spec(
                crossover_fc_hz=5000.0,
                positive_delay_target_role="upper",
                negative_delay_target_role="lower",
                signed_acoustic_path_difference_m=0.0,
            ),
            scope=DRIVER_DELAY_WALK_SCOPE,
            topology_id="active-topology",
            positive_delay_filter="as_upper_delay",
            negative_delay_filter="as_lower_delay",
            predecessor=DspPredecessor(
                {
                    "config_path": "/configs/active-entry.yml",
                    "active_raw": graph,
                }
            ),
        )
