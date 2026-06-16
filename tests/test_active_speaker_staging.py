from __future__ import annotations

from pathlib import Path

import jasper.active_speaker.staging as staging_mod
from jasper.active_speaker import (
    STAGED_STARTUP_CONFIG_KIND,
    build_crossover_preview,
    load_active_speaker_preset,
    load_staged_startup_config,
    stage_protected_startup_config,
)
from jasper.active_speaker.design_draft import DRIVER_RESEARCH_KIND, build_design_draft
from jasper.camilla_config_contract import ACTIVE_OUTPUTD_PLAYBACK_DEVICE
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
from jasper.output_hardware import DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology(*, protection_status: str = "present") -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono cabinet",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
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
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": protection_status,
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })


def _dual_apple_topology(*, protection_status: str = "present") -> OutputTopology:
    raw = _topology(protection_status=protection_status).to_dict()
    raw["hardware"] = {
        "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "apple_dac_1",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "apple_dac_2",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "physical_output_indexes": [2, 3],
            },
        ],
    }
    return OutputTopology.from_mapping(raw)


def _three_way_topology(*, protection_status: str = "present") -> OutputTopology:
    raw = _topology(protection_status=protection_status).to_dict()
    raw["topology_id"] = "bench_mono_3way"
    raw["speaker_groups"][0]["mode"] = "active_3_way"
    raw["speaker_groups"][0]["channels"] = [
        {
            "role": "woofer",
            "physical_output_index": 0,
            "identity_verified": True,
        },
        {
            "role": "mid",
            "physical_output_index": 1,
            "identity_verified": True,
        },
        {
            "role": "tweeter",
            "physical_output_index": 2,
            "identity_verified": True,
            "startup_muted": True,
            "protection_required": True,
            "protection_status": protection_status,
        },
    ]
    return OutputTopology.from_mapping(raw)


def _topology_with_subwoofer() -> OutputTopology:
    raw = _topology().to_dict()
    raw["topology_id"] = "bench_mono_with_sub"
    raw["speaker_groups"].append({
        "id": "sub",
        "label": "Bench subwoofer",
        "kind": "subwoofer",
        "mode": "subwoofer",
        "channels": [
            {
                "role": "subwoofer",
                "physical_output_index": 2,
                "identity_verified": True,
            }
        ],
    })
    raw["routing"]["subwoofer_group_ids"] = ["sub"]
    return OutputTopology.from_mapping(raw)


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
                    "protection_status": "present",
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
                    "protection_status": "present",
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


def _driver_research(
    *,
    frequency_hz: float = 2500,
    way_count: int = 2,
) -> dict:
    drivers = [
        {
            "role": "woofer",
            "manufacturer": "Dayton Audio",
            "model": "Epique E150HE-44",
            "usable_frequency_range_hz": [45, 5000],
            "recommended_lowpass_hz": frequency_hz,
            "sources": ["https://example.test/woofer"],
        },
        {
            "role": "tweeter",
            "manufacturer": "Eminence",
            "model": "F110M-8",
            "recommended_highpass_hz": frequency_hz,
            "do_not_test_below_hz": 1200,
            "sources": ["https://example.test/tweeter"],
        },
    ]
    candidates = [
        {
            "between_roles": ["woofer", "tweeter"],
            "frequency_hz": frequency_hz,
            "filter_type": "Linkwitz-Riley",
            "slope_db_per_octave": 24,
            "confidence": "medium",
        }
    ]
    if way_count == 3:
        drivers.insert(1, {
            "role": "mid",
            "manufacturer": "Example",
            "model": "Mid driver",
            "usable_frequency_range_hz": [250, 5000],
            "recommended_highpass_hz": 450,
            "recommended_lowpass_hz": frequency_hz,
            "sources": ["https://example.test/mid"],
        })
        candidates = [
            {
                "between_roles": ["woofer", "mid"],
                "frequency_hz": 450,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            },
            {
                "between_roles": ["mid", "tweeter"],
                "frequency_hz": frequency_hz,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            },
        ]
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": drivers,
        "crossover_candidates": candidates,
    }


def _crossover_preview(
    topology: OutputTopology,
    *,
    frequency_hz: float = 2500,
    way_count: int = 2,
) -> dict:
    return build_crossover_preview(
        build_design_draft(
            topology,
            driver_research=_driver_research(
                frequency_hz=frequency_hz,
                way_count=way_count,
            ),
            created_at="2026-06-10T12:00:00Z",
        ),
        created_at="2026-06-10T12:30:00Z",
    )


def _valid_config(path: str | Path) -> CamillaConfigValidationResult:
    return CamillaConfigValidationResult(
        status=ValidationStatus.VALID,
        path=str(path),
    )


