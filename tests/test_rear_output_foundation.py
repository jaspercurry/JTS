# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest
import yaml

from jasper.active_speaker import camilla_yaml as emit
from jasper.active_speaker import graph_safety as gs
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.path_safety import staged_target_signature, topology_target_signature
from jasper.active_speaker.profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, SpeakerBaselineProfile
from jasper.active_speaker.runtime_contract import active_ring_channels_for_topology
from jasper.active_speaker.safe_playback import playback_target_signature
from jasper.active_speaker.staging import _bind_preset_to_topology, compile_preset_from_crossover_preview
from jasper.output_topology import (
    OutputTopology, OutputTopologyError, channel_identity_report,
    set_channel_identity_verified, topology_config_fingerprint, _with_server_owned_identity,
)
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_runtime_contract import _active_topology, _classify_staged_active, _dynamic_bass_descriptor
from tests.test_active_speaker_staging import _crossover_preview


def _rear_pair(layout):
    raw = _two_way_preset(layout)
    topology = _active_topology(layout, "active_2_way").to_dict()
    raw["artifact_schema_version"] = topology["artifact_schema_version"] = 2
    outputs = raw["channel_map"]["outputs"]
    # Synthetic wiring, preserving the original primary indices.
    for group in topology["speaker_groups"]:
        index = len(outputs)
        outputs.append({
            "index": index, "side": group["kind"], "driver_role": "woofer",
            "output_variant": "rear", "label": f"{group['kind']} rear", "startup_muted": True,
        })
        group["channels"].append({
            "role": "woofer", "output_variant": "rear", "physical_output_index": index,
            "startup_muted": True, "identity_verified": False,
        })
    return ActiveSpeakerPreset.from_mapping(raw), OutputTopology.from_mapping(topology)


@pytest.mark.parametrize("layout,width", [("mono", 3), ("stereo", 6)])
def test_rear_identity_roundtrip_and_width(layout, width):
    preset, topology = _rear_pair(layout)
    assert preset.way_count == 2
    assert len(preset.crossover_regions) == 1
    assert not topology.evaluation()["blockers"]
    assert active_ring_channels_for_topology(topology) == width
    assert ActiveSpeakerPreset.from_mapping(preset.to_dict()) == preset
    assert OutputTopology.from_mapping(topology.to_dict()) == topology
    baseline = SpeakerBaselineProfile.from_preset(preset, baseline_id="rear")
    assert SpeakerBaselineProfile.from_mapping(baseline.to_dict()) == baseline
    targets = active_driver_targets(topology)
    old_targets = active_driver_targets(_active_topology(layout, "active_2_way"))
    assert {t["target_id"]: t["target_fingerprint"] for t in old_targets}.items() <= {
        t["target_id"]: t["target_fingerprint"] for t in targets
    }.items()
    assert len({t["target_id"] for t in targets}) == width
    assert len({t["target_fingerprint"] for t in targets}) == width
    for group in topology.speaker_groups:
        assert f"{group.id}:woofer" in {t["target_id"] for t in targets}
        assert f"{group.id}:woofer:rear" in {t["target_id"] for t in targets}
    bound, issues, _, _ = _bind_preset_to_topology(preset, topology, allow_mapped_role_order=True)
    assert not [i for i in issues if i["severity"] == "blocker"]
    assert [(o.index, o.side, o.driver_role, o.output_variant) for o in bound.channel_map.outputs] == [
        (o.index, o.side, o.driver_role, o.output_variant) for o in preset.channel_map.outputs
    ]
    preview = _crossover_preview(topology, frequency_hz=2370, way_count=2)
    compiled, issues, _ = compile_preset_from_crossover_preview(topology, preview)
    assert compiled.channel_map == bound.channel_map
    assert compiled.crossover_regions[0].fc_hz == 2370


@pytest.mark.parametrize("reader,index", [(ActiveSpeakerPreset.from_mapping, 0), (OutputTopology.from_mapping, 1)])
def test_variant_records_require_version_two(reader, index):
    raw = _rear_pair("mono")[index].to_dict()
    raw["artifact_schema_version"] = 1
    with pytest.raises((ActiveSpeakerConfigError, OutputTopologyError)):
        reader(raw)


@pytest.mark.parametrize("variant,role", [("rear", "tweeter"), ("other", "woofer"), ("primary", "woofer")])
def test_invalid_or_duplicate_variant_refuses(variant, role):
    raw = _rear_pair("mono")[0].to_dict()
    raw["channel_map"]["outputs"][-1].update(output_variant=variant, driver_role=role)
    with pytest.raises(ActiveSpeakerConfigError):
        ActiveSpeakerPreset.from_mapping(raw)


