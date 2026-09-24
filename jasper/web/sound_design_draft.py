# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The Sound declaration's one writer: a design draft saved from the wizard,
or a measured crossover written back onto it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from jasper.active_speaker.crossover_declaration import declared_crossover_geometry, manual_settings_for_crossover
from jasper.active_speaker.design_draft import load_design_draft, save_design_draft
from jasper.active_speaker.installation import installation_view
from jasper.log_event import log_event
from jasper.output_topology_store import load_output_topology

if TYPE_CHECKING:
    from jasper.active_speaker.crossover_declaration import CrossoverGeometry

logger = logging.getLogger(__name__)


def _active_speaker_design_draft_save_payload(
    raw: dict[str, Any], *, durable: bool = False
) -> dict[str, Any]:
    """Persist a design draft from current topology plus bounded research JSON.

    ``durable`` is a caller-only knob (never read from ``raw``, so an HTTP
    body can't set it): the crossover-accept seam
    (:func:`apply_measured_crossover_geometry`) opts in, ordinary wizard
    edits keep the cheaper default.
    """
    if not isinstance(raw, dict):
        raise ValueError("design draft request must be an object")
    allowed = {
        "driver_research",
        "manual_settings",
        "operator_inputs",
    }
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "design draft request has unknown fields: " + ", ".join(unknown)
        )
    topology = load_output_topology()
    payload = save_design_draft(
        topology,
        driver_research=raw.get("driver_research"),
        manual_settings=raw.get("manual_settings"),
        operator_inputs=raw.get("operator_inputs"),
        durable=durable,
    )
    log_event(
        logger,
        "sound.active_speaker_design_draft_save",
        status=str(payload.get("status")),
        topology_id=topology.topology_id,
        driver_count=str((payload.get("summary") or {}).get("driver_count")),
        candidate_count=str(
            (payload.get("summary") or {}).get("crossover_candidate_count")
        ),
        manual_driver_count=str(
            (payload.get("summary") or {}).get("manual_driver_count")
        ),
        manual_candidate_count=str(
            (payload.get("summary") or {}).get("manual_crossover_candidate_count")
        ),
        safety_profile_issues=",".join(issue["code"] for issue in
                                     (payload.get("driver_safety_profile") or {}).get("issues", [])),
        issues=len(payload.get("issues") or []),
    )
    return installation_view(payload)


def apply_measured_crossover_geometry(
    *, between_roles: tuple[str, str],
    configured: "CrossoverGeometry", selected: "CrossoverGeometry",
) -> dict[str, Any]:
    """Write a measured crossover onto the Sound declaration. Durable: every
    write through this function is fsynced before it is visible.

    The declaration states a crossover as three fields (corner, filter type,
    slope) and all three go through this one writer, in one write, one fsync
    and one Undo leg: ``measurement_emit.require_candidate_speaker_identity``
    compares the speaker identity, crossover regions included, and slope compiles into
    ``CrossoverRegion.order``, so a candidate measured at a
    different slope is as unreconcilable with the saved declaration as one
    measured at a different corner.
    """
    draft = load_design_draft(topology=load_output_topology())
    current = declared_crossover_geometry(draft, between_roles)
    if current is None or not current.matches(configured):
        raise ValueError("Sound changed since this measurement; review afresh")
    return _active_speaker_design_draft_save_payload({
        "driver_research": draft.get("driver_research"),
        "manual_settings": manual_settings_for_crossover(draft, between_roles, selected),
        "operator_inputs": draft.get("operator_inputs"),
    }, durable=True)
