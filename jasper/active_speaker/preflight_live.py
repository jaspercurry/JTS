# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read current facts for preflight. Live admission remains with resource owners."""
from __future__ import annotations

import logging
from typing import Any

from jasper.audio_measurement import measurement_geometry
from jasper.audio_measurement.household_mic import resolved_household_sensitivity
from jasper.audio_measurement.band_ladders import NEAR_FIELD_BANDS_HZ
from jasper.audio_measurement.wired_capture import WiredCaptureError, require_wired_mic
from jasper.platform.log_event import log_event
from jasper.platform.control_client import read_output_volume

from .angle_capture import REGIME_BRANCHES, AngleCaptureRequest
from . import candidate_bank
from .commission_wiring import commissioning_spl_ceiling_db
from .crossover_v2.conductor_context import published_driver_caps, resolve_conductor_context
from .crossover_v2.refusal_copy import CrossoverV2Refused
from .design_draft import load_design_draft
from .measured_crossover_candidate import MeasuredCrossoverCandidate
from .measurement_programs import BASE_CANDIDATE, PURPOSE_REAR, candidate_identity, near_field_drivers
from .preflight import PreflightFacts, PreflightIssue
from .rear_seed import rear_seed_undeclared
from .setup_status import conductor_status


def _declared_geometry() -> tuple[measurement_geometry.DeclaredGeometry | None, str | None]:
    """The declared room, and its unreadable field, ``None`` when it reads or none is
    declared: declared but unreadable is not undeclared (ADR-0388)."""
    try:
        return measurement_geometry.load_declared_geometry(), None
    except (OSError, ValueError, TypeError) as exc:
        return None, getattr(exc, "field", None) or type(exc).__name__


def read_preflight_facts(
    plan: AngleCaptureRequest, *, context: Any = None,
    rig_clear_attested: bool | None = None, mover_available: bool = False,
) -> PreflightFacts:
    issues: list[PreflightIssue] = []
    if context is None:
        try:
            context = resolve_conductor_context(conductor_status())
        except CrossoverV2Refused as exc:
            issues.append(PreflightIssue.from_code(exc.code or "measure_box_not_ready", str(exc)))
    device = None
    try:
        device = require_wired_mic()
    except WiredCaptureError:
        pass
    stop = None
    if context is not None:
        try:
            stop = commissioning_spl_ceiling_db(context.topology, preset=context.preset)
        except ValueError:
            pass
    candidates: dict[str, MeasuredCrossoverCandidate | PreflightIssue] = {}
    for name in dict.fromkeys(candidate_identity(stop.candidate_id) for stop in plan.stops):
        if name == BASE_CANDIDATE:
            continue
        try:
            candidates[name] = candidate_bank.find_banked_candidate(name).candidate
        except candidate_bank.CandidateBankRefusal as exc:
            candidates[name] = PreflightIssue.from_code(exc.code, f"{name}: {exc.detail}")
    swept_floors = [(stop.pose.driver, stop.stimulus["band_hz"][0]) for stop in plan.stops
                    if stop.pose.driver and stop.stimulus is not None]
    output_volume = read_output_volume()
    if output_volume.get("muted"):
        log_event(logging.getLogger(__name__), "active_speaker.measurement_output_muted", fields=output_volume)
    geometry, geometry_unreadable = _declared_geometry()
    # The rear seed is read from a pair round, computed from the draft and the room it banks (ADR-0425).
    pair_take = any(PURPOSE_REAR in stop.purposes and stop.regime == REGIME_BRANCHES and not stop.pose.driver
                    for stop in plan.stops)
    return PreflightFacts(
        rig_clear_attested=rig_clear_attested, mover_available=mover_available,
        output_volume=output_volume,
        candidates=candidates, mic_present=device is not None,
        mic_identified=bool(device is not None and device.model_key),
        mic_sensitivity=resolved_household_sensitivity(device) if device is not None else None,
        geometry_unreadable=geometry_unreadable,
        rear_seed_undeclared=(tuple(rear_seed_undeclared(load_design_draft(computed=False), geometry))
                              if pair_take and geometry_unreadable is None else ()),
        commissioning_stop_db_spl=stop, mover=plan.mover, issues=tuple(issues),
        declared_target_ids=tuple(context.role_targets) if context is not None else None,
        # A driver sweeping a declared band is offered only when that sweep, clipped to the driver's
        # own band, starts at or below the near-field view's top band, so its takes read a band.
        near_field_drivers=(tuple(driver for driver in near_field_drivers(context.topology)
                                  if all(max(floor, context.driver_bands[driver].lower_hz) <= NEAR_FIELD_BANDS_HZ[-1][0]
                                         for named, floor in swept_floors if named == driver))
                            if context is not None and any(stop.pose.driver for stop in plan.stops) else None),
        roles_bands=context.roles_bands if context is not None else (),
        driver_caps=published_driver_caps(context.safety_profile, context.role_targets) if context is not None else {},
        context=context,
    )
