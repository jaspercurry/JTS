# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Keystone safety net for the LOCAL subwoofer + bass-management feature.

The emitter (``camilla_yaml``) and the matched graph re-proof
(``runtime_contract.classify_camilla_graph`` via ``graph_safety``) are built
together so they cannot drift: this module emits a sub-bearing baseline config,
re-proves it against the saved topology (must be allowed), then TAMPERS each half
of the single bass-management crossover and proves the re-proof fails closed.

There is NO hardware validation for this subsystem — these emit<->re-proof
round-trips ARE the safety net (a wrong crossover blows a driver).
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from jasper.active_speaker import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    LocalSubwoofer,
    emit_active_speaker_baseline_config,
    lowest_driver_role,
)
from jasper.active_speaker.profile import (
    DEFAULT_SUB_CROSSOVER_HZ,
    SUB_CROSSOVER_HZ_HI,
    SUB_CROSSOVER_HZ_LO,
)
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_bass_extension_graph,
    classify_camilla_graph as _classify_camilla_graph,
    classify_output_contract,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
from tests.test_bass_extension_profile import _applied_baseline, _profile

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def classify_camilla_graph(*args, **kwargs):
    kwargs.setdefault("bass_profile_summary", NO_BASS_EXTENSION_PROFILE_SUMMARY)
    return _classify_camilla_graph(*args, **kwargs)


def test_local_sub_owns_one_natural_bass_extension_pair() -> None:
    topology = _active_2way_sub_topology()
    applied = _applied_baseline()
    profile = replace(
        _profile(topology=topology, applied_baseline=applied),
        bass_owner={
            "kind": "local_sub",
            "roles": ["subwoofer"],
            "channels": [4],
        },
    )
    text = emit_active_speaker_baseline_config(
        _active_2way_sub_preset(),
        playback_device=ACTIVE_PCM,
        bass_extension_profile=profile,
    )
    payload = yaml.safe_load(text)

    owner_steps = [
        step for step in payload["pipeline"]
        if step.get("channels") == [4]
    ]
    assert len(owner_steps) == 1
    assert owner_steps[0]["names"] == [
        "as_sub_lowpass",
        "bass_ext_lt",
        "bass_ext_subsonic",
        "as_sub_baseline_gain",
        "as_sub_baseline_limiter",
    ]
    proof = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=text,
        applied_baseline_state=applied,
        desired_profile=profile,
    )
    assert proof.allowed is True
    assert proof.classification == GRAPH_APPROVED_ACTIVE_RUNTIME

    limiter = payload["filters"]["as_sub_baseline_limiter"]["parameters"]
    assert limiter["clip_limit"] == -1.0
    limiter["clip_limit"] = -2.0
    source = next(
        line for line in text.splitlines() if line.startswith("# Source:")
    )
    tampered = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}",
        applied_baseline_state=applied,
        desired_profile=profile,
    )

    assert tampered.allowed is False
    assert "active_output_driver_chain_unrecognized" in {
        issue["code"] for issue in tampered.issues
    }


# --------------------------------------------------------------------------- #
# Topology + preset fixtures (active-2way+sub and passive-1way+sub).
# --------------------------------------------------------------------------- #


def _topology(groups: list[dict], routing: dict) -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench speaker",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": groups,
        "routing": routing,
    })


def _active_2way_sub_topology() -> OutputTopology:
    return _topology(
        [
            {
                "id": "left",
                "label": "Left",
                "kind": "left",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            },
            {
                "id": "right",
                "label": "Right",
                "kind": "right",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 2, "identity_verified": True},
                    {
                        "role": "tweeter",
                        "physical_output_index": 3,
                        "identity_verified": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            },
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [{"role": "subwoofer", "physical_output_index": 4}],
            },
        ],
        {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    )


