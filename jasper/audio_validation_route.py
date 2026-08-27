# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Live fan-in identity assessment for the declared low-latency route.

Compares what fan-in is actually running against the identity the audio
runtime plan says the route needs. This is a live comparison, never a stored
verdict — see ADR-0185.
"""
from __future__ import annotations

from typing import Any, Mapping

from .fanin.status import DIRECT_HEALTH_CAPTURING, DIRECT_HEALTH_IDLE


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


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
    """Return the ways live fan-in departs from the route's declared identity.

    The direct USB source, the negotiated capture geometry, and the resampler
    lock/target are live timing facts, not config promises, so the low-latency
    route is only actually installed when fan-in reports them.
    ``allow_idle_direct_lane`` relaxes the lock/health legs for a host that is
    simply not streaming (fan-in's own ``direct.health == "idle"``); static
    identity and geometry are still checked in that state.
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
