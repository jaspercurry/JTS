# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pick the ingress tap the harness must arm for the route that's actually live.

The route-latency harness has two ingress taps to choose from, because the USB
route has two shapes:

* **usbsink bridge** (solo / aloop mode): audio enters through
  ``jasper-usbsink-audio``'s capture, whose tap is armed over HTTP on
  ``127.0.0.1:8781`` and writes ``/run/jasper-usbsink/impulse-tap.jsonl``. This
  is what the harness historically arms.
* **fan-in DIRECT capture** (USB *combo* mode — ``JASPER_FANIN_USB_DIRECT=
  enabled``, the P3/P4 shipped default on an eligible gadget box): audio enters
  through ``jasper-fanin``'s own ``hw:UAC2Gadget`` capture, whose tap is armed
  over the fan-in control UDS (``TAP_ARM`` verb) and writes
  ``/run/jasper-fanin/impulse-tap.jsonl``. **In combo mode the usbsink bridge is
  in standby and opens no capture, so its :8781 tap never fires** — arming it
  against known-good combo audio records zero detections (the reported bug).

This module owns the DECISION of which tap to arm. It is deliberately split into
a pure core (``fanin_direct_lane_active`` / ``resolve_tap_transport`` — no I/O,
fully unit-testable) plus a thin live layer (``probe_fanin_direct_active`` /
``build_resolved_tap``) that reads the fan-in STATUS and constructs the concrete
client. Mirrors :mod:`jasper.fanin.coupling_auto`'s pure-decision + live-wrapper
idiom.

**The combo signal is the daemon's own surface, not an env guess.** The harness
runs as a bare ``sudo`` CLI that does not ``EnvironmentFile=`` ``fanin.env``, so
``JASPER_FANIN_USB_DIRECT`` is not reliably in its environment. Instead the auto
pass reads fan-in ``STATUS`` and looks for an input lane reporting
``source == "direct"`` — the live topology fact that means fan-in owns the gadget
capture and its tap is the one that fires (``rust/jasper-fanin/src/state.rs``
renders ``source:"direct"`` on the USB DIRECT lane). This follows AGENTS.md's
"prefer the daemon's own surface (/state, STATUS) over /etc/* files": it reflects
what the box is ACTUALLY running right now, not merely what an env file intends.

**Fail-safe direction = usbsink** (the historical default). ``auto`` only
resolves to the fan-in tap when it can positively prove a live direct lane; an
unreachable/silent fan-in STATUS resolves to the usbsink tap and says so, and an
operator can always force either transport with ``--tap-transport``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jasper.route_latency.status_socket import (
    FANIN_STATUS_SOCKET,
    read_status_socket_or_none,
)
from jasper.route_latency.tap_client import (
    DEFAULT_TAP_HOST,
    DEFAULT_TAP_PATH,
    DEFAULT_TAP_PORT,
    FANIN_CONTROL_SOCKET,
    FANIN_DEFAULT_TAP_PATH,
    FaninTapClient,
    TapArmer,
    TapClient,
)

# The transport tokens the CLI's --tap-transport accepts.
TAP_TRANSPORT_AUTO = "auto"
TAP_TRANSPORT_USBSINK = "usbsink"
TAP_TRANSPORT_FANIN = "fanin"
TAP_TRANSPORTS = (TAP_TRANSPORT_AUTO, TAP_TRANSPORT_USBSINK, TAP_TRANSPORT_FANIN)

# The STATUS lane marker that means fan-in owns the gadget capture directly (the
# combo/direct route). Pinned to rust/jasper-fanin/src/state.rs's serializer by
# the tap-transport contract test.
FANIN_DIRECT_SOURCE = "direct"


def fanin_direct_lane_active(fanin_status: dict[str, Any] | None) -> bool:
    """True iff fan-in STATUS reports a live DIRECT-capture input lane.

    Pure: takes a parsed fan-in STATUS dict (or ``None`` when unreachable) and
    returns whether any ``inputs[]`` entry has ``source == "direct"`` — the
    marker that fan-in is capturing ``hw:UAC2Gadget`` itself (combo mode), so the
    fan-in tap is the live ingress and the usbsink bridge tap is dead. ``None`` /
    malformed / no-direct-lane all return ``False`` (fail-safe toward the usbsink
    default).
    """

    if not isinstance(fanin_status, dict):
        return False
    inputs = fanin_status.get("inputs")
    if not isinstance(inputs, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("source") == FANIN_DIRECT_SOURCE
        for entry in inputs
    )


