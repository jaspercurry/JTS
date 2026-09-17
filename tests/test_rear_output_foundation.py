# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import yaml
from scipy.io import wavfile

from jasper.active_speaker import camilla_yaml as emit
from jasper.active_speaker import graph_safety as gs
from jasper.active_speaker.branch_peak import stimulus_branch_peaks_dbfs
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.path_safety import staged_target_signature, topology_target_signature
from jasper.active_speaker.profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, SpeakerBaselineProfile
from jasper.active_speaker.rear_calibration import diagnostic_seed, rear_stage_mixer_names
from jasper.active_speaker.runtime_contract import active_ring_channels_for_topology
from jasper.active_speaker.safe_playback import playback_target_signature
from jasper.bass_extension.dynamic_graph import validated_base_graph
from jasper.active_speaker.staging import _bind_preset_to_topology, compile_preset_from_crossover_preview
from jasper.output_topology import (
    OutputTopology, OutputTopologyError, channel_identity_report,
    set_channel_identity_verified, save_output_topology, topology_config_fingerprint, _with_server_owned_identity,
)
from jasper.web.sound_active_speaker import _active_speaker_channel_identity_save_payload
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_runtime_contract import (
    _active_topology, _classify_staged_active, _dynamic_bass_descriptor, _staged_metadata,
    classify_camilla_graph,
)
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


@pytest.mark.parametrize("variant", ["primary", "rear"])
def test_identity_route_confirms_only_the_selected_woofer(monkeypatch, tmp_path, variant):
    path = tmp_path / "topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    raw = _rear_pair("mono")[1].to_dict()
    for channel in raw["speaker_groups"][0]["channels"]:
        channel["identity_verified"] = False
    save_output_topology(OutputTopology.from_mapping(raw), path)
    response = _active_speaker_channel_identity_save_payload({
        "speaker_group_id": "mono", "role": "woofer", "output_variant": variant,
        "identity_verified": True,
    })
    assert {item["id"] for item in response["channel_identity"]["targets"] if item["identity_verified"]} == {
        "mono:woofer" + (":rear" if variant == "rear" else ""),
    }


ACTIVE_PCM = "jts_ring_active_playback"


def _seed(**overrides) -> dict:
    """The diagnostic document, audible unless a case asks otherwise."""
    return {**diagnostic_seed(48000), "rear_muted": False, **overrides}


def _cardioid_baseline(document: dict | None = None, **kwargs) -> tuple[ActiveSpeakerPreset, OutputTopology, str]:
    preset, topology = _rear_pair("mono")
    return preset, topology, emit.emit_active_speaker_baseline_config(
        preset, playback_device=ACTIVE_PCM, rear_calibration=document or _seed(), **kwargs,
    )


def _classify(topology, text: str, *, document: dict | None = None):
    """The staged-active door, with the saved rear calibration section (or none)."""
    config_path = Path("/var/lib/camilladsp/configs/test-staged-active.yml")
    return classify_camilla_graph(
        topology=topology, text=text, config_path=str(config_path),
        staged_config=_staged_metadata(topology, config_path),
        rear_calibration=document,
    )


def _mixer_names(payload: dict) -> list[str]:
    return [step["name"] for step in payload["pipeline"] if step["type"] == "Mixer"]


def _reserialized(text: str, payload: dict) -> str:
    """The mutated graph, keeping the comment header its source marker lives in."""
    header = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    return header + "\n" + yaml.safe_dump(payload)


def _post_split_names(payload: dict, channel: int) -> list[str]:
    """Filter names the pipeline wires to one channel after the active split."""
    split_seen = False
    names: list[str] = []
    for step in payload["pipeline"]:
        if step["type"] == "Mixer":
            split_seen = split_seen or str(step["name"]).startswith("split_active_")
            continue
        if split_seen and channel in step["channels"]:
            names.extend(step["names"])
    return names


def test_rear_calibration_plays_the_rear_behind_the_shared_woofer_chain():
    preset, topology, text = _cardioid_baseline()
    payload = yaml.safe_load(text)
    view = gs.view_from_yaml_dict(payload)

    assert not gs.output_terminally_muted(
        payload, view, 2, mute_name="as_out2_rear_pending_mute", mute_gain_db=-120.0,
    )
    assert "as_out2_rear_pending_mute" not in payload["filters"]
    stage = [name for name in _post_split_names(payload, 2) if name.startswith("rear_out2_")]
    assert stage and _post_split_names(payload, 2)[: len(stage)] == stage
    # Downstream of the stage the rear carries the SAME crossover / delay /
    # gain / limiter chain the front woofer has always carried.
    legacy = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    unfitted = yaml.safe_load(
        emit.emit_active_speaker_baseline_config(legacy, playback_device=ACTIVE_PCM)
    )
    assert _post_split_names(payload, 2)[len(stage):] == _post_split_names(unfitted, 0)
    assert _classify(topology, text, document=_seed()).allowed
    # No saved document, no tolerated fragment: the rear must then be muted.
    assert "rear_output_not_muted" in {i["code"] for i in _classify(topology, text).issues}


