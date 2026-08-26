# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Route-latency gates and live fan-in/outputd identity assessment."""
from __future__ import annotations

import json
import math
from typing import Any, Mapping

from .audio_runtime_plan import (
    USB_LOW_LATENCY_P95_BUDGET_MS as ROUTE_LATENCY_P95_BUDGET_MS,
    USB_LOW_LATENCY_P99_BUDGET_MS as ROUTE_LATENCY_P99_BUDGET_MS,
)
from .fanin.status import DIRECT_HEALTH_CAPTURING, DIRECT_HEALTH_IDLE


ROUTE_LATENCY_P95_MIN_DURATION_SECONDS = 5 * 60
ROUTE_LATENCY_P99_MIN_DURATION_SECONDS = 30 * 60
ROUTE_LATENCY_RERUN_ACTION = "run_route_latency_validation"
#: Codes about the PROOF's validity, not a measured breach; artifact staleness
#: and clock skew arrive here as config_mismatch. Disclosed, never failed —
#: ADR-0101.
ROUTE_LATENCY_DISCLOSURE_ISSUES = frozenset(
    {"config_mismatch", "p95_uncertified", "p99_uncertified"}
)
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def percentile_min_samples(percentile: float) -> int:
    """Return the minimum samples to certify a percentile.

    Rule: ``ceil(10 / (1 - percentile))``. Accepts either 0.95/0.99 or
    95/99. Values must be strictly between 0 and 1 after normalization.
    """

    p = float(percentile)
    if p > 1.0:
        p = p / 100.0
    if not 0.0 < p < 1.0:
        raise ValueError(f"percentile must be in (0, 1), got {percentile!r}")
    return int(math.ceil(10.0 / (1.0 - p)))


def certified_route_latency_percentiles(
    *,
    sample_count: int,
    duration_seconds: float,
    jittered_impulse_spacing: bool = False,
) -> tuple[int, ...]:
    """Return which route-latency percentiles this run may certify."""

    certified: list[int] = []
    if (
        sample_count >= percentile_min_samples(95)
        and duration_seconds >= ROUTE_LATENCY_P95_MIN_DURATION_SECONDS
    ):
        certified.append(95)
    if (
        sample_count >= percentile_min_samples(99)
        and duration_seconds >= ROUTE_LATENCY_P99_MIN_DURATION_SECONDS
        and jittered_impulse_spacing
    ):
        certified.append(99)
    return tuple(certified)


def _p99_sample_and_duration_sufficient(
    *,
    sample_count: int,
    duration_seconds: float,
) -> bool:
    return (
        sample_count >= percentile_min_samples(99)
        and duration_seconds >= ROUTE_LATENCY_P99_MIN_DURATION_SECONDS
    )


def route_latency_gate_status(
    *,
    p95_ms: float | None,
    p99_ms: float | None,
    sample_count: int,
    duration_seconds: float,
    jittered_impulse_spacing: bool = False,
    config_match: bool = True,
    route_health_ok: bool = True,
) -> tuple[str, str, tuple[int, ...], tuple[str, ...]]:
    """Classify a route-latency artifact using the production gate."""

    certified = certified_route_latency_percentiles(
        sample_count=sample_count,
        duration_seconds=duration_seconds,
        jittered_impulse_spacing=jittered_impulse_spacing,
    )
    issues: list[str] = []
    if not config_match:
        issues.append("config_mismatch")
    if not route_health_ok:
        issues.append("route_health_anomaly")
    if p95_ms is None:
        issues.append("p95_missing")
    elif p95_ms > ROUTE_LATENCY_P95_BUDGET_MS:
        issues.append(f"p95_exceeds_{ROUTE_LATENCY_P95_BUDGET_MS:g}ms")
    if 95 not in certified:
        issues.append("p95_uncertified")

    if issues:
        if all(issue in ROUTE_LATENCY_DISCLOSURE_ISSUES for issue in issues):
            return "warn", ROUTE_LATENCY_RERUN_ACTION, certified, tuple(issues)
        return "fail", "fix_route_latency_before_claim", certified, tuple(issues)

    if p99_ms is None:
        return "warn", "run_p99_promotion_validation", certified, ("p99_missing",)
    if 99 not in certified:
        if (
            _p99_sample_and_duration_sufficient(
                sample_count=sample_count,
                duration_seconds=duration_seconds,
            )
            and not jittered_impulse_spacing
        ):
            return "warn", "run_p99_promotion_validation", certified, (
                "p99_spacing_unverified",
            )
        return "warn", "run_p99_promotion_validation", certified, ("p99_uncertified",)
    if p99_ms > ROUTE_LATENCY_P99_BUDGET_MS:
        return "warn", "reduce_tail_latency_before_promotion", certified, (
            f"p99_exceeds_{ROUTE_LATENCY_P99_BUDGET_MS:g}ms",
        )
    return "pass", "usb_low_latency_route_promotable", certified, ()


