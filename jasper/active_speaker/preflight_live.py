# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read current facts for preflight. Live admission remains with resource owners."""
from __future__ import annotations

import logging
import math
from typing import Any, Mapping

import yaml

from jasper.audio_measurement import measurement_geometry
from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from jasper.audio_measurement.band_ladders import NEAR_FIELD_BANDS_HZ
from jasper.audio_measurement.wired_capture import WiredCaptureError, require_wired_mic
from jasper.platform.biquad import PeqFilter
from jasper.platform.log_event import log_event
from jasper.platform.control_client import read_output_volume
from jasper.platform.speaker_layout import measurement_target_id

from .angle_capture import REGIME_SUMMED, AngleCaptureRequest
from . import candidate_bank
from .baseline_profile import load_applied_baseline_profile_state
from .candidate_parts import candidate_from_applied_profile, candidate_from_design_draft, program_charge_db
from .commission_wiring import commissioning_spl_ceiling_db
from .crossover_v2.conductor_context import published_driver_caps, resolve_conductor_context
from .crossover_v2.refusal_copy import CrossoverV2Refused
from .design_draft import load_design_draft
from .measured_crossover_candidate import MeasuredCrossoverCandidate, candidate_room_peqs, plays_rear
from .measurement import active_driver_targets
from .measurement_emit import compile_tuning_graph, load_tuning_declaration, room_layer_charge_db, timing_floor_db
from .measurement_programs import BASE_CANDIDATE, candidate_identity, near_field_drivers
from .preflight import PreflightFacts, PreflightIssue
from .program_headroom import output_peaks_db
from .setup_status import conductor_status


def _draft_floor_db(topology: Any) -> Mapping[str, float] | None:
    """The timing floor of the declared draft, the run's base with no applied tune
    (``candidate_parts.baseline_candidate_id``); ``None`` when it cannot be read."""
    try:
        return timing_floor_db(load_tuning_declaration(topology),
                               candidate_from_design_draft(topology, load_design_draft(topology=topology)))
    except (OSError, RuntimeError, ValueError, LookupError):
        return None


def _driver_peaks_db(profile: Any, candidate: MeasuredCrossoverCandidate) -> dict[str, float] | None:
    """The loudest ``candidate``'s own graph plays each driver, dB re unity, by
    measurement target; ``None`` when that graph cannot be built."""
    try:
        peaks = output_peaks_db(yaml.safe_load(compile_tuning_graph(profile, candidate)), charged=True)
        found: dict[str, float] = {}
        for target in active_driver_targets(profile.topology):
            name = measurement_target_id(target["role"], target.get("output_variant", "primary"))
            found[name] = max(found.get(name, -math.inf), peaks[target["output_index"]])
    except (RuntimeError, ValueError, LookupError):
        return None
    return found


def _geometry_unreadable() -> str | None:
    """The declared room's unreadable field, or ``None`` when it reads or none is
    declared: declared but unreadable is not undeclared (ADR-0388)."""
    try:
        measurement_geometry.load_declared_geometry()
    except (OSError, ValueError, TypeError) as exc:
        return getattr(exc, "field", None) or type(exc).__name__
    return None


