# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from dataclasses import replace
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest
import yaml as yaml_lib

import jasper.active_speaker.baseline_profile as baseline_profile_mod
from jasper.active_speaker import (
    compile_preset_from_crossover_preview,
    emit_active_speaker_baseline_config,
)
from jasper.active_speaker.baseline_profile import (
    PROVENANCE_MANUAL,
    PROVENANCE_MEASURED,
    PROVENANCE_PRESERVED,
    PROVENANCE_RECOMMENDED_START,
    _derive_corrections,
    _GAIN_SOURCE_TO_PROVENANCE,
    active_layer_a_fingerprint,
    apply_baseline_profile,
    baseline_candidate_fingerprint,
    build_baseline_profile_candidate,
    load_applied_baseline_profile_state,
    recompose_applied_baseline_yaml,
    restore_applied_baseline_profile,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.design_draft import DRIVER_RESEARCH_KIND, build_design_draft
from jasper.active_speaker.measurement import (
    load_measurement_state,
    record_driver_measurement,
    record_summed_test_artifact,
    record_summed_validation,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    MeasuredCrossoverCandidateError,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset, CrossoverRegion
from jasper.camilla_config_contract import PeqFilter
from jasper.active_speaker.runtime_contract import NO_BASS_EXTENSION_PROFILE_SUMMARY
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
from jasper.output_hardware import DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_profile import _two_way_preset


def _topology(
    *,
    tweeter_output: int = 1,
    tweeter_verified: bool = True,
    device_id: str = "hifiberry_dac8x",
    device_label: str = "HiFiBerry DAC8x",
    physical_output_count: int = 8,
    card_id: str = "DAC8",
) -> OutputTopology:
    return mono_output_topology(
        tweeter_output=tweeter_output,
        tweeter_verified=tweeter_verified,
        topology_name="Bench mono",
        device_id=device_id,
        device_label=device_label,
        physical_output_count=physical_output_count,
        card_id=card_id,
    )


def _dual_apple_topology() -> OutputTopology:
    return _topology(
        device_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        device_label="Dual Apple USB-C DACs",
        physical_output_count=4,
        card_id="",
    )


def _stereo_three_way_topology() -> OutputTopology:
    raw = _topology().to_dict()
    raw["topology_id"] = "bench_stereo_3way"
    raw["speaker_groups"] = [
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "active_3_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
                {"role": "mid", "physical_output_index": 1, "identity_verified": True},
                {
                    "role": "tweeter",
                    "physical_output_index": 2,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "software_guard_requested",
                },
            ],
        },
        {
            "id": "right",
            "label": "Right speaker",
            "kind": "right",
            "mode": "active_3_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 3, "identity_verified": True},
                {"role": "mid", "physical_output_index": 4, "identity_verified": True},
                {
                    "role": "tweeter",
                    "physical_output_index": 5,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "software_guard_requested",
                },
            ],
        },
    ]
    raw["routing"] = {
        "main_left_group_id": "left",
        "main_right_group_id": "right",
        "mono_group_id": None,
        "subwoofer_group_ids": [],
    }
    return OutputTopology.from_mapping(raw)


def _safe_session(
    *,
    role: str,
    output_index: int,
    playback_id: str,
) -> dict:
    target = {
        "speaker_group_id": "mono",
        "role": role,
        "driver_role": role,
        "output_index": output_index,
    }
    return {
        "status": "armed",
        "quiet_start": {
            "status": "floor_confirmed",
            "floor_audio_confirmed": True,
            "current_target": target,
            "last_operator_result": {
                "accepted": True,
                "outcome": "heard_correct_driver",
                "playback_id": playback_id,
                "target": target,
            },
        },
    }


def _research(*, tweeter_gain_db: float = -18.5, with_subwoofer: bool = False) -> dict:
    drivers = [
        {
            "role": "woofer",
            "model": "Epique E150HE-44",
            "recommended_lowpass_hz": 2500,
            "usable_frequency_range_hz": [45, 5000],
            "sources": ["https://example.test/woofer"],
        },
        {
            "role": "tweeter",
            "model": "F110M-8",
            "recommended_highpass_hz": 2500,
            "do_not_test_below_hz": 1200,
            "gain_offset_db": tweeter_gain_db,
            "sources": ["https://example.test/tweeter"],
        },
    ]
    if with_subwoofer:
        drivers.append({
            "role": "subwoofer",
            "model": "Sub driver",
            "recommended_lowpass_hz": 80,
            "usable_frequency_range_hz": [20, 200],
            "sources": ["https://example.test/sub"],
        })
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": drivers,
        "crossover_candidates": [
            {
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            }
        ],
    }


def _draft(
    topology: OutputTopology,
    *,
    tweeter_gain_db: float = -18.5,
    with_subwoofer: bool = False,
) -> dict:
    return build_design_draft(
        topology,
        driver_research=_research(
            tweeter_gain_db=tweeter_gain_db, with_subwoofer=with_subwoofer
        ),
        created_at="2026-06-14T12:00:00Z",
    )


def _dual_apple_sub_topology(*, sub_output: int = 2) -> OutputTopology:
    """Dual-Apple mono 2-way (woofer@0, tweeter@1) PLUS a local sub on its own
    output. ``sub_output`` defaults to the next contiguous channel (2); pass a
    non-contiguous index to exercise the fail-closed path."""
    raw = _dual_apple_topology().to_dict()
    raw["topology_id"] = "bench_mono_sub"
    raw["speaker_groups"].append({
        "id": "sub",
        "label": "Bench subwoofer",
        "kind": "subwoofer",
        "mode": "subwoofer",
        "channels": [
            {
                "role": "subwoofer",
                "physical_output_index": sub_output,
                "identity_verified": True,
            }
        ],
    })
    raw["routing"]["subwoofer_group_ids"] = ["sub"]
    return OutputTopology.from_mapping(raw)


def _measurements(topology: OutputTopology, tmp_path: Path) -> dict:
    state_path = tmp_path / "measurements.json"
    for role in ("woofer", "tweeter"):
        output_index = 0 if role == "woofer" else 1
        playback_id = f"playback-{role}"
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42.0,
                "test_level_dbfs": -68.0,
                "playback_id": playback_id,
            },
            safe_session=_safe_session(
                role=role,
                output_index=output_index,
                playback_id=playback_id,
            ),
            state_path=state_path,
            now=f"2026-06-14T12:0{1 if role == 'woofer' else 2}:00Z",
        )
    record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": "summed-playback-audible",
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "tone_summed-playback-audible.wav",
                    "metadata_basename": "tone_summed-playback-audible.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2500, "level_dbfs": -72},
            },
        },
        state_path=state_path,
        now="2026-06-14T12:02:30Z",
    )
    return record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40.0,
            "polarity": "normal",
            "delay_ms": 0.0,
            "summed_test_id": "summed-playback-audible",
        },
        state_path=state_path,
        now="2026-06-14T12:03:00Z",
    )


def _valid_config(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(
        status=ValidationStatus.VALID,
        path=str(path),
    )


_SENSITIVITY_TRIM_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "sensitivity_trim_fixture.json").read_text()
)


def _derive_sensitivity_trims(way_count: int, sensitivities: dict[str, float]):
    """Run the production datasheet-trim derivation for a sensitivity-only input.

    _derive_corrections reads only preset.way_count and preset.crossover_regions
    (the latter short-circuits with no measurements), so a minimal duck preset
    exercises the real path. Returns {role: gain_db} for the roles the source
    actually attributed to the sensitivity gap.
    """
    drivers = {
        role: {"sensitivity_db_2v83_1m": value}
        for role, value in sensitivities.items()
    }
    preset = SimpleNamespace(way_count=way_count, crossover_regions=[])
    corrections, _issues, meta = _derive_corrections(
        preset, {"drivers": drivers}, {}
    )
    sources = meta["sources"]
    return {
        role: corrections[role]["gain_db"]
        for role in corrections
        if sources.get(role) == "sensitivity"
    }


def test_sensitivity_trim_matches_shared_parity_fixture():
    """Python source reproduces the committed fixture (the JS contract).

    The /sound/ form pre-fills these trims client-side (optimistic UI); the
    server re-derives them here authoritatively. scripts/check-sensitivity-trim-parity.mjs
    asserts the JS (active-speaker-ui.js::sensitivityTrimsFromGap) matches the
    same fixture, so the two cannot silently diverge. Mirrors the eq-math.js
    parity model (test_python_matches_shared_parity_fixture).
    """
    cases = _SENSITIVITY_TRIM_FIXTURE["cases"]
    assert cases, "expected sensitivity-trim parity cases"
    for case in cases:
        got = _derive_sensitivity_trims(case["way_count"], case["sensitivities"])
        expected = {
            role: float(trim) for role, trim in case["expected_trims"].items()
        }
        assert got == pytest.approx(expected), case["name"]


def test_baseline_profile_compiles_durable_camilla_yaml(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(
        draft,
        created_at="2026-06-14T12:10:00Z",
    )
    measurements = _measurements(topology, tmp_path)
    config_path = tmp_path / "active_speaker_baseline.yml"

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_path,
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )
    # #1666: every write=True candidate lands on its own content-addressed
    # sibling next to config_path, never config_path itself -- read back
    # through the candidate's own reported path.
    assert Path(payload["config"]["path"]) != config_path
    yaml = Path(payload["config"]["path"]).read_text(encoding="utf-8")

    assert payload["status"] == "ready_to_apply"
    assert payload["permissions"]["may_apply"] is True
    assert "baseline_output_handoff_not_supported" not in {
        issue["code"] for issue in payload["issues"]
    }
    assert payload["safety"]["positive_gain_allowed"] is False
    assert payload["verification"]["driver_measurements_complete"] is True
    assert payload["verification"]["summed_validation_complete"] is True
    assert payload["corrections"]["tweeter"]["gain_db"] == -18.5
    assert "Source: jasper.active_speaker.camilla_yaml" in yaml
    assert payload["config"]["playback_device"] == "outputd_active_content_playback"
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"
    assert 'device: "outputd_active_content_playback"' in yaml
    assert "active_baseline_headroom" in yaml
    assert (
        "  active_baseline_headroom:\n"
        "    type: Gain\n"
        "    parameters: { gain: 0.0000, inverted: false, mute: false }"
    ) in yaml
    assert "as_tweeter_baseline_limiter" in yaml


def test_baseline_profile_state_keeps_shared_parent_group(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A root web writer must not hide applied Layer-A state from control."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    calls: list[dict[str, object]] = []
    real_write = baseline_profile_mod.atomic_write_text

    def recording_write(path, text, *, mode, group_from_parent=False):
        calls.append({
            "path": Path(path),
            "mode": mode,
            "group_from_parent": group_from_parent,
        })
        real_write(
            path,
            text,
            mode=mode,
            group_from_parent=group_from_parent,
        )

    monkeypatch.setattr(baseline_profile_mod, "atomic_write_text", recording_write)
    state_path = tmp_path / "baseline_profile.json"
    build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=state_path,
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    state_writes = [call for call in calls if call["path"] == state_path]
    assert state_writes
    assert all(call["mode"] == 0o640 for call in state_writes)
    assert all(call["group_from_parent"] is True for call in state_writes)


def test_baseline_profile_compiles_with_local_subwoofer(tmp_path: Path) -> None:
    # B2: a topology with a routed local subwoofer now COMPILES through the SAME
    # multi-output emitter — may_apply true — and the emitted graph re-proves as
    # an approved active runtime (the emit<->re-proof keystone).
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        classify_camilla_graph,
    )

    topology = _dual_apple_sub_topology()
    draft = _draft(topology, with_subwoofer=True)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    config_path = tmp_path / "active_speaker_baseline_sub.yml"

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile_sub.json",
        config_path=config_path,
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )
    # #1666: candidate lands on a content-addressed sibling, not config_path.
    yaml = Path(payload["config"]["path"]).read_text(encoding="utf-8")

    assert payload["status"] == "ready_to_apply"
    assert payload["permissions"]["may_apply"] is True
    assert "baseline_subwoofer_not_supported" not in {
        issue["code"] for issue in payload["issues"]
    }
    # The sub lane: band-limit (LP) + non-positive gain + soft-clip limiter, and
    # the mains' woofer carries the complementary bass-management high-pass.
    assert "as_sub_lowpass" in yaml
    assert "as_sub_baseline_limiter" in yaml
    assert "as_woofer_bass_mgmt_hp" in yaml
    assert payload["safety"]["positive_gain_allowed"] is False
    assert "volume_limit: 0.0" in yaml

    # Keystone: the emitted sub-bearing graph re-proves as approved.
    graph = classify_camilla_graph(
        topology=topology,
        text=yaml,
        bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
    )
    assert graph.allowed is True, [i["code"] for i in graph.issues]
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert graph.details["subwoofer_present"] is True


def test_baseline_profile_blocks_misrouted_subwoofer(tmp_path: Path) -> None:
    # Fail-closed: a sub pinned to a NON-contiguous output (3, leaving a gap at the
    # next channel after the 2 mains) cannot be made safe — the candidate must BLOCK
    # rather than emit a sub on the wrong / un-band-limited output.
    topology = _dual_apple_sub_topology(sub_output=3)
    draft = _draft(topology, with_subwoofer=True)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    config_path = tmp_path / "active_speaker_baseline_bad_sub.yml"

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile_bad_sub.json",
        config_path=config_path,
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_apply"] is False
    assert "active_subwoofer_output_not_contiguous" in {
        issue["code"] for issue in payload["issues"]
    }
    assert config_path.exists() is False


def test_baseline_capture_device_threads_through_surgically(tmp_path: Path) -> None:
    """Slice 1 inv 1 + inv 7 (config layer): threading the default capture device
    reproduces today's baseline byte-for-byte, and a follower capture changes
    EXACTLY the capture device line — relocating where Layer A reads its program
    never touches the crossover / per-driver limiters / tweeter HP / 0 dB ceiling
    (docs/HANDOFF-distributed-active.md gap 1).
    """
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)

    def _emit(suffix: str, capture_device: str | None) -> tuple[dict, str]:
        kwargs: dict = {}
        if capture_device is not None:
            kwargs["capture_device"] = capture_device
        config_path = tmp_path / f"config_{suffix}.yml"
        payload = build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            write=True,
            state_path=tmp_path / f"state_{suffix}.json",
            config_path=config_path,
            validate=_valid_config,
            created_at="2026-06-14T12:20:00Z",
            **kwargs,
        )
        # #1666: candidate lands on a content-addressed sibling, not config_path.
        return payload, Path(payload["config"]["path"]).read_text(encoding="utf-8")

    implicit, implicit_yaml = _emit("implicit", None)
    explicit, explicit_yaml = _emit("explicit_default", "plug:jasper_capture")
    follower, follower_yaml = _emit("follower", "hw:CARD=Loopback,DEV=1")

    # inv 1: passing the default explicitly is byte-identical to not passing it,
    # and the solo baseline captures from the fan-in tap.
    assert implicit_yaml == explicit_yaml
    assert implicit["config"]["sha256"] == explicit["config"]["sha256"]
    assert 'device: "plug:jasper_capture"' in implicit_yaml

    # A follower's round-trip-loopback capture changes ONLY the capture line.
    assert 'device: "hw:CARD=Loopback,DEV=1"' in follower_yaml
    impl_lines = implicit_yaml.splitlines()
    foll_lines = follower_yaml.splitlines()
    assert len(impl_lines) == len(foll_lines)
    diff = [(a, b) for a, b in zip(impl_lines, foll_lines) if a != b]
    assert len(diff) == 1
    assert diff[0][0].strip() == 'device: "plug:jasper_capture"'
    assert diff[0][1].strip() == 'device: "hw:CARD=Loopback,DEV=1"'
    assert implicit["config"]["sha256"] != follower["config"]["sha256"]


