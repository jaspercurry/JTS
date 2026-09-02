# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The request-time doors that describe a HANDOFF between two branches.

ONE table, because the evidence packet's doors block, its not-evaluated list
and the prescriber CLI's next actions all read it: a reader must never be shown
a door shut under one name and listed as unanswered under another. A speaker
with no crossover region can open none of them — see ADR-0212.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from jasper.audio_measurement.program_analysis import ABSOLUTE_NO_CROSSOVER_TOPOLOGY

from .alignment_prescription import (
    ALIGNMENT_NO_CROSSOVER_REGION,
    alignment_prescription_response_format,
)
from .topology_prescription import (
    TOPOLOGY_NO_CROSSOVER_REGION,
    topology_prescription_response_format,
)

__all__ = [
    "HANDOFF_DOORS",
    "handoff_door_field",
    "no_crossover_not_evaluated_entries",
    "no_crossover_reason",
    "request_time_prescriptions",
]

#: Each handoff door, and the code it refuses a speaker with no crossover
#: region by.
HANDOFF_DOORS: tuple[tuple[str, str], ...] = (
    ("alignment", ALIGNMENT_NO_CROSSOVER_REGION),
    ("topology", TOPOLOGY_NO_CROSSOVER_REGION),
)


def no_crossover_reason(blend: Mapping[str, Any]) -> str | None:
    """The round's own reason for having no crossover region.

    ``None`` when the round declares a crossover — either it banked a band, or
    it failed to establish one, which the packet's ``field_null`` covers. The
    named SHAPE displaces that null, which would otherwise read as the second
    and send an operator to re-measure for a band that will never exist.
    """
    if blend.get("band_hz") is not None:
        return None
    if blend.get("reason") == ABSOLUTE_NO_CROSSOVER_TOPOLOGY:
        return ABSOLUTE_NO_CROSSOVER_TOPOLOGY
    return None


def handoff_door_field(door: str) -> str:
    """Where one shut door's absence sits: the door's OWN path, so a reader
    following ``field`` lands on the block that refused."""
    return f"request_time_prescriptions.{door}"


def request_time_prescriptions(
    no_crossover: bool,
    absence: Callable[[str, bool, str], dict[str, Any]],
) -> dict[str, Any]:
    """The two doors that open at session request time, or why they do not.

    Publishing their contracts to a speaker with no crossover region would
    document a door it cannot open. ``absence`` is the packet's own absence
    vocabulary, injected rather than restated here.
    """
    if not no_crossover:
        return {
            "alignment": alignment_prescription_response_format(),
            "topology": topology_prescription_response_format(),
        }
    return {
        door: {"available": False, **absence(refusal, False, handoff_door_field(door))}
        for door, refusal in HANDOFF_DOORS
    }


def no_crossover_not_evaluated_entries(no_crossover: bool) -> list[dict[str, str]]:
    """The three doors this shape cannot open, as not-evaluated rows.

    Their own blocks already say ``available: false``, but a reader who scans
    only the not-evaluated list would otherwise take the region as merely
    unmeasured and the two contracts as offers.
    """
    if not no_crossover:
        return []
    entries = [
        {
            "field": "crossover_region.band_hz",
            "reason": (
                f"{ABSOLUTE_NO_CROSSOVER_TOPOLOGY}: this speaker declares no "
                "crossover region, so there is no band for a blend prescription "
                "to sit inside and none will be measured by any later round"
            ),
        }
    ]
    entries.extend(
        {
            "field": handoff_door_field(door),
            "reason": (
                f"{refusal}: this door prescribes across a handoff between two "
                "branches, which this speaker does not have, so the session "
                "boundary refuses it by name rather than staging it"
            ),
        }
        for door, refusal in HANDOFF_DOORS
    )
    return entries