def test_the_stage_delays_the_rear_branch_without_a_subsample_allpass():
    payload = yaml.safe_load(_cardioid_baseline(_seed(common_delay_ms=2.0))[2])
    delays = {
        name for name, spec in payload["filters"].items()
        if spec["type"] == "Delay" and name.startswith("rear_out2_")
    }
    assert delays
    # branch_peak cannot model a subsample allpass, so a subsample Delay here
    # would drop every branch-peak proof back to the full-stimulus bound.
    assert not [
        name for name, spec in payload["filters"].items()
        if spec["type"] == "Delay" and "subsample" in spec["parameters"]
    ]


@pytest.mark.parametrize("channel", [0, 1, 2])
def test_every_output_of_the_cardioid_graph_renders_a_finite_branch_peak(tmp_path, channel):
    payload = yaml.safe_load(_cardioid_baseline()[2])
    rate, frames = 48000, 4800
    samples = np.zeros((frames, 2))
    samples[:, 0] = 0.25 * np.sin(2.0 * np.pi * 300.0 * np.arange(frames) / rate)
    wav = tmp_path / "stimulus.wav"
    wavfile.write(str(wav), rate, (samples * 32767.0).astype(np.int16))
    peak = stimulus_branch_peaks_dbfs(payload, wav, output_channels={"out": channel})["out"]
    assert np.isfinite(peak) and peak <= 0.0


def test_muted_rear_keeps_the_stage_but_silences_its_output_gain():
    payload = yaml.safe_load(_cardioid_baseline(_seed(rear_muted=True))[2])
    assert payload["filters"]["rear_out2_output_gain"]["parameters"]["mute"] is True
    assert payload["filters"]["rear_out2_bass_gain"]["parameters"]["mute"] is False


def test_dynamic_bass_owns_both_woofers_and_leaves_the_stage_alone():
    descriptor = _dynamic_bass_descriptor()
    text = _cardioid_baseline()[2]
    decorated = yaml.safe_load(_cardioid_baseline(bass_extension=descriptor)[2])
    assert validated_base_graph(decorated, descriptor, (0, 2)) == yaml.safe_load(text)


def test_the_mixer_sequence_grows_by_exactly_the_stages_split_then_sum():
    preset, topology, text = _cardioid_baseline()
    plain = yaml.safe_load(
        emit.emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    )
    assert _mixer_names(plain) == ["split_active_2way"]
    assert _mixer_names(yaml.safe_load(text)) == ["split_active_2way", *rear_stage_mixer_names(2)]
    assert _classify(topology, text, document=_seed()).allowed


@pytest.mark.parametrize("mutation", ["reordered", "third_mixer"])
def test_the_mixer_sequence_refuses_a_reordered_or_extra_post_split_mixer(mutation):
    _, topology, text = _cardioid_baseline()
    payload = deepcopy(yaml.safe_load(text))
    steps = payload["pipeline"]
    if mutation == "reordered":
        split, summed = (
            next(i for i, step in enumerate(steps) if step.get("name") == name)
            for name in rear_stage_mixer_names(2)
        )
        steps[split], steps[summed] = steps[summed], steps[split]
    else:
        steps.append({"type": "Mixer", "name": "rear_out2_sum"})
    result = _classify(topology, _reserialized(text, payload), document=_seed())
    assert "active_graph_mixer_sequence_invalid" in {issue["code"] for issue in result.issues}


def _retarget(payload: dict, name: str, channel: int) -> None:
    for step in payload["pipeline"]:
        if step.get("type") == "Filter" and name in step.get("names", []):
            step["channels"] = [channel]


@pytest.mark.parametrize("tamper", [
    "branch_gain", "boosting_filter", "repointed_channel", "subsample", "mixer", "unwire",
])
def test_runtime_refuses_a_stage_that_is_not_the_saved_document_recompiled(tamper):
    """Every tamper leaves the NAMES intact — only content differs."""
    _, topology, text = _cardioid_baseline()
    payload = yaml.safe_load(text)
    if tamper == "branch_gain":
        payload["filters"]["rear_out2_bass_gain"]["parameters"]["gain"] = 3.0
    elif tamper == "boosting_filter":
        payload["filters"]["rear_out2_front_gain"] = {
            "type": "Biquad",
            "parameters": {"type": "Peaking", "freq": 60.0, "q": 4.0, "gain": 40.0},
        }
    elif tamper == "repointed_channel":
        _retarget(payload, "rear_out2_front_gain", 1)
    elif tamper == "subsample":
        payload["filters"]["rear_out2_cancellation_delay"]["parameters"]["subsample"] = True
    elif tamper == "mixer":
        payload["mixers"]["rear_out2_sum"]["mapping"][0]["sources"].append(
            {"channel": 3, "gain": 0.0, "inverted": False}
        )
    else:
        payload["pipeline"] = [
            step for step in payload["pipeline"]
            if step.get("name") != "rear_out2_sum"
        ]
    result = _classify(topology, _reserialized(text, payload), document=_seed())
    assert "rear_stage_unproven" in {issue["code"] for issue in result.issues}


