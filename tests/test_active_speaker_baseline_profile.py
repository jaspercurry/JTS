# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import shlex
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
    MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB,
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
)
from jasper.active_speaker import driver_base_trim as dbt
from jasper.active_speaker.commissioning_coordinator import (
    build_commissioning_view,
)
from jasper.active_speaker.crossover_preview import (
    build_crossover_preview,
    crossover_preview_fingerprint,
)
from jasper.active_speaker.crossover_v2.intervention import LEVEL_MATCH_AXIS
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
from jasper.dsp_apply import CamillaConfigValidationResult
from jasper.output_hardware import DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import (
    mono_output_topology,
    valid_camilla_config as _valid_config,
)
from tests.test_active_speaker_profile import _two_way_preset


# What a REAL MEASURE analysis records and PR-L4 item 5 counts as evidence: the
# trim solve's own per-role output, and the cross-branch alignment confidence
# (measured across both branches at once, hence summed-domain evidence). Stub
# analyses in this file carried neither, which is fine while a candidate's mere
# existence satisfied the completeness flags and is not once the flags count
# what the analysis actually recorded.
_MEASURE_EVIDENCE = {
    "trim_band_average_db": {"woofer": 0.0, "tweeter": -12.4},
    "alignment_confidence": 0.82,
}


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
            # 2000, not 2500, and the gap is load-bearing. Since #2603 this one
            # field IS the declared protection floor, and the apply-time gate
            # (camilla_yaml._assert_tweeter_crossover_honours_declared_floor)
            # refuses any crossover below it. Declaring 2500 while ALSO crossing
            # at 2500 left this fixture speaker with zero downward headroom, so
            # every alternative-Fc test below (the 2500 -> 2250 nudge the #2292
            # undo pins use) was exercising a crossover its own driver
            # declaration forbids. 2000 keeps the fixture honest -- still far
            # above this driver's own do_not_test_below_hz of 1200 -- and lets
            # those tests go on testing apply/undo mechanics rather than
            # tripping a safety gate. Raise it here to test the gate itself;
            # tests/test_active_speaker_protection_floor.py owns those pins.
            "recommended_highpass_hz": 2000,
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
    # #1666: every write=True candidate lands on its own source-fingerprinted
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
    # #2285 P2: the ACTIVE ring is the ONE legal outputd endpoint, so an
    # active-capable topology resolves to it unconditionally. The SOURCE is
    # unchanged (`outputd_active_lane`) — the transport moved, not the seam.
    assert payload["config"]["playback_device"] == "jts_ring_active_playback"
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"
    assert 'device: "jts_ring_active_playback"' in yaml
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
    # #1666: candidate lands on a source-fingerprinted sibling, not config_path.
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
    never touches the crossover / per-driver limiters / tweeter HP / 0 dB ceiling.

    #2285 P2: the DEFAULT is now the ring capture lane, not the snd-aloop
    fan-in tap. An active-capable topology resolves its playback to the ACTIVE
    ring (the one legal endpoint), and ``active_emit_devices`` moves BOTH
    halves together — a graph sinking to the ring while sourcing the tap
    captures a device fan-in stops feeding. What is pinned here is the
    threading, not the name: passing the resolved default explicitly must stay
    byte-identical to omitting it.
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
        # #1666: candidate lands on a source-fingerprinted sibling, not config_path.
        return payload, Path(payload["config"]["path"]).read_text(encoding="utf-8")

    implicit, implicit_yaml = _emit("implicit", None)
    explicit, explicit_yaml = _emit("explicit_default", "jts_ring_capture")
    follower, follower_yaml = _emit("follower", "hw:CARD=Loopback,DEV=1")

    # inv 1: passing the default explicitly is byte-identical to not passing it,
    # and the solo baseline captures from Ring A.
    assert implicit_yaml == explicit_yaml
    assert implicit["config"]["sha256"] == explicit["config"]["sha256"]
    assert 'device: "jts_ring_capture"' in implicit_yaml

    # A follower's round-trip-loopback capture changes ONLY the capture line.
    assert 'device: "hw:CARD=Loopback,DEV=1"' in follower_yaml
    impl_lines = implicit_yaml.splitlines()
    foll_lines = follower_yaml.splitlines()
    assert len(impl_lines) == len(foll_lines)
    diff = [(a, b) for a, b in zip(impl_lines, foll_lines) if a != b]
    assert len(diff) == 1
    assert diff[0][0].strip() == 'device: "jts_ring_capture"'
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
    # #1666's sibling rename is gated OFF for driver_domain=True (see
    # build_baseline_profile_candidate): this seam compiles and immediately
    # consumes its OWN config_path, so unlike a solo candidate the reported
    # path IS config_path. Read back through the reported path either way.
    assert Path(payload["config"]["path"]) == config_path
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


def test_a_write_free_rebuild_knowing_less_does_not_supersede_the_applied_profile(
    tmp_path: Path,
) -> None:
    """Only the apply paths carry a measured candidate; the read side rebuilds.

    So a rebuild's ``source`` is the applied profile's minus
    ``measured_candidate_fingerprint``, and comparing composite fingerprints
    reported a live measured tune as superseded with an empty ``changed``.
    See ADR-0195.
    """

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    baseline_state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    measured = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=True,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=_v2_candidate(preset),
        created_at="2026-07-18T12:20:00Z",
    )
    applied = {**measured, "status": "applied", "applied_at": "2026-07-18T12:21:00Z"}
    baseline_state_path.write_text(json.dumps(applied), encoding="utf-8")

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=False,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    # The blind set — keys the applied record carries and this rebuild cannot —
    # is exactly one. Anything else appearing here is a source key whose change
    # would stop being detected, which is what the derived `changed` guards.
    assert set(applied["source"]) - set(payload["source"]) == {
        "measured_candidate_fingerprint",
    }
    assert applied["source"]["fingerprint"] != payload["source"]["fingerprint"]
    assert payload["revalidation"]["required"] is False
    assert payload["applied_recomposition_profile"]["status"] == "applied"
    assert payload["applied_profile_stands"] is True
    # A phone-measured apply satisfies its own gate from the candidate's own
    # evidence and records no driver checks in the measurement state. The
    # standing profile carries that proof, so what remains outstanding is the
    # wizard's combined check — and `summed_test_driver_target_proof_missing`,
    # which gates the audible test and banking the check, is not raised.
    assert payload["driver_target_proof_from_applied_profile"] is True
    assert "baseline_driver_measurements_missing" not in {
        issue["code"] for issue in payload["issues"]
    }

    view = build_commissioning_view(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        baseline_profile=payload,
        applied_profile_verdict="",
    )

    assert view["driver_target_proof"]["complete"] is True
    assert view["driver_target_proof"]["source"] == "applied_profile"
    assert next(
        step for step in view["steps"] if step["id"] == "profile"
    )["status"] == "done"
    assert view["next_action"]["id"] == "start_combined_test"


def test_a_provisional_applied_profile_never_stands_in_for_driver_evidence(
    tmp_path: Path,
) -> None:
    """Sensitivity-derived trims are not measurement, whatever else holds."""

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    baseline_state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    ready = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=True,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=_v2_candidate(preset),
        created_at="2026-07-18T12:20:00Z",
    )
    applied = {
        **ready,
        "status": "applied",
        "applied_at": "2026-07-18T12:21:00Z",
        "provisional": True,
    }
    baseline_state_path.write_text(json.dumps(applied), encoding="utf-8")

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=False,
        state_path=baseline_state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert payload["revalidation"]["required"] is False
    assert payload["driver_target_proof_from_applied_profile"] is False
    assert "baseline_driver_measurements_missing" in {
        issue["code"] for issue in payload["issues"]
    }


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
    # PR-L4 item 5: a `complete: true` must name the lane that satisfied it and
    # show that lane's own count. This test used to stop at the booleans, which
    # is how `summed_validation_complete: true` beside a zero count got pinned
    # as intended — the shape the 2026-07-27 forensics found in the record of a
    # 10 dB-dark speaker. Nothing here is vacuous: the summed flag is carried by
    # a real recorded validation, and the driver flag by a named non-measurement
    # source, which is why `captured_driver_count` is legitimately 0.
    assert payload["verification"]["summed_validation_source"] == "measurements"
    assert payload["verification"]["validated_summed_group_count"] >= 1
    assert payload["verification"]["captured_driver_count"] == 0
    assert payload["verification"]["measured_candidate_evidence"] == {
        "driver": 0, "summed": 0,
    }
    assert payload["revalidation"]["required"] is True
    assert payload["revalidation"]["next_step"] == "save_profile"


