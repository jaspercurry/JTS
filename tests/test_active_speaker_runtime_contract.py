# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest
import yaml

from pathlib import Path

import jasper.active_speaker.runtime_contract as runtime_contract_module
from jasper.active_speaker import (
    ACTIVE_PROGRAM_BAKE_SOURCE,
    ActiveSpeakerPreset,
    emit_active_speaker_baseline_config,
    emit_active_speaker_commissioning_config,
    emit_active_speaker_driver_domain_config,
    emit_active_speaker_program_bake_config,
)
from jasper.active_speaker.camilla_yaml import STARTUP_MUTE_GAIN_DB
from jasper.active_speaker.runtime_contract import (
    ACTIVE_DRIVER_DOMAIN_SOURCE,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_DRIVER_DOMAIN_BASELINE,
    GRAPH_FLAT_FULL_RANGE,
    GRAPH_GUARDED_COMMISSIONING,
    GRAPH_PROGRAM_BAKE_PIPE,
    GRAPH_UNSAFE,
    CONTRACT_ACTIVE_MONO_2WAY,
    CONTRACT_ACTIVE_MONO_3WAY,
    CONTRACT_ACTIVE_STEREO_2WAY,
    CONTRACT_ACTIVE_STEREO_3WAY,
    CONTRACT_NORMAL_MONO_FULL_RANGE,
    CONTRACT_NORMAL_STEREO_FULL_RANGE,
    CONTRACT_SUBWOOFER_PRESENT,
    classify_camilla_graph as _classify_camilla_graph,
    classify_output_contract,
    apply_safe_graph_decision_to_statefile,
    flat_program_graph_blocked_reason,
    safe_graph_for_current_topology,
    NO_BASS_EXTENSION_PROFILE_SUMMARY,
    classify_active_bass_extension_graph,
    classify_bass_extension_graph,
)
from jasper.audio_measurement.evidence_identity import ExactDspStateIdentity
from jasper.bass_extension import _intent_payload
from jasper.bass_extension.profile import save_bass_extension_profile
from jasper.camilla_config_contract import (
    FilterSpec,
    PeqFilter,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
from jasper.sound.profile import SimpleEq, SoundProfile

from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset
from tests.test_bass_extension_profile import _applied_baseline, _profile

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"


def classify_camilla_graph(*args, **kwargs):
    """Frozen in-memory verifier input with explicit no-profile evidence."""

    kwargs.setdefault(
        "bass_profile_summary", NO_BASS_EXTENSION_PROFILE_SUMMARY
    )
    return _classify_camilla_graph(*args, **kwargs)


def _write_authority(
    tmp_path: Path,
    *,
    staged: dict | None = None,
    applied_config: Path | None = None,
) -> dict[str, Path]:
    applied = tmp_path / "applied-baseline.json"
    if applied_config is not None:
        applied.write_text(
            json.dumps({"config": {"path": str(applied_config)}}),
            encoding="utf-8",
        )
    staged_path = tmp_path / "staged-metadata.json"
    staged_path.write_text(json.dumps(staged or {}), encoding="utf-8")
    return {
        "applied_baseline_path": applied,
        "profile_path": tmp_path / "bass-profile.json",
        "intent_path": tmp_path / "bass-intent.json",
        "staged_metadata_path": staged_path,
    }


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


def _active_baseline_yaml(
    layout: str,
    way: int,
    *,
    room_peqs: tuple[PeqFilter, ...] = (),
    preference_filters: tuple[FilterSpec, ...] = (),
    output_trim_db: float = 0.0,
    bass_extension_profile=None,
) -> str:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return emit_active_speaker_baseline_config(
        ActiveSpeakerPreset.from_mapping(raw),
        playback_device=ACTIVE_PCM,
        room_peqs=room_peqs,
        preference_filters=preference_filters,
        output_trim_db=output_trim_db,
        baseline_id=f"baseline-{layout}-{way}way",
        bass_extension_profile=bass_extension_profile,
    )


def _driver_domain_yaml(
    layout: str, way: int, *, channel: str = "left", pair_trim_db: float = 0.0,
) -> str:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return emit_active_speaker_driver_domain_config(
        ActiveSpeakerPreset.from_mapping(raw),
        playback_device=ACTIVE_PCM,
        program_channel=channel,
        pair_trim_db=pair_trim_db,
        baseline_id=f"follower-{layout}-{way}way",
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


def _classify_staged_active(topology: OutputTopology, text: str):
    config_path = Path("/var/lib/camilladsp/configs/test-staged-active.yml")
    return classify_camilla_graph(
        topology=topology,
        text=text,
        config_path=str(config_path),
        staged_config=_staged_metadata(topology, config_path),
    )


def _persisted_boundary(
    tmp_path: Path,
    *,
    topology: OutputTopology,
    graph_text: str,
    profile=None,
) -> dict[str, object]:
    config = tmp_path / "active-speaker-baseline.yml"
    config.write_text(graph_text, encoding="utf-8")
    applied = _applied_baseline()
    applied["status"] = "applied"
    applied["config"] = {"path": str(config)}
    applied_path = tmp_path / "applied-baseline.json"
    applied_path.write_text(json.dumps(applied), encoding="utf-8")
    profile_path = tmp_path / "bass-profile.json"
    if profile is not None:
        save_bass_extension_profile(profile, profile_path)
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(
        f"config_path: {config}\nvolume: -18.0\nmute: false\n",
        encoding="utf-8",
    )
    staged_path = tmp_path / "staged-metadata.json"
    staged_path.write_text("{}\n", encoding="utf-8")
    return {
        "topology": topology,
        "config": config,
        "applied": applied,
        "applied_baseline_path": applied_path,
        "profile_path": profile_path,
        "intent_path": tmp_path / "bass-intent.json",
        "staged_metadata_path": staged_path,
        "statefile_path": statefile,
    }


def _sealed_profile(topology: OutputTopology, applied: dict):
    return replace(
        _profile(topology=topology, applied_baseline=applied),
        bass_owner={"kind": "woofer_way", "roles": ["woofer"], "channels": [0]},
    )


def test_low_level_baseline_without_bass_authority_fails_closed() -> None:
    graph = _classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"),
        text=_active_baseline_yaml("mono", 2),
    )

    assert graph.allowed is False
    assert "bass_extension_evidence_missing" in {
        issue["code"] for issue in graph.issues
    }


def test_persisted_boot_boundary_accepts_stable_no_profile_baseline(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert graph.allowed is True
    assert graph.details["bass_extension_profile_summary"] == (
        NO_BASS_EXTENSION_PROFILE_SUMMARY
    )


@pytest.mark.parametrize(
    ("audible_outputs", "graph_class"),
    [
        (frozenset(), GRAPH_ALL_MUTED_ACTIVE_STARTUP),
        (frozenset({0}), GRAPH_GUARDED_COMMISSIONING),
    ],
)
@pytest.mark.parametrize(
    ("authority_mutation", "expected_issue"),
    [
        ("missing", "active_staged_metadata_missing"),
        ("locator", "active_staged_locator_mismatch"),
        ("topology", "active_staged_metadata_mismatch"),
        ("guard", "active_staged_guard_not_ready"),
    ],
)
def test_persisted_guarded_graphs_require_complete_staged_authority(
    tmp_path: Path,
    audible_outputs: frozenset[int],
    graph_class: str,
    authority_mutation: str,
    expected_issue: str,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_yaml("mono", 2, audible_outputs),
    )
    staged_path = authority["staged_metadata_path"]
    if authority_mutation == "missing":
        staged_path.unlink()
    else:
        staged = _staged_metadata(topology, authority["config"])
        if authority_mutation == "locator":
            staged["config"]["path"] = str(tmp_path / "other.yml")
        elif authority_mutation == "topology":
            staged["topology"]["topology_id"] = "wrong-topology"
        else:
            staged["software_guard"]["passed"] = False
        staged_path.write_text(json.dumps(staged), encoding="utf-8")

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=staged_path,
    )

    assert graph.allowed is False, graph_class
    assert expected_issue in {issue["code"] for issue in graph.issues}


def test_desired_boundary_is_disk_free_and_rejects_persisted_paths(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    profile = _sealed_profile(topology, applied)
    text = _active_baseline_yaml(
        "mono", 2, bass_extension_profile=profile
    )

    accepted = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=text,
        applied_baseline_state=applied,
        desired_profile=profile,
    )
    refused = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=text,
        applied_baseline_state=applied,
        desired_profile=profile,
        profile_path=tmp_path / "must-not-be-read.json",
    )

    assert accepted.allowed is True
    assert refused.allowed is False
    assert refused.issues[0]["code"] == "bass_extension_source_invalid"


def test_desired_boundary_distinguishes_explicit_no_profile_from_omission() -> None:
    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    text = _active_baseline_yaml("mono", 2)

    explicit_none = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=text,
        applied_baseline_state=applied,
        desired_profile=None,
    )
    omitted = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=text,
        applied_baseline_state=applied,
    )
    invalid = classify_bass_extension_graph(
        topology,
        evidence_source="desired",
        graph_text=text,
        applied_baseline_state=applied,
        desired_profile=object(),
    )

    assert explicit_none.allowed is True
    assert omitted.allowed is False
    assert omitted.issues[0]["code"] == "bass_extension_source_invalid"
    assert invalid.allowed is False
    assert invalid.issues[0]["code"] == "bass_extension_source_invalid"


