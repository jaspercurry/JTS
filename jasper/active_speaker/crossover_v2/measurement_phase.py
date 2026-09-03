# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which stimulus one measurement plays — the engine's kind, in flow words.

The engine names a measurement kind (:data:`~.contracts.MEASURE_KINDS`); the
flow names a :mod:`~.journey` phase. This module is the only door between them.
A wrong row does not fail — it plays the wrong stimulus and banks a record that
looks correct, so the map is a pinned table and an unmapped kind refuses by
name. Capture regime is not an input: it says where the microphone sits, not
what the speaker emits.
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

#: The refusal code for a kind this map does not carry, in the spelling the
#: flow's reason registry uses.
UNMAPPED_MEASUREMENT_KIND = "unmapped_measurement_kind"


class NoPhaseForMeasurementError(RuntimeError):
    """This map carries no stimulus for that measurement kind."""

    def __init__(self, kind: str) -> None:
        super().__init__(
            f"no stimulus is mapped for measurement kind {kind!r}"
        )
        self.code = UNMAPPED_MEASUREMENT_KIND
        self.kind = kind


#: kind → the flow phase whose composed program that measurement plays. Each row
#: is a claim about which sound the speaker makes: ``baseline`` and ``verify``
#: share one summed program, which is what makes the before→after comparison
#: answerable; ``candidate`` is routed per-driver, because a summed curve cannot
#: answer a per-driver claim.
PHASE_BY_MEASURE_KIND = {
    MEASURE_KIND_BASELINE: PHASE_ENTRY_BASELINE,
    MEASURE_KIND_CANDIDATE: PHASE_MEASURE,
    MEASURE_KIND_VERIFY: PHASE_VERIFY,
}


def phase_for_measurement(kind: str) -> str:
    """The flow phase whose program this measurement kind plays."""
    try:
        return PHASE_BY_MEASURE_KIND[kind]
    except KeyError:
        raise NoPhaseForMeasurementError(kind) from None
