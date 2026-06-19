from __future__ import annotations

from pathlib import Path

from jasper.active_speaker import (
    ActiveSpeakerPreset,
    emit_active_speaker_baseline_config,
    emit_active_speaker_commissioning_config,
)
from jasper.active_speaker.runtime_contract import (
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_FLAT_FULL_RANGE,
    GRAPH_GUARDED_COMMISSIONING,
    CONTRACT_ACTIVE_MONO_2WAY,
    CONTRACT_ACTIVE_MONO_3WAY,
    CONTRACT_ACTIVE_STEREO_2WAY,
    CONTRACT_ACTIVE_STEREO_3WAY,
    CONTRACT_NORMAL_MONO_FULL_RANGE,
    CONTRACT_NORMAL_STEREO_FULL_RANGE,
    CONTRACT_SUBWOOFER_PRESENT,
    classify_camilla_graph,
    classify_output_contract,
    apply_safe_graph_decision_to_statefile,
    safe_graph_for_current_topology,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology

from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def _flat_yaml() -> str:
    return (
        "devices:\n"
        "  samplerate: 48000\n"
        "  volume_limit: 0.0\n"
        "  playback:\n"
        "    type: Alsa\n"
        "    channels: 2\n"
        "    device: outputd_content_playback\n"
        "filters:\n"
        "  flat:\n"
        "    type: Gain\n"
        "    parameters: { gain: 0.0, mute: false }\n"
    )


def _topology(groups: list[dict], routing: dict | None = None) -> OutputTopology:
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
        "routing": routing or {},
    })


def _full_range_stereo() -> OutputTopology:
    return _topology(
        [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
        ],
        {"main_left_group_id": "left", "main_right_group_id": "right"},
    )


def _full_range_mono() -> OutputTopology:
    return _topology(
        [
            {
                "id": "mono",
                "label": "Mono speaker",
                "kind": "mono",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            }
        ],
        {"mono_group_id": "mono"},
    )


def _active_group(kind: str, mode: str, start: int) -> dict:
    roles = ("woofer", "tweeter") if mode == "active_2_way" else (
        "woofer",
        "mid",
        "tweeter",
    )
    channels = []
    for offset, role in enumerate(roles):
        channel = {
            "role": role,
            "physical_output_index": start + offset,
            "identity_verified": True,
        }
        if role == "tweeter":
            channel.update({
                "startup_muted": True,
                "protection_required": True,
                "protection_status": "software_guard_requested",
            })
        channels.append(channel)
    return {
        "id": kind,
        "label": f"{kind.title()} speaker",
        "kind": kind,
        "mode": mode,
        "channels": channels,
    }


def _active_topology(layout: str, mode: str) -> OutputTopology:
    if layout == "mono":
        return _topology([_active_group("mono", mode, 0)], {"mono_group_id": "mono"})
    step = 2 if mode == "active_2_way" else 3
    return _topology(
        [_active_group("left", mode, 0), _active_group("right", mode, step)],
        {"main_left_group_id": "left", "main_right_group_id": "right"},
    )


def _subwoofer_topology() -> OutputTopology:
    return _topology(
        [
            {
                "id": "sub",
                "label": "Subwoofer",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [{"role": "subwoofer", "physical_output_index": 0}],
            }
        ],
        {"subwoofer_group_ids": ["sub"]},
    )


def _active_yaml(layout: str, way: int, audible: set[int] | frozenset[int]) -> str:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return emit_active_speaker_commissioning_config(
        ActiveSpeakerPreset.from_mapping(raw),
        playback_device=ACTIVE_PCM,
        audible_outputs=audible,
    )


def _active_baseline_yaml(layout: str, way: int) -> str:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return emit_active_speaker_baseline_config(
        ActiveSpeakerPreset.from_mapping(raw),
        playback_device=ACTIVE_PCM,
        baseline_id=f"baseline-{layout}-{way}way",
    )


