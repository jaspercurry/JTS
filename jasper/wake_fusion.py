# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-leg wake thresholds for each acoustic condition."""
from __future__ import annotations


class WakeFuser:
    def __init__(
        self, offsets: dict[tuple[str, str], float] | None = None,
    ) -> None:
        self._offsets = dict(offsets or {})

    def effective_threshold(
        self, leg_token: str, condition: str, base_threshold: float,
    ) -> float:
        return base_threshold + self._offsets.get((leg_token, condition), 0.0)
