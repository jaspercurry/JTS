# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WHERE this speaker may be crossed, and what it looks like re-cornered there.

A sibling of :mod:`.programs`, :mod:`.priors`, :mod:`.spatial` and
:mod:`.candidates`.  Those answer what a phase plays, what the analyzer is told,
what a capture-consuming phase decides, and what one build produced.  This one
answers the corner question in its two halves: **is a given corner admissible
for this speaker's declarations, and what does the preset become when a round
is opened at it?**

**The corner is executed, not hunted.**  A round crosses at the corner the
household declared, or at the one an operator pinned; nothing here ranks one
corner against another.  :func:`_fc_rejection` is the single owner of "is this
corner admissible", so a pinned corner and a declared one are judged on
identical terms rather than by two spellings of the same comparisons, and
:func:`recornered_preset` is the single owner of "what does this speaker look
like at that corner", so the preset a session opens at can never disagree with
the declaration it will be graded against.

**Only a damage stop refuses a corner.**  The two bounds :func:`_fc_rejection`
applies are the two drivers' declared HARD EXCITATION bands — the upper
driver's low edge and the lower driver's high edge — and each names a
component-damage mechanism the manufacturer or the household declared.  A third
bound used to sit beside them, an invented ``crossover_search_band_hz`` that
narrowed where a speaker *may* be crossed without naming any mechanism; the
owner deleted it on 2026-08-22 (#2870) because a corner the drivers' own bands
admit is a corner the operator is entitled to ask for.  Do not reintroduce a
bound here that cannot name what it protects.

**The filename is historical.**  This module also held R17's corner sweep — a
candidate set, a per-corner evaluation, a compute budget and an adjudication —
until that hunt was deleted (``docs/tuning-master-plan.md`` ruling R1, ticket
2.3).  Renaming it to match what is left would touch every module, test and
document that names it, for a one-word gain, so the name stayed and this
paragraph is the pointer: there is no sweep here, and a reader who came looking
for one is reading about cancelled work.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

__all__ = [
    "FC_REJECT_ABOVE_LOWER_DRIVER_BAND",
    "FC_REJECT_BELOW_DECLARED_FLOOR",
    "recornered_preset",
]

# Why a corner was refused. Named codes, never a bare number, because every one
# of these is a household- or operator-actionable declaration rather than an
# internal detail.
# Owner ruling, 2026-08-17: "exact is legal -- if the user/manufacturer says
# 1600, we should be able to do it. no nannies." A corner exactly AT the
# declared minimum recommended crossover is a SANCTIONED operating point, so
# only STRICTLY BELOW is refused, and the reason says so. The old
# ``at_or_below_declared_floor`` name described a behaviour that no longer
# exists. The one-sidedness the old strictness cited is a CONTINUUM (every Fc
# within an octave of the floor is clamped the same way, just less), not a cliff
# at equality, so there was no math to repair -- only conservatism to drop.
FC_REJECT_BELOW_DECLARED_FLOOR = "below_declared_floor"
FC_REJECT_ABOVE_LOWER_DRIVER_BAND = "above_lower_driver_band"


def _fc_rejection(
    fc_hz: float,
    hf_hard_floor_hz: float,
    lower_driver_hard_ceiling_hz: float,
) -> str | None:
    """The FIRST bound ``fc_hz`` violates, hardest first, or ``None``.

    Both bounds are hard excitation edges — the upper driver's low edge and the
    lower driver's high edge — so both name a damage mechanism.  There is no
    third, softer bound: see this module's docstring on the search band the
    2026-08-22 ruling deleted.
    """
    if fc_hz < float(hf_hard_floor_hz):
        return FC_REJECT_BELOW_DECLARED_FLOOR
    if fc_hz > float(lower_driver_hard_ceiling_hz):
        return FC_REJECT_ABOVE_LOWER_DRIVER_BAND
    return None


def recornered_preset(preset: Any, *, fc_hz: float, order: int | None = None) -> Any:
    """``preset`` with every crossover region moved to ``fc_hz`` (and ``order``).

    The single owner of "what does this speaker look like at that corner".  The
    request boundary hands a topology-pinned session a preset at the pinned
    corner so the round it runs is that topology's round end to end
    (:func:`~.topology_prescription.apply_topology_pin`, which both stages call);
    a second spelling would be a second answer to that question, and the two
    would drift in the region ``id`` if nowhere else.

    ``order`` moves only when one is pinned.  ``None`` is the automatic path and
    leaves the declared slope exactly as it was: direction and role assignment
    are always the preset's, and so is the order unless an operator supplied one.

    **Rewriting the ``id`` is required, and its spelling is a CONTRACT with a
    module this one may not import.**  ``baseline_profile.build_baseline_profile``
    admits a reviewed candidate only when its ``source_preset`` equals — by
    whole-dataclass ``!=``, ``id`` included — the preset
    ``staging.compile_preset_from_crossover_preview`` recompiles from the SAVED
    declaration, which spells it
    ``f"{lower_role}_{upper_role}_{int(round(frequency))}hz"``.  So a region left
    named ``..._1649hz`` while crossing at 4000 Hz is not merely a label that
    lies: it is refused ``measured_candidate_preset_mismatch`` forever.  The
    corner is the ONLY thing in the name, and a pinned order deliberately does
    not join it — an ``_lr2`` suffix would be a name that recompilation can
    never produce, which is the same permanent refusal wearing a tidier hat.
    Change this format only together with staging's.

    **A pinned ORDER still has to be declared before it can be applied**, for
    the same equality: the recompiled preset carries the declaration's slope, so
    an order-2 candidate measured against an order-4 declaration is a candidate
    that MEASURES and grades honestly and is refused at apply until the saved
    crossover names that order.  Measuring a candidate and adopting it are two acts
    here; this function serves the first.
    """
    moved: dict[str, Any] = {"fc_hz": float(fc_hz)}
    if order is not None:
        moved["order"] = int(order)
    return replace(preset, crossover_regions=tuple(
        replace(
            region,
            id=(
                f"{region.lower_driver}_{region.upper_driver}"
                f"_{int(round(float(fc_hz)))}hz"
            ),
            **moved,
        )
        for region in preset.crossover_regions
    ))