def test_driver_domain_seam_emits_layer_a_only_follower_graph(
    tmp_path: Path,
) -> None:
    """Slice 3: ``driver_domain=True`` switches the compile/apply seam to the
    Slice-2 driver-domain-only emit — a follower's Layer A (channel-select +
    split + per-driver crossover/limiter) with NO program-domain headroom and
    NO preference EQ. ``program_channel`` is required and validated; the default
    (full baseline) is unchanged (invariant 7)."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)

    config_path = tmp_path / "follower_driver_domain.yml"
    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "follower_state.json",
        config_path=config_path,
        capture_device="hw:CARD=Loopback,DEV=1",
        driver_domain=True,
        program_channel="left",
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )
    # #1666: candidate lands on a content-addressed sibling, not config_path.
    yaml = Path(payload["config"]["path"]).read_text(encoding="utf-8")

    assert payload["status"] == "ready_to_apply"
    assert payload["config"]["domain"] == "driver"
    assert payload["config"]["program_channel"] == "left"
    # Driver-domain marker + channel pick present; the leader-baked program
    # domain (headroom + preference EQ) is ABSENT.
    assert "emit_active_speaker_driver_domain_config" in yaml
    assert "# program_channel=left" in yaml
    assert 'device: "hw:CARD=Loopback,DEV=1"' in yaml
    assert "active_baseline_headroom" not in yaml
    # The protective Layer A (per-driver limiter, 0 dB ceiling) survives.
    assert "as_tweeter_baseline_limiter" in yaml
    assert "volume_limit: 0.0" in yaml

    # program_channel is required + validated when driver_domain is set.
    with pytest.raises(ValueError, match="program_channel"):
        build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            driver_domain=True,
            program_channel="stereo",  # not a single-box pick
        )


def test_pairing_intent_does_not_change_emitted_baseline_config(
    tmp_path: Path,
) -> None:
    """Slice 1 inv 7 (config layer): pairing intent is commission-time design
    intent and never reaches the emitter, so the emitted CamillaDSP config is
    byte-identical (same sha256) across every pairing value. Pins the
    output_topology.py claim "the emitted CamillaDSP config is unaffected".
    """
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)

    def _sha(intent: str) -> str:
        payload = build_baseline_profile_candidate(
            replace(topology, pairing_intent=intent),
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            write=True,
            state_path=tmp_path / f"state_{intent}.json",
            config_path=tmp_path / f"config_{intent}.yml",
            validate=_valid_config,
            created_at="2026-06-14T12:20:00Z",
        )
        return payload["config"]["sha256"]

    shas = {_sha(intent) for intent in ("solo", "will_be_follower", "has_follower")}
    assert len(shas) == 1


def test_pairing_intent_change_does_not_invalidate_baseline_cache(
    tmp_path: Path,
) -> None:
    """Slice 1 inv 7: because pairing intent does not determine the emitted
    config, toggling it must NOT invalidate the durable baseline cache. A
    write=False read after a pairing change still returns the cached
    ready-to-apply state instead of dropping to ready_to_compile (which would
    surface as a spurious "needs recompile" on the /sound/ wizard). Also guards
    the magic-string exclusion in _source_payload against a silent field rename.
    """
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"

    first = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )
    assert first["status"] == "ready_to_apply"

    cached = build_baseline_profile_candidate(
        replace(topology, pairing_intent="has_follower"),
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )
    assert cached["status"] == "ready_to_apply"
    assert cached["permissions"]["may_apply"] is True


def test_graph_context_change_invalidates_baseline_cache(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    first = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    changed = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        state_path=state_path,
        config_path=config_path,
        capture_device="changed-capture",
        validate=_valid_config,
    )

    assert first["status"] == "ready_to_apply"
    assert changed["status"] == "ready_to_compile"
    assert changed["recomposition_snapshot"]["capture_device"] == "changed-capture"
    assert changed["candidate_fingerprint"] != first["candidate_fingerprint"]


def test_baseline_source_binds_exact_normalized_preview_candidate(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    changed_preview = deepcopy(preview)
    changed_preview["groups"][0]["crossovers"][0]["candidate"][
        "confidence"
    ] = "high"
    measurements = _measurements(topology, tmp_path)

    first = baseline_profile_mod._source_payload(
        topology, draft, preview, measurements
    )
    changed = baseline_profile_mod._source_payload(
        topology, draft, changed_preview, measurements
    )

    assert (
        first["crossover_preview_fingerprint"]
        != changed["crossover_preview_fingerprint"]
    )
    assert first["fingerprint"] != changed["fingerprint"]


def test_candidate_identity_distinguishes_owner_and_graph_context(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    manual = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        tuning_owner="manual",
    )
    automatic = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        tuning_owner="automatic",
    )

    assert manual["source"]["fingerprint"] == automatic["source"]["fingerprint"]
    assert manual["candidate_fingerprint"] != automatic["candidate_fingerprint"]

    mutations = (
        (("tuning_owner",), "automatic"),
        (("corrections", "tweeter", "gain_db"), -7.0),
        (("preset", "crossover_regions", 0, "fc_hz"), 2600.0),
        (("playback_device",), "changed-playback"),
        (("domain",), "driver"),
        (("program_channel",), "left"),
        (("driver_domain_pair_trim_db",), -2.0),
        (("capture_device",), "changed-capture"),
        (("capture_format",), "S24LE3"),
    )
    for path, value in mutations:
        changed = deepcopy(manual)
        target = changed["recomposition_snapshot"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert baseline_candidate_fingerprint(changed) != manual["candidate_fingerprint"]


def test_baseline_profile_blocks_until_summed_validation_exists(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={"summary": {"driver_measurements_complete": True}},
        config_path=tmp_path / "active_speaker_baseline.yml",
        state_path=tmp_path / "baseline_profile.json",
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_apply"] is False
    assert "baseline_summed_validation_missing" in {
        issue["code"] for issue in payload["issues"]
    }


def test_baseline_profile_blocks_when_summed_validation_is_superseded(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    state_path = tmp_path / "measurements.json"
    _measurements(topology, tmp_path)
    measurements = record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": "summed-playback-newer",
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "tone_summed-playback-newer.wav",
                    "metadata_basename": "tone_summed-playback-newer.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2500, "level_dbfs": -72},
            },
        },
        state_path=state_path,
        now="2026-06-14T12:04:00Z",
    )

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        config_path=tmp_path / "active_speaker_baseline.yml",
        state_path=tmp_path / "baseline_profile.json",
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    assert measurements["summary"]["summed_validation_complete"] is False
    assert "baseline_summed_validation_missing" in {
        issue["code"] for issue in payload["issues"]
    }


def test_saved_baseline_profile_cache_invalidates_when_topology_changes(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(
        draft,
        created_at="2026-06-14T12:10:00Z",
    )
    measurements = _measurements(topology, tmp_path)
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    ready = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )

    changed_topology = _dual_apple_topology()
    changed_raw = changed_topology.to_dict()
    changed_raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = 2
    changed_raw["speaker_groups"][0]["channels"][1]["identity_verified"] = False
    changed_topology = OutputTopology.from_mapping(changed_raw)
    changed_measurements = load_measurement_state(
        changed_topology,
        state_path=tmp_path / "measurements.json",
    )
    stale = build_baseline_profile_candidate(
        changed_topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=changed_measurements,
        write=False,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert ready["status"] == "ready_to_apply"
    assert stale["status"] == "blocked"
    assert stale["permissions"]["may_apply"] is False
    assert "baseline_driver_measurements_missing" in {
        issue["code"] for issue in stale["issues"]
    }


def test_superseded_applied_profile_reports_revalidation_path(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(
        draft,
        created_at="2026-06-14T12:10:00Z",
    )
    measurements = _measurements(topology, tmp_path)
    measurements_path = tmp_path / "measurements.json"
    baseline_state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    ready = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )
    assert ready["status"] == "ready_to_apply"

    applied = {**ready, "status": "applied", "applied_at": "2026-06-14T12:21:00Z"}
    applied["permissions"] = {**applied["permissions"], "may_apply": False}
    baseline_state_path.write_text(json.dumps(applied), encoding="utf-8")

    newer_test = record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": "summed-playback-newer",
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "tone_summed-playback-newer.wav",
                    "metadata_basename": "tone_summed-playback-newer.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2500, "level_dbfs": -72},
            },
        },
        state_path=measurements_path,
        now="2026-06-14T12:22:00Z",
    )
    blocked = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=newer_test,
        write=False,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert blocked["status"] == "blocked"
    assert blocked["revalidation"]["required"] is True
    assert blocked["revalidation"]["reason"] == "applied_profile_superseded"
    assert blocked["revalidation"]["next_step"] == "combined_check"
    assert blocked["revalidation"]["superseded_profile"]["config"]["exists"] is True
    assert "measurement_summary_fingerprint" in blocked["revalidation"]["changed"]

    revalidated = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40.0,
            "polarity": "normal",
            "delay_ms": 0.0,
            "summed_test_id": "summed-playback-newer",
        },
        state_path=measurements_path,
        now="2026-06-14T12:23:00Z",
    )
    ready_to_save = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=revalidated,
        write=False,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert ready_to_save["status"] == "ready_to_compile"
    assert ready_to_save["revalidation"]["required"] is True
    assert ready_to_save["revalidation"]["next_step"] == "save_profile"


def test_superseded_applied_profile_revalidates_without_raw_driver_measurements(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(
        draft,
        created_at="2026-06-14T12:10:00Z",
    )
    baseline_state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    ready = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=_measurements(topology, tmp_path),
        write=True,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
        created_at="2026-06-14T12:20:00Z",
    )
    applied = {**ready, "status": "applied", "applied_at": "2026-06-14T12:21:00Z"}
    applied["permissions"] = {**applied["permissions"], "may_apply": False}
    baseline_state_path.write_text(json.dumps(applied), encoding="utf-8")

    revalidation_measurements_path = tmp_path / "measurements_revalidation.json"
    record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": "summed-playback-revalidate",
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "tone_summed-playback-revalidate.wav",
                    "metadata_basename": "tone_summed-playback-revalidate.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2500, "level_dbfs": -72},
            },
        },
        state_path=revalidation_measurements_path,
        now="2026-06-14T12:22:00Z",
    )
    measurements = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40.0,
            "polarity": "normal",
            "delay_ms": 0.0,
            "summed_test_id": "summed-playback-revalidate",
        },
        state_path=revalidation_measurements_path,
        driver_target_proof_complete=True,
        now="2026-06-14T12:23:00Z",
    )
    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert payload["status"] == "ready_to_compile"
    assert "baseline_driver_measurements_missing" not in {
        issue["code"] for issue in payload["issues"]
    }
    assert payload["verification"]["driver_measurements_complete"] is False
    assert payload["verification"]["driver_target_proof_complete"] is True
    assert (
        payload["verification"]["driver_target_proof_source"]
        == "applied_profile_revalidation"
    )
    assert payload["verification"]["summed_validation_complete"] is True
    assert payload["revalidation"]["required"] is True
    assert payload["revalidation"]["next_step"] == "save_profile"


def test_baseline_profile_never_emits_positive_driver_gain(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology, tweeter_gain_db=4.0)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "ready_to_apply"
    assert payload["corrections"]["tweeter"]["gain_db"] == 0.0
    assert "positive_driver_gain_ignored" in {
        issue["code"] for issue in payload["issues"]
    }


def test_baseline_profile_apply_requires_registered_active_output_lane(
    tmp_path: Path,
) -> None:
    topology = _topology(
        device_id="unregistered_lab_dac",
        device_label="Unregistered lab DAC",
        physical_output_count=2,
        card_id="LabDAC",
    )
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        playback_device="hw:LabDAC,0",
        validate=_valid_config,
    )

    assert payload["status"] == "compiled_apply_blocked"
    assert payload["permissions"]["may_apply"] is False
    assert payload["config"]["playback_device_source"] == "explicit"
    assert "baseline_output_handoff_not_supported" in {
        issue["code"] for issue in payload["issues"]
    }


def test_baseline_profile_does_not_apply_direct_dac_diagnostic_route(
    tmp_path: Path,
) -> None:
    # A coherent single DAC with NO active outputd lane (un-profiled). Durable
    # baseline apply must resolve MISSING — never silently fall back to a
    # direct-DAC diagnostic route. (The registered DAC8x now declares an active
    # lane, so it would resolve to that lane; this invariant needs a no-lane
    # single to exercise the "missing, not direct-dac" path.)
    topology = _topology(
        device_id="unregistered_lab_dac",
        device_label="Unregistered lab DAC",
        card_id="LabDAC",
    )
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_apply"] is False
    assert payload["config"]["playback_device_source"] == "missing"
    assert "baseline_playback_device_missing" in {
        issue["code"] for issue in payload["issues"]
    }


def test_baseline_profile_missing_evidence_does_not_invent_route_width_block(
    tmp_path: Path,
) -> None:
    topology = _stereo_three_way_topology()

    payload = build_baseline_profile_candidate(
        topology,
        design_draft={},
        crossover_preview={},
        measurements={},
        write=False,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    issue_codes = {issue["code"] for issue in payload["issues"]}
    assert "baseline_crossover_preview_not_ready" in issue_codes
    assert "baseline_driver_measurements_missing" in issue_codes
    assert "baseline_summed_validation_missing" in issue_codes
    assert "active_playback_route_too_narrow" not in issue_codes


async def test_apply_baseline_profile_uses_shared_dsp_apply_transaction(
    monkeypatch,
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    calls: list[str] = []
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    current_path = str(prior)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    async def load_config(path: str) -> bool:
        nonlocal current_path
        calls.append(path)
        current_path = path
        return True

    async def current_config_path() -> str:
        return current_path

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "applied"
    assert payload["profile"]["status"] == "applied"
    assert payload["profile"]["permissions"]["may_apply"] is False
    # #1666: the transaction loads the candidate's own content-addressed
    # sibling, never the literal config_path passed in.
    assert calls == [payload["profile"]["config"]["path"]]
    assert payload["profile"]["config"]["path"] != str(tmp_path / "active_speaker_baseline.yml")
    snapshot = payload["profile"]["recomposition_snapshot"]
    assert snapshot["schema_version"] == 1
    assert snapshot["domain"] == "full"
    assert snapshot["corrections"] == payload["profile"]["corrections"]

    # Production recompose consumes only the applied snapshot. Mutable design /
    # measurement stores are intentionally not arguments, so later captures
    # cannot change Layer A while applying preference or room EQ.
    recomposed, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=payload["profile"],
    )
    assert issues == []
    assert recomposed == (tmp_path / "active_speaker_baseline.yml").read_text()


async def test_apply_baseline_profile_preserves_only_current_sealed_bass_block(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from jasper.bass_extension.profile import (
        evaluate_bass_extension_profile,
        save_bass_extension_profile,
    )
    from jasper.active_speaker.runtime_contract import classify_bass_extension_graph
    from tests.test_active_speaker_runtime_contract import _sealed_profile

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    bass_path = tmp_path / "bass_extension_profile.json"
    monkeypatch.setenv("JASPER_BASS_EXTENSION_PROFILE_STATE", str(bass_path))
    loaded_graphs: list[str] = []

    async def load_config(path: str) -> bool:
        loaded_graphs.append(Path(path).read_text(encoding="utf-8"))
        return True

    first = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )
    profile = _sealed_profile(topology, first["profile"])
    save_bass_extension_profile(profile, bass_path)

    with mock.patch(
        "jasper.active_speaker.runtime_contract.classify_bass_extension_graph",
        wraps=classify_bass_extension_graph,
    ) as prove:
        repeated = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            state_path=state_path,
            config_path=config_path,
            validate=_valid_config,
        )

    assert repeated["status"] == "applied"
    assert "bass_ext_lt" in loaded_graphs[-1]
    assert "bass_ext_subsonic" in loaded_graphs[-1]
    assert evaluate_bass_extension_profile(
        path=bass_path,
        topology=topology,
        applied_baseline_state=repeated["profile"],
    ).status == "accepted"
    assert prove.call_args.kwargs["desired_profile"] == profile

    deferred = replace(
        profile,
        enclosure={
            "adapter_id": "ported_v1",
            "adapter_version": 1,
            "cabinet_fingerprint": "ported-cabinet",
        },
        natural={
            "fb_hz": 43.1,
            "knee_hz": 55.0,
            "knee_slope_db_oct": 21.0,
            "fit_rms_db": 0.4,
            "natural_curve": {
                "freqs_hz": np.geomspace(10.0, 500.0, 96).tolist(),
                "magnitude_db": [0.0] * 96,
            },
            "notes": [],
        },
    )
    save_bass_extension_profile(deferred, bass_path)
    with mock.patch(
        "jasper.active_speaker.runtime_contract.classify_bass_extension_graph",
        wraps=classify_bass_extension_graph,
    ) as prove:
        deferred_apply = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            state_path=state_path,
            config_path=config_path,
            validate=_valid_config,
        )
    assert deferred_apply["status"] == "applied"
    assert "bass_ext_lt" not in loaded_graphs[-1]
    assert prove.call_args.kwargs["desired_profile"] == deferred
    assert evaluate_bass_extension_profile(
        path=bass_path,
        topology=topology,
        applied_baseline_state=deferred_apply["profile"],
    ).status == "accepted"

    bypassed = replace(profile, status="bypassed")
    save_bass_extension_profile(bypassed, bass_path)
    with mock.patch(
        "jasper.active_speaker.runtime_contract.classify_bass_extension_graph",
        wraps=classify_bass_extension_graph,
    ) as prove:
        bypassed_apply = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            state_path=state_path,
            config_path=config_path,
            validate=_valid_config,
        )
    assert bypassed_apply["status"] == "applied"
    assert "bass_ext_lt" not in loaded_graphs[-1]
    assert prove.call_args.kwargs["desired_profile"] == bypassed
    assert evaluate_bass_extension_profile(
        path=bass_path,
        topology=topology,
        applied_baseline_state=bypassed_apply["profile"],
    ).status == "bypassed"

    save_bass_extension_profile(profile, bass_path)
    changed_manual = {
        "drivers": [
            {"role": "woofer", "gain_offset_db": 0.0},
            {"role": "tweeter", "gain_offset_db": -7.0},
        ],
        "crossover_candidates": _research()["crossover_candidates"],
    }
    changed_draft = build_design_draft(
        topology,
        driver_research=_research(),
        manual_settings=changed_manual,
        created_at="2026-07-18T12:00:00Z",
    )
    changed_preview = build_crossover_preview(changed_draft)
    with mock.patch(
        "jasper.active_speaker.runtime_contract.classify_bass_extension_graph",
        wraps=classify_bass_extension_graph,
    ) as prove:
        changed = await apply_baseline_profile(
            topology,
            design_draft=changed_draft,
            crossover_preview=changed_preview,
            measurements=measurements,
            load_config=load_config,
            state_path=state_path,
            config_path=config_path,
            validate=_valid_config,
        )

    assert changed["status"] == "applied"
    assert "bass_ext_lt" not in loaded_graphs[-1]
    assert "bass_ext_subsonic" not in loaded_graphs[-1]
    assert evaluate_bass_extension_profile(
        path=bass_path,
        topology=topology,
        applied_baseline_state=changed["profile"],
    ).status == "stale"
    assert prove.call_args.kwargs["desired_profile"] == profile


async def test_apply_baseline_profile_refuses_failed_graph_proof_before_load(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from jasper.active_speaker.runtime_contract import GRAPH_UNSAFE, GraphSafety

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    loads: list[str] = []

    async def load_config(path: str) -> bool:
        loads.append(path)
        return True

    monkeypatch.setattr(
        "jasper.active_speaker.runtime_contract.classify_bass_extension_graph",
        lambda *_args, **_kwargs: GraphSafety(
            classification=GRAPH_UNSAFE,
            allowed=False,
            issues=({
                "severity": "blocker",
                "code": "injected_graph_refusal",
                "message": "injected whole-graph refusal",
            },),
        ),
    )

    result = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert result["status"] == "blocked"
    assert loads == []
    assert "baseline_graph_safety_proof_failed" in {
        issue["code"] for issue in result["issues"]
    }


async def test_apply_baseline_profile_driver_domain_path_remains_unchanged(
    monkeypatch,
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    loaded: list[str] = []
    monkeypatch.setenv(
        "JASPER_BASS_EXTENSION_PROFILE_STATE",
        str(tmp_path / "missing_bass_profile.json"),
    )

    async def load_config(path: str) -> bool:
        loaded.append(Path(path).read_text(encoding="utf-8"))
        return True

    result = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        state_path=tmp_path / "follower_state.json",
        config_path=tmp_path / "follower_driver_domain.yml",
        capture_device="hw:CARD=Loopback,DEV=1",
        driver_domain=True,
        program_channel="left",
        validate=_valid_config,
    )

    assert result["status"] == "applied"
    assert len(loaded) == 1
    assert "emit_active_speaker_driver_domain_config" in loaded[0]
    assert "# program_channel=left" in loaded[0]
    assert "active_baseline_headroom" not in loaded[0]


async def test_apply_baseline_profile_refuses_stale_reviewed_candidate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    base_manual = {
        "drivers": [
            {"role": "woofer", "gain_offset_db": 0.0},
            {"role": "tweeter", "gain_offset_db": -6.0},
        ],
        "crossover_candidates": _research()["crossover_candidates"],
    }
    draft_a = build_design_draft(
        topology,
        driver_research=_research(),
        manual_settings=base_manual,
        created_at="2026-06-14T12:00:00Z",
    )
    preview_a = build_crossover_preview(draft_a)
    measurements = _measurements(topology, tmp_path)
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    candidate_a = build_baseline_profile_candidate(
        topology,
        design_draft=draft_a,
        crossover_preview=preview_a,
        measurements=measurements,
        write=False,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )
    manual_b = deepcopy(base_manual)
    manual_b["drivers"][1]["gain_offset_db"] = -7.0
    draft_b = build_design_draft(
        topology,
        driver_research=_research(),
        manual_settings=manual_b,
        created_at="2026-06-14T12:00:00Z",
    )
    preview_b = build_crossover_preview(draft_b)
    loads: list[str] = []
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    async def load_config(path: str) -> bool:
        loads.append(path)
        return True

    async def unexpected_bundle_write(*_args, **_kwargs):
        pytest.fail("stale candidate refusal must not write the evidence bundle")

    async def unexpected_candidate_verified() -> None:
        pytest.fail("stale candidate refusal must not run pre-load side effects")

    monkeypatch.setattr(
        baseline_profile_mod,
        "_record_apply_outcome_into_bundle",
        unexpected_bundle_write,
    )

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft_a,
        crossover_preview=preview_a,
        measurements=measurements,
        load_config=load_config,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
        expected_candidate_fingerprint=str(candidate_a["candidate_fingerprint"]),
        on_candidate_verified=unexpected_candidate_verified,
        refresh_inputs=lambda: (topology, draft_b, preview_b, measurements),
    )

    assert payload["status"] == "blocked"
    assert payload["apply"] is None
    assert loads == []
    assert not state_path.exists()
    assert not config_path.exists()
    assert "baseline_candidate_fingerprint_mismatch" in {
        issue["code"] for issue in payload["issues"]
    }
    assert (
        payload["profile"]["candidate_fingerprint"]
        != candidate_a["candidate_fingerprint"]
    )


async def test_apply_holds_writer_lock_and_refuses_config_race(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from contextlib import asynccontextmanager

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    reviewed = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )
    real_lock = baseline_profile_mod.dsp_writer_lock
    lock_held = False
    observed_sources: list[str] = []

    @asynccontextmanager
    async def observed_lock(config_dir, *, source):
        nonlocal lock_held
        observed_sources.append(source)
        async with real_lock(config_dir, source=source):
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

    monkeypatch.setattr(baseline_profile_mod, "dsp_writer_lock", observed_lock)
    validations = 0

    def racing_validate(path: str | Path) -> CamillaConfigValidationResult:
        nonlocal validations
        assert lock_held is True
        validations += 1
        result = _valid_config(path)
        if validations == 2:
            target = Path(path)
            target.write_text(
                target.read_text(encoding="utf-8") + "# raced writer\n",
                encoding="utf-8",
            )
        return result

    loads: list[str] = []

    async def load_config(path: str) -> bool:
        assert lock_held is True
        loads.append(path)
        return True

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        state_path=state_path,
        config_path=config_path,
        validate=racing_validate,
        expected_candidate_fingerprint=reviewed["candidate_fingerprint"],
    )

    assert payload["status"] == "apply_failed"
    assert payload["apply"]["result"] == "candidate_changed"
    assert validations == 2
    assert loads == []
    assert lock_held is False
    assert observed_sources == ["active_speaker_baseline_apply"]


async def test_apply_baseline_profile_threads_capture_device(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Slice 1: apply_baseline_profile threads capture_device into the emitted
    config, so the multiroom reconciler can apply a follower's round-trip-loopback
    baseline. The default keeps the solo apply byte-identical.
    """
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    config_path = tmp_path / "active_speaker_baseline.yml"
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    current_path = str(prior)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str:
        return current_path

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_path,
        capture_device="hw:CARD=Loopback,DEV=1",
        validate=_valid_config,
    )

    assert payload["status"] == "applied"
    assert 'device: "hw:CARD=Loopback,DEV=1"' in config_path.read_text(encoding="utf-8")