def _passive_1way_sub_topology() -> OutputTopology:
    return _topology(
        [
            {
                "id": "left",
                "label": "Left",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [{"role": "subwoofer", "physical_output_index": 2}],
            },
        ],
        {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    )


def _active_2way_sub_preset() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "sub-2way",
        "name": "2-way + local sub",
        "way_count": 2,
        "channel_map": {
            "layout": "stereo",
            "outputs": [
                {"index": 0, "side": "left", "driver_role": "woofer", "label": "L woofer"},
                {"index": 1, "side": "left", "driver_role": "tweeter", "label": "L tweeter"},
                {"index": 2, "side": "right", "driver_role": "woofer", "label": "R woofer"},
                {"index": 3, "side": "right", "driver_role": "tweeter", "label": "R tweeter"},
            ],
        },
        "drivers": {
            "woofer": {"manufacturer": "Example", "model": "Woofer"},
            "tweeter": {"manufacturer": "Example", "model": "Tweeter"},
        },
        "crossover_regions": [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": 1600,
            "order": 4,
        }],
        "local_subwoofer": {"physical_output_index": 4, "label": "Sub"},
    })


def _passive_1way_sub_preset() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": "sub-passive",
        "name": "passive full-range + local sub",
        "way_count": 1,
        "channel_map": {
            "layout": "stereo",
            "outputs": [
                {"index": 0, "side": "left", "driver_role": "full_range", "label": "L full"},
                {"index": 1, "side": "right", "driver_role": "full_range", "label": "R full"},
            ],
        },
        "drivers": {"full_range": {"manufacturer": "Example", "model": "Full-range"}},
        "local_subwoofer": {"physical_output_index": 2, "label": "Sub", "crossover_fc_hz": 100.0},
    })


def _baseline(preset: ActiveSpeakerPreset) -> str:
    return emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, baseline_id="b1"
    )


# --------------------------------------------------------------------------- #
# Profile model.
# --------------------------------------------------------------------------- #


def test_sub_crossover_corner_mirrors_wireless_sub() -> None:
    # The local-sub corner reuses the wireless sub's exact numbers.
    from jasper.multiroom.config import (
        CROSSOVER_HZ_HI,
        CROSSOVER_HZ_LO,
        DEFAULT_CROSSOVER_HZ,
    )

    assert DEFAULT_SUB_CROSSOVER_HZ == DEFAULT_CROSSOVER_HZ == 80.0
    assert SUB_CROSSOVER_HZ_LO == CROSSOVER_HZ_LO == 40.0
    assert SUB_CROSSOVER_HZ_HI == CROSSOVER_HZ_HI == 200.0


def test_local_sub_defaults_to_80hz() -> None:
    sub = LocalSubwoofer.from_mapping({"physical_output_index": 4})
    assert sub.crossover_fc_hz == 80.0
    assert sub.startup_muted is True


@pytest.mark.parametrize("fc", [39.0, 201.0, 0.0])
def test_local_sub_rejects_out_of_range_corner(fc: float) -> None:
    with pytest.raises(ActiveSpeakerConfigError):
        LocalSubwoofer(physical_output_index=4, label="Sub", crossover_fc_hz=fc).validate()


def test_subless_one_way_preset_is_rejected() -> None:
    # A 1-way passive main is ONLY valid with a local sub; subless passive takes
    # the flat program lane, never this multi-output path.
    with pytest.raises(ActiveSpeakerConfigError, match="local subwoofer"):
        ActiveSpeakerPreset.from_mapping({
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_preset",
            "preset_id": "bad-passive",
            "name": "passive subless",
            "way_count": 1,
            "channel_map": {
                "layout": "stereo",
                "outputs": [
                    {"index": 0, "side": "left", "driver_role": "full_range", "label": "L"},
                    {"index": 1, "side": "right", "driver_role": "full_range", "label": "R"},
                ],
            },
            "drivers": {"full_range": {"manufacturer": "X", "model": "FR"}},
        })


def test_sub_output_index_must_be_next_contiguous_channel() -> None:
    with pytest.raises(ActiveSpeakerConfigError, match="contiguous"):
        ActiveSpeakerPreset.from_mapping({
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_preset",
            "preset_id": "bad-sub-index",
            "name": "bad sub index",
            "way_count": 1,
            "channel_map": {
                "layout": "stereo",
                "outputs": [
                    {"index": 0, "side": "left", "driver_role": "full_range", "label": "L"},
                    {"index": 1, "side": "right", "driver_role": "full_range", "label": "R"},
                ],
            },
            "drivers": {"full_range": {"manufacturer": "X", "model": "FR"}},
            # Should be 2 (next contiguous channel), not 5.
            "local_subwoofer": {"physical_output_index": 5, "label": "Sub"},
        })