def _staged_metadata(topology: OutputTopology, path: Path) -> dict:
    targets = []
    for group in topology.speaker_groups:
        for channel in group.channels:
            targets.append({
                "speaker_group_id": group.id,
                "role": channel.role,
                "physical_output_index": channel.physical_output_index,
                "identity_verified": channel.identity_verified,
                "startup_muted": channel.startup_muted,
                "protection_required": channel.protection_required,
                "protection_status": channel.protection_status,
            })
    return {
        "status": "staged",
        "topology": {"topology_id": topology.topology_id},
        "hardware": {
            "device_id": topology.hardware.device_id,
            "card_id": topology.hardware.card_id,
            "physical_output_count": topology.hardware.physical_output_count,
            "clock_domain_id": topology.hardware.clock_domain_id,
        },
        "targets": targets,
        "config": {"path": str(path)},
        "software_guard": {"passed": True, "no_load": True, "no_playback": True},
    }


def test_no_topology_allows_flat_outputd_cutover() -> None:
    topology = _topology([])
    graph = classify_camilla_graph(topology=topology, text=_flat_yaml())

    assert graph.classification == GRAPH_FLAT_FULL_RANGE
    assert graph.allowed is True


def test_full_range_stereo_allows_flat_outputd_cutover() -> None:
    topology = _full_range_stereo()
    contract = classify_output_contract(topology)
    graph = classify_camilla_graph(topology=topology, text=_flat_yaml())

    assert contract.classification == CONTRACT_NORMAL_STEREO_FULL_RANGE
    assert contract.requires_roleful_graph is False
    assert graph.allowed is True


def test_full_range_mono_rejects_wider_flat_outputd_cutover(tmp_path: Path) -> None:
    topology = _full_range_mono()
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    decision = safe_graph_for_current_topology(
        topology,
        flat_config_path=flat,
        staged_config={},
    )

    assert classify_output_contract(topology).classification == CONTRACT_NORMAL_MONO_FULL_RANGE
    assert decision.status == "blocked"
    assert "flat_full_range_graph_wider_than_topology" in {
        issue["code"] for issue in decision.issues
    }


def test_mono_active_2way_rejects_flat_and_allows_guarded_graphs() -> None:
    topology = _active_topology("mono", "active_2_way")

    flat = classify_camilla_graph(topology=topology, text=_flat_yaml())
    startup = classify_camilla_graph(
        topology=topology,
        text=_active_yaml("mono", 2, frozenset()),
    )
    woofer = classify_camilla_graph(
        topology=topology,
        text=_active_yaml("mono", 2, {0}),
    )
    tweeter = classify_camilla_graph(
        topology=topology,
        text=_active_yaml("mono", 2, {1}),
    )

    assert classify_output_contract(topology).classification == CONTRACT_ACTIVE_MONO_2WAY
    assert flat.allowed is False
    assert "flat_full_range_graph_illegal" in flat.issues[0]["code"]
    assert startup.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    assert startup.allowed is True
    assert woofer.classification == GRAPH_GUARDED_COMMISSIONING
    assert woofer.allowed is True
    assert tweeter.classification == GRAPH_GUARDED_COMMISSIONING
    assert tweeter.allowed is True


def test_mono_active_2way_allows_approved_baseline_runtime() -> None:
    topology = _active_topology("mono", "active_2_way")

    graph = classify_camilla_graph(
        topology=topology,
        text=_active_baseline_yaml("mono", 2),
    )

    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert graph.allowed is True
    assert graph.details["baseline_candidate"] is True
    assert graph.details["unmuted_outputs"] == [0, 1]


def test_tweeter_commissioning_requires_protective_highpass() -> None:
    topology = _active_topology("mono", "active_2_way")
    unsafe = _active_yaml("mono", 2, {1}).replace("as_tweeter_protective_hp", "as_tweeter_missing_hp")

    graph = classify_camilla_graph(topology=topology, text=unsafe)

    assert graph.allowed is False
    assert graph.classification == "unsafe"
    assert "active_graph_tweeter_guard_missing" in {
        issue["code"] for issue in graph.issues
    }


