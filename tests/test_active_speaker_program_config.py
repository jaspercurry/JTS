# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R15 declared-protection graph contracts on the real stereo carrier."""

from __future__ import annotations

import copy

import pytest
import yaml

from jasper.active_speaker import ActiveSpeakerPreset
from jasper.active_speaker.protected_neutral_graph import (
    PROTECTED_NEUTRAL_GRAPH_EXCLUSIONS,
    ProtectedNeutralGraphError,
    build_protected_neutral_session_graph,
)
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"
ROLE_CHANNELS = {"woofer": 0, "tweeter": 1}


def _safety_profile(*, tweeter_hp_hz: float = 1600.0) -> dict:
    return {
        "profile_fingerprint": "a" * 64,
        "targets": [
            {
                "role": "woofer",
                "target_id": "speaker:woofer",
                "target_fingerprint": "d" * 64,
                "hard_excitation_band_hz": [500.0, 6000.0],
                "crossover_search_band_hz": [1800.0, 2600.0],
                "required_protection_filters": [
                    {
                        "kind": "highpass",
                        "cutoff_hz": 150.0,
                        "minimum_slope_db_per_octave": 24.0,
                        "family_or_equivalent": "equivalent_or_steeper",
                    },
                    {
                        "kind": "lowpass",
                        "cutoff_hz": 6000.0,
                        "minimum_slope_db_per_octave": 24.0,
                        "family_or_equivalent": "equivalent_or_steeper",
                    },
                ],
            },
            {
                "role": "tweeter",
                "target_id": "speaker:tweeter",
                "target_fingerprint": "e" * 64,
                "hard_excitation_band_hz": [500.0, 23000.0],
                "crossover_search_band_hz": [1900.0, 2800.0],
                "required_protection_filters": [
                    {
                        "kind": "highpass",
                        "cutoff_hz": tweeter_hp_hz,
                        "minimum_slope_db_per_octave": 24.0,
                        "family_or_equivalent": "equivalent_or_steeper",
                    },
                    {
                        "kind": "lowpass",
                        "cutoff_hz": 23000.0,
                        "minimum_slope_db_per_octave": 24.0,
                        "family_or_equivalent": "equivalent_or_steeper",
                    },
                ],
            },
        ],
    }


def _preset(
    layout: str = "mono",
    *,
    configured_inverted: bool = False,
    configured_fc_hz: float = 1600.0,
):
    raw = _two_way_preset(layout)
    raw["crossover_regions"][0]["fc_hz"] = configured_fc_hz
    if configured_inverted:
        raw["crossover_regions"][0]["upper_polarity"] = "inverted"
    return ActiveSpeakerPreset.from_mapping(raw)


def _session_graph(
    preset: ActiveSpeakerPreset | None = None, *, profile: dict | None = None
):
    return build_protected_neutral_session_graph(
        preset=preset or _preset(),
        component_safety_profile=profile or _safety_profile(),
        role_channels=ROLE_CHANNELS,
        playback_device=ACTIVE_PCM,
    )


def test_program_config_uses_the_real_two_lane_carrier() -> None:
    graph = _session_graph()
    parsed = yaml.safe_load(graph.yaml_text)

    assert parsed["devices"]["capture"]["channels"] == 2
    assert parsed["devices"]["volume_limit"] == 0.0
    assert graph.spec.stimulus_peak_ceiling_dbfs == -12.0
    routing = {
        entry["dest"]: [source["channel"] for source in entry["sources"]]
        for entry in parsed["mixers"]["split_active_2way"]["mapping"]
    }
    assert routing == {0: [0], 1: [1]}


def test_declared_required_filter_wins_over_mismatched_hard_floor() -> None:
    graph = _session_graph(profile=_safety_profile(tweeter_hp_hz=5000.0))
    parsed = yaml.safe_load(graph.yaml_text)

    assert parsed["filters"]["as_tweeter_measure_p0_hp"]["parameters"] == {
        "type": "LinkwitzRileyHighpass",
        "freq": 5000.0,
        "order": 4,
    }
    assert graph.spec.role("tweeter").filters[0].cutoff_hz == 5000.0
    assert graph.protection_by_role["tweeter"][0].frequency_hz == 5000.0


def test_declared_floor_jts_shape_is_emitted_exactly() -> None:
    graph = _session_graph(profile=_safety_profile(tweeter_hp_hz=1800.0))
    assert graph.spec.role("tweeter").filters[0].cutoff_hz == 1800.0
    assert "freq: 1800.0" in graph.yaml_text


def test_unrepresentable_declared_filter_fails_closed() -> None:
    profile = _safety_profile()
    profile["targets"][1]["required_protection_filters"][0][
        "family_or_equivalent"
    ] = "butterworth_only"
    with pytest.raises(ProtectedNeutralGraphError, match="cannot be represented"):
        _session_graph(profile=profile)


def test_configured_preview_polarity_never_enters_measurement_graph() -> None:
    graph = _session_graph(_preset(configured_inverted=True))
    parsed = yaml.safe_load(graph.yaml_text)
    tweeter_sources = parsed["mixers"]["split_active_2way"]["mapping"][1]["sources"]

    assert graph.measurement_polarity_sign_by_role["tweeter"] == 1
    assert graph.configured_polarity_sign_by_role["tweeter"] == -1
    assert tweeter_sources == [{"channel": 1, "gain": 0.0, "inverted": False}]


def test_confirmed_physical_polarity_is_the_only_measurement_inversion() -> None:
    profile = copy.deepcopy(_safety_profile())
    profile["targets"][1]["physical_polarity"] = "inverted"
    graph = _session_graph(_preset(configured_inverted=False), profile=profile)
    parsed = yaml.safe_load(graph.yaml_text)
    tweeter_source = parsed["mixers"]["split_active_2way"]["mapping"][1]["sources"][0]

    assert graph.measurement_polarity_sign_by_role["tweeter"] == -1
    assert graph.configured_polarity_sign_by_role["tweeter"] == 1
    assert tweeter_source["inverted"] is True


def test_graph_spec_is_the_single_protection_and_identity_source() -> None:
    graph = _session_graph()
    spec = graph.identity["canonical_spec"]

    assert spec == graph.spec.to_dict()
    assert spec["excluded_filter_layers"] == list(PROTECTED_NEUTRAL_GRAPH_EXCLUSIONS)
    assert graph.identity["canonical_spec_fingerprint"] == graph.spec.fingerprint
    assert "configured_crossover" not in graph.yaml_text
    filter_names = set(yaml.safe_load(graph.yaml_text)["filters"])
    assert not any("linearization" in name for name in filter_names)


def test_graph_rejects_non_two_way_or_wrong_lane_map() -> None:
    with pytest.raises(ProtectedNeutralGraphError, match="2-way"):
        _session_graph(ActiveSpeakerPreset.from_mapping(_three_way_preset("stereo")))
    with pytest.raises(ProtectedNeutralGraphError, match="woofer lane 0"):
        build_protected_neutral_session_graph(
            preset=_preset(),
            component_safety_profile=_safety_profile(),
            role_channels={"woofer": 1, "tweeter": 0},
            playback_device=ACTIVE_PCM,
        )
