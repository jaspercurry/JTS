# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Verify the USB resampler is warm (at its churn-safe floor) before a
route-latency capture.

A cold session starts at the resampler's acquisition ceiling and descends to
the 576-frame floor over several minutes; measuring before it reaches
the floor records the descent, not the certified steady state. This module
reads the same fan-in ``STATUS`` shape the manual checklist
already names (``held_target_frames``, ``decay.frozen_reason``) and reduces
it to one warm/cold verdict.

Exposed as the harness's ``warm-check`` subcommand
(:mod:`jasper.cli.route_latency_harness`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# The hardware-validated churn-safe floor (see
# `DEFAULT_CUSHION_DECAY_FLOOR_FRAMES` in `rust/jasper-fanin/src/config.rs`)
# and the `decay.frozen_reason` STATUS value that means "settled at it, not
# just passing through" (`DecayFrozenReason::AtFloor` in
# `rust/jasper-fanin/src/lane_resampler.rs`, serialized in `state.rs`).
EXPECTED_HELD_TARGET_FRAMES = 576
WARM_FROZEN_REASON = "at_floor"


@dataclass(frozen=True)
class WarmVerdict:
    """One warm/cold verdict for the USB resampler lane."""

    locked: Any
    held_target_frames: Any
    frozen_reason: Any
    warm: bool

    def to_status_dict(self) -> dict[str, Any]:
        """The on-box CLI's printed JSON shape (unchanged key names —
        ``held``, not ``held_target_frames`` — from the original probe)."""

        return {
            "locked": self.locked,
            "held": self.held_target_frames,
            "frozen_reason": self.frozen_reason,
            "warm": self.warm,
        }


def find_usb_lane(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the USB input lane from a fan-in ``STATUS`` reply.

    Matches ``label == "usbsink"`` or a lane name containing ``"usb"``.
    Raises ``LookupError`` when no input lane matches — an unreachable or
    misconfigured box, not a warm/cold distinction the caller should try to
    paper over.
    """

    inputs = status.get("inputs")
    if not isinstance(inputs, list):
        raise LookupError("fan-in STATUS reply has no 'inputs' list")
    for item in inputs:
        if not isinstance(item, Mapping):
            continue
        if item.get("label") == "usbsink" or "usb" in str(item.get("lane", "")):
            return item
    raise LookupError("no usbsink input lane in fan-in STATUS reply")


def warm_verdict(status: Mapping[str, Any]) -> WarmVerdict:
    """Compute the warm/cold verdict from a fan-in ``STATUS`` reply.

    Warm requires all three: the lane's resampler reports ``locked``, its
    ``held_target_frames`` equals the hardware-validated floor (576), and
    ``decay.frozen_reason`` is ``"at_floor"`` (not e.g. ``"prime_hold"``,
    which means a compliance proof is live but decay hasn't settled yet).
    """

    lane = find_usb_lane(status)
    resampler = lane.get("resampler")
    resampler = resampler if isinstance(resampler, Mapping) else {}
    decay = resampler.get("decay")
    decay = decay if isinstance(decay, Mapping) else {}

    locked = resampler.get("locked")
    held = resampler.get("held_target_frames")
    frozen_reason = decay.get("frozen_reason")
    warm = bool(
        locked
        and held == EXPECTED_HELD_TARGET_FRAMES
        and frozen_reason == WARM_FROZEN_REASON
    )
    return WarmVerdict(
        locked=locked,
        held_target_frames=held,
        frozen_reason=frozen_reason,
        warm=warm,
    )


__all__ = [
    "EXPECTED_HELD_TARGET_FRAMES",
    "WARM_FROZEN_REASON",
    "WarmVerdict",
    "find_usb_lane",
    "warm_verdict",
]
