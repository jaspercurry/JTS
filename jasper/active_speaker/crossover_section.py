# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The crossover sections a branch runs through, as a numpy-free leaf: a
contract or an emitter that names them does not pay :mod:`.branch_chain`'s
maths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CrossoverSection:
    """One Linkwitz-Riley section a branch runs through. ``order`` is the LR order the graph
    emits (:data:`jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`).
    """

    fc_hz: float
    order: int
    highpass: bool


def sections_by_role(regions: Iterable[Any]) -> dict[str, tuple[CrossoverSection, ...]]:
    """Map each driver role to the Linkwitz-Riley sections its branch runs through, from a
    preset's crossover regions. The single derivation for both the session and the
    emitter, so the two cannot drift apart. A role with no region gets no sections (runs
    FULL RANGE in the emitted graph). ``regions`` are duck-typed on
    ``lower_driver``/``upper_driver``/``fc_hz``/``order``, mirroring
    ``camilla_yaml.filters._emit_baseline_driver_definitions``.
    """
    out: dict[str, list[CrossoverSection]] = {}
    for region in regions:
        fc_hz = float(getattr(region, "fc_hz", 0.0))
        order = int(getattr(region, "order", 0))
        lower = getattr(region, "lower_driver", None)
        upper = getattr(region, "upper_driver", None)
        if lower is not None:
            out.setdefault(str(lower), []).append(
                CrossoverSection(fc_hz=fc_hz, order=order, highpass=False)
            )
        if upper is not None:
            out.setdefault(str(upper), []).append(
                CrossoverSection(fc_hz=fc_hz, order=order, highpass=True)
            )
    return {role: tuple(sections) for role, sections in out.items()}