def test_persisted_boundaries_reject_explicit_desired_profile_evidence(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )

    boot = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
        desired_profile=None,
    )
    candidate = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_candidate",
        candidate_kind="explicit",
        candidate_path=authority["config"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
        desired_profile=None,
    )

    assert boot.allowed is False
    assert boot.issues[0]["code"] == "bass_extension_source_invalid"
    assert candidate.allowed is False
    assert candidate.issues[0]["code"] == "bass_extension_source_invalid"


@pytest.mark.parametrize("candidate_kind", ["explicit", "applied_baseline"])
def test_persisted_candidate_boundary_derives_only_declared_provenance(
    tmp_path: Path,
    candidate_kind: str,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )
    candidate_path = (
        authority["config"] if candidate_kind == "explicit" else None
    )

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_candidate",
        candidate_kind=candidate_kind,
        candidate_path=candidate_path,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )
    invalid = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_candidate",
        candidate_kind="applied_baseline",
        candidate_path=authority["config"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert graph.allowed is True
    assert invalid.allowed is False
    assert invalid.issues[0]["code"] == "bass_extension_candidate_invalid"


@pytest.mark.parametrize(
    "authority_key",
    [
        "applied_baseline_path",
        "profile_path",
        "intent_path",
        "staged_metadata_path",
    ],
    ids=["applied-baseline", "profile", "intent", "staged-metadata"],
)
def test_persisted_boundary_retries_once_then_refuses_unstable_authority(
    tmp_path: Path,
    monkeypatch,
    authority_key: str,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )
    changing_path = authority[authority_key]
    calls = 0

    from jasper.active_speaker import runtime_contract as contract_module

    real_read = contract_module._read_optional_bytes

    def alternating_read(path: Path):
        nonlocal calls
        if path == changing_path:
            calls += 1
            return b"first" if calls % 2 else b"second"
        return real_read(path)

    monkeypatch.setattr(contract_module, "_read_optional_bytes", alternating_read)
    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert calls == 4
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_snapshot_unstable"


@pytest.mark.parametrize(
    "mutation",
    ["selected_graph", "selector"],
    ids=["selected-graph", "selector-target"],
)
def test_persisted_boundary_refuses_selected_authority_mutation(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    text = _active_baseline_yaml("mono", 2)
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=text,
    )
    alternate = tmp_path / "alternate-active-speaker-baseline.yml"
    alternate.write_text(text, encoding="utf-8")
    selected_path = authority["config"]
    statefile_path = authority["statefile_path"]
    selected_bytes = selected_path.read_bytes()
    real_read = Path.read_bytes
    calls = 0

    def alternating_read(path: Path) -> bytes:
        nonlocal calls
        target = selected_path if mutation == "selected_graph" else statefile_path
        if path != target:
            return real_read(path)
        calls += 1
        if mutation == "selected_graph":
            suffix = b"# first\n" if calls % 2 else b"# second\n"
            return selected_bytes + suffix
        selected = selected_path if calls % 2 else alternate
        return f"config_path: {selected}\nvolume: -18.0\nmute: false\n".encode()

    monkeypatch.setattr(Path, "read_bytes", alternating_read)
    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=statefile_path,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert calls == 4
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_snapshot_unstable"


@pytest.mark.parametrize(
    ("candidate_kind", "mutation"),
    [
        ("explicit", "bytes"),
        ("applied_baseline", "bytes"),
        ("applied_baseline", "locator"),
        ("staged_all_muted", "bytes"),
        ("staged_all_muted", "locator"),
    ],
)
def test_persisted_candidate_boundary_refuses_each_mutating_seam(
    tmp_path: Path,
    monkeypatch,
    candidate_kind: str,
    mutation: str,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    text = _active_baseline_yaml("mono", 2)
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=text,
    )
    selected_path = authority["config"]
    authority["staged_metadata_path"].write_text(
        json.dumps(_staged_metadata(topology, selected_path)),
        encoding="utf-8",
    )
    alternate = tmp_path / "alternate-candidate.yml"
    alternate.write_text(text, encoding="utf-8")
    calls = 0

    if mutation == "bytes":
        selected_bytes = selected_path.read_bytes()
        real_read = Path.read_bytes

        def alternating_read(path: Path) -> bytes:
            nonlocal calls
            if path != selected_path:
                return real_read(path)
            calls += 1
            suffix = b"# first\n" if calls % 2 else b"# second\n"
            return selected_bytes + suffix

        monkeypatch.setattr(Path, "read_bytes", alternating_read)
    else:
        from jasper.active_speaker import runtime_contract as contract_module

        def alternating_locator(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return selected_path if calls % 2 else alternate

        monkeypatch.setattr(
            contract_module,
            "_candidate_locator",
            alternating_locator,
        )

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_candidate",
        candidate_kind=candidate_kind,
        candidate_path=selected_path if candidate_kind == "explicit" else None,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert calls == 4
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_snapshot_unstable"


@pytest.mark.parametrize(
    "candidate_kind",
    ["explicit", "applied_baseline", "staged_all_muted"],
)
@pytest.mark.parametrize(
    "authority_key",
    [
        "applied_baseline_path",
        "profile_path",
        "intent_path",
        "staged_metadata_path",
    ],
    ids=["applied-baseline", "profile", "intent", "staged-metadata"],
)
def test_persisted_candidate_refuses_each_authority_mutation(
    tmp_path: Path,
    monkeypatch,
    candidate_kind: str,
    authority_key: str,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    text = _active_baseline_yaml("mono", 2)
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=text,
    )
    selected_path = authority["config"]
    authority["staged_metadata_path"].write_text(
        json.dumps(_staged_metadata(topology, selected_path)),
        encoding="utf-8",
    )
    changing_path = authority[authority_key]
    first = {"snapshot_marker": "first"}
    second = {"snapshot_marker": "second"}
    if authority_key == "applied_baseline_path":
        first = {**authority["applied"], **first}
        second = {**authority["applied"], **second}
    elif authority_key == "staged_metadata_path":
        staged = _staged_metadata(topology, selected_path)
        first = {**staged, **first}
        second = {**staged, **second}
    alternating_bytes = (
        json.dumps(first).encode("utf-8"),
        json.dumps(second).encode("utf-8"),
    )
    calls = 0

    from jasper.active_speaker import runtime_contract as contract_module

    real_read = contract_module._read_optional_bytes

    def alternating_read(path: Path):
        nonlocal calls
        if path != changing_path:
            return real_read(path)
        value = alternating_bytes[calls % 2]
        calls += 1
        return value

    monkeypatch.setattr(contract_module, "_read_optional_bytes", alternating_read)
    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_candidate",
        candidate_kind=candidate_kind,
        candidate_path=selected_path if candidate_kind == "explicit" else None,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert calls == 4
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_snapshot_unstable"


def test_persisted_boundary_refuses_embedded_nul_locator_without_raising(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )
    authority["statefile_path"].write_text(
        "config_path: bad\x00path\nvolume: -18.0\nmute: false\n",
        encoding="utf-8",
    )

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_snapshot_unstable"


async def test_live_boundary_keeps_readback_inside_whole_snapshot_sandwich(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    text = _active_baseline_yaml("mono", 2)
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=text,
    )
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        authority["statefile_path"].write_text(
            f"config_path: {authority['config']}\nvolume: -21.0\nmute: false\n",
            encoding="utf-8",
        )
        return "\n".join(
            line for line in text.splitlines() if not line.startswith("#")
        ) + "\n"

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 1
    assert graph.allowed is True


@pytest.mark.parametrize(
    "authority_key",
    [
        "applied_baseline_path",
        "profile_path",
        "intent_path",
        "staged_metadata_path",
    ],
    ids=["applied-baseline", "profile", "intent", "staged-metadata-guard"],
)
async def test_live_boundary_refuses_each_authority_mutation_across_await(
    tmp_path: Path,
    authority_key: str,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    text = _active_baseline_yaml("mono", 2)
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=text,
    )
    alternate = tmp_path / "alternate-active-speaker-baseline.yml"
    alternate.write_text(text, encoding="utf-8")
    changing_path = authority[authority_key]
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        if authority_key == "applied_baseline_path":
            applied = dict(authority["applied"])
            applied["config"] = {
                "path": str(
                    alternate if callback_count % 2 else authority["config"]
                )
            }
            changing_path.write_text(json.dumps(applied), encoding="utf-8")
        elif authority_key == "staged_metadata_path":
            staged = _staged_metadata(topology, authority["config"])
            staged["software_guard"]["passed"] = callback_count % 2 == 0
            changing_path.write_text(json.dumps(staged), encoding="utf-8")
        else:
            changing_path.write_text(
                json.dumps({"mutation": callback_count}),
                encoding="utf-8",
            )
        return text

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 2
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


async def test_live_boundary_refuses_mismatched_live_yaml_as_unstable(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    text = _active_baseline_yaml("mono", 2)
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=text,
    )
    live_payload = yaml.safe_load(text)
    live_payload["devices"]["samplerate"] += 1
    mismatched_live = yaml.safe_dump(live_payload, sort_keys=False)
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        return mismatched_live

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 2
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


@pytest.mark.parametrize(
    "live_result",
    [None, b"not-text", "filters: [\n"],
    ids=["none", "non-text", "malformed-yaml"],
)
async def test_live_boundary_refuses_invalid_live_result_with_stable_selected_file(
    tmp_path: Path,
    live_result,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )
    callback_count = 0

    async def active_readback():
        nonlocal callback_count
        callback_count += 1
        return live_result

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 2
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


async def test_live_boundary_refuses_changed_selector_as_unstable(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    text = _active_baseline_yaml("mono", 2)
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=text,
    )
    alternate = tmp_path / "alternate-active-speaker-baseline.yml"
    alternate.write_text(text, encoding="utf-8")
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        selected = alternate if callback_count % 2 else authority["config"]
        authority["statefile_path"].write_text(
            f"config_path: {selected}\nvolume: -18.0\nmute: false\n",
            encoding="utf-8",
        )
        return text

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 2
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


async def test_live_boundary_refuses_changed_selected_file_as_unstable(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    text = _active_baseline_yaml("mono", 2)
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=text,
    )
    selected_path = authority["config"]
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        before = selected_path.read_text(encoding="utf-8")
        after = text if before != text else text + "# snapshot churn\n"
        selected_path.write_text(after, encoding="utf-8")
        return before

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 2
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


async def test_live_boundary_refuses_unparseable_selected_file_as_unstable(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )
    broken = "filters: [\n"
    authority["config"].write_text(broken, encoding="utf-8")
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        return broken

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 2
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


async def test_live_boundary_refuses_embedded_nul_locator_without_raising(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )
    authority["statefile_path"].write_text(
        "config_path: bad\x00path\nvolume: -18.0\nmute: false\n",
        encoding="utf-8",
    )
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        return _active_baseline_yaml("mono", 2)

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 0
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


async def test_live_boundary_refuses_recursive_yaml_without_raising(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    recursive = "graph: &graph [*graph]\n"
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=recursive,
    )
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        return recursive

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 2
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


async def test_live_boundary_preserves_stable_selected_classifier_issue(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    selected_text = "\n".join(
        line
        for line in _active_baseline_yaml("mono", 2).splitlines()
        if not line.startswith("# Source:")
    ) + "\n"
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=selected_text,
    )

    synchronous = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )
    active = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=lambda: asyncio.sleep(0, result=selected_text),
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert synchronous.allowed is False
    assert active.allowed is False
    assert active.issues[0]["code"] == synchronous.issues[0]["code"]
    assert active.issues[0]["code"] != "bass_extension_active_snapshot_unstable"


async def test_live_boundary_fails_closed_on_arbitrary_reader_error(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )
    callback_count = 0

    async def active_readback() -> str:
        nonlocal callback_count
        callback_count += 1
        raise LookupError("untrusted live-reader failure")

    graph = await classify_active_bass_extension_graph(
        topology,
        statefile_path=authority["statefile_path"],
        read_active_graph_text=active_readback,
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert callback_count == 2
    assert graph.allowed is False
    assert graph.issues[0]["code"] == "bass_extension_active_snapshot_unstable"


async def test_live_boundary_propagates_reader_cancellation(tmp_path: Path) -> None:
    topology = _active_topology("mono", "active_2_way")
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=_active_baseline_yaml("mono", 2),
    )

    async def active_readback() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await classify_active_bass_extension_graph(
            topology,
            statefile_path=authority["statefile_path"],
            read_active_graph_text=active_readback,
            applied_baseline_path=authority["applied_baseline_path"],
            profile_path=authority["profile_path"],
            intent_path=authority["intent_path"],
            staged_metadata_path=authority["staged_metadata_path"],
        )


def test_pending_intent_authorizes_only_recorded_graph_profile_pair(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    profile = _sealed_profile(topology, applied)
    predecessor = _active_baseline_yaml("mono", 2).encode()
    desired = _active_baseline_yaml(
        "mono", 2, bass_extension_profile=profile
    ).encode()
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=desired.decode(),
        profile=profile,
    )
    authority["applied_baseline_path"].write_text(
        json.dumps({**applied, "status": "applied", "config": {"path": str(authority["config"])}}),
        encoding="utf-8",
    )
    profile_bytes = authority["profile_path"].read_bytes()
    intent = _intent_payload(
        predecessor_identity=ExactDspStateIdentity(
            {"config_path": str(authority["config"]), "graph": "predecessor"}
        ),
        predecessor_profile_bytes=None,
        desired_profile_bytes=profile_bytes,
        selected_path=authority["config"],
        selected_mode=0o640,
        predecessor_graph_bytes=predecessor,
        desired_graph_bytes=desired,
        selector_target=authority["config"],
    )
    authority["intent_path"].write_text(json.dumps(intent), encoding="utf-8")

    accepted = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )
    intent["graphs"]["desired"] = "0" * 64
    authority["intent_path"].write_text(json.dumps(intent), encoding="utf-8")
    refused = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert accepted.allowed is True
    assert refused.allowed is False
    assert "bass_extension_authority_invalid" in {
        issue["code"] for issue in refused.issues
    }