def test_completeness_flags_are_never_true_without_a_named_evidence_lane(
    tmp_path: Path,
) -> None:
    """PR-L4 item 5, as an invariant rather than a single scenario.

    Across the shapes a profile can be compiled from, no ``*_complete: true``
    may be published with its source reading ``missing`` — and a source naming
    a lane must have a non-zero count in that lane. This is the assertion whose
    absence let a candidate object's mere existence stand in for measured
    evidence.
    """
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")

    def _verification(**kwargs):
        return build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            write=False,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
            **kwargs,
        )["verification"]

    preset, _preset_issues, _gates = compile_preset_from_crossover_preview(
        topology, dict(preview)
    )
    lanes = {
        "measured_candidate": "measured_candidate_evidence",
        "measurements": None,  # counted by the two summary counts below
    }
    candidates = [
        _verification(measurements={}),
        _verification(measurements=_measurements(topology, tmp_path)),
        # The measured-candidate lane, which the first version of this test
        # never exercised — so its `source == "measured_candidate"` branch was
        # dead and the invariant was, itself, vacuous about vacuousness
        # (PR-L4 review S1).
        _verification(
            measurements={},
            tuning_owner="automatic",
            measured_candidate=MeasuredCrossoverCandidate(
                program_id="prog-v2-invariant",
                analysis={"drift_ppm": 3.0, **_MEASURE_EVIDENCE},
                source_preset=preset,
                role_attenuations_db={"woofer": 0.0, "tweeter": -12.4},
            ),
        ),
    ]
    # A blocked payload publishes `verification: {}` — no flags, so no claim.
    # That is the honest shape and there is nothing here to check; the invariant
    # is about blocks that DO claim something.
    graded = [v for v in candidates if v]
    assert graded, "at least one shape must reach a verification block"
    seen_measured_candidate: set[str] = set()
    for verification in graded:
        for flag, source_key, lane_count in (
            ("driver_target_proof_complete", "driver_target_proof_source", "driver"),
            ("summed_validation_complete", "summed_validation_source", "summed"),
        ):
            source = verification[source_key]
            if not verification[flag]:
                assert source == "missing"
                continue
            assert source != "missing", (flag, verification)
            assert source in lanes or source == "applied_profile_revalidation"
            if source == "measured_candidate":
                assert verification["measured_candidate_evidence"][lane_count] > 0
                seen_measured_candidate.add(source_key)

    # ...and the measured-candidate branch above is REACHED, on both flags.
    assert seen_measured_candidate == {
        "driver_target_proof_source", "summed_validation_source",
    }


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
    # #1666: the transaction loads the candidate's own source-fingerprinted
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


