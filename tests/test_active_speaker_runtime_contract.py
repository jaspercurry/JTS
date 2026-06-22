# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from pathlib import Path

from jasper.active_speaker import (
    ACTIVE_PROGRAM_BAKE_SOURCE,
    ActiveSpeakerPreset,
    emit_active_speaker_baseline_config,
    emit_active_speaker_commissioning_config,
    emit_active_speaker_driver_domain_config,
    emit_active_speaker_program_bake_config,
)
from jasper.active_speaker.runtime_contract import (
    ACTIVE_DRIVER_DOMAIN_SOURCE,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_DRIVER_DOMAIN_BASELINE,
    GRAPH_FLAT_FULL_RANGE,
    GRAPH_GUARDED_COMMISSIONING,
    GRAPH_PROGRAM_BAKE_PIPE,
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
    flat_program_graph_blocked_reason,
    safe_graph_for_current_topology,
)
from jasper.camilla_config_contract import FilterSpec
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
from jasper.sound.profile import SimpleEq, SoundProfile

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


def _active_baseline_yaml(
    layout: str,
    way: int,
    *,
    preference_filters: tuple[FilterSpec, ...] = (),
    output_trim_db: float = 0.0,
) -> str:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return emit_active_speaker_baseline_config(
        ActiveSpeakerPreset.from_mapping(raw),
        playback_device=ACTIVE_PCM,
        preference_filters=preference_filters,
        output_trim_db=output_trim_db,
        baseline_id=f"baseline-{layout}-{way}way",
    )


def _driver_domain_yaml(layout: str, way: int, *, channel: str = "left") -> str:
    raw = _two_way_preset(layout) if way == 2 else _three_way_preset(layout)
    return emit_active_speaker_driver_domain_config(
        ActiveSpeakerPreset.from_mapping(raw),
        playback_device=ACTIVE_PCM,
        program_channel=channel,
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


# --- PR-3: preference EQ folds into the active baseline, pre-split -------

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
    # preference SHELF folded in, feed it back through the SAME classifier for the
    # SAME topology -> still GRAPH_APPROVED_ACTIVE_RUNTIME. Folding EQ never breaks
    # the protection contract. Cover stereo + 3-way because the classifier's
    # per-output name collection gathers the pre-split [0,1] pref names into
    # outputs 0/1 but not the right-channel/higher outputs — an asymmetry that
    # only manifests beyond mono-2-way.
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


def test_baseline_preference_boost_folds_into_headroom() -> None:
    # Invariant 4 (emitter-side): a +N dB preference boost reduces the pre-split
    # headroom by >= total_positive_boost_db(prefs) and keeps volume_limit 0.0.
    # Tested with a SHELF (summing a shelf's band gain is the conservative bound
    # and the easy-to-get-wrong case).
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

    # +5 shelf + +3 peak = +8 dB worst-case boost; headroom drops by >= 8 dB and
    # stays non-positive. (0 dB baseline -> 8 dB attenuation.)
    assert _headroom_db(flat) - _headroom_db(boosted) >= 8.0 - 1e-6
    assert _headroom_db(boosted) <= 0.0
    assert "volume_limit: 0.0" in boosted


def test_baseline_output_trim_folds_into_headroom_with_eq() -> None:
    # output_trim_db (manual headroom + loudness match) folds into the SAME
    # active_baseline_headroom gain as the boost, so the active path honours the
    # household's loudness setting exactly like emit_sound_config. It applies
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
    # With EQ: -(0 baseline + 2 boost + 4 trim) = -6.
    with_eq = _active_baseline_yaml("mono", 2, preference_filters=prefs, output_trim_db=4.0)
    assert _headroom_db(with_eq) == -6.0
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


def test_driver_domain_rejects_channel_select_after_split() -> None:
    text = _driver_domain_yaml("mono", 2)
    swapped = text.replace(
        "  - type: Mixer\n    name: channel_select\n"
        "  - type: Mixer\n    name: split_active_2way\n",
        "  - type: Mixer\n    name: split_active_2way\n"
        "  - type: Mixer\n    name: channel_select\n",
    )
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
        staged_config={},
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
        staged_config={},
    )
    assert decision.selected_config_path != str(config)
    assert decision.status != "preserve_current"
