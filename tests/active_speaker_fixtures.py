# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared topology builders for active-speaker tests."""

from __future__ import annotations

from pathlib import Path

from jasper.active_speaker.design_draft import DRIVER_RESEARCH_KIND, build_design_draft
from jasper.active_speaker.measurement import (
    record_driver_measurement,
    record_summed_test_artifact,
    record_summed_validation,
)
from jasper.audio_hardware import dac as dac_registry
from jasper.audio_hardware.dac import DacProfile
from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
from jasper.output_hardware import DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    set_channel_identity_verified,
)


def valid_camilla_config(path: str | Path) -> CamillaConfigValidationResult:
    """Return the suite's standard successful Camilla validation result."""
    return CamillaConfigValidationResult(
        status=ValidationStatus.VALID,
        path=str(path),
    )


def mono_output_topology(
    *,
    mode: str = "active_2_way",
    with_subwoofer: bool = False,
    identity_verified: bool = True,
    tweeter_verified: bool | None = None,
    tweeter_output: int | None = None,
    protection_status: str = "software_guard_requested",
    topology_id: str = "bench_mono",
    topology_name: str = "Bench mono cabinet",
    group_label: str = "Mono cabinet",
    sub_label: str = "Subwoofer",
    device_id: str = "hifiberry_dac8x",
    device_label: str = "HiFiBerry DAC8x",
    physical_output_count: int = 8,
    card_id: str | None = "DAC8",
) -> OutputTopology:
    """Build the suite's guarded mono topology with evidence-backed variants."""

    resolved_tweeter_verified = (
        identity_verified if tweeter_verified is None else tweeter_verified
    )
    if mode == "active_2_way":
        channels = [
            {
                "role": "woofer",
                "physical_output_index": 0,
                "identity_verified": identity_verified,
            },
            {
                "role": "tweeter",
                "physical_output_index": 1
                if tweeter_output is None
                else tweeter_output,
                "identity_verified": resolved_tweeter_verified,
                "startup_muted": True,
                "protection_required": True,
                "protection_status": protection_status,
            },
        ]
    elif mode == "active_3_way":
        channels = [
            {
                "role": "woofer",
                "physical_output_index": 0,
                "identity_verified": identity_verified,
            },
            {
                "role": "mid",
                "physical_output_index": 1,
                "identity_verified": identity_verified,
            },
            {
                "role": "tweeter",
                "physical_output_index": 2
                if tweeter_output is None
                else tweeter_output,
                "identity_verified": resolved_tweeter_verified,
                "startup_muted": True,
                "protection_required": True,
                "protection_status": protection_status,
            },
        ]
    elif mode == "full_range_passive":
        channels = [
            {
                "role": "full_range",
                "physical_output_index": 0,
                "identity_verified": identity_verified,
            }
        ]
    else:
        raise ValueError(f"unsupported mono output topology mode: {mode}")

    speaker_groups = [
        {
            "id": "mono",
            "label": group_label,
            "kind": "mono",
            "mode": mode,
            "channels": channels,
        }
    ]
    routing: dict[str, object] = {"mono_group_id": "mono"}
    if with_subwoofer:
        speaker_groups.append(
            {
                "id": "sub",
                "label": sub_label,
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [
                    {
                        "role": "subwoofer",
                        "physical_output_index": 3 if mode == "active_3_way" else 2,
                        "identity_verified": True,
                        "startup_muted": True,
                    }
                ],
            }
        )
        routing["subwoofer_group_ids"] = ["sub"]

    hardware = {
        "device_id": device_id,
        "device_label": device_label,
        "physical_output_count": physical_output_count,
    }
    if card_id is not None:
        hardware["card_id"] = card_id

    topology = OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": topology_id,
            "name": topology_name,
            "status": "draft",
            "hardware": hardware,
            "speaker_groups": speaker_groups,
            "routing": routing,
        }
    )
    # A parsed mapping cannot carry an audition: `identity_verified` survives a
    # save only where `set_channel_identity_verified` recorded it, so a bench
    # box whose lanes are confirmed is built the way a real one is.
    for group in topology.speaker_groups:
        for channel in group.channels:
            if channel.identity_verified:
                topology = set_channel_identity_verified(
                    topology,
                    speaker_group_id=group.id,
                    role=channel.role,
                    identity_verified=True,
                )
    return topology


def passive_stereo_output_topology() -> OutputTopology:
    """Full-range passive left+right mains with no subwoofer.

    TWO physical outputs, so the roleful Layer-A graph a recommissioning round
    compiles fits the active ring's 2..8-channel accept-set.
    """
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_stereo_passive",
        "name": "Bench stereo passive",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": side, "label": side.title(), "kind": side,
                "mode": "full_range_passive",
                "channels": [{
                    "role": "full_range",
                    "physical_output_index": index,
                    "identity_verified": True,
                }],
            }
            for index, side in enumerate(("left", "right"))
        ],
        "routing": {"main_left_group_id": "left", "main_right_group_id": "right"},
    })


