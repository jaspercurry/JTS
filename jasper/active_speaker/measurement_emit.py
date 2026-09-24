# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile tuning and measurement graphs from candidates and declarations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence

import yaml

from jasper.biquad import FilterSpec
from jasper.output_topology_store import load_output_topology_strict
from jasper.active_speaker.branch_chain import confirmed_protection_sections, rear_branch_sum_headroom_db
from jasper.active_speaker._common import MeasurementGraphRefused
from jasper.active_speaker.playback_route import resolve_active_playback_device
from jasper.active_speaker import camilla_yaml, candidate_bank
from jasper.active_speaker.crossover_v2.measure_spec import (
    CANDIDATE_SCOPES,
    GRAPH_SCOPE_DRIVERS,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    candidate_room_peqs, candidate_on_declaration,
    compile_candidate_config, driver_corrections, effective_preset,
    prove_candidate_config,
)
from jasper.active_speaker.measurement_programs import GRAPH_LAYERS
from jasper.active_speaker.profile import (
    ActiveSpeakerPreset, required_driver_roles,
)

__all__ = [
    "MeasurementGraphProfile",
    "MeasurementGraphRefused",
    "TuningGraphScope",
    "compile_tuning_graph",
    "load_tuning_declaration",
    "emit_measurement_graph",
    "measurement_graph_evidence",
]

TuningGraphScope = Literal["candidate", "candidate_branches", "timing"]


@dataclass(frozen=True)
class MeasurementGraphProfile:
    """Session inputs; protection comes from the confirmed speaker declaration."""

    preset: Any
    topology: Any
    role_channels: Mapping[str, int]
    playback_device: str
    protection_sections_by_role: Mapping[str, Sequence[Any]] | None = None
    #: Physical targets this take deliberately silences. A role absent from
    #: ``role_channels`` must be named here or the graph refuses to emit —
    #: silence is a decision, never an omission.
    parked_target_ids: tuple[str, ...] = ()


def load_tuning_declaration(
    topology: Any = None, *, design_draft: Mapping[str, Any] | None = None, playback_device: str | None = None,
) -> MeasurementGraphProfile:
    from .commission_wiring import resolve_commission_preset  # lazy: commissioning consumes measurement graphs
    from .crossover_preview import build_crossover_preview  # lazy: declaration compilation imports baseline readers
    from .design_draft import design_draft_view, load_design_draft  # lazy: declaration compilation imports baseline readers
    from .driver_safety import driver_floor_issues  # lazy: declaration compilation imports baseline readers

    topology = topology if topology is not None else load_output_topology_strict()
    draft = (design_draft_view(design_draft, topology=topology) if design_draft is not None
             else load_design_draft(topology=topology))
    safety = draft.get("driver_safety_profile", {})
    issues = driver_floor_issues(safety)
    if issues:
        raise MeasurementGraphRefused(issues[0]["code"], issues[0])
    try:
        protection = confirmed_protection_sections(safety)
    except ValueError as exc:
        raise MeasurementGraphRefused("driver_protection_invalid", str(exc)) from exc
    preset = resolve_commission_preset(topology, crossover_preview=build_crossover_preview(draft))
    return MeasurementGraphProfile(
        preset, topology, {},
        playback_device=str(resolve_active_playback_device(
            topology, playback_device=playback_device, required_output_count=camilla_yaml._output_count(preset),
        )[0]),
        protection_sections_by_role=protection,
    )


def _filter_list(value: Any) -> bool:
    return (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        and all(isinstance(entry, Mapping) for entry in value)
    )


def measurement_graph_evidence(
    *,
    scope: str,
    candidate: MeasuredCrossoverCandidate | None = None,
    candidate_id: str = "",
) -> dict[str, Any]:
    if scope == GRAPH_SCOPE_DRIVERS:
        return {}
    if candidate is None and candidate_id:
        candidate = candidate_bank.find_banked_candidate(candidate_id).candidate
    if candidate is None:
        raise MeasurementGraphRefused("measurement_candidate_required", scope)
    if not isinstance(candidate, MeasuredCrossoverCandidate):
        raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
    if scope == "timing":
        candidate = _front_drivers(candidate)
    return {name: dict(getattr(candidate, name)) for name in GRAPH_LAYERS}


def require_candidate_speaker_identity(candidate: MeasuredCrossoverCandidate, preset: ActiveSpeakerPreset) -> None:
    if candidate.source_preset.speaker_identity() != preset.speaker_identity():
        raise MeasurementGraphRefused("measurement_candidate_speaker_mismatch", candidate.fingerprint)


def _front_drivers(candidate: MeasuredCrossoverCandidate) -> MeasuredCrossoverCandidate:
    """The timing take plays the front drivers its prediction models: no bass, the rear muted (#5632 F3)."""
    rear = candidate.rear_calibration
    return replace(candidate, bass_extension={}, rear_calibration={**rear, "rear_muted": True} if rear else {})