def test_lowest_driver_role() -> None:
    assert lowest_driver_role(1) == "full_range"
    assert lowest_driver_role(2) == "woofer"
    assert lowest_driver_role(3) == "woofer"


def test_preset_round_trips_local_subwoofer() -> None:
    preset = _active_2way_sub_preset()
    assert preset.has_local_subwoofer
    again = ActiveSpeakerPreset.from_mapping(preset.to_dict())
    assert again.local_subwoofer is not None
    assert again.local_subwoofer.physical_output_index == 4


# --------------------------------------------------------------------------- #
# Emit shape (the structural half of the keystone).
# --------------------------------------------------------------------------- #


def _parse(text: str) -> dict:
    return yaml.safe_load(text)


def _pipeline_step(
    payload: dict, channels: set[int], *, contains: str | None = None
) -> dict | None:
    target = sorted(channels)
    for step in payload.get("pipeline", []):
        if step.get("type") != "Filter" or sorted(step.get("channels", [])) != target:
            continue
        if contains is not None and contains not in step.get("names", []):
            continue
        return step
    return None


def test_active_2way_sub_emit_shape() -> None:
    payload = _parse(_baseline(_active_2way_sub_preset()))

    # output_count grew by the sub output: 4 mains + 1 sub = 5.
    assert payload["devices"]["playback"]["channels"] == 5
    assert payload["devices"]["volume_limit"] == 0.0
    assert payload["mixers"]["split_active_2way"]["channels"] == {"in": 2, "out": 5}

    # The sub dest is the clip-safe mono sum (L+R at -6.0206 dB each).
    sub_dest = next(
        d for d in payload["mixers"]["split_active_2way"]["mapping"] if d["dest"] == 4
    )
    sub_srcs = sorted((s["channel"], round(s["gain"], 4)) for s in sub_dest["sources"])
    assert sub_srcs == [(0, -6.0206), (1, -6.0206)]

    # The mains' lowest driver (woofer, outputs 0+2) carries the LR4 bass-mgmt HP.
    woofer_step = _pipeline_step(payload, {0, 2})
    assert woofer_step is not None
    assert "as_woofer_bass_mgmt_hp" in woofer_step["names"]
    hp = payload["filters"]["as_woofer_bass_mgmt_hp"]
    assert hp["type"] == "BiquadCombo"
    assert hp["parameters"]["type"] == "LinkwitzRileyHighpass"
    assert hp["parameters"]["freq"] == DEFAULT_SUB_CROSSOVER_HZ
    assert hp["parameters"]["order"] == 4

    # The sub output is band-limited (LP) + gain<=0 + soft-clip limiter.
    sub_step = _pipeline_step(payload, {4})
    assert sub_step is not None
    assert sub_step["names"] == [
        "as_sub_lowpass",
        "as_sub_baseline_gain",
        "as_sub_baseline_limiter",
    ]
    lp = payload["filters"]["as_sub_lowpass"]
    assert lp["parameters"]["type"] == "LinkwitzRileyLowpass"
    assert lp["parameters"]["freq"] == DEFAULT_SUB_CROSSOVER_HZ
    assert payload["filters"]["as_sub_baseline_gain"]["parameters"]["gain"] <= 0.0
    lim = payload["filters"]["as_sub_baseline_limiter"]
    assert lim["type"] == "Limiter"
    assert lim["parameters"]["soft_clip"] is True
    assert lim["parameters"]["clip_limit"] <= 0.0

    # The sub LP and the mains HP share ONE corner — two halves of one crossover.
    assert lp["parameters"]["freq"] == hp["parameters"]["freq"]


