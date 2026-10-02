# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile tuning and measurement graphs from candidates and declarations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence

import yaml

from jasper.platform.biquad import FilterSpec
from jasper.audio_routes.output_topology_store import load_output_topology_strict
from jasper.active_speaker.branch_chain import confirmed_protection_sections
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
    compile_candidate_config,
    prove_candidate_config,
)
from jasper.active_speaker.camilla_names import STARTUP_MUTE_GAIN_DB, output_rear_pending_mute_name
from jasper.active_speaker.graph_safety import output_terminally_muted, view_from_emitted_text, view_from_yaml_dict
from jasper.active_speaker.rear_calibration import front_floor_db
from jasper.active_speaker.measurement_programs import GRAPH_LAYERS
from jasper.active_speaker.profile import (
    ActiveSpeakerPreset, required_driver_roles,
)
from jasper.active_speaker.program_headroom import graph_headroom_db

__all__ = [
    "MeasurementGraphProfile",
    "MeasurementGraphRefused",
    "TuningGraphScope",
    "compile_tuning_graph",
    "load_tuning_declaration",
    "emit_measurement_graph",
    "measurement_graph_evidence",
    "park_muted_outputs",
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
    cleared_layers: tuple[str, ...] = (),
) -> dict[str, Any]:
    if scope == GRAPH_SCOPE_DRIVERS:
        return {}
    if candidate is None and candidate_id:
        candidate = candidate_bank.find_banked_candidate(candidate_id).candidate
    if candidate is None:
        raise MeasurementGraphRefused("measurement_candidate_required", scope)
    if not isinstance(candidate, MeasuredCrossoverCandidate):
        raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
    candidate = played_candidate(candidate, cleared_layers)
    if scope == "timing":
        candidate = _front_drivers(candidate)
    return {name: dict(getattr(candidate, name)) for name in GRAPH_LAYERS}


def played_candidate(candidate: MeasuredCrossoverCandidate, cleared_layers: tuple[str, ...]) -> MeasuredCrossoverCandidate:
    """``candidate`` as its take plays it: the layers the take's purpose clears
    emptied, never banked (ADR-0370)."""
    emptied: dict[str, Any] = {name: {} for name in cleared_layers}
    return replace(candidate, **emptied) if emptied else candidate


def require_candidate_speaker_identity(candidate: MeasuredCrossoverCandidate, preset: ActiveSpeakerPreset) -> None:
    if candidate.source_preset.speaker_identity() != preset.speaker_identity():
        raise MeasurementGraphRefused("measurement_candidate_speaker_mismatch", candidate.fingerprint)


def _front_drivers(candidate: MeasuredCrossoverCandidate) -> MeasuredCrossoverCandidate:
    """The timing take plays the front drivers its prediction models: no bass, the rear muted (#5632)."""
    rear = candidate.rear_calibration
    return replace(candidate, bass_extension={}, rear_calibration={**rear, "rear_muted": True} if rear else {})


def timing_candidate(candidate: MeasuredCrossoverCandidate, *, headroom_db: float) -> MeasuredCrossoverCandidate:
    """The front drivers at the candidate's level: its whole charge, ``headroom_db``, moves into the
    trims, and the timing graph charges only what still peaks, so the take never plays louder than
    the candidate. The dropped layers' cuts do not move, so at a cut it plays louder by that cut's
    depth. See ADR-0345 and ADR-0385."""
    front = _front_drivers(candidate)
    return replace(front, linearization={}, room_correction={}, blend_correction=(),
                   role_attenuations_db={role: gain - headroom_db for role, gain in candidate.role_attenuations_db.items()})


