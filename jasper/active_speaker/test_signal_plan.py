"""Preset-derived driver test signal selection for active speakers.

This module owns the acoustic-band policy for quiet commissioning tones. It is
side-effect free: callers get a bounded plan or a blocker issue, never samples
or hardware access.
"""

from __future__ import annotations

import math
from typing import Any

from ._common import issue as _issue
from .driver_protection import driver_protection_payload, driver_protection_profile
from .profile import ActiveSpeakerPreset, crossover_edges_for_role

DRIVER_TEST_SIGNAL_PLAN_KIND = "jts_active_speaker_driver_test_signal_plan"
PROTECTIVE_TWEETER_HP_MULTIPLIER = 2.0
EDGE_MARGIN_RATIO = 1.25
SUBWOOFER_SUBSONIC_FLOOR_HZ = 25.0
MIN_DRIVER_TEST_FREQUENCY_HZ = 20.0
MAX_DRIVER_TEST_FREQUENCY_HZ = 20_000.0


def protective_tweeter_highpass_frequency_hz(
    preset: ActiveSpeakerPreset,
    role: str,
) -> float | None:
    """Return the extra commissioning tweeter high-pass emitted in CamillaDSP."""

    if str(role or "").strip().lower() != "tweeter":
        return None
    fc_values = [
        region.fc_hz
        for region in preset.crossover_regions
        if region.upper_driver == "tweeter"
    ]
    if not fc_values:
        return None
    return max(fc_values) * PROTECTIVE_TWEETER_HP_MULTIPLIER


