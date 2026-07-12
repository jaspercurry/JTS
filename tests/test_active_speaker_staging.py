# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

import yaml as yaml_lib

import jasper.active_speaker.staging as staging_mod
from jasper.active_speaker import (
    STAGED_STARTUP_CONFIG_KIND,
    ActiveSpeakerPreset,
    build_crossover_preview,
    emit_active_speaker_commissioning_config,
    load_active_speaker_preset,
    load_staged_startup_config,
    stage_protected_startup_config,
)
from jasper.active_speaker.design_draft import DRIVER_RESEARCH_KIND, build_design_draft
from jasper.active_speaker.path_safety import _startup_muted_by_candidate
from jasper.camilla_config_contract import ACTIVE_OUTPUTD_PLAYBACK_DEVICE
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
from jasper.output_hardware import DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology

# Canonical preset fixtures (stereo 2-way: tweeters on physical outputs 1 and 3).
from tests.test_active_speaker_profile import _two_way_preset


def _topology(*, protection_status: str = "present") -> OutputTopology:
    return mono_output_topology(protection_status=protection_status)


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


def _stereo_two_way_topology() -> OutputTopology:
    raw = _topology().to_dict()
    raw["topology_id"] = "bench_stereo_2way"
    raw["speaker_groups"] = [
        {
            "id": "left",
            "label": "Left speaker",
            "kind": "left",
            "mode": "active_2_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
                {
                    "role": "tweeter",
                    "physical_output_index": 1,
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
            "mode": "active_2_way",
            "channels": [
                {"role": "woofer", "physical_output_index": 2, "identity_verified": True},
                {
                    "role": "tweeter",
                    "physical_output_index": 3,
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
    with_subwoofer: bool = False,
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
    if with_subwoofer:
        drivers.append({
            "role": "subwoofer",
            "manufacturer": "Example",
            "model": "Sub driver",
            "usable_frequency_range_hz": [20, 200],
            "recommended_lowpass_hz": 80,
            "sources": ["https://example.test/sub"],
        })
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
    with_subwoofer: bool = False,
) -> dict:
    return build_crossover_preview(
        build_design_draft(
            topology,
            driver_research=_driver_research(
                frequency_hz=frequency_hz,
                way_count=way_count,
                with_subwoofer=with_subwoofer,
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
    # Stage 2: the DAC8x declares an active outputd lane, so staging resolves to
    # that lane (not a direct-DAC route) — staging never silently defaults to
    # hw:<card>,0 on outputd-owned hardware.
    assert payload["config"]["playback_device"] == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"
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


def test_compile_preset_from_crossover_preview_sets_polarity_and_delay() -> None:
    topology = _stereo_two_way_topology()
    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)
    for group in preview["groups"]:
        crossover = group["crossovers"][0]
        crossover["lower_polarity"] = "non-inverted"
        crossover["upper_polarity"] = "inverted"
        crossover["delay_ms"] = 0.3
        crossover["delay_target_role"] = "woofer"

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is not None, issues
    region = preset.crossover_regions[0]
    assert region.lower_polarity == "non-inverted"
    assert region.upper_polarity == "inverted"
    assert region.delay_ms == 0.3
    assert region.delay_target_driver == "woofer"


def test_compile_preset_from_crossover_preview_omits_polarity_and_delay_by_default() -> None:
    # Legacy-shaped preview (no persisted working-crossover values): the
    # region stays byte-identical to the pre-Slice-0 schema defaults.
    topology = _topology()
    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is not None, issues
    region = preset.crossover_regions[0]
    assert region.lower_polarity == "non-inverted"
    assert region.upper_polarity == "non-inverted"
    assert region.delay_ms is None
    assert region.delay_target_driver is None


def test_compile_preset_from_crossover_preview_stereo_polarity_mismatch_blocks() -> None:
    topology = _stereo_two_way_topology()
    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)
    left_group = next(g for g in preview["groups"] if g["kind"] == "left")
    left_group["crossovers"][0]["lower_polarity"] = "inverted"

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is None
    assert "crossover_preview_stereo_values_differ" in {
        issue["code"] for issue in issues
    }


def test_compile_preset_from_crossover_preview_stereo_delay_mismatch_blocks() -> None:
    topology = _stereo_two_way_topology()
    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)
    right_group = next(g for g in preview["groups"] if g["kind"] == "right")
    right_group["crossovers"][0]["delay_ms"] = 0.5
    right_group["crossovers"][0]["delay_target_role"] = "woofer"

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is None
    assert "crossover_preview_stereo_values_differ" in {
        issue["code"] for issue in issues
    }


# --- Manual /sound/ form entry path, end to end ------------------------------
#
# The tests above hand-mutate an already-built preview dict, which never
# exercises _normalise_candidate's validation or crossover_preview's
# between_roles realignment. These two start from a manual_settings candidate
# shaped exactly like jasper/active_speaker/design_draft.py's manualSettingsPayload
# (deploy/assets/sound-profile/js/main.js) sends, and follow it through the
# real chain: build_design_draft -> build_crossover_preview ->
# compile_preset_from_crossover_preview.


def test_compile_preset_from_crossover_preview_manual_settings_end_to_end_sets_polarity_and_delay() -> None:
    topology = _topology()
    draft = build_design_draft(
        topology,
        driver_research=_driver_research(frequency_hz=2500, way_count=2),
        manual_settings={
            "drivers": [],
            "crossover_candidates": [{
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
                "lower_polarity": "non-inverted",
                "upper_polarity": "inverted",
                "delay_ms": 0.15,
                "delay_target_role": "tweeter",
            }],
        },
        created_at="2026-07-11T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-07-11T12:00:05Z")

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is not None, issues
    region = preset.crossover_regions[0]
    assert region.lower_polarity == "non-inverted"
    assert region.upper_polarity == "inverted"
    assert region.delay_ms == 0.15
    assert region.delay_target_driver == "tweeter"


def test_compile_preset_from_crossover_preview_manual_settings_reversed_between_roles_realigns_end_to_end() -> None:
    # The candidate declares its pair as [tweeter, woofer] -- reversed from
    # this topology's own (lower_role, upper_role)=(woofer, tweeter). The same
    # PHYSICAL role (tweeter) must end up inverted/delayed regardless of which
    # order the candidate (or a reversed research import) listed the pair in.
    topology = _topology()
    draft = build_design_draft(
        topology,
        driver_research=_driver_research(frequency_hz=2500, way_count=2),
        manual_settings={
            "drivers": [],
            "crossover_candidates": [{
                "between_roles": ["tweeter", "woofer"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
                # Describes the candidate's OWN between_roles[0]=tweeter.
                "lower_polarity": "inverted",
                # Describes the candidate's OWN between_roles[1]=woofer.
                "upper_polarity": "non-inverted",
                "delay_ms": 0.15,
                "delay_target_role": "tweeter",
            }],
        },
        created_at="2026-07-11T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-07-11T12:00:05Z")

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is not None, issues
    region = preset.crossover_regions[0]
    assert region.lower_driver == "woofer"
    assert region.upper_driver == "tweeter"
    # Realigned to THIS function's (lower=woofer, upper=tweeter) convention:
    # tweeter is the physical role the candidate inverted/delayed, so it must
    # land on upper_polarity/delay here, not lower_polarity.
    assert region.lower_polarity == "non-inverted"
    assert region.upper_polarity == "inverted"
    assert region.delay_ms == 0.15
    assert region.delay_target_driver == "tweeter"


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


def test_stage_protected_startup_config_arms_subwoofer_muted(
    tmp_path: Path,
) -> None:
    # B2: a routed local subwoofer now STAGES — the sub output is wired into the
    # protected startup graph MUTED, exactly like the woofer/tweeter, rather than
    # blocking. The mains pick up the complementary bass-management high-pass.
    topology = _topology_with_subwoofer()
    preview = _crossover_preview(topology, with_subwoofer=True)
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

    assert payload["status"] == "staged"
    assert out.exists() is True
    assert subwoofer_gate["passed"] is True
    assert payload["issues"] == []

    text = out.read_text(encoding="utf-8")
    parsed = yaml_lib.safe_load(text)
    # output_count grows by the sub output: 2 mains + 1 sub = 3.
    assert parsed["devices"]["playback"]["channels"] == 3
    sub_steps = [
        step for step in parsed["pipeline"]
        if step.get("type") == "Filter" and step.get("channels") == [2]
    ]
    # The sub output (channel 2): its protective lane (band-limit LP + excursion
    # limiter) runs FIRST, then the per-output commission mute. So the sub is
    # band-limited + excursion-limited even when the mute is later lifted to ramp.
    assert sub_steps[0]["names"] == ["as_sub_lowpass", "as_sub_startup_limiter"]
    assert sub_steps[-1]["names"] == ["as_out2_commission_mute"]
    assert parsed["filters"]["as_sub_lowpass"]["parameters"]["type"] == (
        "LinkwitzRileyLowpass"
    )
    assert parsed["filters"]["as_sub_startup_limiter"]["type"] == "Limiter"
    # The sub starts MUTED at boot: its per-output commission mute is a hard mute
    # (all_commission_mutes_engaged is asserted by the fully-muted gate too).
    assert parsed["filters"]["as_out2_commission_mute"]["parameters"]["mute"] is True
    # The mains' lowest driver (woofer, output 0) carries the bass-management HP.
    woofer_step = next(
        step for step in parsed["pipeline"]
        if step.get("type") == "Filter" and step.get("channels") == [0]
    )
    assert "as_woofer_bass_mgmt_hp" in woofer_step["names"]


def test_stage_protected_startup_config_blocks_misrouted_subwoofer(
    tmp_path: Path,
) -> None:
    # Fail-closed: a sub pinned to a NON-contiguous output (not the next channel
    # after the mains) can never be armed safely — staging must block, never stage a
    # mains-only graph that silently drops the sub.
    raw = _topology().to_dict()
    raw["topology_id"] = "bench_mono_bad_sub"
    raw["speaker_groups"].append({
        "id": "sub",
        "label": "Bench subwoofer",
        "kind": "subwoofer",
        "mode": "subwoofer",
        # Mains occupy 0+1; a safe sub is on output 2. Output 5 is misrouted.
        "channels": [
            {"role": "subwoofer", "physical_output_index": 5, "identity_verified": True}
        ],
    })
    raw["routing"]["subwoofer_group_ids"] = ["sub"]
    topology = OutputTopology.from_mapping(raw)
    preview = _crossover_preview(topology, with_subwoofer=True)
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
    assert "active_subwoofer_output_not_contiguous" in {
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
    # Stage 2: a stereo 3-way (6 lanes) fits within the DAC8x active outputd
    # lane (width 8), so staging resolves to it rather than a direct-DAC route.
    assert payload["config"]["playback_device"] == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"
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
    # Single-audio-path commissioning isolates per *physical output*: the tweeter
    # (mono 2-way output index 1) is muted by its per-output commission mute, and
    # the per-role startup mute is gone. Protective HP + limiter still wrap it.
    assert "as_out1_commission_mute" in text
    assert "as_tweeter_startup_mute" not in text
    assert "as_tweeter_startup_limiter" in text
    assert loaded["status"] == "staged"
    assert loaded["software_guard"]["passed"] is True


def test_stage_protected_startup_config_blocks_incomplete_software_guard_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original_emit = staging_mod.emit_active_speaker_commissioning_config

    def corrupt_tweeter_mute(*args, **kwargs):
        # Simulate an audible tweeter: unmute its per-output commission mute. The
        # tweeter is mono 2-way output index 1, so flip as_out1_commission_mute.
        text = original_emit(*args, **kwargs)
        text = text.replace(
            (
                "  as_out1_commission_mute:\n"
                "    type: Gain\n"
                "    parameters: { gain: -120.0000, inverted: false, mute: true }"
            ),
            (
                "  as_out1_commission_mute:\n"
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
        "emit_active_speaker_commissioning_config",
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


def test_stage_protected_startup_config_boot_candidate_is_fully_muted(
    tmp_path: Path,
) -> None:
    # Crash-recovery invariant: the staged boot config has EVERY active output
    # muted. A reboot partway through commissioning must land everything-muted,
    # never a driver unmuted at level. Per-driver unmute is a transient runtime
    # load, never the frozen boot config.
    out = tmp_path / "active_staged.yml"
    payload = stage_protected_startup_config(
        _topology(),
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    muted_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "staged_candidate_fully_muted"
    )

    assert payload["status"] == "staged"
    assert muted_gate["passed"] is True
    parsed = yaml_lib.safe_load(out.read_text(encoding="utf-8"))
    commission_mutes = {
        name: spec
        for name, spec in parsed["filters"].items()
        if name.endswith("_commission_mute")
    }
    assert commission_mutes  # the production graph carries a per-output mute mask
    assert all(
        spec["parameters"]["mute"] is True for spec in commission_mutes.values()
    )


def test_stage_protected_startup_config_blocks_unmuted_boot_candidate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Crash-recovery guard, blocking direction: if the staged boot config is NOT
    # fully muted, staging must fail closed. Use a physically-protected topology
    # so the software guard is not computed and only the fully-muted gate fires —
    # isolating this guard from the software-guard path.
    original_emit = staging_mod.emit_active_speaker_commissioning_config

    def unmute_one_output(*args, **kwargs):
        text = original_emit(*args, **kwargs).replace(
            (
                "  as_out0_commission_mute:\n"
                "    type: Gain\n"
                "    parameters: { gain: -120.0000, inverted: false, mute: true }"
            ),
            (
                "  as_out0_commission_mute:\n"
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
        "emit_active_speaker_commissioning_config",
        unmute_one_output,
    )
    payload = stage_protected_startup_config(
        _topology(protection_status="present"),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    muted_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "staged_candidate_fully_muted"
    )
    codes = {issue["code"] for issue in payload["issues"]}

    assert payload["status"] == "blocked"
    assert muted_gate["passed"] is False
    assert "staged_config_not_fully_muted" in codes
    # Physical protection: the software guard never ran, so this is the gate that
    # caught the unmuted output.
    assert "software_tweeter_guard_incomplete" not in codes


def test_software_guard_evidence_passes_for_muted_tweeter_outputs() -> None:
    # Software guard now proves the tweeter is muted via its per-output commission
    # mute, not a per-role startup mute. A fully-muted commissioning config passes.
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    yaml = emit_active_speaker_commissioning_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        audible_outputs=frozenset(),
    )
    evidence = staging_mod._software_guard_evidence(yaml, preset=preset)

    assert evidence["passed"] is True
    assert evidence["checks"]["startup_muted"] is True
    assert evidence["checks"]["tweeter_pipeline_guarded"] is True
    # Stereo 2-way tweeters live on physical outputs 1 and 3.
    assert evidence["tweeter_channels"] == [1, 3]


def test_software_guard_evidence_blocks_audible_tweeter_output() -> None:
    # Unmute one tweeter output (index 1): a single audible tweeter must fail the
    # software guard's startup_muted check, even though output 3 stays muted.
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    yaml = emit_active_speaker_commissioning_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        audible_outputs={1},
    )
    evidence = staging_mod._software_guard_evidence(yaml, preset=preset)

    assert evidence["checks"]["startup_muted"] is False
    assert evidence["passed"] is False


def test_physical_protection_staged_config_reads_as_muted_via_fallback(
    tmp_path: Path,
) -> None:
    # A physically-protected candidate carries no software_guard block, so
    # path_safety._startup_muted_by_candidate falls back to scanning the staged
    # YAML. The single-audio-path commissioning config mutes via per-output
    # `as_out{idx}_commission_mute`; the fallback must read that as "startup
    # muted" or a physically-protected speaker's startup-load preflight would
    # wrongly report the boot config as unmuted.
    out = tmp_path / "active_staged.yml"
    payload = stage_protected_startup_config(
        _topology(protection_status="present"),
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "staged"
    assert payload["software_guard"] == {}  # physical protection: no software guard
    assert _startup_muted_by_candidate(payload) is True


def test_all_commission_mutes_engaged_requires_pipeline_wiring() -> None:
    # The always-on crash-recovery gate must verify each per-output mute is not
    # just DEFINED muted but actually WIRED into the pipeline on its channel.
    # A mute filter that is defined (-120 dB, muted) but whose pipeline step is
    # missing must fail closed — otherwise the gate trusts emitter lockstep.
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    yaml = emit_active_speaker_commissioning_config(
        preset, playback_device="hw:CARD=DAC8x,DEV=0", audible_outputs=frozenset()
    )
    assert staging_mod._all_commission_mutes_engaged(yaml, preset=preset) is True
    # Drop output 0's commission-mute PIPELINE step; keep its filter definition.
    unwired = yaml.replace(
        "  - type: Filter\n    channels: [0]\n    names: [as_out0_commission_mute]",
        "",
    )
    assert unwired != yaml  # the step existed and was removed
    assert "as_out0_commission_mute:" in unwired  # definition still present + muted
    assert staging_mod._all_commission_mutes_engaged(unwired, preset=preset) is False


def test_software_guard_evidence_blocks_when_tweeter_protection_unwired() -> None:
    # Isolate tweeter_pipeline_guarded: remove the tweeter's per-role protective
    # HP + limiter pipeline step while leaving every mute intact. startup_muted
    # stays True, but the HP/limiter no longer wrap the tweeter channel, so the
    # structural guard (and therefore `passed`) must fail.
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    yaml = emit_active_speaker_commissioning_config(
        preset, playback_device="hw:CARD=DAC8x,DEV=0", audible_outputs=frozenset()
    )
    baseline = staging_mod._software_guard_evidence(yaml, preset=preset)
    assert baseline["checks"]["tweeter_pipeline_guarded"] is True
    # The tweeter per-role chain is the pipeline Filter on channels [1, 3] whose
    # names begin with as_tweeter_protective_hp (unique to the tweeter chain).
    stripped, n = re.subn(
        r" {2}- type: Filter\n {4}channels: \[1, 3\]\n"
        r" {4}names: \[as_tweeter_protective_hp[^\]]*\]\n",
        "",
        yaml,
    )
    assert n == 1  # exactly the tweeter HP/limiter chain step was removed
    evidence = staging_mod._software_guard_evidence(stripped, preset=preset)
    assert evidence["checks"]["tweeter_pipeline_guarded"] is False
    assert evidence["checks"]["startup_muted"] is True  # mutes untouched
    assert evidence["passed"] is False