@pytest.mark.parametrize(
    "malformed_field",
    ["desired_profile_bytes", "desired_graph_bytes"],
)
def test_pending_intent_refuses_unpaired_surrogate_without_raising(
    tmp_path: Path,
    malformed_field: str,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    applied = _applied_baseline()
    profile = _sealed_profile(topology, applied)
    predecessor = _active_baseline_yaml("mono", 2).encode()
    desired = _active_baseline_yaml(
        "mono", 2, bass_extension_profile=profile
    ).encode()
    authority = _persisted_boundary(
        tmp_path,
        topology=topology,
        graph_text=desired.decode(),
        profile=profile,
    )
    authority["applied_baseline_path"].write_text(
        json.dumps({
            **applied,
            "status": "applied",
            "config": {"path": str(authority["config"])},
        }),
        encoding="utf-8",
    )
    profile_bytes = authority["profile_path"].read_bytes()
    intent = _intent_payload(
        predecessor_identity=ExactDspStateIdentity(
            {"config_path": str(authority["config"]), "graph": "predecessor"}
        ),
        predecessor_profile_bytes=None,
        desired_profile_bytes=profile_bytes,
        selected_path=authority["config"],
        selected_mode=0o640,
        predecessor_graph_bytes=predecessor,
        desired_graph_bytes=desired,
        selector_target=authority["config"],
    )
    if malformed_field == "desired_profile_bytes":
        intent["profiles"]["desired"]["bytes"] = "\ud800"
    else:
        intent["config"]["desired_bytes"] = "\ud800"
    authority["intent_path"].write_text(json.dumps(intent), encoding="utf-8")

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=authority["statefile_path"],
        applied_baseline_path=authority["applied_baseline_path"],
        profile_path=authority["profile_path"],
        intent_path=authority["intent_path"],
        staged_metadata_path=authority["staged_metadata_path"],
    )

    assert graph.allowed is False
    assert "bass_extension_authority_invalid" in {
        issue["code"] for issue in graph.issues
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
        **_write_authority(tmp_path),
    )

    assert classify_output_contract(topology).classification == CONTRACT_NORMAL_MONO_FULL_RANGE
    assert decision.status == "blocked"
    assert "flat_full_range_graph_wider_than_topology" in {
        issue["code"] for issue in decision.issues
    }


def test_mono_active_2way_rejects_flat_and_allows_guarded_graphs() -> None:
    topology = _active_topology("mono", "active_2_way")

    flat = classify_camilla_graph(topology=topology, text=_flat_yaml())
    startup = _classify_staged_active(
        topology, _active_yaml("mono", 2, frozenset())
    )
    woofer = _classify_staged_active(topology, _active_yaml("mono", 2, {0}))
    tweeter = _classify_staged_active(topology, _active_yaml("mono", 2, {1}))

    assert classify_output_contract(topology).classification == CONTRACT_ACTIVE_MONO_2WAY
    assert flat.allowed is False
    assert "flat_full_range_graph_illegal" in flat.issues[0]["code"]
    assert startup.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    assert startup.allowed is True
    assert woofer.classification == GRAPH_GUARDED_COMMISSIONING
    assert woofer.allowed is True
    assert tweeter.classification == GRAPH_GUARDED_COMMISSIONING
    assert tweeter.allowed is True


@pytest.mark.parametrize("channel", [0, 1], ids=["woofer", "tweeter"])
def test_guarded_commissioning_rejects_post_mute_filter(channel: int) -> None:
    base = _active_yaml("mono", 2, {channel})
    payload = yaml.safe_load(base)
    payload["filters"]["forged_post_mute_peq"] = {
        "type": "Biquad",
        "parameters": {
            "type": "Peaking",
            "freq": 100.0,
            "q": 1.0,
            "gain": 60.0,
        },
    }
    payload["pipeline"].append({
        "type": "Filter",
        "channels": [channel],
        "names": ["forged_post_mute_peq"],
    })

    graph = classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"),
        text=_dump_baseline(base, payload),
    )

    assert graph.allowed is False
    assert "active_commissioning_chain_unrecognized" in _baseline_codes(graph)


def test_guarded_commissioning_rejects_second_active_split() -> None:
    base = _active_yaml("mono", 2, {0})
    payload = yaml.safe_load(base)
    payload["pipeline"].append({"type": "Mixer", "name": "split_active_2way"})

    graph = classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"),
        text=_dump_baseline(base, payload),
    )

    assert graph.allowed is False
    assert "active_graph_mixer_sequence_invalid" in _baseline_codes(graph)


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


