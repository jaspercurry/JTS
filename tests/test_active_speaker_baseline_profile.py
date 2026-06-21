# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from jasper.active_speaker.baseline_profile import (
    apply_baseline_profile,
    build_baseline_profile_candidate,
)
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.design_draft import DRIVER_RESEARCH_KIND, build_design_draft
from jasper.active_speaker.measurement import (
    load_measurement_state,
    record_driver_measurement,
    record_summed_test_artifact,
    record_summed_validation,
)
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
from jasper.output_hardware import DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology(
    *,
    tweeter_output: int = 1,
    tweeter_verified: bool = True,
    device_id: str = "hifiberry_dac8x",
    device_label: str = "HiFiBerry DAC8x",
    physical_output_count: int = 8,
    card_id: str = "DAC8",
) -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": device_id,
            "device_label": device_label,
            "physical_output_count": physical_output_count,
            "card_id": card_id,
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": tweeter_output,
                        "identity_verified": tweeter_verified,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })


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


def _research(*, tweeter_gain_db: float = -18.5) -> dict:
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [
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
        ],
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


def _draft(topology: OutputTopology, *, tweeter_gain_db: float = -18.5) -> dict:
    return build_design_draft(
        topology,
        driver_research=_research(tweeter_gain_db=tweeter_gain_db),
        created_at="2026-06-14T12:00:00Z",
    )


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
    yaml = config_path.read_text(encoding="utf-8")

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
    assert "as_tweeter_baseline_limiter" in yaml


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
        return payload, config_path.read_text(encoding="utf-8")

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
    yaml = config_path.read_text(encoding="utf-8")

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
    assert calls == [str(tmp_path / "active_speaker_baseline.yml")]


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
    build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=True,
        state_path=tmp_path / "baseline_profile.json",
        config_path=config_path,
        validate=_valid_config,
    )
    durable_yaml = config_path.read_text(encoding="utf-8")

    flat_yaml, flat_issues = recompose_baseline_yaml(
        topology,
        crossover_preview=preview,
        measurements=measurements,
    )
    assert flat_issues == []
    assert flat_yaml == durable_yaml


def test_recompose_baseline_yaml_folds_preference_eq_and_stays_approved(
    tmp_path: Path,
) -> None:
    # The keystone (invariant 2), end-to-end through the recompose seam: a
    # +6 dB preference (a +4 dB highshelf -- a SHELF, the conservative
    # boost-sum case -- plus a +2 dB peak) folds into the single
    # active_baseline_headroom gain (12 -> 18 dB) and rides PRE-SPLIT, and the
    # emitted graph still re-proves as GRAPH_APPROVED_ACTIVE_RUNTIME. Folding EQ
    # never breaks the protection contract.
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

    # invariant 4 (emitter-side): the headroom gain is folded by the worst-case
    # additive boost (sum of positive shelf+peak gains = 6 dB), keeping it
    # non-positive -- 12 dB baseline headroom -> 18 dB attenuation.
    match = re.search(
        r"active_baseline_headroom:\n\s+type: Gain\n\s+parameters: \{ gain: (-?\d+\.\d+)",
        eq_yaml,
    )
    assert match is not None
    assert float(match.group(1)) == -18.0

    # invariant 5: the preference filter step is wired on the program channels
    # strictly BEFORE the split mixer.
    pipeline = eq_yaml[eq_yaml.index("\npipeline:"):]
    pref_idx = pipeline.index("pref_hs, pref_pk")
    mixer_idx = pipeline.index("type: Mixer")
    assert pref_idx < mixer_idx

    # invariant 2 (keystone): the protection contract still holds.
    graph = classify_camilla_graph(topology=topology, text=eq_yaml)
    assert graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    assert graph.allowed is True

    # output_trim_db (manual headroom + loudness match) threads through recompose
    # and folds into the SAME headroom gain (12 baseline + 6 boost + 4 trim = 22),
    # so the active EQ apply honours the household's loudness setting; still APPROVED.
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
    assert trim_match is not None and float(trim_match.group(1)) == -22.0
    assert classify_camilla_graph(topology=topology, text=trimmed_yaml).allowed is True


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
    from jasper.correction import sweep as sweep_mod

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
    from jasper.active_speaker.staging import compile_preset_from_crossover_preview

    preset, issues, _gates = compile_preset_from_crossover_preview(topology, dict(preview))
    assert preset is not None, issues
    state_path = tmp_path / "measurements.json"

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
        driver_research=_research_with_sensitivity(tweeter_gain_db=-15.0),
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
    assert payload["corrections_source"]["tweeter"] == "explicit"
    assert payload["provisional"] is False
    assert payload["level_match"]["skipped_reason"] == "explicit_gain"
    codes = {issue["code"] for issue in payload["issues"]}
    assert "driver_gain_derived_from_measurement" not in codes
    assert "driver_gain_derived_from_sensitivity" not in codes
