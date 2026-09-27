# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The crossover section a branch runs through, as a numpy-free leaf: a
contract that names one does not pay :mod:`.branch_chain`'s maths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CrossoverSection:
    """One Linkwitz-Riley section a branch runs through. ``order`` is the LR order the graph
    emits (:data:`jasper.active_speaker.profile.SUPPORTED_LR_ORDERS`).
    """

    fc_hz: float
    order: int
    highpass: bool