def _normal_json(value: Any) -> JsonValue:
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _string_issues(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _route_identity_mismatches(
    observed: Mapping[str, Any],
    expected: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    issues: list[str] = []
    for key, expected_value in expected.items():
        if key not in observed:
            issues.append(f"identity_mismatch:{key}")
            continue
        if _normal_json(observed.get(key)) != _normal_json(expected_value):
            issues.append(f"identity_mismatch:{key}")
    return tuple(issues)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _fanin_input_status(
    fanin_status: Mapping[str, Any] | None,
    lane: str,
) -> Mapping[str, Any] | None:
    if not isinstance(fanin_status, Mapping):
        return None
    inputs = fanin_status.get("inputs")
    if not isinstance(inputs, list):
        return None
    for item in inputs:
        if isinstance(item, Mapping) and item.get("label") == lane:
            return item
    return None


def route_live_state_issues(
    expected_identity: Mapping[str, JsonValue],
    *,
    fanin_status: Mapping[str, Any] | None = None,
    allow_idle_direct_lane: bool = False,
) -> tuple[str, ...]:
    """Return live runtime mismatches that invalidate route-latency promotion.

    Route artifacts bind measurements to the intended route identity. Promotion
    also needs fan-in to be running that identity: the direct USB source,
    negotiated capture geometry, and resampler lock/target are live timing
    facts, not config promises. Artifact creation keeps the strict default
    because its measurement window must have a capturing, locked direct lane.
    Doctor may allow an idle direct lane because a stored certification remains
    valid while the host is not streaming; static identity and geometry are
    still checked in that state.
    """

    issues: list[str] = []

    direct_expected = _mapping_or_empty(
        expected_identity.get("fanin_direct_config"),
    )
    if direct_expected:
        lane = str(direct_expected.get("lane") or "usbsink")
        lane_status = _fanin_input_status(fanin_status, lane)
        if lane_status is None:
            issues.append(f"live_fanin_input_missing:{lane}")
        else:
            expected_source = str(direct_expected.get("source") or "direct")
            if lane_status.get("source") != expected_source:
                issues.append(f"live_fanin_direct_mismatch:{lane}:source")
            direct = lane_status.get("direct")
            if not isinstance(direct, Mapping):
                issues.append(f"live_fanin_direct_missing:{lane}")
            else:
                expected_device = str(direct_expected.get("device") or "")
                if expected_device and direct.get("device") != expected_device:
                    issues.append(f"live_fanin_direct_mismatch:{lane}:device")

                health = str(direct.get("health") or "unknown")
                idle_allowed = (
                    allow_idle_direct_lane and health == DIRECT_HEALTH_IDLE
                )
                if health != DIRECT_HEALTH_CAPTURING and not idle_allowed:
                    issues.append(f"live_fanin_direct_unhealthy:{lane}:{health}")

                expected_period = _int_or_none(
                    direct_expected.get("period_frames")
                )
                observed_period = _int_or_none(direct.get("period_frames"))
                if (
                    expected_period is not None
                    and observed_period != expected_period
                ):
                    issues.append(
                        f"live_fanin_direct_mismatch:{lane}:period_frames"
                    )

                minimum_buffer = _int_or_none(
                    direct_expected.get("min_buffer_frames")
                )
                observed_buffer = _int_or_none(direct.get("buffer_frames"))
                expected_buffer = _int_or_none(
                    direct_expected.get("negotiated_buffer_frames")
                )
                if (
                    expected_buffer is not None
                    and observed_buffer != expected_buffer
                ):
                    issues.append(
                        "live_fanin_direct_mismatch:"
                        f"{lane}:negotiated_buffer_frames"
                    )
                if (
                    minimum_buffer is not None
                    and (
                        observed_buffer is None
                        or observed_buffer < minimum_buffer
                    )
                ):
                    issues.append(
                        f"live_fanin_direct_mismatch:{lane}:buffer_frames"
                    )
                if (
                    direct_expected.get("buffer_period_aligned") is True
                    and observed_period is not None
                    and observed_period > 0
                    and observed_buffer is not None
                    and observed_buffer % observed_period != 0
                ):
                    issues.append(
                        f"live_fanin_direct_mismatch:{lane}:buffer_alignment"
                    )

    resampler_expected = _mapping_or_empty(
        expected_identity.get("fanin_resampler_config"),
    )
    if resampler_expected.get("enabled") is True:
        lane = str(resampler_expected.get("lane") or "usbsink")
        lane_status = _fanin_input_status(fanin_status, lane)
        if lane_status is None:
            issues.append(f"live_fanin_input_missing:{lane}")
        else:
            resampler = lane_status.get("resampler")
            if not isinstance(resampler, Mapping):
                issues.append(f"live_fanin_resampler_missing:{lane}")
            else:
                direct = lane_status.get("direct")
                lane_is_explicitly_idle = (
                    isinstance(direct, Mapping)
                    and direct.get("health") == DIRECT_HEALTH_IDLE
                )
                idle_unlock_allowed = (
                    allow_idle_direct_lane
                    and lane_is_explicitly_idle
                    and resampler.get("locked") is False
                )
                if resampler.get("locked") is not True and not idle_unlock_allowed:
                    issues.append(f"live_fanin_resampler_unlocked:{lane}")
                expected_target = _int_or_none(
                    resampler_expected.get("target_frames"),
                )
                cushion = _int_or_none(
                    resampler_expected.get("warmup_cushion_frames"),
                )
                if expected_target is not None and cushion is not None:
                    expected_target += cushion
                observed_target = _int_or_none(resampler.get("target_fill_frames"))
                if (
                    expected_target is not None
                    and observed_target != expected_target
                ):
                    issues.append(
                        f"live_fanin_resampler_mismatch:{lane}:target_fill_frames"
                    )

    return tuple(dict.fromkeys(issues))
