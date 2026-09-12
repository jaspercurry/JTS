# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile temporary measurement graphs without changing saved playback state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence

from jasper.active_speaker import camilla_yaml, candidate_bank
from jasper.active_speaker.crossover_v2.measure_spec import (
    CANDIDATE_SCOPES,
    GRAPH_SCOPE_DRIVERS,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    candidate_room_peqs,
    compile_candidate_config,
    prove_candidate_config,
)
from jasper.active_speaker.profile import (
    ActiveSpeakerPreset, required_driver_roles,
)

__all__ = [
    "MeasurementGraphProfile",
    "MeasurementGraphRefused",
    "TuningGraphScope",
    "compile_tuning_graph",
    "emit_measurement_graph",
    "measurement_bass_extension",
]

TuningGraphScope = Literal["candidate", "candidate_branches"]


@dataclass(frozen=True)
class MeasurementGraphProfile:
    """Session inputs; protection comes from the confirmed speaker declaration."""

    preset: Any
    topology: Any
    role_channels: Mapping[str, int]
    playback_device: str
    protection_sections_by_role: Mapping[str, Sequence[Any]] | None = None


class MeasurementGraphRefused(ValueError):
    def __init__(self, reason: str, detail: Any) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")

    @property
    def code(self) -> str:
        return self.reason


def _filter_list(value: Any) -> bool:
    return (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        and all(isinstance(entry, Mapping) for entry in value)
    )


def measurement_bass_extension(
    *,
    scope: str,
    candidate: MeasuredCrossoverCandidate | None = None,
    candidate_id: str = "",
) -> dict[str, Any]:
    """Resolve the same optional layer for graph emission and peak admission."""
    if scope == GRAPH_SCOPE_DRIVERS:
        return {}
    if candidate is None and candidate_id:
        candidate = candidate_bank.find_banked_candidate(candidate_id).candidate
    if candidate is None:
        raise MeasurementGraphRefused("measurement_candidate_required", scope)
    if not isinstance(candidate, MeasuredCrossoverCandidate):
        raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
    return dict(candidate.bass_extension)


def require_candidate_speaker_identity(candidate: MeasuredCrossoverCandidate, preset: ActiveSpeakerPreset) -> None:
    # ADR-0303 frees tuning values, not the physical speaker assignment.
    def structure(value: ActiveSpeakerPreset) -> tuple[Any, ...]:
        return (value.way_count, value.channel_map, value.drivers, value.local_subwoofer, tuple(
            replace(region, delay_ms=None, delay_target_driver=None,
                    lower_polarity="non-inverted", upper_polarity="non-inverted")
            for region in value.crossover_regions
        ))
    if structure(candidate.source_preset) != structure(preset):
        raise MeasurementGraphRefused("measurement_candidate_speaker_mismatch", candidate.fingerprint)


def compile_tuning_graph(
    profile: MeasurementGraphProfile,
    *,
    scope: TuningGraphScope = "candidate",
    candidate: MeasuredCrossoverCandidate | None = None,
) -> str:
    """Compile and prove the candidate's complete speaker, room and bass graph."""
    if scope not in CANDIDATE_SCOPES:
        raise MeasurementGraphRefused("measurement_scope_invalid", scope)
    if candidate is None:
        raise MeasurementGraphRefused("measurement_candidate_required", scope)
    if not isinstance(candidate, MeasuredCrossoverCandidate):
        raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
    require_candidate_speaker_identity(candidate, profile.preset)
    # The shared reducer skips malformed records; refuse before it loses identity.
    if set(candidate.linearization) - set(required_driver_roles(candidate.source_preset.way_count)) or any(
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
        protection_sections_by_role=profile.protection_sections_by_role,
        room_peqs=candidate_room_peqs(candidate),
    )
    prove_candidate_config(candidate, candidate_text)
    if scope == "candidate_branches":
        if set(profile.role_channels) != {"woofer", "tweeter"} or set(profile.role_channels.values()) != {0, 1}:
            raise MeasurementGraphRefused("measurement_branch_channels", profile.role_channels)
        prefix, rest = candidate_text.split("\nmixers:\n", 1)
        _, pipeline = rest.split("\npipeline:\n", 1)
        mixer = camilla_yaml._emit_role_routed_mixer(
            candidate.source_preset, dict(profile.role_channels), apply_region_polarity=False,
        )
        return prefix + "\nmixers:\n" + mixer + "\npipeline:\n" + pipeline
    return candidate_text


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