def test_default_active_speaker_preset_is_epique_f110m_safe_bringup() -> None:
    preset = load_active_speaker_preset()

    assert preset.preset_id == "epique-e150he44-eminence-f110m8-safe-v1"
    assert preset.name == "Dayton Epique E150HE-44 + Eminence F110M-8 safe bring-up"
    assert preset.crossover_regions[0].fc_hz == 2500
    assert preset.safety.max_commissioning_level_db_spl == 80


def test_stage_protected_startup_config_writes_muted_candidate(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"

    payload = stage_protected_startup_config(
        _topology(),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    text = out.read_text(encoding="utf-8")
    loaded = load_staged_startup_config(metadata_path=meta)

    assert payload["kind"] == STAGED_STARTUP_CONFIG_KIND
    assert payload["status"] == "staged"
    assert payload["preset"]["preset_id"] == "epique-e150he44-eminence-f110m8-safe-v1"
    assert payload["config"]["playback_device"] == "hw:CARD=DAC8,DEV=0"
    assert payload["config"]["playback_device_source"] == "topology_direct_dac"
    assert payload["config"]["playback_channels"] == 2
    assert payload["config"]["validation"]["status"] == "valid"
    assert payload["config"]["tweeter_protective_highpass_hz"] == 5000
    assert payload["load"]["load_allowed"] is False
    assert payload["load"]["load_gate"] == "startup_load_preflight_required"
    assert payload["issues"] == []
    assert "preset_id=epique-e150he44-eminence-f110m8-safe-v1" in text
    assert "split_active_2way" in text
    assert "as_tweeter_protective_hp" in text
    assert "freq: 5000.0000" in text
    assert "mute: true" in text
    assert loaded["status"] == "staged"


def test_stage_protected_startup_config_uses_crossover_preview_frequency(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    preview = _crossover_preview(_topology(), frequency_hz=3200)

    payload = stage_protected_startup_config(
        _topology(),
        crossover_preview=preview,
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    text = out.read_text(encoding="utf-8")

    assert payload["status"] == "staged"
    assert payload["preset"]["source"]["mode"] == "crossover_preview"
    assert payload["preset"]["preset_id"] == "preview-bench_mono-2way"
    assert payload["config"]["tweeter_protective_highpass_hz"] == 6400
    assert "freq: 3200.0000" in text
    assert "freq: 6400.0000" in text


def test_stage_protected_startup_config_blocks_unready_crossover_preview(
    tmp_path: Path,
) -> None:
    preview = _crossover_preview(_topology())
    preview["status"] = "stale"
    preview["permissions"]["may_prepare_protected_startup_config"] = False

    payload = stage_protected_startup_config(
        _topology(),
        crossover_preview=preview,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "blocked"
    assert "crossover_preview_not_ready" in {
        issue["code"] for issue in payload["issues"]
    }


def test_stage_protected_startup_config_blocks_subwoofer_topology_until_supported(
    tmp_path: Path,
) -> None:
    topology = _topology_with_subwoofer()
    preview = _crossover_preview(topology)
    out = tmp_path / "active_staged.yml"

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    subwoofer_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "subwoofer_startup_staging_scope"
    )

    assert payload["status"] == "blocked"
    assert out.exists() is False
    assert subwoofer_gate["passed"] is False
    assert "subwoofer_staging_not_supported" in {
        issue["code"] for issue in payload["issues"]
    }


def test_stage_protected_startup_config_supports_active_three_way_preview(
    tmp_path: Path,
) -> None:
    topology = _three_way_topology()
    preview = _crossover_preview(topology, frequency_hz=2800, way_count=3)
    out = tmp_path / "active_staged.yml"

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    text = out.read_text(encoding="utf-8")

    assert payload["status"] == "staged"
    assert payload["preset"]["way_count"] == 3
    assert payload["config"]["playback_channels"] == 3
    assert payload["config"]["tweeter_protective_highpass_hz"] == 5600
    assert "split_active_3way" in text
    assert "freq: 450.0000" in text
    assert "freq: 2800.0000" in text
    assert "freq: 5600.0000" in text


def test_stage_protected_startup_config_supports_stereo_three_way_on_dac8x(
    tmp_path: Path,
) -> None:
    topology = _stereo_three_way_topology()
    preview = _crossover_preview(topology, frequency_hz=2800, way_count=3)

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
    )

    assert payload["status"] == "staged"
    assert payload["config"]["playback_device"] == "hw:CARD=DAC8,DEV=0"
    assert payload["config"]["playback_device_source"] == "topology_direct_dac"
    assert payload["config"]["playback_channels"] == 6
    capacity_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "active_playback_route_capacity"
    )
    assert capacity_gate["passed"] is True


def test_stage_protected_startup_config_preview_honors_saved_role_mapping(
    tmp_path: Path,
) -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][0]["physical_output_index"] = 1
    raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = 0
    topology = OutputTopology.from_mapping(raw)
    preview = _crossover_preview(topology)

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    role_order_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "active_output_role_order"
    )

    assert payload["status"] == "staged"
    assert role_order_gate["passed"] is True
    assert role_order_gate["message"] == (
        "Preview-derived DSP will follow the saved output role mapping"
    )