async def test_apply_baseline_profile_persists_applied_state_durably(
    monkeypatch, tmp_path: Path,
) -> None:
    """#2292 scope 2: persist_applied_baseline_profile's write is durable
    (fsync file + parent directory). The earlier pre-apply "ready_to_apply"
    review write to the SAME state path (build_baseline_profile_candidate's
    own write=True) is NOT durable -- only the apply seam itself opts in."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    fsync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd))

    async def load_config(path: str) -> bool:
        return True

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert payload["status"] == "applied"
    # Exactly one durable WRITE (file fsync + parent-dir fsync) across the
    # whole apply -- persist_applied_baseline_profile's, not the earlier
    # pre-apply candidate review write to the same path. The third fsync is
    # the base-trim seam's: this profile is not level-matched by measurement,
    # so the apply CLEARS the banked trim, and that unlink is made durable by
    # its own parent-dir fsync (no file fsync -- there is no file to sync).
    assert len(fsync_calls) == 3


async def test_apply_baseline_profile_releases_the_staged_startup_hold(
    monkeypatch, tmp_path: Path,
) -> None:
    """A completed commission is the END of the startup-load hold.

    Observed on jts3: after save-and-apply put the baseline live, the ephemeral
    hold marker was still set, because only the rollback path cleared it. It is
    inert while it lingers — ``safe_graph_for_current_topology``'s rung ALSO
    requires the current graph to classify as all-muted-active-startup, which an
    applied baseline does not — but it is a latent surprise for the NEXT
    commission and it makes the doctor's "marker present" state ambiguous. The
    apply seam owns the release, so every caller that records a baseline as
    applied clears it.
    """
    from jasper.active_speaker.startup_hold import (
        hold_staged_startup,
        staged_startup_hold_active,
    )

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )

    async def load_config(path: str) -> bool:
        return True

    assert hold_staged_startup() is True
    assert staged_startup_hold_active() is True

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

    assert payload["status"] == "applied"
    assert staged_startup_hold_active() is False


async def test_persist_applied_baseline_releases_the_hold_on_its_idempotent_return(
    monkeypatch, tmp_path: Path,
) -> None:
    """The release sits ABOVE the already-applied early return, on purpose.

    ``persist_applied_baseline_profile`` returns early when the same candidate
    is already the applied SSOT, without writing. A release placed below that
    return would skip exactly the case where the hold is most likely to be stale
    — a re-run of a commission that already finished — and no test would notice,
    because the ordinary path releases either way. So this pins the placement,
    not just the behaviour: it re-takes the hold and drives the IDEMPOTENT call.
    """
    from jasper.active_speaker.startup_hold import (
        hold_staged_startup,
        staged_startup_hold_active,
    )

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft)
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    state_path = tmp_path / "baseline_profile.json"

    captured: dict[str, object] = {}
    real_persist = baseline_profile_mod.persist_applied_baseline_profile

    def spy_persist(candidate, **kwargs):
        captured["candidate"] = candidate
        captured["kwargs"] = kwargs
        return real_persist(candidate, **kwargs)

    monkeypatch.setattr(
        baseline_profile_mod, "persist_applied_baseline_profile", spy_persist
    )

    async def load_config(path: str) -> bool:
        return True

    payload = await apply_baseline_profile(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        load_config=load_config,
        state_path=state_path,
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    assert payload["status"] == "applied"
    assert "candidate" in captured, "the apply never reached the persist seam"

    # Second, identical persist: same candidate, same apply proof, already
    # applied — the early-return path. Re-take the hold first so its release is
    # the only thing that can clear it.
    writes: list[object] = []
    real_write = baseline_profile_mod.atomic_write_text

    def counting_write(*args, **kwargs):
        writes.append(args[0])
        return real_write(*args, **kwargs)

    monkeypatch.setattr(baseline_profile_mod, "atomic_write_text", counting_write)
    assert hold_staged_startup() is True
    assert staged_startup_hold_active() is True

    again = real_persist(captured["candidate"], **captured["kwargs"])

    assert again["status"] == "applied"
    assert writes == [], (
        "this call must be the idempotent early return — if it wrote, the test "
        "is exercising the ordinary path and proves nothing about placement"
    )
    assert staged_startup_hold_active() is False


async def test_apply_baseline_profile_reloads_when_target_config_differs(
    monkeypatch, tmp_path: Path,
) -> None:
    """Sequential applies of genuinely different candidates each command
    their own real CamillaDSP reload -- pre-PR-#2292 behavior: this
    transaction never skips a load based on the candidate's path (#2292
    scope 2 tried a same-path double-load guard; an adversarial review
    demonstrated the candidate filename is a SOURCE fingerprint that does
    not cover every input to the compiled graph -- e.g. bass_extension_profile
    -- so two different graphs can share one filename, and get_config_file_path
    also does not see set_active_config_raw loads (camilla.py), together
    making path equality an unsafe proxy for graph equality. The guard was
    dropped rather than repaired; #2291 Phase 3/4 owns any future guard built
    on a live graph-identity oracle instead of a path)."""
    topology = _dual_apple_topology()
    measurements = _measurements(topology, tmp_path)
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    state_path = tmp_path / "baseline_profile.json"
    config_path = tmp_path / "active_speaker_baseline.yml"
    calls: list[str] = []
    current_path: str | None = None

    async def load_config(path: str) -> bool:
        nonlocal current_path
        calls.append(path)
        current_path = path
        return True

    async def current_config_path() -> str | None:
        return current_path

    draft_a = _draft(topology, tweeter_gain_db=-18.5)
    preview_a = build_crossover_preview(draft_a)
    first = await apply_baseline_profile(
        topology,
        design_draft=draft_a,
        crossover_preview=preview_a,
        measurements=measurements,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )
    assert first["status"] == "applied"

    draft_b = _draft(topology, tweeter_gain_db=-6.0)
    preview_b = build_crossover_preview(draft_b)
    second = await apply_baseline_profile(
        topology,
        design_draft=draft_b,
        crossover_preview=preview_b,
        measurements=measurements,
        load_config=load_config,
        get_current_config_path=current_config_path,
        state_path=state_path,
        config_path=config_path,
        validate=_valid_config,
    )

    assert second["status"] == "applied"
    assert second["profile"]["config"]["path"] != first["profile"]["config"]["path"]
    assert calls == [
        first["profile"]["config"]["path"], second["profile"]["config"]["path"],
    ]


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


def test_recompose_applied_baseline_yaml_matches_the_durable_candidate_it_records(
    tmp_path: Path,
) -> None:
    """Pins the IMMUTABLE-snapshot recompose (#2572): re-deriving
    ``recompose_applied_baseline_yaml`` from the applied record's frozen
    ``recomposition_snapshot`` must reproduce the exact bytes the durable
    builder wrote — this is what the graph carrier calls on every ``/sound``
    save and every deploy reconcile. Nothing pinned it, so "the record can
    reproduce its own graph" was an untested assumption.

    WHY THIS IS LOAD-BEARING NOW. It is the premise under the reconcile's
    content-aware no-op (``jasper.sound.runtime._running_config_is_intent``,
    ``tests/test_reconcile_adoption_durability.py``): a kept correction survives
    a deploy only because re-deriving it from the record reproduces the exact
    bytes already on disk. If that ever stops holding, the no-op silently stops
    firing — the reconcile falls back to write-and-apply, the applied record is
    displaced from the statefile again, and mid-series adoption breaks the same
    way it did on jts3. A degradation with no failing test is the shape this
    guards against, which is why the assertion is byte equality and not a
    weaker "both are approved graphs".
    """
    from jasper.active_speaker.baseline_profile import (
        recompose_applied_baseline_yaml,
    )

    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-06-14T12:10:00Z")
    built = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=_measurements(topology, tmp_path),
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )
    # #1666: the candidate lands on its own source-fingerprinted sibling, and
    # THAT path is what the applied record carries — the name the reconcile then
    # finds as the running config.
    durable_path = Path(built["config"]["path"])
    assert durable_path.name.startswith("active_speaker_baseline_candidate_")
    durable_yaml = durable_path.read_text(encoding="utf-8")

    built["status"] = "applied"
    recomposed, issues = recompose_applied_baseline_yaml(
        topology, applied_profile=built
    )

    assert issues == []
    assert recomposed == durable_yaml


def _applied_mono_baseline(tmp_path: Path):
    """A real APPLIED profile on the jts3-shaped box (mono 2-way on a DAC8x)."""
    topology = _topology()
    draft = _draft(topology)
    applied = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=build_crossover_preview(draft),
        measurements=_measurements(topology, tmp_path),
        write=False,
        state_path=tmp_path / "active-speaker-profile.json",
        config_path=tmp_path / "configs" / "active-speaker-baseline.yml",
        validate=_valid_config,
    )
    applied["status"] = "applied"
    return topology, applied


# The resolutions the ring re-emit is pinned against. ``S24_3LE`` is a SENTINEL
# — no resolver answers it today — and it is here because the two real tokens
# cannot prove pass-through on their own: ``RING_WIRE_FORMAT`` is what a
# hardcoded narrow constant would emit, and ``RING_WIRE_FORMAT_WIDE`` is
# byte-identical to ``DEFAULT_PLAYBACK_FORMAT``, the very default the defect
# inherited. A pin against either alone passes against the code that shipped the
# jts3 shear. The guard below fails if a future default swallows the sentinel too.
_RING_WIRE_PASSTHROUGH_TOKENS = ("S16_LE", "S32_LE", "S24_3LE")


@pytest.mark.parametrize("resolved_format", _RING_WIRE_PASSTHROUGH_TOKENS)
def test_ring_reemit_declares_whatever_the_resolver_answers(
    tmp_path: Path, monkeypatch, resolved_format: str
) -> None:
    """Defect A: the ring re-emit READS ``resolve_ring_wire``, it does not
    inherit the box's program-lane format.

    On jts3 the re-emit wrote ``format: S32_LE`` — the box's program-lane
    default — against a resolver answering ``S16_LE``
    (``captures/r7b-jts3-arm2-20260811T132227Z``, file 12): a sheared attach
    waiting at the arm's last rung. What has to be pinned is PASS-THROUGH, not
    any one value, so the resolver is driven across every token in
    ``_RING_WIRE_PASSTHROUGH_TOKENS`` and the graph must follow it to each.
    """
    from jasper.active_speaker.baseline_profile import (
        recompose_applied_baseline_yaml,
    )
    from jasper.camilla_config_contract import (
        DEFAULT_PLAYBACK_FORMAT,
        parse_camilla_devices_config,
    )
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT,
        RingWire,
    )

    # NON-VACUITY: at least one token must differ from BOTH the shipped ring
    # wire and the program-lane default, or this whole parametrization passes
    # against an emitter that reads a constant.
    assert any(
        token not in (RING_WIRE_FORMAT, DEFAULT_PLAYBACK_FORMAT)
        for token in _RING_WIRE_PASSTHROUGH_TOKENS
    ), "no token distinguishes resolver-adoption from a constant; add one"

    topology, applied = _applied_mono_baseline(tmp_path)
    resolved = RingWire(
        sample_format=resolved_format,
        ring_a_channels=2,
        ring_b_channels=2,
        period_frames=128,
        ring_active_channels=2,
    )
    # The emitter reaches the resolver through jasper.fanin_coupling, so patch
    # it there rather than on a name some other module re-exported.
    monkeypatch.setattr(
        "jasper.fanin_coupling.resolve_ring_wire", lambda topology=None: resolved
    )

    ring_yaml, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )
    assert issues == []
    assert ring_yaml is not None
    devices = parse_camilla_devices_config(ring_yaml)
    assert devices["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    assert devices["playback_format"] == resolved_format

    # CONTROL: the same evidence emitted at the ALSA active lane is untouched by
    # the resolver — a helper that answered the ring's wire for every sink would
    # pass the assertion above and mis-declare every unarmed box.
    from jasper.active_speaker.runtime_contract import (
        OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )

    alsa_yaml, alsa_issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )
    assert alsa_issues == []
    assert alsa_yaml is not None
    assert (
        parse_camilla_devices_config(alsa_yaml)["playback_format"]
        == DEFAULT_PLAYBACK_FORMAT
    )


def test_ring_reemit_refuses_a_typod_wire_instead_of_raising(
    tmp_path: Path, monkeypatch
) -> None:
    """A typo'd ``JASPER_FANIN_RING_WIRE_FORMAT`` is a REFUSAL, not a traceback.

    Failing loud on a token neither language recognizes is right — jasper-fanin
    parks on the same value rather than guessing a wire. But an operator running
    ``jasper-active-speaker baseline-reemit --endpoint ring`` must get this
    function's ordinary blocker, with the parser's own sentence in it, not a
    Python stack trace. Nothing has been written at this point either way; the
    emit precedes every write.
    """
    from jasper.active_speaker.baseline_profile import (
        recompose_applied_baseline_yaml,
    )
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_WIRE_FORMAT_ENV_VAR,
    )

    topology, applied = _applied_mono_baseline(tmp_path)
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(f"{RING_WIRE_FORMAT_ENV_VAR}=s32le\n", encoding="utf-8")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )

    ring_yaml, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )
    assert ring_yaml is None
    assert [issue["code"] for issue in issues] == ["ring_wire_declaration_invalid"]
    detail = issues[0]["message"]
    assert RING_WIRE_FORMAT_ENV_VAR in detail
    assert "s32le" in detail, "the operator needs to see the value they typed"

    # CONTROL: the same box emitting at the ALSA lane is unaffected — the wire is
    # only resolved for a ring sink, so a typo cannot block an unarmed box's
    # ordinary re-emit.
    from jasper.active_speaker.runtime_contract import (
        OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )

    alsa_yaml, alsa_issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )
    assert alsa_issues == []
    assert alsa_yaml is not None


def test_ring_reemit_carries_the_certified_ring_chunk_and_target(
    tmp_path: Path,
) -> None:
    """Question C: a ring-endpoint graph carries the RING's CamillaDSP geometry,
    not the box's ``CamillaFloor``.

    jts3's DAC8x floor is ``CamillaFloor(256, 1536)``. Its
    ``target_level`` alone (1536) is six times the whole 2-slot ring's
    256-frame capacity, so a ring graph emitted at the floor is a second shear
    waiting at the same rung the format shear halted. Both numbers come from the
    ONE home that already encodes the certified ring pairing for the stereo ring
    (``jasper.fanin_coupling``'s ``RING_CAMILLA_*``), never a second copy.
    """
    from jasper.active_speaker.baseline_profile import (
        recompose_applied_baseline_yaml,
    )
    from jasper.audio_hardware.dac import camilla_floor_for
    from jasper.camilla_config_contract import parse_camilla_devices_config
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAMILLA_CHUNKSIZE,
        RING_CAMILLA_TARGET_LEVEL,
    )

    topology, applied = _applied_mono_baseline(tmp_path)
    floor = camilla_floor_for(topology.hardware.device_id)
    assert floor is not None, (
        "this test's whole point is a box whose DAC declares a CamillaDSP floor; "
        f"{topology.hardware.device_id} declares none, so it proves nothing"
    )
    assert floor.chunksize != RING_CAMILLA_CHUNKSIZE
    assert floor.target_level != RING_CAMILLA_TARGET_LEVEL

    ring_yaml, issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )
    assert issues == []
    assert ring_yaml is not None
    devices = parse_camilla_devices_config(ring_yaml)
    assert devices["chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert devices["target_level"] == RING_CAMILLA_TARGET_LEVEL
    assert "  enable_rate_adjust: false" in ring_yaml
    # The ring pair is deliberately OUTSIDE CamillaFloor's own 4x rule (128/128
    # would not construct as a floor): that rule sizes a rate-ADJUSTED
    # resampler's steady-state fill, and the ring graph runs rate_adjust off.
    with pytest.raises(ValueError, match="target_level"):
        type(floor)(
            chunksize=RING_CAMILLA_CHUNKSIZE,
            target_level=RING_CAMILLA_TARGET_LEVEL,
        )

    # CONTROL: the ALSA active lane still takes the box's floor, resolved by the
    # emitter at emit time. A helper that forced ring geometry everywhere would
    # pass every assertion above and silently retune every unarmed box.
    from jasper.active_speaker.runtime_contract import (
        OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )

    alsa_yaml, alsa_issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        playback_device=OUTPUTD_ACTIVE_PLAYBACK_DEVICE,
    )
    assert alsa_issues == []
    assert alsa_yaml is not None
    alsa_devices = parse_camilla_devices_config(alsa_yaml)
    assert alsa_devices["chunksize"] != RING_CAMILLA_CHUNKSIZE
    assert alsa_devices["target_level"] != RING_CAMILLA_TARGET_LEVEL


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
                capture_session_id=f"capture-{role}",
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
                capture_session_id="capture-summed",
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
    # Datasheet says the horn is 25.2 dB hotter; the MEASURED capture says 21 dB.
    # The 4.2 dB gap is a real measured refinement and stays inside
    # MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB — the two frames still agree about
    # what speaker this is, which is what lets the measured value win. A gap
    # BEYOND that tolerance is a different situation and has its own test.
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),  # fc 2000, 25.2 dB gap
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=21.0
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
    # The MEASURED ~21 dB trim is used, not the 25.2 dB datasheet estimate.
    tweeter_trim = payload["corrections"]["tweeter"]["gain_db"]
    assert tweeter_trim == pytest.approx(-21.0, abs=1.5)
    assert abs(tweeter_trim - (-25.2)) > 3.0
    assert payload["corrections"]["woofer"]["gain_db"] == 0.0
    assert payload["corrections_source"]["tweeter"] == "measured"
    assert payload["provisional"] is False
    assert payload["safety"]["positive_gain_allowed"] is False
    codes = {issue["code"] for issue in payload["issues"]}
    assert "driver_gain_derived_from_measurement" in codes
    assert "driver_gain_derived_from_sensitivity" not in codes
    assert "baseline_level_match_provisional" not in codes


def test_measured_trim_far_from_the_datasheet_is_refused_with_both_numbers(
    tmp_path: Path, caplog,
) -> None:
    """PR-L4 item 3(a): the two level frames finally meet.

    The pad-folded datasheet gap and the measured phone level match answer the
    same physical question from independent evidence, and the precedence ladder
    used to silently drop whichever lost. On the 2026-07-27 JTS3 run they were
    ~12 dB apart — the datasheet right, the measurement carrying the frame
    defect PR-L3 later located — and no line of code held both at once.

    Beyond tolerance the measured value is refused (the datasheet rung, the one
    with a physical model behind it, carries the trim) and BOTH numbers reach
    the household copy, because "your measurement and your driver spec disagree
    by 13 dB" is the actionable sentence, not a silently different gain.
    """
    caplog.set_level(logging.WARNING, logger="jasper.active_speaker.baseline_profile")
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),  # 25.2 dB datasheet gap
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    # 12.0 dB measured against a 25.2 dB datasheet: 13.2 dB apart, the shape
    # the forensics found. Well beyond anything datasheet tolerance, pad
    # impedance, and estimator spread can jointly explain.
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=12.0
    )

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    assert payload["corrections_source"]["tweeter"] == "sensitivity"
    assert payload["corrections"]["tweeter"]["gain_db"] == pytest.approx(-25.2, abs=0.1)
    issues = {issue["code"]: issue for issue in payload["issues"]}
    assert "driver_level_frame_disagreement" in issues
    # Both numbers, in the copy — the whole point of the check.
    message = issues["driver_level_frame_disagreement"]["message"]
    assert "-12.0 dB" in message and "-25.2 dB" in message
    assert "13.2 dB apart" in message
    # The refusal is per role and is recorded on the level-match ledger; the
    # woofer's own measured reference (0 dB) is untouched by the tweeter's
    # broken frame, so `applied` stays true and the ledger says what was dropped.
    assert payload["level_match"]["frame_disagreements"] == [
        "tweeter measured -12.0 dB vs datasheet -25.2 dB (13.2 dB apart)"
    ]
    assert payload["level_match"]["frame_tolerance_db"] == (
        MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB
    )
    assert "event=baseline_profile.level_frame_disagreement" in caplog.text


def test_the_two_level_sittings_are_compared_and_disclosed(
    tmp_path: Path, caplog,
) -> None:
    """Ruling S8: two SITTINGS of one level fact are compared, and the gap is
    disclosed with the frames it was taken in.

    A v2 measured candidate carries ``solve_branch_trims``' power-band average
    over the mirrored halves about Fc; the saved phone level-match captures
    carry ``driver_acoustics``' point-at-Fc read of the same handover
    condition. One definition, two captures — so the finding names both
    sittings rather than declaring either number wrong.
    """
    caplog.set_level(logging.WARNING, logger="jasper.active_speaker.baseline_profile")
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    # The phone level match measured a 12 dB gap...
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=12.0
    )
    preset, _issues, _gates = compile_preset_from_crossover_preview(
        topology, dict(preview)
    )
    # ...and the crossover sweep's candidate says 25 dB. 13 dB apart.
    candidate = MeasuredCrossoverCandidate(
        program_id="prog-v2-estimators",
        analysis={"drift_ppm": 3.0, **_MEASURE_EVIDENCE},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": -25.0},
    )

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        measured_candidate=candidate,
        tuning_owner="automatic",
        write=False,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
    )

    notes = payload["level_match"]["sitting_differences"]
    assert notes and "13.0 dB apart" in notes[0]
    assert "crossover sweep -25.0 dB" in notes[0]
    assert "level match -12.0 dB" in notes[0]
    assert "driver_level_sittings_differ" in {
        issue["code"] for issue in payload["issues"]
    }
    assert "event=baseline_profile.level_sittings_differ" in caplog.text
    # The gap is unplaceable without its frames, so both sittings are named.
    frame = payload["level_match"]["sitting_frame"]
    assert frame["crossover_sweep_axis"] == LEVEL_MATCH_AXIS
    assert frame["level_match_sitting"] == "guided_captures"
    # Disclosed, NOT refused: the candidate's own trim still ships.
    assert payload["corrections"]["tweeter"]["gain_db"] == pytest.approx(-25.0)


def test_measured_vs_datasheet_tolerance_clears_its_own_error_budget() -> None:
    """The tolerance's derivation, pinned rather than left in prose: it must
    clear the sum of what CAN honestly differ between the two frames
    (~2 dB datasheet spec + ~2 dB realized pad impedance + ~1.3 dB measured
    frame spread + ~0.5 dB estimator systematic) and stay well under the ~12 dB
    defect it exists to catch."""
    honest_worst_case_db = 2.0 + 2.0 + 1.3 + 0.5
    assert MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB > honest_worst_case_db
    assert MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB < 12.0


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
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=21.0
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
        -21.0, abs=1.5
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
    # #1666: the started event names the candidate's own source-fingerprinted
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
    # #1666: the transaction loads the candidate's own source-fingerprinted
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
        analysis={"drift_ppm": 3.0, "sweeps": ["w", "t", "w"], **_MEASURE_EVIDENCE},
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
    # #1666: candidate lands on a source-fingerprinted sibling, not the literal
    # config_path passed in.
    config_text = Path(payload["config"]["path"]).read_text()
    assert "delay: 0.2500" in config_text
    assert payload["candidate_fingerprint"] is not None


def test_build_baseline_profile_candidate_threads_linearization_outcome(
    tmp_path: Path,
) -> None:
    """Gauge fix (2026-07-24): the single writer's linearization_outcome —
    read straight off ``measured_candidate``, never re-derived — lands on
    the persisted payload's top level, matching "linearization"'s own
    top-level convenience-copy placement."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = MeasuredCrossoverCandidate(
        program_id="prog-v2-1",
        analysis={"drift_ppm": 3.0, "sweeps": ["w", "t", "w"], **_MEASURE_EVIDENCE},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
        linearization_outcome="ineligible_mic_tier",
    )

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=False,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )

    assert payload["linearization_outcome"] == "ineligible_mic_tier"