# --- C3a-4: the active-baseline runtime graph's fail-closed protections ---
#
# When source == ACTIVE_BASELINE_SOURCE the classifier SUPPRESSES the
# commission-mute checks and instead treats every output as unmuted, validating
# them through a NEW block of per-driver blocker predicates
# (runtime_contract.py:1047-1291). That block is the ENTIRE fail-closed safety
# net for a tweeter-bearing active-baseline runtime graph, yet only the positive
# path (test_mono_active_2way_allows_approved_baseline_runtime) was pinned. These
# mutate the emitted baseline YAML to break one protection at a time and assert
# the classifier rejects it with the matching blocker — so a fail-OPEN regression
# (e.g. dropping the gain<=0 check) can't pass green. The baseline-source comment
# header is preserved by mutating text rather than parse->dump (the classifier
# reads the source from that comment; a YAML round-trip strips it).


def _baseline_codes(graph) -> set[str]:
    return {issue["code"] for issue in graph.issues}


def _classify_baseline(text: str):
    return classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"),
        text=text,
    )


def _dump_baseline(base: str, payload: dict) -> str:
    source = next(line for line in base.splitlines() if line.startswith("# Source:"))
    return f"{source}\n{yaml.safe_dump(payload, sort_keys=False)}"


def _isolated_baseline_yaml(
    layout: str,
    way: int,
    audible_outputs: set[int],
) -> str:
    base = _active_baseline_yaml(layout, way)
    payload = yaml.safe_load(base)
    output_count = way if layout == "mono" else way * 2
    for index in range(output_count):
        name = f"as_out{index}_commission_mute"
        is_audible = index in audible_outputs
        payload["filters"][name] = {
            "type": "Gain",
            "parameters": {
                "gain": 0.0 if is_audible else STARTUP_MUTE_GAIN_DB,
                "inverted": False,
                "mute": not is_audible,
            },
        }
        payload["pipeline"].append(
            {"type": "Filter", "channels": [index], "names": [name]}
        )
    return _dump_baseline(base, payload)


def test_baseline_commissioning_allows_one_exact_adjacent_pair() -> None:
    topology = _active_topology("stereo", "active_3_way")

    graph = _classify_staged_active(
        topology,
        _isolated_baseline_yaml("stereo", 3, {1, 2}),
    )

    assert graph.allowed is True
    assert graph.classification == GRAPH_GUARDED_COMMISSIONING
    assert graph.details["baseline_candidate"] is False
    assert graph.details["baseline_commissioning_candidate"] is True
    assert graph.details["baseline_commissioning_group"] == "left"
    assert graph.details["baseline_commissioning_roles"] == ["mid", "tweeter"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "channel_drift",
        "parameter_drift",
        "tail_reorder",
        "tail_not_final",
    ],
)
def test_baseline_commissioning_refuses_mute_definition_or_tail_drift(
    mutation: str,
) -> None:
    topology = _active_topology("stereo", "active_3_way")
    base = _isolated_baseline_yaml("stereo", 3, {1, 2})
    payload = yaml.safe_load(base)
    if mutation == "missing":
        del payload["filters"]["as_out5_commission_mute"]
        payload["pipeline"] = payload["pipeline"][:-1]
    elif mutation == "extra":
        payload["filters"]["as_out6_commission_mute"] = {
            "type": "Gain",
            "parameters": {
                "gain": STARTUP_MUTE_GAIN_DB,
                "inverted": False,
                "mute": True,
            },
        }
        payload["pipeline"].append(
            {
                "type": "Filter",
                "channels": [6],
                "names": ["as_out6_commission_mute"],
            }
        )
    elif mutation == "channel_drift":
        payload["pipeline"][-3]["channels"] = [2]
    elif mutation == "parameter_drift":
        payload["filters"]["as_out0_commission_mute"]["parameters"][
            "gain"
        ] = STARTUP_MUTE_GAIN_DB + 1.0
    elif mutation == "tail_reorder":
        payload["pipeline"][-2:] = reversed(payload["pipeline"][-2:])
    else:
        payload["pipeline"].append(
            {
                "type": "Filter",
                "channels": [0],
                "names": ["as_woofer_limiter"],
            }
        )

    graph = classify_camilla_graph(
        topology=topology,
        text=_dump_baseline(base, payload),
    )

    assert graph.allowed is False
    assert any(
        code.startswith("active_baseline_commissioning_mute")
        for code in _baseline_codes(graph)
    )


@pytest.mark.parametrize("audible_outputs", [{0, 2}, {1, 5}])
def test_baseline_commissioning_refuses_nonadjacent_or_cross_group_pair(
    audible_outputs: set[int],
) -> None:
    graph = classify_camilla_graph(
        topology=_active_topology("stereo", "active_3_way"),
        text=_isolated_baseline_yaml("stereo", 3, audible_outputs),
    )

    assert graph.allowed is False
    assert "active_baseline_commissioning_target_invalid" in _baseline_codes(graph)


def test_baseline_headroom_unwired_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    tampered = base.replace(
        "names: [active_baseline_headroom]",
        "names: []",
    )
    assert tampered != base
    graph = _classify_baseline(tampered)

    assert graph.allowed is False
    assert "active_baseline_headroom_unwired" in _baseline_codes(graph)


def test_baseline_positive_headroom_gain_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    tampered = base.replace(
        "active_baseline_headroom:\n    type: Gain\n"
        "    parameters: { gain: 0.0000,",
        "active_baseline_headroom:\n    type: Gain\n"
        "    parameters: { gain: 2.0000,",
    )
    assert tampered != base
    graph = _classify_baseline(tampered)

    assert graph.allowed is False
    assert "active_baseline_headroom_invalid" in _baseline_codes(graph)


def test_baseline_missing_driver_chain_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    tampered = base.replace(
        "names: [as_woofer_woofer_tweeter_lp, as_woofer_delay, "
        "as_woofer_baseline_gain, as_woofer_baseline_limiter]",
        "names: [as_woofer_woofer_tweeter_lp, as_woofer_delay]",
    )
    assert tampered != base
    graph = _classify_baseline(tampered)

    assert graph.allowed is False
    assert "active_baseline_driver_chain_missing" in _baseline_codes(graph)


def test_baseline_positive_driver_gain_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    tampered = base.replace(
        "as_woofer_baseline_gain:\n    type: Gain\n"
        "    parameters: { gain: 0.0000,",
        "as_woofer_baseline_gain:\n    type: Gain\n"
        "    parameters: { gain: 3.0000,",
    )
    assert tampered != base
    graph = _classify_baseline(tampered)

    assert graph.allowed is False
    assert "active_baseline_gain_positive" in _baseline_codes(graph)


@pytest.mark.parametrize("gain", [60.0, float("nan")], ids=["positive", "nan"])
def test_baseline_appended_unsafe_post_split_gain_is_blocked(gain: float) -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["forged_post_limiter_gain"] = {
        "type": "Gain",
        "parameters": {"gain": gain, "inverted": False, "mute": False},
    }
    payload["pipeline"].append(
        {
            "type": "Filter",
            "channels": [0, 1],
            "names": ["forged_post_limiter_gain"],
        }
    )
    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_gain_positive" in _baseline_codes(graph)


def test_baseline_appended_post_limiter_peq_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["forged_post_limiter_peq"] = {
        "type": "Biquad",
        "parameters": {
            "type": "Peaking",
            "freq": 2000.0,
            "q": 1.0,
            "gain": 60.0,
        },
    }
    payload["pipeline"].append(
        {
            "type": "Filter",
            "channels": [0, 1],
            "names": ["forged_post_limiter_peq"],
        }
    )
    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_post_limiter_filter_unsafe" in _baseline_codes(graph)


def test_baseline_tweeter_boost_inserted_before_limiter_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["forged_tweeter_low_boost"] = {
        "type": "Biquad",
        "parameters": {
            "type": "Peaking",
            "freq": 100.0,
            "q": 1.0,
            "gain": 60.0,
        },
    }
    tweeter_names = payload["pipeline"][3]["names"]
    tweeter_names.insert(
        tweeter_names.index("as_tweeter_baseline_limiter"),
        "forged_tweeter_low_boost",
    )

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_driver_chain_unrecognized" in _baseline_codes(graph)


def test_baseline_canonical_delay_with_wrong_filter_type_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["as_tweeter_delay"] = {
        "type": "Gain",
        "parameters": {"gain": -3.0, "inverted": False, "mute": False},
    }

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_driver_chain_unrecognized" in _baseline_codes(graph)


def test_baseline_huge_integer_delay_is_blocked_without_raising() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["as_tweeter_delay"]["parameters"]["delay"] = 10**400

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_driver_chain_unrecognized" in _baseline_codes(graph)


def test_baseline_non_string_chain_name_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    tweeter_names = payload["pipeline"][3]["names"]
    tweeter_names.insert(
        tweeter_names.index("as_tweeter_baseline_limiter"),
        123,
    )

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_driver_chain_unrecognized" in _baseline_codes(graph)


@pytest.mark.parametrize("position", ["between", "after"])
def test_baseline_second_active_split_mixer_is_blocked(position: str) -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    forged = {"type": "Mixer", "name": "split_active_2way"}
    if position == "between":
        payload["pipeline"].insert(3, forged)
    else:
        payload["pipeline"].append(forged)

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_graph_mixer_sequence_invalid" in _baseline_codes(graph)