def timing_candidate(candidate: MeasuredCrossoverCandidate, *, output_trim_db: float = 0.0) -> MeasuredCrossoverCandidate:
    from .linearization_fit import linearization_filters_by_role  # lazy: NumPy cost belongs to graph compilation

    front = _front_drivers(candidate)
    # Headroom this graph no longer charges, the muted rear's included, moves into the trims. The dropped
    # layers' cuts do not, so at a cut the take plays louder than the candidate. See ADR-0345.
    headroom = camilla_yaml.program_headroom_db(
        linearization_filters_by_role(candidate.linearization),
        branch_context=camilla_yaml._branch_context(effective_preset(candidate), driver_corrections(candidate)),
        room_peqs=candidate_room_peqs(candidate), output_trim_db=output_trim_db,
        rear_calibration=candidate.rear_calibration,
    ) - rear_branch_sum_headroom_db(front.rear_calibration)
    return replace(front, linearization={}, room_correction={}, blend_correction=(),
                   role_attenuations_db={role: gain - headroom for role, gain in candidate.role_attenuations_db.items()})


def compile_tuning_graph(
    profile: MeasurementGraphProfile,
    candidate: MeasuredCrossoverCandidate | None = None,
    *,
    scope: TuningGraphScope = "candidate",
    preference_filters: Sequence[FilterSpec] | None = None,
    output_trim_db: float = 0.0,
    branch_channels: Mapping[str, int] | None = None,
) -> str:
    """Compile and prove the candidate at the requested layer.

    ``branch_channels`` names the two measurement targets a
    ``candidate_branches`` take excites and the stereo program channel each
    rides (:func:`~.crossover_v2.measure_spec.branch_channels_for`). Every
    other scope states none.
    """
    if scope not in CANDIDATE_SCOPES:
        raise MeasurementGraphRefused("measurement_scope_invalid", scope)
    if candidate is None:
        raise MeasurementGraphRefused("measurement_candidate_required", scope)
    if not isinstance(candidate, MeasuredCrossoverCandidate):
        raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
    require_candidate_speaker_identity(candidate, profile.preset)
    candidate = candidate_on_declaration(candidate, profile.preset)
    if scope == "timing":
        candidate = timing_candidate(candidate, output_trim_db=output_trim_db)
        preference_filters, output_trim_db = (), 0.0
    # The shared reducer skips malformed records; refuse before it loses identity.
    if set(candidate.linearization) - set(required_driver_roles(candidate.source_preset.way_count)) or any(
        not isinstance(value, Mapping) or not _filter_list(value.get("filters"))
        for value in candidate.linearization.values()
    ):
        raise MeasurementGraphRefused("measurement_filters_invalid", candidate.fingerprint)
    excited_target_ids: tuple[str, ...] = ()
    branches: dict[str, int] = {}
    if scope == "candidate_branches":
        # Two branches on a stereo recording clock; WHICH two targets they are
        # (woofer/tweeter, or front/rear woofer) is the take's own choice, so it
        # arrives with the take rather than from the box's acoustic roles. A
        # rear the take drives on its own program channel must reach the emitter
        # as an excited target: muted, its branch would record silence.
        branches = dict(branch_channels or {})
        if len(branches) != 2 or not all(branches) or set(branches.values()) != {0, 1}:
            raise MeasurementGraphRefused("measurement_branch_channels", branch_channels)
        excited_target_ids = tuple(branches)
    devices = camilla_yaml.active_emit_devices(profile.playback_device, topology=profile.topology)
    candidate_text = compile_candidate_config(
        candidate, playback_device=profile.playback_device,
        preference_filters=preference_filters or (), output_trim_db=output_trim_db,
        capture_device=devices.capture_device,
        capture_format=devices.capture_format,
        playback_format=devices.playback_format,
        chunksize=devices.chunksize, target_level=devices.target_level,
        queuelimit=devices.queuelimit, enable_rate_adjust=devices.enable_rate_adjust,
        protection_sections_by_role=profile.protection_sections_by_role,
        room_peqs=candidate_room_peqs(candidate),
        excited_target_ids=excited_target_ids,
    )
    prove_candidate_config(candidate, candidate_text)
    if scope == "candidate_branches":
        prefix, rest = candidate_text.split("\nmixers:\n", 1)
        mixer_text, pipeline = rest.split("\npipeline:\n", 1)
        mixers = yaml.safe_load(mixer_text)
        mixers.update(yaml.safe_load(camilla_yaml._emit_role_routed_mixer(
            candidate.source_preset, branches, apply_region_polarity=False,
        )))
        return prefix + "\n" + yaml.safe_dump({"mixers": mixers}, sort_keys=False) + "\npipeline:\n" + pipeline
    return candidate_text


def emit_measurement_graph(
    profile: MeasurementGraphProfile,
    inverted_roles: tuple[str, ...] = (),
    measurement_delays_us: Mapping[str, float] | None = None,
    level_trims_db: Mapping[str, float] | None = None,
    excited_channels: Mapping[str, int] | None = None,
) -> str:
    """Compile the protected neutral graph for separate driver analysis.

    Trims arrive resolved by ``driver_base_trim.measured_level_trims``. Device
    fields travel together because ring capture and playback share one wire.

    ``excited_channels`` is one take's own choice of target, keyed by
    measurement target id (:func:`~.crossover_v2.measure_spec.branch_channels_for`):
    that take routes exactly those targets and parks every other declared one.
    ``None`` routes the session's :attr:`MeasurementGraphProfile.role_channels`.
    """
    if excited_channels:
        profile = replace(
            profile, role_channels=dict(excited_channels),
            parked_target_ids=tuple(sorted(camilla_yaml.preset_target_ids(profile.preset) - set(excited_channels))),
        )
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
        parked_target_ids=profile.parked_target_ids,
    )