def test_build_baseline_profile_candidate_linearization_outcome_defaults_empty(
    tmp_path: Path,
) -> None:
    """A measured_candidate that never set linearization_outcome (the
    field's own dataclass default) threads through as "" — same shape
    every pre-gauge-fix / plain trims-only candidate produces."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)
    assert candidate.linearization_outcome == ""

    payload = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements={},
        write=False,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )

    assert payload["linearization_outcome"] == ""


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
        analysis={"drift_ppm": 1.0, **_MEASURE_EVIDENCE},
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
    # #1666: the applied candidate lands on its own source-fingerprinted
    # sibling (what load_config was actually called with); the canonical file at
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
        analysis={"drift_ppm": 4.0, "sweeps": ["w", "t", "w", "t"], **_MEASURE_EVIDENCE},
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
        analysis={"drift_ppm": 2.0, "sweeps": ["w", "t", "w"], **_MEASURE_EVIDENCE},
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
        analysis={"drift_ppm": 2.0, "sweeps": ["w", "t", "w"], **_MEASURE_EVIDENCE},
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


def test_frozen_applied_profile_carries_linearization_outcome_top_level():
    """Gauge fix (2026-07-24): mirrors
    test_frozen_applied_profile_carries_linearization_top_level for the new
    field — this is a field-by-field allowlist, so a candidate saved OVER
    an applied, linearized profile must not silently lose
    linearization_outcome from the retained applied_recomposition_profile
    sidecar (the same Gap 3c bug class "linearization" itself was fixed
    for)."""
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
        "linearization_outcome": "fitted",
        "recomposition_snapshot": {
            "schema_version": 1,
            "linearization": {"woofer": [{"biquad_type": "Peaking"}]},
        },
    }
    from jasper.active_speaker.baseline_profile import _frozen_applied_profile

    frozen = _frozen_applied_profile(saved)
    assert frozen is not None
    assert frozen["linearization_outcome"] == "fitted"


def test_frozen_applied_profile_defaults_linearization_outcome_when_absent():
    """Era-tolerant: a pre-gauge-fix applied dict with no
    "linearization_outcome" key at all must not raise, defaulting to ""."""
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
    assert frozen["linearization_outcome"] == ""


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
        analysis={"drift_ppm": 2.0, "sweeps": ["w", "t", "w"], **_MEASURE_EVIDENCE},
        source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": -2.0},
        linearization=_linearization_payload(),
    )
    plain_candidate = _v2_candidate(preset, tweeter_gain_db=-2.5, delay_us=300.0)
    return (
        topology, draft, preview, state_path, config_path,
        load_config, current_config_path, linearized_candidate, plain_candidate,
    )


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


async def _apply_prior_then_run8(monkeypatch, tmp_path: Path):
    """Apply one profile (the household's pre-existing crossover), then a
    SECOND (the run-8-shaped measured candidate) over it. Returns
    ``(state_path, config_path, load_config, current_config_path,
    prior_payload, run8_payload, retained)`` — ``retained`` is the exact
    frozen ``applied_recomposition_profile`` snapshot ``handle_v2_apply``
    reads its way-back pointer and #1811 offset from at the moment of the
    run-8 apply."""
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
    assert run8_config_path != config_path, "run-8 must land on a source-fingerprinted sibling"
    assert "applied_recomposition_profile" not in run8_payload["profile"]

    return (
        state_path, config_path, load_config, current_config_path,
        prior_payload, run8_payload, retained,
    )



# --- #1666: apply-promotion durability ---------------------------------- #
#
# build_baseline_profile_candidate never writes baseline_config_path()
# directly; every write=True candidate lands on its own source-fingerprinted
# sibling. The canonical name is published ONLY by a post-success promote
# (a byte copy of the just-applied candidate) in _apply_baseline_profile_locked.
# Root cause: the OLD parity check
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
    lands its candidate on a source-fingerprinted sibling -- never writes
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


# ---------- the measured base trim replaces the datasheet prefill ------------


def _with_banked_base_trim(
    topology: OutputTopology,
    research: dict,
    tmp_path: Path,
    monkeypatch,
    *,
    trims: dict[str, float],
    declaration: str | None = None,
) -> dict:
    """``_baseline_payload``, with a base trim banked against this speaker's own
    declaration (or, when ``declaration`` is given, against a different one)."""
    draft = build_design_draft(
        topology,
        driver_research=research,
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    state = tmp_path / "driver_base_trim.json"
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(state))
    dbt.write_base_trim(
        trims_db=trims,
        roles=tuple(trims),
        speaker_group_ids=["mono"],
        declaration_fingerprint=(
            declaration if declaration is not None
            else crossover_preview_fingerprint(preview)
        ),
        trim_source="strict_measured_candidate",
        state_path=state,
    )
    return build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=_measurements(topology, tmp_path),
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=tmp_path / "active_speaker_baseline.yml",
        validate=_valid_config,
        created_at="2026-06-19T12:20:00Z",
    )


def test_banked_base_trim_replaces_the_sensitivity_prefill(
    tmp_path: Path, monkeypatch
) -> None:
    """The incident, inverted. Without a measurement this speaker ships the
    -25.2 dB trim its DE250-shaped datasheet gap implies; with one it ships the
    number a microphone actually read, and the provenance says so."""
    topology = _dual_apple_topology()
    payload = _with_banked_base_trim(
        topology,
        _research_with_sensitivity(),  # 25.2 dB datasheet gap, no measurement
        tmp_path,
        monkeypatch,
        trims={"woofer": 0.0, "tweeter": -19.4},
    )

    assert payload["corrections"]["tweeter"]["gain_db"] == -19.4
    assert payload["corrections"]["woofer"]["gain_db"] == 0.0
    assert payload["corrections_source"]["tweeter"] == "measured"
    assert payload["provisional"] is False
    assert payload["level_match"]["source"] == "banked_base_trim"
    codes = {issue["code"] for issue in payload["issues"]}
    assert "driver_gain_derived_from_measurement" in codes
    assert "driver_gain_derived_from_sensitivity" not in codes
    assert "baseline_level_match_provisional" not in codes


def test_a_base_trim_for_another_declaration_falls_back_and_says_why(
    tmp_path: Path, monkeypatch
) -> None:
    """A speaker whose declaration moved under its banked trim keeps the safe
    datasheet estimate AND is told the measurement no longer applies — a
    silently-dropped trim is indistinguishable from never having measured."""
    topology = _dual_apple_topology()
    payload = _with_banked_base_trim(
        topology,
        _research_with_sensitivity(),
        tmp_path,
        monkeypatch,
        trims={"woofer": 0.0, "tweeter": -19.4},
        declaration="f" * 64,
    )

    assert payload["corrections"]["tweeter"]["gain_db"] == -25.2  # datasheet
    assert payload["corrections_source"]["tweeter"] == "sensitivity"
    assert payload["provisional"] is True
    codes = {issue["code"] for issue in payload["issues"]}
    assert "driver_base_trim_not_applied" in codes
    assert "driver_gain_derived_from_sensitivity" in codes
    assert "driver_gain_derived_from_measurement" not in codes
    message = next(
        issue["message"] for issue in payload["issues"]
        if issue["code"] == "driver_base_trim_not_applied"
    )
    assert dbt.REMEASURE_REMEDIATION in message


def _applied_measured_profile(tmp_path: Path, *, measured: bool) -> tuple:
    """``(preset, preview, applied profile)`` for a box that just applied one
    profile — level-matched by its own measurement, or by the datasheet gap."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    # 21 dB measured against the 25.2 dB datasheet gap is a refinement the two
    # frames still agree on, so the measured trim wins. 12 dB is 13.2 dB apart
    # — beyond MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB — so the measured value
    # is refused per role and the profile ships the datasheet estimate.
    measurements = _acoustic_measurements(
        topology,
        preview,
        tmp_path,
        fc=2000.0,
        tweeter_hotter_db=21.0 if measured else 12.0,
    )
    measurements["summary"]["latest_summed_validations"]["mono"]["acoustic"] = {
        "verdict": "blend_ok",
        "mic_clipping": False,
    }
    candidate = build_baseline_profile_candidate(
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
    preset, _issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    return preset, preview, candidate


@pytest.mark.parametrize("measured", [True, False], ids=["measured", "datasheet"])
def test_the_applied_level_match_is_the_evidence_a_level_matched_walk_reads(
    tmp_path: Path, monkeypatch, measured: bool
) -> None:
    """Ruling S16, and blind-run finding F-1's fix.

    F-1: a box applied a measured level match, reported
    ``corrections_source: measured`` beside ``level_match.applied: true``, and
    still refused every ``--level-matched`` walk
    ``walk_level_match_no_evidence`` — because the resolver those walks ask
    (``measured_level_trims``) reads the banked base trim and the guided
    captures, and the applied candidate's own corrections were a THIRD
    representation neither of them could see. One build, two contradictory
    answers to "has this speaker measured its per-driver level?".

    The apply seam now banks what it applied, so the two answers are one
    answer. The final assertion is the contradiction itself, made mechanical:
    a profile that CLAIMS a measured level match resolves evidence, and one
    that does not, does not.
    """
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(tmp_path / "driver_base_trim.json"))
    preset, preview, candidate = _applied_measured_profile(
        tmp_path, measured=measured
    )
    assert candidate["status"] == "ready_to_apply"
    # Before the apply nothing is banked, and this box has no guided captures
    # of its own to fall back on — exactly the state F-1 was measured in.
    assert dbt.load_base_trim() is None
    assert baseline_profile_mod.measured_level_trims(preset, {}, preview)[0] == {}

    applied = baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    trims, meta = baseline_profile_mod.measured_level_trims(preset, {}, preview)
    claims_measured = (
        applied["level_match"].get("applied") is True
        and set(applied["corrections_source"].values()) == {"measured"}
    )
    assert claims_measured is measured
    assert bool(trims) is claims_measured
    if not measured:
        return
    assert meta["source"] == "banked_base_trim"
    assert meta["base_trim"]["trim_source"] == applied["level_match"]["comparison"]
    assert meta["measured_group_ids"] == applied["automatic_candidate"][
        "measured_group_ids"
    ]
    # EXACT, not approximate. The banked record is the trim the graph is
    # playing; a tolerance here would let the writer round the value it banks
    # and still pass, which is the divergence this artifact exists to close.
    assert trims == {
        role: entry["gain_db"] for role, entry in applied["corrections"].items()
    }


def test_applying_an_unmeasured_profile_clears_a_stale_banked_trim(
    tmp_path: Path, monkeypatch
) -> None:
    """Single ownership points both ways. A banked trim the box has stopped
    playing would let a ``--level-matched`` walk level its graph by numbers
    nothing is applying — the same lie F-1 found, facing the other way."""
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(tmp_path / "driver_base_trim.json"))
    # Both candidates are compiled BEFORE anything is banked: once a measured
    # trim exists the resolver prefers it, so a datasheet-level profile can
    # only be built against an empty bank.
    _preset, _preview, datasheet_candidate = _applied_measured_profile(
        tmp_path / "second", measured=False
    )
    preset, preview, measured_candidate = _applied_measured_profile(
        tmp_path, measured=True
    )
    baseline_profile_mod.persist_applied_baseline_profile(
        measured_candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )
    assert dbt.load_base_trim() is not None

    baseline_profile_mod.persist_applied_baseline_profile(
        datasheet_candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "second" / "applied_profile.json",
    )

    assert dbt.load_base_trim() is None
    assert baseline_profile_mod.measured_level_trims(preset, {}, preview)[0] == {}