def test_stage_protected_startup_config_uses_outputd_active_lane_for_dual_apple(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"

    payload = stage_protected_startup_config(
        _dual_apple_topology(),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "staged"
    assert payload["config"]["playback_device"] == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"
    assert f'device: "{ACTIVE_OUTPUTD_PLAYBACK_DEVICE}"' in out.read_text(
        encoding="utf-8"
    )


def test_stage_protected_startup_config_blocks_missing_tweeter_protection(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"

    payload = stage_protected_startup_config(
        _topology(protection_status="required_missing"),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "blocked"
    assert out.exists() is False
    assert "tweeter_protection_required" in {
        issue["code"] for issue in payload["issues"]
    }
    assert payload["config"]["validation"]["status"] == "skipped"


def test_stage_protected_startup_config_allows_software_guard_request_no_load_candidate(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"

    payload = stage_protected_startup_config(
        _topology(protection_status="software_guard_requested"),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    text = out.read_text(encoding="utf-8")
    loaded = load_staged_startup_config(metadata_path=meta)
    codes = {issue["code"]: issue["severity"] for issue in payload["issues"]}
    guard_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "software_tweeter_guard_evidence"
    )

    assert payload["status"] == "staged"
    assert payload["load"]["load_allowed"] is False
    assert payload["software_guard"]["passed"] is True
    assert payload["software_guard"]["no_load"] is True
    assert payload["software_guard"]["no_playback"] is True
    assert all(payload["software_guard"]["checks"].values())
    assert guard_gate["passed"] is True
    assert codes == {"software_tweeter_guard_requested": "warning"}
    assert "as_tweeter_protective_hp" in text
    assert "as_tweeter_startup_mute" in text
    assert "as_tweeter_startup_limiter" in text
    assert loaded["status"] == "staged"
    assert loaded["software_guard"]["passed"] is True


def test_stage_protected_startup_config_blocks_incomplete_software_guard_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original_emit = staging_mod.emit_active_speaker_startup_config

    def corrupt_tweeter_mute(*args, **kwargs):
        text = original_emit(*args, **kwargs)
        text = text.replace(
            (
                "  as_tweeter_startup_mute:\n"
                "    type: Gain\n"
                "    parameters: { gain: -120.0000, inverted: false, mute: true }"
            ),
            (
                "  as_tweeter_startup_mute:\n"
                "    type: Gain\n"
                "    parameters: { gain: -120.0000, inverted: false, mute: false }"
            ),
        )
        out_path = kwargs.get("out_path")
        if out_path is not None:
            Path(out_path).write_text(text, encoding="utf-8")
        return text

    monkeypatch.setattr(
        staging_mod,
        "emit_active_speaker_startup_config",
        corrupt_tweeter_mute,
    )
    payload = stage_protected_startup_config(
        _topology(protection_status="software_guard_requested"),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "blocked"
    assert payload["software_guard"]["passed"] is False
    assert payload["software_guard"]["checks"]["startup_muted"] is False
    assert "software_tweeter_guard_incomplete" in {
        issue["code"] for issue in payload["issues"]
    }


def test_stage_protected_startup_config_blocks_noncontiguous_outputs(
    tmp_path: Path,
) -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = 3
    topology = OutputTopology.from_mapping(raw)

    payload = stage_protected_startup_config(
        topology,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "blocked"
    assert "active_outputs_must_be_contiguous" in {
        issue["code"] for issue in payload["issues"]
    }


def test_stage_protected_startup_config_blocks_swapped_role_outputs(
    tmp_path: Path,
) -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][0]["physical_output_index"] = 1
    raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = 0
    topology = OutputTopology.from_mapping(raw)

    payload = stage_protected_startup_config(
        topology,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    role_order_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "active_output_role_order"
    )

    assert payload["status"] == "blocked"
    assert "active_outputs_must_match_role_order" in {
        issue["code"] for issue in payload["issues"]
    }
    assert role_order_gate["passed"] is False
    assert "woofer on DAC output 1" in role_order_gate["message"]
