# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Whether one live preference-EQ edit must duck the fader.

CamillaDSP applies every config it is handed through one diff of its own
(``config_diff`` in its ``config/utils.rs``): a changed ``devices`` block,
``pipeline`` or ``mixers`` section, or a filter whose KIND changed (``Biquad``
to ``Conv``) rebuilds the filter group and resets every filter's state;
anything else — a biquad's ``type``, ``freq``, ``q`` or ``gain`` alike — is
written into the running filters in place, coefficients recomputed and state
kept. Only the rebuild can step the graph's gain by tens of dB at an unchanged
fader or tear the waveform, so only the rebuild is worth the ~0.85 s duck that
:meth:`jasper.camilla.CamillaController._graph_mutation` brackets it with.

This module asks the same question by the same rule, so an edit is ducked
exactly when CamillaDSP will rebuild. It is a comparison of the RUNNING graph
against the WANTED one, never a caller declaring its own change safe
(ADR-0177, and why #3309 was rejected). See ADR-0211.

Both sides must be CamillaDSP's own normalization of a config
(:meth:`~jasper.camilla.CamillaController.get_active_config_raw` and
:meth:`~jasper.camilla.CamillaController.normalize_config_raw`), never an
emitter's raw text: the running readback is a default-filled superset of what
JTS wrote, so comparing against the emitted bytes would report a structural
difference on every edit and duck every one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import yaml

try:  # libyaml when the wheel carries it — this parses two full CamillaDSP
    # configs per edit on a Pi, inside the DSP writer lock, so the pure-Python
    # loader is the dominant cost of deciding duck-vs-quiet.
    from yaml import CSafeLoader as _Loader
except ImportError:  # pragma: no cover - depends on the installed wheel
    from yaml import SafeLoader as _Loader  # type: ignore[assignment]

__all__ = ["LiveEditPlan", "plan_live_edit"]


@dataclass(frozen=True)
class LiveEditPlan:
    """The route one edit takes, and why.

    ``method`` is ``"swap"`` when CamillaDSP will rebuild the pipeline (the
    write ducks), ``"parameters"`` when it will update the running filters in
    place (the write does not duck), and ``"unchanged"`` when the running
    graph already is the wanted one and nothing is written. ``reason`` names
    the section that forced a swap, and is empty otherwise.
    """

    method: str
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
