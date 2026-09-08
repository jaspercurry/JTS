# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile temporary measurement graphs without changing saved playback state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence

from jasper.json_fields import finite_float
from jasper.active_speaker import camilla_yaml
from jasper.active_speaker.baseline_profile import (
    applied_baseline_hardware_match,
    recompose_applied_baseline_yaml,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    compile_candidate_config,
    prove_candidate_config,
)
from jasper.active_speaker.profile import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    required_driver_roles,
)

__all__ = [
    "MeasurementGraphProfile",
    "MeasurementGraphRefused",
    "compile_tuning_graph",
    "emit_measurement_graph",
]


@dataclass(frozen=True)
class MeasurementGraphProfile:
    """Session inputs; protection comes from the confirmed speaker declaration."""

    preset: Any
    topology: Any
    role_channels: Mapping[str, int]
    playback_device: str
    protection_sections_by_role: Mapping[str, Sequence[Any]] | None = None
    applied_profile: Mapping[str, Any] | None = None


class MeasurementGraphRefused(ValueError):
    def __init__(self, reason: str, detail: Any) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


def _without_alignment(preset: ActiveSpeakerPreset) -> ActiveSpeakerPreset:
    return replace(preset, crossover_regions=tuple(
        replace(region, delay_ms=None, delay_target_driver=None,
                upper_polarity="non-inverted")
        for region in preset.crossover_regions
    ))


def _filter_list(value: Any) -> bool:
    return (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        and all(isinstance(entry, Mapping) for entry in value)
    )


def compile_tuning_graph(
    profile: MeasurementGraphProfile,
    *,
    scope: Literal["base", "speaker_tune"] = "base",
    candidate: MeasuredCrossoverCandidate | None = None,
) -> str:
    """Compile a stereo graph for summed captures, clouds and confirmation.

    Base keeps the applied structure, protection, trims and alignment; accepted
    speaker tune also keeps linearization and blend. A named candidate replaces
    those corrections with its complete candidate layer. None includes room,
    preference or bass-extension processing. Callers must compare DSP readback
    with the emitted graph before attributing a capture to it.
    """
    if scope not in ("base", "speaker_tune"):
        raise MeasurementGraphRefused("measurement_scope_invalid", scope)
    snapshot, issues = applied_baseline_hardware_match(
        profile.topology, applied_profile=profile.applied_profile or {},
    )
    if snapshot is None:
        raise MeasurementGraphRefused("measurement_profile_unavailable", issues)
    applied_preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
    if _without_alignment(applied_preset) != _without_alignment(profile.preset):
        raise MeasurementGraphRefused(
            "measurement_base_mismatch", "declared speaker differs from applied base",
        )
    if candidate is not None:
        if not isinstance(candidate, MeasuredCrossoverCandidate):
            raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
        if candidate.source_preset != profile.preset:
            raise MeasurementGraphRefused(
                "measurement_candidate_base_mismatch", candidate.fingerprint,
            )
        # The shared reducer skips malformed records. Refuse before reduction
        # so the graph cannot silently omit part of the named candidate.
        if set(candidate.linearization) - set(required_driver_roles(profile.preset.way_count)) or any(
            not isinstance(value, Mapping) or not _filter_list(value.get("filters"))
            for value in candidate.linearization.values()
        ):
            raise MeasurementGraphRefused("measurement_filters_invalid", candidate.fingerprint)
        devices = camilla_yaml.active_emit_devices(profile.playback_device, topology=profile.topology)
        candidate_text = compile_candidate_config(
            candidate, playback_device=profile.playback_device,
            capture_device=devices.capture_device,
            capture_format=devices.capture_format,
            playback_format=devices.playback_format,
            chunksize=devices.chunksize, target_level=devices.target_level,
            queuelimit=devices.queuelimit, enable_rate_adjust=devices.enable_rate_adjust,
        )
        prove_candidate_config(candidate, candidate_text)
        return candidate_text
    corrections = snapshot.get("corrections")
    if (
        not isinstance(corrections, Mapping)
        or set(corrections) != set(required_driver_roles(applied_preset.way_count))
        or any(
            not isinstance(value, Mapping)
            or finite_float(value.get("gain_db")) is None
            or finite_float(value.get("delay_ms")) is None
            or not isinstance(value.get("inverted"), bool)
            for value in corrections.values()
        )
    ):
        raise MeasurementGraphRefused("measurement_corrections_invalid", scope)
    try:
        camilla_yaml._validated_driver_corrections(
            applied_preset, {role: dict(value) for role, value in corrections.items()},
        )
    except ActiveSpeakerConfigError as exc:
        raise MeasurementGraphRefused("measurement_corrections_invalid", str(exc)) from exc
    if scope == "speaker_tune":
        linearization = snapshot.get("linearization", {})
        if (
            not isinstance(linearization, Mapping)
            or set(linearization) - set(required_driver_roles(profile.preset.way_count))
            or any(not _filter_list(value) for value in linearization.values())
            or not _filter_list(snapshot.get("blend_correction", []))
        ):
            raise MeasurementGraphRefused("measurement_filters_invalid", scope)
    text, issues = recompose_applied_baseline_yaml(
        profile.topology, applied_profile=profile.applied_profile or {},
        playback_device=profile.playback_device, bass_extension_profile=None,
        drop_measured_correction=scope == "base",
    )
    if text is None:
        raise MeasurementGraphRefused("measurement_graph_unavailable", issues)
    return text


def emit_measurement_graph(
    profile: MeasurementGraphProfile,
    inverted_roles: tuple[str, ...] = (),
    measurement_delays_us: Mapping[str, float] | None = None,
    level_trims_db: Mapping[str, float] | None = None,
) -> str:
    """Compile the protected neutral graph for separate driver analysis.

    Trims arrive resolved by ``baseline_profile.measured_level_trims``. Device
    fields travel together because ring capture and playback share one wire.
    """
    devices = camilla_yaml.active_emit_devices(profile.playback_device, topology=profile.topology)
    return camilla_yaml.emit_active_speaker_program_config(
        profile.preset,
        role_channels=dict(profile.role_channels),
        playback_device=profile.playback_device,
        protection_sections_by_role=profile.protection_sections_by_role,
        capture_device=devices.capture_device,
        capture_format=devices.capture_format,
        playback_format=devices.playback_format,
        chunksize=devices.chunksize,
        target_level=devices.target_level,
        queuelimit=devices.queuelimit,
        enable_rate_adjust=devices.enable_rate_adjust,
        inverted_roles=inverted_roles,
        measurement_delays_us=measurement_delays_us,
        measurement_level_trims_db=level_trims_db,
    )