def test_passive_1way_sub_emit_shape() -> None:
    payload = _parse(_baseline(_passive_1way_sub_preset()))

    assert payload["devices"]["playback"]["channels"] == 3
    assert payload["mixers"]["split_active_1way"]["channels"] == {"in": 2, "out": 3}

    # The degenerate full-range main carries the bass-mgmt HP at the sub corner.
    # (Both the program headroom step and the driver chain ride [0, 1]; the driver
    # chain is the one carrying the HP.)
    main_step = _pipeline_step(payload, {0, 1}, contains="as_full_range_bass_mgmt_hp")
    assert main_step is not None
    assert payload["filters"]["as_full_range_bass_mgmt_hp"]["parameters"]["freq"] == 100.0
    assert payload["filters"]["as_sub_lowpass"]["parameters"]["freq"] == 100.0

    # No positive gain anywhere.
    for name, fdef in payload["filters"].items():
        if fdef.get("type") == "Gain":
            assert fdef["parameters"]["gain"] <= 0.0, name


# --------------------------------------------------------------------------- #
# The keystone: emit -> re-proof allowed; tamper -> blocked.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "topology_fn,preset_fn",
    [
        (_active_2way_sub_topology, _active_2way_sub_preset),
        (_passive_1way_sub_topology, _passive_1way_sub_preset),
    ],
)
def test_sub_baseline_reproof_allows_well_formed_graph(topology_fn, preset_fn) -> None:
    topology = topology_fn()
    contract = classify_output_contract(topology)
    assert contract.subwoofer_present is True
    assert contract.requires_roleful_graph is True

    graph = classify_camilla_graph(topology=topology, text=_baseline(preset_fn()))
    assert graph.allowed is True, [i["code"] for i in graph.issues]
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert graph.details["subwoofer_present"] is True


def test_sub_baseline_reproof_blocks_filter_after_sub_limiter() -> None:
    baseline = _baseline(_active_2way_sub_preset())
    payload = yaml.safe_load(baseline)
    payload["filters"]["forged_post_sub_limiter_peq"] = {
        "type": "Biquad",
        "parameters": {
            "type": "Peaking",
            "freq": 2000.0,
            "q": 1.0,
            "gain": 60.0,
        },
    }
    payload["pipeline"].append({
        "type": "Filter",
        "channels": [4],
        "names": ["forged_post_sub_limiter_peq"],
    })
    source = next(
        line for line in baseline.splitlines() if line.startswith("# Source:")
    )

    graph = classify_camilla_graph(
        topology=_active_2way_sub_topology(),
        text=f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}",
    )

    assert graph.allowed is False
    assert "active_output_post_limiter_filter_unsafe" in {
        issue["code"] for issue in graph.issues
    }


@pytest.mark.parametrize("corner_hz", [39.0, 201.0, 20_000.0])
def test_sub_baseline_reproof_blocks_out_of_range_paired_corner(
    corner_hz: float,
) -> None:
    baseline = _baseline(_active_2way_sub_preset())
    payload = yaml.safe_load(baseline)
    for name in ("as_sub_lowpass", "as_woofer_bass_mgmt_hp"):
        payload["filters"][name]["parameters"]["freq"] = corner_hz
    source = next(
        line for line in baseline.splitlines() if line.startswith("# Source:")
    )

    graph = classify_camilla_graph(
        topology=_active_2way_sub_topology(),
        text=f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}",
    )

    assert graph.allowed is False
    assert "active_output_driver_chain_unrecognized" in {
        issue["code"] for issue in graph.issues
    }


@pytest.mark.parametrize("order", [2, 8])
def test_sub_baseline_reproof_requires_canonical_crossover_order(order: int) -> None:
    baseline = _baseline(_active_2way_sub_preset())
    payload = yaml.safe_load(baseline)
    for name in ("as_sub_lowpass", "as_woofer_bass_mgmt_hp"):
        payload["filters"][name]["parameters"]["order"] = order
    source = next(
        line for line in baseline.splitlines() if line.startswith("# Source:")
    )

    graph = classify_camilla_graph(
        topology=_active_2way_sub_topology(),
        text=f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}",
    )

    assert graph.allowed is False
    assert "active_output_driver_chain_unrecognized" in {
        issue["code"] for issue in graph.issues
    }


