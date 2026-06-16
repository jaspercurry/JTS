"""Driver-aware protection and closed-loop level policy.

This module is intentionally deterministic and side-effect free. It decides
whether a commissioning tone may be considered for a driver role/style, and
how a mic observation should move the separate commissioning test level. It
does not play audio, write CamillaDSP state, or persist level changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ._common import issue as _issue
from .calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    MAX_TEST_LEVEL_DBFS,
    MIC_USABLE_MAX_DBFS,
    MIC_USABLE_MIN_DBFS,
    MIN_TEST_LEVEL_DBFS,
    TEST_LEVEL_STEP_DB,
    clamp_test_level_dbfs,
    classify_mic_meter,
)

SCHEMA_VERSION = 1
DRIVER_PROTECTION_KIND = "jts_active_speaker_driver_protection"
AUTO_LEVEL_DECISION_KIND = "jts_active_speaker_auto_level_decision"
DRIVER_PROTECTION_POLICY_VERSION = "driver_protection_auto_level_v1"

LOW_FREQUENCY_ROLES = frozenset({"woofer", "mid", "subwoofer"})
HIGH_FREQUENCY_ROLES = frozenset({"tweeter"})
SUPPORTED_AUDIBLE_ROLES = LOW_FREQUENCY_ROLES | HIGH_FREQUENCY_ROLES

_UNKNOWN_HF_STYLE = "unknown_high_frequency"
_STYLE_HIGH_PASS_HZ = {
    "compression_driver": 2000.0,
    "horn_compression_driver": 2000.0,
    "dome_tweeter": 3000.0,
    "amt_tweeter": 3000.0,
    "planar_tweeter": 3500.0,
    "ribbon_tweeter": 5000.0,
    "supertweeter": 8000.0,
    _UNKNOWN_HF_STYLE: 5000.0,
}


@dataclass(frozen=True)
class DriverProtectionProfile:
    role: str
    role_class: str
    driver_style: str | None
    min_highpass_hz: float | None
    floor_test_frequency_hz: float
    floor_test_duration_ms: int
    max_auto_level_dbfs: float
    requires_floor_confirmation_above_floor: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "role_class": self.role_class,
            "driver_style": self.driver_style,
            "min_highpass_hz": self.min_highpass_hz,
            "floor_test_frequency_hz": self.floor_test_frequency_hz,
            "floor_test_duration_ms": self.floor_test_duration_ms,
            "max_auto_level_dbfs": self.max_auto_level_dbfs,
            "requires_floor_confirmation_above_floor": (
                self.requires_floor_confirmation_above_floor
            ),
        }


def normalise_driver_role(role: Any) -> str:
    return str(role or "").strip().lower()


def normalise_driver_style(style: Any) -> str | None:
    if style is None:
        return None
    token = str(style or "").strip().lower().replace("-", "_").replace(" ", "_")
    return token or None


def driver_protection_profile(
    role: Any,
    *,
    driver_style: Any = None,
) -> DriverProtectionProfile:
    """Return conservative commissioning bounds for one driver target."""

    role_id = normalise_driver_role(role)
    style = normalise_driver_style(driver_style)
    if role_id in LOW_FREQUENCY_ROLES:
        if role_id == "subwoofer":
            frequency = 50.0
            duration_ms = 300
        elif role_id == "mid":
            frequency = 800.0
            duration_ms = 300
        else:
            frequency = 120.0
            duration_ms = 300
        return DriverProtectionProfile(
            role=role_id,
            role_class="low_frequency",
            driver_style=style,
            min_highpass_hz=None,
            floor_test_frequency_hz=frequency,
            floor_test_duration_ms=duration_ms,
            max_auto_level_dbfs=MAX_TEST_LEVEL_DBFS,
            requires_floor_confirmation_above_floor=True,
        )
    if role_id in HIGH_FREQUENCY_ROLES:
        hf_style = style or _UNKNOWN_HF_STYLE
        min_highpass = _STYLE_HIGH_PASS_HZ.get(hf_style, _STYLE_HIGH_PASS_HZ[_UNKNOWN_HF_STYLE])
        return DriverProtectionProfile(
            role=role_id,
            role_class="high_frequency",
            driver_style=hf_style,
            min_highpass_hz=min_highpass,
            floor_test_frequency_hz=max(min_highpass, 3000.0),
            floor_test_duration_ms=100,
            max_auto_level_dbfs=-65.0,
            requires_floor_confirmation_above_floor=True,
        )
    return DriverProtectionProfile(
        role=role_id,
        role_class="unsupported",
        driver_style=style,
        min_highpass_hz=None,
        floor_test_frequency_hz=500.0,
        floor_test_duration_ms=300,
        max_auto_level_dbfs=MIN_TEST_LEVEL_DBFS,
        requires_floor_confirmation_above_floor=True,
    )


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _current_level(calibration_level: dict[str, Any] | None) -> float:
    if not isinstance(calibration_level, dict):
        return MIN_TEST_LEVEL_DBFS
    test_signal = (
        calibration_level.get("test_signal")
        if isinstance(calibration_level.get("test_signal"), dict)
        else {}
    )
    return clamp_test_level_dbfs(test_signal.get("requested_level_dbfs"))


def _level_at_floor(level: float) -> bool:
    return level <= MIN_TEST_LEVEL_DBFS + 1e-6


def _band_highpass_hz(band_limit: Any) -> float | None:
    if not isinstance(band_limit, dict):
        return None
    if band_limit.get("type") not in {"highpass", "bandpass"}:
        return None
    return _finite_float(band_limit.get("highpass_hz"))


def _highpass_satisfied(
    *,
    profile: DriverProtectionProfile,
    band_limit: Any,
) -> bool:
    if profile.min_highpass_hz is None:
        return True
    highpass = _band_highpass_hz(band_limit)
    return highpass is not None and highpass >= profile.min_highpass_hz


def driver_protection_payload(
    role: Any,
    *,
    driver_style: Any = None,
    protection_status: Any = None,
    band_limit: Any = None,
) -> dict[str, Any]:
    """Return the protection envelope for one target.

    ``audio_allowed`` means the driver role/style has enough deterministic
    protection evidence to be considered by higher-level readiness gates. It
    does not bypass safe-session, backend, floor-confirmation, or Stop checks.
    """

    profile = driver_protection_profile(role, driver_style=driver_style)
    status = str(protection_status or "").strip().lower()
    issues: list[dict[str, str]] = []
    if profile.role_class == "unsupported":
        issues.append(_issue(
            "blocker",
            "driver_role_not_supported",
            "this driver role is not enabled for active-speaker audible tests",
        ))
    if profile.role_class == "high_frequency":
        if status not in {"present", "software_guard_requested"}:
            issues.append(_issue(
                "blocker",
                "high_frequency_protection_missing",
                "high-frequency drivers require marked physical protection or software-guarded bring-up",
            ))
        if not _highpass_satisfied(profile=profile, band_limit=band_limit):
            issues.append(_issue(
                "blocker",
                "high_frequency_highpass_missing",
                "high-frequency driver tone requires a protective high-pass band limit",
            ))
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": DRIVER_PROTECTION_KIND,
        "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
        **profile.to_dict(),
        "protection_status": status or None,
        "band_limit_highpass_ok": _highpass_satisfied(
            profile=profile,
            band_limit=band_limit,
        ),
        "audio_allowed": not issues and profile.role_class in {
            "low_frequency",
            "high_frequency",
        },
        "issues": issues,
    }


def _meter_from_inputs(
    *,
    calibration_level: dict[str, Any] | None,
    observed_mic_dbfs: Any = None,
    mic_clipping: bool = False,
) -> dict[str, Any]:
    if observed_mic_dbfs is not None or mic_clipping:
        return classify_mic_meter(
            observed_dbfs=observed_mic_dbfs,
            clipping=mic_clipping,
        )
    if isinstance(calibration_level, dict) and isinstance(
        calibration_level.get("mic_meter"),
        dict,
    ):
        return dict(calibration_level["mic_meter"])
    return classify_mic_meter()


def auto_level_decision(
    calibration_level: dict[str, Any] | None,
    *,
    role: Any,
    driver_style: Any = None,
    protection_status: Any = None,
    band_limit: Any = None,
    observed_mic_dbfs: Any = None,
    mic_clipping: bool = False,
    floor_audio_confirmed: bool = False,
    stop_control_available: bool = True,
) -> dict[str, Any]:
    """Return one bounded closed-loop level decision.

    The decision is deliberately one bounded ramp step only. Callers that
    persist state must run this again after each observed tone, which keeps the
    loop interruptible and makes every upward move inspectable without forcing
    one-dB discovery clicks.
    """

    protection = driver_protection_payload(
        role,
        driver_style=driver_style,
        protection_status=protection_status,
        band_limit=band_limit,
    )
    profile = driver_protection_profile(role, driver_style=driver_style)
    current = _current_level(calibration_level)
    meter = _meter_from_inputs(
        calibration_level=calibration_level,
        observed_mic_dbfs=observed_mic_dbfs,
        mic_clipping=mic_clipping,
    )
    meter_status = str(meter.get("status") or "unmeasured")
    max_level = min(MAX_TEST_LEVEL_DBFS, profile.max_auto_level_dbfs)
    issues = [issue for issue in protection["issues"] if isinstance(issue, dict)]
    if not stop_control_available:
        issues.append(_issue(
            "blocker",
            "stop_control_required",
            "closed-loop active-speaker level changes require Stop to be available",
        ))

    action = "hold"
    status = "blocked" if any(issue.get("severity") == "blocker" for issue in issues) else "hold"
    next_level = current
    reason = "level held"

    if meter_status == "clipping":
        action = "reset_to_floor"
        status = "reset"
        next_level = MIN_TEST_LEVEL_DBFS
        reason = "microphone clipped; reset to the quietest setting"
    elif issues:
        action = "hold"
        status = "blocked"
        reason = "selected driver is not ready for a quiet test"
    elif meter_status == "too_loud":
        action = "lower"
        status = "lower"
        next_level = max(MIN_TEST_LEVEL_DBFS, current - TEST_LEVEL_STEP_DB)
        reason = "microphone reading is too loud"
    elif meter_status in {"too_quiet", "low"}:
        if (
            profile.requires_floor_confirmation_above_floor
            and not floor_audio_confirmed
        ):
            action = "hold_for_floor_confirmation"
            status = "waiting_for_floor_confirmation"
            next_level = current
            reason = "quietest-level audio must be confirmed before raising"
        elif current >= max_level - 1e-6:
            action = "hold_at_cap"
            status = "maxed"
            next_level = max_level
            reason = "driver-specific auto-level cap reached"
            issues.append(_issue(
                "warning",
                "auto_level_cap_reached",
                "mic target was not reached before the driver-specific level cap",
            ))
        else:
            action = "raise"
            status = "raise"
            next_level = min(current + AUDIBLE_RAMP_STEP_DB, max_level)
            reason = "microphone reading is below the usable window"
    elif meter_status == "usable":
        action = "hold"
        status = "locked"
        next_level = current
        reason = "microphone reading is in the usable window"
    elif meter_status == "unmeasured":
        if (
            profile.requires_floor_confirmation_above_floor
            and not floor_audio_confirmed
        ):
            action = "hold_for_floor_confirmation"
            status = "waiting_for_floor_confirmation"
            next_level = current
            reason = "quietest-level audio must be confirmed before raising"
        elif current >= max_level - 1e-6:
            action = "hold_at_cap"
            status = "maxed"
            next_level = max_level
            reason = "driver-specific auto-level cap reached"
            issues.append(_issue(
                "warning",
                "auto_level_cap_reached",
                "operator-controlled raise reached the driver-specific level cap",
            ))
        else:
            action = "raise"
            status = "raise"
            next_level = min(current + AUDIBLE_RAMP_STEP_DB, max_level)
            reason = "operator-controlled raise toward audible"

    next_level = clamp_test_level_dbfs(next_level)
    if next_level > max_level:
        next_level = max_level
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": AUTO_LEVEL_DECISION_KIND,
        "policy_version": DRIVER_PROTECTION_POLICY_VERSION,
        "status": status,
        "action": action,
        "reason": reason,
        "current_level_dbfs": current,
        "next_level_dbfs": next_level,
        "applied_delta_db": round(next_level - current, 3),
        "max_auto_level_dbfs": max_level,
        "step_db": AUDIBLE_RAMP_STEP_DB,
        "manual_step_db": TEST_LEVEL_STEP_DB,
        "mic_meter": {
            **meter,
            "usable_min_dbfs": MIC_USABLE_MIN_DBFS,
            "usable_max_dbfs": MIC_USABLE_MAX_DBFS,
        },
        "floor_audio_confirmed": bool(floor_audio_confirmed),
        "stop_control_available": bool(stop_control_available),
        "driver_protection": protection,
        "issues": issues,
    }