def _finite_positive(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def _edge(
    *,
    kind: str,
    frequency_hz: Any,
    source: str,
    reason: str,
    direction: str,
) -> dict[str, Any] | None:
    frequency = _finite_positive(frequency_hz)
    if frequency is None:
        return None
    return {
        "kind": kind,
        "direction": direction,
        "frequency_hz": frequency,
        "source": source,
        "reason": reason,
    }


def _band_type(highpass_hz: float | None, lowpass_hz: float | None) -> str:
    if highpass_hz is not None and lowpass_hz is not None:
        return "bandpass"
    if highpass_hz is not None:
        return "highpass"
    if lowpass_hz is not None:
        return "lowpass"
    return "unknown"


def _round_frequency(value: float) -> float:
    return round(float(value), 1)


def _candidate_frequency(
    *,
    role: str,
    profile_floor_hz: float,
    highpass_hz: float | None,
    lowpass_hz: float | None,
    minimum_tone_hz: float,
    maximum_tone_hz: float,
) -> tuple[float | None, str]:
    if highpass_hz is not None and lowpass_hz is not None:
        preferred = math.sqrt(highpass_hz * lowpass_hz)
        if minimum_tone_hz < preferred < maximum_tone_hz:
            return preferred, "geometric_mean_of_passband_edges"
        return math.sqrt(minimum_tone_hz * maximum_tone_hz), (
            "geometric_mean_of_margin_bounded_band"
        )
    if lowpass_hz is not None:
        preferred = lowpass_hz / 2.0
        return min(max(preferred, minimum_tone_hz), maximum_tone_hz), (
            "one_octave_below_lowpass_edge"
        )
    if highpass_hz is not None:
        preferred = max(profile_floor_hz, minimum_tone_hz)
        if preferred >= maximum_tone_hz:
            return None, "highpass_floor_exceeds_frequency_ceiling"
        return preferred, "above_strictest_highpass_edge"
    return None, f"{role}_has_no_preset_band_edges"


def driver_test_signal_plan(
    preset: ActiveSpeakerPreset,
    role: str,
    *,
    driver_style: Any = None,
    edge_margin_ratio: float = EDGE_MARGIN_RATIO,
) -> dict[str, Any]:
    """Return a safe preset-derived tone frequency for one driver role.

    The passband is derived from the active preset crossover edges plus
    code-owned protection policy. The returned tone is inside the margin-bounded
    band; if no such tone exists, the plan is blocked and must not be played.
    """

    role_id = str(role or "").strip().lower()
    lower_edge_hz, upper_edge_hz = crossover_edges_for_role(preset, role_id)
    return driver_test_signal_plan_from_edges(
        role_id,
        crossover_highpass_hz=lower_edge_hz,
        crossover_lowpass_hz=upper_edge_hz,
        protective_highpass_hz=protective_tweeter_highpass_frequency_hz(
            preset,
            role_id,
        ),
        driver_style=driver_style,
        edge_margin_ratio=edge_margin_ratio,
        crossover_edge_source="preset.crossover_regions",
        protective_edge_source="active_speaker.test_signal_plan",
    )


def driver_test_signal_plan_from_edges(
    role: str,
    *,
    crossover_highpass_hz: Any = None,
    crossover_lowpass_hz: Any = None,
    protective_highpass_hz: Any = None,
    driver_style: Any = None,
    edge_margin_ratio: float = EDGE_MARGIN_RATIO,
    crossover_edge_source: str = "compiled_active_speaker_edges",
    protective_edge_source: str = "compiled_active_speaker_edges",
) -> dict[str, Any]:
    """Return a safe tone from already-compiled driver band edges.

    This is the policy seam for future compiled shapes, including subwoofer
    add-ons once the product flow emits their low-pass edge. The current
    production adapter above feeds it from ``ActiveSpeakerPreset`` for 2/3-way
    main speakers.
    """

    role_id = str(role or "").strip().lower()
    margin = _finite_positive(edge_margin_ratio) or EDGE_MARGIN_RATIO
    profile = driver_protection_profile(role_id, driver_style=driver_style)

    highpass_edges: list[dict[str, Any]] = []
    lowpass_edges: list[dict[str, Any]] = []

    crossover_hp = _edge(
        kind="crossover_highpass",
        frequency_hz=crossover_highpass_hz,
        source=crossover_edge_source,
        reason=f"{role_id} is the upper driver of an active crossover",
        direction="highpass",
    )
    if crossover_hp is not None:
        highpass_edges.append(crossover_hp)

    crossover_lp = _edge(
        kind="crossover_lowpass",
        frequency_hz=crossover_lowpass_hz,
        source=crossover_edge_source,
        reason=f"{role_id} is the lower driver of an active crossover",
        direction="lowpass",
    )
    if crossover_lp is not None:
        lowpass_edges.append(crossover_lp)

    protective_hp = _edge(
        kind="protective_tweeter_highpass",
        frequency_hz=protective_highpass_hz,
        source=protective_edge_source,
        reason=(
            "extra tweeter commissioning high-pass emitted in the Camilla graph "
            f"at {PROTECTIVE_TWEETER_HP_MULTIPLIER:g}x the strictest tweeter crossover"
        ),
        direction="highpass",
    )
    if protective_hp is not None:
        highpass_edges.append(protective_hp)

    protection_hp = _edge(
        kind="driver_protection_minimum",
        frequency_hz=profile.min_highpass_hz,
        source="driver_protection_profile",
        reason="driver protection policy recommended minimum test frequency",
        direction="highpass",
    )
    if protection_hp is not None:
        highpass_edges.append(protection_hp)

    subsonic_floor = _edge(
        kind="subwoofer_subsonic_floor",
        frequency_hz=SUBWOOFER_SUBSONIC_FLOOR_HZ if role_id == "subwoofer" else None,
        source="active_speaker.test_signal_plan",
        reason="subwoofer tests stay above the commissioning subsonic floor",
        direction="highpass",
    )
    if subsonic_floor is not None:
        highpass_edges.append(subsonic_floor)

    highpass_hz = (
        max(edge["frequency_hz"] for edge in highpass_edges)
        if highpass_edges else None
    )
    lowpass_hz = (
        min(edge["frequency_hz"] for edge in lowpass_edges)
        if lowpass_edges else None
    )
    minimum_tone_hz = (
        max(MIN_DRIVER_TEST_FREQUENCY_HZ, highpass_hz * margin)
        if highpass_hz is not None else MIN_DRIVER_TEST_FREQUENCY_HZ
    )
    maximum_tone_hz = (
        min(MAX_DRIVER_TEST_FREQUENCY_HZ, lowpass_hz / margin)
        if lowpass_hz is not None else MAX_DRIVER_TEST_FREQUENCY_HZ
    )

    issues: list[dict[str, str]] = []
    candidate_hz: float | None
    reason: str
    if not highpass_edges and not lowpass_edges:
        candidate_hz = None
        reason = "missing_preset_band_edges"
        issues.append(_issue(
            "blocker",
            "driver_test_signal_edges_missing",
            "no crossover or protection edge defines a safe test band",
        ))
    elif minimum_tone_hz >= maximum_tone_hz:
        candidate_hz = None
        reason = "empty_margin_bounded_band"
        issues.append(_issue(
            "blocker",
            "driver_test_signal_no_safe_band",
            "crossover/protection edges leave no frequency inside the driver "
            "test band after safety margin",
        ))
    else:
        candidate_hz, reason = _candidate_frequency(
            role=role_id,
            profile_floor_hz=profile.floor_test_frequency_hz,
            highpass_hz=highpass_hz,
            lowpass_hz=lowpass_hz,
            minimum_tone_hz=minimum_tone_hz,
            maximum_tone_hz=maximum_tone_hz,
        )
        if candidate_hz is None or not (
            minimum_tone_hz <= candidate_hz <= maximum_tone_hz
        ):
            issues.append(_issue(
                "blocker",
                "driver_test_signal_no_safe_frequency",
                "could not choose a test tone inside the preset-derived safe band",
            ))

    frequency_hz = _round_frequency(candidate_hz) if candidate_hz is not None else None
    band_type = _band_type(highpass_hz, lowpass_hz)
    band_limit: dict[str, Any] = {"type": band_type}
    if highpass_hz is not None:
        band_limit["highpass_hz"] = highpass_hz
    if lowpass_hz is not None:
        band_limit["lowpass_hz"] = lowpass_hz

    driver_protection = driver_protection_payload(
        role_id,
        driver_style=driver_style,
        protection_status=(
            "software_guard_requested"
            if profile.role_class == "high_frequency" else None
        ),
        band_limit=band_limit if band_type != "unknown" else None,
    )
    issues.extend(
        issue
        for issue in driver_protection.get("issues", [])
        if isinstance(issue, dict)
    )

    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_TEST_SIGNAL_PLAN_KIND,
        "status": (
            "blocked"
            if any(issue.get("severity") == "blocker" for issue in issues)
            else "ready"
        ),
        "role": role_id,
        "driver_style": profile.driver_style,
        "frequency_hz": frequency_hz,
        "selection_reason": reason,
        "band_limit": band_limit,
        "allowed_band": {
            "type": band_type,
            "highpass_hz": highpass_hz,
            "lowpass_hz": lowpass_hz,
            "minimum_tone_hz": _round_frequency(minimum_tone_hz),
            "maximum_tone_hz": _round_frequency(maximum_tone_hz),
            "edge_margin_ratio": margin,
            "edges": [*highpass_edges, *lowpass_edges],
        },
        "driver_protection": driver_protection,
        "issues": issues,
    }
