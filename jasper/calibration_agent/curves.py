# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared coercion for calibration-agent curve payloads."""

from __future__ import annotations

from typing import Any


def curve_values(curve: Any) -> tuple[Any, Any] | None:
    """Return ``(freqs_hz, magnitude_db)`` from a CurveJSON object or dict."""
    if curve is None:
        return None
    freqs = getattr(curve, "freqs_hz", None)
    mags = getattr(curve, "magnitude_db", None)
    if freqs is None and isinstance(curve, dict):
        freqs = curve.get("freqs_hz")
        mags = curve.get("magnitude_db")
    if freqs is None or mags is None:
        return None
    return freqs, mags
