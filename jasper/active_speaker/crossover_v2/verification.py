# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The post-apply grade's household-facing result codes."""

from __future__ import annotations

__all__ = [
    "RESULT_INCONCLUSIVE",
    "RESULT_KEEP_PREVIOUS",
    "RESULT_VERIFIED_BEST_EVALUATED",
    "RESULT_VERIFIED_TARGET",
]


#: What the household is told a graded round came to. The domain owns these
#: four even though the web host picks one: the renderer
#: (:mod:`jasper.active_speaker.crossover_envelope_v2`) may not import
#: :mod:`jasper.web`, so both sides import the symbol from here.
RESULT_VERIFIED_TARGET = "verified_target"
RESULT_VERIFIED_BEST_EVALUATED = "verified_best_evaluated"
RESULT_KEEP_PREVIOUS = "keep_previous"
#: Shares its value with the host's ``GRADE_INCONCLUSIVE``, which answers the
#: neighbouring question ("did the check finish?") about the same round. A bare
#: ``"inconclusive"`` in the renderer therefore cannot be attributed to one of
#: the two by its value alone.
RESULT_INCONCLUSIVE = "inconclusive"
