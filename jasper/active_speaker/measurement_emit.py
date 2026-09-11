# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile temporary measurement graphs without changing saved playback state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence

from jasper.camilla_config_contract import PeqFilter
from jasper.json_fields import finite_float
from jasper.active_speaker import camilla_yaml
from jasper.active_speaker.baseline_profile import (
    applied_baseline_hardware_match,
    applied_bass_extension,
    recompose_applied_baseline_yaml,
)
from jasper.active_speaker.crossover_v2.measure_spec import (
    CANDIDATE_SCOPES,
    GRAPH_SCOPE_DRIVERS,
    GRAPH_SCOPES,
)
from jasper.active_speaker.linearization_fit import linearization_filters_by_role
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    candidate_room_peqs,
    compile_candidate_config,
    driver_corrections,
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
    "TuningGraphScope",
    "candidate_upstream_snapshot",
    "compile_tuning_graph",
    "emit_measurement_graph",
    "measurement_bass_extension",
]

TuningGraphScope = Literal[
    "base", "speaker_tune", "room_tune", "applied", "candidate",
    "room_candidate", "bass_candidate", "candidate_branches",
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

    @property
    def code(self) -> str:
        return self.reason


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


def measurement_bass_extension(
    profile: MeasurementGraphProfile,
    *,
    scope: str,
    candidate: MeasuredCrossoverCandidate | None = None,
) -> dict[str, Any]:
    """Resolve the same optional layer for graph emission and peak admission."""
    if scope == "bass_candidate":
        if candidate is None:
            raise MeasurementGraphRefused("measurement_candidate_required", scope)
        if not isinstance(candidate, MeasuredCrossoverCandidate):
            raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
        if not candidate.bass_extension:
            raise MeasurementGraphRefused("measurement_candidate_no_bass", candidate.fingerprint)
        return dict(candidate.bass_extension)
    if scope == "applied":
        return applied_bass_extension(profile.applied_profile or {})
    return {}


def candidate_upstream_snapshot(
    candidate: MeasuredCrossoverCandidate,
    *,
    topology: Any,
    applied_profile: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Require an overlay to preserve the currently accepted upstream layers."""
    snapshot, issues = applied_baseline_hardware_match(topology, applied_profile=applied_profile)
    if snapshot is None:
        raise MeasurementGraphRefused("measurement_profile_unavailable", issues)
    preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
    if _without_alignment(preset) != _without_alignment(candidate.source_preset):
        raise MeasurementGraphRefused("measurement_candidate_base_mismatch", candidate.fingerprint)
    if (
        driver_corrections(candidate) != snapshot.get("corrections")
        or linearization_filters_by_role(candidate.linearization) != snapshot.get("linearization", {})
        or [dict(f) for f in candidate.blend_correction] != snapshot.get("blend_correction", [])
    ):
        raise MeasurementGraphRefused("measurement_candidate_tune_mismatch", candidate.fingerprint)
    if candidate.bass_extension and candidate.room_correction != snapshot.get(
        "room_correction", applied_profile.get("room_correction", {}),
    ):
        raise MeasurementGraphRefused("measurement_candidate_room_mismatch", candidate.fingerprint)
    return snapshot


def compile_tuning_graph(
    profile: MeasurementGraphProfile,
    *,
    scope: TuningGraphScope = "base",
    candidate: MeasuredCrossoverCandidate | None = None,
) -> str:
    """Compile a temporary graph from the accepted upstream tuning layers.

    Room starts at speaker_tune; bass starts at room_tune. Candidate scopes
    replace only their named layer. Applied includes all saved tuning layers.
    Callers must prove DSP readback before attributing a capture to this graph.
    """
    if scope == GRAPH_SCOPE_DRIVERS or scope not in GRAPH_SCOPES:
        raise MeasurementGraphRefused("measurement_scope_invalid", scope)
    if scope in CANDIDATE_SCOPES and candidate is None:
        raise MeasurementGraphRefused("measurement_candidate_required", scope)
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
    room_peqs: tuple[PeqFilter, ...] | None = (
        None if scope in {"room_tune", "bass_candidate", "applied"} else ()
    )
    bass_extension = measurement_bass_extension(profile, scope=scope, candidate=candidate)
    if candidate is not None:
        if not isinstance(candidate, MeasuredCrossoverCandidate):
            raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
        if candidate.source_preset != profile.preset:
            raise MeasurementGraphRefused(
                "measurement_candidate_base_mismatch", candidate.fingerprint,
            )
        if candidate.bass_extension and scope != "bass_candidate":
            raise MeasurementGraphRefused("measurement_candidate_bass_scope", candidate.fingerprint)
        if scope in {"room_candidate", "bass_candidate"}:
            if scope == "room_candidate":
                room_peqs = candidate_room_peqs(candidate)
            if scope == "room_candidate" and not room_peqs:
                raise MeasurementGraphRefused(
                    "measurement_candidate_no_room", candidate.fingerprint,
                )
            candidate_upstream_snapshot(
                candidate, topology=profile.topology,
                applied_profile=profile.applied_profile or {},
            )
        else:
            # A candidate carrying a room layer has no plain-candidate graph:
            # this branch emits the speaker layer only, so the capture would be
            # attributed to a graph missing part of the named candidate. The
            # room set is measured under the room_candidate scope, or not at all.
            if candidate_room_peqs(candidate):
                raise MeasurementGraphRefused(
                    "measurement_candidate_room_scope", candidate.fingerprint,
                )
            # The shared reducer skips malformed records. Refuse before reduction
            # so the graph cannot silently omit part of the named candidate.
            if set(candidate.linearization) - set(required_driver_roles(profile.preset.way_count)) or any(
                not isinstance(value, Mapping) or not _filter_list(value.get("filters"))
                for value in candidate.linearization.values()
            ):
                raise MeasurementGraphRefused("measurement_filters_invalid", candidate.fingerprint)
            devices = camilla_yaml.active_emit_devices(
                profile.playback_device, topology=profile.topology,
            )
            candidate_text = compile_candidate_config(
                candidate, playback_device=profile.playback_device,
                capture_device=devices.capture_device,
                capture_format=devices.capture_format,
                playback_format=devices.playback_format,
                chunksize=devices.chunksize, target_level=devices.target_level,
                queuelimit=devices.queuelimit, enable_rate_adjust=devices.enable_rate_adjust,
                protection_sections_by_role=profile.protection_sections_by_role,
            )
            prove_candidate_config(candidate, candidate_text)
            if scope == "candidate_branches":
                if set(profile.role_channels) != {"woofer", "tweeter"} or set(profile.role_channels.values()) != {0, 1}:
                    raise MeasurementGraphRefused("measurement_branch_channels", profile.role_channels)
                prefix, rest = candidate_text.split("\nmixers:\n", 1)
                _, pipeline = rest.split("\npipeline:\n", 1)
                mixer = camilla_yaml._emit_role_routed_mixer(
                    profile.preset, dict(profile.role_channels), apply_region_polarity=False,
                )
                return prefix + "\nmixers:\n" + mixer + "\npipeline:\n" + pipeline
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
    if scope != "base":
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
        playback_device=profile.playback_device, bass_extension=bass_extension,
        room_peqs=room_peqs, drop_measured_correction=scope == "base",
        protection_sections_by_role=profile.protection_sections_by_role,
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