async def test_apply_baseline_profile_records_apply_outcome_into_bundle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """apply_baseline_profile's single chokepoint (STEP 1 CONTRACT §7.4)
    records every apply attempt into the active-speaker commissioning bundle
    the run's comparison set was stamped with — see
    jasper.active_speaker.bundles.record_apply."""

    from jasper.active_speaker import bundles
    from jasper.active_speaker.measurement import (
        active_driver_targets,
        start_active_comparison_set,
    )

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(sessions_dir))
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    state_path = tmp_path / "measurements.json"
    _measurements(topology, tmp_path)

    bundle = bundles.open_bundle(topology, calibration_id="cal-1")
    assert bundle is not None
    driver_level_locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        }
        for target in active_driver_targets(topology)
    }
    start_active_comparison_set(
        topology,
        profile_context_id="ctx-1",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="cal-1",
        driver_level_locks=driver_level_locks,
        bundle_session_id=bundle["session_id"],
        state_path=state_path,
    )
    measurements = load_measurement_state(topology, state_path=state_path)
    assert (
        measurements["active_comparison_set"]["bundle_session_id"]
        == bundle["session_id"]
    )

    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    current_path = str(prior)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str:
        return current_path

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    assert payload["status"] == "applied"

    bundle_dir = Path(bundle["bundle_dir"])
    summary = bundles.summarize_bundle(bundle_dir)
    assert summary["state"] == "applied"
    assert summary["has_apply"] is True
    assert summary["has_proposal"] is True
    full = bundles._read_info(bundle_dir)
    assert full["apply"] is not None
    assert (
        full["fingerprints"]["graph_fingerprint"]
        == payload["profile"]["source"]["fingerprint"]
    )
    assert full["rollback_target"] == {"config_path": str(prior)}