def test_newer_guided_captures_replace_the_banked_trim_and_the_receipt_says_so(
    tmp_path: Path, monkeypatch
) -> None:
    """Ruling S20: measurements dispose; the receipt names which measurement.

    A banked base trim for THIS declaration and THESE roles loses to guided
    level-match captures taken AFTER it was banked: the fresh evidence drives
    the profile, the ledger stamps the old record superseded, and the next
    apply re-banks under the new evidence identity."""
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(tmp_path / "driver_base_trim.json"))
    # Banked BEFORE the captures below (their created_at is 12:01/12:02).
    monkeypatch.setattr(dbt, "_utc_now", lambda: "2026-06-19T12:00:45Z")
    dbt.write_base_trim(
        trims_db={"woofer": 0.0, "tweeter": -23.0},
        roles=("woofer", "tweeter"),
        speaker_group_ids=["mono"],
        declaration_fingerprint=crossover_preview_fingerprint(preview),
        trim_source="strict_measured_candidate",
    )
    stale = dbt.load_base_trim()
    assert stale is not None

    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=21.0
    )
    measurements["summary"]["latest_summed_validations"]["mono"]["acoustic"] = {
        "verdict": "blend_ok",
        "mic_clipping": False,
    }
    candidate = build_baseline_profile_candidate(
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

    assert candidate["corrections"]["tweeter"]["gain_db"] == -21.0
    assert candidate["level_match"]["source"] == "guided_captures"
    assert candidate["level_match"]["base_trim"]["status"] == dbt.STATUS_SUPERSEDED

    monkeypatch.setattr(dbt, "_utc_now", lambda: "2026-06-19T12:30:00Z")
    baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    banked = dbt.load_base_trim()
    assert banked is not None
    assert (
        banked["trim_source"],
        banked["measured_at"],
        banked["trims_db"]["tweeter"],
    ) != (
        stale["trim_source"],
        stale["measured_at"],
        stale["trims_db"]["tweeter"],
    )
    assert banked["trim_source"] == candidate["level_match"]["comparison"]
    assert banked["trims_db"] == {"woofer": 0.0, "tweeter": -21.0}


def test_a_frozen_re_persist_re_banks_the_evidence_time_not_the_persist_time(
    tmp_path: Path, monkeypatch
) -> None:
    """Ruling S20 at the write seam. ``measured_at`` means when the evidence
    was measured, so re-persisting a FROZEN candidate (the apply-retry shape:
    the bank is re-written before the idempotent early-return) cannot re-date
    the bank past captures taken after its evidence — those captures still
    supersede on the next resolve."""
    topology = _dual_apple_topology()
    draft = build_design_draft(
        topology,
        driver_research=_research_with_sensitivity(),
        created_at="2026-06-19T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-06-19T12:10:00Z")
    monkeypatch.setenv(dbt.STATE_PATH_ENV, str(tmp_path / "driver_base_trim.json"))
    measurements = _acoustic_measurements(
        topology, preview, tmp_path, fc=2000.0, tweeter_hotter_db=21.0
    )
    measurements["summary"]["latest_summed_validations"]["mono"]["acoustic"] = {
        "verdict": "blend_ok",
        "mic_clipping": False,
    }
    candidate = build_baseline_profile_candidate(
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
    monkeypatch.setattr(dbt, "_utc_now", lambda: "2026-06-19T12:30:00Z")
    baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )
    monkeypatch.setattr(dbt, "_utc_now", lambda: "2026-06-19T13:00:00Z")
    baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    banked = dbt.load_base_trim()
    assert banked is not None
    # The newest capture that fed the trim — not either persist's wall clock.
    assert banked["measured_at"] == "2026-06-19T12:02:00Z"

    newer = deepcopy(measurements)
    newer["latest_by_target"]["mono:woofer"]["created_at"] = "2026-06-19T12:40:00Z"
    newer["latest_by_target"]["mono:tweeter"]["created_at"] = "2026-06-19T12:41:00Z"
    relevelled = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=newer,
        write=True,
        state_path=tmp_path / "baseline_b.json",
        config_path=tmp_path / "baseline_b.yml",
        validate=_valid_config,
        created_at="2026-06-19T13:10:00Z",
    )
    assert relevelled["level_match"]["source"] == "guided_captures"
    assert relevelled["level_match"]["base_trim"]["status"] == dbt.STATUS_SUPERSEDED