def dual_apple_output_topology() -> OutputTopology:
    """Build the suite's standard two-DAC active-speaker topology."""
    return mono_output_topology(
        topology_name="Bench mono",
        device_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        device_label="Dual Apple USB-C DACs",
        physical_output_count=4,
        card_id="",
    )


PASSIVE_ONLY_DAC_ID = "bench_passive_only_dac"
PASSIVE_ONLY_DAC_LABEL = "Bench passive-only DAC"


def register_passive_only_dac(monkeypatch) -> DacProfile:
    """Register a synthetic DAC that declares NO active outputd lane.

    The active-lane capability guard needs a subject, and as of the InnoMaker
    flip every profile in the shipped registry declares a lane — so pinning the
    guard against whichever real profile happens to lack one is what made these
    tests break each time a profile gained the lane. This synthetic stands in
    for the NEXT passive-only DAC the registry meets, which is the population
    the guard actually protects.

    Patches ``_BY_ID`` ONLY — the dict read at call time by both
    :func:`jasper.audio_hardware.dac.by_id` and
    :func:`~jasper.audio_hardware.dac.is_known_profile_id`, so every caller
    that resolves or tests a profile BY ID sees the synthetic one. Registry
    ENUMERATION is deliberately left alone: ``all_profiles()`` reads
    ``REGISTRY``, so it will NOT list it. That fits the guard surfaces under
    test, which all resolve ``topology.hardware.device_id`` through ``by_id``;
    a test that needs the synthetic profile enumerated has to patch
    ``REGISTRY`` as well.
    """

    profile = DacProfile(
        id=PASSIVE_ONLY_DAC_ID,
        label=PASSIVE_ONLY_DAC_LABEL,
        kind="single",
        physical_output_count=2,
        coherent_clock_domain=True,
        clock_domain_label="Single bench passive-only device clock",
        clock_domain_contract="single_device",
        outputd_sink="alsa",
        supported_card_matches=("bench passive only",),
        supports_active_outputd_lane=False,
    )
    monkeypatch.setattr(
        dac_registry,
        "_BY_ID",
        {**dac_registry._BY_ID, profile.id: profile},
    )
    return profile


def safe_measurement_session(
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


def standard_driver_research(
    *,
    tweeter_gain_db: float = -18.5,
    with_subwoofer: bool = False,
) -> dict:
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
            # NOTE for the next author who writes an alternative-Fc test on this
            # fixture: 2500 here is ALSO the crossover_candidates frequency_hz
            # below, and since #2603 this one field IS the declared protection
            # floor. So this speaker has zero downward headroom -- any test that
            # nudges its crossover DOWN is proposing a crossover the driver's own
            # declaration forbids, and the apply-time gate
            # (camilla_yaml._assert_tweeter_crossover_honours_declared_floor)
            # will refuse it. That is not a bug in the gate; it is this fixture
            # declaring a floor it does not itself leave room under. The twin of
            # this block in tests/test_active_speaker_baseline_profile.py::_research
            # hit exactly that and now declares 2000; do the same here rather
            # than re-deriving the diagnosis, if you need downward room.
            # Left at 2500 because nothing currently CONSUMES this floor —
            # measured, not assumed: changing it to 2000 and running all 51
            # importers passes 2733 tests with nothing churning — so moving it
            # today would buy nothing. Change it freely when you need the room.
            "recommended_highpass_hz": 2500,
            "do_not_test_below_hz": 1200,
            "gain_offset_db": tweeter_gain_db,
            "sources": ["https://example.test/tweeter"],
        },
    ]
    if with_subwoofer:
        drivers.append(
            {
                "role": "subwoofer",
                "model": "Sub driver",
                "recommended_lowpass_hz": 80,
                "usable_frequency_range_hz": [20, 200],
                "sources": ["https://example.test/sub"],
            }
        )
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


def standard_design_draft(
    topology: OutputTopology,
    *,
    tweeter_gain_db: float = -18.5,
    with_subwoofer: bool = False,
) -> dict:
    return build_design_draft(
        topology,
        driver_research=standard_driver_research(
            tweeter_gain_db=tweeter_gain_db,
            with_subwoofer=with_subwoofer,
        ),
        created_at="2026-06-14T12:00:00Z",
    )


def standard_measurements(topology: OutputTopology, tmp_path: Path) -> dict:
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
            safe_session=safe_measurement_session(
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