def test_passive_main_bass_management_requires_canonical_order() -> None:
    baseline = _baseline(_passive_1way_sub_preset())
    payload = yaml.safe_load(baseline)
    payload["filters"]["as_full_range_bass_mgmt_hp"]["parameters"]["order"] = 2
    source = next(
        line for line in baseline.splitlines() if line.startswith("# Source:")
    )

    graph = classify_camilla_graph(
        topology=_passive_1way_sub_topology(),
        text=f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}",
    )

    assert graph.allowed is False
    assert "active_baseline_bass_mgmt_highpass_missing" in {
        issue["code"] for issue in graph.issues
    }


@pytest.mark.parametrize(
    "topology_fn,preset_fn",
    [
        (_active_2way_sub_topology, _active_2way_sub_preset),
        (_passive_1way_sub_topology, _passive_1way_sub_preset),
    ],
)
def test_sub_baseline_reproof_blocks_missing_sub_lowpass(topology_fn, preset_fn) -> None:
    yaml_text = _baseline(preset_fn())
    # Drop the sub low-pass from the sub's pipeline chain — an un-band-limited sub
    # output (a full-range feed reaching the sub) MUST be blocked.
    tampered = yaml_text.replace("as_sub_lowpass, ", "")
    graph = classify_camilla_graph(topology=topology_fn(), text=tampered)
    assert graph.allowed is False
    assert "active_baseline_sub_guard_missing" in {i["code"] for i in graph.issues}


@pytest.mark.parametrize(
    "topology_fn,preset_fn,hp_name",
    [
        (_active_2way_sub_topology, _active_2way_sub_preset, "as_woofer_bass_mgmt_hp"),
        (_passive_1way_sub_topology, _passive_1way_sub_preset, "as_full_range_bass_mgmt_hp"),
    ],
)
def test_sub_baseline_reproof_blocks_missing_mains_highpass(
    topology_fn, preset_fn, hp_name
) -> None:
    yaml_text = _baseline(preset_fn())
    # Drop the complementary mains high-pass — the sub LP alone is half a
    # crossover; the mains would still carry full bass. MUST be blocked.
    tampered = yaml_text.replace(f"{hp_name}, ", "")
    graph = classify_camilla_graph(topology=topology_fn(), text=tampered)
    assert graph.allowed is False
    assert "active_baseline_bass_mgmt_highpass_missing" in {
        i["code"] for i in graph.issues
    }


@pytest.mark.parametrize(
    "topology_fn,preset_fn",
    [
        (_active_2way_sub_topology, _active_2way_sub_preset),
        (_passive_1way_sub_topology, _passive_1way_sub_preset),
    ],
)
def test_sub_baseline_reproof_blocks_positive_sub_gain(topology_fn, preset_fn) -> None:
    yaml_text = _baseline(preset_fn())
    tampered = yaml_text.replace(
        "  as_sub_baseline_gain:\n    type: Gain\n    parameters: { gain: 0.0000",
        "  as_sub_baseline_gain:\n    type: Gain\n    parameters: { gain: 3.0000",
    )
    assert tampered != yaml_text
    graph = classify_camilla_graph(topology=topology_fn(), text=tampered)
    assert graph.allowed is False
    assert "active_baseline_sub_guard_missing" in {i["code"] for i in graph.issues}


@pytest.mark.parametrize(
    "topology_fn,preset_fn",
    [
        (_active_2way_sub_topology, _active_2way_sub_preset),
        (_passive_1way_sub_topology, _passive_1way_sub_preset),
    ],
)
def test_sub_baseline_reproof_blocks_removed_sub_limiter(topology_fn, preset_fn) -> None:
    yaml_text = _baseline(preset_fn())
    # Rename the sub limiter DEF so the wired name no longer resolves to a
    # Limiter — an unprotected (excursion-unbounded) sub MUST be blocked.
    tampered = yaml_text.replace("as_sub_baseline_limiter:", "as_sub_baseline_limiter_x:", 1)
    graph = classify_camilla_graph(topology=topology_fn(), text=tampered)
    assert graph.allowed is False
    assert "active_baseline_sub_guard_missing" in {i["code"] for i in graph.issues}


