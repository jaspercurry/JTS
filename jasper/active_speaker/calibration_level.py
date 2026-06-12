"""Calibration-level contract for active-speaker channel tests.

This module owns the tiny but load-bearing distinction between normal
listening volume and commissioning test-signal level. It does not play audio
or read microphones; it only clamps the requested test tone level and classifies
future microphone meter observations into coarse operator guidance.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CALIBRATION_LEVEL_KIND = "jts_active_speaker_calibration_level"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_calibration_level.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE"

MIN_TEST_LEVEL_DBFS = -80.0
DEFAULT_TEST_LEVEL_DBFS = MIN_TEST_LEVEL_DBFS
MAX_TEST_LEVEL_DBFS = -45.0
TEST_LEVEL_STEP_DB = 1.0
AUDIBLE_RAMP_STEP_DB = 6.0

MIC_TOO_QUIET_BELOW_DBFS = -55.0
MIC_USABLE_MIN_DBFS = -45.0
MIC_USABLE_MAX_DBFS = -18.0


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def _default_state_payload(path: Path) -> dict[str, Any]:
    payload = calibration_level_payload()
    payload.update({
        "updated_at": None,
        "last_action": "default_floor",
        "state_path": str(path),
    })
    return payload


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
        "status": "floor" if requested <= MIN_TEST_LEVEL_DBFS else "guarded",
        "test_signal": {
            "requested_level_dbfs": requested,
            "default_level_dbfs": DEFAULT_TEST_LEVEL_DBFS,
            "min_level_dbfs": MIN_TEST_LEVEL_DBFS,
            "max_level_dbfs": MAX_TEST_LEVEL_DBFS,
            "step_db": TEST_LEVEL_STEP_DB,
            "normal_system_volume_untouched": True,
        },
        "software_gain_guard": {
            "current_level_dbfs": requested,
            "floor_level_dbfs": MIN_TEST_LEVEL_DBFS,
            "max_level_dbfs": MAX_TEST_LEVEL_DBFS,
            "manual_step_db": TEST_LEVEL_STEP_DB,
            "audible_ramp_step_db": AUDIBLE_RAMP_STEP_DB,
            "upward_step_limit_db": AUDIBLE_RAMP_STEP_DB,
            "starts_at_floor": requested <= MIN_TEST_LEVEL_DBFS,
            "live_camilla_write": False,
            "future_camilla_volume_write_required": True,
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
            "backend_is_level_authority": True,
            "upward_steps_are_limited": True,
            "audible_ramp_step_is_bounded": True,
            "continuous_audio_ramp_requires_cancellable_backend": True,
            "requires_explicit_target": True,
            "requires_stop_control": True,
        },
        "issues": [],
    }


def load_calibration_level_state(
    *,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the persisted calibration level, defaulting to the safe floor."""

    path = _state_path(state_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_state_payload(path)
    if not isinstance(raw, dict):
        return _default_state_payload(path)
    test_signal = (
        raw.get("test_signal") if isinstance(raw.get("test_signal"), dict) else {}
    )
    level = clamp_test_level_dbfs(test_signal.get("requested_level_dbfs"))
    meter = raw.get("mic_meter") if isinstance(raw.get("mic_meter"), dict) else {}
    payload = calibration_level_payload(
        requested_level_dbfs=level,
        observed_mic_dbfs=meter.get("observed_dbfs"),
        mic_clipping=meter.get("status") == "clipping",
    )
    payload.update({
        "updated_at": raw.get("updated_at"),
        "last_action": raw.get("last_action") or "load",
        "state_path": str(path),
        "issues": [
            issue for issue in raw.get("issues", []) if isinstance(issue, dict)
        ],
    })
    return payload


def update_calibration_level_state(
    *,
    action: str = "set",
    requested_level_dbfs: Any = None,
    observed_mic_dbfs: Any = None,
    mic_clipping: bool = False,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Persist one guarded level transition.

    Upward motion remains backend-owned. Manual ``set`` stays one dB at a time;
    the product-facing audible ramp action may move by a larger bounded step so
    the user is not forced through dozens of clicks. Lowering and reset-to-floor
    remain unrestricted because they reduce risk.
    """

    path = _state_path(state_path)
    current_state = load_calibration_level_state(state_path=path)
    current = clamp_test_level_dbfs(
        (current_state.get("test_signal") or {}).get("requested_level_dbfs")
    )
    action_id = str(action or "set").strip().lower()
    issues: list[dict[str, str]] = []

    if mic_clipping:
        next_level = MIN_TEST_LEVEL_DBFS
        action_id = "clip_reset"
        issues.append({
            "severity": "warning",
            "code": "mic_clipping_reset_to_floor",
            "message": "microphone clipping reset calibration level to the floor",
        })
    elif action_id in {"reset", "floor", "stop"}:
        next_level = MIN_TEST_LEVEL_DBFS
    elif action_id == "observe":
        next_level = current
    elif action_id == "raise":
        next_level = min(current + TEST_LEVEL_STEP_DB, MAX_TEST_LEVEL_DBFS)
    elif action_id in {"ramp", "raise_toward_audible", "audible_ramp"}:
        requested = _finite_float(requested_level_dbfs)
        requested = current + AUDIBLE_RAMP_STEP_DB if requested is None else requested
        if requested > current + AUDIBLE_RAMP_STEP_DB:
            next_level = current + AUDIBLE_RAMP_STEP_DB
            issues.append({
                "severity": "warning",
                "code": "audible_ramp_step_limited",
                "message": (
                    "requested test level was above the bounded audible-ramp "
                    "step limit"
                ),
            })
        else:
            next_level = requested
    elif action_id == "lower":
        requested = _finite_float(requested_level_dbfs)
        if requested is None:
            next_level = max(current - TEST_LEVEL_STEP_DB, MIN_TEST_LEVEL_DBFS)
        else:
            next_level = min(clamp_test_level_dbfs(requested), current)
    elif action_id == "set":
        requested = clamp_test_level_dbfs(requested_level_dbfs)
        if requested > current + TEST_LEVEL_STEP_DB:
            next_level = current + TEST_LEVEL_STEP_DB
            issues.append({
                "severity": "warning",
                "code": "upward_step_limited",
                "message": (
                    "requested calibration level was above the one-step "
                    "software guard limit"
                ),
            })
        else:
            next_level = requested
    else:
        raise ValueError("unknown calibration level action")

    next_level = clamp_test_level_dbfs(next_level)
    payload = calibration_level_payload(
        requested_level_dbfs=next_level,
        observed_mic_dbfs=observed_mic_dbfs,
        mic_clipping=mic_clipping,
    )
    payload.update({
        "updated_at": _utc_now(),
        "last_action": action_id,
        "state_path": str(path),
        "prior_level_dbfs": current,
        "requested_level_dbfs": (
            _finite_float(requested_level_dbfs)
            if requested_level_dbfs is not None else None
        ),
        "applied_delta_db": round(next_level - current, 3),
        "issues": issues,
    })
    _atomic_write_json(path, payload)
    return payload
