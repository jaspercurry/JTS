"""Calibration-level contract for active-speaker channel tests.

This module owns the tiny but load-bearing distinction between normal
listening volume and commissioning test-signal level. It does not play audio
or read microphones; it only clamps the requested test tone level and classifies
future microphone meter observations into coarse operator guidance.
"""

from __future__ import annotations

import math
from typing import Any

SCHEMA_VERSION = 1
CALIBRATION_LEVEL_KIND = "jts_active_speaker_calibration_level"

MIN_TEST_LEVEL_DBFS = -80.0
DEFAULT_TEST_LEVEL_DBFS = MIN_TEST_LEVEL_DBFS
MAX_TEST_LEVEL_DBFS = -45.0
TEST_LEVEL_STEP_DB = 1.0

MIC_TOO_QUIET_BELOW_DBFS = -55.0
MIC_USABLE_MIN_DBFS = -45.0
MIC_USABLE_MAX_DBFS = -18.0


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def clamp_test_level_dbfs(value: Any) -> float:
    """Clamp an operator-requested test level to the commissioning envelope."""

    out = _finite_float(value)
    if out is None:
        out = DEFAULT_TEST_LEVEL_DBFS
    return min(max(out, MIN_TEST_LEVEL_DBFS), MAX_TEST_LEVEL_DBFS)


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

    observed = _finite_float(observed_dbfs)
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


def calibration_level_payload(
    *,
    requested_level_dbfs: Any = DEFAULT_TEST_LEVEL_DBFS,
    observed_mic_dbfs: Any = None,
    mic_clipping: bool = False,
) -> dict[str, Any]:
    """Return the versioned calibration-level contract for UI and tone plans."""

    requested = clamp_test_level_dbfs(requested_level_dbfs)
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": CALIBRATION_LEVEL_KIND,
        "test_signal": {
            "requested_level_dbfs": requested,
            "default_level_dbfs": DEFAULT_TEST_LEVEL_DBFS,
            "min_level_dbfs": MIN_TEST_LEVEL_DBFS,
            "max_level_dbfs": MAX_TEST_LEVEL_DBFS,
            "step_db": TEST_LEVEL_STEP_DB,
            "normal_system_volume_untouched": True,
        },
        "mic_meter": {
            **classify_mic_meter(
                observed_dbfs=observed_mic_dbfs,
                clipping=mic_clipping,
            ),
            "usable_min_dbfs": MIC_USABLE_MIN_DBFS,
            "usable_max_dbfs": MIC_USABLE_MAX_DBFS,
            "too_quiet_below_dbfs": MIC_TOO_QUIET_BELOW_DBFS,
        },
        "safety": {
            "operator_controls_level": True,
            "jts_enforces_bounds": True,
            "start_at_minimum": True,
            "requires_explicit_target": True,
            "requires_stop_control": True,
        },
    }