@pytest.mark.parametrize(
    "filter_type,value",
    [
        ("Gain", False),
        ("Gain", "0"),
        ("Delay", False),
        ("Delay", "0"),
    ],
    ids=["gain-bool", "gain-string", "delay-bool", "delay-string"],
)
def test_baseline_runtime_tail_requires_exact_numeric_type(
    filter_type: str,
    value: object,
) -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    name = "as_commission_malformed_tail"
    if filter_type == "Gain":
        parameters = {"gain": value, "inverted": False, "mute": False}
    else:
        parameters = {"delay": value, "unit": "ms"}
    payload["filters"][name] = {
        "type": filter_type,
        "parameters": parameters,
    }
    payload["pipeline"].append({
        "type": "Filter",
        "channels": [0],
        "names": [name],
    })

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_post_limiter_filter_unsafe" in _baseline_codes(graph)


def test_baseline_limiter_moved_before_split_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    limiter = "as_woofer_baseline_limiter"
    payload["pipeline"][2]["names"].remove(limiter)
    payload["pipeline"].insert(
        1,
        {"type": "Filter", "channels": [0], "names": [limiter]},
    )
    payload["filters"]["forged_post_split_peq"] = {
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
        "channels": [0],
        "names": ["forged_post_split_peq"],
    })

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_limiter_order_invalid" in _baseline_codes(graph)


def test_baseline_duplicate_post_split_limiter_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["pipeline"].append({
        "type": "Filter",
        "channels": [0],
        "names": ["as_woofer_baseline_limiter"],
    })

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_limiter_order_invalid" in _baseline_codes(graph)


def test_baseline_stacked_post_split_delays_exceeding_ceiling_are_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    for index in (1, 2):
        payload["filters"][f"as_commission_forged_delay_{index}"] = {
            "type": "Delay",
            "parameters": {"delay": 15.0, "unit": "ms"},
        }
    payload["pipeline"].append({
        "type": "Filter",
        "channels": [0],
        "names": [
            "as_commission_forged_delay_1",
            "as_commission_forged_delay_2",
        ],
    })

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_delay_ceiling_exceeded" in _baseline_codes(graph)


def test_baseline_delay_plus_runtime_tail_exceeding_ceiling_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["as_woofer_delay"]["parameters"]["delay"] = 10.0
    payload["filters"]["as_commission_forged_delay"] = {
        "type": "Delay",
        "parameters": {"delay": 15.0, "unit": "ms"},
    }
    payload["pipeline"].append({
        "type": "Filter",
        "channels": [0],
        "names": ["as_commission_forged_delay"],
    })

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_delay_ceiling_exceeded" in _baseline_codes(graph)


def test_baseline_oversize_delay_before_limiter_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["forged_pre_limiter_delay"] = {
        "type": "Delay",
        "parameters": {"delay": 30.0, "unit": "ms"},
    }
    payload["pipeline"][2]["names"].insert(0, "forged_pre_limiter_delay")

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_delay_ceiling_exceeded" in _baseline_codes(graph)


def test_baseline_quoted_channel_cannot_evade_post_limiter_check() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["forged_post_limiter_peq"] = {
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
        "channels": ["0"],
        "names": ["forged_post_limiter_peq"],
    })

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_post_limiter_filter_unsafe" in _baseline_codes(graph)


def test_baseline_mismatched_crossover_pair_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    payload = yaml.safe_load(base)
    payload["filters"]["as_tweeter_woofer_tweeter_hp"]["parameters"][
        "freq"
    ] = 1200.0

    graph = _classify_baseline(_dump_baseline(base, payload))

    assert graph.allowed is False
    assert "active_output_crossover_pair_mismatch" in _baseline_codes(graph)


def test_baseline_role_chain_must_remain_grouped() -> None:
    base = _active_baseline_yaml("stereo", 2)
    payload = yaml.safe_load(base)
    woofer_step = payload["pipeline"][2]
    assert woofer_step["channels"] == [0, 2]
    woofer_step["channels"] = [0]
    payload["pipeline"].insert(
        3,
        {
            "type": "Filter",
            "channels": [2],
            "names": list(woofer_step["names"]),
        },
    )

    graph = classify_camilla_graph(
        topology=_active_topology("stereo", "active_2_way"),
        text=_dump_baseline(base, payload),
    )

    assert graph.allowed is False
    assert "active_output_driver_chain_not_grouped" in _baseline_codes(graph)


@pytest.mark.parametrize(
    "channels",
    [["0", "2"], [0.9, 2.9], [0, 0]],
    ids=["quoted", "fractional", "duplicate"],
)
def test_baseline_grouped_chain_requires_exact_integer_channels(channels) -> None:
    base = _active_baseline_yaml("stereo", 2)
    payload = yaml.safe_load(base)
    payload["pipeline"][2]["channels"] = channels

    graph = classify_camilla_graph(
        topology=_active_topology("stereo", "active_2_way"),
        text=_dump_baseline(base, payload),
    )

    assert graph.allowed is False
    assert "active_output_driver_chain_not_grouped" in _baseline_codes(graph)


def test_baseline_positive_limiter_clip_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    tampered = base.replace(
        "as_woofer_baseline_limiter:\n    type: Limiter\n"
        "    parameters:\n      soft_clip: true\n      clip_limit: -1.0000",
        "as_woofer_baseline_limiter:\n    type: Limiter\n"
        "    parameters:\n      soft_clip: true\n      clip_limit: 1.0000",
    )
    assert tampered != base
    graph = _classify_baseline(tampered)

    assert graph.allowed is False
    assert "active_baseline_limiter_invalid" in _baseline_codes(graph)


def test_baseline_limiter_without_soft_clip_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    tampered = base.replace(
        "as_woofer_baseline_limiter:\n    type: Limiter\n"
        "    parameters:\n      soft_clip: true\n      clip_limit: -1.0000",
        "as_woofer_baseline_limiter:\n    type: Limiter\n"
        "    parameters:\n      soft_clip: false\n      clip_limit: -1.0000",
    )
    assert tampered != base
    graph = _classify_baseline(tampered)

    assert graph.allowed is False
    assert "active_baseline_limiter_invalid" in _baseline_codes(graph)


def test_baseline_non_limiter_drive_protection_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    tampered = base.replace(
        "as_woofer_baseline_limiter:\n    type: Limiter",
        "as_woofer_baseline_limiter:\n    type: Gain",
    )
    assert tampered != base
    graph = _classify_baseline(tampered)

    assert graph.allowed is False
    assert "active_baseline_limiter_invalid" in _baseline_codes(graph)


def test_baseline_tweeter_without_highpass_is_blocked() -> None:
    base = _active_baseline_yaml("mono", 2)
    tampered = base.replace(
        "names: [as_tweeter_woofer_tweeter_hp, as_tweeter_delay, "
        "as_tweeter_baseline_gain, as_tweeter_baseline_limiter]",
        "names: [as_tweeter_delay, as_tweeter_baseline_gain, "
        "as_tweeter_baseline_limiter]",
    )
    assert tampered != base
    graph = _classify_baseline(tampered)

    assert graph.allowed is False
    assert "active_baseline_tweeter_highpass_missing" in _baseline_codes(graph)


# --- PR-3: preference EQ rides at unity in the active baseline, pre-split ---

@pytest.mark.parametrize(
    "layout,mode,way",
    [
        ("mono", "active_2_way", 2),
        ("mono", "active_3_way", 3),
        ("stereo", "active_2_way", 2),
        ("stereo", "active_3_way", 3),
    ],
)
def test_baseline_with_preference_eq_stays_approved(layout, mode, way) -> None:
    # Keystone (invariant 2), emitter-level: emit the real active baseline with a
    # preference SHELF inserted pre-split, feed it back through the SAME classifier
    # for the SAME topology -> still GRAPH_APPROVED_ACTIVE_RUNTIME. Adding EQ
    # never breaks the protection contract. Cover stereo + 3-way because the
    # classifier's per-output name collection gathers the pre-split [0,1] pref
    # names into outputs 0/1 but not the right-channel/higher outputs — an
    # asymmetry that only manifests beyond mono-2-way.
    topology = _active_topology(layout, mode)
    prefs = (
        FilterSpec(name="pref_hs", biquad_type="Highshelf", freq=9000.0, gain=5.0, slope=6.0),
        FilterSpec(name="pref_pk", biquad_type="Peaking", freq=120.0, gain=3.0, q=1.0),
    )

    graph = classify_camilla_graph(
        topology=topology,
        text=_active_baseline_yaml(layout, way, preference_filters=prefs),
    )

    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert graph.allowed is True


def test_baseline_preference_boost_rides_at_unity() -> None:
    # Invariant 4 (emitter-side): a +N dB preference boost does not add hidden
    # program attenuation. It rides at unity just like the stereo /sound path,
    # while the active graph keeps it safe by placing EQ pre-split and preserving
    # volume_limit 0.0. Tested with a SHELF because that was the easy-to-get-wrong
    # bass/treble boost case.
    import re

    flat = _active_baseline_yaml("mono", 2)
    boosted = _active_baseline_yaml(
        "mono",
        2,
        preference_filters=(
            FilterSpec(name="pref_hs", biquad_type="Highshelf", freq=9000.0, gain=5.0, slope=6.0),
            FilterSpec(name="pref_pk", biquad_type="Peaking", freq=120.0, gain=3.0, q=1.0),
        ),
    )

    def _headroom_db(text: str) -> float:
        match = re.search(
            r"active_baseline_headroom:\n\s+type: Gain\n\s+parameters: \{ gain: (-?\d+\.\d+)",
            text,
        )
        assert match is not None
        return float(match.group(1))

    assert _headroom_db(flat) == 0.0
    assert _headroom_db(boosted) == 0.0
    assert "volume_limit: 0.0" in boosted