def probe_fanin_direct_active(socket_path: str = FANIN_STATUS_SOCKET) -> bool:
    """Live combo probe: read fan-in STATUS and test for a direct-capture lane.

    Fail-soft — an unreachable/malformed STATUS (``read_status_socket_or_none``
    returns ``None``) reads as "no direct lane," so ``auto`` falls back to the
    usbsink tap rather than forcing the fan-in tap on a box it can't prove is in
    combo mode.
    """

    status = read_status_socket_or_none(
        socket_path, event="route_latency.tap_transport_probe"
    )
    return fanin_direct_lane_active(status)


def resolve_tap_transport(choice: str, *, combo_active: bool) -> str:
    """Resolve a ``--tap-transport`` choice to a concrete transport (pure).

    ``auto`` -> ``fanin`` iff ``combo_active`` else ``usbsink`` (the fail-safe
    default). An explicit ``fanin`` / ``usbsink`` passes through unchanged so an
    operator can always override the auto decision. Any unrecognized value fails
    safe to ``usbsink``.
    """

    if choice == TAP_TRANSPORT_FANIN:
        return TAP_TRANSPORT_FANIN
    if choice == TAP_TRANSPORT_USBSINK:
        return TAP_TRANSPORT_USBSINK
    # auto (and any unknown value) -> pick by the live combo signal.
    return TAP_TRANSPORT_FANIN if combo_active else TAP_TRANSPORT_USBSINK


@dataclass(frozen=True)
class ResolvedTap:
    """A concrete tap target: which transport, its arm/disarm client, the JSONL
    path the tap writes (and the harness reads back), and a log-friendly reason.

    ``tap_path`` is authoritative for BOTH ends of the run: it is passed to the
    arm request (so the daemon writes exactly there) AND used as ``--tap-events``
    for the offline analyze, so a combo run reads the fan-in JSONL the fan-in tap
    just wrote — never the stale usbsink path.
    """

    transport: str
    client: TapArmer
    tap_path: str
    reason: str


def build_resolved_tap(
    *,
    transport_choice: str,
    explicit_tap_path: str | None,
    tap_host: str = DEFAULT_TAP_HOST,
    tap_port: int = DEFAULT_TAP_PORT,
    fanin_socket: str = FANIN_CONTROL_SOCKET,
    combo_probe: Callable[[], bool] | None = None,
) -> ResolvedTap:
    """Resolve the transport choice + live box state into a concrete tap target.

    Only probes the fan-in STATUS when ``transport_choice`` is ``auto`` (an
    explicit choice needs no probe). ``combo_probe`` is injected for tests; in
    production it defaults to :func:`probe_fanin_direct_active` against
    ``fanin_socket``. ``explicit_tap_path`` (the operator's ``--tap-path``, or
    ``None``) overrides the transport's default JSONL path either way.
    """

    if transport_choice == TAP_TRANSPORT_AUTO:
        probe = combo_probe or (lambda: probe_fanin_direct_active(fanin_socket))
        combo_active = probe()
        transport = resolve_tap_transport(transport_choice, combo_active=combo_active)
        if transport == TAP_TRANSPORT_FANIN:
            reason = (
                "auto: fan-in STATUS reports a source=direct capture lane "
                "(combo box — the usbsink bridge tap is dead in combo mode)"
            )
        else:
            reason = (
                "auto: no fan-in direct-capture lane in STATUS "
                "(usbsink bridge tap; fan-in STATUS aloop-only or unreachable)"
            )
    else:
        transport = resolve_tap_transport(transport_choice, combo_active=False)
        reason = f"forced via --tap-transport {transport}"

    if transport == TAP_TRANSPORT_FANIN:
        return ResolvedTap(
            transport=TAP_TRANSPORT_FANIN,
            client=FaninTapClient(socket_path=fanin_socket),
            tap_path=explicit_tap_path or FANIN_DEFAULT_TAP_PATH,
            reason=reason,
        )
    return ResolvedTap(
        transport=TAP_TRANSPORT_USBSINK,
        client=TapClient(host=tap_host, port=tap_port),
        tap_path=explicit_tap_path or DEFAULT_TAP_PATH,
        reason=reason,
    )


__all__ = [
    "FANIN_DIRECT_SOURCE",
    "TAP_TRANSPORTS",
    "TAP_TRANSPORT_AUTO",
    "TAP_TRANSPORT_FANIN",
    "TAP_TRANSPORT_USBSINK",
    "ResolvedTap",
    "build_resolved_tap",
    "fanin_direct_lane_active",
    "probe_fanin_direct_active",
    "resolve_tap_transport",
]