async def test_apply_baseline_profile_records_blocked_attempt_into_bundle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A blocked apply (not ready — see test_baseline_profile_blocks_*) never
    reaches the DSP transaction, but the attempt is still evidence: the
    bundle records it as a failed apply, not silently dropped."""

    from jasper.active_speaker import bundles
    from jasper.active_speaker.measurement import (
        active_driver_targets,
        start_active_comparison_set,
    )

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(sessions_dir))
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    state_path = tmp_path / "measurements.json"
    # No driver/summed measurements recorded: automatic_candidate_readiness
    # is not ready, so build_baseline_profile_candidate blocks the apply.

    bundle = bundles.open_bundle(topology, calibration_id="")
    assert bundle is not None
    driver_level_locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        }
        for target in active_driver_targets(topology)
    }
    start_active_comparison_set(
        topology,
        profile_context_id="ctx-1",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks=driver_level_locks,
        bundle_session_id=bundle["session_id"],
        state_path=state_path,
    )
    measurements = load_measurement_state(topology, state_path=state_path)

    async def load_config(_path: str) -> bool:
        return True

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        tuning_owner="automatic",
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    assert payload["status"] == "blocked"

    bundle_dir = Path(bundle["bundle_dir"])
    full = bundles._read_info(bundle_dir)
    assert full["state"] == "failed"
    assert full["apply"] is None


async def test_apply_baseline_profile_is_a_noop_when_no_bundle_is_open(
    tmp_path: Path,
) -> None:
    """A comparison set never gets a bundle_session_id (a manual-only apply,
    a follower/driver_domain apply): apply_baseline_profile must complete
    exactly as before — the bundle hook is additive and silent when there is
    nothing to record into."""

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    assert measurements.get("active_comparison_set") is None

    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    current_path = str(prior)

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str:
        return current_path

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "applied"


async def test_new_candidate_cannot_overwrite_applied_graph_or_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    state_path = tmp_path / "baseline_profile.json"
    applied_config_path = tmp_path / "active_speaker_baseline.yml"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(applied_config_path),
    )
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    current_path: str | None = None

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def get_current_config_path() -> str | None:
        return current_path

    first_draft = _draft(topology, tweeter_gain_db=-18.5)
    first_preview = build_crossover_preview(first_draft)
    measurements = _measurements(topology, tmp_path)
    applied = await apply_baseline_profile(
        topology,
        design_draft=first_draft,
        crossover_preview=first_preview,
        measurements=measurements,
        load_config=load_config,
        get_current_config_path=get_current_config_path,
        state_path=state_path,
        validate=_valid_config,
    )
    assert applied["status"] == "applied"
    applied_yaml = applied_config_path.read_text(encoding="utf-8")
    applied_snapshot = applied["profile"]["recomposition_snapshot"]

    second_draft = _draft(topology, tweeter_gain_db=-10.0)
    second_draft["updated_at"] = "2026-07-10T13:00:00Z"
    second_preview = build_crossover_preview(second_draft)
    candidate = build_baseline_profile_candidate(
        topology,
        design_draft=second_draft,
        crossover_preview=second_preview,
        measurements=measurements,
        write=True,
        state_path=state_path,
        validate=_valid_config,
    )

    candidate_path = Path(candidate["config"]["path"])
    assert candidate_path != applied_config_path
    assert "_candidate_" in candidate_path.name
    assert applied_config_path.read_text(encoding="utf-8") == applied_yaml
    retained = load_applied_baseline_profile_state(state_path)
    assert retained is not None
    assert retained["recomposition_snapshot"] == applied_snapshot
    assert retained["provisional"] is applied["profile"]["provisional"]
    assert candidate["applied_recomposition_profile"] == retained


def test_applied_candidate_identity_is_rederived_from_frozen_snapshot(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    state_path = tmp_path / "baseline_profile.json"
    candidate = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=state_path,
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    saved = dict(candidate)
    saved["status"] = "applied"
    saved["candidate_fingerprint"] = "declared-wrong"
    state_path.write_text(json.dumps(saved), encoding="utf-8")
    expected = baseline_candidate_fingerprint(saved)

    frozen = load_applied_baseline_profile_state(state_path)
    assert frozen is not None
    assert frozen["candidate_fingerprint"] == expected
    assert frozen["candidate_fingerprint"] != "declared-wrong"

    rebuilt = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        state_path=state_path,
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    assert rebuilt["candidate_fingerprint"] == expected
    assert (
        rebuilt["applied_recomposition_profile"]["candidate_fingerprint"]
        == expected
    )


# --- Fail-safe level trim derived from the driver sensitivity gap -------------
#
# Regression cover for the DE250 compression-driver bug: research that declares
# sensitivities (woofer 83.3 dB, tweeter 108.5 dB) but no explicit
# gain_offset_db used to compile both drivers at 0 dB, leaving the ~25 dB-hotter
# horn at full level (shrill / horn-dominant, and a diaphragm hazard).


def _research_with_sensitivity(
    *,
    woofer_sens_db: float = 83.3,
    tweeter_sens_db: float = 108.5,
    tweeter_gain_db: float | None = None,
    tweeter_gain_provenance: str | None = None,
) -> dict:
    tweeter: dict = {
        "role": "tweeter",
        "model": "DE250-8",
        "sensitivity_db_2v83_1m": tweeter_sens_db,
        "recommended_highpass_hz": 2000,
        "do_not_test_below_hz": 1600,
        "sources": ["https://example.test/tweeter"],
    }
    if tweeter_gain_db is not None:
        tweeter["gain_offset_db"] = tweeter_gain_db
    if tweeter_gain_provenance is not None:
        tweeter["gain_offset_db_provenance"] = tweeter_gain_provenance
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [
            {
                "role": "woofer",
                "model": "Epique E150HE-44",
                "sensitivity_db_2v83_1m": woofer_sens_db,
                "usable_frequency_range_hz": [30, 4000],
                "recommended_lowpass_hz": 2000,
                "sources": ["https://example.test/woofer"],
            },
            tweeter,
        ],
        "crossover_candidates": [
            {
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2000,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            }
        ],
    }


def _baseline_payload(topology: OutputTopology, research: dict, tmp_path: Path) -> dict:
    draft = build_design_draft(
        topology,
        driver_research=research,
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    return build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        created_at="2026-06-19T12:20:00Z",
    )


def test_baseline_profile_derives_level_trim_from_sensitivity_gap(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    payload = _baseline_payload(
        topology,
        _research_with_sensitivity(),  # 25.2 dB gap, no explicit gain_offset_db
        tmp_path,
    )

    assert payload["status"] == "ready_to_apply"
    # Hotter horn is attenuated to the woofer reference; woofer stays at unity.
    assert payload["corrections"]["tweeter"]["gain_db"] == -25.2
    assert payload["corrections"]["woofer"]["gain_db"] == 0.0
    assert payload["safety"]["positive_gain_allowed"] is False
    assert "driver_gain_derived_from_sensitivity" in {
        issue["code"] for issue in payload["issues"]
    }


def test_baseline_profile_explicit_gain_overrides_sensitivity_trim(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    payload = _baseline_payload(
        topology,
        _research_with_sensitivity(tweeter_gain_db=-18.5),
        tmp_path,
    )

    # An explicit (e.g. measured) trim wins over the sensitivity heuristic.
    assert payload["corrections"]["tweeter"]["gain_db"] == -18.5
    assert "driver_gain_derived_from_sensitivity" not in {
        issue["code"] for issue in payload["issues"]
    }


def test_baseline_profile_no_trim_when_sensitivities_match(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    payload = _baseline_payload(
        topology,
        _research_with_sensitivity(woofer_sens_db=90.0, tweeter_sens_db=90.0),
        tmp_path,
    )

    assert payload["corrections"]["tweeter"]["gain_db"] == 0.0
    assert payload["corrections"]["woofer"]["gain_db"] == 0.0
    assert "driver_gain_derived_from_sensitivity" not in {
        issue["code"] for issue in payload["issues"]
    }


def test_baseline_profile_folds_declared_pad_into_the_sensitivity_gap(
    tmp_path: Path,
) -> None:
    """#1665: an L-pad'd (or otherwise attenuated) tweeter's EFFECTIVE
    sensitivity -- naked minus the pad's own hardware attenuation -- is what
    should set the software trim. Double-attenuating (a full naked-sensitivity
    trim stacked on top of a hardware pad already doing part of the work)
    would needlessly starve the tweeter."""
    topology = _dual_apple_topology()
    research = _research_with_sensitivity()  # naked gap 25.2 dB, no explicit gain
    research["drivers"][1]["pad"] = {"kind": "direct_db", "attenuation_db": -10.0}
    payload = _baseline_payload(topology, research, tmp_path)

    assert payload["status"] == "ready_to_apply"
    # Naked gap is 25.2 dB (108.5 - 83.3); a -10 dB pad already does some of
    # that work in hardware, so the remaining software trim is 15.2 dB.
    assert payload["corrections"]["tweeter"]["gain_db"] == -15.2
    assert payload["corrections"]["woofer"]["gain_db"] == 0.0


def test_recompose_baseline_yaml_matches_durable_builder_when_flat(
    tmp_path: Path,
) -> None:
    # recompose_baseline_yaml is the carrier's composition seam (PR-3). With no
    # preference EQ it must reproduce the durable builder's config byte-for-byte
    # (it reuses the SAME derivation primitives), so applying flat EQ on an
    # active speaker is a no-op on the protected graph.
    from jasper.active_speaker.baseline_profile import recompose_baseline_yaml

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    config_path = tmp_path / "active_speaker_baseline.yml"
    built = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_path,
        validate=_valid_config,
    )
    # #1666: candidate lands on a content-addressed sibling, not config_path.
    durable_yaml = Path(built["config"]["path"]).read_text(encoding="utf-8")

    flat_yaml, flat_issues = recompose_baseline_yaml(
        topology,
        crossover_preview=preview,
        measurements=measurements,
    )
    assert flat_issues == []
    assert flat_yaml == durable_yaml


def test_recompose_baseline_yaml_inserts_preference_eq_and_stays_approved(
    tmp_path: Path,
) -> None:
    # The keystone (invariant 2), end-to-end through the recompose seam: a
    # +6 dB preference (a +4 dB highshelf -- a SHELF, the conservative
    # easy-to-get-wrong case -- plus a +2 dB peak) rides PRE-SPLIT at unity,
    # and the emitted graph still re-proves as GRAPH_APPROVED_ACTIVE_RUNTIME.
    # Adding EQ never breaks the protection contract.
    import re

    from jasper.active_speaker.baseline_profile import recompose_baseline_yaml
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        classify_camilla_graph,
    )
    from jasper.camilla_config_contract import FilterSpec

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)

    prefs = [
        FilterSpec(name="pref_hs", biquad_type="Highshelf", freq=8000.0, gain=4.0, slope=6.0),
        FilterSpec(name="pref_pk", biquad_type="Peaking", freq=120.0, gain=2.0, q=1.0),
    ]
    eq_yaml, eq_issues = recompose_baseline_yaml(
        topology,
        crossover_preview=preview,
        measurements=measurements,
        preference_filters=prefs,
    )
    assert eq_issues == []
    assert "pref_hs:" in eq_yaml and "pref_pk:" in eq_yaml
    assert "volume_limit: 0.0" in eq_yaml

    # invariant 4 (emitter-side): preference boosts ride at unity just like the
    # stereo /sound path. The active graph protects drivers by placing EQ
    # pre-split, not by adding automatic program attenuation.
    match = re.search(
        r"active_baseline_headroom:\n\s+type: Gain\n\s+parameters: \{ gain: (-?\d+\.\d+)",
        eq_yaml,
    )
    assert match is not None
    assert float(match.group(1)) == 0.0

    # invariant 5: the preference filter step is wired on the program channels
    # strictly BEFORE the split mixer.
    pipeline = eq_yaml[eq_yaml.index("\npipeline:"):]
    pref_idx = pipeline.index("pref_hs, pref_pk")
    mixer_idx = pipeline.index("type: Mixer")
    assert pref_idx < mixer_idx

    # invariant 2 (keystone): the protection contract still holds.
    graph = classify_camilla_graph(
        topology=topology,
        text=eq_yaml,
        bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
    )
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert graph.allowed is True

    # output_trim_db (manual headroom + loudness match) threads through recompose
    # and folds into the SAME headroom gain (0 baseline + 4 trim = 4), so the
    # active EQ apply honours the household's loudness setting; still APPROVED.
    trimmed_yaml, trim_issues = recompose_baseline_yaml(
        topology,
        crossover_preview=preview,
        measurements=measurements,
        preference_filters=prefs,
        output_trim_db=4.0,
    )
    assert trim_issues == []
    trim_match = re.search(
        r"active_baseline_headroom:\n\s+type: Gain\n\s+parameters: \{ gain: (-?\d+\.\d+)",
        trimmed_yaml,
    )
    assert trim_match is not None and float(trim_match.group(1)) == -4.0
    assert classify_camilla_graph(
        topology=topology,
        text=trimmed_yaml,
        bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
    ).allowed is True


def test_recompose_baseline_yaml_inserts_room_peqs_and_folds_headroom(
    tmp_path: Path,
) -> None:
    # Active room correction rides the same safe pre-split program bus as
    # preference EQ, but positive room boosts are correction safety headroom:
    # they fold into active_baseline_headroom instead of emitting a separate
    # room_headroom gain.
    import re

    from jasper.active_speaker.baseline_profile import recompose_baseline_yaml
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        classify_camilla_graph,
    )

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)

    room_peqs = [
        PeqFilter(freq=45.0, q=5.0, gain=2.0),
        PeqFilter(freq=80.0, q=6.0, gain=-4.0),
        PeqFilter(freq=120.0, q=4.0, gain=1.0),
    ]
    room_yaml, room_issues = recompose_baseline_yaml(
        topology,
        crossover_preview=preview,
        measurements=measurements,
        room_peqs=room_peqs,
    )
    assert room_issues == []
    assert room_yaml is not None
    assert "room_peq_1:" in room_yaml and "room_peq_3:" in room_yaml
    assert "room_headroom" not in room_yaml
    match = re.search(
        r"active_baseline_headroom:\n\s+type: Gain\n\s+parameters: \{ gain: (-?\d+\.\d+)",
        room_yaml,
    )
    assert match is not None
    assert float(match.group(1)) == -3.0

    pipeline = room_yaml[room_yaml.index("\npipeline:"):]
    assert (
        pipeline.index("names: [room_peq_1, room_peq_2, room_peq_3]")
        < pipeline.index("names: [active_baseline_headroom]")
        < pipeline.index("type: Mixer")
    )

    graph = classify_camilla_graph(
        topology=topology,
        text=room_yaml,
        bass_profile_summary=NO_BASS_EXTENSION_PROFILE_SUMMARY,
    )
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert graph.allowed is True, graph.issues


def test_applied_room_and_reset_only_mutate_program_domain(tmp_path: Path) -> None:
    """Room apply/reset preserve the exact immutable Layer-A suffix.

    The production carrier calls ``recompose_applied_baseline_yaml`` for both
    Room apply and the shared Reset/automatic-revert no-room target.  Compare
    the parsed driver-domain graph, not just filter counts: routing, crossover
    filters, polarity, delay, gain, and protection must remain identical while
    Room PEQs and their headroom live only before the split mixer.
    """
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    measurements = _measurements(topology, tmp_path)
    applied = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    applied["status"] = "applied"

    flat_yaml, flat_issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
    )
    room_yaml, room_issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        room_peqs=[
            PeqFilter(freq=45.0, q=5.0, gain=2.0),
            PeqFilter(freq=80.0, q=6.0, gain=-4.0),
        ],
    )
    reset_yaml, reset_issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        room_peqs=[],
    )

    assert flat_issues == room_issues == reset_issues == []
    assert flat_yaml is not None and room_yaml is not None
    assert reset_yaml == flat_yaml
    flat = yaml_lib.safe_load(flat_yaml)
    room = yaml_lib.safe_load(room_yaml)

    def driver_domain(document: dict) -> dict:
        pipeline = document["pipeline"]
        split_index = next(
            index
            for index, step in enumerate(pipeline)
            if step.get("type") == "Mixer"
        )
        suffix = pipeline[split_index:]
        driver_filter_names = {
            name
            for step in suffix
            if step.get("type") == "Filter"
            for name in step.get("names", [])
        }
        return {
            "devices": document["devices"],
            "mixers": document["mixers"],
            "pipeline_suffix": suffix,
            "filters": {
                name: document["filters"][name]
                for name in sorted(driver_filter_names)
            },
        }

    assert driver_domain(room) == driver_domain(flat)
    room_split_index = next(
        index
        for index, step in enumerate(room["pipeline"])
        if step.get("type") == "Mixer"
    )
    assert any(
        name.startswith("room_peq_")
        for step in room["pipeline"][:room_split_index]
        for name in step.get("names", [])
    )
    assert not any(
        name.startswith("room_peq_")
        for step in room["pipeline"][room_split_index:]
        for name in step.get("names", [])
    )


def _applied_layer_a_yaml(tmp_path: Path) -> str:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    applied = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=_measurements(topology, tmp_path),
        write=False,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    applied["status"] = "applied"
    text, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
    )
    assert issues == []
    assert text is not None
    return text


@pytest.mark.parametrize("mutation", ["playback", "mixer", "pipeline_suffix"])
def test_layer_a_fingerprint_rejects_every_bound_domain_mutation(
    tmp_path: Path,
    mutation: str,
) -> None:
    baseline_yaml = _applied_layer_a_yaml(tmp_path)
    changed = yaml_lib.safe_load(baseline_yaml)
    split_index = next(
        index
        for index, step in enumerate(changed["pipeline"])
        if step.get("type") == "Mixer"
    )
    split_name = changed["pipeline"][split_index]["name"]
    if mutation == "playback":
        changed["devices"]["playback"]["device"] = "unexpected_output"
    elif mutation == "mixer":
        source = changed["mixers"][split_name]["mapping"][0]["sources"][0]
        source["gain"] = float(source.get("gain", 0.0)) - 0.25
    else:
        driver_step = changed["pipeline"][split_index + 1]
        driver_step["channels"] = [int(driver_step["channels"][0]) + 1]

    assert active_layer_a_fingerprint(yaml_lib.safe_dump(changed)) != (
        active_layer_a_fingerprint(baseline_yaml)
    )


def test_layer_a_fingerprint_ignores_capture_only_mutation(tmp_path: Path) -> None:
    baseline_yaml = _applied_layer_a_yaml(tmp_path)
    changed = yaml_lib.safe_load(baseline_yaml)
    changed["devices"]["capture"] = {
        "type": "Alsa",
        "channels": 2,
        "device": "alternate_program_capture",
        "format": "S32_LE",
    }

    assert active_layer_a_fingerprint(yaml_lib.safe_dump(changed)) == (
        active_layer_a_fingerprint(baseline_yaml)
    )


def test_layer_a_fingerprint_ignores_camilla_readback_null_defaults(
    tmp_path: Path,
) -> None:
    baseline_yaml = _applied_layer_a_yaml(tmp_path)
    readback = yaml_lib.safe_load(baseline_yaml)
    readback["devices"].update({
        "adjust_period": None,
        "multithreaded": None,
        "volume_ramp_time": None,
    })
    split_index = next(
        index
        for index, step in enumerate(readback["pipeline"])
        if step.get("type") == "Mixer"
    )
    split_name = readback["pipeline"][split_index]["name"]
    for step in readback["pipeline"][split_index:]:
        step.update({"bypassed": None, "description": None})
    for route in readback["mixers"][split_name]["mapping"]:
        route["mute"] = None
        for source in route["sources"]:
            source.update({"mute": None, "scale": None})
    for step in readback["pipeline"][split_index:]:
        for name in step.get("names", []):
            readback["filters"][name]["description"] = None
            readback["filters"][name]["parameters"]["scale"] = None

    assert active_layer_a_fingerprint(yaml_lib.safe_dump(readback)) == (
        active_layer_a_fingerprint(baseline_yaml)
    )


def test_recompose_baseline_yaml_refuses_when_preview_not_ready() -> None:
    # When the saved evidence can no longer produce a baseline, recompose returns
    # (None, issues) so the carrier refuses instead of emitting a partial graph.
    from jasper.active_speaker.baseline_profile import recompose_baseline_yaml

    topology = _dual_apple_topology()
    yaml, issues = recompose_baseline_yaml(
        topology,
        crossover_preview={},
        measurements={},
    )
    assert yaml is None
    assert any(
        issue["code"] == "baseline_crossover_preview_not_ready" for issue in issues
    )


# --- MEASURED level-match trim refines / overrides the datasheet trim ---------
#
# End-to-end: a phone near-field capture per driver through the production
# crossover produces an overlap-band level, and the measured driver-to-driver
# delta OVERRIDES the interim datasheet sensitivity trim. When no usable capture
# exists the datasheet trim is kept and the config is marked provisional.


def _driver_capture_wav(
    tmp_path: Path,
    name: str,
    *,
    kind: str,
    fc: float,
    gain_db: float,
    sr: int = 48000,
):
    """Synthesize a near-field driver capture through a crossover at ``fc``.

    A low-passed (woofer) or high-passed (tweeter) sweep at a relative level, the
    way the production graph would excite one driver. Returns ``(path, meta)``.
    """
    import numpy as np
    from scipy.signal import fftconvolve, firwin

    from jasper.active_speaker import driver_acoustics as da
    from jasper.audio_measurement import sweep as sweep_mod

    sig, meta = sweep_mod.synchronized_swept_sine(
        f1=da.DEFAULT_F1_HZ,
        f2=da.DEFAULT_F2_HZ,
        duration_approx_s=1.0,
        sample_rate=sr,
        amplitude_dbfs=da.DEFAULT_AMPLITUDE_DBFS,
    )
    gain = 10 ** (gain_db / 20)
    if kind == "lowpass":
        ir = (firwin(1023, fc, fs=sr) * gain).astype(np.float64)
    else:
        ir = (firwin(1023, fc, fs=sr, pass_zero=False) * gain).astype(np.float64)
    captured = fftconvolve(sig.astype(np.float64), ir)
    path = tmp_path / name
    sweep_mod.write_sweep_wav(path, captured.astype(np.float32), sr)
    return path, meta.to_dict()


def _acoustic_measurements(
    topology: OutputTopology,
    preview: dict,
    tmp_path: Path,
    *,
    fc: float,
    tweeter_hotter_db: float,
) -> dict:
    """Record real per-driver acoustic captures + a summed validation.

    The tweeter is measured ``tweeter_hotter_db`` hotter than the woofer at the
    handoff (the woofer is attenuated so the tweeter capture does not clip).
    """
    from jasper.active_speaker.commissioning_capture import (
        record_driver_acoustic_capture,
    )
    from jasper.active_speaker.capture_geometry import (
        DRIVER_PLACEMENT_POLICY_ID,
        SUMMED_PLACEMENT_POLICY_ID,
        normalized_placement_proof,
    )
    from jasper.active_speaker.measurement import (
        active_driver_targets,
        active_summed_targets,
        start_active_comparison_set,
    )
    from jasper.active_speaker.staging import compile_preset_from_crossover_preview

    preset, issues, _gates = compile_preset_from_crossover_preview(topology, dict(preview))
    assert preset is not None, issues
    state_path = tmp_path / "measurements.json"
    driver_targets = {
        target["role"]: target for target in active_driver_targets(topology)
    }
    comparison_set = start_active_comparison_set(
        topology,
        profile_context_id="protected-profile",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks={
            target["target_id"]: {
                "target_id": target["target_id"],
                "speaker_group_id": target["speaker_group_id"],
                "role": role,
                "tone_frequency_hz": 250.0 if role == "woofer" else 6250.0,
                "tone_peak_dbfs": -12.0,
                "commissioning_gain_db": 0.0,
                "locked_main_volume_db": -12.0,
            }
            for role, target in driver_targets.items()
        },
        state_path=state_path,
        now="2026-06-19T12:00:30Z",
    )
    page = {
        "capture_protocol_version": 2,
        "capture_page_build": "20260711.1",
    }

    for role, kind, output_index, gain_db in (
        ("woofer", "lowpass", 0, -tweeter_hotter_db),
        ("tweeter", "highpass", 1, 0.0),
    ):
        wav, meta = _driver_capture_wav(
            tmp_path, f"{role}.wav", kind=kind, fc=fc, gain_db=gain_db
        )
        playback_id = f"playback-{role}"
        out = record_driver_acoustic_capture(
            topology,
            preset,
            speaker_group_id="mono",
            role=role,
            captured_wav=wav,
            sweep_meta=meta,
            playback_id=playback_id,
            test_level_dbfs=-40.0,
            placement_proof=normalized_placement_proof(
                policy_id=DRIVER_PLACEMENT_POLICY_ID,
                acknowledgement_binding=f"binding-{role}-abcdefghijkl",
                relay_session_id=f"relay-{role}",
                capture_page=page,
                speaker_group_id="mono",
                role=role,
                target_fingerprint=driver_targets[role]["target_fingerprint"],
                comparison_set=comparison_set,
            ),
            safe_session=_safe_session(
                role=role, output_index=output_index, playback_id=playback_id
            ),
            state_path=state_path,
            now=f"2026-06-19T12:0{1 if role == 'woofer' else 2}:00Z",
        )
        assert out["recorded"] is True, out
        assert out["verdict"] == "present", out

    record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": "summed-playback-audible",
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "tone_summed.wav",
                    "metadata_basename": "tone_summed.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": fc, "level_dbfs": -72},
            },
        },
        state_path=state_path,
        now="2026-06-19T12:02:30Z",
    )
    return record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40.0,
            "polarity": "normal",
            "delay_ms": 0.0,
            "summed_test_id": "summed-playback-audible",
            "placement_proof": normalized_placement_proof(
                policy_id=SUMMED_PLACEMENT_POLICY_ID,
                acknowledgement_binding="binding-summed-abcdefghijkl",
                relay_session_id="relay-summed",
                capture_page=page,
                speaker_group_id="mono",
                role="summed",
                target_fingerprint=active_summed_targets(topology)[0][
                    "group_fingerprint"
                ],
                comparison_set=comparison_set,
            ),
        },
        state_path=state_path,
        now="2026-06-19T12:03:00Z",
    )


def test_baseline_measured_trim_overrides_datasheet(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    # Datasheet says the horn is 25.2 dB hotter; the MEASURED capture says 18 dB.
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),  # fc 2000, 25.2 dB gap
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=18.0
    )
    measurements["summary"]["latest_summed_validations"]["mono"]["acoustic"] = {
        "verdict": "blend_ok",
        "mic_clipping": False,
    }

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        created_at="2026-06-19T12:20:00Z",
    )

    assert payload["status"] == "ready_to_apply"
    # The MEASURED ~18 dB trim is used, not the 25.2 dB datasheet estimate.
    tweeter_trim = payload["corrections"]["tweeter"]["gain_db"]
    assert tweeter_trim == pytest.approx(-18.0, abs=1.5)
    assert abs(tweeter_trim - (-25.2)) > 3.0
    assert payload["corrections"]["woofer"]["gain_db"] == 0.0
    assert payload["corrections_source"]["tweeter"] == "measured"
    assert payload["provisional"] is False
    assert payload["safety"]["positive_gain_allowed"] is False
    codes = {issue["code"] for issue in payload["issues"]}
    assert "driver_gain_derived_from_measurement" in codes
    assert "driver_gain_derived_from_sensitivity" not in codes
    assert "baseline_level_match_provisional" not in codes


def test_baseline_measured_trim_overrides_ui_sensitivity_estimate(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    research = _research_with_sensitivity()
    draft = build_design_draft(
        topology,
        driver_research=research,
        manual_settings={
            "drivers": [{
                **research["drivers"][1],
                "gain_offset_db": -25.2,
                "gain_offset_db_provenance": "sensitivity_estimate",
            }],
            "crossover_candidates": [],
        },
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=18.0
    )

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["corrections"]["tweeter"]["gain_db"] == pytest.approx(
        -18.0, abs=1.5
    )
    assert payload["corrections_source"]["tweeter"] == "measured"
    assert payload["gain_provenance"]["tweeter"] == "sensitivity_estimate"
    assert payload["provisional"] is False


def test_baseline_provisional_when_no_measured_capture(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    # Operator-only records (no acoustic overlap evidence) + a sensitivity gap:
    # the datasheet trim is kept and the config is marked provisional.
    payload = _baseline_payload(topology, _research_with_sensitivity(), tmp_path)

    assert payload["status"] == "ready_to_apply"
    assert payload["corrections"]["tweeter"]["gain_db"] == -25.2  # datasheet
    assert payload["corrections_source"]["tweeter"] == "sensitivity"
    assert payload["provisional"] is True
    codes = {issue["code"] for issue in payload["issues"]}
    assert "driver_gain_derived_from_sensitivity" in codes
    assert "baseline_level_match_provisional" in codes
    assert "driver_gain_derived_from_measurement" not in codes


def _by_ear_measurements(topology: OutputTopology, tmp_path: Path) -> dict:
    """A fully by-ear commission: every driver confirmed by ear with NO mic
    reading, and the combined check recorded via an operator listening check —
    no phone capture anywhere. This is the path a household takes if they skip
    the optional level match."""
    state_path = tmp_path / "measurements.json"
    for role in ("woofer", "tweeter"):
        output_index = 0 if role == "woofer" else 1
        playback_id = f"playback-{role}"
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                # No observed_mic_dbfs — by ear only.
                "playback_id": playback_id,
            },
            safe_session=_safe_session(
                role=role, output_index=output_index, playback_id=playback_id
            ),
            state_path=state_path,
            now=f"2026-06-20T12:0{1 if role == 'woofer' else 2}:00Z",
        )
    record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": "summed-by-ear",
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "tone_summed.wav",
                    "metadata_basename": "tone_summed.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2000, "level_dbfs": -72},
            },
        },
        state_path=state_path,
        now="2026-06-20T12:02:30Z",
    )
    return record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "operator_listening_check": True,  # by ear, no mic reading
            "summed_test_id": "summed-by-ear",
        },
        state_path=state_path,
        now="2026-06-20T12:03:00Z",
    )


def test_baseline_applies_without_any_phone_level_match(tmp_path: Path) -> None:
    """The phone level match is OPTIONAL: a fully by-ear commission (no mic
    capture for any driver or the combined check) still compiles an applicable
    baseline — it just stays provisional on the datasheet trim."""
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-20T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-20T12:10:00Z")
    measurements = _by_ear_measurements(topology, tmp_path)

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        created_at="2026-06-20T12:20:00Z",
    )

    assert payload["status"] == "ready_to_apply"
    assert payload["permissions"]["may_apply"] is True
    assert payload["provisional"] is True
    assert payload["corrections_source"]["tweeter"] == "sensitivity"
    assert payload["level_match"]["groups_measured"] == 0


def test_baseline_explicit_gain_skips_measured(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(
            tweeter_gain_db=-15.0,
            tweeter_gain_provenance="operator_pinned",
        ),
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    # Even with usable measured captures, an explicit operator gain wins and the
    # measured chain is skipped (its reference assumption would be inconsistent).
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=18.0
    )

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        created_at="2026-06-19T12:20:00Z",
    )

    assert payload["corrections"]["tweeter"]["gain_db"] == -15.0
    assert payload["corrections_source"]["tweeter"] == "operator_pinned"
    assert payload["provisional"] is False
    assert payload["level_match"]["skipped_reason"] == "operator_pinned_gain"
    codes = {issue["code"] for issue in payload["issues"]}
    assert "driver_gain_derived_from_measurement" not in codes
    assert "driver_gain_derived_from_sensitivity" not in codes


def test_automatic_tuning_explicitly_overwrites_operator_pin(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(
            tweeter_gain_db=-15.0,
            tweeter_gain_provenance="operator_pinned",
        ),
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=18.0
    )
    measurements["summary"]["latest_summed_validations"]["mono"]["acoustic"] = {
        "verdict": "blend_ok",
        "mic_clipping": False,
    }

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        tuning_owner="automatic",
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "ready_to_apply"
    assert payload["tuning_owner"] == "automatic"
    assert payload["recomposition_snapshot"]["tuning_owner"] == "automatic"
    assert payload["corrections_source"]["tweeter"] == "measured"
    assert payload["corrections"]["tweeter"]["gain_db"] == pytest.approx(
        -18.0, abs=1.5
    )


def test_automatic_tuning_refuses_incomparable_excitation(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=18.0
    )
    measurements["summary"]["latest_driver_measurements"][
        "mono:tweeter"
    ]["excitation"]["effective_peak_dbfs"] += 1.0
    measurements["summary"]["latest_summed_validations"]["mono"]["acoustic"] = {
        "verdict": "blend_ok",
        "mic_clipping": False,
    }

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        tuning_owner="automatic",
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    assert payload["automatic_candidate"] == {
        "ready": False,
        "reason": "automatic_crossover_measurements_incomparable",
        "detail": (
            "Repeat the driver sweeps in one guided run so microphone placement, "
            "level, and excitation can be compared."
        ),
        "required_group_ids": ["mono"],
        "measured_group_ids": [],
        "summed_group_ids": ["mono"],
        "measurement_comparable": False,
        "excitation_comparable": False,
    }


def test_manual_migration_preserves_exact_applied_corrections(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-20T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-20T12:10:00Z")
    measurements = _by_ear_measurements(topology, tmp_path)
    preserved = {
        "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
        "tweeter": {"gain_db": -11.0, "delay_ms": 0.4, "inverted": True},
    }
    probe = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
    )
    applied = {
        "status": "applied",
        "source": probe["source"],
        "corrections": preserved,
    }

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        tuning_owner="manual",
        preserved_applied_profile=applied,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "ready_to_apply"
    assert payload["corrections"] == preserved
    assert payload["tuning_owner"] == "manual"
    assert payload["level_match"]["applied"] is False
    assert {issue["code"] for issue in payload["issues"]} >= {
        "manual_crossover_preserved"
    }
    # Wholesale carry-forward: every sub-parameter of every role is stamped
    # "preserved", distinct from the legacy operator_pinned sources/
    # gain_provenance stamping (kept byte-compatible) asserted below.
    assert payload["corrections_provenance"] == {
        role: {"gain_db": "preserved", "delay_ms": "preserved", "inverted": "preserved"}
        for role in preserved
    }
    assert payload["corrections_source"] == {role: "operator_pinned" for role in preserved}
    assert payload["gain_provenance"] == {role: "operator_pinned" for role in preserved}


def test_manual_migration_refuses_unsafe_preserved_gain(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-20T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-20T12:10:00Z")
    preserved = {
        "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
        "tweeter": {"gain_db": 1.0, "delay_ms": 0.0, "inverted": False},
    }
    measurements = _by_ear_measurements(topology, tmp_path)
    probe = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
    )

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        preserved_applied_profile={
            "status": "applied",
            "source": probe["source"],
            "corrections": preserved,
        },
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_apply"] is False
    assert "preserved_manual_correction_invalid" in {
        issue["code"] for issue in payload["issues"]
    }


def test_manual_migration_refuses_changed_crossover_preview(tmp_path: Path) -> None:
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-20T12:00:00Z",
    )
    applied_preview = build_crossover_preview(
        draft, created_at="2026-06-20T12:10:00Z"
    )
    measurements = _by_ear_measurements(topology, tmp_path)
    applied_source = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=applied_preview,
        measurements=measurements,
    )["source"]
    changed_preview = {
        **applied_preview,
        "updated_at": "2026-06-20T12:11:00Z",
    }
    config_path = tmp_path / "changed_candidate.yml"

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=changed_preview,
        measurements=measurements,
        tuning_owner="manual",
        preserved_applied_profile={
            "status": "applied",
            "source": applied_source,
            "corrections": {
                "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
                "tweeter": {"gain_db": -11.0, "delay_ms": 0.0, "inverted": False},
            },
        },
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_path,
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_apply"] is False
    assert not config_path.exists()
    assert "manual_crossover_source_changed" in {
        issue["code"] for issue in payload["issues"]
    }


# --- Persisted working-crossover values (Slice 0): polarity/delay ------------
#
# Precedence: [automatic tuning_owner + fresh authorized measured alignment
# evidence] > [persisted working-crossover values from the preview/preset] >
# [preserved_applied_profile carryover] > [schema defaults]. Manual tuning
# never consults measured alignment evidence for these two sub-parameters —
# mirrors the shipped gain rule that a manual pin is never silently replaced.


def _duck_preset(*, way_count: int = 2, crossover_regions=()) -> SimpleNamespace:
    """A minimal duck object exercising the SAME attributes _derive_corrections
    reads (preset.way_count, preset.crossover_regions) — mirrors the existing
    ``_derive_sensitivity_trims`` pattern above."""
    return SimpleNamespace(way_count=way_count, crossover_regions=list(crossover_regions))


def test_gain_source_to_provenance_migration_mapping_pinned():
    # SC-3's migration table, verbatim: explicit/operator_pinned -> manual,
    # measured -> measured, sensitivity/estimate -> recommended_start,
    # none -> no entry (an untouched role makes no provenance claim).
    assert _GAIN_SOURCE_TO_PROVENANCE["measured"] == PROVENANCE_MEASURED
    assert _GAIN_SOURCE_TO_PROVENANCE["operator_pinned"] == PROVENANCE_MANUAL
    assert _GAIN_SOURCE_TO_PROVENANCE["explicit"] == PROVENANCE_MANUAL
    assert _GAIN_SOURCE_TO_PROVENANCE["sensitivity"] == PROVENANCE_RECOMMENDED_START
    assert _GAIN_SOURCE_TO_PROVENANCE["estimate"] == PROVENANCE_RECOMMENDED_START
    assert "none" not in _GAIN_SOURCE_TO_PROVENANCE


def test_derive_corrections_manual_tier_sets_polarity_and_delay_from_region():
    region = CrossoverRegion(
        id="woofer_tweeter_2000hz",
        lower_driver="woofer",
        upper_driver="tweeter",
        fc_hz=2000.0,
        upper_polarity="inverted",
        delay_target_driver="tweeter",
        delay_ms=0.35,
    )
    preset = _duck_preset(crossover_regions=[region])

    corrections, _issues, meta = _derive_corrections(preset, {}, {})

    assert corrections["tweeter"]["inverted"] is True
    assert corrections["tweeter"]["delay_ms"] == 0.35
    assert corrections["woofer"]["inverted"] is False
    assert corrections["woofer"]["delay_ms"] == 0.0
    assert meta["corrections_provenance"]["tweeter"]["inverted"] == PROVENANCE_MANUAL
    assert meta["corrections_provenance"]["tweeter"]["delay_ms"] == PROVENANCE_MANUAL
    # "non-inverted" is indistinguishable from the schema default, so an
    # untouched role makes no provenance claim (mirrors gain's "none").
    assert "woofer" not in meta["corrections_provenance"]


def test_derive_corrections_manual_tier_sets_polarity_and_delay_via_manual_settings_entry_path():
    """Extends the test above (which hand-builds a CrossoverRegion in
    isolation) to the actual entry path a /sound/ manual-form submission
    takes: a manual_settings candidate -- auto-tagged source="manual_settings"
    by normalise_manual_settings, exactly like manualSettingsPayload() in
    deploy/assets/sound-profile/js/main.js sends -- through
    build_design_draft -> build_crossover_preview ->
    compile_preset_from_crossover_preview, and only THEN into
    _derive_corrections."""
    topology = _topology()
    draft = build_design_draft(
        topology,
        driver_research=_research(),
        manual_settings={
            "drivers": [],
            "crossover_candidates": [{
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
                "upper_polarity": "inverted",
                "delay_ms": 0.35,
                "delay_target_role": "tweeter",
            }],
        },
        created_at="2026-07-11T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-07-11T12:00:05Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues

    corrections, _issues, meta = _derive_corrections(preset, preview, {})

    assert corrections["tweeter"]["inverted"] is True
    assert corrections["tweeter"]["delay_ms"] == 0.35
    assert corrections["woofer"]["inverted"] is False
    assert meta["corrections_provenance"]["tweeter"]["inverted"] == PROVENANCE_MANUAL
    assert meta["corrections_provenance"]["tweeter"]["delay_ms"] == PROVENANCE_MANUAL


def test_derive_corrections_both_sides_inverted_is_schema_legal():
    # Both lower and upper "inverted" in the same region is schema-legal — the
    # preset author's intent, not a contradiction (net polarity is theirs to
    # judge). Emit both inversions.
    region = CrossoverRegion(
        id="woofer_tweeter_2000hz",
        lower_driver="woofer",
        upper_driver="tweeter",
        fc_hz=2000.0,
        lower_polarity="inverted",
        upper_polarity="inverted",
    )
    preset = _duck_preset(crossover_regions=[region])

    corrections, _issues, _meta = _derive_corrections(preset, {}, {})

    assert corrections["woofer"]["inverted"] is True
    assert corrections["tweeter"]["inverted"] is True


def test_baseline_config_emits_single_net_inversion_not_double():
    """Regression for the double-inversion emit bug: a region's own polarity
    AND ``corrections['inverted']`` both trace back to the SAME manual-tier
    source (a preview-persisted "inverted" region), since
    ``_derive_corrections`` reads the region to populate ``corrections``. If
    ``emit_active_speaker_baseline_config``'s split mixer ALSO applied the
    region's polarity (on top of the per-driver gain filter that reads
    ``corrections``), the two inversions would cancel to a net non-inversion —
    silently dropping the operator's intended polarity flip. The mixer must
    stay a no-op inverter on this emit path; the gain filter is the sole
    inverter.
    """
    raw = _two_way_preset()
    raw["crossover_regions"][0]["upper_polarity"] = "inverted"
    preset = ActiveSpeakerPreset.from_mapping(raw)

    yaml_text = emit_active_speaker_baseline_config(
        preset,
        playback_device="hw:ActiveDAC",
        corrections={
            "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
            "tweeter": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": True},
        },
    )
    parsed = yaml_lib.safe_load(yaml_text)

    # The per-driver baseline gain filter is the sole inverter.
    assert parsed["filters"]["as_tweeter_baseline_gain"]["parameters"]["inverted"] is True
    assert parsed["filters"]["as_woofer_baseline_gain"]["parameters"]["inverted"] is False
    # The split mixer's source for the tweeter output does NOT also invert.
    tweeter_index = next(
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == "tweeter"
    )
    mixer = parsed["mixers"][f"split_active_{preset.way_count}way"]
    dest = next(entry for entry in mixer["mapping"] if entry["dest"] == tweeter_index)
    assert all(source["inverted"] is False for source in dest["sources"])


# --- Spec-promise guard 1: trim-only apply preserves manual polarity/delay --


def test_manual_apply_preserves_persisted_polarity_and_delay_against_trim_evidence(
    tmp_path: Path,
) -> None:
    """A preview persists inverted-upper polarity + a 0.35 ms delay. Fresh
    MEASURED gain-trim evidence (an unrelated sub-parameter) applies, and a
    summed-validation record with CONFLICTING polarity/delay also exists — but
    manual tuning_owner (the default) never consults it for these two
    sub-parameters, so the persisted working values survive untouched."""
    topology = _dual_apple_topology()
    research = _research_with_sensitivity(
        tweeter_gain_db=-15.0, tweeter_gain_provenance="operator_pinned",
    )
    research["crossover_candidates"][0].update({
        "upper_polarity": "inverted",
        "delay_ms": 0.35,
        "delay_target_role": "tweeter",
    })
    draft = build_design_draft(
        topology,
        driver_research=research,
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=18.0
    )
    # A conflicting summed observation is present (mutating the SAME dict
    # measurements["latest_summed_by_group"] aliases), but manual tuning must
    # never consult it for delay/polarity.
    summed = measurements["summary"]["latest_summed_validations"]["mono"]
    summed["polarity"] = "invert_woofer"
    summed["delay_ms"] = 5.0
    summed["delay_target_role"] = "woofer"

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        created_at="2026-06-19T12:20:00Z",
    )

    assert payload["status"] == "ready_to_apply"
    assert payload["tuning_owner"] == "manual"
    # The trim (gain) evidence DID apply — proves this was a real trim-only
    # apply, not just an absence of measurement.
    assert payload["corrections_source"]["tweeter"] == "operator_pinned"
    # The persisted polarity/delay survive, unaffected by the conflicting
    # summed evidence.
    assert payload["corrections"]["tweeter"]["inverted"] is True
    assert payload["corrections"]["tweeter"]["delay_ms"] == 0.35
    assert payload["corrections"]["woofer"]["inverted"] is False
    assert payload["corrections_provenance"]["tweeter"]["inverted"] == PROVENANCE_MANUAL
    assert payload["corrections_provenance"]["tweeter"]["delay_ms"] == PROVENANCE_MANUAL


# --- Lane E admitted polarity; Lane F exclusively owns measured delay --------


def test_derive_corrections_stereo_alignment_does_not_mutate_shared_preset(
    monkeypatch,
):
    from jasper.active_speaker import crossover_contract

    monkeypatch.setattr(
        crossover_contract,
        "preset_matches_applied_profile",
        lambda *_args, **_kwargs: True,
    )
    region = CrossoverRegion(
        id="woofer_tweeter_2000hz",
        lower_driver="woofer",
        upper_driver="tweeter",
        fc_hz=2000.0,
        lower_polarity="inverted",
        upper_polarity="inverted",
        delay_target_driver="tweeter",
        delay_ms=0.35,
    )
    preset = _duck_preset(crossover_regions=[region])
    measurements = {
        "latest_summed_pairs_by_group": {
            "left": {"woofer:tweeter": {"in_phase": {}, "reverse": {}}},
            "right": {"woofer:tweeter": {"in_phase": {}, "reverse": {}}},
        },
    }
    corrections, issues, _meta = _derive_corrections(
        preset, {}, measurements, tuning_owner="automatic",
        expected_profile_context_id="protected-profile",
    )

    # Every role's persisted (manual) delay/inversion survives untouched.
    assert corrections["woofer"]["inverted"] is True
    assert corrections["tweeter"]["inverted"] is True
    assert corrections["tweeter"]["delay_ms"] == 0.35
    warning = next(
        issue for issue in issues
        if issue["code"] == "group_specific_alignment_not_applied"
    )
    assert "measurement-derived" in warning["message"]


def test_derive_corrections_manual_tuning_never_looks_at_summed_evidence_at_all():
    # Same fixture as above but tuning_owner="manual": the guard/warning never
    # fires because the measured branch is never entered.
    region = CrossoverRegion(
        id="woofer_tweeter_2000hz",
        lower_driver="woofer",
        upper_driver="tweeter",
        fc_hz=2000.0,
        lower_polarity="inverted",
        upper_polarity="inverted",
        delay_target_driver="tweeter",
        delay_ms=0.35,
    )
    preset = _duck_preset(crossover_regions=[region])
    measurements = {
        "latest_summed_pairs_by_group": {
            "left": {"woofer:tweeter": {"in_phase": {}, "reverse": {}}},
            "right": {"woofer:tweeter": {"in_phase": {}, "reverse": {}}},
        },
    }

    corrections, issues, _meta = _derive_corrections(
        preset, {}, measurements, tuning_owner="manual",
    )

    assert corrections["woofer"]["inverted"] is True
    assert corrections["tweeter"]["inverted"] is True
    assert corrections["tweeter"]["delay_ms"] == 0.35
    assert "group_specific_alignment_not_applied" not in {
        issue["code"] for issue in issues
    }


def test_derive_corrections_automatic_uses_admitted_pair_and_never_capture_delay(
    tmp_path: Path,
) -> None:
    import copy

    from tests.test_active_speaker_commissioning_capture import (
        _alignment_applied_profile,
        _valid_alignment_pair,
    )

    preset, measurements = _valid_alignment_pair(tmp_path)
    measurements = copy.deepcopy(measurements)
    pair = measurements["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    pair["in_phase"].update({
        "outcome": "polarity_or_delay_problem",
        "validated": False,
        "delay_ms": 9.9,
        "delay_target_role": "tweeter",
    })
    pair["in_phase"]["acoustic"].update({
        "verdict": "polarity_or_delay_problem",
        "null_depth_db": 24.0,
    })
    pair["reverse"].update({
        "outcome": "polarity_or_delay_problem",
        "validated": False,
        "delay_ms": 8.8,
        "delay_target_role": "woofer",
    })
    pair["reverse"]["acoustic"].update({
        "verdict": "polarity_or_delay_problem",
        "null_depth_db": 2.0,
    })
    for record in pair.values():
        record["acoustic"].update({
            "null_depth_capped": False,
            "snr": {
                "verdict": "ok",
                "worst_relevant": {"verdict": "ok"},
            },
        })
    applied_profile = _alignment_applied_profile(
        preset,
        topology_id=measurements["active_comparison_set"]["topology_id"],
    )

    corrections, _issues, meta = _derive_corrections(
        preset,
        {},
        measurements,
        tuning_owner="automatic",
        expected_profile_context_id="protected-profile",
        applied_profile_context=applied_profile,
    )

    assert corrections["tweeter"]["inverted"] is True
    assert corrections["tweeter"]["delay_ms"] == 0.0
    assert meta["corrections_provenance"]["tweeter"] == {
        "inverted": PROVENANCE_MEASURED,
    }

    changed_graph = copy.deepcopy(applied_profile)
    changed_graph["recomposition_snapshot"]["corrections"]["tweeter"][
        "gain_db"
    ] = -1.0
    stale_corrections, stale_issues, stale_meta = _derive_corrections(
        preset,
        {},
        measurements,
        tuning_owner="automatic",
        expected_profile_context_id="protected-profile",
        applied_profile_context=changed_graph,
    )
    assert stale_corrections["tweeter"]["inverted"] is False
    assert "tweeter" not in stale_meta["corrections_provenance"]
    assert "summed_alignment_graph_context_changed" in {
        issue["code"] for issue in stale_issues
    }


def test_derive_corrections_never_applies_polarity_without_band_snr(
    tmp_path: Path,
) -> None:
    from tests.test_active_speaker_commissioning_capture import (
        _alignment_applied_profile,
        _valid_alignment_pair,
    )

    preset, measurements = _valid_alignment_pair(tmp_path)
    applied_profile = _alignment_applied_profile(
        preset,
        topology_id=measurements["active_comparison_set"]["topology_id"],
    )
    pair = measurements["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    pair["in_phase"]["outcome"] = "polarity_or_delay_problem"
    pair["in_phase"]["validated"] = False
    pair["in_phase"]["acoustic"].update({
        "verdict": "polarity_or_delay_problem",
        "null_depth_db": 24.0,
    })
    pair["reverse"]["outcome"] = "polarity_or_delay_problem"
    pair["reverse"]["validated"] = False
    pair["reverse"]["acoustic"].update({
        "verdict": "polarity_or_delay_problem",
        "null_depth_db": 2.0,
    })

    corrections, issues, meta = _derive_corrections(
        preset,
        {},
        measurements,
        tuning_owner="automatic",
        expected_profile_context_id="protected-profile",
        applied_profile_context=applied_profile,
    )

    assert corrections["tweeter"]["inverted"] is False
    assert "tweeter" not in meta["corrections_provenance"]
    assert "summed_alignment_quality_not_applied" in {
        issue["code"] for issue in issues
    }


def test_derive_corrections_surfaces_rejected_alignment_evidence(
    tmp_path: Path,
) -> None:
    from tests.test_active_speaker_commissioning_capture import (
        _alignment_applied_profile,
        _valid_alignment_pair,
    )

    preset, measurements = _valid_alignment_pair(tmp_path)
    applied_profile = _alignment_applied_profile(
        preset,
        topology_id=measurements["active_comparison_set"]["topology_id"],
    )
    pair = measurements["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    for record in pair.values():
        record["excitation"] = None

    corrections, issues, meta = _derive_corrections(
        preset,
        {},
        measurements,
        tuning_owner="automatic",
        expected_profile_context_id="protected-profile",
        applied_profile_context=applied_profile,
    )

    assert corrections["tweeter"]["inverted"] is False
    assert "tweeter" not in meta["corrections_provenance"]
    assert "summed_alignment_evidence_not_applied" in {
        issue["code"] for issue in issues
    }


def test_derive_corrections_rejects_flat_record_and_stale_profile_context(
    tmp_path: Path,
) -> None:
    from tests.test_active_speaker_commissioning_capture import _valid_alignment_pair

    preset, measurements = _valid_alignment_pair(tmp_path)
    malicious = {
        "latest_summed_by_group": {
            "mono": {
                "validated": True,
                "polarity": "invert_tweeter",
                "delay_ms": 12.0,
                "delay_target_role": "tweeter",
            },
        },
    }
    flat_corrections, _issues, flat_meta = _derive_corrections(
        preset,
        {},
        malicious,
        tuning_owner="automatic",
        expected_profile_context_id="protected-profile",
    )
    stale_corrections, _issues, stale_meta = _derive_corrections(
        preset,
        {},
        measurements,
        tuning_owner="automatic",
        expected_profile_context_id="different-current-profile",
    )

    for corrections, meta in (
        (flat_corrections, flat_meta),
        (stale_corrections, stale_meta),
    ):
        assert corrections["tweeter"]["inverted"] is False
        assert corrections["tweeter"]["delay_ms"] == 0.0
        assert "tweeter" not in meta["corrections_provenance"]


# --- corrections_provenance block on the candidate/applied payload ---------


def test_corrections_provenance_present_on_candidate_and_applied_payload(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    research = _research_with_sensitivity()  # 25.2 dB gap, no explicit gain
    research["crossover_candidates"][0].update({
        "upper_polarity": "inverted",
        "delay_ms": 0.4,
        "delay_target_role": "tweeter",
    })

    payload = _baseline_payload(topology, research, tmp_path)

    assert payload["status"] == "ready_to_apply"
    provenance = payload["corrections_provenance"]
    assert provenance["tweeter"]["gain_db"] == PROVENANCE_RECOMMENDED_START
    assert provenance["tweeter"]["inverted"] == PROVENANCE_MANUAL
    assert provenance["tweeter"]["delay_ms"] == PROVENANCE_MANUAL
    assert "woofer" not in provenance
    # Only the canonical vocabulary is ever used.
    allowed = {
        PROVENANCE_MANUAL,
        PROVENANCE_MEASURED,
        PROVENANCE_RECOMMENDED_START,
        PROVENANCE_PRESERVED,
    }
    for role_entry in provenance.values():
        assert set(role_entry.values()) <= allowed
    # Legacy corrections_source / gain_provenance stay byte-compatible for
    # this legacy-shaped fixture (no gain_offset_db_provenance anywhere).
    assert payload["corrections_source"]["tweeter"] == "sensitivity"
    assert payload["gain_provenance"] == {}
    # The recomposition_snapshot (the frozen "applied" projection once this
    # candidate is later applied) carries the same block.
    assert payload["recomposition_snapshot"]["corrections_provenance"] == provenance


# --- lifecycle events (lane E, docs/active-crossover-information-design.md
# "Structured events") -------------------------------------------------------

_BASELINE_LOGGER = "jasper.active_speaker.baseline_profile"


def _events(caplog, name: str) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith(f"event={name}")
    ]


async def test_apply_baseline_profile_emits_started_before_dsp_apply(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    current_path = str(prior)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    started_before_load: list[bool] = []

    async def load_config(path: str) -> bool:
        nonlocal current_path
        # apply_started must already be in the log by the time load_config
        # (called from inside apply_dsp_config) runs.
        started_before_load.append(
            any(
                r.getMessage().startswith("event=correction.crossover_apply_started")
                for r in caplog.records
            )
        )
        current_path = path
        return True

    async def current_config_path() -> str:
        return current_path

    with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
        payload = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            get_current_config_path=current_config_path,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
        )

    assert payload["status"] == "applied"
    assert started_before_load == [True]
    started = _events(caplog, "correction.crossover_apply_started")
    assert len(started) == 1
    assert "baseline_id=baseline-bench_mono" in started[0]
    assert "tuning_owner=manual" in started[0]
    assert "topology_id=bench_mono" in started[0]
    # #1666: the started event names the candidate's own content-addressed
    # sibling, never the literal config_path passed in.
    assert f"config_path={payload['profile']['config']['path']}" in started[0]
    assert f"config_path={tmp_path}/active_speaker_baseline.yml" not in started[0]


async def test_apply_baseline_profile_success_emits_succeeded_with_fingerprints(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    current_path = str(prior)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str:
        return current_path

    with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
        payload = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            get_current_config_path=current_config_path,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
        )

    assert payload["status"] == "applied"
    fingerprint = payload["profile"]["candidate_fingerprint"]
    succeeded = _events(caplog, "correction.crossover_apply_succeeded")
    assert len(succeeded) == 1
    message = succeeded[0]
    assert f"candidate_fingerprint={fingerprint}" in message
    assert f"applied_fingerprint={fingerprint}" in message
    assert f"applied_at={payload['profile']['applied_at']}" in message
    # Exactly one succeeded event, no rolled_back event alongside it.
    assert _events(caplog, "correction.crossover_apply_rolled_back") == []


async def test_apply_baseline_profile_dsp_error_emits_exactly_one_rolled_back_event(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    # Spec-promise guard: a failed apply emits exactly one typed rolled_back
    # event, never a silent failure (docs/active-crossover-information-design.md
    # "Structured events" pins apply_rolled_back as THE failure event name).
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    async def load_config(path: str) -> bool:
        # CamillaDSP rejects the candidate -> apply_dsp_config raises
        # DspApplyError with rollback_attempted False (no prior config path
        # was ever established in this fixture).
        return False

    with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
        payload = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
        )

    assert payload["status"] == "apply_failed"
    rolled_back = _events(caplog, "correction.crossover_apply_rolled_back")
    assert len(rolled_back) == 1
    message = rolled_back[0]
    assert "reason=" in message
    # Faithful to exc.state, not hardcoded: no prior config path was ever
    # established in this fixture (no get_current_config_path passed), so
    # _rollback's early-out leaves rollback_attempted False and the other two
    # rollback_* fields at their DspApplyState defaults (None -> "null").
    assert "rollback_attempted=false" in message
    assert "rollback_succeeded=null" in message
    assert "rollback_error=null" in message
    assert _events(caplog, "correction.crossover_apply_succeeded") == []
    # There is no separate "apply_failed" event name -- rolled_back is it.
    assert not any(
        "correction.crossover_apply_failed" in r.getMessage() for r in caplog.records
    )


async def test_apply_baseline_profile_dsp_error_reports_real_rollback_attempt(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    # Complements the previous test: with a prior config path known,
    # apply_dsp_config actually attempts (and here succeeds at) a rollback on
    # load failure. Proves rollback_attempted/rollback_succeeded reflect a
    # real attempt from exc.state rather than always reading False/None.
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    calls: list[str] = []

    async def load_config(path: str) -> bool:
        calls.append(path)
        # The candidate load is rejected; the rollback load (to the prior
        # config) succeeds.
        return path == str(prior)

    async def get_current_config_path() -> str:
        return str(prior)

    with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
        payload = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            get_current_config_path=get_current_config_path,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
        )

    assert payload["status"] == "apply_failed"
    # #1666: the transaction loads the candidate's own content-addressed
    # sibling, never the literal config_path passed in; a failed apply never
    # touches config_path either (nothing promotes on failure).
    assert calls == [payload["profile"]["config"]["path"], str(prior)]
    assert not (tmp_path / "active_speaker_baseline.yml").exists()
    rolled_back = _events(caplog, "correction.crossover_apply_rolled_back")
    assert len(rolled_back) == 1
    message = rolled_back[0]
    assert "rollback_attempted=true" in message
    assert "rollback_succeeded=true" in message
    assert "rollback_error=null" in message


async def test_apply_baseline_profile_blocked_emits_no_apply_events(
    tmp_path: Path, caplog,
) -> None:
    # may_apply is False before the DSP transaction even starts (insufficient
    # evidence) -- no apply_started/succeeded/rolled_back should fire.
    topology = _topology()

    with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
        payload = await apply_baseline_profile(
            topology,
            design_draft={},
            crossover_preview={},
            measurements={},
            load_config=lambda *a, **k: pytest.fail("load_config must not run"),
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
        )

    assert payload["status"] == "blocked"
    assert not any(
        r.getMessage().startswith("event=correction.crossover_apply_")
        for r in caplog.records
    )


# --- Wave 4 (crossover measurement v2 §5.8): MeasuredCrossoverCandidate -----
#
# The new v2 measured-crossover candidate (trims + optional delay/polarity,
# jasper.active_speaker.measured_crossover_candidate) is a drop-in peer of
# the legacy MeasuredElectricalCandidate for build_baseline_profile_candidate
# / apply_baseline_profile's existing measured_candidate seam — same
# apply-with-rollback transaction, same freshness gate, no new apply path.


def _v2_candidate(
    preset: ActiveSpeakerPreset,
    *,
    delay_us: float = 250.0,
    delay_role: str = "tweeter",
    polarity: str = "invert",
    tweeter_gain_db: float = -2.0,
) -> MeasuredCrossoverCandidate:
    return MeasuredCrossoverCandidate(
        program_id="prog-v2-1",
        analysis={"drift_ppm": 3.0, "sweeps": ["w", "t", "w"]},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": tweeter_gain_db},
        alignment=MeasuredCrossoverAlignment(
            delay_us=delay_us, delay_role=delay_role, polarity=polarity
        ),
    )


def test_build_baseline_profile_candidate_accepts_v2_measured_candidate(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=candidate,
        created_at="2026-07-18T12:20:00Z",
    )

    assert payload["status"] == "ready_to_apply", payload.get("issues")
    assert payload["corrections"] == {
        "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
        "tweeter": {"gain_db": -2.0, "delay_ms": 0.25, "inverted": True},
    }
    # #1666: candidate lands on a content-addressed sibling, not the literal
    # config_path passed in.
    config_text = Path(payload["config"]["path"]).read_text()
    assert "delay: 0.2500" in config_text
    assert payload["candidate_fingerprint"] is not None


def test_build_baseline_profile_candidate_v2_candidate_requires_automatic(
    tmp_path: Path,
) -> None:
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)

    with pytest.raises(ValueError, match="automatic tuning ownership"):
        build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            write=False,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
            tuning_owner="manual",
            measured_candidate=candidate,
        )


def test_v2_candidate_trims_only_matches_legacy_trims_only_shape(
    tmp_path: Path,
) -> None:
    """Absent alignment is exactly today's trims-only apply behavior: the
    compiled corrections carry zero delay and the preset's own (unchanged)
    polarity, identical in shape to a plain gain-only candidate."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    trims_only = MeasuredCrossoverCandidate(
        program_id="prog-v2-2",
        analysis={"drift_ppm": 1.0},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
    )

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=trims_only,
        created_at="2026-07-18T12:20:00Z",
    )

    assert payload["status"] == "ready_to_apply", payload.get("issues")
    assert payload["corrections"] == {
        "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
        "tweeter": {"gain_db": -2.0, "delay_ms": 0.0, "inverted": False},
    }


