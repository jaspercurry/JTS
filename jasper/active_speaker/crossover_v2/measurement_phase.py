# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which stimulus one measurement plays — the engine's kind, in flow words.

Two vocabularies meet here and nowhere else. The engine names a measurement
:data:`~.contracts.MEASURE_KINDS` — *baseline* · *candidate* · *verify* — which
says what the measurement IS. The flow names a
:mod:`~.journey` PHASE, which says which composed program the session plays.
:func:`~.programs.program_for_phase` is the door between a phase and an
``ExcitationProgram``; this module is the door between a spec and that phase.

**A wrong entry here does not fail — it plays the wrong stimulus and banks a
record that looks correct.** Nothing downstream re-derives the intent: the
record carries the kind it was asked for and the analysis reads the curve it
got, so a baseline resolved to the per-driver program would be graded against a
summed sweep and the verdict would be about two different questions. That is
why the map is a table with a pinned row per pair rather than a chain of
conditionals, and why an unmapped kind refuses by name instead of falling
through to a default.

**The regime does NOT choose the stimulus, and that is the load-bearing
finding.** ``near_field`` and ``reference_axis`` are
:data:`~jasper.active_speaker.driver_acoustics.CAPTURE_GEOMETRIES` — they say
where the MICROPHONE sits, not what the speaker emits. A near-field capture of
a candidate plays the same per-driver program a reference-axis capture of it
plays; what differs is the mic, and the splice that near-field still owes
(``R-3``). So the resolved phase is invariant under regime, and that invariance
is asserted rather than assumed — a future regime that really did need its own
stimulus would have to break the pin to get in.

**Three kinds, three phases, and the flow has more phases than that.** ``check``
(the gain solve that MEASURE's program depends on), ``applying``, ``lateral``
and the two position-group clouds have no engine kind at all, because the
engine's ``measure`` verb does not express them: a lateral pose replays the
anchor's program verbatim and a cloud is a prompted position group, both of
which are walk shapes rather than measurement kinds. They are reachable only
through the flow's own walk, and stay that way until the wave that lifts it.
"""

from __future__ import annotations

from .contracts import (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
)
from .journey import PHASE_ENTRY_BASELINE, PHASE_MEASURE, PHASE_VERIFY

__all__ = [
    "UNMAPPED_MEASUREMENT_KIND",
    "NoPhaseForMeasurementError",
    "PHASE_BY_MEASURE_KIND",
    "phase_for_measurement",
]

#: The refusal code for a kind this map does not carry. A reason code in the
#: spelling the flow's registry uses, so a caller reports it rather than
#: rendering an exception's prose.
UNMAPPED_MEASUREMENT_KIND = "unmapped_measurement_kind"


class NoPhaseForMeasurementError(RuntimeError):
    """This map carries no stimulus for that measurement kind.

    Loud by design, and the ``take_kind`` precedent is why: an unresolvable
    fact is reported as itself, never guessed. A default arm here would play
    *some* stimulus for a kind nobody mapped, and the record would carry no
    sign that the choice was arbitrary.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(
            f"no stimulus is mapped for measurement kind {kind!r}"
        )
        self.code = UNMAPPED_MEASUREMENT_KIND
        self.kind = kind


#: kind → the flow phase whose composed program that measurement plays.
#:
#: Each row is a claim about WHICH SOUND the speaker makes, so each is pinned:
#:
#: * ``baseline`` → :data:`~.journey.PHASE_ENTRY_BASELINE` — #2291's "before":
#:   one summed sweep at the design-axis mark, taken as the last thing stage 1
#:   does. It shares its program OBJECT with ``verify`` (see below), which is
#:   what makes the before→after comparison answerable at all.
#: * ``candidate`` → :data:`~.journey.PHASE_MEASURE` — the per-driver anchor a
#:   candidate is built and graded from. Routed, not summed: a candidate is a
#:   per-driver claim and a summed curve cannot answer it.
#: * ``verify`` → :data:`~.journey.PHASE_VERIFY` — the "after" half of the same
#:   summed question the baseline asks.
PHASE_BY_MEASURE_KIND = {
    MEASURE_KIND_BASELINE: PHASE_ENTRY_BASELINE,
    MEASURE_KIND_CANDIDATE: PHASE_MEASURE,
    MEASURE_KIND_VERIFY: PHASE_VERIFY,
}


def phase_for_measurement(kind: str) -> str:
    """The flow phase whose program this measurement kind plays.

    Takes the kind alone. The regime is deliberately not a parameter: it is a
    capture geometry, and a parameter nothing reads would invite a future
    caller to believe it were consulted.
    """
    try:
        return PHASE_BY_MEASURE_KIND[kind]
    except KeyError:
        raise NoPhaseForMeasurementError(kind) from None