def test_baseline_room_peqs_pre_split_and_boost_headroom_folds() -> None:
    import re

    room_peqs = (
        PeqFilter(freq=45.0, q=5.0, gain=2.0),
        PeqFilter(freq=80.0, q=6.0, gain=-4.0),
        PeqFilter(freq=120.0, q=4.0, gain=1.0),
    )
    text = _active_baseline_yaml("mono", 2, room_peqs=room_peqs)

    assert "room_peq_1:" in text and "room_peq_2:" in text
    assert "room_peq_3:" in text
    assert "room_headroom" not in text
    match = re.search(
        r"active_baseline_headroom:\n\s+type: Gain\n\s+parameters: \{ gain: (-?\d+\.\d+)",
        text,
    )
    assert match is not None
    assert float(match.group(1)) == -3.0

    pipeline = text[text.index("\npipeline:"):]
    room_idx = pipeline.index("names: [room_peq_1, room_peq_2, room_peq_3]")
    headroom_idx = pipeline.index("names: [active_baseline_headroom]")
    mixer_idx = pipeline.index("type: Mixer")
    assert room_idx < headroom_idx < mixer_idx

    graph = classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"), text=text
    )
    assert graph.allowed is True, graph.issues
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME


def test_baseline_output_trim_folds_into_headroom_with_eq() -> None:
    # output_trim_db (manual headroom + loudness match) folds into the SAME
    # active_baseline_headroom gain, so the active path honours the household's
    # loudness setting exactly like emit_sound_config. It applies
    # ONLY when the profile has EQ (mirrors the stereo path): a flat profile
    # plays at unity and ignores the trim.
    import re

    def _headroom_db(text: str) -> float:
        match = re.search(
            r"active_baseline_headroom:\n\s+type: Gain\n\s+parameters: \{ gain: (-?\d+\.\d+)",
            text,
        )
        assert match is not None
        return float(match.group(1))

    prefs = (
        FilterSpec(name="pref_pk", biquad_type="Peaking", freq=2000.0, gain=2.0, q=1.0),
    )
    # With EQ: -(0 baseline + 4 trim) = -4. Preference boosts ride at unity.
    with_eq = _active_baseline_yaml("mono", 2, preference_filters=prefs, output_trim_db=4.0)
    assert _headroom_db(with_eq) == -4.0
    assert _headroom_db(with_eq) <= 0.0
    assert classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"), text=with_eq
    ).allowed is True

    # Flat profile: the trim is ignored (can't clip from EQ), headroom stays 0
    # and the config is byte-identical to no-trim — preserves the no-EQ contract.
    flat_no_trim = _active_baseline_yaml("mono", 2)
    flat_with_trim = _active_baseline_yaml("mono", 2, output_trim_db=4.0)
    assert _headroom_db(flat_with_trim) == 0.0
    assert flat_with_trim == flat_no_trim


def test_baseline_preference_step_is_before_split_mixer() -> None:
    # Invariant 5: the preference filter step is wired on the program channels
    # strictly BEFORE the split mixer (pipeline index of pref step < Mixer step).
    yaml = _active_baseline_yaml(
        "mono",
        2,
        preference_filters=(
            FilterSpec(name="pref_pk", biquad_type="Peaking", freq=2000.0, gain=2.0, q=1.0),
        ),
    )
    pipeline = yaml[yaml.index("\npipeline:"):]
    assert pipeline.index("names: [pref_pk]") < pipeline.index("type: Mixer")


def test_baseline_empty_preference_is_byte_identical() -> None:
    # An all-flat preference profile emits nothing extra: the baseline is
    # byte-for-byte the pre-PR-3 config. Inactive bands (near-zero gain) drop out
    # exactly like the stereo emitter's build_sound_filters does.
    base = _active_baseline_yaml("mono", 2)
    assert _active_baseline_yaml("mono", 2, preference_filters=()) == base
    near_zero = (
        FilterSpec(name="pref_noop", biquad_type="Peaking", freq=1000.0, gain=0.0, q=1.0),
    )
    assert _active_baseline_yaml("mono", 2, preference_filters=near_zero) == base


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


def test_unparseable_active_candidate_is_rejected() -> None:
    # A config that classifies as an active-speaker startup candidate (it carries
    # the recognised marker, here in a comment) but is not parseable YAML must
    # fail closed — never read as "safe" — with the precise camilla_yaml_unparseable
    # code. classify_camilla_config_text keys on a substring marker, not a full
    # parse, so a malformed body still reaches the runtime contract's own parse.
    topology = _active_topology("mono", "active_2_way")
    text = "# Auto-generated active-speaker startup config\nfilters: {unterminated\n"

    graph = classify_camilla_graph(topology=topology, text=text)

    assert graph.allowed is False
    assert "camilla_yaml_unparseable" in {issue["code"] for issue in graph.issues}


def test_non_object_active_candidate_is_rejected() -> None:
    # A candidate that parses but to a non-mapping (here a YAML list) fails closed
    # with camilla_yaml_not_object — kept DISTINCT from the unparseable code so the
    # caller can report which one fired.
    topology = _active_topology("mono", "active_2_way")
    text = "# Auto-generated active-speaker startup config\n- a\n- b\n"

    graph = classify_camilla_graph(topology=topology, text=text)

    assert graph.allowed is False
    assert "camilla_yaml_not_object" in {issue["code"] for issue in graph.issues}


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
    startup = _classify_staged_active(
        topology,
        _active_yaml("mono", 3, frozenset()),
    )

    assert classify_output_contract(topology).classification == CONTRACT_ACTIVE_MONO_3WAY
    assert flat.allowed is False
    assert startup.allowed is True
    assert startup.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP


def test_stereo_active_2way_rejects_flat_and_requires_all_tweeters_guarded() -> None:
    topology = _active_topology("stereo", "active_2_way")
    flat = classify_camilla_graph(topology=topology, text=_flat_yaml())
    startup = _classify_staged_active(
        topology,
        _active_yaml("stereo", 2, frozenset()),
    )

    assert classify_output_contract(topology).classification == CONTRACT_ACTIVE_STEREO_2WAY
    assert flat.allowed is False
    assert startup.allowed is True
    assert startup.details["tweeter_outputs"] == [1, 3]


def test_stereo_active_3way_rejects_flat_and_allows_all_muted_startup() -> None:
    topology = _active_topology("stereo", "active_3_way")
    flat = classify_camilla_graph(topology=topology, text=_flat_yaml())
    startup = _classify_staged_active(
        topology,
        _active_yaml("stereo", 3, frozenset()),
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
        **_write_authority(
            tmp_path, staged=_staged_metadata(topology, staged_path)
        ),
    )

    assert decision.status == "select_active_startup"
    assert decision.selected_config_path == str(staged_path)


def test_safe_graph_preserves_staged_startup_after_identity_confirmation(
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    staged_path = tmp_path / "active_speaker_staged_startup.yml"
    staged_path.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    staged = _staged_metadata(topology, staged_path)
    for target in staged["targets"]:
        if target["role"] == "woofer":
            target["identity_verified"] = False

    decision = safe_graph_for_current_topology(
        topology,
        current_config_path=staged_path,
        **_write_authority(tmp_path, staged=staged),
    )

    assert decision.status == "preserve_current"
    assert decision.current_graph is not None
    assert decision.current_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    assert decision.current_graph.details["staged_metadata_matches_topology"] is True
    assert "active_staged_metadata_mismatch" not in {
        issue.id for issue in decision.issues
    }


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
        **_write_authority(
            tmp_path, staged=_staged_metadata(topology, staged_path)
        ),
    )

    assert decision.status == "select_active_startup"
    assert decision.current_graph is not None
    assert decision.current_graph.classification == GRAPH_UNSAFE
    assert "active_staged_locator_mismatch" in {
        issue["code"] for issue in decision.current_graph.issues
    }
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
        **_write_authority(
            tmp_path, staged=_staged_metadata(topology, staged_path)
        ),
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
        **_write_authority(
            tmp_path,
            staged=_staged_metadata(topology, current_path),
            applied_config=baseline_path,
        ),
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
        **_write_authority(tmp_path),
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
        **_write_authority(tmp_path),
    )

    assert apply_safe_graph_decision_to_statefile(
        decision,
        statefile_path=statefile,
    ) is True
    repaired = statefile.read_text(encoding="utf-8")
    assert f"config_path: {flat}" in repaired
    assert "volume: -20.0" in repaired
    assert "- true" in repaired