def test_build_baseline_profile_candidate_blocks_on_failed_alignment_proof(
    monkeypatch, tmp_path: Path, caplog,
) -> None:
    """A failed delay_graph/graph_safety proof is a blocker issue, exactly like
    a failed CamillaDSP validation — fail closed, no partial write reaches
    "ready_to_apply" — and the refusal leaves a stable journal event for
    triage."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)

    def _boom(*_args, **_kwargs):
        raise MeasuredCrossoverCandidateError(
            "delay_graph_proof_failed", "simulated proof failure"
        )

    monkeypatch.setattr(
        "jasper.active_speaker.measured_crossover_candidate.prove_candidate_config",
        _boom,
    )

    with caplog.at_level(logging.ERROR, logger=_BASELINE_LOGGER):
        payload = build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            write=True,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
            tuning_owner="automatic",
            measured_candidate=candidate,
        )

    assert payload["status"] == "blocked"
    assert payload["permissions"]["may_apply"] is False
    issue_codes = {issue["code"] for issue in payload["issues"]}
    assert "measured_candidate_alignment_proof_failed" in issue_codes
    blocked_events = _events(caplog, "correction.crossover_alignment_proof_blocked")
    assert len(blocked_events) == 1
    assert "code=delay_graph_proof_failed" in blocked_events[0]
    assert f"candidate_fingerprint={candidate.fingerprint}" in blocked_events[0]


async def test_apply_baseline_profile_applies_v2_measured_candidate(
    monkeypatch, tmp_path: Path,
) -> None:
    """End-to-end: publish a v2 candidate with delay+polarity, apply it through
    the existing atomic DSP transaction, and confirm the emitted config
    carries both — through the SAME rollback-capable apply_baseline_profile
    used for every other candidate shape."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    prior = tmp_path / "prior.yml"
    prior.write_text("devices:\n  volume_limit: 0\n", encoding="utf-8")
    current_path = str(prior)
    calls: list[str] = []

    async def load_config(path: str) -> bool:
        nonlocal current_path
        calls.append(path)
        current_path = path
        return True

    async def current_config_path() -> str:
        return current_path

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )

    assert payload["status"] == "applied"
    assert payload["profile"]["corrections"]["tweeter"] == {
        "gain_db": -2.0,
        "delay_ms": 0.25,
        "inverted": True,
    }
    # #1666: the applied candidate lands on its own content-addressed sibling
    # (what load_config was actually called with); the canonical file at
    # tmp_path/active_speaker_baseline.yml is a POST-success promoted copy, so
    # it independently carries the same content.
    config_text = (tmp_path / "active_speaker_baseline.yml").read_text()
    assert "as_tweeter_delay" in config_text
    assert "delay: 0.2500" in config_text
    assert calls == [payload["profile"]["config"]["path"]]
    assert calls != [str(tmp_path / "active_speaker_baseline.yml")]