def test_tweeter_commissioning_requires_protective_limiter() -> None:
    topology = _active_topology("mono", "active_2_way")
    unsafe = _active_yaml("mono", 2, {1}).replace(
        "clip_limit: -12.0000",
        "clip_limit: 6.0000",
    )

    graph = classify_camilla_graph(topology=topology, text=unsafe)

    assert graph.allowed is False
    assert "active_graph_tweeter_guard_missing" in {
        issue["code"] for issue in graph.issues
    }


def test_active_graph_rejects_unassigned_unmuted_output() -> None:
    topology = _active_topology("mono", "active_2_way")
    unsafe = _active_yaml("mono", 2, frozenset()).replace(
        "channels: 2\n    device:",
        "channels: 3\n    device:",
    ).replace(
        "channels: { in: 2, out: 2 }",
        "channels: { in: 2, out: 3 }",
    )
    unsafe = unsafe.replace(
        "  as_out1_commission_mute:\n"
        "    type: Gain\n"
        "    parameters: { gain: -120.0000, inverted: false, mute: true }\n",
        "  as_out1_commission_mute:\n"
        "    type: Gain\n"
        "    parameters: { gain: -120.0000, inverted: false, mute: true }\n"
        "  as_out2_commission_mute:\n"
        "    type: Gain\n"
        "    parameters: { gain: 0.0000, inverted: false, mute: false }\n",
    )
    unsafe = unsafe + (
        "  - type: Filter\n"
        "    channels: [2]\n"
        "    names: [as_out2_commission_mute]\n"
    )

    graph = classify_camilla_graph(topology=topology, text=unsafe)

    assert graph.allowed is False
    assert "active_graph_unmutes_unknown_outputs" in {
        issue["code"] for issue in graph.issues
    }


def test_mono_active_3way_rejects_flat_and_allows_all_muted_startup() -> None:
    topology = _active_topology("mono", "active_3_way")
    flat = classify_camilla_graph(topology=topology, text=_flat_yaml())
    startup = classify_camilla_graph(
        topology=topology,
        text=_active_yaml("mono", 3, frozenset()),
    )

    assert classify_output_contract(topology).classification == CONTRACT_ACTIVE_MONO_3WAY
    assert flat.allowed is False
    assert startup.allowed is True
    assert startup.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP


def test_stereo_active_2way_rejects_flat_and_requires_all_tweeters_guarded() -> None:
    topology = _active_topology("stereo", "active_2_way")
    flat = classify_camilla_graph(topology=topology, text=_flat_yaml())
    startup = classify_camilla_graph(
        topology=topology,
        text=_active_yaml("stereo", 2, frozenset()),
    )

    assert classify_output_contract(topology).classification == CONTRACT_ACTIVE_STEREO_2WAY
    assert flat.allowed is False
    assert startup.allowed is True
    assert startup.details["tweeter_outputs"] == [1, 3]


def test_stereo_active_3way_rejects_flat_and_allows_all_muted_startup() -> None:
    topology = _active_topology("stereo", "active_3_way")
    flat = classify_camilla_graph(topology=topology, text=_flat_yaml())
    startup = classify_camilla_graph(
        topology=topology,
        text=_active_yaml("stereo", 3, frozenset()),
    )

    assert classify_output_contract(topology).classification == CONTRACT_ACTIVE_STEREO_3WAY
    assert flat.allowed is False
    assert startup.allowed is True


def test_subwoofer_topology_is_roleful_and_fails_closed() -> None:
    topology = _subwoofer_topology()
    contract = classify_output_contract(topology)
    graph = classify_camilla_graph(topology=topology, text=_flat_yaml())

    assert contract.classification == CONTRACT_SUBWOOFER_PRESENT
    assert contract.requires_roleful_graph is True
    assert graph.allowed is False