def test_the_compose_clock_never_enters_the_candidate_fingerprint(
    tmp_path: Path,
) -> None:
    """Two composes of the same inputs are one candidate, whatever the clock
    did between them. The apply seams compare ``baseline_candidate_fingerprint``
    across two composes (``handle_v2_apply``'s review compose against
    ``apply_baseline_profile``'s recompose, and the idempotent re-apply against
    the persisted profile), while the measured-candidate arm stamps its compose
    instant into ``level_match.newest_capture_at`` for the bank's evidence
    clock — a second boundary between composes must not turn that stamp into
    ``baseline_candidate_fingerprint_mismatch``."""
    topology = _dual_apple_topology()
    draft = _draft(topology)
    preview = build_crossover_preview(draft, created_at="2026-07-18T12:10:00Z")
    preset, issues, _gates = compile_preset_from_crossover_preview(topology, preview)
    assert preset is not None, issues
    candidate = _v2_candidate(preset)

    def compose(created_at: str) -> dict:
        return build_baseline_profile_candidate(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements={},
            write=False,
            state_path=tmp_path / "baseline_profile.json",
            config_path=tmp_path / "active_speaker_baseline.yml",
            validate=_valid_config,
            tuning_owner="automatic",
            measured_candidate=candidate,
            created_at=created_at,
        )

    reviewed = compose("2026-07-18T12:20:00Z")
    recomposed = compose("2026-07-18T12:20:01Z")

    # The recency stamp itself still tracks each compose's clock (the bank
    # seam reads it off whichever compose gets persisted)…
    assert (
        reviewed["recomposition_snapshot"]["level_match"]["newest_capture_at"]
        == "2026-07-18T12:20:00Z"
    )
    assert (
        recomposed["recomposition_snapshot"]["level_match"]["newest_capture_at"]
        == "2026-07-18T12:20:01Z"
    )
    # …and candidate identity does not move with it.
    assert reviewed["candidate_fingerprint"] == recomposed["candidate_fingerprint"]