def test_a_rear_named_filter_smuggled_into_a_graph_without_a_rear_output_refuses():
    """The stage's name shape grants nothing: a topology with no rear output
    still refuses a `rear_out*` filter exactly as it did before the stage."""
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    topology = _active_topology("mono", "active_2_way")
    text = emit.emit_active_speaker_baseline_config(preset, playback_device=ACTIVE_PCM)
    payload = yaml.safe_load(text)
    payload["filters"]["rear_out9_smuggled"] = {
        "type": "Biquad",
        "parameters": {"type": "Peaking", "freq": 60.0, "q": 4.0, "gain": 40.0},
    }
    split = next(
        index for index, step in enumerate(payload["pipeline"])
        if step.get("name") == "split_active_2way"
    )
    payload["pipeline"].insert(
        split + 1, {"type": "Filter", "channels": [0], "names": ["rear_out9_smuggled"]},
    )
    result = _classify(topology, _reserialized(text, payload))
    assert not result.allowed
    assert "active_output_driver_chain_unrecognized" in {i["code"] for i in result.issues}


@pytest.mark.parametrize("emitter,kwargs", [
    (emit.emit_active_speaker_startup_config, {}),
    (emit.emit_active_speaker_commissioning_config, {"audible_outputs": None}),
    (emit.emit_active_speaker_program_config, {"role_channels": {"woofer": 0, "tweeter": 1}}),
    (emit.emit_active_speaker_driver_domain_config, {"program_channel": "left"}),
])
def test_undecorated_emitters_keep_the_emitter_drift_guard(emitter, kwargs):
    """The tweeter gate's text view REFUSES CamillaDSP's re-serialised dialect
    — that refusal is the drift check, so only a decorated graph is read back
    parsed."""
    preset, _ = _rear_pair("mono")
    text = emitter(preset, playback_device=ACTIVE_PCM, **kwargs)
    with pytest.raises(ActiveSpeakerConfigError):
        emit._assert_tweeter_outputs_protected(
            yaml.safe_dump(yaml.safe_load(text)), preset,
        )


def test_the_decorated_baseline_gate_reads_the_re_serialised_graph():
    preset, _, text = _cardioid_baseline()
    emit._assert_tweeter_outputs_protected(text, preset, decorated=True)
    with pytest.raises(ActiveSpeakerConfigError):
        emit._assert_tweeter_outputs_protected("pipeline: [", preset, decorated=True)
@pytest.mark.parametrize("role_channels,expected,rear_muted", [
    ({"woofer": 0, "tweeter": 1},
     {0: [(0, 0.0, False)], 1: [(1, 0.0, False)], 2: [(0, 0.0, False)]}, True),
    ({"woofer": 0, "woofer:rear": 1},
     {0: [(0, 0.0, False)], 1: [], 2: [(1, 0.0, False)]}, False),
])
def test_program_take_routes_by_physical_target_and_parks_the_rest(role_channels, expected, rear_muted):
    """A take names roles or physical targets. The crossover take drives both
    woofers off the role's channel and keeps the rear muted; the cardioid take
    gives the rear its own channel, so it must NOT be muted (a muted branch
    records silence) and keeps its role's protection at its own output index,
    while the tweeter it does not name is parked with no source at all."""
    preset, _ = _rear_pair("mono")
    text = emit.emit_active_speaker_program_config(
        preset, role_channels=role_channels, playback_device="jts_ring_active_playback",
    )
    payload = yaml.safe_load(text)
    view = gs.view_from_yaml_dict(payload)
    assert {entry["dest"]: [(s["channel"], s["gain"], s["inverted"]) for s in entry["sources"]]
            for entry in payload["mixers"]["split_active_2way"]["mapping"]} == expected
    assert gs.output_terminally_muted(
        payload, view, 2, mute_name="as_out2_rear_pending_mute", mute_gain_db=-120.0,
    ) is rear_muted
    protection = next(step for step in payload["pipeline"]
                      if step.get("type") == "Filter" and step.get("channels") == [0, 2])
    assert any(payload["filters"][name]["type"] == "Limiter" for name in protection["names"])
    assert any(payload["filters"][name].get("parameters", {}).get("type")
               == "LinkwitzRileyLowpass" for name in protection["names"])
