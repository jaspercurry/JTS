# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small numeric coercions shared by correction evidence serializers."""
from __future__ import annotations

import math
from typing import Any


def round_finite(value: Any, digits: int = 2) -> float | None:
    """Return a rounded finite float, or ``None`` for unusable input."""

    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return round(out, digits)