def compile_tuning_graph(
    profile: MeasurementGraphProfile,
    candidate: MeasuredCrossoverCandidate | None = None,
    *,
    scope: TuningGraphScope = "candidate",
    preference_filters: Sequence[FilterSpec] | None = None,
    output_trim_db: float = 0.0,
    branch_channels: Mapping[str, int] | None = None,
    cleared_layers: tuple[str, ...] = (),
) -> str:
    """Compile and prove the candidate at the requested layer.

    ``branch_channels`` names the two measurement targets a
    ``candidate_branches`` take excites and the stereo program channel each
    rides (:func:`~.crossover_v2.measure_spec.branch_channels_for`). Every
    other scope states none. ``cleared_layers`` are the candidate layers the
    take's purpose plays emptied (:func:`played_candidate`).
    """
    if scope not in CANDIDATE_SCOPES:
        raise MeasurementGraphRefused("measurement_scope_invalid", scope)
    if candidate is None:
        raise MeasurementGraphRefused("measurement_candidate_required", scope)
    if not isinstance(candidate, MeasuredCrossoverCandidate):
        raise MeasurementGraphRefused("measurement_candidate_invalid", type(candidate).__name__)
    require_candidate_speaker_identity(candidate, profile.preset)
    candidate = played_candidate(candidate_on_declaration(candidate, profile.preset), cleared_layers)
    if scope == "timing":
        charged = compile_tuning_graph(profile, candidate, preference_filters=preference_filters,
                                       output_trim_db=output_trim_db)
        candidate = timing_candidate(candidate, headroom_db=graph_headroom_db(view_from_emitted_text(charged)))
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
        **devices.emit_kwargs(),
        protection_sections_by_role=profile.protection_sections_by_role,
        room_peqs=candidate_room_peqs(candidate),
        excited_target_ids=excited_target_ids,
    )
    prove_candidate_config(candidate, candidate_text)
    if scope == "candidate_branches":
        mixers = yaml.safe_load(candidate_text)["mixers"]
        mixers.update(yaml.safe_load(camilla_yaml._emit_role_routed_mixer(
            candidate.source_preset, branches, apply_region_polarity=False,
        )))
        return _with_mixers(candidate_text, mixers)
    return candidate_text


def _with_mixers(text: str, mixers: Mapping[str, Any]) -> str:
    """``text`` with its ``mixers`` section replaced, every other line kept as emitted."""
    prefix, rest = text.split("\nmixers:\n", 1)
    return (prefix + "\n" + yaml.safe_dump({"mixers": dict(mixers)}, sort_keys=False)
            + "\npipeline:\n" + rest.split("\npipeline:\n", 1)[1])


def park_muted_outputs(text: str) -> str:
    """``text`` with no split source fed to an output that ends in its pending mute, so a
    summed take parks that output as a branch take parks a target it does not name;
    ``text`` itself when it feeds none (#6113)."""
    graph = yaml.safe_load(text)
    view = view_from_yaml_dict(graph)
    fed = [entry for name, mixer in (graph.get("mixers") or {}).items() if name.startswith("split_active_")
           for entry in mixer["mapping"] if entry.get("sources") and output_terminally_muted(
               graph, view, entry["dest"], mute_name=output_rear_pending_mute_name(entry["dest"]),
               mute_gain_db=STARTUP_MUTE_GAIN_DB)]
    for entry in fed:
        entry["sources"] = []
    return _with_mixers(text, graph["mixers"]) if fed else text


def timing_floor_db(profile: MeasurementGraphProfile, candidate: MeasuredCrossoverCandidate) -> dict[str, float]:
    """How far under unity, before ``candidate``'s charge is folded into its trims,
    the timing take's graph plays each front driver in its passband, dB: the
    driver's trim, on a cardioid's front woofer the lowest static response of its
    front chain, less what the timing graph still charges itself (ADR-0385)."""
    try:
        still = graph_headroom_db(view_from_emitted_text(compile_tuning_graph(profile, candidate, scope="timing")))
    except camilla_yaml.ProgramHeadroomExhausted as exc:
        still = exc.charge_db
    rear = candidate.rear_calibration
    return {role: trim - still + (front_floor_db(rear) if rear and role == "woofer" else 0.0)
            for role, trim in candidate.role_attenuations_db.items()}


def room_layer_charge_db(profile: MeasurementGraphProfile, candidate: MeasuredCrossoverCandidate) -> float:
    """What ``candidate``'s room layer adds to the charge of the graph it plays on ``profile``, dB:
    that graph's charge less the room-cleared take's. Negative where its cuts net a boost elsewhere
    (ADR-0385)."""
    if not candidate_room_peqs(candidate):
        return 0.0

    def charge(cleared_layers: tuple[str, ...]) -> float:
        try:
            return graph_headroom_db(view_from_emitted_text(
                compile_tuning_graph(profile, candidate, cleared_layers=cleared_layers)))
        except camilla_yaml.ProgramHeadroomExhausted as exc:
            return exc.charge_db

    return charge(()) - charge(("room_correction",))


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
        **devices.emit_kwargs(),
        inverted_roles=inverted_roles,
        measurement_delays_us=measurement_delays_us,
        measurement_level_trims_db=level_trims_db,
        parked_target_ids=profile.parked_target_ids,
    )