async def test_apply_v2_measured_candidate_reproves_sealed_bass_and_stales_it(
    monkeypatch, tmp_path: Path,
) -> None:
    from jasper.active_speaker.measured_crossover_candidate import (
        prove_candidate_config,
    )
    from jasper.active_speaker.runtime_contract import (
        classify_bass_extension_graph,
    )
    from jasper.bass_extension.profile import (
        evaluate_bass_extension_profile,
        save_bass_extension_profile,
    )
    from tests.test_active_speaker_runtime_contract import _sealed_profile

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    measured = _v2_candidate(preset)
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    bass_path = tmp_path / "bass_extension_profile.json"
    monkeypatch.setenv("JASPER_BASS_EXTENSION_PROFILE_STATE", str(bass_path))
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    loaded_graphs: list[str] = []

    async def load_config(path: str) -> bool:
        loaded_graphs.append(Path(path).read_text(encoding="utf-8"))
        return True

    first = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=measured,
    )
    assert first["status"] == "applied"
    profile = _sealed_profile(topology, first["profile"])
    save_bass_extension_profile(profile, bass_path)

    with (
        mock.patch(
            "jasper.active_speaker.measured_crossover_candidate."
            "prove_candidate_config",
            wraps=prove_candidate_config,
        ) as prove_measured,
        mock.patch(
            "jasper.active_speaker.runtime_contract."
            "classify_bass_extension_graph",
            wraps=classify_bass_extension_graph,
        ) as prove_graph,
    ):
        repeated = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            load_config=load_config,
            state_path=state_path,
            config_path=config_path,
            validate=_valid_config,
            tuning_owner="automatic",
            measured_candidate=measured,
        )

    assert repeated["status"] == "applied"
    assert prove_measured.call_count == 1
    assert prove_graph.call_count == 1
    repeated_text = loaded_graphs[-1]
    repeated_yaml = yaml_lib.safe_load(repeated_text)
    assert prove_measured.call_args.args == (measured, repeated_text)
    assert prove_graph.call_args.kwargs["graph_text"] == repeated_text
    assert prove_graph.call_args.kwargs["desired_profile"] == profile
    assert "bass_ext_lt" in repeated_yaml["filters"]
    assert "bass_ext_subsonic" in repeated_yaml["filters"]
    assert "delay: 0.2500" in repeated_text
    assert repeated_yaml["filters"]["as_tweeter_baseline_gain"]["parameters"][
        "inverted"
    ] is True
    assert repeated["profile"]["corrections"]["tweeter"] == {
        "gain_db": -2.0,
        "delay_ms": 0.25,
        "inverted": True,
    }
    assert evaluate_bass_extension_profile(
        path=bass_path,
        topology=topology,
        applied_baseline_state=repeated["profile"],
    ).status == "accepted"

    changed_measured = MeasuredCrossoverCandidate(
        program_id="prog-v2-2",
        analysis={"drift_ppm": 4.0, "sweeps": ["w", "t", "w", "t"]},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": -3.0},
        alignment=MeasuredCrossoverAlignment(
            delay_us=375.0,
            delay_role="tweeter",
            polarity="keep",
        ),
    )
    changed = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=changed_measured,
    )

    assert changed["status"] == "applied"
    changed_text = loaded_graphs[-1]
    changed_yaml = yaml_lib.safe_load(changed_text)
    assert "bass_ext_lt" not in changed_text
    assert "bass_ext_subsonic" not in changed_text
    assert "delay: 0.3750" in changed_text
    assert changed_yaml["filters"]["as_tweeter_baseline_gain"]["parameters"][
        "inverted"
    ] is False
    assert changed["profile"]["corrections"]["tweeter"] == {
        "gain_db": -3.0,
        "delay_ms": 0.375,
        "inverted": False,
    }
    assert evaluate_bass_extension_profile(
        path=bass_path,
        topology=topology,
        applied_baseline_state=changed["profile"],
    ).status == "stale"