def test_identity_evidence_does_not_transfer_between_variants(tmp_path):
    _, topology = _rear_pair("mono")
    recorded = set_channel_identity_verified(
        topology, speaker_group_id="mono", role="woofer", output_variant="rear", identity_verified=True,
    )
    report = channel_identity_report(recorded)
    assert {t["id"] for t in report["targets"] if t["identity_verified"]} == {
        "mono:woofer", "mono:tweeter", "mono:woofer:rear",
    }
    raw = recorded.to_dict()
    channels = raw["speaker_groups"][0]["channels"]
    channels[0]["output_variant"], channels[-1]["output_variant"] = "rear", "primary"
    swapped = OutputTopology.from_mapping(raw)
    admitted = _with_server_owned_identity(swapped, recorded, tmp_path / "topology.json")
    assert {t["id"] for t in channel_identity_report(admitted)["targets"] if t["identity_verified"]} == {"mono:tweeter"}
    assert topology_config_fingerprint(swapped) != topology_config_fingerprint(recorded)
    assert topology_target_signature(swapped) != topology_target_signature(recorded)
    staged = {"targets": topology_target_signature(recorded)[::-1]}
    assert staged_target_signature(staged) == topology_target_signature(recorded)
    front = {"speaker_group_id": "mono", "role": "woofer", "output_index": 0}
    assert playback_target_signature(front) != playback_target_signature(dict(front, output_variant="rear"))
    original_targets = {t["target_id"]: t["target_fingerprint"] for t in active_driver_targets(recorded)}
    assert all(t["target_fingerprint"] != original_targets[t["target_id"]]
               for t in active_driver_targets(swapped) if t["role"] == "woofer")


@pytest.mark.parametrize("layout", ["mono", "stereo"])
@pytest.mark.parametrize("emitter,kwargs", [
    (emit.emit_active_speaker_startup_config, {}),
    (emit.emit_active_speaker_commissioning_config, {"audible_outputs": None}),
    (emit.emit_active_speaker_baseline_config, {}),
    (emit.emit_active_speaker_baseline_config, {"bass_extension": _dynamic_bass_descriptor()}),
    (emit.emit_active_speaker_driver_domain_config, {"program_channel": "left"}),
    (emit.emit_active_speaker_driver_domain_config, {"program_channel": "left", "bass_extension": _dynamic_bass_descriptor()}),
    (emit.emit_active_speaker_program_config, {"role_channels": {"woofer": 0, "tweeter": 1}}),
])
def test_every_emit_keeps_rear_muted_and_primary_filters_unchanged(layout, emitter, kwargs):
    preset, _ = _rear_pair(layout)
    kwargs = dict(kwargs, playback_device="jts_ring_active_playback")
    if "audible_outputs" in kwargs:
        kwargs["audible_outputs"] = frozenset(o.index for o in preset.channel_map.outputs)
    text = emitter(preset, **kwargs)
    payload = yaml.safe_load(text)
    view = gs.view_from_yaml_dict(payload)
    assert payload["devices"]["capture"]["channels"] == 2
    assert payload["devices"]["playback"]["channels"] == len(preset.channel_map.outputs)
    assert payload["devices"]["volume_limit"] == 0.0
    assert payload["devices"]["samplerate"] == 48000
    for output in preset.channel_map.outputs:
        if output.output_variant == "rear":
            assert gs.output_terminally_muted(payload, view, output.index,
                mute_name=f"as_out{output.index}_rear_pending_mute", mute_gain_db=-120.0)
    legacy = ActiveSpeakerPreset.from_mapping(_two_way_preset(layout))
    if "audible_outputs" in kwargs:
        kwargs["audible_outputs"] = frozenset(o.index for o in legacy.channel_map.outputs)
    old = yaml.safe_load(emitter(legacy, **kwargs))
    assert {k: payload["filters"][k] for k in old["filters"]} == old["filters"]
    assert "output_variant" not in legacy.to_dict()["channel_map"]["outputs"][0]
    assert legacy.to_dict()["artifact_schema_version"] == 1


@pytest.mark.parametrize("mutation", ["unmute", "unwire", "reinject"])
def test_runtime_refuses_audible_rear_graph(mutation):
    preset, topology = _rear_pair("mono")
    text = emit.emit_active_speaker_commissioning_config(
        preset, playback_device="jts_ring_active_playback", audible_outputs=frozenset(),
    )
    clean = _classify_staged_active(topology, text)
    assert clean.allowed, clean.issues
    payload = yaml.safe_load(text)
    name = "as_out2_rear_pending_mute"
    if mutation == "unmute":
        payload["filters"][name]["parameters"].update(gain=0.0, mute=False)
    elif mutation == "unwire":
        payload["pipeline"] = [p for p in payload["pipeline"] if name not in p.get("names", [])]
    else:
        payload["pipeline"].append({"type": "Mixer", "name": "split_active_2way"})
    result = _classify_staged_active(topology, yaml.safe_dump(payload))
    assert "rear_output_not_muted" in {i["code"] for i in result.issues}