def test_the_banked_trim_carries_its_own_source_into_the_level_match_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    """Ruling S16 (d): a receipt reading the banked trim still names the
    measurement behind it. The record stamps WHICH evidence the apply levelled
    by, and the profile's own ``level_match`` repeats that rather than minting
    a second word for it."""
    payload = _with_banked_base_trim(
        _dual_apple_topology(),
        _research_with_sensitivity(),
        tmp_path,
        monkeypatch,
        trims={"woofer": 0.0, "tweeter": -19.4},
    )
    level_match = payload["level_match"]
    assert level_match["source"] == "banked_base_trim"
    assert level_match["base_trim"]["trim_source"] == "strict_measured_candidate"
    assert level_match["comparison"] == "strict_measured_candidate"


def test_a_banked_trim_far_from_the_datasheet_still_meets_the_existing_frame_check(
    tmp_path: Path, monkeypatch
) -> None:
    """The interaction the base trim inherits, pinned so its change is visible.

    ``MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB`` discards a measured trim more
    than 6 dB from the datasheet one, and a banked base trim enters that ladder
    at exactly the rung the guided level match did — so a driver whose real
    in-situ level is far from its datasheet's (a compression driver rated on a
    different horn: the case this whole step exists for) is still refused today.
    Retiring or one-siding that check is deliberately a separate change with its
    own review; this test is what will go red when it lands.
    """
    topology = _dual_apple_topology()
    payload = _with_banked_base_trim(
        topology,
        _research_with_sensitivity(),  # 25.2 dB datasheet gap
        tmp_path,
        monkeypatch,
        trims={"woofer": 0.0, "tweeter": -6.0},  # 19.2 dB from the datasheet
    )

    assert payload["corrections"]["tweeter"]["gain_db"] == -25.2  # datasheet
    assert payload["corrections_source"]["tweeter"] == "sensitivity"
    codes = {issue["code"] for issue in payload["issues"]}
    assert "driver_level_frame_disagreement" in codes
    assert payload["level_match"]["frame_tolerance_db"] == (
        MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB
    )


def _bank_events(caplog) -> list[dict[str, str]]:
    """The base-trim seam's events as FIELDS, never as prose.

    ``log_event`` renders logfmt, so one ``shlex.split`` recovers the
    structured pairs a consumer actually reads — the result and reason codes —
    without pinning the wording of any detail string.
    """
    out: list[dict[str, str]] = []
    for message in _events(caplog, "dsp.baseline_base_trim_banked"):
        fields: dict[str, str] = {}
        for token in shlex.split(message):
            key, _, value = token.partition("=")
            fields[key] = value
        out.append(fields)
    return out


def _applied_with_sources(
    tmp_path: Path, sources: dict[str, str]
) -> dict[str, object]:
    """A really-applied measured candidate, re-sourced one role at a time.

    Built by the real compiler and re-fingerprinted, so it still passes every
    guard ``persist_applied_baseline_profile`` puts in front of the seam —
    only the per-role evidence differs between arms.
    """
    _preset, _preview, candidate = _applied_measured_profile(tmp_path, measured=True)
    candidate = deepcopy(candidate)
    candidate["corrections_source"] = dict(sources)
    candidate["candidate_fingerprint"] = baseline_candidate_fingerprint(candidate)
    return candidate