# --- Layer-1a driver linearization threading (#1668 PR-D) -------------------
#
# The three gaps: build_baseline_profile_candidate threads candidate.
# linearization into the emit call + the recomposition_snapshot + the
# top-level payload mirror; _frozen_applied_profile carries "linearization"
# into the SSOT frozen dict; recompose_applied_baseline_yaml reads the
# snapshot's "linearization" era-tolerantly and re-emits it. The HIGHEST
# PRIORITY regression here is the snapshot round trip: build -> snapshot has
# the key -> recompose re-emits IDENTICAL filters (this is the fix for the
# CRITICAL silent-reversion gap -- before it, every /sound preference-EQ
# recompose silently dropped an applied profile's linearization stage).


def _linearization_payload() -> dict:
    return {
        "woofer": {
            "role": "woofer",
            "filters": [
                {"biquad_type": "Peaking", "freq": 900.0, "q": 3.0, "gain": -1.2},
            ],
            "fit_band_hz": [150.0, 3951.5], "target_level_db": -20.22,
            "residual_rms_db": 0.4, "residual_max_db": 1.1,
            "reason_summary": {"250": "envelope_fitted"},
            "mic_tier": "reference", "driver_class": "unknown", "n_repeats": 3,
        },
        "tweeter": {
            "role": "tweeter",
            "filters": [
                {
                    "biquad_type": "Highshelf", "freq": 8000.0,
                    "q": 0.7071067811865476, "gain": -3.0,
                },
                {"biquad_type": "Peaking", "freq": 4063.6, "q": 1.89, "gain": -3.38},
            ],
            "fit_band_hz": [2020.0, 13905.2], "target_level_db": -8.63,
            "residual_rms_db": 2.63, "residual_max_db": 7.13,
            "reason_summary": {"2000": "envelope_fitted"},
            "mic_tier": "reference", "driver_class": "unknown", "n_repeats": 3,
        },
    }


def _linearization_filter_lines(text: str) -> dict:
    payload = yaml_lib.safe_load(text)
    return {
        name: spec["parameters"]
        for name, spec in payload["filters"].items()
        if "linearization" in name
    }


async def test_apply_then_recompose_reemits_identical_linearization_filters(
    monkeypatch, tmp_path: Path,
) -> None:
    """HIGHEST PRIORITY (#1668 PR-D gap 3): the applied profile's
    linearization stage must survive recompose_applied_baseline_yaml (the
    /sound preference-EQ seam and every other production recompose caller)
    byte-for-byte identical filters, not just at the moment of apply."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    candidate = MeasuredCrossoverCandidate(
        program_id="prog-lin-1",
        analysis={"drift_ppm": 2.0, "sweeps": ["w", "t", "w"]},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
        linearization=_linearization_payload(),
    )

    async def load_config(path: str) -> bool:
        return True

    applied = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )
    assert applied["status"] == "applied"
    profile = applied["profile"]
    # Gap 3b: both the top-level mirror and the immutable snapshot carry it.
    assert profile["linearization"]
    assert profile["recomposition_snapshot"]["linearization"]

    applied_text = Path(profile["config"]["path"]).read_text(encoding="utf-8")
    assert "as_tweeter_linearization_shelf" in applied_text
    assert "as_woofer_linearization_peak_1" in applied_text

    recomposed_text, recompose_issues = recompose_applied_baseline_yaml(
        topology, applied_profile=profile,
    )
    assert recompose_issues == []
    assert recomposed_text is not None
    assert _linearization_filter_lines(recomposed_text) == _linearization_filter_lines(
        applied_text
    )


async def test_apply_baseline_profile_emits_baseline_linearization_event_with_counts(
    monkeypatch, tmp_path: Path, caplog,
) -> None:
    """SF3 (#1668 PR-D review): apply-time observability for what
    linearization actually reached hardware -- one dsp.baseline_linearization
    event per successful apply, carrying a per-role filter count."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    candidate = MeasuredCrossoverCandidate(
        program_id="prog-lin-2",
        analysis={"drift_ppm": 2.0, "sweeps": ["w", "t", "w"]},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
        linearization=_linearization_payload(),
    )

    async def load_config(path: str) -> bool:
        return True

    with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
        applied = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            load_config=load_config,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
            tuning_owner="automatic",
            measured_candidate=candidate,
        )
    assert applied["status"] == "applied"

    # _linearization_payload(): woofer carries 1 filter, tweeter carries 2
    # (a shelf plus a peak).
    events = _events(caplog, "dsp.baseline_linearization")
    assert len(events) == 1
    assert "woofer=1" in events[0]
    assert "tweeter=2" in events[0]
    assert "none=" not in events[0]


async def test_apply_baseline_profile_emits_baseline_linearization_event_none_when_absent(
    monkeypatch, tmp_path: Path, caplog,
) -> None:
    """The counterpart: a plain (non-linearized) apply still logs the event,
    with none=true -- absence is an observable fact, not silence."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    candidate = _v2_candidate(preset, tweeter_gain_db=-2.0, delay_us=250.0)

    async def load_config(path: str) -> bool:
        return True

    with caplog.at_level(logging.INFO, logger=_BASELINE_LOGGER):
        applied = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            load_config=load_config,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
            tuning_owner="automatic",
            measured_candidate=candidate,
        )
    assert applied["status"] == "applied"

    events = _events(caplog, "dsp.baseline_linearization")
    assert len(events) == 1
    assert "none=true" in events[0]


async def test_recompose_of_legacy_snapshot_missing_linearization_emits_no_stage(
    monkeypatch, tmp_path: Path,
) -> None:
    """A pre-PR-D applied profile (recomposition_snapshot with NO
    "linearization" key at all -- not merely an empty one) must recompose
    cleanly with no linearization stage: era-tolerant, never a KeyError or a
    phantom stage."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    candidate = _v2_candidate(preset, tweeter_gain_db=-2.0, delay_us=250.0)

    async def load_config(path: str) -> bool:
        return True

    applied = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )
    assert applied["status"] == "applied"
    profile = deepcopy(applied["profile"])
    profile.pop("linearization", None)
    profile["recomposition_snapshot"].pop("linearization", None)

    recomposed_text, recompose_issues = recompose_applied_baseline_yaml(
        topology, applied_profile=profile,
    )
    assert recompose_issues == []
    assert recomposed_text is not None
    assert "linearization" not in recomposed_text


def test_frozen_applied_profile_carries_linearization_top_level():
    """Gap 3c: _frozen_applied_profile (a field-by-field allowlist, unlike
    persist_applied_baseline_profile's whole-object spread) must copy
    "linearization" -- otherwise a candidate saved OVER an applied,
    linearized profile silently loses it from the retained
    applied_recomposition_profile sidecar."""
    saved = {
        "status": "applied",
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "baseline_id": "baseline-x",
        "applied_at": "2026-07-23T00:00:00Z",
        "source": {},
        "config": {},
        "corrections": {"woofer": {"gain_db": 0.0}},
        "corrections_source": {},
        "gain_provenance": {},
        "corrections_provenance": {},
        "level_match": {},
        "tuning_owner": "automatic",
        "provisional": False,
        "linearization": {"woofer": [{"biquad_type": "Peaking"}]},
        "recomposition_snapshot": {
            "schema_version": 1,
            "linearization": {"woofer": [{"biquad_type": "Peaking"}]},
        },
    }
    from jasper.active_speaker.baseline_profile import _frozen_applied_profile

    frozen = _frozen_applied_profile(saved)
    assert frozen is not None
    assert frozen["linearization"] == {"woofer": [{"biquad_type": "Peaking"}]}
    assert frozen["recomposition_snapshot"]["linearization"] == {
        "woofer": [{"biquad_type": "Peaking"}]
    }


def test_frozen_applied_profile_defaults_linearization_when_absent():
    """Era-tolerant: a pre-PR-D applied dict with no "linearization" key at
    all must not raise, defaulting to {}."""
    from jasper.active_speaker.baseline_profile import _frozen_applied_profile

    saved = {
        "status": "applied",
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "baseline_id": "baseline-x",
        "applied_at": "2026-07-23T00:00:00Z",
        "source": {},
        "config": {},
        "corrections": {},
        "corrections_source": {},
        "gain_provenance": {},
        "corrections_provenance": {},
        "level_match": {},
        "tuning_owner": "automatic",
        "provisional": False,
        "recomposition_snapshot": {"schema_version": 1},
    }
    frozen = _frozen_applied_profile(saved)
    assert frozen is not None
    assert frozen["linearization"] == {}


async def _linearization_restore_fixture(monkeypatch, tmp_path: Path):
    """Shared setup for both restore-direction tests below: a preset plus
    load_config/current_config_path recorders and the two candidate shapes
    (linearized vs plain)."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    current_path: str | None = None

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str | None:
        return current_path

    linearized_candidate = MeasuredCrossoverCandidate(
        program_id="prog-lin-a",
        analysis={"drift_ppm": 2.0, "sweeps": ["w", "t", "w"]},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
        linearization=_linearization_payload(),
    )
    plain_candidate = _v2_candidate(preset, tweeter_gain_db=-2.5, delay_us=300.0)
    return (
        topology, draft, preview, state_path, config_path,
        load_config, current_config_path, linearized_candidate, plain_candidate,
    )


async def test_restore_to_linearized_profile_brings_linearization_back(
    monkeypatch, tmp_path: Path,
) -> None:
    """Gap 3e (TRACE promote/restore genericity), direction A: apply a
    linearized profile, capture it as "retained" (a write=False preview
    always reflects whatever is CURRENTLY applied), apply a plain profile
    over it, then restore back to the retained linearized snapshot ->
    linearization is back, in both the JSON SSOT and the reloaded config
    file. Pins that persist_applied_baseline_profile / restore_applied_
    baseline_profile are whole-object copies, not field-by-field
    allowlists, end to end (not just by code reading)."""
    (
        topology, draft, preview, state_path, config_path,
        load_config, current_config_path, linearized_candidate, plain_candidate,
    ) = await _linearization_restore_fixture(monkeypatch, tmp_path)

    linearized_applied = await apply_baseline_profile(
        topology, design_draft=draft, crossover_preview=preview, measurements={},
        load_config=load_config, get_current_config_path=current_config_path,
        state_path=state_path, config_path=config_path, validate=_valid_config,
        tuning_owner="automatic", measured_candidate=linearized_candidate,
    )
    assert linearized_applied["status"] == "applied"
    # Captured BEFORE the plain apply below supersedes it -- linearized IS
    # the currently-applied profile at this point, so this preview build's
    # own applied_recomposition_profile sidecar reflects it.
    retained_linearized = build_baseline_profile_candidate(
        topology, design_draft=draft, crossover_preview=preview, measurements={},
        write=False, state_path=state_path, config_path=config_path,
        tuning_owner="automatic", measured_candidate=plain_candidate,
    )["applied_recomposition_profile"]
    assert retained_linearized["linearization"]

    plain_applied = await apply_baseline_profile(
        topology, design_draft=draft, crossover_preview=preview, measurements={},
        load_config=load_config, get_current_config_path=current_config_path,
        state_path=state_path, config_path=config_path, validate=_valid_config,
        tuning_owner="automatic", measured_candidate=plain_candidate,
    )
    assert plain_applied["status"] == "applied"
    assert plain_applied["profile"]["linearization"] == {}

    restored = await restore_applied_baseline_profile(
        retained_linearized, load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path, config_path=config_path, validate=_valid_config,
    )
    assert restored["status"] == "restored", restored.get("issues")
    restored_active = load_applied_baseline_profile_state(state_path)
    assert restored_active is not None
    assert restored_active["linearization"]
    assert restored_active["recomposition_snapshot"]["linearization"]
    restored_text = config_path.read_text(encoding="utf-8")
    assert "as_tweeter_linearization_shelf" in restored_text


async def test_restore_to_pre_linearization_profile_leaves_linearization_gone(
    monkeypatch, tmp_path: Path,
) -> None:
    """Gap 3e, direction B: apply a plain profile, capture it as "retained",
    apply a linearized profile over it, then restore back to the retained
    plain snapshot -> linearization is gone, in both the JSON SSOT and the
    reloaded config file (never a phantom carry-forward from the
    superseded linearized apply)."""
    (
        topology, draft, preview, state_path, config_path,
        load_config, current_config_path, linearized_candidate, plain_candidate,
    ) = await _linearization_restore_fixture(monkeypatch, tmp_path)

    plain_applied = await apply_baseline_profile(
        topology, design_draft=draft, crossover_preview=preview, measurements={},
        load_config=load_config, get_current_config_path=current_config_path,
        state_path=state_path, config_path=config_path, validate=_valid_config,
        tuning_owner="automatic", measured_candidate=plain_candidate,
    )
    assert plain_applied["status"] == "applied"
    # Captured BEFORE the linearized apply below supersedes it.
    retained_plain = build_baseline_profile_candidate(
        topology, design_draft=draft, crossover_preview=preview, measurements={},
        write=False, state_path=state_path, config_path=config_path,
        tuning_owner="automatic", measured_candidate=linearized_candidate,
    )["applied_recomposition_profile"]
    assert retained_plain["linearization"] == {}

    linearized_applied = await apply_baseline_profile(
        topology, design_draft=draft, crossover_preview=preview, measurements={},
        load_config=load_config, get_current_config_path=current_config_path,
        state_path=state_path, config_path=config_path, validate=_valid_config,
        tuning_owner="automatic", measured_candidate=linearized_candidate,
    )
    assert linearized_applied["status"] == "applied"
    assert linearized_applied["profile"]["linearization"]

    restored = await restore_applied_baseline_profile(
        retained_plain, load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path, config_path=config_path, validate=_valid_config,
    )
    assert restored["status"] == "restored", restored.get("issues")
    restored_active = load_applied_baseline_profile_state(state_path)
    assert restored_active is not None
    assert restored_active["linearization"] == {}
    restored_text = config_path.read_text(encoding="utf-8")
    assert "linearization" not in restored_text


async def test_apply_baseline_profile_refuses_stale_v2_candidate_fingerprint(
    monkeypatch, tmp_path: Path,
) -> None:
    """The candidate fingerprint covers the new alignment fields: reviewing
    one delay_us and applying a candidate with a DIFFERENT delay_us is caught
    by the existing expected_candidate_fingerprint staleness gate (#1423/#1441
    apply-freshness hardening), unchanged."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues

    reviewed = _v2_candidate(preset, delay_us=250.0)
    reviewed_fingerprint = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=False,
        tuning_owner="automatic",
        measured_candidate=reviewed,
    )["candidate_fingerprint"]

    changed = _v2_candidate(preset, delay_us=999.0)

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )

    async def load_config(_path: str) -> bool:
        pytest.fail("load_config must not run against a stale reviewed candidate")

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=changed,
        expected_candidate_fingerprint=reviewed_fingerprint,
    )

    assert payload["status"] == "blocked"
    issue_codes = {issue["code"] for issue in payload["issues"]}
    assert "baseline_candidate_fingerprint_mismatch" in issue_codes


# --- v2 Undo — restore the pre-candidate applied profile (W6 run-8 Blocker Q) --
#
# The verify_fail screen's Undo posted to the LEGACY /crossover/restore, which
# expects a pending commissioning-run candidate apply — a v2 apply never
# creates one (it commits straight through apply_baseline_profile's own
# atomic transaction), so the legacy path 500s ("there is no pending
# candidate apply to restore") and the household is stuck on the bad-sounding
# candidate. restore_applied_baseline_profile is the v2-aware fix: reload the
# frozen pre-candidate applied_recomposition_profile through the SAME
# apply_dsp_config transaction the forward apply rides, never recomposed.


