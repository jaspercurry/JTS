# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How one live preference-EQ edit should reach CamillaDSP: patch or swap.

Replacing a CamillaDSP pipeline can move the graph's own gain by tens of dB at
an unchanged fader — loudest when a boosted correction is removed and the
headroom it carried goes away — so every replace ducks the fader across the
swap (:meth:`jasper.camilla.CamillaController._graph_mutation`). That costs
about 0.85 s of fade per edit, which is the whole of ``/eq/``'s user
experience. Writing declared parameters of filters that are ALREADY running
carries no such step, and :meth:`~jasper.camilla.CamillaController.patch_config`
does not duck.

Which of the two an edit needs is decided HERE, by comparing the graph that is
RUNNING against the graph the edit wants. It is never a caller declaring its
own change safe: inferring a writer's intent rather than proving it is the
defect ADR-0177 closed, and the reason the first attempt at this (#3309) was
correctly rejected. Any structural difference at all — a filter added, removed
or retyped, a pipeline step moved, anything under ``devices`` or ``mixers`` —
falls back to the swap and its duck. Only finite numbers inside a surviving
filter's ``parameters`` may move.

Both sides must be CamillaDSP's own normalization of a config
(:meth:`~jasper.camilla.CamillaController.get_active_config_raw` and
:meth:`~jasper.camilla.CamillaController.normalize_config_raw`), never an
emitter's raw text: the running readback is a default-filled superset of what
JTS wrote, so comparing against the emitted bytes would report a structural
difference on every edit and silently never patch.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

try:  # libyaml when the wheel carries it — this parses two full CamillaDSP
    # configs per edit on a Pi, inside the DSP writer lock, so the pure-Python
    # loader is the dominant cost of deciding patch-vs-swap.
    from yaml import CSafeLoader as _Loader
except ImportError:  # pragma: no cover - depends on the installed wheel
    from yaml import SafeLoader as _Loader  # type: ignore[assignment]

__all__ = ["LiveEditPlan", "plan_live_edit"]


@dataclass(frozen=True)
class LiveEditPlan:
    """The route one edit takes, and why.

    ``patch`` is ``None`` when the pipeline must be replaced, an empty mapping
    when the two graphs are identical and nothing needs to be written at all,
    and otherwise the ``PatchConfig`` payload that turns the running graph into
    the wanted one.
    """

    patch: dict[str, Any] | None
    reason: str

    @property
    def method(self) -> str:
        """The route, in the vocabulary the log line and the payload use.

        One name per route, derived once. Deriving it a second time at the call
        site is how the two spellings drift, and a drift here is silent: the
        edit simply stops being written while the response still says "live".
        """
        if self.patch is None:
            return "active_config_raw"
        return "patch_config" if self.patch else "unchanged"

    @classmethod
    def swap(cls, reason: str) -> "LiveEditPlan":
        """Fall back to the ducked pipeline replace, and say why."""
        return cls(None, reason)


def _swap(reason: str) -> LiveEditPlan:
    return LiveEditPlan.swap(reason)


def _is_number(value: Any) -> bool:
    # bool is an int subclass, and a flag flipping is a shape change, not a
    # value moving.
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _parameters_only_moved(before: Any, after: Any) -> bool:
    """True when two filter definitions differ only in finite numbers."""

    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    if set(before) != set(after):
        return False
    for key in before:
        if key == "parameters":
            continue
        if before[key] != after[key]:
            return False
    old_params, new_params = before.get("parameters"), after.get("parameters")
    if not isinstance(old_params, Mapping) or not isinstance(new_params, Mapping):
        return False
    if set(old_params) != set(new_params):
        return False
    for key, wanted in new_params.items():
        current = old_params[key]
        if current == wanted:
            continue
        # A biquad's own ``type`` lives in here beside its numbers; changing it
        # rebuilds the filter's shape, so it is a swap like any other.
        if not _is_number(current) or not _is_number(wanted):
            return False
        if not math.isfinite(wanted):
            return False
    return True


def plan_live_edit(
    running_yaml: str | None, wanted_yaml: str | None,
) -> LiveEditPlan:
    """Return how to get from the running graph to the wanted one."""

    if not running_yaml or not wanted_yaml:
        return _swap("graph_unreadable")
    try:
        running = yaml.load(running_yaml, Loader=_Loader)
        wanted = yaml.load(wanted_yaml, Loader=_Loader)
    except (RecursionError, UnicodeError, ValueError, yaml.YAMLError):
        return _swap("graph_unparseable")
    if not isinstance(running, Mapping) or not isinstance(wanted, Mapping):
        return _swap("graph_not_a_mapping")

    if set(running) != set(wanted):
        return _swap("sections_differ")
    for section in running:
        if section == "filters":
            continue
        if running[section] != wanted[section]:
            return _swap(f"{section}_differs")

    running_filters = running.get("filters") or {}
    wanted_filters = wanted.get("filters") or {}
    if not isinstance(running_filters, Mapping) or not isinstance(
        wanted_filters, Mapping
    ):
        return _swap("filters_not_a_mapping")
    if set(running_filters) != set(wanted_filters):
        return _swap("filter_set_differs")

    changed: dict[str, Any] = {}
    for name in sorted(wanted_filters):
        before, after = running_filters[name], wanted_filters[name]
        if before == after:
            continue
        if not _parameters_only_moved(before, after):
            return _swap("filter_shape_differs")
        # The whole definition, not a delta: PatchConfig merges what it is
        # given, so sending the wanted filter entire leaves no room for a key
        # the diff did not think about to keep its old value.
        changed[name] = after

    if not changed:
        return LiveEditPlan({}, "")
    return LiveEditPlan({"filters": changed}, "")