@pytest.mark.parametrize(
    "topology_fn,preset_fn",
    [
        (_active_2way_sub_topology, _active_2way_sub_preset),
        (_passive_1way_sub_topology, _passive_1way_sub_preset),
    ],
)
def test_sub_baseline_reproof_blocks_positive_volume_limit(topology_fn, preset_fn) -> None:
    yaml_text = _baseline(preset_fn())
    tampered = yaml_text.replace("volume_limit: 0.0", "volume_limit: 3.0")
    graph = classify_camilla_graph(topology=topology_fn(), text=tampered)
    assert graph.allowed is False
    assert "volume_limit_positive" in {i["code"] for i in graph.issues}


@pytest.mark.parametrize(
    "topology_fn,preset_fn",
    [
        (_active_2way_sub_topology, _active_2way_sub_preset),
        (_passive_1way_sub_topology, _passive_1way_sub_preset),
    ],
)
def test_sub_baseline_reproof_blocks_split_crossover_corner(topology_fn, preset_fn) -> None:
    # Both halves PRESENT but at DIFFERENT corners (the sub LP moved off the mains
    # HP corner) — NOT two halves of one crossover; the sub would reproduce
    # midrange / leave a mid-band hole. The emitter never writes this (one Fc
    # drives both halves), but the re-proof must still reject a corrupted/tampered
    # statefile that splits the crossover. Precise single-filter string mutation
    # anchored on the unique as_sub_lowpass block (a plain "freq: 80" replace would
    # also move the mains HP and keep them matched; a yaml round-trip reformats the
    # graph enough that classify mis-parses it).
    yaml_text = _baseline(preset_fn())
    # Move ONLY the sub LP's freq (anchored on the unique as_sub_lowpass block;
    # freq-agnostic since the two presets use different corners) off the mains HP.
    tampered, n = re.subn(
        r"(as_sub_lowpass:\n    type: BiquadCombo\n    parameters:\n"
        r"      type: LinkwitzRileyLowpass\n      freq: )[0-9.]+",
        r"\g<1>1234.0000",
        yaml_text,
    )
    assert n == 1  # the anchor must have matched exactly once
    graph = classify_camilla_graph(topology=topology_fn(), text=tampered)
    assert graph.allowed is False
    assert "active_baseline_bass_mgmt_corner_split" in {i["code"] for i in graph.issues}


def test_commissioning_reproof_blocks_audible_sub_without_band_limit() -> None:
    """A subwoofer output that is UNMUTED (audible) in a commissioning/startup
    graph MUST be band-limited (LR4 low-pass) + excursion-limited — the
    non-baseline analogue of the baseline sub guard. The honest emitter keeps the
    sub muted in the commissioning sequence, but restore_active_camilla_solo loads
    a guarded_commissioning graph off disk, so a corrupted/tampered statefile that
    unmutes the sub AND drops its low-pass (a full-range feed to a powered sub)
    must be rejected — mirroring the tweeter audible guard. (This is the
    commissioning-path counterpart to the baseline sub-guard tamper tests above,
    which only protect the durable-baseline class.)"""
    from jasper.active_speaker.camilla_yaml import (
        emit_active_speaker_commissioning_config,
    )

    preset = _active_2way_sub_preset()
    topology = _active_2way_sub_topology()
    sub_idx = preset.local_subwoofer.physical_output_index
    honest = emit_active_speaker_commissioning_config(
        preset, playback_device="hw:TEST,0", audible_outputs={sub_idx}
    )
    # An HONESTLY audible sub (LP + limiter present) is allowed — the guard must
    # not false-positive on the legitimate audible-sub commissioning graph.
    assert classify_camilla_graph(topology=topology, text=honest).allowed is True
    chain = "[as_sub_lowpass, as_sub_startup_limiter]"
    assert chain in honest  # the sub's wired protective chain — anchor present
    # Drop the band-limit -> full-range feed to a powered sub -> rejected.
    no_lp = classify_camilla_graph(
        topology=topology, text=honest.replace(chain, "[as_sub_startup_limiter]")
    )
    assert no_lp.allowed is False
    assert "active_graph_unprotected_sub_audible" in {i["code"] for i in no_lp.issues}
    # Drop the excursion limiter -> rejected too.
    no_lim = classify_camilla_graph(
        topology=topology, text=honest.replace(chain, "[as_sub_lowpass]")
    )
    assert no_lim.allowed is False
    assert "active_graph_unprotected_sub_audible" in {i["code"] for i in no_lim.issues}


