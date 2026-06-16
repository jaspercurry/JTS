from __future__ import annotations

from pathlib import Path

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
    topology = _topology()
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