def read_preflight_facts(
    plan: AngleCaptureRequest, *, context: Any = None, device: Any = None,
    rig_clear_attested: bool | None = None, mover_available: bool = False,
) -> PreflightFacts:
    issues: list[PreflightIssue] = []
    if context is None:
        try:
            context = resolve_conductor_context(conductor_status())
        except CrossoverV2Refused as exc:
            issues.append(PreflightIssue.from_code(exc.code or "measure_box_not_ready", str(exc)))
    if device is None:
        try:
            device = require_wired_mic()
        except WiredCaptureError:
            pass
    stop = None
    applied_bass_extension: Mapping[str, Any] = {}
    applied_room_peqs: tuple[PeqFilter, ...] | None = ()
    applied_room_charge_db: float | None = None
    applied_program_charge: float | None = 0.0
    applied_floor: Mapping[str, float] | None = {}
    applied_rear: bool | None = False
    applied: MeasuredCrossoverCandidate | None = None
    if context is not None:
        try:
            stop = commissioning_spl_ceiling_db(context.topology, preset=context.preset)
        except ValueError:
            pass
        state: Mapping[str, Any] | None = None
        try:
            state = load_applied_baseline_profile_state() or {}
            applied = candidate_from_applied_profile(context.topology, state)
            applied_bass_extension, applied_room_peqs = applied.bass_extension, candidate_room_peqs(applied)
            applied_program_charge, applied_rear = program_charge_db(applied), plays_rear(applied)
            profile = load_tuning_declaration(context.topology)
            applied_floor = timing_floor_db(profile, applied)
            if applied_room_peqs:
                applied_room_charge_db = room_layer_charge_db(profile, applied)
        except (OSError, RuntimeError, ValueError, LookupError):
            # No applied profile has no room layer, charge or rear, and its base is the draft;
            # one that cannot be read has unknown ones.
            applied_room_peqs = () if state is not None and state.get("status") != "applied" else None
            unread = applied_room_peqs is None
            applied_program_charge, applied_rear = (None, None) if unread else (0.0, False)
            applied_floor = None if unread else _draft_floor_db(context.topology)
    candidates: dict[str, MeasuredCrossoverCandidate | PreflightIssue] = {}
    for name in dict.fromkeys(candidate_identity(stop.candidate_id) for stop in plan.stops):
        if name == BASE_CANDIDATE:
            continue
        try:
            candidates[name] = candidate_bank.find_banked_candidate(name).candidate
        except candidate_bank.CandidateBankRefusal as exc:
            candidates[name] = PreflightIssue.from_code(exc.code, f"{name}: {exc.detail}")
    # A summed take at the run's fader plays its candidate's own graph; with no applied tune
    # the base is the draft, read as unity (ADR-0385).
    summed = {candidate_identity(stop.candidate_id) for stop in plan.stops
              if stop.regime == REGIME_SUMMED and not stop.pose.driver}
    played = {name: candidate for name, candidate in ((BASE_CANDIDATE, applied), *candidates.items())
              if name in summed and isinstance(candidate, MeasuredCrossoverCandidate)}
    declared = None
    if played and context is not None:
        try:
            declared = load_tuning_declaration(context.topology)
        except (OSError, RuntimeError, ValueError, LookupError):
            pass
    driver_peaks = {name: peaks for name, candidate in played.items()
                    if declared is not None and (peaks := _driver_peaks_db(declared, candidate)) is not None}
    swept_floors = [(stop.pose.driver, stop.stimulus["band_hz"][0]) for stop in plan.stops
                    if stop.pose.driver and stop.stimulus is not None]
    output_volume = read_output_volume()
    if output_volume.get("muted"):
        log_event(logging.getLogger(__name__), "active_speaker.measurement_output_muted", fields=output_volume)
    return PreflightFacts(
        rig_clear_attested=rig_clear_attested, mover_available=mover_available,
        output_volume=output_volume,
        candidates=candidates, mic_present=device is not None,
        mic_identified=bool(device is not None and device.model_key),
        mic_sensitivity=resolved_household_sensitivity(device) if device is not None else None,
        geometry_unreadable=_geometry_unreadable(),
        commissioning_stop_db_spl=stop, mover=plan.mover, issues=tuple(issues),
        applied_bass_extension=applied_bass_extension, applied_room_peqs=applied_room_peqs,
        applied_room_charge_db=applied_room_charge_db, applied_program_charge_db=applied_program_charge,
        applied_timing_floor_db=applied_floor, applied_rear_plays=applied_rear, driver_peaks_db=driver_peaks,
        declared_target_ids=tuple(context.role_targets) if context is not None else None,
        # A driver sweeping a declared band is offered only when that sweep, clipped to the driver's
        # own band, starts at or below the near-field view's top band, so its takes read a band.
        near_field_drivers=(tuple(driver for driver in near_field_drivers(context.topology)
                                  if all(max(floor, context.driver_bands[driver].lower_hz) <= NEAR_FIELD_BANDS_HZ[-1][0]
                                         for named, floor in swept_floors if named == driver))
                            if context is not None and any(stop.pose.driver for stop in plan.stops) else None),
        roles_bands=context.roles_bands if context is not None else (),
        driver_caps=published_driver_caps(context.safety_profile, context.role_targets) if context is not None else {},
    )
