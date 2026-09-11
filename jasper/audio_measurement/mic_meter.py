# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Advisory microphone levels in capture dBFS, independent of the test executor."""

from __future__ import annotations

from typing import Any

from jasper.json_fields import finite_float

MIC_TOO_QUIET_BELOW_DBFS = -55.0
MIC_USABLE_MIN_DBFS = -45.0
MIC_USABLE_MAX_DBFS = -18.0


def classify_mic_meter(
    *,
    observed_dbfs: Any = None,
    clipping: bool = False,
) -> dict[str, Any]:
    """Classify a future microphone meter reading into coarse guidance.

    The thresholds are intentionally in capture dBFS, not SPL. SPL depends on
    microphone sensitivity and calibration provenance, while clipping/usable
    capture headroom is the first safety signal this contract can own
    deterministically.
    """

    try:
        observed = finite_float(float(observed_dbfs))
    except (TypeError, ValueError, OverflowError):
        observed = None
    if clipping:
        return {
            "status": "clipping",
            "tone": "danger",
            "observed_dbfs": observed,
            "recommendation": "stop_or_lower",
        }
    if observed is None:
        return {
            "status": "unmeasured",
            "tone": "idle",
            "observed_dbfs": None,
            "recommendation": "start_at_minimum",
        }
    if observed < MIC_TOO_QUIET_BELOW_DBFS:
        status = "too_quiet"
        tone = "warn"
        recommendation = "raise_slowly"
    elif observed < MIC_USABLE_MIN_DBFS:
        status = "low"
        tone = "warn"
        recommendation = "raise_slowly"
    elif observed <= MIC_USABLE_MAX_DBFS:
        status = "usable"
        tone = "ok"
        recommendation = "hold_level"
    else:
        status = "too_loud"
        tone = "danger"
        recommendation = "lower_level"
    return {
        "status": status,
        "tone": tone,
        "observed_dbfs": round(observed, 1),
        "recommendation": recommendation,
    }