def test_safe_graph_decision_selects_staged_active_startup(tmp_path: Path) -> None:
    topology = _active_topology("mono", "active_2_way")
    staged_path = tmp_path / "active_speaker_staged_startup.yml"
    staged_path.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    flat_path = tmp_path / "outputd-cutover.yml"
    flat_path.write_text(_flat_yaml(), encoding="utf-8")

    decision = safe_graph_for_current_topology(
        topology,
        current_config_path=flat_path,
        flat_config_path=flat_path,
        staged_config=_staged_metadata(topology, staged_path),
    )

    assert decision.status == "select_active_startup"
    assert decision.selected_config_path == str(staged_path)


def test_safe_graph_decision_does_not_persist_guarded_commissioning(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    current_path = tmp_path / "tweeter_commission.yml"
    current_path.write_text(_active_yaml("mono", 2, {1}), encoding="utf-8")
    staged_path = tmp_path / "active_speaker_staged_startup.yml"
    staged_path.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")

    decision = safe_graph_for_current_topology(
        topology,
        current_config_path=current_path,
        staged_config=_staged_metadata(topology, staged_path),
    )

    assert decision.status == "select_active_startup"
    assert decision.current_graph is not None
    assert decision.current_graph.classification == GRAPH_GUARDED_COMMISSIONING
    assert decision.selected_config_path == str(staged_path)


def test_safe_graph_decision_preserves_approved_active_baseline(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    current_path = tmp_path / "active_speaker_baseline.yml"
    current_path.write_text(_active_baseline_yaml("mono", 2), encoding="utf-8")
    staged_path = tmp_path / "active_speaker_staged_startup.yml"
    staged_path.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")

    decision = safe_graph_for_current_topology(
        topology,
        current_config_path=current_path,
        staged_config=_staged_metadata(topology, staged_path),
    )

    assert decision.status == "preserve_current"
    assert decision.current_graph is not None
    assert decision.current_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert decision.selected_config_path == str(current_path)


def test_safe_graph_decision_prefers_applied_baseline_over_staged_current(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    current_path = tmp_path / "active_speaker_staged_startup.yml"
    current_path.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    baseline_path = tmp_path / "active_speaker_baseline.yml"
    baseline_path.write_text(_active_baseline_yaml("mono", 2), encoding="utf-8")

    decision = safe_graph_for_current_topology(
        topology,
        current_config_path=current_path,
        preferred_config_path=baseline_path,
        staged_config=_staged_metadata(topology, current_path),
    )

    assert decision.status == "select_active_baseline"
    assert decision.current_graph is not None
    assert decision.current_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    assert decision.preferred_graph is not None
    assert decision.preferred_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert decision.selected_config_path == str(baseline_path)


def test_safe_graph_decision_blocks_active_topology_without_staged_graph(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    flat_path = tmp_path / "outputd-cutover.yml"
    flat_path.write_text(_flat_yaml(), encoding="utf-8")

    decision = safe_graph_for_current_topology(
        topology,
        current_config_path=flat_path,
        flat_config_path=flat_path,
        staged_config={},
    )

    assert decision.status == "blocked"
    assert decision.ok is False
    assert "active_startup_graph_missing" in {
        issue["code"] for issue in decision.issues
    }


def test_statefile_repair_preserves_existing_volume_and_mute(tmp_path: Path) -> None:
    topology = _full_range_stereo()
    current = tmp_path / "missing.yml"
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(
        f"config_path: {current}\n"
        "mute:\n"
        "- true\n"
        "- false\n"
        "volume: -20.0\n",
        encoding="utf-8",
    )
    decision = safe_graph_for_current_topology(
        topology,
        statefile_path=statefile,
        flat_config_path=flat,
        staged_config={},
    )

    assert apply_safe_graph_decision_to_statefile(
        decision,
        statefile_path=statefile,
    ) is True
    repaired = statefile.read_text(encoding="utf-8")
    assert f"config_path: {flat}" in repaired
    assert "volume: -20.0" in repaired
    assert "- true" in repaired
