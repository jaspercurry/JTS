# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether writing a CamillaDSP graph must duck the fader.

A write ducks exactly when CamillaDSP will rebuild its pipeline (a changed
``devices``, ``pipeline`` or ``mixers`` section, a changed filter set, or a
filter whose kind changed); a change confined to filters' ``parameters`` goes
in place and does not (ADR-0211). Decided by comparing the RUNNING graph
against the WANTED one, never by a caller declaring its own change safe
(ADR-0177). The test is structural only: a moved broadband trim lands as a
level step rather than a fade, which ADR-0219 accepts to keep an A/B unfaded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import yaml

try:  # libyaml when the wheel carries it — this parses two full CamillaDSP
    # configs per edit on a Pi, inside the DSP writer lock, so the pure-Python
    # loader is the dominant cost of deciding duck-vs-quiet.
    from yaml import CSafeLoader as _Loader
except ImportError:  # pragma: no cover - depends on the installed wheel
    from yaml import SafeLoader as _Loader  # type: ignore[assignment]

__all__ = [
    "LiveEditPlan", "does_live_edits", "plan_live_edit", "plan_live_edit_for",
]


@dataclass(frozen=True)
class LiveEditPlan:
    """The route one write takes, and why.

    ``method`` is ``"unchanged"`` when the running graph already is the wanted
    one and nothing is written. ``reason`` names the section that forced a
    swap, and is empty otherwise.
    """

    method: Literal["swap", "parameters", "unchanged"]
    reason: str = ""

    @property
    def duck(self) -> bool:
        return self.method == "swap"

    @classmethod
    def swap(cls, reason: str) -> "LiveEditPlan":
        return cls("swap", reason)


def plan_live_edit(
    running_yaml: str | None, wanted_yaml: str | None,
) -> LiveEditPlan:
    """Return how to get from the running graph to the wanted one."""

    if not running_yaml or not wanted_yaml:
        return LiveEditPlan.swap("graph_unreadable")
    try:
        running = yaml.load(running_yaml, Loader=_Loader)
        wanted = yaml.load(wanted_yaml, Loader=_Loader)
    except (RecursionError, UnicodeError, ValueError, yaml.YAMLError):
        return LiveEditPlan.swap("graph_unparseable")
    if not isinstance(running, Mapping) or not isinstance(wanted, Mapping):
        return LiveEditPlan.swap("graph_not_a_mapping")
    if running == wanted:
        return LiveEditPlan("unchanged")

    if set(running) != set(wanted):
        return LiveEditPlan.swap("sections_differ")
    for section in running:
        if section != "filters" and running[section] != wanted[section]:
            return LiveEditPlan.swap(f"{section}_differs")

    running_filters = running.get("filters") or {}
    wanted_filters = wanted.get("filters") or {}
    if not isinstance(running_filters, Mapping) or not isinstance(
        wanted_filters, Mapping
    ):
        return LiveEditPlan.swap("filters_not_a_mapping")
    if set(running_filters) != set(wanted_filters):
        return LiveEditPlan.swap("filter_set_differs")
    for name, after in wanted_filters.items():
        before = running_filters[name]
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            return LiveEditPlan.swap("filter_not_a_mapping")
        # The filter's KIND (Biquad, Conv, Gain...), not a biquad's ``type``,
        # which lives under ``parameters`` and moves in place.
        if before.get("type") != after.get("type"):
            return LiveEditPlan.swap("filter_kind_differs")
    return LiveEditPlan("parameters")


async def plan_live_edit_for(cam: Any, wanted_yaml: str) -> LiveEditPlan:
    """The plan for writing ``wanted_yaml`` onto whatever ``cam`` runs now.

    Both sides must be CamillaDSP's own normalization: ``ReadConfig`` parses
    and default-fills without applying, and the running readback is that same
    default-filled superset of what JTS wrote. Comparing the emitted bytes
    against it instead would differ on every edit and duck every one.
    """

    running = await cam.get_active_config_raw(best_effort=True)
    wanted = await cam.normalize_config_raw(wanted_yaml, best_effort=True)
    return plan_live_edit(running, wanted)


def does_live_edits(cam: Any) -> bool:
    """Whether ``cam`` exposes the raw-config API a plan needs."""
    names = ("get_active_config_raw", "normalize_config_raw",
             "set_active_config_raw")
    return all(hasattr(cam, name) for name in names)
