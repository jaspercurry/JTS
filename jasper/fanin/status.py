# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pi-side helpers for interpreting jasper-fanin's ``STATUS`` JSON.

The Rust jasper-fanin daemon answers ``STATUS\\n`` on its control socket with a
JSON snapshot (per-input lanes, output transport, watchdog metrics). Several
Pi-side surfaces need to interpret that snapshot — jasper-control's ``/state``
aggregator, the route-latency harness, the mux source arbiter, jasper-doctor —
so the load-bearing field contracts live here, once, rather than as a copy of a
magic string in each caller.

Import-light on purpose (no daemon/socket I/O, no heavy deps) so any surface can
use it, including the socket-activated wizard process and CI without hardware.
"""
from __future__ import annotations

from typing import Any


# The STATUS input-lane ``source`` value on the USB DIRECT lane. Every
# aloop-reading lane serialises ``source:"lane"``; only the gadget-direct-capture
# lane serialises ``source:"direct"`` (rust/jasper-fanin/src/state.rs, pinned by
# its ``source":"direct"`` unit tests). This is the load-bearing combo-mode
# signal: on a USB "combo" box fan-in owns the gadget capture directly and the
# jasper-usbsink bridge stands by.
FANIN_INPUT_SOURCE_DIRECT = "direct"
USBSINK_INPUT_LABEL = "usbsink"


def fanin_usbsink_lane_is_direct(fanin_status: dict[str, Any] | None) -> bool:
    """True when fan-in's ``usbsink`` input lane is in DIRECT capture mode.

    This is the fan-in-side signal that a box is running USB "combo" mode: fan-in
    DIRECT-captures the UAC2 gadget (``source=="direct"``) and the jasper-usbsink
    bridge is in standby (opens no PCM, publishes frozen idle ``playing`` /
    ``rms_dbfs``). Callers that also hold the bridge ``state.json`` can treat its
    ``standby`` flag as an equivalent-by-design fallback.

    Fail-soft: a missing / malformed STATUS, an absent ``inputs`` array, or no
    ``usbsink`` lane all return ``False``."""
    if not isinstance(fanin_status, dict):
        return False
    inputs = fanin_status.get("inputs")
    if not isinstance(inputs, list):
        return False
    for entry in inputs:
        if (
            isinstance(entry, dict)
            and entry.get("label") == USBSINK_INPUT_LABEL
            and entry.get("source") == FANIN_INPUT_SOURCE_DIRECT
        ):
            return True
    return False


__all__ = [
    "FANIN_INPUT_SOURCE_DIRECT",
    "USBSINK_INPUT_LABEL",
    "fanin_usbsink_lane_is_direct",
]