def test_commissioning_reproof_blocks_audible_sub_with_high_corner() -> None:
    """An audible sub's low-pass CORNER is bounded, not just its presence. A
    degenerate high corner (a 20 kHz "low-pass" = effectively full-range to a bass
    driver) must be rejected: for a sub LOW-pass a higher corner is LESS protective
    (the opposite direction from a tweeter high-pass), so the commissioning guard
    caps the corner at the legal sub-crossover ceiling. (The baseline class bounds
    the corner via bass_management_corner_matched; this is its commissioning-path
    counterpart.) Anchored single-filter tamper so ONLY the sub LP moves — a plain
    "freq: 80" replace would also move the mains HP."""
    from jasper.active_speaker.camilla_yaml import (
        emit_active_speaker_commissioning_config,
    )

    preset = _active_2way_sub_preset()
    topology = _active_2way_sub_topology()
    sub_idx = preset.local_subwoofer.physical_output_index
    honest = emit_active_speaker_commissioning_config(
        preset, playback_device="hw:TEST,0", audible_outputs={sub_idx}
    )
    assert classify_camilla_graph(topology=topology, text=honest).allowed is True
    # Push ONLY the sub LP corner far above the legal sub ceiling (200 Hz).
    tampered, n = re.subn(
        r"(as_sub_lowpass:\n    type: BiquadCombo\n    parameters:\n"
        r"      type: LinkwitzRileyLowpass\n      freq: )[0-9.]+",
        r"\g<1>20000.0000",
        honest,
    )
    assert n == 1  # the as_sub_lowpass anchor matched exactly once
    graph = classify_camilla_graph(topology=topology, text=tampered)
    assert graph.allowed is False
    assert "active_graph_unprotected_sub_audible" in {i["code"] for i in graph.issues}


def test_commissioning_reproof_blocks_malformed_sub_lowpass_while_muted() -> None:
    from jasper.active_speaker.camilla_yaml import (
        emit_active_speaker_commissioning_config,
    )

    preset = _active_2way_sub_preset()
    honest = emit_active_speaker_commissioning_config(
        preset,
        playback_device="hw:TEST,0",
        audible_outputs=set(),
    )
    tampered = honest.replace(
        "as_sub_lowpass:\n    type: BiquadCombo\n    parameters:\n"
        "      type: LinkwitzRileyLowpass",
        "as_sub_lowpass:\n    type: Biquad\n    parameters:\n"
        "      type: Peaking",
    )
    assert tampered != honest

    graph = classify_camilla_graph(
        topology=_active_2way_sub_topology(),
        text=tampered,
    )

    assert graph.allowed is False
    assert "active_commissioning_chain_unrecognized" in {
        issue["code"] for issue in graph.issues
    }


# --------------------------------------------------------------------------- #
# User-settable crossover Fc threading + passive-mains+sub build path.
# --------------------------------------------------------------------------- #


def _passive_1way_sub_topology_fc(fc: float | None) -> OutputTopology:
    sub_channel: dict = {"role": "subwoofer", "physical_output_index": 2}
    if fc is not None:
        sub_channel["crossover_fc_hz"] = fc
    return _topology(
        [
            {
                "id": "left",
                "label": "Left",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [
                    {"role": "full_range", "physical_output_index": 0, "identity_verified": True}
                ],
            },
            {
                "id": "right",
                "label": "Right",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [
                    {"role": "full_range", "physical_output_index": 1, "identity_verified": True}
                ],
            },
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [sub_channel],
            },
        ],
        {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    )


