# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The graph identity a round compares against: the layers it measures THROUGH.

A round banks a graph fingerprint at entry so a later capture can be told
whether it is still measuring the same speaker. The question that fingerprint
has to answer is *comparability*, not identity: the candidate NAME is stable
across an out-of-band rewrite (the filename's ``<hash>`` is a SOURCE
fingerprint — ``baseline_profile._source_payload``), so only a
content-derived hash can say the bytes moved (#3489).

**Scoped, not whole-graph, and that is the whole design.** Preference EQ sits
ABOVE everything tunable: when a round tunes layer N it measures through layer
N and everything below it, never anything above. A whole-graph content hash
would therefore move on every household ``/sound/`` save and report a
comparability boundary on a layer no capture in the round went through —
noise that trains a driver to ignore the flag. So this hashes the candidate
and below (structure, linearization, blend, trim, headroom, limiters) and
deliberately drops the preference slots.

Two consequences, and they are the pins:

* a household EQ save moves the whole-graph fingerprint and NOT this one, so
  there is nothing to disclose;
* any change to a tuning layer moves this one, so the boundary fires exactly
  where a round's own banked captures stopped being comparable.

**One comparison, no classification.** A caller compares two of these for
equality and discloses :data:`COMPARABILITY_BOUNDARY`; nothing here says *why*
the graph changed, because the round does not act differently per cause.

**Compare within a namespace, never across one.** This hash belongs to a
THIRD namespace, beside two that already name a graph and are easy to read as
each other: the round candidate's fingerprint and the compiled baseline
profile's. Those two name the same tune through different derivations — on
jts3 the same commissioned speaker answered ``2f383a77`` and ``2af5b407`` —
so a difference between any two of the three is a namespace difference, not
drift. Only two values FROM THIS FUNCTION may be compared to each other.

**Two limits of the scope, stated rather than discovered later.**

* ``active_baseline_headroom`` is IN scope (it is the common program
  attenuation every layer below plays through) and it carries
  ``output_trim_db``, which the household owns. Since #3492 that fold is
  UNCONDITIONAL, so a profile crossing flat↔non-flat no longer moves it; what
  remains is the trim's own VALUE, which is profile-dependent only when
  ``match_loudness`` is on (``sound.settings.output_trim_db`` adds
  ``loudness_compensation_db(profile)``). On such a box an EQ save moves this
  fingerprint too. Correct as far as it goes — the common program level really
  did change — but it means the quiet-on-EQ-save property above is a
  fixed-trim property, not a universal one, and the term cannot be separated
  back out of a single summed gain.
* Every member of ``programs.SUMMED_SWEEP_PHASES`` — VERIFY, both position
  clouds, **and ENTRY_BASELINE** — measures the STANDING production graph
  rather than a measurement graph, and that graph carries the household's
  preference EQ. A save between two of them therefore does change what those
  captures went through while this fingerprint stays put. ENTRY_BASELINE is the
  sharpest case, because it and VERIFY are the pair
  :func:`~.verification.evaluate_benefit` differences: a save between them
  moves the "before" and the "after" apart for a reason no verdict attributes.
  The layering rule says such a capture should not have played through
  preference EQ at all; closing that is the measurement path's work, not this
  fingerprint's.

The substrate is unchanged and shared:
:func:`~..commissioning_admission.parse_running_graph` reads the graph and
:func:`~jasper.audio_measurement.evidence_identity.json_fingerprint` hashes it,
which is what makes this the same quantity as
:func:`~..commissioning_admission.running_graph_fingerprint` minus one layer,
rather than a second definition of "which graph".
"""

from __future__ import annotations

from typing import Any, Mapping

from jasper.audio_measurement.evidence_identity import json_fingerprint

__all__ = ["COMPARABILITY_BOUNDARY", "tuning_scope_fingerprint"]

#: The one disclosure this comparison produces: the graph a round entered on is
#: not the graph standing in front of it now, in the layers the round measures
#: through. A round's captures either side of it are not comparable to each
#: other. Never a gate — a capture that measured the speaker correctly is not
#: rejected because the speaker changed between two of them.
COMPARABILITY_BOUNDARY = "tuning_scope_graph_changed"


def tuning_scope_fingerprint(graph_text: str | None) -> str:
    """Hash one CamillaDSP graph's tuning layers, preference slots excluded.

    ``graph_text`` is whatever the caller holds — a config FILE's contents or a
    live readback. The two are not the same document: ``set_active_config_raw``
    deliberately leaves the persisted ``config_file_path`` alone, so a
    live-only change is invisible to a caller hashing the file. Each caller
    says which one it passed.

    Raises :class:`~..commissioning_admission.ActiveCommissioningAdmissionError`
    on a graph that will not parse, for the reason
    :func:`~..commissioning_admission.running_graph_fingerprint` does: a
    fingerprint over an unparseable readback would be a hash of the empty
    document and would compare equal to every other one.
    """

    from ..commissioning_admission import parse_running_graph

    return json_fingerprint(
        _without_preference_layer(parse_running_graph(graph_text))
    )


def _without_preference_layer(graph: Mapping[str, Any]) -> dict[str, Any]:
    """The same graph with every preference-EQ slot taken out.

    Both halves are needed and each on its own would leave the layer visible:
    the ``filters`` block carries the slots' parameters (a gain drag), and the
    ``pipeline`` block carries which of them are wired (a band added, or the
    step disappearing entirely on a profile that went flat).
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

    ``None`` means the step WAS the preference layer and nothing else. It has
    to vanish rather than survive with an empty ``names`` list: a flat profile
    emits no step at all (``_emit_baseline_pipeline`` writes one only for a
    non-empty name list), so a graph whose household went flat must hash equal
    to the same graph before the save.
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
