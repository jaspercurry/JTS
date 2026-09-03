# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The graph identity a round compares against: layers measured THROUGH.
Hashes the candidate layer and below (structure, linearization, blend, trim,
headroom, limiters); drops preference-EQ slots — content-derived, not
name-derived, since the name survives an out-of-band rewrite (#3489). Two
namespaces only: round candidate vs compiled baseline, never compared across.
Blind spots: ``active_baseline_headroom.output_trim_db`` moves this on a
``match_loudness`` EQ save; ``SUMMED_SWEEP_PHASES`` measures the standing
production graph (EQ included) while this fingerprint stays put.
"""

from __future__ import annotations

from typing import Any, Mapping

from jasper.audio_measurement.evidence_identity import json_fingerprint

__all__ = ["COMPARABILITY_BOUNDARY", "tuning_scope_fingerprint"]

#: Disclosure only, never a gate: the round's captures either side of it are not
#: comparable to each other.
COMPARABILITY_BOUNDARY = "tuning_scope_graph_changed"


def tuning_scope_fingerprint(graph_text: str | None) -> str:
    """Hash one CamillaDSP graph's tuning layers, preference slots excluded.

    ``graph_text`` is a config FILE's contents or a live readback, and the two
    are not the same document: ``set_active_config_raw`` leaves the persisted
    ``config_file_path`` alone, so a live-only change is invisible to a caller
    hashing the file. Raises
    :class:`~..commissioning_admission.ActiveCommissioningAdmissionError` on a
    graph that will not parse, rather than hashing an empty document that would
    compare equal to every other one.
    """

    from ..commissioning_admission import parse_running_graph

    return json_fingerprint(
        _without_preference_layer(parse_running_graph(graph_text))
    )


def _without_preference_layer(graph: Mapping[str, Any]) -> dict[str, Any]:
    """The same graph with every preference-EQ slot taken out.

    Both blocks must be scrubbed: ``filters`` carries the slots' parameters,
    ``pipeline`` carries which of them are wired.
    """

    from jasper.sound.profile import sound_filter_slot_names

    slots = sound_filter_slot_names()
    scoped = dict(graph)
    filters = scoped.get("filters")
    if isinstance(filters, Mapping):
        scoped["filters"] = {
            name: spec for name, spec in filters.items() if name not in slots
        }
    pipeline = scoped.get("pipeline")
    if isinstance(pipeline, list):
        scoped["pipeline"] = [
            step
            for step in (_step_without_slots(raw, slots) for raw in pipeline)
            if step is not None
        ]
    return scoped


def _step_without_slots(step: Any, slots: frozenset[str]) -> Any | None:
    """One pipeline step with the preference names dropped, or ``None``.

    ``None`` means the step was nothing but the preference layer; it must
    vanish rather than survive with an empty ``names`` list, so the hash does
    not move with the household's profile.
    """

    if not isinstance(step, Mapping):
        return step
    names = step.get("names")
    if not isinstance(names, list):
        return step
    kept = [name for name in names if name not in slots]
    if len(kept) == len(names):
        return step
    return {**step, "names": kept} if kept else None