def _subless_passive_topology() -> OutputTopology:
    return _topology(
        [
            {
                "id": "left",
                "label": "Left",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
        ],
        {"main_left_group_id": "left", "main_right_group_id": "right"},
    )


def test_local_subwoofer_uses_topology_crossover_fc() -> None:
    from jasper.active_speaker.staging import _local_subwoofer_from_topology

    topology = _passive_1way_sub_topology_fc(120.0)
    sub, issues = _local_subwoofer_from_topology(topology, main_output_count=2)
    assert issues == []
    assert sub is not None
    # The user-set 120 Hz corner wins, NOT the 80 Hz default.
    assert sub.crossover_fc_hz == 120.0


def test_local_subwoofer_falls_back_to_default_corner() -> None:
    from jasper.active_speaker.profile import DEFAULT_SUB_CROSSOVER_HZ
    from jasper.active_speaker.staging import _local_subwoofer_from_topology

    topology = _passive_1way_sub_topology_fc(None)
    sub, issues = _local_subwoofer_from_topology(topology, main_output_count=2)
    assert issues == []
    assert sub is not None
    assert sub.crossover_fc_hz == DEFAULT_SUB_CROSSOVER_HZ == 80.0


def test_topology_is_passive_mains_with_sub_predicate() -> None:
    from jasper.active_speaker.staging import topology_is_passive_mains_with_sub

    assert topology_is_passive_mains_with_sub(_passive_1way_sub_topology_fc(120.0)) is True
    # A SUBLESS passive speaker is NOT routed through the active emitter.
    assert topology_is_passive_mains_with_sub(_subless_passive_topology()) is False
    # An active 2-way + sub is the preview-driven path, not this one.
    assert topology_is_passive_mains_with_sub(_active_2way_sub_topology()) is False


def test_passive_mains_sub_builds_and_reproves_at_topology_fc(tmp_path) -> None:
    # The headline of this slice: a full_range_passive + sub topology now COMPILES
    # end-to-end through the active multi-output emitter (the degenerate 1-way bass-
    # management path), with NO crossover preview / active measurements, and the
    # emitted graph re-proves as an approved active runtime with the sub LP + mains
    # HP at the user-set topology corner (120 Hz, not the 80 Hz default).
    from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate

    topology = _passive_1way_sub_topology_fc(120.0)
    config_path = tmp_path / "passive_sub_baseline.yml"
    payload = build_baseline_profile_candidate(
        topology,
        design_draft={"status": "ready"},
        crossover_preview={},  # passive mains produce no active preview
        measurements={},  # no active per-driver / summed measurements
        write=True,
        state_path=tmp_path / "passive_sub_state.json",
        config_path=config_path,
    )

    assert payload["status"] == "ready_to_apply", payload.get("issues")
    assert payload["permissions"]["may_apply"] is True
    assert payload["preset"]["way_count"] == 1
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"

    # #1666: candidate lands on a content-addressed sibling, not config_path.
    text = Path(payload["config"]["path"]).read_text(encoding="utf-8")
    graph = classify_camilla_graph(topology=topology, text=text)
    assert graph.allowed is True, [i["code"] for i in graph.issues]
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME

    payload_yaml = yaml.safe_load(text)
    assert payload_yaml["filters"]["as_sub_lowpass"]["parameters"]["freq"] == 120.0
    assert payload_yaml["filters"]["as_full_range_bass_mgmt_hp"]["parameters"]["freq"] == 120.0


def test_subless_passive_does_not_compile_an_active_preset(tmp_path) -> None:
    # A SUBLESS passive topology must NOT route through the active emitter: with no
    # ready active crossover preview it blocks (the active preview-driven path), and
    # never produces a passive 1-way preset. (At runtime such a topology takes the
    # flat emit_sound_config lane; this asserts the build path does not hijack it.)
    from jasper.active_speaker.baseline_profile import build_baseline_profile_candidate

    topology = _subless_passive_topology()
    payload = build_baseline_profile_candidate(
        topology,
        design_draft={"status": "ready"},
        crossover_preview={},
        measurements={},
        write=False,
        state_path=tmp_path / "subless_state.json",
        config_path=tmp_path / "subless_baseline.yml",
    )

    assert payload["status"] == "blocked"
    codes = {i["code"] for i in payload["issues"]}
    assert "baseline_crossover_preview_not_ready" in codes
    assert payload.get("preset", {}).get("way_count") in (None,)
