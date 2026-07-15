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

import json
import socket
import sys
import time
from typing import Any


# The STATUS input-lane ``source`` value on the USB DIRECT lane. Every
# aloop-reading lane serialises ``source:"lane"``; only the gadget-direct-capture
# lane serialises ``source:"direct"`` (rust/jasper-fanin/src/state.rs, pinned by
# its ``source":"direct"`` unit tests). This is the load-bearing USB signal:
# fan-in owns the gadget capture directly as the sole live ingress owner.
FANIN_INPUT_SOURCE_DIRECT = "direct"
USBSINK_INPUT_LABEL = "usbsink"
FANIN_STATUS_SOCKET = "/run/jasper-fanin/control.sock"


def read_fanin_status(
    socket_path: str = FANIN_STATUS_SOCKET,
    *,
    timeout_sec: float = 0.5,
    max_bytes: int = 64 * 1024,
) -> dict[str, Any] | None:
    """Read one bounded fan-in STATUS snapshot, failing soft to ``None``.

    This is the import-light shared probe for lifecycle/management surfaces.
    It bounds connect/read time and bytes so an unhealthy local daemon cannot
    pin a root coordinator or socket-activated web process.
    """

    if timeout_sec <= 0 or max_bytes <= 0:
        return None
    try:
        deadline = time.monotonic() + timeout_sec
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_sec)
            sock.connect(socket_path)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                chunk = sock.recv(min(8192, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
        return None
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


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


def main(argv: list[str] | None = None) -> int:
    """Small systemd/script probe for the boot-time USB composition gate."""

    args = sys.argv[1:] if argv is None else argv
    if args != ["--usbsink-direct-armed"]:
        print("usage: python -m jasper.fanin.status --usbsink-direct-armed", file=sys.stderr)
        return 2
    armed = fanin_usbsink_lane_is_direct(read_fanin_status(timeout_sec=1.0))
    print(
        "event=fanin.usb_direct_gate result=" + ("armed" if armed else "not_armed"),
        file=sys.stderr,
    )
    return 0 if armed else 1


__all__ = [
    "FANIN_INPUT_SOURCE_DIRECT",
    "USBSINK_INPUT_LABEL",
    "fanin_usbsink_input",
    "fanin_usbsink_lane_is_direct",
    "FANIN_STATUS_SOCKET",
    "read_fanin_status",
]


if __name__ == "__main__":
    raise SystemExit(main())