@pytest.mark.parametrize(
    "sources, banked, result, reason",
    [
        (
            {"woofer": "measured", "tweeter": "measured"},
            True,
            "ok",
            None,
        ),
        (
            {"woofer": "measured", "tweeter": "operator_pinned"},
            True,
            "left_standing",
            "partly_measured",
        ),
        (
            {"woofer": "sensitivity", "tweeter": "sensitivity"},
            False,
            "cleared",
            "unmeasured",
        ),
    ],
    ids=["all-measured", "one-operator-pin", "none-measured"],
)
def test_a_partly_pinned_profile_neither_banks_nor_clears(
    tmp_path: Path, caplog, sources, banked, result, reason
) -> None:
    """Three answers, not two.

    ``_bank_applied_base_trim`` required EVERY role to be sourced ``measured``
    while ``crossover_contract._snapshot_owner`` — the predicate the seam's own
    docstring claims to mirror — needs only ANY. A candidate with one
    operator-pinned driver therefore read as ``automatic`` to the contract and
    as unmeasured to the bank, and the apply DESTROYED a good banked record on
    the strength of a single pin. A pin does not un-measure a speaker: the
    prior full measurement is still the best evidence anyone has, so the
    middle arm leaves it alone and says so.

    The third arm is unchanged and deliberately so: a role that fell back to
    the DATASHEET is weaker evidence, not a pin, and still clears.
    """
    caplog.set_level(logging.INFO, logger=_BASELINE_LOGGER)
    candidate = _applied_with_sources(tmp_path, sources)
    # A record from an earlier, fully measured apply is standing before each
    # arm runs -- the arms differ only in what they do to it.
    baseline_profile_mod.persist_applied_baseline_profile(
        _applied_with_sources(
            tmp_path / "prior", {"woofer": "measured", "tweeter": "measured"}
        ),
        apply_state={"result": "success"},
        state_path=tmp_path / "prior_applied.json",
    )
    assert dbt.load_base_trim() is not None
    caplog.clear()

    baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    assert (dbt.load_base_trim() is not None) is banked
    events = _bank_events(caplog)
    assert [event["result"] for event in events] == [result]
    if reason is not None:
        assert events[0]["reason"] == reason


@pytest.mark.parametrize(
    "named_chain",
    [
        pytest.param("c" * 64, id="a_candidate_resolved_it"),
        pytest.param(None, id="the_guided_captures_did"),
    ],
)
def test_the_banked_trim_names_the_chain_it_was_co_fitted_with(
    tmp_path: Path, named_chain
) -> None:
    """#3479: the seam banks the FRAME beside the number.

    A trim is degenerate with the chain it was resolved against, so the apply
    passes the resolving candidate's own fingerprint — already on the profile's
    source block — through to the record, and it reaches the level-match ledger
    every downstream reader looks at. A profile the guided captures levelled
    names no candidate and banks no frame, which is a different fact from
    naming the bare one.
    """
    candidate = _applied_with_sources(
        tmp_path, {"woofer": "measured", "tweeter": "measured"}
    )
    source = dict(candidate["source"])
    if named_chain is None:
        source.pop("measured_candidate_fingerprint", None)
    else:
        source["measured_candidate_fingerprint"] = named_chain
    candidate["source"] = source
    candidate["candidate_fingerprint"] = baseline_candidate_fingerprint(candidate)

    baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    record = dbt.load_base_trim()
    assert record is not None
    assert record["chain_fingerprint"] == named_chain
    _trims, meta = dbt.banked_base_trims(
        record["declaration_fingerprint"], record["roles"]
    )
    assert meta["chain_fingerprint"] == named_chain


def test_a_measured_profile_that_cannot_be_banked_drops_the_stale_record(
    tmp_path: Path, caplog
) -> None:
    """Absent beats wrong.

    A refused write left the PREVIOUS apply's record standing, so the box went
    on levelling a ``--level-matched`` walk by numbers describing a graph it
    had stopped playing. The resolver's fallback (guided captures, then the
    datasheet) is conservative; a stale record is not.
    """
    caplog.set_level(logging.INFO, logger=_BASELINE_LOGGER)
    baseline_profile_mod.persist_applied_baseline_profile(
        _applied_with_sources(tmp_path, {"woofer": "measured", "tweeter": "measured"}),
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )
    assert dbt.load_base_trim() is not None
    caplog.clear()

    doomed = _applied_with_sources(
        tmp_path / "next", {"woofer": "measured", "tweeter": "measured"}
    )
    # The declaration the record would be keyed by is unreadable, so the
    # writer refuses -- the seam must not leave the prior record behind.
    doomed["source"] = {**doomed["source"], "crossover_preview_fingerprint": ""}
    doomed["candidate_fingerprint"] = baseline_candidate_fingerprint(doomed)
    baseline_profile_mod.persist_applied_baseline_profile(
        doomed,
        apply_state={"result": "success"},
        state_path=tmp_path / "next_applied.json",
    )

    assert dbt.load_base_trim() is None
    results = [event["result"] for event in _bank_events(caplog)]
    assert results == ["failed", "cleared"]
    assert _bank_events(caplog)[0]["reason"] == dbt.REFUSE_NO_DECLARATION
    assert _bank_events(caplog)[1]["reason"] == dbt.BANK_WRITE_REFUSED


def test_a_malformed_correction_entry_refuses_instead_of_escaping(
    tmp_path: Path, caplog
) -> None:
    """The seam promises never to fail a successful apply, and broke it.

    ``float((entry or {}).get("gain_db"))`` raises AttributeError on a
    correction entry that is not a Mapping, and AttributeError was not in the
    seam's except tuple -- so a malformed entry propagated out of
    ``persist_applied_baseline_profile`` and turned an apply whose graph was
    already live and read back into a failure.
    """
    caplog.set_level(logging.INFO, logger=_BASELINE_LOGGER)
    candidate = _applied_with_sources(
        tmp_path, {"woofer": "measured", "tweeter": "measured"}
    )
    candidate["corrections"] = {**candidate["corrections"], "tweeter": "-12.0"}
    candidate["candidate_fingerprint"] = baseline_candidate_fingerprint(candidate)

    payload = baseline_profile_mod.persist_applied_baseline_profile(
        candidate,
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )

    assert payload["status"] == "applied"
    events = _bank_events(caplog)
    assert [event["result"] for event in events] == ["left_standing"]
    assert events[0]["reason"] == dbt.BANK_CORRECTION_ENTRY_UNREADABLE


def test_the_two_unreadable_guards_no_longer_share_one_slug(
    tmp_path: Path, caplog
) -> None:
    """``profile_unreadable`` meant two unrelated things -- a profile naming no
    corrections at all, and a measured profile whose readiness block was not
    kept -- so an operator reading the reason could not tell which had
    happened, and the two arms now behave differently besides."""
    caplog.set_level(logging.INFO, logger=_BASELINE_LOGGER)
    base = _applied_with_sources(
        tmp_path, {"woofer": "measured", "tweeter": "measured"}
    )

    no_corrections = deepcopy(base)
    no_corrections["corrections"] = "not-a-mapping"
    no_corrections["candidate_fingerprint"] = baseline_candidate_fingerprint(
        no_corrections
    )
    baseline_profile_mod.persist_applied_baseline_profile(
        no_corrections,
        apply_state={"result": "success"},
        state_path=tmp_path / "a.json",
    )
    no_readiness = deepcopy(base)
    no_readiness.pop("automatic_candidate", None)
    no_readiness["candidate_fingerprint"] = baseline_candidate_fingerprint(no_readiness)
    baseline_profile_mod.persist_applied_baseline_profile(
        no_readiness,
        apply_state={"result": "success"},
        state_path=tmp_path / "b.json",
    )

    reasons = [event["reason"] for event in _bank_events(caplog)]
    assert reasons == [
        dbt.BANK_CORRECTIONS_UNREADABLE,
        dbt.BANK_READINESS_UNREADABLE,
    ]


def test_a_follower_domain_graph_never_touches_the_solo_base_trim(
    tmp_path: Path,
) -> None:
    """A driver-domain candidate is a wireless follower's Layer-A-only graph.
    It has no solo lineage, and ``build_baseline_profile_candidate`` already
    excludes it from the solo artifacts for that reason. This artifact is one
    of those, so a consolidation that ever routes such a graph through the
    apply seam must not be able to clear a measurement it knows nothing about.
    """
    baseline_profile_mod.persist_applied_baseline_profile(
        _applied_with_sources(tmp_path, {"woofer": "measured", "tweeter": "measured"}),
        apply_state={"result": "success"},
        state_path=tmp_path / "applied_profile.json",
    )
    banked = deepcopy(dbt.load_base_trim())
    assert banked is not None

    follower = _applied_with_sources(
        tmp_path / "follower", {"woofer": "sensitivity", "tweeter": "sensitivity"}
    )
    follower["recomposition_snapshot"] = {
        **follower["recomposition_snapshot"], "domain": "driver",
    }
    follower["candidate_fingerprint"] = baseline_candidate_fingerprint(follower)
    baseline_profile_mod.persist_applied_baseline_profile(
        follower,
        apply_state={"result": "success"},
        state_path=tmp_path / "follower_applied.json",
    )

    assert dbt.load_base_trim() == banked