@pytest.mark.parametrize(
    "graph_kind",
    ["flat", "approved_active", "all_muted_active"],
)
def test_preserve_current_uses_exact_persisted_boot_snapshot(
    monkeypatch,
    tmp_path: Path,
    graph_kind: str,
) -> None:
    proved = tmp_path / f"proved-{graph_kind}.yml"
    if graph_kind == "flat":
        topology = _full_range_stereo()
        proved.write_text(_flat_yaml(), encoding="utf-8")
        authority = _write_authority(tmp_path)
    else:
        topology = _active_topology("mono", "active_2_way")
        if graph_kind == "approved_active":
            proved.write_text(_active_baseline_yaml("mono", 2), encoding="utf-8")
            authority = _write_authority(tmp_path)
        else:
            proved.write_text(
                _active_yaml("mono", 2, frozenset()),
                encoding="utf-8",
            )
            authority = _write_authority(
                tmp_path,
                staged=_staged_metadata(topology, proved),
            )

    stale = tmp_path / "stale-preliminary.yml"
    stale.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(
        f"config_path: {stale}\n"
        "volume: -20.0\n"
        "mute:\n"
        "- true\n"
        "- false\n",
        encoding="utf-8",
    )
    real_classify = runtime_contract_module.classify_bass_extension_graph
    switched = False

    def switch_selector_before_canonical_proof(*args, **kwargs):
        nonlocal switched
        if kwargs.get("evidence_source") == "persisted_boot" and not switched:
            switched = True
            statefile.write_text(
                f"config_path: {proved}\n"
                "volume: -20.0\n"
                "mute:\n"
                "- true\n"
                "- false\n",
                encoding="utf-8",
            )
        return real_classify(*args, **kwargs)

    monkeypatch.setattr(
        runtime_contract_module,
        "classify_bass_extension_graph",
        switch_selector_before_canonical_proof,
    )
    decision = safe_graph_for_current_topology(
        topology,
        statefile_path=statefile,
        consider_applied_baseline=False,
        **authority,
    )

    assert switched is True
    assert decision.status == "preserve_current"
    assert decision.current_graph is not None
    assert decision.current_graph.config_path == str(proved)
    assert decision.selected_config_path == str(proved)
    statefile_after_proof = statefile.read_bytes()
    assert apply_safe_graph_decision_to_statefile(
        decision,
        statefile_path=statefile,
    ) is False
    assert statefile.read_bytes() == statefile_after_proof


# --------------------------------------------------------------------------- #
# L0 verdict: a flat full-range *program* graph is illegal when the saved
# topology assigns a protected tweeter role. This is the shared topology
# predicate; the refuse POLICY lives at the callers (graph_carrier raises
# CarrierCannotHostEq, correction raises CorrectionRuntimeSafetyError), pinned
# by tests in test_sound_graph_carrier.py / test_correction_*.py /
# test_multiroom_leader_config.py.
# --------------------------------------------------------------------------- #


def test_flat_program_graph_blocked_for_stereo_active_tweeter() -> None:
    reason = flat_program_graph_blocked_reason(
        _active_topology("stereo", "active_2_way")
    )
    assert reason is not None
    # The reason names the protected output(s) so callers surface an honest hint.
    assert "tweeter" in reason


def test_flat_program_graph_blocked_for_mono_active_tweeter() -> None:
    assert (
        flat_program_graph_blocked_reason(_active_topology("mono", "active_2_way"))
        is not None
    )


def test_flat_program_graph_allowed_for_full_range_stereo() -> None:
    # The common passive-stereo speaker: no protected tweeter -> not blocked.
    assert flat_program_graph_blocked_reason(_full_range_stereo()) is None


def test_flat_program_graph_allowed_for_unconfigured_topology() -> None:
    assert flat_program_graph_blocked_reason(_topology([])) is None


def test_flat_program_graph_allowed_for_subwoofer_only_topology() -> None:
    # Scope: the verdict targets the tweeter-damage hazard (role == "tweeter").
    # A subwoofer is roleful but not a protected tweeter, so a flat program graph
    # is not blocked on its account; the broader "flat illegal for any roleful
    # topology" rule is enforced separately at graph selection
    # (safe_graph_for_current_topology).
    assert flat_program_graph_blocked_reason(_subwoofer_topology()) is None


def test_flat_program_graph_fail_closed_on_corrupt_topology(
    tmp_path: Path, monkeypatch
) -> None:
    # Fail-closed: an unreadable/corrupt saved topology blocks (returns a reason)
    # rather than raising or reading "safe", so a caller can never emit a flat
    # graph over a topology it could not load.
    corrupt = tmp_path / "output_topology.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(corrupt))

    assert flat_program_graph_blocked_reason() is not None


# --- Slice 2 keystone (invariant 3): the driver-domain-only follower graph ----
# Relocating Layer A onto a follower (channel-select prefix instead of the
# program-domain headroom/EQ) re-proves as its own allowed classification, and
# the verifier still REJECTS the ways it can be made unsafe. emitter<->verifier
# stay independent: the emitter writes the graph, the classifier re-derives the
# verdict from the text.

_DRIVER_DOMAIN_CASES = [
    (layout, way, channel)
    for layout in ("mono", "stereo")
    for way in (2, 3)
    for channel in ("left", "right", "mono")
]


@pytest.mark.parametrize("layout,way,channel", _DRIVER_DOMAIN_CASES)
def test_driver_domain_baseline_allowed(layout: str, way: int, channel: str) -> None:
    topology = _active_topology(layout, f"active_{way}_way")
    graph = classify_camilla_graph(
        topology=topology, text=_driver_domain_yaml(layout, way, channel=channel)
    )
    assert graph.allowed, graph.issues
    assert graph.classification == GRAPH_DRIVER_DOMAIN_BASELINE
    assert graph.details["driver_domain_candidate"] is True


def test_driver_domain_source_marker_matches_verifier() -> None:
    # The cross-module routing contract: the emitter's `# Source:` header must be
    # the exact string the verifier independently names, or the driver-domain arm
    # never fires (the round-trip above IS the live pin; this asserts it directly
    # so a rename fails loudly here too).
    text = _driver_domain_yaml("mono", 2)
    source_line = next(
        line for line in text.splitlines() if line.startswith("# Source:")
    )
    assert source_line.split("# Source:")[1].strip() == ACTIVE_DRIVER_DOMAIN_SOURCE


def test_driver_domain_pair_trim_is_allowed_and_non_positive() -> None:
    text = _driver_domain_yaml("mono", 2, pair_trim_db=3.5)
    graph = classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"), text=text,
    )
    assert graph.allowed, graph.issues
    assert "pair_balance_trim" in text
    assert "parameters: { gain: -3.5000" in text
    assert "active_baseline_headroom" not in text


def test_driver_domain_pair_trim_zero_gain_stage_is_still_emitted() -> None:
    """The live balance path patches this stable filter, including at center."""
    text = _driver_domain_yaml("mono", 2, pair_trim_db=0.0)
    graph = classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"), text=text,
    )

    assert graph.allowed, graph.issues
    assert "pair_balance_trim" in text
    assert "parameters: { gain: -0.0000" in text


def test_driver_domain_pair_trim_positive_gain_is_rejected() -> None:
    text = _driver_domain_yaml("mono", 2, pair_trim_db=3.5)
    tampered = text.replace("gain: -3.5000", "gain: 3.5000")

    graph = classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"), text=tampered,
    )

    assert graph.allowed is False
    assert any(
        i.get("code") == "active_driver_domain_pair_trim_invalid"
        for i in graph.issues
    )


def test_driver_domain_pair_trim_after_split_is_rejected() -> None:
    text = _driver_domain_yaml("mono", 2, pair_trim_db=3.5)
    trim_step = (
        "  - type: Filter\n"
        "    channels: [0, 1]\n"
        "    names: [pair_balance_trim]\n"
    )
    split_step = (
        "  - type: Mixer\n"
        "    name: split_active_2way\n"
    )
    tampered = text.replace(trim_step + split_step, split_step + trim_step)

    graph = classify_camilla_graph(
        topology=_active_topology("mono", "active_2_way"), text=tampered,
    )

    assert graph.allowed is False
    assert any(
        i.get("code") == "active_driver_domain_pair_trim_invalid"
        for i in graph.issues
    )


def test_driver_domain_rejects_injected_program_prefix() -> None:
    text = _driver_domain_yaml("mono", 2)
    bad = text.replace(
        "filters:\n",
        "filters:\n"
        "  active_baseline_headroom:\n"
        "    type: Gain\n"
        "    parameters: { gain: -12.0000, inverted: false, mute: false }\n",
    ).replace(
        "pipeline:\n",
        "pipeline:\n"
        "  - type: Filter\n    channels: [0, 1]\n    names: [active_baseline_headroom]\n",
    )
    graph = classify_camilla_graph(topology=_active_topology("mono", "active_2_way"), text=bad)
    assert not graph.allowed
    assert "active_driver_domain_program_prefix_present" in {i["code"] for i in graph.issues}


def test_driver_domain_rejects_injected_room_peq_prefix() -> None:
    text = _driver_domain_yaml("mono", 2)
    bad = text.replace(
        "filters:\n",
        "filters:\n"
        "  room_peq_1:\n"
        "    type: Biquad\n"
        "    parameters:\n"
        "      type: Peaking\n"
        "      freq: 80.0000\n"
        "      q: 4.0000\n"
        "      gain: 2.0000\n",
    ).replace(
        "pipeline:\n",
        "pipeline:\n"
        "  - type: Filter\n    channels: [0, 1]\n    names: [room_peq_1]\n",
    )
    graph = classify_camilla_graph(topology=_active_topology("mono", "active_2_way"), text=bad)
    codes = {i["code"] for i in graph.issues}
    assert not graph.allowed
    assert "active_driver_domain_room_peq_present" in codes
    assert "active_driver_domain_program_filter_step_present" in codes