async def _apply_prior_then_run8(monkeypatch, tmp_path: Path):
    """Apply one profile (the household's pre-existing crossover), then a
    SECOND (the run-8-shaped measured candidate) over it. Returns
    ``(state_path, config_path, load_config, current_config_path,
    prior_payload, run8_payload, retained)`` — ``retained`` is the exact
    frozen snapshot ``handle_v2_apply`` would have stashed as
    ``pre_apply_profile`` at the moment of the run-8 apply."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    current_path: str | None = None

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str | None:
        return current_path

    prior_candidate = _v2_candidate(preset, tweeter_gain_db=-2.0, delay_us=250.0)
    prior_payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=prior_candidate,
    )
    assert prior_payload["status"] == "applied"

    run8_candidate = _v2_candidate(
        preset, tweeter_gain_db=-13.0327, delay_us=404.777,
    )
    # Captured BEFORE the run-8 apply below persists — exactly like
    # handle_v2_apply's own read-only "reviewed_baseline" recompose, which
    # runs before apply_baseline_profile commits. Capturing it AFTER the
    # run-8 apply would read the run-8 profile's own (already-applied) state
    # back as its "prior", not the profile it actually replaced.
    retained = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=False,
        state_path=state_path,
        config_path=config_path,
        tuning_owner="automatic",
        measured_candidate=run8_candidate,
    )["applied_recomposition_profile"]
    assert retained is not None
    assert (
        retained["candidate_fingerprint"]
        == prior_payload["profile"]["candidate_fingerprint"]
    )

    run8_payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=run8_candidate,
    )
    assert run8_payload["status"] == "applied"
    run8_config_path = Path(run8_payload["profile"]["config"]["path"])
    assert run8_config_path != config_path, "run-8 must land on a content-addressed sibling"
    assert "applied_recomposition_profile" not in run8_payload["profile"]

    return (
        state_path, config_path, load_config, current_config_path,
        prior_payload, run8_payload, retained,
    )


async def test_restore_applied_baseline_profile_reverts_active_config_and_state(
    monkeypatch, tmp_path: Path,
) -> None:
    (
        state_path, config_path, load_config, current_config_path,
        prior_payload, run8_payload, retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)
    # #1666: every apply lands on its own content-addressed sibling, so
    # "prior's bytes" live at prior's OWN reported path, not config_path
    # (the canonical name, which by now holds run-8's post-apply promoted
    # copy -- see the canonical-tracks-run8 assertion below).
    prior_config_text = Path(
        prior_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    run8_config_text = Path(
        run8_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    # Sanity: the two profiles' own delay values are genuinely distinct
    # before restoring, so a passing restore is proof of REVERSION, not a
    # no-op.
    assert "delay: 0.2500" in prior_config_text
    assert "delay: 0.4048" in run8_config_text
    assert "delay: 0.4048" not in prior_config_text
    assert config_path.read_text(encoding="utf-8") == run8_config_text, (
        "canonical should hold run-8's promoted bytes before the restore"
    )

    restore_payload = await restore_applied_baseline_profile(
        retained,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert restore_payload["status"] == "restored", restore_payload.get("issues")
    # Reloaded the PRIOR profile's own already-compiled file, unmutated —
    # never recomposed, so the file's bytes are exactly what they were, and
    # the run-8 candidate's delay is gone from the ACTIVE config. The restore
    # ALSO re-promotes canonical back onto prior's bytes (#1666), which is
    # what this next assertion now proves -- pre-fix, canonical was simply
    # never touched by run-8's apply in the first place, so this held
    # trivially; post-fix it holds because restore re-promotes it.
    assert config_path.read_text(encoding="utf-8") == prior_config_text
    active = load_applied_baseline_profile_state(state_path)
    assert active is not None
    assert (
        active["candidate_fingerprint"]
        == prior_payload["profile"]["candidate_fingerprint"]
    )
    assert active["candidate_fingerprint"] != run8_payload["profile"]["candidate_fingerprint"]
    # The JSON SSOT keeps the truthful applied (sibling) path, never canonical.
    assert active["config"]["path"] == prior_payload["profile"]["config"]["path"]
    assert active["config"]["path"] != str(config_path)


async def test_restore_applied_baseline_profile_blocked_when_config_missing(
    monkeypatch, tmp_path: Path,
) -> None:
    (
        state_path, config_path, load_config, current_config_path,
        _prior_payload, _run8_payload, retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)
    # #1666: restore reloads retained's OWN reported path (a content-addressed
    # sibling), never config_path (the canonical name) -- delete the actual
    # restore target, not the unrelated canonical file.
    Path(retained["config"]["path"]).unlink()

    payload = await restore_applied_baseline_profile(
        retained,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    assert {issue["code"] for issue in payload["issues"]} == {"restore_target_missing"}


async def test_restore_applied_baseline_profile_blocked_on_invalid_snapshot(
    monkeypatch, tmp_path: Path,
) -> None:
    (
        state_path, config_path, load_config, current_config_path,
        _prior_payload, _run8_payload, retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)
    tampered = dict(retained)
    tampered["candidate_fingerprint"] = "declared-wrong"

    payload = await restore_applied_baseline_profile(
        tampered,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert payload["status"] == "blocked"
    assert {issue["code"] for issue in payload["issues"]} == {"restore_target_invalid"}


async def test_restore_applied_baseline_profile_reports_restore_failed(
    monkeypatch, tmp_path: Path,
) -> None:
    (
        state_path, config_path, _load_config, current_config_path,
        _prior_payload, _run8_payload, retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    async def failing_load_config(_path: str) -> bool:
        return False

    payload = await restore_applied_baseline_profile(
        retained,
        load_config=failing_load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert payload["status"] == "restore_failed"
    assert {issue["code"] for issue in payload["issues"]} == {"restore_apply_failed"}
    # A failed restore must not clobber the currently-applied (run-8) SSOT.
    active = load_applied_baseline_profile_state(state_path)
    assert active is not None
    assert (
        active["candidate_fingerprint"]
        != retained["candidate_fingerprint"]
    )


# --- #1666: apply-promotion durability ---------------------------------- #
#
# build_baseline_profile_candidate never writes baseline_config_path()
# directly; every write=True candidate lands on its own content-addressed
# sibling. The canonical name is published ONLY by a post-success promote
# (a byte copy of the just-applied candidate) in _apply_baseline_profile_locked
# and restore_applied_baseline_profile. Root cause: the OLD parity check
# (rename to a sibling only when the previously-applied profile's own path
# equalled canonical) made an applied profile's path strictly ALTERNATE
# between canonical and a sibling on every successive apply -- so half the
# time, unvalidated candidate bytes landed on the canonical name BEFORE
# CamillaDSP had confirmed them, and a rejected apply could leave rejected
# bytes there.


async def test_second_apply_promotes_canonical_to_sibling_bytes(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1666 core regression: written FIRST and confirmed to fail against the
    unfixed parity check (git-stash evidence in the PR), before the fix
    landed. After the SECOND (sibling-landing, under the old code) apply, the
    canonical file must be byte-identical to the just-applied candidate --
    not stale at whatever the FIRST apply wrote directly to canonical."""
    (
        state_path, config_path, load_config, current_config_path,
        prior_payload, run8_payload, retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    run8_config_text = Path(
        run8_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")

    assert config_path.exists()
    assert config_path.read_text(encoding="utf-8") == run8_config_text


async def test_first_ever_apply_lands_on_sibling_and_promotes_canonical(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1666: even a speaker's FIRST-EVER apply (no prior applied anchor)
    lands its candidate on a content-addressed sibling -- never writes
    baseline_config_path() in place, changing today's behaviour where a
    first apply wrote canonical directly -- and canonical ends up holding
    the applied bytes via the post-success promote."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    config_path = tmp_path / "active_speaker_baseline.yml"
    current_path: str | None = None

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str | None:
        return current_path

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_path,
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )

    assert payload["status"] == "applied"
    applied_path = Path(payload["profile"]["config"]["path"])
    assert applied_path != config_path
    assert applied_path.name.startswith("active_speaker_baseline_candidate_")
    assert config_path.exists()
    assert config_path.read_text(encoding="utf-8") == applied_path.read_text(
        encoding="utf-8"
    )


async def test_failed_apply_never_touches_canonical_file(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1666: a rejected candidate's bytes must never reach the canonical
    name. Pre-existing canonical content (as if an earlier successful
    apply/promote had run) survives a later failed apply completely
    untouched."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    config_path = tmp_path / "active_speaker_baseline.yml"
    sentinel = "# pre-existing canonical content -- must survive a failed apply\n"
    config_path.write_text(sentinel, encoding="utf-8")

    async def load_config(_path: str) -> bool:
        return False  # CamillaDSP rejects every candidate

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_path,
        validate=_valid_config,
    )

    assert payload["status"] == "apply_failed"
    assert config_path.read_text(encoding="utf-8") == sentinel


async def test_promote_failure_is_fail_soft_apply_still_succeeds(
    monkeypatch, tmp_path: Path, caplog,
) -> None:
    """#1666: a promote failure (disk full, permission drift, ...) must never
    fail an otherwise-successful apply -- the running CamillaDSP graph and
    the JSON SSOT are already correct by the time promote runs. Only a
    WARNING event is emitted; canonical is left exactly as it was."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    config_path = tmp_path / "active_speaker_baseline.yml"
    current_path: str | None = None

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str | None:
        return current_path

    real_write = baseline_profile_mod.atomic_write_text

    def promote_write_fails(path, text, **kwargs):
        # Only the promote's write to the canonical file fails -- the JSON
        # SSOT write (persist_applied_baseline_profile, a different path)
        # goes through normally, exercising the REAL apply-succeeds-anyway
        # path rather than mocking the promote function away entirely.
        if Path(path) == config_path:
            raise OSError("disk full (simulated)")
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(
        baseline_profile_mod, "atomic_write_text", promote_write_fails
    )

    with caplog.at_level(logging.WARNING, logger=_BASELINE_LOGGER):
        payload = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            get_current_config_path=current_config_path,
            state_path=tmp_path / "baseline_profile.json",
            config_path=config_path,
            validate=_valid_config,
        )

    assert payload["status"] == "applied"
    assert not config_path.exists()
    # The applied candidate's OWN sibling file is unaffected -- only the
    # canonical copy failed.
    assert Path(payload["profile"]["config"]["path"]).exists()
    warnings = _events(caplog, "dsp.baseline_promote")
    assert len(warnings) == 1
    assert "result=failed" in warnings[0]
    assert "disk full" in warnings[0]


async def test_promote_failure_from_unicode_decode_error_is_fail_soft(
    monkeypatch, tmp_path: Path, caplog,
) -> None:
    """#1666 review S2: read_text() can raise UnicodeDecodeError (a
    ValueError subtype, not an OSError) on a corrupted-but-present candidate
    sibling. The old ``except OSError`` let that propagate and fail an
    otherwise-successful apply, contradicting the documented "must never
    fail an otherwise-successful apply" contract this promote-fail-soft
    family exists to prove -- see
    test_promote_failure_is_fail_soft_apply_still_succeeds above for the
    OSError case this mirrors."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    config_path = tmp_path / "active_speaker_baseline.yml"
    current_path: str | None = None

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str | None:
        return current_path

    # Arm the corruption only for the very next utf-8 text read AFTER
    # persist_applied_baseline_profile returns -- that is promote's own
    # applied_path.read_text(encoding="utf-8") call, the sole target here.
    # An earlier utf-8 read of the same candidate sibling (the bass-
    # extension graph-safety proof, which runs before the DSP apply) must
    # keep succeeding, or the apply would be blocked before ever reaching
    # promote and this test would no longer exercise the fail-soft catch.
    real_persist = baseline_profile_mod.persist_applied_baseline_profile
    armed = False

    def persist_then_arm(*args, **kwargs):
        nonlocal armed
        result = real_persist(*args, **kwargs)
        armed = True
        return result

    monkeypatch.setattr(
        baseline_profile_mod, "persist_applied_baseline_profile", persist_then_arm
    )

    real_read_text = Path.read_text

    def read_text_fails_once_armed(self, *args, **kwargs):
        nonlocal armed
        if armed and kwargs.get("encoding") == "utf-8":
            armed = False
            raise UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "simulated corrupt candidate bytes"
            )
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_fails_once_armed)

    with caplog.at_level(logging.WARNING, logger=_BASELINE_LOGGER):
        payload = await apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=load_config,
            get_current_config_path=current_config_path,
            state_path=tmp_path / "baseline_profile.json",
            config_path=config_path,
            validate=_valid_config,
        )

    assert payload["status"] == "applied"
    assert not config_path.exists()
    # The applied candidate's OWN sibling file is unaffected -- only the
    # canonical copy's read failed.
    assert Path(payload["profile"]["config"]["path"]).exists()
    warnings = _events(caplog, "dsp.baseline_promote")
    assert len(warnings) == 1
    assert "result=failed" in warnings[0]


async def test_restore_promotes_canonical_to_prior_candidate_bytes(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1666: restore_applied_baseline_profile's own post-success promote
    (not just apply's) republishes canonical -- back onto the RESTORED
    (prior) candidate's bytes, not whatever the most recent apply left."""
    (
        state_path, config_path, load_config, current_config_path,
        prior_payload, run8_payload, retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)
    prior_config_text = Path(
        prior_payload["profile"]["config"]["path"]
    ).read_text(encoding="utf-8")
    # Sanity: canonical currently tracks run-8 (the most recent apply), not
    # prior, before the restore below.
    assert config_path.read_text(encoding="utf-8") != prior_config_text

    restore_payload = await restore_applied_baseline_profile(
        retained,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert restore_payload["status"] == "restored", restore_payload.get("issues")
    assert config_path.read_text(encoding="utf-8") == prior_config_text


async def test_promote_prunes_old_candidate_siblings_beyond_newest_k(
    monkeypatch, tmp_path: Path,
) -> None:
    """#1666: unbounded candidate-sibling growth (each promote is a byte COPY,
    never a move, so every applied candidate's own file lives on disk
    forever) is bounded to the newest _MAX_BASELINE_CANDIDATE_FILES by mtime
    on every successful promote. The just-applied candidate always survives
    (it is always the newest); canonical itself is never a pruning
    candidate (it carries no ``_candidate_`` suffix)."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json")
    )
    config_path = tmp_path / "active_speaker_baseline.yml"
    current_path: str | None = None

    async def load_config(path: str) -> bool:
        nonlocal current_path
        current_path = path
        return True

    async def current_config_path() -> str | None:
        return current_path

    keep = baseline_profile_mod._MAX_BASELINE_CANDIDATE_FILES
    # Pre-seed more orphaned siblings than the keep-count, all older (by
    # explicit mtime) than the real apply below -- the "38 orphans on a
    # fleet Pi" shape from a box that only ever promotes, never prunes.
    orphan_count = keep + 5
    now = time.time()
    for i in range(orphan_count):
        sibling = tmp_path / f"active_speaker_baseline_candidate_orphan{i:03d}.yml"
        sibling.write_text(f"# orphan {i}\n", encoding="utf-8")
        orphan_mtime = now - (orphan_count - i) * 10
        os.utime(sibling, (orphan_mtime, orphan_mtime))

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_path,
        validate=_valid_config,
    )
    assert payload["status"] == "applied"
    applied_path = Path(payload["profile"]["config"]["path"])

    remaining = sorted(tmp_path.glob("active_speaker_baseline_candidate_*.yml"))
    assert len(remaining) == keep
    assert applied_path in remaining
    # The newest orphans (closest to "now") survive; the oldest are pruned.
    survivors = {p.name for p in remaining}
    assert f"active_speaker_baseline_candidate_orphan{orphan_count - 1:03d}.yml" in (
        survivors
    )
    assert "active_speaker_baseline_candidate_orphan000.yml" not in survivors
    # Canonical itself (no _candidate_ suffix) is untouched by pruning.
    assert config_path.exists()
    assert config_path.name not in survivors


def test_prune_keeps_the_protected_undo_target_even_when_it_is_oldest(
    tmp_path: Path,
) -> None:
    """Item 3 (#1605): the Undo target (pre_apply_profile.config.path) is a
    content-addressed sibling that handle_v2_restore reloads. Even when it is
    the OLDEST candidate — so the newest-K mtime prune would evict it —
    passing it via ``also_protect`` keeps it on disk, while the on-disk total
    stays bounded to K (a protected sibling costs a slot, it does not add to
    K). Without this, ~K compiles between two applies would silently break
    Undo."""
    canonical = tmp_path / "active_speaker_baseline.yml"
    canonical.write_text("# canonical\n", encoding="utf-8")
    keep = baseline_profile_mod._MAX_BASELINE_CANDIDATE_FILES
    now = time.time()

    # The Undo target is the very oldest candidate on disk.
    undo_target = tmp_path / "active_speaker_baseline_candidate_undo0000.yml"
    undo_target.write_text("# undo target\n", encoding="utf-8")
    os.utime(undo_target, (now - 10_000, now - 10_000))
    # More newer orphans than the keep-count, all newer than the undo target.
    orphan_count = keep + 5
    for i in range(orphan_count):
        sib = tmp_path / f"active_speaker_baseline_candidate_orphan{i:03d}.yml"
        sib.write_text(f"# orphan {i}\n", encoding="utf-8")
        os.utime(sib, (now - (orphan_count - i), now - (orphan_count - i)))
    # The just-applied candidate is the newest.
    applied = tmp_path / "active_speaker_baseline_candidate_applied.yml"
    applied.write_text("# applied\n", encoding="utf-8")
    os.utime(applied, (now, now))

    baseline_profile_mod._prune_baseline_candidate_siblings(
        canonical, protect=applied, also_protect=[str(undo_target)]
    )

    remaining = {
        p.name for p in tmp_path.glob("active_speaker_baseline_candidate_*.yml")
    }
    assert applied.name in remaining        # the just-applied candidate survives
    assert undo_target.name in remaining    # protected despite being the oldest
    assert len(remaining) == keep           # total still bounded to K
    # A non-protected old orphan is still pruned — protection is targeted.
    assert "active_speaker_baseline_candidate_orphan000.yml" not in remaining
