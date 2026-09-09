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

from dataclasses import dataclass
from typing import Any

from jasper.platform.status_socket import FANIN_STATUS_SOCKET, read_status_socket


# The STATUS input-lane ``source`` value on the USB DIRECT lane. Every
# aloop-reading lane serialises ``source:"lane"``; only the gadget-direct-capture
# lane serialises ``source:"direct"`` (rust/jasper-fanin/src/state.rs, pinned by
# its ``source":"direct"`` unit tests). This is the load-bearing USB signal:
# fan-in owns the gadget capture directly as the sole live ingress owner.
FANIN_INPUT_SOURCE_DIRECT = "direct"
# The STATUS input-lane ``source`` value on a renderer-ingress SHM-ring lane
# (U3 / P6). A third alternative beside ``lane`` and ``direct``, not a layer on
# either: a ring lane opens no aloop substream and no gadget capture. Nothing in
# this module keys off it — the USB predicates below are deliberately bound to
# ``direct`` AND ``label=usbsink``, so a ring lane can never be mistaken for the
# USB one — but the token is named here because this module owns the vocabulary
# the Rust serializer publishes.
FANIN_INPUT_SOURCE_RING = "ring"
USBSINK_INPUT_LABEL = "usbsink"

# Stable ``direct.health`` tokens produced by rust/jasper-fanin.  Health is an
# instantaneous observability signal owned by the capture process; cumulative
# reopen counters describe successful self-heal activity and are deliberately
# not interpreted here as permission to add or remove the USB function.
DIRECT_HEALTH_CAPTURING = "capturing"
DIRECT_HEALTH_IDLE = "idle"
DIRECT_HEALTH_BROKEN = "broken"


@dataclass(frozen=True)
class DirectHealthSample:
    """The identity-bound USB DIRECT lane's status projection.

    ``reopens`` and ``card_gen_reopens`` are telemetry: a climb means fan-in
    closed and reopened a capture handle.  It does not mean the recovery failed.
    Callers that need readiness use ``present`` plus ``health``; lifecycle intent
    remains owned by the source coordinator.
    """

    present: bool
    health: str
    reopens: int
    card_gen_reopens: int
    frames_read: int


def _as_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def extract_direct_sample(
    fanin_status: dict[str, Any] | None,
) -> DirectHealthSample | None:
    """Return the USB DIRECT lane's health projection, or ``None``.

    The selection is bound to both ``label=usbsink`` and ``source=direct`` so an
    ordinary ALoop lane or a future direct lane cannot be mistaken for this one.
    Missing and malformed snapshots fail soft.
    """

    entry = fanin_usbsink_input(fanin_status)
    if not entry or entry.get("source") != FANIN_INPUT_SOURCE_DIRECT:
        return None
    direct = entry.get("direct")
    if not isinstance(direct, dict):
        return None
    return DirectHealthSample(
        present=bool(direct.get("present", False)),
        health=str(direct.get("health", "")),
        reopens=_as_int(direct.get("reopens")),
        card_gen_reopens=_as_int(direct.get("card_gen_reopens")),
        frames_read=_as_int(entry.get("frames_read")),
    )


def read_fanin_status(
    socket_path: str = FANIN_STATUS_SOCKET,
    *,
    timeout_sec: float = 0.5,
    max_bytes: int = 64 * 1024,
) -> dict[str, Any] | None:
    if timeout_sec <= 0 or max_bytes <= 0:
        return None
    try:
        return read_status_socket(socket_path, timeout=timeout_sec, max_bytes=max_bytes)
    except (OSError, ValueError):
        return None


def fanin_inputs_by_label(
    fanin_status: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Project fan-in's input lanes into one ``{label: lane}`` mapping.

    Accepts either shape a caller may hold: the raw STATUS ``inputs`` list, or
    an ``inputs`` already re-keyed by label (the shape the airplay-health
    snapshot publishes). Fail-soft to ``{}`` — a missing / malformed STATUS,
    an absent ``inputs``, and a lane with no string label all drop out rather
    than raising, so every consumer keys off one projection.
    """

    if not isinstance(fanin_status, dict):
        return {}
    inputs = fanin_status.get("inputs")
    if isinstance(inputs, dict):
        return {
            label: entry
            for label, entry in inputs.items()
            if isinstance(label, str) and isinstance(entry, dict)
        }
    if not isinstance(inputs, list):
        return {}
    return {
        entry["label"]: entry
        for entry in inputs
        if isinstance(entry, dict) and isinstance(entry.get("label"), str)
    }


def fanin_usbsink_input(
    fanin_status: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return fan-in's identity-bound USB input entry, if present.

    Input order is not stable and other lanes may also use ``source=direct`` in
    future.  Bind the selection to the canonical ``label=usbsink`` identity so
    every Python consumer projects the same lane from one STATUS snapshot.
    Malformed snapshots fail soft to ``None``.
    """

    if not isinstance(fanin_status, dict):
        return None
    inputs = fanin_status.get("inputs")
    if not isinstance(inputs, list):
        return None
    for entry in inputs:
        if isinstance(entry, dict) and entry.get("label") == USBSINK_INPUT_LABEL:
            return entry
    return None


def fanin_usbsink_lane_is_direct(fanin_status: dict[str, Any] | None) -> bool:
    """True when fan-in's ``usbsink`` input lane is in DIRECT capture mode.

    Fan-in DIRECT-captures the UAC2 gadget when ``source=="direct"``.  That
    identity-bound lane is the sole live USB ingress and the source for its
    level, mute, resampler, and activity telemetry.

    Fail-soft: a missing / malformed STATUS, an absent ``inputs`` array, or no
    ``usbsink`` lane all return ``False``.
    """

    entry = fanin_usbsink_input(fanin_status)
    return bool(entry and entry.get("source") == FANIN_INPUT_SOURCE_DIRECT)


__all__ = [
    "DIRECT_HEALTH_BROKEN",
    "DIRECT_HEALTH_CAPTURING",
    "DIRECT_HEALTH_IDLE",
    "DirectHealthSample",
    "FANIN_INPUT_SOURCE_DIRECT",
    "USBSINK_INPUT_LABEL",
    "extract_direct_sample",
    "fanin_inputs_by_label",
    "fanin_usbsink_input",
    "fanin_usbsink_lane_is_direct",
    "FANIN_STATUS_SOCKET",
    "read_fanin_status",
]