def test_driver_domain_rejects_arbitrary_program_domain_filter_step() -> None:
    text = _driver_domain_yaml("mono", 2)
    bad = text.replace(
        "filters:\n",
        "filters:\n"
        "  pref_pk:\n"
        "    type: Biquad\n"
        "    parameters:\n"
        "      type: Peaking\n"
        "      freq: 1200.0000\n"
        "      q: 1.0000\n"
        "      gain: 3.0000\n",
    ).replace(
        "pipeline:\n",
        "pipeline:\n"
        "  - type: Filter\n    channels: [0, 1]\n    names: [pref_pk]\n",
    )
    graph = classify_camilla_graph(topology=_active_topology("mono", "active_2_way"), text=bad)
    codes = {i["code"] for i in graph.issues}
    assert not graph.allowed
    assert "active_driver_domain_program_filter_step_present" in codes


def test_driver_domain_rejects_channel_select_after_split() -> None:
    text = _driver_domain_yaml("mono", 2)
    valid_prefix = (
        "  - type: Mixer\n    name: channel_select\n"
        "  - type: Filter\n    channels: [0, 1]\n    names: [pair_balance_trim]\n"
        "  - type: Mixer\n    name: split_active_2way\n"
    )
    swapped = text.replace(
        valid_prefix,
        "  - type: Mixer\n    name: split_active_2way\n"
        "  - type: Mixer\n    name: channel_select\n"
        "  - type: Filter\n    channels: [0, 1]\n    names: [pair_balance_trim]\n",
    )
    assert swapped != text
    graph = classify_camilla_graph(topology=_active_topology("mono", "active_2_way"), text=swapped)
    assert not graph.allowed
    assert "active_driver_domain_channel_select_after_split" in {i["code"] for i in graph.issues}


def test_driver_domain_rejects_missing_channel_select() -> None:
    text = _driver_domain_yaml("mono", 2)
    missing = text.replace("  - type: Mixer\n    name: channel_select\n", "")
    graph = classify_camilla_graph(topology=_active_topology("mono", "active_2_way"), text=missing)
    assert not graph.allowed
    assert "active_driver_domain_channel_select_missing" in {i["code"] for i in graph.issues}


def test_driver_domain_not_persistable_as_solo_fallback(tmp_path: Path) -> None:
    # Slice 2 does NOT wire the driver-domain graph into safe_graph_for_current_topology
    # selection (that is Slice 3, gated by S0-sync). Even when a driver-domain graph is
    # the current/preferred candidate for a roleful solo topology, the selector leaves
    # it BLOCKED rather than persisting it as a deploy/restart fallback — keeps
    # invariant 7 (no new solo selection path). staged_config={} keeps this hermetic.
    config = tmp_path / "follower.yml"
    config.write_text(_driver_domain_yaml("mono", 2), encoding="utf-8")
    decision = safe_graph_for_current_topology(
        _active_topology("mono", "active_2_way"),
        current_config_path=str(config),
        preferred_config_path=str(config),
        **_write_authority(tmp_path, applied_config=config),
    )
    assert decision.status == "blocked"
    assert decision.selected_config_path is None


# --- Stage B: camilla#1 program bake — File-sink verifier exemption -----------
# The active-leader's program-domain bake (Layer B/C + headroom, File->SNAPFIFO,
# no Layer A) is SAFE regardless of speaker topology — no DAC is attached, so no
# driver can be over-driven. The exemption keys STRICTLY on devices.playback.type
# == File (via the shared playback_is_pipe parser), never on the source marker;
# the dangerous direction (a flat ALSA-sink graph reaching the DAC under a
# roleful topology) stays blocked. emitter<->verifier stay independent: the
# emitter writes the graph, the classifier re-derives the verdict from the text.


def _program_bake_yaml(**kw) -> str:
    profile = SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=6.0))
    return emit_active_speaker_program_bake_config(profile, **kw)


_FILE_PLAYBACK_BLOCK = (
    "  playback:\n"
    "    type: File\n"
    "    channels: 2\n"
    '    filename: "/run/jasper-snapserver/snapfifo"\n'
    "    format: S16_LE"
)
_ALSA_PLAYBACK_BLOCK = (
    "  playback:\n"
    "    type: Alsa\n"
    "    channels: 2\n"
    '    device: "outputd_content_playback"\n'
    "    format: S16_LE"
)


def _swap_file_sink_for_alsa(text: str) -> str:
    # The verifier re-derives its verdict from the text; flipping ONLY the
    # playback sink (File -> Alsa) is the minimal mutation that turns the safe
    # pipe bake into the dangerous full-range-to-DAC graph.
    assert _FILE_PLAYBACK_BLOCK in text
    return text.replace(_FILE_PLAYBACK_BLOCK, _ALSA_PLAYBACK_BLOCK, 1)


@pytest.mark.parametrize("layout,mode", [
    ("mono", "active_2_way"),
    ("mono", "active_3_way"),
    ("stereo", "active_2_way"),
    ("stereo", "active_3_way"),
])
def test_program_bake_allowed_even_under_tweeter_topology(layout, mode) -> None:
    # POSITIVE round-trip: the emitted bake, fed back through the verifier,
    # classifies allowed under a ROLEFUL tweeter topology — the File sink makes
    # it safe by construction.
    topology = _active_topology(layout, mode)
    graph = classify_camilla_graph(topology=topology, text=_program_bake_yaml())
    assert graph.allowed, graph.issues
    assert graph.classification == GRAPH_PROGRAM_BAKE_PIPE
    assert graph.details["program_bake_pipe"] is True


def test_program_bake_is_file_shaped_no_layer_a_rate_adjust_off() -> None:
    # The bake the verifier exempts is a File/pipe sink, has NO Layer A, and runs
    # rate_adjust off — the three properties that make the exemption sound. (Full
    # parsed-shape coverage lives in test_active_speaker_program_bake.py; here we
    # keep the file's no-yaml string idiom.)
    text = _program_bake_yaml()
    assert "    type: File\n" in text
    assert "enable_rate_adjust: false" in text
    assert "split_active_" not in text
    assert "active_baseline_headroom" not in text


def test_program_bake_alsa_sink_under_tweeter_topology_still_blocked() -> None:
    # NEGATIVE: the dangerous direction stays blocked. A flat ALSA-sink graph
    # (full-range to the DAC) under a tweeter topology is full-range-to-tweeter
    # and the existing tweeter guard must still fire. The File-sink exemption is
    # narrow and additive — it does NOT weaken the ALSA-sink block.
    topology = _active_topology("mono", "active_2_way")
    alsa_flat = _swap_file_sink_for_alsa(_program_bake_yaml())
    graph = classify_camilla_graph(topology=topology, text=alsa_flat)
    assert graph.allowed is False
    assert "flat_full_range_graph_illegal_for_roleful_topology" in {
        i["code"] for i in graph.issues
    }


def test_program_bake_exemption_keys_on_file_not_marker() -> None:
    # The exemption keys STRICTLY on playback.type == File, never on the source
    # marker. An ALSA-sink graph still carrying the program-bake `# Source:`
    # marker is NOT exempted — it stays blocked under a tweeter topology.
    topology = _active_topology("mono", "active_2_way")
    marker_but_alsa = _swap_file_sink_for_alsa(_program_bake_yaml())
    assert f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}" in marker_but_alsa
    graph = classify_camilla_graph(topology=topology, text=marker_but_alsa)
    assert graph.allowed is False


def test_program_bake_source_marker_matches_verifier() -> None:
    # The cross-module routing contract: the emitter's `# Source:` header must be
    # the exact string the verifier (via classify_camilla_config_text) keys on to
    # route the bake to the flat program lane, or the exemption never fires.
    text = _program_bake_yaml()
    source_line = next(
        line for line in text.splitlines() if line.startswith("# Source:")
    )
    assert source_line.split("# Source:")[1].strip() == ACTIVE_PROGRAM_BAKE_SOURCE


def test_program_bake_safe_with_no_topology_configured() -> None:
    # An unconfigured topology (no roleful outputs) is the common solo case; the
    # bake is allowed there too (still a pipe, still no DAC).
    topology = OutputTopology.from_mapping({
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
        "speaker_groups": [],
        "routing": {},
    })
    graph = classify_camilla_graph(topology=topology, text=_program_bake_yaml())
    assert graph.allowed is True
    assert graph.classification == GRAPH_PROGRAM_BAKE_PIPE


def test_program_bake_not_selectable_as_solo_graph(tmp_path: Path) -> None:
    # The exemption makes the bake SAFE (no DAC to over-drive) but it is NOT a
    # selectable speaker output graph: its File sink feeds the snapserver FIFO,
    # not the DAC, so the solo selector must never preserve it (that would leave a
    # solo speaker silent). Wiring camilla#1 is a later Stage-B slice; this PR
    # ships the emit + verifier only. The flat fallback points at a missing path
    # so the ONLY way this could pass is the (wrong) preserve_current branch.
    topology = _full_range_mono()
    config = tmp_path / "grouping_leader.yml"
    config.write_text(_program_bake_yaml(), encoding="utf-8")
    decision = safe_graph_for_current_topology(
        topology,
        current_config_path=str(config),
        preferred_config_path=str(config),
        flat_config_path=str(tmp_path / "no-such-outputd.yml"),
        **_write_authority(tmp_path, applied_config=config),
    )
    assert decision.selected_config_path != str(config)
    assert decision.status != "preserve_current"
